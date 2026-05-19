"""
ACA Environment Variable Inspector
====================================================================
查詢指定 Azure Container App 當前 revision 的 environment variables,
供 SKILL.md Generator v2 在 Step 2.5(env var 收集)階段使用。

設計目標:
- 提供 Foundry Agent 兩份資訊:
    1. 當前部署環境已存在的變數名清單 —— 供避免命名衝突 / 沿用既有變數
    2. 架構配置(architectural_config)的完整內容 —— 例如 OBO_SCOPE_REGISTRY,
       Agent 需要實際值才能決定 skill 所需 token 是否已註冊
- 絕不洩漏 secret 實際值。一般變數只回傳 name,架構配置走白名單。

認證:
  使用 ACA Container App 的 System Managed Identity,
  需要在 target subscription 上授予 "Container Apps Reader"
  內建 role(或等效的最小權限 custom role)。
  這符合租戶全面禁用 API key 的合規要求。

依賴:
  pip install azure-mgmt-appcontainers azure-identity

VERSION: 2.0
2026.04.15 George: v1.0 初版
2026.04.16 George: v2.0 大幅精簡 ——
  移除 group_by_purpose、naming_conventions、sample_value、
  inferred_purpose、value_type、secret_ref。Generator 實際只需要
  變數名清單 + 架構配置實值,其餘欄位 Agent 能從 name 自行推斷,
  保留它們只是增加 token 噪音與維護成本。
"""

import os
import re
import json
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================================
# ACA 系統注入變數過濾
# Container Apps runtime 會自動注入一批 CONTAINER_APP_* 變數,
# 這些不是開發者設的,Agent 若誤以為是可沿用的變數會產生錯誤 skill code,
# 必須過濾掉。
# ============================================================================

_SYSTEM_VAR_PATTERNS = [
    re.compile(r"^CONTAINER_APP_"),
    re.compile(r"^HOSTNAME$"),
    re.compile(r"^PATH$"),
    re.compile(r"^HOME$"),
    re.compile(r"^PWD$"),
    re.compile(r"^PORT$"),
]


def _is_system_var(name: str) -> bool:
    return any(p.match(name) for p in _SYSTEM_VAR_PATTERNS)


# ============================================================================
# Architectural Config 白名單
#
# 這類變數的值「本身就是架構配置」,不是 secret。Agent 做 SKILL.md 生成決策
# 時必須看到完整值,甚至 parse 成結構化物件,才能推理。
#
# 安全邊界:
#   - 明確列舉(allowlist),絕不使用「自動偵測 JSON 就放行」這類規則。
#   - 列入白名單的變數必須滿足:值本身是公開的架構識別符
#     (scope URI、resource name、routing key),不含任何 credential。
#
# 擴充原則:
#   只在 Generator 實際跑起來、發現特定變數「沒有值 Agent 無法推理」時,
#   才加入新項目。避免過度設計。
# ============================================================================

_ARCHITECTURAL_CONFIG_VARS: dict[str, dict] = {
    # OBO Scope Registry ——
    # MS OpenClaw 的能力宣告清單,每個 key 對應 skill sample code 裡
    # credentials dict 的 token key,value 是目標資源的 scope URI。
    # 所有 value 都是 Microsoft 公開文件中的標準 scope。
    # Generator 必須看到完整內容才能決定:
    #   1. 要生成的 skill 所需的 token 是否已註冊
    #   2. sample code 該用哪個 credentials[...] key 取 token
    "OBO_SCOPE_REGISTRY": {"format": "json"},
}


def _try_parse_architectural_value(raw: Optional[str], fmt: str):
    """
    按宣告的格式解析架構 config 值。
    解析失敗時回傳原始字串(不拋例外)並記錄 warning ——
    允許 Agent 仍能看到值,只是失去結構化優勢。
    """
    if raw is None or raw == "":
        return None
    if fmt == "json":
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning(
                f"[aca_env] Architectural config parse failed "
                f"(format=json): {e}. Returning raw string."
            )
            return raw
    return raw


# ============================================================================
# Azure SDK 呼叫 + 快取
#
# ACA revision 很少變,以 (subscription_id, resource_group, app_name) 為 cache
# key,TTL = 300 秒。Generator 一次生成可能查詢多次,快取能顯著降低對 ARM API
# 的重複呼叫。
# ============================================================================

_cache: dict[tuple, tuple[float, tuple]] = {}
_CACHE_TTL_SEC = 300


