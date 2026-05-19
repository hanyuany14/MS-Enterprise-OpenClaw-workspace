"""
Output File Store - 抽象介面層 (Phase 0)
====================================================================
把「產出檔案上傳與下載連結產生」這件事抽象成 Protocol,讓未來搬遷
Foundry Hosted Agent 時可以「換實作」而不是「改架構」。

對應設計文件:async_design_doc_v3.md §17.3 OutputFileStore

階段劃分:
- 階段 1 (現在):BlobOutputFileStore — 上傳到 Azure Blob,產生 SAS URL。
  v3 文件特別強調「BlobOutputFileStore 在本階段就做」,順便解掉 §12
  的 replica restart 殺檔案痛點。
- 階段 2 (POC 通過後):HostedAgentFileStore — 委派給 Hosted Agent
  的 /files endpoint + $HOME 持久化。本階段不實作。

Phase 0 重構原則:
- 邏輯零變更。本檔案的 BlobOutputFileStore.put_files() 內部邏輯是從
  code_agent_hosted.py 的 upload_results() 整段搬過來,行為等同。
- code_agent_hosted.py 的 upload_results() 變成薄殼,呼叫
  state.file_store.put_files(...) 並回傳結果。

VERSION: 1.0
2026.05.12 George × Claude: Phase 0 初版 — 從 upload_results() 抽出
  Blob 上傳邏輯封裝為 BlobOutputFileStore,維持既有行為與 SAS URL 格式。
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Protocol

from azure.storage.blob import (
    BlobSasPermissions,
    BlobServiceClient,
    generate_blob_sas,
)

logger = logging.getLogger(__name__)


# ============================================================================
# UploadResult — 從 code_agent_hosted.py 搬過來
# ============================================================================

@dataclass
class UploadResult:
    """單一檔案上傳結果。

    維持 code_agent_hosted.py v10.2 的 schema:
    - blob_path:  blob 在 container 內的路徑 (例如 "outputs/{sess}/chart.png")
    - sas_url:    完整可下載的 SAS URL (含 query string,不可截斷)
    - original_path: 原始本機檔案路徑 (供 caller 對照用)
    - description: v10.2 新增的選用用途描述,供 format_uploads_for_output() 用
    """
    blob_path: str
    sas_url: str
    original_path: str
    description: str = ""


# ============================================================================
# OutputFileStore Protocol
# ============================================================================

class OutputFileStore(Protocol):
    """產出檔案儲存的抽象介面。

    Phase 0 階段只有一個實作 BlobOutputFileStore。
    階段 2 才會出現 HostedAgentFileStore。
    """

    async def put_files(
        self,
        session_id: str,
        final_script_path: Optional[str],
        output_files: List[str],
    ) -> List[UploadResult]:
        """上傳 session 的產出檔案,回傳含 SAS URL 的結果列表。

        Args:
            session_id: 應用層 session ID (用於 blob 路徑 prefix)
            final_script_path: 最後一輪成功執行的 script 本機路徑 (可為 None)
            output_files: 本次 workflow 產生的 output 檔案本機路徑列表

        Returns:
            UploadResult 列表,順序為:
            1. final_script (如果有) 排第一
            2. 其後依 output_files 順序
            這個順序會被 format_uploads_for_output() 直接用於下游 markdown 排序。
        """
        ...

    async def get_download_url(
        self,
        session_id: str,
        filename: str,
        ttl_seconds: int = 3600,
    ) -> str:
        """產生指定檔案的可下載 URL (供 Teams Adaptive Card 等延後存取用)。

        Phase 0 階段這個方法暫不被使用 (既有流程一次性產出 + 上傳即取得 SAS),
        但介面保留,Phase 4 Teams 通知需要時直接補上實作。
        """
        ...


# ============================================================================
# BlobOutputFileStore — 階段 1 實作
# ============================================================================

class BlobOutputFileStore:
    """Azure Blob Storage 上傳實作。

    Phase 0 邏輯零變更原則:
    - 從 upload_results() 整段搬過來:
      - container 自動建立 (try get_container_properties → except create_container)
      - final_script 上傳到 scripts/{session_id}/final_script.py
      - output_files 上傳到 outputs/{session_id}/{basename}
      - SAS URL 用 account_key 簽,1 小時 expiry,read-only
      - final_script 預設帶 description="執行腳本,可重跑或修改後重新產出"
    - 唯一改變:認證資料 (account_name / account_key) 從環境變數讀改為
      在 __init__ 注入,讓 unit test 可以注入 fake client。
    """

    def __init__(
        self,
        account_name: str,
        account_key: str,
        container_name: str = "code-outputs",
    ):
        """
        Args:
            account_name: Azure Storage Account 名稱
            account_key: Storage Account Key (用於 SAS 簽章)
            container_name: 預設 container,維持既有 "code-outputs" 名稱
        """
        self._account_name = account_name
        self._account_key = account_key
        self._container_name = container_name
        # blob service client 延遲初始化,避免在 import 時就建連線
        self._blob_service: Optional[BlobServiceClient] = None

    def _get_blob_service(self) -> BlobServiceClient:
        """延遲初始化 BlobServiceClient,並做 container 自動建立。"""
        if self._blob_service is None:
            self._blob_service = BlobServiceClient(
                account_url=f"https://{self._account_name}.blob.core.windows.net",
                credential=self._account_key,
            )
        return self._blob_service

    def _ensure_container(self, container_name: str):
        """確保 container 存在 (try get → except create)。

        這個 try/except 不分類 exception 是維持既有行為。
        既有 upload_results() 用裸 except: 抓所有錯誤後就 create,
        Phase 0 保留這個寬鬆策略避免 regression。
        """
        container_client = self._get_blob_service().get_container_client(container_name)
        try:
            container_client.get_container_properties()
        except Exception:
            container_client.create_container()
        return container_client

    async def put_files(
        self,
        session_id: str,
        final_script_path: Optional[str],
        output_files: List[str],
    ) -> List[UploadResult]:
        """上傳 final_script + output_files 到 Blob,回傳 UploadResult 列表。

        此函數是 code_agent_hosted.py upload_results() 的直接搬遷版本,
        行為完全等同 (包含 SAS URL 格式、container 自動建立、描述欄位處理)。
        """
        container_client = self._ensure_container(self._container_name)
        results: List[UploadResult] = []

        # ──────────────────────────────────────────────────────────
        # 1. 上傳 final_script (如果有)
        #    Blob 路徑: scripts/{session_id}/final_script.py
        # ──────────────────────────────────────────────────────────
        if final_script_path and os.path.exists(final_script_path):
            blob_name = f"scripts/{session_id}/final_script.py"
            blob_client = container_client.get_blob_client(blob_name)
            with open(final_script_path, "rb") as f:
                blob_client.upload_blob(f, overwrite=True)

            sas = generate_blob_sas(
                account_name=self._account_name,
                container_name=self._container_name,
                blob_name=blob_name,
                account_key=self._account_key,
                permission=BlobSasPermissions(read=True),
                expiry=datetime.now(timezone.utc) + timedelta(hours=1),
            )
            # v10.2 final_script 預設描述
            results.append(UploadResult(
                blob_path=blob_name,
                sas_url=f"{blob_client.url}?{sas}",
                original_path=final_script_path,
                description="執行腳本,可重跑或修改後重新產出",
            ))

        # ──────────────────────────────────────────────────────────
        # 2. 上傳 output 檔案
        #    Blob 路徑: outputs/{session_id}/{basename}
        # ──────────────────────────────────────────────────────────
        for file_path in output_files:
            if not os.path.exists(file_path):
                # 既有行為:檔案不存在則跳過,不報錯
                continue

            blob_name = f"outputs/{session_id}/{os.path.basename(file_path)}"
            blob_client = container_client.get_blob_client(blob_name)
            with open(file_path, "rb") as f:
                blob_client.upload_blob(f, overwrite=True)

            sas = generate_blob_sas(
                account_name=self._account_name,
                container_name=self._container_name,
                blob_name=blob_name,
                account_key=self._account_key,
                permission=BlobSasPermissions(read=True),
                expiry=datetime.now(timezone.utc) + timedelta(hours=1),
            )
            results.append(UploadResult(
                blob_path=blob_name,
                sas_url=f"{blob_client.url}?{sas}",
                original_path=file_path,
                # output_files 不預填 description,維持既有行為
                # 未來 CodingAgent 可在產檔時主動填,目前一律空字串
            ))

        logger.info(
            f"[BlobOutputFileStore] Uploaded {len(results)} files "
            f"for session={session_id}"
        )
        return results

    async def get_download_url(
        self,
        session_id: str,
        filename: str,
        ttl_seconds: int = 3600,
    ) -> str:
        """產生指定檔案的可下載 URL。

        Phase 0: 此方法定義好介面但暫不被 production 流程使用 —
        既有 upload_results() 是「上傳 + 立刻產生 SAS」一次到位,
        SAS URL 已包含在 UploadResult.sas_url 裡。

        Phase 4 Teams Adaptive Card 通知時,可能需要為「已上傳但 SAS 過期」
        的檔案重新產生 URL,屆時這個方法會被呼叫。
        """
        # 預設假設檔案在 outputs/ prefix 下;若 caller 需要 scripts/ prefix
        # 可以改用 full blob_path 直接生成。Phase 4 再依實際需求調整。
        blob_name = f"outputs/{session_id}/{filename}"
        sas = generate_blob_sas(
            account_name=self._account_name,
            container_name=self._container_name,
            blob_name=blob_name,
            account_key=self._account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
        )
        return (
            f"https://{self._account_name}.blob.core.windows.net/"
            f"{self._container_name}/{blob_name}?{sas}"
        )
