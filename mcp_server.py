"""
MCP Server + REST API - Custom ACA Deployment (Streamable HTTP)
====================================================================
同時提供 MCP 標準協議 和 REST API 的統一 HTTP server，
部署在 Custom ACA 上，單一 process、單一 port。

MCP 層使用 Streamable HTTP transport（新版推薦 transport），
任何 MCP Client（Claude Desktop、APIM MCP Gateway、其他 Agent）
均可透過標準 MCP 協議連線。

REST 層保留原有的 HTTP API，供不支援 MCP 的 caller 使用
（如 Copilot Studio、Power Automate、直接 HTTP 呼叫）。

核心邏輯（workflow 執行、session 管理、metadata persist）在 core_handler.py 中，
本檔案只負責：
- MCP Server 定義（FastMCP from official mcp SDK）+ MCP Tool 註冊
- REST API routes（FastAPI）
- Streamable HTTP transport 啟動
- Health check endpoint

URL 路由總覽（全部共用同一個 port）：
  ┌─ MCP Protocol ─────────────────────────────┐
  │  POST /mcp  — MCP Streamable HTTP       │
  │                   (JSON-RPC 2.0)             │
  │  ※ mount("/mcp") + 預設 sub-path "/mcp"     │
  │    = 完整路徑 /mcp                       │
  └────────────────────────────────────────────┘
  ┌─ REST API ─────────────────────────────────┐
  │  POST /run      — 執行 coding workflow      │
  │  POST /clear    — 清除 session metadata     │
  │  GET  /skills   — 列出本地 skills（debug）   │
  │  POST /sync     — 強制同步 skills from Blob │
  │  POST /api/skills/approve — HITL 審核通過   │
  │  POST /api/skills/reject  — HITL 審核拒絕   │
  └────────────────────────────────────────────┘
  ┌─ Infrastructure ───────────────────────────┐
  │  GET  /health   — Health check              │
  │                   (ACA liveness probe)       │
  └────────────────────────────────────────────┘

MCP Client 連線設定範例:
{
    "mcpServers": {
        "code-agent": {
            "type": "streamable-http",
            "url": "https://your-aca.azurecontainerapps.io/mcp"
        }
    }
}

REST Client 呼叫範例:
  POST https://your-aca.azurecontainerapps.io/run
  Body: {"request": "幫我寫一個 Blob 上傳腳本", "session_id": "abc-123"}

VERSION: 2.5
2026.05.18 George × Claude: v2.5 Phase 3 — Adaptive Timeout Escalation
- 新增 MCP tool: check_pending_tasks(session_id, max_wait) — 使用者問「好了嗎」
  時 Helper Agent 呼叫,long polling 等任務完成或回 still_running
- 新增 MCP tool: cancel_pending_task(session_id) — 使用者表達取消意圖時
  Helper Agent 呼叫,雙路徑取消 (Job Store flag + executor SIGTERM)
- _build_response_payload 加非完成狀態透傳分支:running / rejected /
  cancelling / cancelled / no_running_task / still_running / error 七種
  status 直接透傳 result dict,既有 success/needs_input/failed 邏輯不變
- 不提供 REST 版 (Q3 決策):兩個新 tool 只走 MCP,Helper Agent 走 MCP 連線,
  REST /run 給不會講 MCP 的舊 client 用,沒這兩個查詢/取消需求

VERSION: 2.4.1
2026.04.16 George: v2.4.1 list_aca_environment_variables 同步 aca_env_inspector v2.0 精簡
- 移除 group_by_purpose 參數（inspector 已不支援）
- 移除 docstring 中 group_by_purpose、value_type、secret_ref、
  sample_value、inferred_purpose、naming_conventions_detected 欄位說明
- 回傳結構改為 variable_names 扁平清單

2026.04.15 George: v2.4 新增 list_aca_environment_variables MCP tool
- 新增 ACA Env Var Inspector 整合（aca_env_inspector module）
- 供 SKILL.md Generator v2 Step 2.5 使用

2026.03.30 George: v2.3 HITL Skill Review REST Endpoints
- 新增 POST /api/skills/approve — Logic App callback，審核通過
- 新增 POST /api/skills/reject  — Logic App callback，審核拒絕
- 純 REST API，與 MCP protocol 無關，掛在同一個 FastAPI app 上

2026.03.15 George: v2.1a 修正 — 使用官方 mcp SDK API
- 修正 AttributeError: 'FastMCP' has no attribute 'http_app'
- http_app() 是第三方 fastmcp (PrefectHQ) 的 API
- 官方 mcp SDK (modelcontextprotocol/python-sdk) 使用:
    mcp.streamable_http_app()  — 取得 ASGI sub-app
    mcp.session_manager.run()  — 作為 lifespan context manager
- MCP endpoint 完整路徑: /mcp/mcp
  (FastAPI mount point "/mcp" + SDK 預設 sub-path "/mcp")
- 需要 mcp>=1.8.0（支援 Streamable HTTP transport）

2026.03.14 George: v2.1 合併版 — MCP + REST 同一個 FastAPI app
2026.03.14 George: v2.0 改造為標準 MCP Server (Streamable HTTP)
2026.03.13 George: v1.1 response 加上 session_hint + turn_count
2026.03.12 George: v1.0 初版 (aiohttp)

依賴：
- mcp>=1.8.0 (官方 Python MCP SDK, 含 FastMCP + Streamable HTTP)
- uvicorn (ASGI server)
- fastapi (REST routes + custom endpoints)
- core_handler (本專案的共用核心)

啟動方式：
  uvicorn mcp_server:app --host 0.0.0.0 --port 8080
  或
  python mcp_server.py
"""

