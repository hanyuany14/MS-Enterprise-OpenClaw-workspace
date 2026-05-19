"""
Job Store — Adaptive Timeout Escalation 的持久化層 (Phase 2)
====================================================================
非同步任務 (long-running coding job) 的 Azure Table Storage 持久化封裝。

對應設計文件:
- 主文件 v3 §4 Job Store Schema
- 主文件 v3 §5.5 並行控制 (find_running_by_session)
- 主文件 v3 §5.7 Cancel 機制 (CancelRequested 欄位)
- 補篇 §5 Phase 2 工作項

⚠️ 與 JobStateStore 的分層 (補篇 §7 已強調,容易混淆):
- 本檔案 (JobStore):**持久層**,跨 replica 可見,Table Storage,7 天 TTL。
  用於 §5.5 並行檢查、§5.7 cancel flag 寫入、Teams 通知 conversation
  reference 持久化。
- job_state_store.py (InMemoryJobStateStore):**in-process 層**,per-replica
  dict + asyncio.Event,bg task 生命週期內存在。用於 sync waiter / Teams
  通知分流的快速路徑 (§13.2)。
兩層獨立。Phase 3 的 _run_coding_agent 完成路徑會同時更新兩層。

設計重點:
- PartitionKey = session_id:讓 find_running_by_session 是 partition 內查詢,
  效能最佳 (主文件 §4.1)。
- RowKey = job_id (UUID4):同 session 內 unique。
- 不存敏感資訊:OBO token、credential value 都不會寫進 Table。
  狀態欄位只有「進度」性質的 metadata。

⚠️ 並行語意 (本階段):
- 本階段部署前提是 ACA max_replicas = 1 (主文件 §3.4 / §12 之 5),
  因此 find_running_by_session 不需要 ETag atomic op 也能保證唯一性
  (同一 partition 同時只有一個 process 在 read-then-write)。
- 跨 replica 場景請見主文件 §16 未來工作,本階段不處理。

VERSION: 1.1
2026.05.19 George × Claude: v1.1 — P0 修 Adaptive Timeout 領取漏洞
- Job dataclass 新增 result_picked_up: bool 欄位 (預設 False)
- 新增 find_recent_unpicked_by_session() — 涵蓋 RUNNING + COMPLETED/FAILED
  且未領取的 job,給 check_pending_tasks 用 (主文件 §5.3 修訂版)
- find_running_by_session 不動 (給並行控制用,語意不同)
- 對舊資料相容:缺 ResultPickedUp 欄位等同 false,filter 用
  "ResultPickedUp ne true" 涵蓋兩種情況

VERSION: 1.0
2026.05.17 George × Claude: Phase 2 初版 — Adaptive Timeout Escalation 的
  持久化層。CRUD 介面對齊主文件 §13.2 / §13.3 / §13.4 的呼叫需求。
"""

import json
import logging
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from azure.data.tables import TableServiceClient, UpdateMode
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

logger = logging.getLogger(__name__)


# ============================================================================
# 常數
# ============================================================================

JOBS_TABLE_NAME = "jobs"

# 主文件 §4 schema 中 Status 列舉值
class JobStatus(str, Enum):
    """Job 生命週期狀態。對應主文件 §4 Schema 的 Status 欄位。"""

    PENDING = "pending"        # 已建立但尚未開始執行 (短暫狀態)
    RUNNING = "running"         # bg task 執行中
    COMPLETED = "completed"     # 正常完成
    FAILED = "failed"           # 執行過程拋例外
    TIMEOUT = "timeout"         # TTL 清理時偵測為孤兒
    CANCELLED = "cancelled"     # 使用者主動 cancel


# ============================================================================
# Data Model
# ============================================================================

