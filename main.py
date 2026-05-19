"""
Hosted Agent Entry Point - FoundryCBAgent Adapter
====================================================================
薄 adapter 層：將 FoundryCBAgent protocol 轉接到 core_handler。

核心邏輯（workflow 執行、session 管理、metadata persist）在 core_handler.py 中，
本檔案只負責：
- Foundry/Copilot 專用的 request parsing
- Foundry 專用的 response building（OpenAI Response format）
- Streaming keep-alive 機制
- Debug 指令（/debug skills, /sync skills, /clear）
- 啟動 log 寫入 Blob Storage

VERSION: 5.0
2026.03.12 George: v5.0 抽離 core_handler，main.py 退化為 Foundry adapter
- 核心邏輯搬到 core_handler.py（run_workflow, startup, session 管理）
- 本檔案只保留 Foundry protocol 相關邏輯
- 與 mcp_server.py 共用同一個 core_handler

依賴：
- azure-ai-agentserver-core
- core_handler (本專案的共用核心)
"""

# main.py 最頂部，在所有 import 之前
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stdout,
    force=True,
)


import os
import asyncio
import datetime

from azure.ai.agentserver.core import FoundryCBAgent
from azure.ai.agentserver.core.models import (
    Response as OpenAIResponse,
)

from azure.ai.agentserver.core.models.projects import (
    ItemContentOutputText,
    ResponsesAssistantMessageItemResource,
    ResponseTextDeltaEvent,
    ResponseTextDoneEvent,
    ResponseCompletedEvent,
)

# 核心邏輯
import core_handler

from azure.storage.blob import BlobServiceClient

try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv())
except ImportError:
    pass

logger = logging.getLogger(__name__)

# Keep-alive 間隔（秒）
KEEPALIVE_INTERVAL = float(os.environ.get("KEEPALIVE_INTERVAL", "2.0"))


# ============================================================================
# STARTUP LOG CAPTURE
# 2026.03.11 George: v3.2 啟動 log 寫入 Blob Storage
# ============================================================================

_startup_log_buffer: list[str] = []
_startup_log_handler: logging.Handler = None


def _install_startup_log_capture():
    global _startup_log_handler

    class _BufferHandler(logging.Handler):
        def emit(self, record):
            try:
                _startup_log_buffer.append(self.format(record))
            except Exception:
                pass

    _startup_log_handler = _BufferHandler()
    _startup_log_handler.setLevel(logging.INFO)
    _startup_log_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    )
    logging.getLogger().addHandler(_startup_log_handler)


def _flush_startup_log_to_blob():
    global _startup_log_handler

    if _startup_log_handler:
        logging.getLogger().removeHandler(_startup_log_handler)
        _startup_log_handler = None

    if not _startup_log_buffer:
        return

    account_name = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME")
    account_key = os.environ.get("AZURE_STORAGE_ACCOUNT_KEY")
    container_name = os.environ.get("AZURE_STORAGE_BLOB_CONTAINER", "agent-skills")
    if not account_name or not account_key:
        logger.warning("[StartupLog] AZURE_STORAGE_ACCOUNT_NAME/KEY not set, skip flush")
        return

    try:
        content = "\n".join(_startup_log_buffer)
        service = BlobServiceClient(
            f"https://{account_name}.blob.core.windows.net", account_key
        )
        blob_client = service.get_blob_client(container_name, "diag/last_startup.log")
        blob_client.upload_blob(content, overwrite=True)
        logger.info(
            f"[StartupLog] Flushed {len(_startup_log_buffer)} lines "
            f"to diag/last_startup.log"
        )
    except Exception as e:
        logger.error(f"[StartupLog] Failed to flush: {e}")


# ============================================================================
# STARTUP（Foundry adapter 版：加上 startup log capture）
# ============================================================================

async def startup():
    """Foundry adapter 啟動：log capture + core startup + flush log"""
    _install_startup_log_capture()
    await core_handler.startup()
    _flush_startup_log_to_blob()


# ============================================================================
# AgentRunContext HELPERS（Copilot protocol 專用）
# ============================================================================

def _get_request_dict(context) -> dict:
    if hasattr(context, "request") and isinstance(context.request, dict):
        return context.request
    return {}


def _get_input(context) -> any:
    return _get_request_dict(context).get("input", "")


def _get_stream(context) -> bool:
    return _get_request_dict(context).get("stream", False)


