"""
Skills Sync - Azure Blob Storage 雙向同步 (診斷強化版)
====================================================================
VERSION: 1.2
2026.03.30 George: v1.2 HITL Pending Skill Blob 操作
- 新增 blob_write_pending(): 寫入 pending metadata JSON 到 Blob
- 新增 blob_read_pending_metadata(): 讀取 pending metadata
- 新增 blob_delete_pending(): 刪除 pending
- 新增 blob_upload_skill(): 上傳本地 skill 到 Blob 正式路徑
- 新增 PENDING_BLOB_PREFIX 常量
- 使用既有 _get_blob_container() pattern，保持同步調用風格

2026.03.06 George: v1.1 增加啟動診斷日誌回傳至 Blob 功能
"""

import os
import logging
import datetime
import traceback
from pathlib import Path
import shutil

from azure.storage.blob import BlobServiceClient, ContainerClient

logger = logging.getLogger(__name__)

# 優先讀取環境變數，確保路徑一致性
SKILLS_DIR = os.environ.get("SKILLS_DIR", "/app/skills")
SKILLS_BLOB_CONTAINER = os.environ.get("SKILLS_BLOB_CONTAINER", "agent-skills")
SKILLS_BLOB_PREFIX = "skills/"

def _get_blob_container() -> ContainerClient:
    """取得 Blob Container client"""
    account_name = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME")
    account_key = os.environ.get("AZURE_STORAGE_ACCOUNT_KEY")
    
    if not account_name or not account_key:
        raise ValueError("AZURE_STORAGE_ACCOUNT_NAME or KEY is not set in Environment Variables.")

    blob_service = BlobServiceClient(
        account_url=f"https://{account_name}.blob.core.windows.net",
        credential=account_key,
    )
    container = blob_service.get_container_client(SKILLS_BLOB_CONTAINER)

    # 確保 container 存在
    try:
        container.get_container_properties()
    except Exception:
        try:
            container.create_container()
            logger.info(f"Created blob container: {SKILLS_BLOB_CONTAINER}")
        except Exception as e:
            logger.warning(f"Failed to create container: {e}")

    return container

async def _write_startup_diag(content: str):
    """
    將診斷訊息寫入 Blob Storage: diag/last_blob_sync.log
    用來追蹤 Hosted Agent 啟動階段的狀態。
    """
    try:
        # 這裡不調用 _get_blob_container 以免進入無窮迴圈
        account_name = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME")
        account_key = os.environ.get("AZURE_STORAGE_ACCOUNT_KEY")
        if not account_name or not account_key:
            return

        service = BlobServiceClient(f"https://{account_name}.blob.core.windows.net", account_key)
        container = service.get_container_client(SKILLS_BLOB_CONTAINER)
        blob_client = container.get_blob_client("diag/last_blob_sync.log")
        
        timestamp = datetime.datetime.now().isoformat()
        log_entry = f"[{timestamp}] {content}\n"
        
        # 如果是第一步，覆寫舊日誌；其餘則附加（或是簡單起見每次覆寫最新狀態）
        blob_client.upload_blob(log_entry, overwrite=True)
    except Exception as e:
        logger.error(f"Failed to write diag log: {e}")


async def sync_skills_from_blob() -> int:
    try:
        await _write_startup_diag("INIT: sync_skills_from_blob started.")
        
        # 強烈建議：同步前先清理，避免與 Docker Image 內的舊檔案衝突
        if os.path.exists(SKILLS_DIR):
            await _write_startup_diag("CLEAN: Removing existing skills directory for clean sync.")
            shutil.rmtree(SKILLS_DIR)
        
        os.makedirs(SKILLS_DIR, exist_ok=True)
        container = _get_blob_container()
        
        blobs = list(container.list_blobs(name_starts_with=SKILLS_BLOB_PREFIX))
        await _write_startup_diag(f"INFO: Found {len(blobs)} blobs.")
        
        count = 0
        skill_dirs_seen = set()
        
        for blob in blobs:
            # 修正點 1: 跳過以 / 結尾的目錄佔位符
            if blob.name.endswith('/'):
                continue
                
            relative_path = blob.name[len(SKILLS_BLOB_PREFIX):]
            if not relative_path:
                continue

            local_path = os.path.join(SKILLS_DIR, relative_path)
            dir_name = os.path.dirname(local_path)

            # 修正點 2: 建立資料夾前，檢查是否已有同名檔案（雖然清理過但保險起見）
            if os.path.exists(dir_name) and not os.path.isdir(dir_name):
                os.remove(dir_name) # 移除阻礙資料夾建立的檔案
            
            os.makedirs(dir_name, exist_ok=True)

            # 修正點 3: 如果 local_path 本身已經是資料夾（這會發生在處理順序混亂時）
            if os.path.isdir(local_path):
                continue

            blob_client = container.get_blob_client(blob.name)
            data = blob_client.download_blob().readall()
            with open(local_path, "wb") as f:
                f.write(data)

            skill_dir_name = relative_path.split("/")[0]
            if skill_dir_name not in skill_dirs_seen:
                skill_dirs_seen.add(skill_dir_name)
                count += 1

        success_msg = f"SUCCESS: Synced {count} skills. Paths: {list(skill_dirs_seen)}"
        await _write_startup_diag(success_msg)
        logger.info(success_msg)
        return count

    except Exception as e:
        error_stack = traceback.format_exc()
        error_msg = f"FATAL ERROR during sync: {str(e)}\n{error_stack}"
        await _write_startup_diag(error_msg)
        logger.error(error_msg)
        return 0