import logging
import sys
import os
import json
import contextlib
import contextvars  # 2026.03.17 George: Identity Passthrough

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stdout,
    force=True,
)

# 2026.03.15 George: v2.1a 使用官方 mcp SDK 的 FastMCP
# 注意：這是 from mcp.server.fastmcp import FastMCP（官方 SDK），
# 不是 from fastmcp import FastMCP（第三方 PrefectHQ 套件）。
# 兩者 API 不同：
#   官方: mcp.streamable_http_app(), mcp.session_manager.run()
#   第三方: mcp.http_app(path=...)
from mcp.server.fastmcp import FastMCP
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from mcp.server.transport_security import TransportSecuritySettings

import core_handler
# 2026.04.15 George: v2.4 新增 ACA env var inspector
# 獨立 module,職責為查詢 ACA container app 當前 env var 清單,
# 供 SKILL.md Generator v2 Step 2.5 判斷是否有既有變數可沿用。
import aca_env_inspector
import asyncio
import uuid
import time

try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv())
except ImportError:
    pass

logger = logging.getLogger(__name__)

# HTTP server port（與 Foundry 的 8088 區隔）
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))


# ============================================================================
# MCP SERVER 定義
# 2026.03.14 George: v2.0 使用 FastMCP + Streamable HTTP
# 2026.03.15 George: v2.1a 確認使用官方 SDK API
# - stateless_http=True: ACA 多 replica 不共享 MCP session state，
#   避免 load balancer 導向不同 replica 時出現 "session not found"
# - json_response=True: tool 結果以純 JSON 回傳（不走 SSE stream），
#   簡化 client 端處理。若未來需要 progress notification 可拿掉此選項。
# ============================================================================

mcp = FastMCP(
    "code-agent",
    stateless_http=True,
    json_response=True,
    # 2026.03.15 George: v2.1a 關閉 DNS rebinding 防護
    # ACA 部署場景下，安全性由 ACA ingress + APIM 層負責
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),    
)

# 2026.03.17 George: v2.2 Identity Passthrough
# 從 HTTP Authorization header 提取的 user Bearer token，
# 存入 contextvars 供 MCP tool / REST handler 使用。
_current_user_token: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_current_user_token", default=""
)


# ============================================================================
# MCP TOOLS
# 2026.03.14 George: v2.0 將原 REST endpoints 註冊為 MCP tools
# MCP Client 透過 tools/list 動態發現可用 tools，
# 透過 tools/call 呼叫，回傳 CallToolResult。
# ============================================================================