@dataclass
class Job:
    """Job entity 的 in-memory 結構。

    對應主文件 §4 schema 全部欄位。Table Storage 內欄位名稱大寫 (PascalCase
    符合 Azure SDK 慣例),這個 dataclass 用 snake_case (Python 慣例)。
    序列化方向轉換在 JobStore._to_entity / _from_entity 內處理。
    """

    # ── 識別 ──
    session_id: str                  # 同 PartitionKey
    job_id: str                       # 同 RowKey (UUID4)
    user_id: Optional[str] = None     # 從 OBO token 的 oid 取得 (補篇 §3 顯示
                                       # 實際是從 jwt 解析,不是 contextvars)
    user_email: Optional[str] = None  # 從 JWT 的 upn / preferred_username / email
                                       # claim 取得,Phase 4 Teams 通知收件人用

    # ── 狀態 ──
    status: str = JobStatus.RUNNING.value
    created_at: str = ""              # ISO 8601 UTC
    updated_at: str = ""
    completed_at: Optional[str] = None
    last_heartbeat: str = ""
    replica_id: str = ""              # 哪個 ACA replica 在跑

    # ── Cancel 機制 (主文件 §5.7) ──
    cancel_requested: bool = False
    cancelled_at: Optional[str] = None

    # ── 任務內容 ──
    task_description: str = ""        # 給 Teams 通知卡片顯示用
    result: Optional[str] = None       # JSON 字串 (completed 時填入)
    error: Optional[str] = None        # 錯誤訊息 (failed 時)
    progress: Optional[str] = None     # 選填的進度資訊

    # ── 通知 ──
    conversation_ref: Optional[str] = None   # 序列化 Teams conversation reference

    # ── 結果領取狀態 (2026.05.19 George × Claude: v1.1 P0 fix) ──
    # 用於區分「終態任務尚未被使用者取走」vs「終態任務已被取走」。
    # check_pending_tasks 在 Adaptive Timeout + Teams 通知路徑後,
    # 使用者回 thread 詢問結果時,find_recent_unpicked_by_session 會找到
    # status=COMPLETED/FAILED 且 result_picked_up=False 的 job 並直接回 result。
    # 領取後設為 True,避免重複領取或污染後續 turn。
    # 對應主文件 v3 §5.3 補充設計 (2026.05.19 修訂版)。
    result_picked_up: bool = False


# ============================================================================
# JobStore
# ============================================================================