async def sync_skill_to_blob(skill_dir: str) -> bool:
    """
    Gatekeeper 寫回本地後同步至 Blob。
    """
    if not os.path.isdir(skill_dir):
        logger.warning(f"Skill dir not found: {skill_dir}")
        return False

    try:
        container = _get_blob_container()
        skill_name = os.path.basename(skill_dir)
        uploaded = 0

        for root, _dirs, files in os.walk(skill_dir):
            for filename in files:
                local_path = os.path.join(root, filename)
                relative = os.path.relpath(local_path, SKILLS_DIR)
                blob_name = f"{SKILLS_BLOB_PREFIX}{relative}"
                blob_name = blob_name.replace("\\", "/")

                blob_client = container.get_blob_client(blob_name)
                with open(local_path, "rb") as f:
                    blob_client.upload_blob(f, overwrite=True)
                uploaded += 1

        logger.info(f"Synced skill to Blob: {skill_name} ({uploaded} files)")
        return True

    except Exception as e:
        logger.error(f"Failed to sync skill to Blob: {e}")
        return False


# ============================================================================
# PENDING SKILL BLOB OPERATIONS
# 2026.03.30 George: v1.2 HITL
#
# 供 skill_gatekeeper._write_to_pending() 和
# core_handler.approve/reject_pending_skill() 使用。
#
# Blob 路徑結構：
#   {BLOB_CONTAINER}/skills-pending/{pending_id}/metadata.json
#
# metadata.json 內容 = knowledge JSON + 決策 context（見 _write_to_pending 說明）
# ============================================================================

import json as _json  # 區隔於 skills_sync 本身不需要的 json

# Pending 的 Blob 路徑前綴（與正式 skills 的 prefix 區隔）
PENDING_BLOB_PREFIX = "skills-pending"


async def blob_write_pending(pending_id: str, metadata: dict) -> None:
    """
    2026.03.30 George: v1.2 HITL 新增

    將 pending metadata 寫到 Blob。
    目標路徑: {BLOB_CONTAINER}/{PENDING_BLOB_PREFIX}/{pending_id}/metadata.json
    """
    blob_path = f"{PENDING_BLOB_PREFIX}/{pending_id}/metadata.json"
    content = _json.dumps(metadata, ensure_ascii=False, indent=2)

    try:
        container = _get_blob_container()
        blob_client = container.get_blob_client(blob_path)
        blob_client.upload_blob(content, overwrite=True)
        logger.info(f"[SkillsSync] Pending written: {blob_path} ({len(content)} chars)")
    except Exception as e:
        logger.error(f"[SkillsSync] Failed to write pending: {blob_path} — {e}")
        raise


async def blob_read_pending_metadata(pending_id: str) -> dict | None:
    """
    2026.03.30 George: v1.2 HITL 新增

    從 Blob 讀回 pending metadata。
    目標路徑: {BLOB_CONTAINER}/{PENDING_BLOB_PREFIX}/{pending_id}/metadata.json
    """
    blob_path = f"{PENDING_BLOB_PREFIX}/{pending_id}/metadata.json"

    try:
        container = _get_blob_container()
        blob_client = container.get_blob_client(blob_path)
        stream = blob_client.download_blob()
        content = stream.readall()
        return _json.loads(content)
    except Exception as e:
        logger.warning(f"[SkillsSync] Pending not found or read error: {blob_path} — {e}")
        return None


async def blob_delete_pending(pending_id: str) -> None:
    """
    2026.03.30 George: v1.2 HITL 新增

    刪除 Blob 上的 pending 資料。
    目標路徑: {BLOB_CONTAINER}/{PENDING_BLOB_PREFIX}/{pending_id}/
    """
    prefix = f"{PENDING_BLOB_PREFIX}/{pending_id}/"

    try:
        container = _get_blob_container()
        blobs = list(container.list_blobs(name_starts_with=prefix))
        for blob in blobs:
            container.delete_blob(blob.name)
        logger.info(f"[SkillsSync] Pending deleted: {prefix} ({len(blobs)} blobs)")
    except Exception as e:
        logger.warning(f"[SkillsSync] Failed to delete pending: {prefix} — {e}")
        raise


async def blob_upload_skill(skill_name: str) -> None:
    """
    2026.03.30 George: v1.2 HITL 新增

    將本地 skills/{skill_name}/ 目錄上傳到 Blob 正式路徑。
    approve 後呼叫，確保 Blob 上的正式 skill 與本地一致。

    來源: {SKILLS_DIR}/{skill_name}/
    目標: {BLOB_CONTAINER}/{SKILLS_BLOB_PREFIX}{skill_name}/
    """
    local_skill_dir = os.path.join(SKILLS_DIR, skill_name)
    if not os.path.isdir(local_skill_dir):
        logger.warning(f"[SkillsSync] Local skill dir not found: {local_skill_dir}")
        return

    try:
        container = _get_blob_container()
        uploaded = 0

        for root, _dirs, files in os.walk(local_skill_dir):
            for filename in files:
                local_path = os.path.join(root, filename)
                relative = os.path.relpath(local_path, SKILLS_DIR)
                blob_path = f"{SKILLS_BLOB_PREFIX}{relative}"
                blob_path = blob_path.replace("\\", "/")

                blob_client = container.get_blob_client(blob_path)
                with open(local_path, "rb") as f:
                    blob_client.upload_blob(f, overwrite=True)
                uploaded += 1

        logger.info(f"[SkillsSync] Skill uploaded to Blob: {skill_name} ({uploaded} files)")
    except Exception as e:
        logger.error(f"[SkillsSync] Failed to upload skill: {skill_name} — {e}")
        raise