# ============================================================================
# REQUEST PARSING（Copilot protocol 專用）
# ============================================================================

def _extract_latest_user_input(context) -> str:
    """從 AgentRunContext 中提取【最新的】用戶輸入文字。"""
    raw_input = _get_input(context)

    if isinstance(raw_input, str):
        return raw_input

    if isinstance(raw_input, list):
        for item in reversed(raw_input):
            role = _get_item_role(item)
            content = _get_item_content(item)
            if role == "user" and content:
                return content
        for item in reversed(raw_input):
            content = _get_item_content(item)
            if content:
                return content

    return str(raw_input) if raw_input else ""


def _get_item_role(item) -> str:
    if isinstance(item, dict):
        return str(item.get("role", "")).lower()
    if hasattr(item, "role"):
        return str(item.role).lower()
    return ""


def _get_item_content(item) -> str:
    if isinstance(item, dict):
        content = item.get("content", "") or item.get("text", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict) and "text" in part:
                    parts.append(part["text"])
            return "\n".join(parts) if parts else ""
        return str(content) if content else ""

    for attr in ("content", "text"):
        val = getattr(item, attr, None)
        if isinstance(val, str) and val:
            return val
    return ""


# ============================================================================
# CONVERSATION ID — 穩定化（Copilot protocol 專用）
# ============================================================================

def _extract_stable_conv_id(context) -> str:
    """提取穩定的 conversation ID（Copilot 格式）。"""
    req = _get_request_dict(context)

    conv_val = req.get("conversation")
    if conv_val:
        if isinstance(conv_val, dict):
            cid = conv_val.get("id")
            if cid:
                return str(cid)
        else:
            return str(conv_val)

    conv_id_val = req.get("conversation_id")
    if conv_id_val:
        if isinstance(conv_id_val, dict):
            return str(conv_id_val.get("id", "default"))
        return str(conv_id_val)

    metadata = req.get("metadata")
    if isinstance(metadata, dict):
        cid = metadata.get("conversation_id")
        if cid:
            return str(cid)

    if hasattr(context, "conversation") and context.conversation:
        val = context.conversation
        if isinstance(val, dict):
            return str(val.get("id", "default"))
        return str(val)

    response_id = getattr(context, "response_id", None) or "default"
    logger.warning(
        f"[ConvID] No conversation ID from Copilot, "
        f"falling back to response_id={response_id}"
    )
    return str(response_id)


# ============================================================================
# RESPONSE BUILDERS（Foundry protocol 專用）
# ============================================================================

def _build_response(text: str, context=None, conv_id: str = None) -> OpenAIResponse:
    original_metadata = {}
    if context:
        req_dict = _get_request_dict(context)
        original_metadata = req_dict.get("metadata", {}) or {}
    
    if conv_id:
        if isinstance(original_metadata, dict):
            original_metadata["conversation_id"] = conv_id
        else:
            original_metadata = {"conversation_id": conv_id}

    return OpenAIResponse(
        metadata=original_metadata,
        temperature=0.0,
        top_p=0.0,
        user="user",
        id=getattr(context, "response_id", "response-id") if context else "response-id",
        created_at=datetime.datetime.now(datetime.timezone.utc),
        output=[
            ResponsesAssistantMessageItemResource(
                status="completed",
                content=[ItemContentOutputText(text=text, annotations=[])],
            )
        ],
    )


# ============================================================================
# DEBUG
# ============================================================================

def _log_request_debug(context):
    if os.environ.get("LOG_REQUEST_DEBUG", "").lower() != "true":
        return

    try:
        req = _get_request_dict(context)
        raw_input = req.get("input", "")
        stream = req.get("stream", False)

        logger.info("[DEBUG] ===== Request Body =====")
        logger.info(f"[DEBUG] stream: {stream}")
        logger.info(f"[DEBUG] response_id: {getattr(context, 'response_id', 'N/A')}")
        logger.info(f"[DEBUG] previous_response_id: {req.get('previous_response_id', 'N/A')}")

        if isinstance(raw_input, str):
            logger.info(f"[DEBUG] input (str): {raw_input[:200]}")
        elif isinstance(raw_input, list):
            logger.info(f"[DEBUG] input (list): {len(raw_input)} items")
            for i, item in enumerate(raw_input):
                role = _get_item_role(item)
                content = _get_item_content(item)
                logger.info(
                    f"[DEBUG]   [{i}] role={role}, "
                    f"content={content[:100]}{'...' if len(content) > 100 else ''}"
                )
        else:
            logger.info(f"[DEBUG] input (other): {str(raw_input)[:200]}")

        other_keys = [k for k in req.keys() if k not in ("input", "stream")]
        if other_keys:
            for k in other_keys:
                logger.info(f"[DEBUG] req['{k}'] = {str(req[k])[:200]}")

        logger.info("[DEBUG] ===== End Request =====")
    except Exception as e:
        logger.warning(f"[DEBUG] Failed to log request: {e}")