@mcp.tool()
async def run_coding_workflow(
    request: str,
    session_id: str = "",
    credentials: str = "",
) -> str:
    """
    執行 Python coding workflow。根據用戶的自然語言需求，
    自動生成程式碼、執行、除錯，直到成功為止。

    Args:
        request: 用戶的程式碼需求描述（自然語言）。
                 例如：「幫我寫一個 Azure Blob 上傳腳本」
        session_id: 可選。帶回前一輪回傳的 session_id 以延續對話上下文
                    （包含 turn history 和 final code）。
                    若為空字串則建立新 session。
        credentials: 可選。JSON 格式的 credentials 字串，將注入為執行環境的環境變數。
                     例如：'{"API_KEY": "xxx", "ENDPOINT": "https://..."}'
                     若為空字串則不注入。

    Returns:
        JSON 字串，包含：
        - status: "completed" | "needs_input" | "failed"
        - response: workflow 的回應文字（程式碼執行結果或需要更多資訊的提示）
        - session_id: 本次 session ID，下輪帶回以延續上下文
        - skills_referenced: 本次 workflow 參考的 skills 列表    
        - session_hint: 提示 caller 需要回帶 session_id
        - turn_count: 累積的對話輪次數
        - uploads: 產出檔案的下載連結列表
    """
    # 2026.03.14 George: v2.0
    # MCP tool 參數只支援 primitive types（str, int, float, bool），
    # 因此 credentials 從原本的 dict 改為 JSON string，在此解析。
    parsed_credentials = None
    if credentials and credentials.strip():
        try:
            parsed_credentials = json.loads(credentials)
        except json.JSONDecodeError as e:
            return json.dumps({
                "status": "failed",
                "error": f"Invalid credentials JSON: {e}",
            }, ensure_ascii=False)

    effective_session_id = session_id if session_id and session_id.strip() else None

    # 2026.03.17 George: v2.2 Identity Passthrough — 注入 user token
    user_token = _current_user_token.get("")
    if user_token:
        if parsed_credentials is None:
            parsed_credentials = {}
        parsed_credentials["__user_token"] = user_token

    logger.info(
        f"[MCP] run_coding_workflow session={effective_session_id or '(new)'}, "
        f"input={request[:100]}..."
    )

    try:
        result = await core_handler.run_workflow(
            request,
            session_id=effective_session_id,
            credentials=parsed_credentials,
        )

        return json.dumps(_build_response_payload(result), ensure_ascii=False)

    except Exception as e:
        logger.error(f"[MCP] Workflow error: {e}", exc_info=True)
        return json.dumps({
            "status": "failed",
            "error": str(e),
        }, ensure_ascii=False)


# ============================================================================
# Phase 3 NEW TOOLS: check_pending_tasks / cancel_pending_task
# 2026.05.18 George × Claude: v2.5 Phase 3 — Adaptive Timeout Escalation
#
# 對應主文件 v3 §5.3 (check_pending_tasks) / §5.7 (cancel_pending_task)。
# 這兩個 tool 是 run_coding_workflow 改造為 adaptive timeout 後的搭配工具:
# - run_coding_workflow 回 status="running" 後,使用者問「好了嗎」時,
#   Helper Agent 呼叫 check_pending_tasks
# - 使用者說「取消」「不要做了」時,Helper Agent 呼叫 cancel_pending_task
#
# 業務邏輯在 core_handler.py (cancel_pending_task / check_pending_tasks),
# 這兩個 MCP tool 是薄殼 — 只負責參數驗證 + 呼叫 core_handler + 包 JSON。
#
# 不提供 REST 版 (決策摘要 Q3 已敲定),只暴露 MCP。Helper Agent 走 MCP
# 連線,REST /run 是給不會講 MCP 的舊 client 用,那條路徑沒這兩個需求。
#
# Identity / 權限:
# - 兩個 tool 不需要 OBO token (只動 Job Store + executor,不打下游 API)
# - session_id 作為授權邊界 (UUID v4 熵 122 bits,實務上猜不到);
#   Helper Agent 是平台層信任的 client,只會傳當前 session 的 session_id
# - Phase 5 視需求評估加 user_id 比對檢查 (本階段 Job.user_id 寫 None,
#   即使加檢查也是 no-op)
# ============================================================================


@mcp.tool()
async def check_pending_tasks(session_id: str, max_wait: int = 80) -> str:
    """
    查詢指定 session 內是否有正在執行的長任務,並等待其完成。

    使用情境:當 run_coding_workflow 回傳 status="running" 後,
    使用者問「好了嗎」「進度如何」「執行完了嗎」等查詢進度的話時,
    Helper Agent 應呼叫此 tool。

    行為:
    - 若 session 內沒有 running 任務,立即回傳 no_running_task
    - 若有 running 任務,進行 long polling 最多 max_wait 秒,等到任務完成
      (或 timeout 仍未完成則回 still_running,讓使用者再等一下)

    Args:
        session_id: 必填。要查詢的 session ID,從前一輪 run_coding_workflow
                    或 check_pending_tasks 回傳的 session_id 帶回來。
        max_wait: 可選。最多等多少秒,預設 80 秒。
                  超過時間任務仍未完成則回 still_running。

    Returns:
        JSON 字串,可能的 status 值:
        - "completed" / "needs_input" / "failed": 任務已完成,包含 response
        - "no_running_task": session 內沒有進行中的任務
        - "still_running": long polling 已等過 max_wait 秒,任務仍未完成
        - "cancelled": 任務被取消 (使用者在 cancel 後又留在 thread 內查詢)
        - "failed": 異常情況,包含 error 訊息

    Helper Agent 行為提示:
    - 若回 no_running_task,告知使用者目前沒有進行中的任務,
      不要重新 submit (使用者本意是查狀態,不是發新任務)
    - 若回 still_running,告知使用者任務還在跑,可以稍等或離開
      (完成時會 Teams 通知)
    """
    if not session_id or not session_id.strip():
        return json.dumps({
            "status": "failed",
            "error": "Missing 'session_id'",
        }, ensure_ascii=False)

    # max_wait 防呆:負數或過大都修正
    if max_wait < 1:
        max_wait = 1
    elif max_wait > 300:
        max_wait = 300  # 上限 5 分鐘,避免 MCP client 長時間掛著

    logger.info(
        f"[MCP] check_pending_tasks session={session_id}, max_wait={max_wait}"
    )

    try:
        result = await core_handler.check_pending_tasks(
            session_id.strip(),
            max_wait=max_wait,
        )
        return json.dumps(_build_response_payload(result), ensure_ascii=False)

    except Exception as e:
        logger.error(f"[MCP] check_pending_tasks error: {e}", exc_info=True)
        return json.dumps({
            "status": "failed",
            "error": str(e),
        }, ensure_ascii=False)


