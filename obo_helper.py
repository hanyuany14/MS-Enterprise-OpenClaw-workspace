"""
OBO Helper - 通用 On-Behalf-Of Token Exchange
====================================================================
支援透過 Scope Registry 一次交換多組 resource token，
新增 resource 只需改環境變數 OBO_SCOPE_REGISTRY，不動程式碼。

Scope Registry 格式（JSON 環境變數）：
{
    "FABRIC_SQL_ACCESS_TOKEN": "https://database.windows.net/.default",
    "GRAPH_ACCESS_TOKEN": "https://graph.microsoft.com/.default",
    "AI_SEARCH_ACCESS_TOKEN": "https://search.azure.com/.default"
}
key = 注入到 state.user_data 的環境變數名稱
value = OBO 要換的 scope

也可用 exchange_single() 做單一 scope 的 on-demand exchange。

所需環境變數：
- AZURE_TENANT_ID
- OBO_CLIENT_ID       (此 ACA App Registration 的 Client ID)
- OBO_CLIENT_SECRET   (Client Secret；也可改用 certificate，見下方說明)
- OBO_SCOPE_REGISTRY  (JSON string，定義要交換的 scopes)

VERSION: 1.0
2026.03.17 George: v1.0 初版 — 通用 OBO exchange，支援多 scope registry
"""

import os
import json
import logging
from typing import Dict, Optional

from azure.identity.aio import OnBehalfOfCredential

logger = logging.getLogger(__name__)

# ============================================================================
# SCOPE REGISTRY
# ============================================================================

_DEFAULT_REGISTRY = {
    # 預設空 registry — 由環境變數 OBO_SCOPE_REGISTRY 覆蓋
}

# 已知 resource scope 對照表（方便 CodingAgent instructions 引用）
WELL_KNOWN_SCOPES = {
    "fabric_sql": "https://database.windows.net/.default",
    "graph": "https://graph.microsoft.com/.default",
    "ai_search": "https://search.azure.com/.default",
    "key_vault": "https://vault.azure.net/.default",
    "storage": "https://storage.azure.com/.default",
    "management": "https://management.azure.com/.default",
}


def _load_scope_registry() -> Dict[str, str]:
    """
    從環境變數 OBO_SCOPE_REGISTRY 載入 scope registry。
    若未設定，回傳空 dict（不做任何 OBO exchange）。
    """
    raw = os.environ.get("OBO_SCOPE_REGISTRY", "")
    if not raw.strip():
        return _DEFAULT_REGISTRY.copy()

    try:
        registry = json.loads(raw)
        if not isinstance(registry, dict):
            logger.error(f"[OBO] OBO_SCOPE_REGISTRY is not a JSON object: {type(registry)}")
            return {}
        logger.info(f"[OBO] Scope registry loaded: {list(registry.keys())}")
        return registry
    except json.JSONDecodeError as e:
        logger.error(f"[OBO] Failed to parse OBO_SCOPE_REGISTRY: {e}")
        return {}


def _get_obo_config() -> Dict[str, str]:
    """
    讀取 OBO 所需的 Entra ID 設定。
    缺少任一設定時 raise ValueError。
    """
    tenant_id = os.environ.get("AZURE_TENANT_ID", "")
    client_id = os.environ.get("OBO_CLIENT_ID", "")
    client_secret = os.environ.get("OBO_CLIENT_SECRET", "")

    missing = []
    if not tenant_id:
        missing.append("AZURE_TENANT_ID")
    if not client_id:
        missing.append("OBO_CLIENT_ID")
    if not client_secret:
        missing.append("OBO_CLIENT_SECRET")

    if missing:
        raise ValueError(
            f"[OBO] Missing required environment variables: {', '.join(missing)}. "
            f"OBO token exchange cannot proceed."
        )

    return {
        "tenant_id": tenant_id,
        "client_id": client_id,
        "client_secret": client_secret,
    }


# ============================================================================
# SINGLE SCOPE EXCHANGE
# ============================================================================

async def exchange_single(
    user_access_token: str,
    scope: str,
) -> str:
    """
    對單一 scope 做 OBO token exchange。

    Args:
        user_access_token: Caller 傳入的 user Bearer token
        scope: 目標 resource scope (e.g. "https://database.windows.net/.default")

    Returns:
        交換後的 access token string

    Raises:
        ValueError: 缺少 OBO 設定
        Exception: Azure Identity SDK 的認證錯誤
    """
    config = _get_obo_config()

    credential = OnBehalfOfCredential(
        tenant_id=config["tenant_id"],
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        user_assertion=user_access_token,
    )
    try:
        token = await credential.get_token(scope)
        logger.info(
            f"[OBO] Token exchanged for scope={scope}, "
            f"expires_on={token.expires_on}"
        )
        return token.token
    finally:
        await credential.close()


# ============================================================================
# BATCH EXCHANGE（依 Scope Registry）
# ============================================================================

async def exchange_all(
    user_access_token: str,
    registry: Dict[str, str] = None,
) -> Dict[str, str]:
    """
    依據 Scope Registry，對每個 scope 做 OBO exchange，
    回傳 {env_var_name: access_token} dict。

    失敗的 scope 會 log warning 但不中斷，
    讓不需要該 resource 的 workflow 照常執行。

    Args:
        user_access_token: Caller 傳入的 user Bearer token
        registry: 可選，覆蓋環境變數中的 registry（用於測試）

    Returns:
        Dict[str, str] — {環境變數名稱: token}
        只包含成功交換的 token
    """
    if registry is None:
        registry = _load_scope_registry()

    if not registry:
        logger.info("[OBO] No scopes in registry, skipping OBO exchange")
        return {}

    tokens = {}
    config = _get_obo_config()

    for env_var_name, scope in registry.items():
        credential = OnBehalfOfCredential(
            tenant_id=config["tenant_id"],
            client_id=config["client_id"],
            client_secret=config["client_secret"],
            user_assertion=user_access_token,
        )
        try:
            token = await credential.get_token(scope)
            tokens[env_var_name] = token.token
            logger.info(
                f"[OBO] ✓ {env_var_name} ← {scope} "
                f"(expires_on={token.expires_on})"
            )
        except Exception as e:
            # 不中斷 — 該 scope 可能這輪用不到
            logger.warning(
                f"[OBO] ✗ {env_var_name} ← {scope} FAILED: {e}"
            )
        finally:
            await credential.close()

    logger.info(
        f"[OBO] Exchange complete: {len(tokens)}/{len(registry)} scopes succeeded"
    )
    return tokens