# ============================================================================
# AGENT_RUN - Foundry 主入口
# ============================================================================

async def agent_run(context):
    """FoundryCBAgent 的核心處理函式。"""
    if not core_handler.is_ready():
        await startup()

    _log_request_debug(context)

    user_input = _extract_latest_user_input(context)
    conv_id = _extract_stable_conv_id(context)
    stream = _get_stream(context)

    logger.info(f"[Request] conv={conv_id}, stream={stream}, input={user_input[:100]}...")

    # Debug 指令
    if user_input.strip().lower() == "/debug skills":
        debug_text = core_handler.list_skills()
        if stream:
            return _stream_simple(debug_text, context, conv_id)
        else:
            return _build_response(debug_text, context, conv_id)

    if user_input.strip().lower() == "/sync skills":
        sync_text = await core_handler.sync_skills()
        if stream:
            return _stream_simple(sync_text, context, conv_id)
        else:
            return _build_response(sync_text, context, conv_id)

    if user_input.strip().lower() == "/clear":
        cleared_text = await core_handler.clear_session(conv_id)
        if stream:
            return _stream_simple(cleared_text, context, conv_id)
        else:
            return _build_response(cleared_text, context, conv_id)

    # Streaming
    if stream:
        return _stream_with_keepalive(context, user_input, conv_id)

    # Non-streaming
    try:
        result = await core_handler.run_workflow(user_input, session_id=conv_id)
        response_text = result.get("response", "處理完成，但沒有回應內容。")
        return _build_response(response_text, context, conv_id)
    except Exception as e:
        logger.error(f"[Request] Workflow error: {e}", exc_info=True)
        return _build_response(f"❌ 處理失敗: {str(e)}", context, conv_id)


# ============================================================================
# STREAMING（Foundry protocol 專用）
# ============================================================================

def _stream_simple(text: str, context=None, conv_id: str = None):
    """簡單的 streaming 回傳（用於 /clear 等快速回應）。"""
    yield ResponseTextDeltaEvent(delta=text)
    yield ResponseTextDoneEvent(text=text)
    response_obj = _build_response(text, context, conv_id)
    yield ResponseCompletedEvent(response=response_obj)


async def _stream_with_keepalive(context, user_input: str, conv_id: str):
    """Streaming keep-alive + ResponseCompletedEvent。"""
    task = asyncio.create_task(
        core_handler.run_workflow(user_input, session_id=conv_id)
    )
    tick = 0

    try:
        while not task.done():
            tick += 1
            elapsed = tick * KEEPALIVE_INTERVAL
            yield ResponseTextDeltaEvent(
                delta=f"🏃 任務運行中... ({elapsed:.0f}s)\n"
               )
            await asyncio.sleep(KEEPALIVE_INTERVAL)

        result = await task
        response_text = result.get("response", "處理完成，但沒有回應內容。")

        yield ResponseTextDeltaEvent(delta=f"\n{'─' * 40}\n")
        yield ResponseTextDeltaEvent(delta=response_text)
        yield ResponseTextDoneEvent(text=response_text)

        final_response_obj = _build_response(response_text, context, conv_id)
        yield ResponseCompletedEvent(response=final_response_obj)

    except Exception as e:
        logger.error(f"[Stream] Workflow error: {e}", exc_info=True)
        error_text = f"❌ 處理失敗: {str(e)}"
        yield ResponseTextDeltaEvent(delta=f"\n{error_text}")
        yield ResponseTextDoneEvent(text=error_text)

        error_response_obj = _build_response(error_text, context, conv_id)
        yield ResponseCompletedEvent(response=error_response_obj)

    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


# ============================================================================
# ENTRY POINT
# ============================================================================

my_agent = FoundryCBAgent()
my_agent.agent_run = agent_run

if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(startup())
    logger.info("Starting FoundryCBAgent HTTP server on port 8088...")
    my_agent.run()