@mcp.tool()
async def cancel_pending_task(session_id: str) -> str:
    """
    取消指定 session 內正在執行的長任務。

    使用情境:使用者表達取消意圖時,例如「取消」「不要做了」「算了」
    「中斷一下」等,Helper Agent 應呼叫此 tool。

    取消機制 (主文件 §5.7.2 cooperative cancellation):
    1. 在 Job Store 寫入 cancel flag,讓 turn loop 在下個 turn 邊界看到
    2. 直接對正在跑的 subprocess 發 SIGTERM (給 30 秒優雅收尾),逾時 SIGKILL
    兩個動作都會做,確保任務一定停下來,但不保證瞬間結束。

    回傳的是「已發送取消請求」,不等任務真的結束 — Helper Agent 收到
    cancelling 後告知使用者「取消請求已送出,正在收尾」即可。

    邊界情境:
    - 若取消請求送出時任務剛好完成,任務會以 completed 狀態結束 (完成優先),
      Helper Agent 應如實告知「取消太慢,任務已經完成,結果如下」
    - 若 session 內沒有 running 任務,回 no_running_task

    Args:
        session_id: 必填。要取消的 session ID,從 run_coding_workflow 或
                    check_pending_tasks 回傳的 session_id 帶回來。

    Returns:
        JSON 字串,可能的 status 值:
        - "cancelling": 取消請求已送出,任務即將停下
        - "no_running_task": session 內沒有進行中的任務
        - "failed": 異常情況,包含 error 訊息

    Helper Agent 行為提示:
    - cancelling 後若使用者要立即 submit 新任務,等幾秒再 run_coding_workflow,
      讓 cancel 確認生效 (否則新任務會被 §5.5 並行檢查擋下)
    """
    if not session_id or not session_id.strip():
        return json.dumps({
            "status": "failed",
            "error": "Missing 'session_id'",
        }, ensure_ascii=False)

    logger.info(f"[MCP] cancel_pending_task session={session_id}")

    try:
        result = await core_handler.cancel_pending_task(session_id.strip())
        return json.dumps(_build_response_payload(result), ensure_ascii=False)

    except Exception as e:
        logger.error(f"[MCP] cancel_pending_task error: {e}", exc_info=True)
        return json.dumps({
            "status": "failed",
            "error": str(e),
        }, ensure_ascii=False)


@mcp.tool()
async def clear_session(session_id: str) -> str:
    """
    清除指定 session 的對話歷史與 metadata。
    清除後該 session_id 將無法延續之前的上下文。

    Args:
        session_id: 要清除的 session ID。

    Returns:
        操作結果訊息。
    """
    if not session_id or not session_id.strip():
        return json.dumps({
            "status": "failed",
            "error": "Missing 'session_id'",
        }, ensure_ascii=False)

    result_text = await core_handler.clear_session(session_id)
    return json.dumps({
        "status": "completed",
        "response": result_text,
    }, ensure_ascii=False)


@mcp.tool()
async def list_skills() -> str:
    """
    列出 Coding Agent 目前可用的 Skills 清單，包含每個 skill 的 name 與 description。

    此 tool 提供 runtime skill discovery，用於讓上游 Agent（如 Meeting Insight Agent）
    在生成 suggested_ai_actions 時能以最新的 skill 清單作為 grounding 依據，
    避免依賴 hardcoded 或過時的 skill catalog。

    Returns:
        Markdown 格式的 skill 清單，每項為 "- **skill_name**: description"。
    """
    skills_text = core_handler.list_skills()
    return json.dumps({
        "status": "completed",
        "response": skills_text,
    }, ensure_ascii=False)


@mcp.tool()
async def sync_skills() -> str:
    """
    強制從 Azure Blob Storage 重新同步 Skills 到本地。
    用於 Blob 上的 skills 有更新時，手動觸發同步。

    Returns:
        同步結果訊息，包含載入的 skill 數量及目錄列表。
    """
    sync_text = await core_handler.sync_skills()
    return json.dumps({
        "status": "completed",
        "response": sync_text,
    }, ensure_ascii=False)