async def _fetch_raw_env(
    subscription_id: str,
    resource_group: str,
    app_name: str,
    credential,
) -> tuple[list, str]:
    """
    呼叫 Azure SDK 取得 container app 的 env var 原始結構。

    Returns:
        (env_list, revision_name)
        env_list 是 ContainerAppContainerEnvVar 物件列表,
        每個物件有 .name / .value / .secret_ref 三個屬性。
    """
    cache_key = (subscription_id, resource_group, app_name)
    now = time.monotonic()

    cached = _cache.get(cache_key)
    if cached and (now - cached[0]) < _CACHE_TTL_SEC:
        logger.info(f"[aca_env] Cache hit for {app_name}")
        return cached[1]

    # Lazy import —— 避免在不使用此 tool 時付出 import 成本
    from azure.mgmt.appcontainers.aio import ContainerAppsAPIClient

    logger.info(
        f"[aca_env] Fetching env from ACA "
        f"sub={subscription_id[:8]}... rg={resource_group} app={app_name}"
    )

    async with ContainerAppsAPIClient(credential, subscription_id) as client:
        app = await client.container_apps.get(resource_group, app_name)

    # 取第一個 container 的 env(MS OpenClaw 所有 agent 都是 single-container)
    containers = app.template.containers if app.template else []
    if not containers:
        raise RuntimeError(f"Container app '{app_name}' has no containers")

    env_list = containers[0].env or []
    revision_name = app.latest_revision_name or "unknown"

    result = (env_list, revision_name)
    _cache[cache_key] = (now, result)
    return result


# ============================================================================
# 主入口
# ============================================================================

async def list_environment_variables(
    app_name: str,
    resource_group: Optional[str] = None,
    subscription_id: Optional[str] = None,
    include_system_vars: bool = False,
    credential=None,
) -> dict:
    """
    查詢指定 ACA app 當前 revision 的環境變數。

    Args:
        app_name: ACA container app 名稱(必填)。
        resource_group: Resource group 名稱。若為 None,從環境變數
            ACA_INSPECTOR_DEFAULT_RG 讀取。
        subscription_id: Azure subscription ID。若為 None,從環境變數
            ACA_INSPECTOR_DEFAULT_SUB 讀取。
        include_system_vars: 是否包含 ACA runtime 注入的系統變數
            (CONTAINER_APP_*, PATH, PORT 等)。預設 False。
        credential: Azure credential 物件。若為 None,使用
            DefaultAzureCredential(ACA 部署時會自動採用 System MI)。

    Returns:
        {
            "revision": "<latest revision name,供 debug trace>",
            "variables": ["VAR_NAME_1", "VAR_NAME_2", ...],
            "architectural_config": {
                "OBO_SCOPE_REGISTRY": { ...parsed JSON... },
                ...
            },
        }
        絕不包含 secret 實際值。
    """
    resource_group = resource_group or os.environ.get("ACA_INSPECTOR_DEFAULT_RG")
    subscription_id = subscription_id or os.environ.get("ACA_INSPECTOR_DEFAULT_SUB")
    if not resource_group or not subscription_id:
        raise ValueError(
            "resource_group / subscription_id 未指定,"
            "且環境變數 ACA_INSPECTOR_DEFAULT_RG / ACA_INSPECTOR_DEFAULT_SUB 未設定"
        )

    if credential is None:
        from azure.identity.aio import DefaultAzureCredential
        credential = DefaultAzureCredential()

    env_list, revision = await _fetch_raw_env(
        subscription_id, resource_group, app_name, credential
    )

    variables: list[str] = []
    architectural_config: dict = {}

    for e in env_list:
        name = getattr(e, "name", None)
        if not name:
            continue
        if not include_system_vars and _is_system_var(name):
            continue

        variables.append(name)

        # 架構配置白名單:回傳 parse 後的完整值
        arch_meta = _ARCHITECTURAL_CONFIG_VARS.get(name)
        if arch_meta is not None:
            raw_value = getattr(e, "value", None)
            secret_ref = getattr(e, "secret_ref", None)
            if secret_ref:
                # 異常情境:架構 config 不該走 secretRef
                logger.warning(
                    f"[aca_env] Architectural config '{name}' is stored "
                    f"as secretRef — this is unusual. Agent may lack "
                    f"structural context."
                )
                architectural_config[name] = None
            else:
                architectural_config[name] = _try_parse_architectural_value(
                    raw_value, arch_meta["format"]
                )

    variables.sort()

    return {
        "revision": revision,
        "variables": variables,
        "architectural_config": architectural_config,
    }


def clear_cache() -> None:
    """清除模組級快取。測試或強制重新查詢時使用。"""
    _cache.clear()
    logger.info("[aca_env] Cache cleared")