class JobStore:
    """Azure Table Storage 的 job 持久化封裝。

    建構方式跟 ConversationStore 一致:account_name + account_key,
    讓 core_handler.startup() 可以共用 Storage 帳號配置邏輯。

    使用方式:
        job_store = JobStore(account_name, account_key)
        await job_store.ensure_table()
        await job_store.create(session_id=..., job_id=..., ...)
        existing = await job_store.find_running_by_session(session_id)
    """

    def __init__(self, account_name: str, account_key: str,
                 table_name: str = JOBS_TABLE_NAME):
        # 連線字串組裝跟 ConversationStore 同套路
        conn_str = (
            f"DefaultEndpointsProtocol=https;"
            f"AccountName={account_name};"
            f"AccountKey={account_key};"
            f"EndpointSuffix=core.windows.net"
        )
        self._service_client = TableServiceClient.from_connection_string(conn_str)
        self._table_name = table_name
        self._table_client = self._service_client.get_table_client(table_name)

    # ────────────────────────────────────────────────────────────────────
    # Table 管理
    # ────────────────────────────────────────────────────────────────────

    async def ensure_table(self) -> None:
        """確保 jobs table 存在。startup 時呼叫一次。

        ⚠️ azure-data-tables 同步 client 包在 async 介面內 — 跟
        ConversationStore.ensure_table 同套路。Table 建立是低頻動作
        (整個 ACA app 生命週期只跑一次),sync call 不會造成 event loop
        阻塞問題。
        """
        try:
            self._service_client.create_table(self._table_name)
            logger.info(f"[JobStore] Created table '{self._table_name}'")
        except ResourceExistsError:
            logger.info(f"[JobStore] Table '{self._table_name}' already exists")

    # ────────────────────────────────────────────────────────────────────
    # CRUD
    # ────────────────────────────────────────────────────────────────────

    async def create(
        self,
        session_id: str,
        job_id: str,
        task_description: str,
        user_id: Optional[str] = None,
        user_email: Optional[str] = None,
        replica_id: str = "",
        conversation_ref: Optional[str] = None,
    ) -> Job:
        """建立新的 running job entity。

        Phase 3 呼叫時機:run_coding_workflow 通過 §5.5 並行檢查後,
        起 bg task 之前。寫入失敗 (例如同 RowKey 已存在) 會拋例外。

        Args:
            session_id: PartitionKey,application 層 session ID。
            job_id: RowKey,UUID4。caller 自己生成。
            task_description: 給 Teams 通知卡片用的短描述,通常取
                user_input 前 N 字。
            user_id: 從 OBO JWT 的 oid claim 取得 (可選)。
                Phase 4 起由 run_workflow 外殼解析 JWT 後傳入。
            user_email: 從 OBO JWT 的 upn / preferred_username / email
                claim 取得 (可選)。Phase 4 Teams 通知 webhook payload
                的收件人欄位。
            replica_id: 哪個 ACA replica 在跑,本階段 max_replicas=1 可
                從 hostname 或 env CONTAINER_APP_REVISION 取得。
            conversation_ref: 序列化的 Teams conversation reference,
                Phase 4 Teams 通知會用到。Phase 3 階段可傳 None。

        Returns:
            建立的 Job dataclass 物件。
        """
        now = datetime.now(timezone.utc).isoformat()
        job = Job(
            session_id=session_id,
            job_id=job_id,
            user_id=user_id,
            user_email=user_email,
            status=JobStatus.RUNNING.value,
            created_at=now,
            updated_at=now,
            last_heartbeat=now,
            replica_id=replica_id,
            task_description=task_description,
            conversation_ref=conversation_ref,
        )

        entity = self._to_entity(job)
        # create_entity 在重複 RowKey 時拋 ResourceExistsError;
        # 由 caller 決定怎麼處理 (Phase 3 路徑中 job_id 是新生 uuid,
        # 衝突機率 ~0,真衝突算 bug 應該爆給 caller 看)。
        self._table_client.create_entity(entity=entity)
        logger.info(
            f"[JobStore] Created job: session={session_id}, "
            f"job_id={job_id}, task={task_description[:50]}..."
        )
        return job

    async def update(
        self,
        session_id: str,
        job_id: str,
        **fields: Any,
    ) -> None:
        """更新 job 的部分欄位。

        Phase 3 呼叫時機:bg task 完成 / 失敗 / 取消、心跳更新、
        cancel flag 設定。

        Args:
            session_id: PartitionKey。
            job_id: RowKey。
            **fields: 要更新的欄位 (snake_case),例如:
                status="completed", result=json.dumps(result_dict),
                completed_at=now_iso, cancel_requested=True 等。

        ⚠️ updated_at 會自動更新。caller 不需要顯式傳。
        ⚠️ 找不到 entity 時拋 ResourceNotFoundError,由 caller 處理。
        """
        # 取出當前 entity 做 merge (Table Storage 的 update 是 PATCH 語意,
        # 但我們明確走「讀-改-寫」確保欄位轉換正確)
        try:
            entity = self._table_client.get_entity(
                partition_key=session_id,
                row_key=job_id,
            )
        except ResourceNotFoundError:
            logger.warning(
                f"[JobStore] update on non-existent job: "
                f"session={session_id}, job_id={job_id}"
            )
            raise

        # 套用 caller 指定的欄位 (轉成 Table 的 PascalCase 欄位名)
        for key, value in fields.items():
            entity_key = self._snake_to_pascal(key)
            entity[entity_key] = value

        # 自動更新 updated_at
        entity["UpdatedAt"] = datetime.now(timezone.utc).isoformat()

        # 終態 (completed / failed / cancelled / timeout) 自動補
        # completed_at,讓 TTL 清理邏輯有時間依據
        terminal_statuses = {
            JobStatus.COMPLETED.value,
            JobStatus.FAILED.value,
            JobStatus.CANCELLED.value,
            JobStatus.TIMEOUT.value,
        }
        new_status = fields.get("status")
        if new_status in terminal_statuses and "completed_at" not in fields:
            entity["CompletedAt"] = entity["UpdatedAt"]

        self._table_client.update_entity(entity=entity, mode=UpdateMode.MERGE)
        logger.debug(
            f"[JobStore] Updated job: session={session_id}, "
            f"job_id={job_id}, fields={list(fields.keys())}"
        )

    async def get(self, session_id: str, job_id: str) -> Optional[Job]:
        """取得單一 job entity。

        Phase 3 用途:debug / audit / Teams 通知前讀取最新狀態。

        Returns:
            Job dataclass,找不到時回 None (不拋例外,呼叫端更直覺)。
        """
        try:
            entity = self._table_client.get_entity(
                partition_key=session_id,
                row_key=job_id,
            )
        except ResourceNotFoundError:
            return None
        return self._from_entity(entity)

    async def find_running_by_session(self, session_id: str) -> Optional[Job]:
        """找出指定 session 內當前 running 的 job。

        ⭐ Phase 3 最重要的查詢路徑:
        - run_coding_workflow 並行檢查 (主文件 §5.5):同 session 已有
          running job 時拒絕新任務。
        - check_pending_tasks (主文件 §5.3):使用者問「好了嗎」時找
          running job 註冊 waiter。
        - cancel_pending_task (主文件 §5.7):使用者要取消時找 running
          job 設 CancelRequested flag。

        本階段 max_replicas=1 + §5.5 並行控制保證同一時間每個 session 內
        最多一個 running job。理論上不會出現 2+ 筆,但程式碼保留萬一查到
        多筆的處理 — 取最新的那個,並 log warning。

        Args:
            session_id: PartitionKey。

        Returns:
            running 的 Job,或 None (沒有 running)。
        """
        # 用 partition + filter 查詢:partition scope 內 filter Status,
        # 比 cross-partition scan 快很多
        filter_str = (
            f"PartitionKey eq '{session_id}' and "
            f"Status eq '{JobStatus.RUNNING.value}'"
        )

        try:
            entities = list(self._table_client.query_entities(filter_str))
        except Exception as e:
            logger.error(
                f"[JobStore] find_running_by_session query failed: "
                f"session={session_id}, error={e}",
                exc_info=True,
            )
            raise

        if not entities:
            return None

        if len(entities) > 1:
            # 不該發生,但發生時記 warning 並選最新建立的那個
            logger.warning(
                f"[JobStore] find_running_by_session found {len(entities)} "
                f"running jobs in session={session_id}, expected ≤1. "
                f"Returning latest. Job IDs: {[e.get('RowKey') for e in entities]}"
            )
            entities.sort(key=lambda e: e.get("CreatedAt", ""), reverse=True)

        return self._from_entity(entities[0])

    async def find_recent_unpicked_by_session(
        self,
        session_id: str,
        max_age_hours: int = 24,
    ) -> Optional[Job]:
        """找出指定 session 內「尚待領取結果」的 job。

        2026.05.19 George × Claude: v1.1 P0 fix — Adaptive Timeout 領取漏洞修正。

        對應主文件 v3 §5.3 補充設計 (2026.05.19 修訂版):
        原本 check_pending_tasks 只用 find_running_by_session 找 status=RUNNING
        的 job,但 Adaptive Timeout + Teams 通知路徑下,任務完成後 status 已是
        COMPLETED,使用者收到通知回來查時找不到任務,誤回 no_running_task。

        本方法擴大語意涵蓋三種「使用者主觀視角還沒結束」的 job:
        - status=RUNNING:還在跑,呼叫端應 long poll
        - status=COMPLETED 且 result_picked_up=False:已完成等領取
        - status=FAILED 且 result_picked_up=False:已失敗等領取

        被 cancel / timeout 的 job 不在範圍 (使用者已知道狀態)。

        max_age_hours 限制避免撈到很舊的殘留 job (Q1 決策:1 天保留期)。
        Table Storage 的 Timestamp 系統欄位是 server-side 寫入時間,可靠。

        Args:
            session_id: PartitionKey。
            max_age_hours: 只看 N 小時內的 job,預設 24 (對齊 Q1 1 天保留期)。

        Returns:
            符合條件最新的 Job,或 None。
            若多筆同時符合 (理論上不該發生,並行控制保證 session 內一次一個),
            選 CreatedAt 最新的並 log warning。
        """
        from datetime import timedelta

        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        ).isoformat()

        # 三種狀態都要 OR 進來:
        # - RUNNING:還沒終態,result_picked_up 預設 False 不用過濾
        # - COMPLETED / FAILED + ResultPickedUp=false:等領取
        # ⚠️ Table Storage 的 OData filter 對 bool 用 true/false (小寫,無引號)
        # ⚠️ 舊資料沒寫過 ResultPickedUp 欄位 → 缺欄等同 false,
        #    用 "ResultPickedUp ne true" 才能涵蓋「缺欄」+「顯式 false」兩種
        filter_str = (
            f"PartitionKey eq '{session_id}' and "
            f"CreatedAt ge '{cutoff}' and "
            f"("
            f"Status eq '{JobStatus.RUNNING.value}' or "
            f"("
            f"(Status eq '{JobStatus.COMPLETED.value}' or "
            f"Status eq '{JobStatus.FAILED.value}') and "
            f"ResultPickedUp ne true"
            f")"
            f")"
        )

        try:
            entities = list(self._table_client.query_entities(filter_str))
        except Exception as e:
            logger.error(
                f"[JobStore] find_recent_unpicked_by_session query failed: "
                f"session={session_id}, error={e}",
                exc_info=True,
            )
            raise

        if not entities:
            return None

        if len(entities) > 1:
            # 並行控制下不該同時有多筆未領取,記 warning 並取最新
            logger.warning(
                f"[JobStore] find_recent_unpicked_by_session found "
                f"{len(entities)} unpicked jobs in session={session_id}, "
                f"expected ≤1. Returning latest. "
                f"Job IDs: {[e.get('RowKey') for e in entities]}"
            )
            entities.sort(key=lambda e: e.get("CreatedAt", ""), reverse=True)

        return self._from_entity(entities[0])

    async def is_cancelled(self, session_id: str, job_id: str) -> bool:
        """檢查 job 是否被 cancel。

        Phase 3 呼叫時機:core_handler.run_workflow 在每個 turn 開始前
        check (主文件 §5.7.2 cooperative cancellation)。

        Args:
            session_id: PartitionKey。
            job_id: RowKey。

        Returns:
            True 若 CancelRequested=True 或 Status=cancelled,False 否則。
            (找不到 entity 時也回 False,讓 cancel checkpoint 不會因為
            race 把正常結束的 turn 誤判為 cancelled)
        """
        job = await self.get(session_id, job_id)
        if job is None:
            return False
        return job.cancel_requested or job.status == JobStatus.CANCELLED.value

    async def list_jobs_by_session(self, session_id: str) -> List[Job]:
        """列出 session 內所有 job (debug / audit 用,不在 hot path)。

        Phase 3 不會用到,Phase 5 驗收標準的 audit trail 會用。
        """
        filter_str = f"PartitionKey eq '{session_id}'"
        entities = self._table_client.query_entities(filter_str)
        return [self._from_entity(e) for e in entities]

    # ────────────────────────────────────────────────────────────────────
    # Entity ↔ Dataclass 轉換
    # Table Storage 欄位用 PascalCase (Azure 慣例),內部 Job dataclass 用
    # snake_case (Python 慣例)。轉換集中在這兩個 helper。
    # ────────────────────────────────────────────────────────────────────

    @staticmethod
    def _snake_to_pascal(snake: str) -> str:
        """snake_case → PascalCase 欄位名轉換。

        例如:
            "session_id" → "SessionId"
            "cancel_requested" → "CancelRequested"
            "task_description" → "TaskDescription"
        """
        return "".join(part.capitalize() for part in snake.split("_"))

    @staticmethod
    def _pascal_to_snake(pascal: str) -> str:
        """PascalCase → snake_case 欄位名轉換。

        例如:
            "SessionId" → "session_id"
            "CancelRequested" → "cancel_requested"
        """
        # 簡易實作:對每個大寫字母前面加 _,然後 lowercase,最後 strip 開頭 _
        result = []
        for i, ch in enumerate(pascal):
            if ch.isupper() and i > 0:
                result.append("_")
            result.append(ch.lower())
        return "".join(result)

    def _to_entity(self, job: Job) -> Dict[str, Any]:
        """Job dataclass → Table Storage entity dict。

        PartitionKey / RowKey 是 Azure SDK 固定欄位名,其他欄位用 PascalCase。
        None 值跳過 (Table Storage 不支援 None,但接受欄位缺席)。
        """
        entity: Dict[str, Any] = {
            "PartitionKey": job.session_id,
            "RowKey": job.job_id,
        }

        # 把 dataclass 其餘欄位映射過去
        # 注意:_snake_to_pascal 是 @staticmethod,用 class name 呼叫
        for snake_key, value in asdict(job).items():
            if snake_key in ("session_id", "job_id"):
                # 已經作為 PartitionKey / RowKey,但顯式存一份方便除錯
                # (主文件 §4 schema 明確要求保留 SessionId / JobId 顯式欄位)
                pass
            if value is None:
                # Table Storage 不支援 None,但允許欄位不存在
                continue
            pascal_key = JobStore._snake_to_pascal(snake_key)
            entity[pascal_key] = value

        # 補上顯式的 SessionId / JobId (即使 _snake_to_pascal 也會生成,
        # 但明確寫出來避免 reader 困惑)
        entity["SessionId"] = job.session_id
        entity["JobId"] = job.job_id

        return entity

    def _from_entity(self, entity: Dict[str, Any]) -> Job:
        """Table Storage entity dict → Job dataclass。

        Azure SDK 回來的 entity 是 dict-like,內含 PartitionKey / RowKey /
        Timestamp 等系統欄位。只挑我們關心的欄位轉換。
        """
        # 先準備一個全空的 Job,再從 entity 填欄位
        job_data: Dict[str, Any] = {
            "session_id": entity.get("PartitionKey", ""),
            "job_id": entity.get("RowKey", ""),
        }

        # 把所有 PascalCase 欄位轉成 snake_case 並塞進 job_data
        # 跳過 Azure 系統欄位 (PartitionKey, RowKey, Timestamp, etag)
        # 注意:_pascal_to_snake 是 @staticmethod,用 class name 呼叫
        skip_keys = {"PartitionKey", "RowKey", "Timestamp", "etag"}
        for pascal_key, value in entity.items():
            if pascal_key in skip_keys:
                continue
            snake_key = JobStore._pascal_to_snake(pascal_key)
            # 只接受 Job dataclass 有定義的欄位 (避免 entity 殘留欄位污染)
            if snake_key in Job.__dataclass_fields__:
                job_data[snake_key] = value

        return Job(**job_data)