# ============================================================================
# 2026.04.15 George: v2.4 ACA Env Var Inspector Tool
#
# 用途:給 SKILL.md Generator v2 的 Step 2.5(env var 收集)階段使用。
# 主要動機是「沿用既有變數」—— 當 Foundry Agent 生成新 skill 的 sample
# code 時,若需要用到某個 config(例如 Foundry endpoint、Fabric SQL 連線
# 字串),應優先檢查目標 ACA app 是否已經有對應的 env var,避免:
#   1. 重複定義造成維運混亂
#   2. 命名慣例漂移(同一概念出現多個 key)
#
# 次要動機是「命名衝突偵測」與「命名慣例學習」—— 搭配 Foundry Memory
# Store 的歷史命名慣例做雙向校驗。
#
# 設計重點:
#   - 唯讀。使用 System MI + Container Apps Reader role,最小權限。
#   - 絕不回傳 secret 實際值,只回傳 secret_ref 名稱。
#   - 輸出按用途分組(azure_ai_foundry / fabric_sql / ms_graph / ...)
#     而非扁平清單,優化 LLM 推理。
#   - 有 5 分鐘快取,避免 SKILL.md 生成一次對 ARM API 爆打。
#
# 權限設定(部署時一次性):
#   az role assignment create \
#       --assignee <mcp-server-app-principal-id> \
#       --role "Container Apps Reader" \
#       --scope /subscriptions/<sub>/resourceGroups/<rg>
#
# 環境變數(可選,設定後就不用每次呼叫都指定):
#   ACA_INSPECTOR_DEFAULT_SUB — 預設 subscription ID
#   ACA_INSPECTOR_DEFAULT_RG  — 預設 resource group
# ============================================================================

@mcp.tool()
async def list_aca_environment_variables(
    app_name: str,
    resource_group: str = "",
    subscription_id: str = "",
    include_system_vars: bool = False,
) -> str:
    """
    查詢指定 Azure Container App 當前 revision 的環境變數清單,
    用於 SKILL.md 生成時判斷是否有既有變數可直接沿用,或避免命名衝突。

    Args:
        app_name: ACA container app 名稱,例如 "openclaw-helper-agent"。
        resource_group: 可選。Resource group 名稱。若為空字串則使用
                        環境變數 ACA_INSPECTOR_DEFAULT_RG。
        subscription_id: 可選。Azure subscription ID。若為空字串則使用
                         環境變數 ACA_INSPECTOR_DEFAULT_SUB。
        include_system_vars: 是否包含 ACA runtime 自動注入的系統變數
                             (CONTAINER_APP_*, PATH, PORT 等)。
                             預設 False —— 這些對 SKILL.md 無參考價值。

    Returns:
        JSON 字串,結構如下:
        {
          "status": "completed",
          "data": {
            "app_name": "openclaw-helper-agent",
            "resource_group": "rg-openclaw",
            "revision": "openclaw-helper-agent--abc123",
            "total_count": 23,
            "system_vars_excluded": true,
            "variable_names": [
              "AZURE_AI_FOUNDRY_PROJECT_ENDPOINT",
              "AZURE_AI_FOUNDRY_API_KEY",
              "FABRIC_SQL_CONNECTION_STRING",
              "OBO_CLIENT_ID"
            ]
          }
        }

        失敗時:
        {"status": "failed", "error": "..."}
    """
    # MCP tool 不支援 None 預設值的 optional string 參數,
    # 因此用空字串代表「未指定」,在此轉回 None 交給 inspector。
    rg = resource_group.strip() or None
    sub = subscription_id.strip() or None

    logger.info(
        f"[MCP] list_aca_environment_variables "
        f"app={app_name} rg={rg or '(default)'} sys={include_system_vars}"
    )

    try:
        data = await aca_env_inspector.list_environment_variables(
            app_name=app_name,
            resource_group=rg,
            subscription_id=sub,
            include_system_vars=include_system_vars,
        )
        return json.dumps(
            {"status": "completed", "data": data},
            ensure_ascii=False,
        )

    except Exception as e:
        logger.error(
            f"[MCP] list_aca_environment_variables error: {e}",
            exc_info=True,
        )
        return json.dumps(
            {"status": "failed", "error": str(e)},
            ensure_ascii=False,
        )


# ============================================================================
# SHARED HELPER
# 2026.03.14 George: v2.1 共用 response payload 建構邏輯
# MCP tools 和 REST routes 共用同一個 response 格式，避免重複程式碼。
# ============================================================================

