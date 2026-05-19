"""
HTTP API Adapter - Custom ACA Deployment
====================================================================
獨立的 HTTP API endpoint，部署在 Custom ACA 上。
未來可由 Azure APIM 包裝為 MCP Tool endpoint。

核心邏輯（workflow 執行、session 管理、metadata persist）在 core_handler.py 中，
本檔案只負責：
- HTTP request/response 處理（aiohttp）
- JSON request schema 解析
- JSON response schema 建構
- Health check endpoint

API:
  POST /run       — 執行 coding workflow
  POST /clear     — 清除 session metadata
  GET  /health    — Health check
  GET  /skills    — 列出本地 skills（debug 用）
  POST /sync      — 強制同步 skills from Blob

Request schema (POST /run):
{
    "request": "幫我寫一個 Azure Blob 上傳腳本",
    "session_id": "abc-123",          // optional, for stateful follow-up
    "credentials": {                   // optional, injected as env vars
        "API_KEY": "...",
        "ENDPOINT": "..."
    }
}

Response schema:
{
    "status": "completed" | "needs_input" | "failed",
    "response": "...",
    "session_id": "abc-123",
    "uploads": []
}

VERSION: 1.1
2026.03.13 George: v1.1 response 加上 session_hint + turn_count
- 讓 caller 明確知道需要回帶 session_id 以延續上下文

2026.03.12 George: v1.0 初版
- 基於 aiohttp 的輕量 HTTP server
- 與 main.py (Foundry adapter) 共用 core_handler
- 預設 port 8080（與 Foundry 的 8088 區隔）
- 未來由 APIM 包裝為 MCP endpoint

依賴：
- aiohttp
- core_handler (本專案的共用核心)
"""

import logging
import sys
import os
import json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stdout,
    force=True,
)

from aiohttp import web

import core_handler

try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv())
except ImportError:
    pass

logger = logging.getLogger(__name__)

# HTTP server port（與 Foundry 的 8088 區隔）
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))


# ============================================================================
# STARTUP
# ============================================================================

async def on_startup(app):
    """aiohttp app startup hook — 初始化 core handler。"""
    logger.info("=" * 60)
    logger.info("HTTP API Adapter - Starting...")
    logger.info("=" * 60)
    await core_handler.startup()
    logger.info(f"HTTP API ready on port {HTTP_PORT}")


# ============================================================================
# ROUTE HANDLERS
# ============================================================================

async def handle_run(request: web.Request) -> web.Response:
    """
    POST /run — 執行 coding workflow。
    
    Request body:
    {
        "request": "用戶的請求文字",
        "session_id": "abc-123",       // optional
        "credentials": {"KEY": "VAL"}  // optional
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
    except (json.JSONDecodeError, Exception) as e:
        return web.json_response(
            {"status": "failed", "error": f"Invalid JSON: {e}"},
            status=400,
        )

    user_input = body.get("request", "")
    if not user_input or not user_input.strip():
        return web.json_response(
            {"status": "failed", "error": "Missing 'request' field"},
            status=400,
        )

    session_id = body.get("session_id")
    credentials = body.get("credentials")

    logger.info(
        f"[HTTP] POST /run session={session_id or '(new)'}, "
        f"input={user_input[:100]}..."
    )

    try:
        result = await core_handler.run_workflow(
            user_input,
            session_id=session_id,
            credentials=credentials,
        )

        # 將 workflow result 轉為結構化 response
        status = "completed"
        if result.get("needs_input"):
            status = "needs_input"
        elif not result.get("success"):
            status = "failed"

        # 2026.03.13 George: 加上 session_hint 和 turn_count，
        # 讓 caller 明確知道需要回帶 session_id 以延續上下文
        return web.json_response({
            "status": status,
            "response": result.get("response", ""),
            "session_id": result.get("session_id", ""),
            "session_hint": (
                "帶回此 session_id 以延續對話上下文（turn history + final code）"
            ),
            "turn_count": len(result.get("turn_history", [])) if "turn_history" in result else None,
            "uploads": [
                {
                    "filename": os.path.basename(getattr(u, "original_path", "")),
                    "url": getattr(u, "sas_url", ""),
                }
                for u in result.get("uploads", [])
                if hasattr(u, "sas_url")
            ],
        })

    except Exception as e:
        logger.error(f"[HTTP] Workflow error: {e}", exc_info=True)
        return web.json_response(
            {"status": "failed", "error": str(e)},
            status=500,
        )


async def handle_clear(request: web.Request) -> web.Response:
    """
    POST /clear — 清除 session metadata。
    
    Request body:
    {
        "session_id": "abc-123"
    }
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"status": "failed", "error": "Invalid JSON"},
            status=400,
        )

    session_id = body.get("session_id")
    if not session_id:
        return web.json_response(
            {"status": "failed", "error": "Missing 'session_id'"},
            status=400,
        )

    result_text = await core_handler.clear_session(session_id)
    return web.json_response({"status": "completed", "response": result_text})


async def handle_health(request: web.Request) -> web.Response:
    """GET /health — Health check。"""
    return web.json_response({
        "status": "healthy" if core_handler.is_ready() else "starting",
        "service": "code-agent-http",
        "version": "10.0",
    })


async def handle_skills(request: web.Request) -> web.Response:
    """GET /skills — 列出本地 skills（debug 用）。"""
    skills_text = core_handler.debug_list_skills()
    return web.json_response({"status": "completed", "response": skills_text})


async def handle_sync(request: web.Request) -> web.Response:
    """POST /sync — 強制同步 skills from Blob。"""
    sync_text = await core_handler.sync_skills()
    return web.json_response({"status": "completed", "response": sync_text})


# ============================================================================
# APP SETUP
# ============================================================================

def create_app() -> web.Application:
    """建立 aiohttp app。"""
    app = web.Application()
    app.on_startup.append(on_startup)

    # Routes
    app.router.add_post("/run", handle_run)
    app.router.add_post("/clear", handle_clear)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/skills", handle_skills)
    app.router.add_post("/sync", handle_sync)

    return app


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    logger.info(f"Starting HTTP API server on port {HTTP_PORT}...")
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=HTTP_PORT)