def _build_response_payload(result: dict) -> dict:
    """
    從 core_handler.run_workflow() 的 result dict 建構標準化 response payload。
    供 MCP tools 和 REST routes 共用。

    2026.05.18 George × Claude: v2.5 Phase 3
    新增 Adaptive Timeout / cancel / check 等非完成狀態的透傳分支。
    既有「正常完成」路徑 (status=completed/needs_input/failed) 邏輯完全不變,
    確保 Phase 0 行為 100% 相容。
    """
    # Phase 3 新增:Adaptive Timeout 等 result dict 已自帶 status 欄位,
    # 直接透傳而不做 success/needs_input/failed 轉換。
    #
    # 涵蓋的非完成狀態:
    # - "running":run_workflow detach 路徑 (主文件 §5.2)
    # - "rejected":並行被拒 (主文件 §5.5)
    # - "cancelling":cancel_pending_task 確認回應 (§5.7)
    # - "cancelled":bg task 已 cancel 但 waiter 仍在等的回應 shape
    #                (core_handler.py Step 7a 路徑)
    # - "no_running_task":check/cancel_pending_task 找不到 running job
    # - "still_running":check_pending_tasks long poll 結束仍未完成
    # - "error":cancel/check_pending_tasks 異常 (Storage 未初始化等)
    #
    # 這些 result 內已經帶好 session_id / job_id / task_description / message,
    # 直接 return 即可。
    incoming_status = result.get("status")
    if incoming_status in (
        "running", "rejected", "cancelling", "cancelled",
        "no_running_task", "still_running", "error",
    ):
        return result

    # ─── Phase 0 既有邏輯 (完全保留) ───
    status = "completed"
    if result.get("needs_input"):
        status = "needs_input"
    elif not result.get("success"):
        status = "failed"

    # 2026.03.13 George: v1.1 session_hint + turn_count（保留）
    return {
        "status": status,
        "response": result.get("response", ""),
        "session_id": result.get("session_id", ""),
        "session_hint": (
            "帶回此 session_id 以延續對話上下文（turn history + final code）"
        ),
        "skills_referenced": result.get("skills_referenced", []),        #<- 2026.04.02 新增: 可以幫助測試skill.md的引用情況
        "turn_count": (
            len(result.get("turn_history", []))
            if "turn_history" in result
            else None
        ),
        "uploads": [
            {
                "filename": os.path.basename(getattr(u, "original_path", "")),
                "url": getattr(u, "sas_url", ""),
            }
            for u in result.get("uploads", [])
            if hasattr(u, "sas_url")
        ],
    }


# ============================================================================
# FASTAPI APP + LIFESPAN
# 2026.03.14 George: v2.0 (v2.1a 修正)
# 2026.03.15 George: v2.1a
# 官方 mcp SDK 需要將 mcp.session_manager.run() 納入 lifespan，
# 確保 MCP session manager 正確初始化/釋放。
# 同時在 lifespan 中呼叫 core_handler.startup() 初始化 workflow。
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan — 啟動 MCP session manager + 初始化 core handler。

    2026.03.15 George: v2.1a
    使用 AsyncExitStack 同時管理:
    1. mcp.session_manager.run() — MCP 內部 session 生命週期
    2. core_handler.startup()   — workflow + skills + conversation store
    """
    logger.info("=" * 60)
    logger.info("MCP Server + REST API (Streamable HTTP) - Starting...")
    logger.info("=" * 60)

    async with contextlib.AsyncExitStack() as stack:
        # 1. 啟動 MCP session manager（官方 SDK 要求）
        await stack.enter_async_context(mcp.session_manager.run())
        logger.info("MCP session manager started")

        # 2. 初始化 core handler（workflow + skills sync + conversation store）
        await core_handler.startup()

        logger.info(f"Server ready on port {HTTP_PORT}")
        logger.info(f"  MCP endpoint:  http://0.0.0.0:{HTTP_PORT}/mcp/mcp")
        logger.info(f"  REST endpoint: http://0.0.0.0:{HTTP_PORT}/run")
        logger.info(f"  Health check:  http://0.0.0.0:{HTTP_PORT}/health")
        logger.info(f"  Transport: Streamable HTTP (stateless, json_response)")

        yield  # app 運行中

    logger.info("Server shutting down...")


# FastAPI 主 app
app = FastAPI(
    title="Code Agent - MCP + REST",
    version="2.2",
    lifespan=lifespan,
)


# 2026.03.17 George: v2.2 Identity Passthrough — Bearer token extraction middleware
@app.middleware("http")
async def extract_bearer_token(request: Request, call_next):
    """
    從 Authorization header 提取 Bearer token，
    存入 contextvars 供下游 MCP tool / REST handler 使用。
    
    注意：此 middleware 對 FastAPI 自身的 routes (/run, /clear, /health) 生效，
    但對 mount 的 MCP sub-app 可能需要額外驗證（見部署注意事項）。
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        _current_user_token.set(auth_header[7:])
    else:
        _current_user_token.set("")
    response = await call_next(request)
    return response


# ============================================================================
# REST API ROUTES
# 2026.03.14 George: v2.1 從 aiohttp 轉為 FastAPI routes
# 保留與 v1.x 完全相同的 request/response schema，確保向後相容。
# ============================================================================

@app.post("/run")
async def rest_run(request: Request):
    """
    POST /run — 執行 coding workflow（REST API）。

    與 MCP tool run_coding_workflow 功能相同，
    但使用傳統 REST JSON 格式，供不支援 MCP 的 caller 使用。

    Request body:
    {
        "request": "用戶的請求文字",
        "session_id": "abc-123",       // optional
        "credentials": {"KEY": "VAL"}  // optional (直接傳 dict)
    }

    Response:
    {
        "status": "completed" | "needs_input" | "failed",
        "response": "...",
        "session_id": "abc-123",
        "uploads": [...]
    }
    """
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse(
            {"status": "failed", "error": f"Invalid JSON: {e}"},
            status_code=400,
        )

    user_input = body.get("request", "")
    if not user_input or not user_input.strip():
        return JSONResponse(
            {"status": "failed", "error": "Missing 'request' field"},
            status_code=400,
        )

    session_id = body.get("session_id")
    # 2026.03.14 George: v2.1 REST API 的 credentials 直接接受 dict（向後相容 v1.x）
    # 2026.04.16 George: v2.2.1 強化 credentials 型別處理
    #   APIM 或其他 gateway 可能會把 credentials 序列化成字串，或傳空字串，
    #   這裡統一歸一化成 dict，避免 `credentials[key] = val` 爆 TypeError
    raw_credentials = body.get("credentials")
    if raw_credentials is None or raw_credentials == "":
        credentials = {}
    elif isinstance(raw_credentials, dict):
        credentials = raw_credentials
    elif isinstance(raw_credentials, str):
        # 容錯：可能是 JSON 字串形式（APIM set-body 常見情況）
        try:
            parsed = json.loads(raw_credentials)
            credentials = parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                f"[REST] credentials is non-JSON string, ignoring. "
                f"value prefix={raw_credentials[:50]!r}"
            )
            credentials = {}
    else:
        logger.warning(
            f"[REST] credentials has unexpected type {type(raw_credentials).__name__}, "
            f"falling back to empty dict"
        )
        credentials = {}

    # 2026.03.17 George: v2.2 Identity Passthrough — 從 header 注入 user token
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        if credentials is None:
            credentials = {}
        credentials["__user_token"] = auth_header[7:]

    logger.info(
        f"[REST] POST /run session={session_id or '(new)'}, "
        f"input={user_input[:100]}..."
    )

    try:
        result = await core_handler.run_workflow(
            user_input,
            session_id=session_id,
            credentials=credentials,
        )
        return JSONResponse(_build_response_payload(result))

    except Exception as e:
        logger.error(f"[REST] Workflow error: {e}", exc_info=True)
        return JSONResponse(
            {"status": "failed", "error": str(e)},
            status_code=500,
        )


@app.post("/clear")
async def rest_clear(request: Request):
    """
    POST /clear — 清除 session metadata（REST API）。

    Request body:
    {
        "session_id": "abc-123"
    }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"status": "failed", "error": "Invalid JSON"},
            status_code=400,
        )

    session_id = body.get("session_id")
    if not session_id:
        return JSONResponse(
            {"status": "failed", "error": "Missing 'session_id'"},
            status_code=400,
        )

    result_text = await core_handler.clear_session(session_id)
    return JSONResponse({"status": "completed", "response": result_text})


@app.get("/skills")
async def rest_skills():
    """GET /skills — 列出本地 skills（REST API）。"""
    skills_text = core_handler.list_skills()
    return JSONResponse({"status": "completed", "response": skills_text})


@app.post("/sync")
async def rest_sync():
    """POST /sync — 強制同步 skills from Blob（REST API）。"""
    sync_text = await core_handler.sync_skills()
    return JSONResponse({"status": "completed", "response": sync_text})


# ============================================================================
# SKILL REVIEW ENDPOINTS
# 2026.03.30 George: v2.3 HITL
#
# Logic App Adaptive Card 的 Approve/Reject 按鈕分別 POST 到這兩個 endpoint。
# 這是純 REST API，與 MCP protocol 無關，只是掛在同一個 FastAPI app 上。
#
# Request body:
#   {
#       "pending_id": "pending-20260326-a1b2c3",
#       "reviewer": "george@contoso.com"      // optional
#   }
# ============================================================================


@app.post("/api/skills/approve")
async def rest_skills_approve(request: Request):
    """
    POST /api/skills/approve — HITL 審核通過。
    Logic App callback endpoint。

    觸發 core_handler.approve_pending_skill():
    - 從 Blob 讀回 knowledge JSON
    - 對當下最新的正式 skill 做 merge + dedup
    - 上傳到 Blob + sync 到本地 + rebuild workflow
    - 清理 pending
    """
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse(
            {"status": "error", "reason": f"Invalid JSON: {e}"},
            status_code=400,
        )

    pending_id = body.get("pending_id")
    if not pending_id:
        return JSONResponse(
            {"status": "error", "reason": "Missing 'pending_id'"},
            status_code=400,
        )

    reviewer = body.get("reviewer", "unknown")

    logger.info(
        f"[REST] POST /api/skills/approve — "
        f"pending_id={pending_id}, reviewer={reviewer}"
    )

    try:
        result = await core_handler.approve_pending_skill(pending_id, reviewer)
        status_code = 200 if result.get("status") == "approved" else 404
        return JSONResponse(result, status_code=status_code)
    except Exception as e:
        logger.error(f"[REST] Approve error: {e}", exc_info=True)
        return JSONResponse(
            {"status": "error", "reason": str(e)},
            status_code=500,
        )


@app.post("/api/skills/reject")
async def rest_skills_reject(request: Request):
    """
    POST /api/skills/reject — HITL 審核拒絕。
    Logic App callback endpoint。

    觸發 core_handler.reject_pending_skill():
    - 刪除 Blob pending
    - 不觸發 sync（正式目錄無變動）
    """
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse(
            {"status": "error", "reason": f"Invalid JSON: {e}"},
            status_code=400,
        )

    pending_id = body.get("pending_id")
    if not pending_id:
        return JSONResponse(
            {"status": "error", "reason": "Missing 'pending_id'"},
            status_code=400,
        )

    reviewer = body.get("reviewer", "unknown")

    logger.info(
        f"[REST] POST /api/skills/reject — "
        f"pending_id={pending_id}, reviewer={reviewer}"
    )

    try:
        result = await core_handler.reject_pending_skill(pending_id, reviewer)
        return JSONResponse(result, status_code=200)
    except Exception as e:
        logger.error(f"[REST] Reject error: {e}", exc_info=True)
        return JSONResponse(
            {"status": "error", "reason": str(e)},
            status_code=500,
        )


# ============================================================================
# HEALTH CHECK
# 2026.03.14 George: v2.0 (v2.1a 延續)
# /health 供 ACA liveness/readiness probe 使用，
# 不走 MCP 協議（probe 不會講 JSON-RPC）。
# ============================================================================

@app.get("/health")
async def health_check():
    """
    GET /health — Health check。
    供 ACA liveness/readiness probe 使用。
    """
    return {
        "status": "healthy" if core_handler.is_ready() else "starting",
        "service": "code-agent-mcp",
        "version": "10.0",
        "transport": "streamable-http",
        # 2026.03.15 George: v2.1a 顯示兩種連線方式的狀態
        "endpoints": {
            "mcp": "/mcp/mcp",
            "rest": "/run",
            "health": "/health",
        },
    }


# ============================================================================
# MOUNT MCP SUB-APP
# 2026.03.15 George: v2.1a
# 官方 mcp SDK 使用 mcp.streamable_http_app() 取得 ASGI sub-app，
# 不是第三方 fastmcp 的 mcp.http_app()。
#
# 路徑計算：
#   app.mount("/mcp", ...) 的 mount point = "/mcp"
#   streamable_http_app() 預設 sub-path = "/mcp"
#   → MCP Client 連線 URL = https://your-aca.../mcp/mcp
#
# 重要：mount 必須放在 @app.get/post routes 之後，
# 因為 mount("/mcp") 會 catch-all /mcp 底下所有路徑。
# FastAPI 先比對 @app 定義的 routes（/run, /clear, /health），
# 不匹配時才 fallback 到 mount 的 sub-app。
# ============================================================================

app.mount("/", mcp.streamable_http_app())



# ============================================================================
# ENTRY POINT
# 2026.03.14 George: v2.0 (v2.1a 延續)
# 改用 uvicorn 啟動 ASGI app（取代 aiohttp.web.run_app）
# 部署時 Dockerfile CMD:
#   uvicorn mcp_server:app --host 0.0.0.0 --port 8080
# 本地開發可直接 python mcp_server.py
# ============================================================================

if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting MCP + REST Server on port {HTTP_PORT}...")
    uvicorn.run(
        "mcp_server:app",
        host="0.0.0.0",
        port=HTTP_PORT,
        log_level="info",
    ) 