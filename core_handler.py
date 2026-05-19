"""
Core Handler - 共用核心邏輯
====================================================================
從 main.py 抽離的 protocol-agnostic 核心,供多個 adapter 共用:
- main.py (Foundry Hosted Agent adapter)
- mcp_server.py (HTTP API adapter for Custom ACA)

包含:
- Workflow 初始化(startup)
- Workflow 執行(run_workflow)
- Session 管理(metadata persist via conversation_store)
- 累積式 turn history(跨輪 context 注入)
- Recent full outputs(N=1 sliding window,保留上一輪完整輸出)
- Debug 工具(skills listing, sync, clear)

VERSION: 1.9
2026.05.19 George × Claude: v1.9 — P0 修 Adaptive Timeout 領取漏洞 (完整版)
- 問題:Adaptive Timeout detach 後背景任務跑完,Job Store status 已更新
  為 COMPLETED,使用者收到 Teams 通知回 thread 問結果時,check_pending_tasks
  用 find_running_by_session 找不到,誤回 no_running_task。
- 衍生問題 (v1.9 初版未涵蓋):「同步 long poll 領取」路徑沒標 picked_up,
  下一輪同 session retry 時新 RUNNING job 被舊 COMPLETED-未領取搶先撈到。
- 修法 (改 3 設計 — picked_up 標記責任歸 _deliver_result_or_notify):
  - 檔頭加 import json (原 v1.8 line 886 用 inline import,v1.9 終態分流
    在 function 外用 json.loads,沒 inline 直接爆 NameError → 必須提升為
    module-level import)
  - job_store.py: Job dataclass 加 result_picked_up 欄位 (預設 False)
  - job_store.py: 新增 find_recent_unpicked_by_session,涵蓋 RUNNING +
    COMPLETED/FAILED 且未領取 (24h 內)
  - 新增 _mark_result_picked_up helper: 統一 picked_up 標記寫入邏輯,
    失敗只 log warning 不阻斷主流程
  - _deliver_result_or_notify 加 session_id 參數,在 set_result 兩條路徑
    (sync + grace-recovered) 後呼叫 _mark_result_picked_up
  - cancelled 路徑的 has_waiter set_result 後也呼叫 _mark_result_picked_up
    (保險;雖然 find_recent_unpicked_by_session filter 不含 CANCELLED,撈
    不到這筆,但語意一致比較好維護)
  - check_pending_tasks 終態分流改用 _mark_result_picked_up (程式碼重用)
  - check_pending_tasks 改用 find_recent_unpicked_by_session 取代
    find_running_by_session
  - 兩個 _deliver_result_or_notify caller (line ~922 正常完成、line ~963
    exception) 都傳 session_id
- 設計取捨:
  - Teams 通知路徑 (沒人在等) 不標 picked_up — 結果只在 Job Store,使用者
    下次 check_pending_tasks 走終態分流時才標記
  - _mark_result_picked_up 失敗只 warning 不 raise — 結果已交付給使用者,
    標記失敗最壞情況是下次同 session 撈到再領一次,使用者多看一次同樣結果
    不致命,比拋例外炸主流程好
- find_running_by_session 不動 (給 run_workflow §5.5 並行檢查用,語意需求
  不同 — 並行檢查只在意「真的還在跑」)
- Q1 決策:結果保留 1 天 (find_recent_unpicked_by_session max_age_hours=24)
- Q2 決策:領取一次後標記 picked_up,不支援連續取回 (UX 上不合理情境)
- Q3 決策:Adaptive Card 不加按鈕/不帶 session_id (使用者打字觸發即可)

VERSION: 1.8
2026.05.18 George × Claude: v1.8 Phase 4 — Teams 通知 Logic App 整合
- 新增 jwt_helper.py (純 base64 decode JWT payload,不驗簽章)
- run_workflow 外殼:解析 __user_token 的 JWT claims (oid + upn),
  傳給 _job_store.create() 寫進 user_id / user_email 欄位
- job_store.py: Job dataclass 加 user_email 欄位、create() 簽章加
  user_email 參數
- _send_teams_notification_stub → _send_teams_notification:
  - 從 Job Store 拉 user identity (補篇 §3 唯一可靠來源,exception
    路徑下 state 可能未建立完整)
  - 組 webhook payload (job_id / session_id / user_email / user_oid /
    task_description / status / result_summary / error)
  - fire-and-forget asyncio.create_task 發送,不阻塞 bg task 收尾
- 新增 _post_webhook_with_retry: exponential backoff 1s/2s/4s,
  最多 4 次嘗試,5xx/408/429 重試,其他 4xx 立即放棄
- TEAMS_NOTIFY_WEBHOOK_URL env var:未設定退回 stub 行為 + log warning
- GRACE_PERIOD_SECONDS 改為 env override (預設 3)
- result_summary 截至 1500 字 (Adaptive Card 容納範圍),Logic App 端
  不做截斷,只負責顯示
- 通知失敗只 log,不寫回 Job Store (Q4 決定 — 通知失敗罕見且任務
  狀態以 Job Store 為準,使用者可在原 thread 主動問 check_pending_tasks)
- 沒動的:Adaptive Timeout 外殼邏輯、cancel/check_pending_tasks、
  _run_coding_agent_inner、_apply_metadata_to_state 等

VERSION: 1.7
2026.05.18 George × Claude: v1.7 Phase 3 — Adaptive Timeout Escalation
- startup() Step 3 擴展 — 加 JobStore 初始化 (Table Storage `jobs`,跟
  ConversationStore 共用同一組 Storage 帳號)。
- startup() 新增 replica_id 計算 (ACA CONTAINER_APP_REPLICA_NAME → hostname
  fallback),供 Job entity ReplicaId 欄位使用。
- run_workflow() 從「直接同步等」改為 adaptive timeout 外殼:
  - §5.6.4 確保 session_id 存在
  - §5.5 並行檢查 → status="rejected"
  - 起 bg task asyncio.create_task(_run_coding_agent_inner(...))
  - wait_for(event.wait(), timeout=ADAPTIVE_TIMEOUT_SECONDS=80)
  - §3.4.1 race window 修正
  - 完成 → 同步路徑回 result;timeout → detach 回 status="running"
- 新增 _run_coding_agent_inner — v1.6 run_workflow 主體搬進來,加結尾
  Job Store 終態更新 + _deliver_result_or_notify 通知分流。
- 新增 _deliver_result_or_notify — §3.5 grace period + 重檢 waiter 邏輯,
  Phase 1 驗證 4 已驗證。
- 新增 _send_teams_notification_stub — Phase 4 才接真實 notifier。
- 新增 cancel_pending_task / check_pending_tasks — §5.3 / §5.7 業務邏輯,
  mcp_server.py 的 MCP tool 對應入口。雙路徑 cancel (Q4 已敲定):
  Job Store cancel flag + executor.cancel(session_id) 同時做。
- sync_skills() re-inject 增加註解:JobStore 不掛在 _workflow,不需 re-inject。
- 沒動的:_apply_metadata_to_state / _inject_session_context /
  _build_turn_summary / approve_pending_skill / reject_pending_skill /
  list_skills / clear_session / is_ready。

VERSION: 1.6
2026.05.12 George × Claude: v1.6 Phase 0 — 抽象介面層依賴注入
- startup() 在 create_workflow() 後額外建立三個介面實作:
  - LocalSubprocessExecutor (從原本的 execute_code() 邏輯抽出)
  - BlobOutputFileStore (從原本的 upload_results() 邏輯抽出)
  - InMemoryJobStateStore (Phase 0 空殼,Phase 3 才填邏輯)
- 注入方式採「create_workflow 返回後 attribute 賦值」,不改 create_workflow 簽名
- BlobOutputFileStore 沿用既有 AZURE_STORAGE_ACCOUNT_* 環境變數,
  缺設定時 file_store=None,維持「沒帳號就 uploads=[] 不 crash」既有行為
- 為什麼介面建立放在 startup() 而非 create_workflow():
  1. 三個介面跟 LLM agent 創建是不同職責,分開更清晰
  2. core_handler 已經負責 conv_store 等基礎設施初始化,介面歸這層更一致
  3. 未來階段 2 切換到 HostedAgentExecutor 時,改 startup() 一處即可,
     不用動 code_agent_hosted.py 內部
- run_workflow() 本體零變更 (依賴注入在 startup 階段做完,run_workflow 只是
  跑 _workflow.run() — _workflow 內部使用注入後的 executor/file_store)

VERSION: 1.5
2026.04.28 George: v1.5 修正跨輪 session_id / work_dir 漂移 bug
- _apply_metadata_to_state: 新增還原 session_id / work_dir / output_files
  / execution_count,並 os.makedirs 確保 work_dir 存在
- run_workflow: effective_session_id 計算延後到 _apply_metadata_to_state
  之後,確保 RowKey 與 entity 內 session_id 永遠一致
- 修正前症狀:同一 conversation 跨輪 create_new_state() 生新 uuid,導致
  state.session_id ≠ caller 傳的 session_id,work_dir 散落在多個
  /app/session_xxx/ 目錄,turn_history 累積不同路徑
- 修正後:state.session_id ≡ Table RowKey ≡ Helper Agent 的 session_id

2026.04.23 George: v1.4 Recent full outputs (N=1 sliding window)
- 新增 RECENT_FULL_OUTPUTS_WINDOW = 1(保留上 N 輪的完整輸出)
- run_workflow: 每輪結束時把 result["response"] 塞進 state.recent_full_outputs
- _apply_metadata_to_state: 從 metadata 還原 recent_full_outputs 回 state
- _inject_session_context: 新增「上一輪的完整輸出」區塊
- 解決:turn summary 只取前 5 行/300 字,導致 Data Agent 回的 markdown table
  下一輪無法回看原始資料的問題
- Helper Agent instructions 無需修改:資料透過現有的 SessionContext
  assistant message 機制注入,與 turn_history / final_code 同管道

2026.03.30 George: v1.3 HITL Skill Review
- 新增 approve_pending_skill(): 審核通過 → 從 pending 讀回 knowledge → merge 寫入 → sync
- 新增 reject_pending_skill(): 審核拒絕 → 刪除 pending
- 新增 imports: blob_read_pending_metadata, blob_delete_pending, blob_upload_skill

2026.03.13 George: v1.1 累積式 turn history
- 將 _inject_final_code_context 擴充為 _inject_session_context
  注入 turn_history 摘要 + original_user_request + final_code
- 新增 _build_turn_summary(): workflow 結束時生成本輪 turn summary
- turn_history 累積到 state.turn_history,由 conversation_store 持久化
- MAX_TURN_HISTORY = 10(保留第一輪 + 最近 N-1 輪)

2026.03.12 George: v1.0 從 main.py v4.0 抽離
- run_workflow(): 核心 workflow 執行(建 state → 載入 metadata → 注入 context → 跑 workflow → 存 metadata)
- startup(): 初始化 workflow + skills sync + conversation store
- list_skills(), sync_skills(), clear_session(): 管理指令
- is_ready(): adapter 啟動前檢查

依賴:
- code_agent_hosted (workflow engine)
- conversation_store (Table Storage 持久化)
- skills_sync (Blob Storage 同步)
"""

import os
import asyncio
import json
import socket
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx  # 2026.05.18 George × Claude: v1.8 Phase 4 — Teams notification HTTP

from code_agent_hosted import (
    CodeAgentWorkflow,
    create_workflow,
    create_new_state,
    ConversationState,
)
from conversation_store import ConversationStore
from skills_sync import (
    sync_skills_from_blob,
    SKILLS_DIR,
)

# 2026.03.30 George: v1.3 HITL — 新增 imports
from skills_sync import (
    blob_read_pending_metadata,
    blob_delete_pending,
    blob_upload_skill,
)

# 2026.03.17 George: v1.2 Identity Passthrough — OBO token exchange
from obo_helper import exchange_all as obo_exchange_all

# 2026.05.18 George × Claude: v1.8 Phase 4 — JWT claims 解析 (oid / upn)
# 用於從 Bearer token 取得 user identity 寫進 Job Store + Teams 通知 payload。
# 純 base64 decode,不驗簽章 — 詳見 jwt_helper.py 模組註解。
from jwt_helper import parse_jwt_claims

# 2026.05.12 George × Claude: v1.6 Phase 0 — 抽象介面層
# 三個介面分別對應 design doc §17 的三個 Protocol。
# 本階段只用階段 1 實作 (Local/Blob/InMemory),階段 2 才會出現 Hosted* 版本。
from code_executor import CodeExecutor, LocalSubprocessExecutor
from output_file_store import OutputFileStore, BlobOutputFileStore
from job_state_store import JobStateStore, InMemoryJobStateStore

# 2026.05.18 George × Claude: v1.7 Phase 3 — Adaptive Timeout Escalation
# JobStore 是持久層 (Table Storage),跟 in-process 的 JobStateStore 不同。
# 在 core_handler 這層直接持有 (不注入 workflow),供 run_workflow /
# cancel_pending_task / check_pending_tasks 使用。
from job_store import JobStore, JobStatus

try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv())
except ImportError:
    pass

logger = logging.getLogger(__name__)


# ============================================================================
# GLOBALS(在 startup 時初始化)
# ============================================================================

_workflow: Optional[CodeAgentWorkflow] = None
_conv_store: Optional[ConversationStore] = None
_credential = None
_project_client = None

# 2026.05.12 George × Claude: v1.6 Phase 0 — 抽象介面層 globals
# 在 startup() 中初始化後注入到 _workflow。對外暴露,供 mcp_server.py 在啟動
# 完成後可印 DI 狀態 log,以及 Phase 3 cancel_pending_task 等 MCP tool 直接
# 取用 _executor / _job_state_store。
_executor: Optional[CodeExecutor] = None
_file_store: Optional[OutputFileStore] = None
_job_state_store: Optional[JobStateStore] = None

# 2026.05.18 George × Claude: v1.7 Phase 3 — Adaptive Timeout Escalation
# Job Store 是 Table Storage 持久層,跟 in-process 的 _job_state_store 不同。
# 對外暴露,供 mcp_server.py 的 cancel_pending_task / check_pending_tasks
# MCP tool 直接取用。
_job_store: Optional[JobStore] = None

# 2026.05.18 George × Claude: v1.7 Phase 3
# replica_id 在 startup 計算一次,寫入 Job entity 的 ReplicaId 欄位用。
# ACA 環境下 CONTAINER_APP_REPLICA_NAME 是平台注入的,fallback 用 hostname。
# 本階段 max_replicas=1,replica_id 主要供 audit / 未來跨 replica 路徑用。
_replica_id: str = ""

# 2026.05.18 George × Claude: v1.7 Phase 3
# Adaptive Timeout 同步等待秒數。短任務 80 秒內完成走同步路徑,
# 超過則 detach 走 Teams 通知。可調區間 60~90 秒,主文件 §3.1。
#ADAPTIVE_TIMEOUT_SECONDS = 80
ADAPTIVE_TIMEOUT_SECONDS = int(os.environ.get("ADAPTIVE_TIMEOUT_SECONDS", "80"))

# 2026.05.18 George × Claude: v1.7 Phase 3
# §3.5 Grace period 秒數,bg task 完成後等使用者重新註冊 waiter 的時間。
# 主文件 §3.5.2 暫定 3 秒,實測調整 (2~5 秒區間)。
# 2026.05.18 George × Claude: v1.8 Phase 4 — 改為 env override
GRACE_PERIOD_SECONDS = int(os.environ.get("GRACE_PERIOD_SECONDS", "3"))

# 2026.05.18 George × Claude: v1.8 Phase 4 — Teams 通知 webhook
# Logic App HTTP trigger URL,bg task 完成且無 waiter 時 POST 到此 URL。
# 未設定時 fallback 為「只 log 不發 HTTP」(維持 Phase 3 stub 行為),
# 並於 startup 記 warning。
TEAMS_NOTIFY_WEBHOOK_URL = os.environ.get("TEAMS_NOTIFY_WEBHOOK_URL", "")

# 2026.05.18 George × Claude: v1.8 Phase 4 — 通知重試常數
# 通知是 fire-and-forget,失敗最多重試 3 次 (共 4 次嘗試),間隔 1/2/4 秒
# exponential backoff。HTTP 5xx 重試,4xx 立即放棄 (payload 結構問題重試
# 也救不回來)。
TEAMS_NOTIFY_MAX_ATTEMPTS = 4  # 1 次首發 + 3 次重試
TEAMS_NOTIFY_TIMEOUT_SECONDS = 10.0
TEAMS_NOTIFY_RESULT_SUMMARY_MAX_CHARS = 1500  # 截至 Adaptive Card 容納範圍


def is_ready() -> bool:
    """檢查 core 是否已初始化。"""
    return _workflow is not None


# ============================================================================
# STARTUP
# ============================================================================

async def startup():
    """
    初始化 workflow + sync skills + conversation store。
    由各 adapter 的 startup 呼叫。

    2026.05.12 George × Claude: v1.6 Phase 0 — 在 step 4 額外建立並注入
    三個介面實作 (CodeExecutor / OutputFileStore / JobStateStore)。

    2026.05.18 George × Claude: v1.7 Phase 3 — Step 3 擴展 JobStore (持久層)
    + 計算 replica_id;新增 step 5 為 cancel checkpoint 等 Phase 3 業務邏輯
    準備好所有依賴。
    """
    global _workflow, _conv_store, _credential, _project_client
    # 2026.05.12 George × Claude: v1.6 Phase 0
    global _executor, _file_store, _job_state_store
    # 2026.05.18 George × Claude: v1.7 Phase 3
    global _job_store, _replica_id

    logger.info("=" * 60)
    logger.info("Core Handler - Starting...")
    logger.info("=" * 60)

    # 2026.05.18 George × Claude: v1.7 Phase 3
    # 計算 replica_id (寫進 Job entity 的 ReplicaId 欄位,供 audit / 未來
    # 跨 replica 路徑使用)。ACA 平台會注入 CONTAINER_APP_REPLICA_NAME,
    # 本地測試 fallback 到 hostname。
    _replica_id = (
        os.environ.get("CONTAINER_APP_REPLICA_NAME")
        or socket.gethostname()
        or "unknown"
    )
    logger.info(f"[Phase 3] replica_id={_replica_id}")

    # 1. 從 Blob Storage 同步 skills 到本地
    try:
        count = await sync_skills_from_blob()
        logger.info(f"Skills sync: {count} skills loaded from Blob Storage")
    except Exception as e:
        logger.warning(f"Skills sync failed (will use local skills only): {e}")

    # 2. 初始化 workflow
    from azure.identity.aio import DefaultAzureCredential as AsyncDefaultAzureCredential
    _credential = AsyncDefaultAzureCredential()
    _workflow, _project_client = await create_workflow(_credential)
    logger.info("Workflow initialized")

    # 3. 初始化 Conversation Store + Job Store (兩者共用同一組 Storage 帳號)
    account_name = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME")
    account_key = os.environ.get("AZURE_STORAGE_ACCOUNT_KEY")
    if account_name and account_key:
        _conv_store = ConversationStore(account_name, account_key)
        await _conv_store.ensure_table()
        logger.info("Conversation Store initialized (Table Storage)")

        # 2026.05.18 George × Claude: v1.7 Phase 3
        # Job Store 跟 Conversation Store 共用 Storage 帳號,但是獨立 table
        # (jobs)。schema 見 job_store.py + 主文件 §4。
        _job_store = JobStore(account_name, account_key)
        await _job_store.ensure_table()
        logger.info("Job Store initialized (Table Storage)")
    else:
        logger.warning(
            "AZURE_STORAGE_ACCOUNT_NAME/KEY not set, "
            "workflow metadata will not persist across requests"
        )
        # _job_store 留 None。run_workflow 進入時會檢查並回報 config 錯誤,
        # 而不是 silently 跳過 — adaptive timeout 機制依賴 persistent job
        # state,沒有它整套都不能用。

    # 4. (Phase 0) 建立抽象介面實作並注入 workflow
    # 2026.05.12 George × Claude: v1.6 Phase 0
    # 注入方式:create_workflow() 返回後直接 setattr,不改 workflow 簽名。
    # 這讓 code_agent_hosted.py 的 CodeAgentWorkflow 變更只是「多兩個 optional
    # attribute」,不影響既有 unit test 與 main.py adapter 的呼叫方式。

    # 4a. CodeExecutor: 階段 1 用 LocalSubprocessExecutor
    # 階段 2 POC 通過後可改成 HostedAgentExecutor,這裡是唯一改動點
    _executor = LocalSubprocessExecutor()
    _workflow.executor = _executor
    logger.info(f"[Phase 0] CodeExecutor injected: {type(_executor).__name__}")

    # 4b. OutputFileStore: 沿用既有 Storage 帳號,缺設定時 file_store=None
    # 維持「沒帳號就 uploads=[] 不 crash」的既有行為 (upload_results() 原本
    # 在缺帳號時就 return []),Phase 0 不改變此行為
    if account_name and account_key:
        _file_store = BlobOutputFileStore(
            account_name=account_name,
            account_key=account_key,
            container_name="code-outputs",  # 維持既有 container 名稱不變
        )
        _workflow.file_store = _file_store
        logger.info(
            f"[Phase 0] OutputFileStore injected: {type(_file_store).__name__}, "
            f"container=code-outputs"
        )
    else:
        _file_store = None
        _workflow.file_store = None
        logger.warning(
            "[Phase 0] OutputFileStore not configured (AZURE_STORAGE_ACCOUNT_* missing); "
            "file uploads will be skipped"
        )

    # 4c. JobStateStore: in-process waiter 機制
    # Phase 0 是空殼,Phase 3 已填完整邏輯 (見 job_state_store.py)。
    _job_state_store = InMemoryJobStateStore()
    _workflow.job_state_store = _job_state_store
    logger.info(
        f"[Phase 3] JobStateStore injected: {type(_job_state_store).__name__}"
    )

    # 2026.05.18 George × Claude: v1.8 Phase 4 — Teams 通知 webhook 狀態
    # 不設定不阻斷啟動 (退回 Phase 3 stub 行為),但記 warning 提醒
    if TEAMS_NOTIFY_WEBHOOK_URL:
        # URL 可能含 sig= 等敏感參數,只 log host + path 前綴
        url_preview = TEAMS_NOTIFY_WEBHOOK_URL.split("?")[0][:80]
        logger.info(
            f"[Phase 4] Teams notification webhook configured: "
            f"{url_preview}..."
        )
    else:
        logger.warning(
            "[Phase 4] TEAMS_NOTIFY_WEBHOOK_URL not set — Teams notifications "
            "will fall back to log-only stub. Set this env var to enable "
            "real notifications via Logic App."
        )
    logger.info(
        f"[Phase 4] GRACE_PERIOD_SECONDS={GRACE_PERIOD_SECONDS}, "
        f"ADAPTIVE_TIMEOUT_SECONDS={ADAPTIVE_TIMEOUT_SECONDS}"
    )

    logger.info("Core Handler ready")


# ============================================================================
# CORE: Workflow execution
# ============================================================================

# 2026.03.13 George: 累積式 turn history 上限
MAX_TURN_HISTORY = 10

# 2026.04.23 George: v1.4 Recent full outputs sliding window 大小
# N=1 代表只保留「上一輪」的完整輸出,給下一輪回看用
# 未來若需擴大(例如 N=3),只需改這個常數,conversation_store schema 不用動
RECENT_FULL_OUTPUTS_WINDOW = 1


async def run_workflow(
    user_input: str,
    session_id: str = None,
    credentials: dict = None,
) -> dict:
    """
    執行 workflow,回傳 result dict。

    Protocol-agnostic — adapter 負責從各自的 request format 提取參數。

    2026.05.18 George × Claude: v1.7 Phase 3 — Adaptive Timeout Escalation
    本函式從「直接同步等」改為 adaptive timeout 外殼:
    - 短任務 (< 80 秒):同步路徑,行為等同 Phase 0 (v1.6) 完全相容
    - 長任務 (≥ 80 秒):detach 進入 bg task,回傳 status="running" + job_id,
      bg task 完成後若無 waiter 則推 Teams 通知 (Phase 4 才接真實 notifier)
    - 並行任務 (同 session 已有 running job):回傳 status="rejected"

    對應主文件 v3 §3 Adaptive Timeout Escalation、§5.5 並行控制、
    §3.4.1 race window 修正、§13.2 完整骨架。

    Args:
        user_input: 用戶的請求文字
        session_id: 可選的 session ID(用於跨 invocation 延續 metadata)
        credentials: 可選的 credentials dict(注入為環境變數)

    Returns:
        以下三種 shape 之一:

        正常完成 (短任務 / race window 修正):
            {success, response, uploads, needs_input, session_id,
             skills_referenced, turn_history (optional)}

        長任務 detach:
            {status: "running", job_id, session_id, task_description, message}

        並行被拒:
            {status: "rejected", reason, session_id, existing_job, message}

        mcp_server.py 的 _build_response_payload 會根據 shape 分流。
    """
    # ─────────────────────────────────────────────────────────────
    # Pre-check: Job Store 必須已初始化才能跑 adaptive timeout。
    # 若 _job_store 是 None (Storage 帳號沒設定),整個 adaptive timeout
    # 機制都無法運作,只能 fallback 到「直接跑」的舊行為。本階段假設
    # production 一定有 _job_store,本 fallback 路徑只供開發 / 測試用。
    # ─────────────────────────────────────────────────────────────
    if _job_store is None or _job_state_store is None:
        logger.warning(
            "[run_workflow] _job_store or _job_state_store not initialized, "
            "falling back to direct synchronous execution (adaptive timeout "
            "disabled). This should only happen in dev/test without Storage."
        )
        return await _run_coding_agent_inner(
            user_input=user_input,
            session_id_hint=session_id,
            credentials=credentials,
            job_id=None,  # 沒 job 追蹤
        )

    # ─────────────────────────────────────────────────────────────
    # Step 1: 確保 session_id 存在 (主文件 §5.6.4)
    # 既有行為 (Phase 0):session_id 為 None 時,_run_coding_agent_inner
    # 內部會用 state.session_id (create_new_state 新生 uuid)。但 §5.5 並行
    # 檢查需要在 inner 跑之前就知道 session_id,所以這裡先確定。
    #
    # 兩種情境:
    #   (a) caller 傳了 session_id → 直接拿來用 (effective_session_id)
    #   (b) caller 沒傳 → 先生個新 uuid 作為 effective_session_id
    # 後者本來在 inner 內 (line _apply_metadata_to_state 後) 才決定,Phase 3
    # 提前到這裡。inner 內邏輯依然會還原 metadata 內的 session_id,只是 RowKey
    # 已經提前確定下來。
    # ─────────────────────────────────────────────────────────────
    if session_id and session_id.strip():
        effective_session_id = session_id.strip()
    else:
        # 沒傳 session_id → 開新 session (上游 v1.5 修正已避免漂移)
        effective_session_id = str(uuid.uuid4())
        logger.info(
            f"[run_workflow] New session created: {effective_session_id}"
        )

    # ─────────────────────────────────────────────────────────────
    # Step 2: §5.5 並行檢查 — 同 session 已有 running job 則拒絕
    # ─────────────────────────────────────────────────────────────
    existing = await _job_store.find_running_by_session(effective_session_id)
    if existing is not None:
        logger.info(
            f"[run_workflow] Concurrent job exists in session="
            f"{effective_session_id}, rejecting new submission. "
            f"existing_job_id={existing.job_id}, "
            f"started_at={existing.created_at}"
        )
        return {
            "status": "rejected",
            "reason": "concurrent_job_exists",
            "session_id": effective_session_id,
            "existing_job": {
                "job_id": existing.job_id,
                "task_description": existing.task_description,
                "started_at": existing.created_at,
            },
            "message": "你還有一個任務在進行中,請先等它完成或取消",
        }

    # ─────────────────────────────────────────────────────────────
    # Step 3: 建 job (Table Storage 持久化) + 註冊 in-process waiter
    # 2026.05.18 George × Claude: v1.8 Phase 4 — 解析 JWT claims
    # 寫進 Job Store user_id / user_email,供 Teams 通知收件人使用。
    # ─────────────────────────────────────────────────────────────
    job_id = str(uuid.uuid4())

    # task_description 取 user_input 前 200 字,給 Teams 通知卡片用
    task_description = user_input[:200] if user_input else ""

    # 2026.05.18 George × Claude: v1.8 Phase 4
    # 從 credentials.__user_token 解析 JWT claims。注意這裡用 .get() 而非
    # .pop() — _run_coding_agent_inner 內 line ~625 還要再用 user_token 做
    # OBO exchange,不能在這裡 pop 掉。JWT 解析是純讀取,無副作用。
    user_oid: Optional[str] = None
    user_email: Optional[str] = None
    if credentials and credentials.get("__user_token"):
        try:
            claims = parse_jwt_claims(credentials["__user_token"])
            user_oid = claims.get("oid")
            user_email = claims.get("user_email")
            if user_oid or user_email:
                logger.info(
                    f"[run_workflow] JWT claims: "
                    f"oid={user_oid!r}, user_email={user_email!r}"
                )
            else:
                # 解析成功但 claims 為空 — token 格式可能不是預期的 Entra ID
                # JWT (例如 opaque token、或 dev/test 用的假 token)
                logger.warning(
                    "[run_workflow] JWT parsed but no oid/upn claims found "
                    "(token may not be Entra ID JWT)"
                )
        except Exception as e:
            # 解析失敗不阻斷流程 — Teams 通知會 fallback 到 user_email=None,
            # Logic App 端應有 graceful degradation
            logger.warning(
                f"[run_workflow] JWT claim extraction failed (continuing "
                f"without user identity): {type(e).__name__}: {e}"
            )

    await _job_store.create(
        session_id=effective_session_id,
        job_id=job_id,
        task_description=task_description,
        user_id=user_oid,
        user_email=user_email,
        replica_id=_replica_id,
        # conversation_ref: 本階段仍傳 None。未來若要從 Bot Framework 拿
        # conversation reference,在這裡塞。Phase 4 走 Logic App + user_email
        # 路徑,不需要 conversation_ref。
    )

    event = await _job_state_store.register_waiter(job_id)

    # ─────────────────────────────────────────────────────────────
    # Step 4: 起 bg task,asyncio.create_task 會在當前 event loop 排程,
    # handler return 之後 bg task 仍存活 (Phase 1 驗證 1 已實證)。
    # ─────────────────────────────────────────────────────────────
    bg_task = asyncio.create_task(
        _run_coding_agent_inner(
            user_input=user_input,
            session_id_hint=effective_session_id,
            credentials=credentials,
            job_id=job_id,
        )
    )
    # 名字方便 debug
    bg_task.set_name(f"coding-agent-{job_id[:8]}")

    # ─────────────────────────────────────────────────────────────
    # Step 5: Adaptive Timeout - 等 ADAPTIVE_TIMEOUT_SECONDS 秒看是否完成
    #
    # ⚠️ 不能直接 asyncio.wait_for(bg_task, timeout=...) — 它預設 timeout 會
    # cancel bg task。改用 wait_for(event.wait(), ...) 等 JobStateStore 的
    # waiter event,timeout 時 bg task 不受影響,繼續跑。
    # 對應主文件 §13.1。
    # ─────────────────────────────────────────────────────────────
    try:
        await asyncio.wait_for(
            event.wait(),
            timeout=ADAPTIVE_TIMEOUT_SECONDS,
        )

        # 同步完成路徑
        result = await _job_state_store.cleanup_result(job_id)
        if result is None:
            # 不該發生:event.set() 後 result 一定已寫入 _results dict。
            # 若真的發生,可能是極端 race 或 bug,回 timeout 邊界 race
            # 修正的 detach 路徑當保險。
            logger.error(
                f"[run_workflow] event set but no result for job={job_id}, "
                f"unexpected state, returning as running"
            )
            return _build_running_response(
                job_id, effective_session_id, task_description
            )

        logger.info(
            f"[run_workflow] Job {job_id} completed synchronously within "
            f"{ADAPTIVE_TIMEOUT_SECONDS}s"
        )
        return result

    except asyncio.TimeoutError:
        # ─────────────────────────────────────────────────────────
        # §3.4.1 Race window 修正:event 沒被 set,但 bg task 可能剛好
        # 在 timeout 邊界完成,result 已寫入但 event.set() 與 wait_for
        # 觸發 TimeoutError 的順序不確定。先檢查 _pending_results。
        # ─────────────────────────────────────────────────────────
        result = await _job_state_store.cleanup_result(job_id)
        if result is not None:
            logger.info(
                f"[run_workflow] Race window caught: job {job_id} completed "
                f"in timeout boundary, returning result directly"
            )
            return result

        # 真的還沒完成,detach 進入 running 狀態
        logger.info(
            f"[run_workflow] Job {job_id} exceeded "
            f"{ADAPTIVE_TIMEOUT_SECONDS}s, detaching to background"
        )
        return _build_running_response(
            job_id, effective_session_id, task_description
        )

    finally:
        # 無論走哪條路徑,waiter dict 都要清乾淨,避免 leak。
        await _job_state_store.cleanup_waiter(job_id)


def _build_running_response(
    job_id: str,
    session_id: str,
    task_description: str,
) -> dict:
    """組「長任務 detach」回應 (主文件 §5.2)。"""
    return {
        "status": "running",
        "job_id": job_id,
        "session_id": session_id,
        "task_description": task_description,
        "message": "任務較長,完成後會主動通知你",
    }


async def _run_coding_agent_inner(
    user_input: str,
    session_id_hint: Optional[str],
    credentials: Optional[dict],
    job_id: Optional[str],
) -> dict:
    """
    Coding Agent 的核心執行函式 (Phase 0 v1.6 run_workflow 主體搬進來)。

    2026.05.18 George × Claude: v1.7 Phase 3 抽出此函式
    把 v1.6 run_workflow 的主體 (line 325-449) 整段搬進來,
    新增完成路徑的 Job Store 終態更新 + _deliver_result_or_notify 通知分流。

    執行邏輯 100% 保留 (從 create_new_state 到 save_metadata,流程不變):
    1. 建 state、注入 credentials、OBO exchange
    2. 從 Table Storage 載入 metadata、_apply_metadata_to_state
    3. _inject_session_context (turn history + recent_full_outputs + final_code)
    4. await _workflow.run(user_input, state) 跑 coding agent loop
    5. 累積 turn_history、更新 recent_full_outputs
    6. save_metadata
    7. 回傳 result dict

    Phase 3 新增的尾巴邏輯:
    8. (若 job_id 非 None) Job Store 更新終態 + result JSON 存 result 欄位
    9. (若 job_id 非 None) _deliver_result_or_notify 走 waiter 或 Teams 通知

    Args:
        user_input: 使用者請求文字
        session_id_hint: 外殼決定的 effective session_id (從外面傳進來,
                        確保跟 Job Store entity 的 PartitionKey 一致)
        credentials: 含 __user_token 的 dict,OBO exchange 在內部進行
        job_id: 對應的 Job ID。None 表示 fallback 路徑 (沒有 _job_store
               時的直接同步呼叫),這條路徑下不更新 Job Store,也不走
               通知分流,行為等同 Phase 0 v1.6 的 run_workflow。

    Returns:
        result dict — 與 Phase 0 v1.6 run_workflow 回傳格式相同。
    """
    # 用 try/except 包整段,確保任何例外都會更新 Job Store 為 failed
    # (否則 bg task 拋例外 detach 後,Job Store 會永遠 stuck 在 running)
    try:
        # ─────────────────────────────────────────────────────────
        # 以下整段邏輯與 Phase 0 v1.6 run_workflow 主體相同,除了:
        # - session_id 來自 session_id_hint (外殼算好的 effective)
        # - 結尾新增 job_id 完成路徑
        # ─────────────────────────────────────────────────────────

        # 1. 建立新的 state
        state = create_new_state()

        # 注入 credentials 為環境變數(如果有的話)
        # 2026.03.17 George: v1.2 Identity Passthrough — OBO exchange
        if credentials:
            # 提取 user token(由 mcp_server.py 注入的 __user_token)
            user_token = credentials.pop("__user_token", None)
            if user_token:
                try:
                    obo_tokens = await obo_exchange_all(user_token)
                    if obo_tokens:
                        state.user_data.update(obo_tokens)
                        logger.info(
                            f"[OBO] Injected {len(obo_tokens)} resource tokens: "
                            f"{list(obo_tokens.keys())}"
                        )
                except ValueError as e:
                    # OBO 設定不完整(缺 env vars)— log warning 但不阻斷
                    logger.warning(f"[OBO] Skipped (config incomplete): {e}")
                except Exception as e:
                    logger.error(f"[OBO] Unexpected error during exchange: {e}")

            # 剩餘的 credentials 照舊注入(向後相容)
            if credentials:
                state.user_data.update(credentials)
                logger.info(f"[State] Injected {len(credentials)} credentials as env vars")

        # 2. 從 Table Storage 載入 metadata
        # session_id_hint 是外殼算好的 effective_session_id (Phase 3 新增)。
        # 若為 None (fallback 路徑),behavior 同 Phase 0 v1.6 — 不載入。
        metadata = None
        if _conv_store and session_id_hint:
            metadata = await _conv_store.load_metadata(session_id_hint)
            if metadata:
                _apply_metadata_to_state(state, metadata)
                logger.info(
                    f"[State] Loaded metadata: session={session_id_hint}, "
                    f"exec_count={metadata.get('execution_count', 0)}, "
                    f"turns={len(metadata.get('turn_history', []))}, "
                    f"recent_full_outputs={len(metadata.get('recent_full_outputs', {}))}"
                )

        # state.session_id 此時若有 metadata 已被還原,沒有則仍是
        # create_new_state() 生的新 uuid。effective_session_id 用於後續
        # 寫回 Table 的 RowKey。
        effective_session_id = session_id_hint or state.session_id

        # 2.5 (Phase 3 新增) 同步 state.session_id 與 effective_session_id
        # 若 Phase 0 v1.5 修正後 state.session_id 已經跟 effective 一致 (有
        # metadata 還原),這行是 no-op。沒 metadata 還原時 (e.g. 新 session),
        # 強制把 state.session_id 設為外殼決定的 effective,確保
        # executor.execute(session_id=state.session_id, ...) 跟 Job Store
        # PartitionKey 一致,以及 cancel 路徑能找對 proc。
        if session_id_hint:
            state.session_id = session_id_hint

        # 2.7 (Phase 3 新增) 把 job_id 寫進 state,讓 code_agent_hosted.py
        # 的 turn loop 可以在每個 turn 開始前檢查 cancel flag。
        # state.job_id 是 Phase 3 新增的 attribute (ConversationState 需要
        # 新增此欄位,預設 None)。fallback 路徑下 job_id=None。
        state.job_id = job_id

        # 3. 注入 session context
        if metadata:
            _inject_session_context(state, metadata)

        # 4. 執行 workflow(workflow.run 內部會 add_user_message)
        result = await _workflow.run(user_input, state)

        # 4.5 生成 turn summary 並累積到 turn_history
        turn_history = (metadata or {}).get("turn_history", [])
        new_turn = _build_turn_summary(
            turn_number=len(turn_history) + 1,
            user_request=user_input,
            result=result,
            state=state,
        )
        turn_history.append(new_turn)

        # 控制 history 長度:保留第一輪 + 最近 N-1 輪
        if len(turn_history) > MAX_TURN_HISTORY:
            turn_history = [turn_history[0]] + turn_history[-(MAX_TURN_HISTORY - 1):]
            logger.info(
                f"[State] turn_history trimmed to {len(turn_history)} turns "
                f"(max={MAX_TURN_HISTORY})"
            )

        state.turn_history = turn_history

        # 4.6 更新 recent_full_outputs(N=1 sliding window)
        full_response = result.get("response", "") or ""
        if full_response.strip():
            new_entry = {str(new_turn["turn"]): full_response}

            if RECENT_FULL_OUTPUTS_WINDOW <= 1:
                # N=1: 直接覆蓋(最常見路徑,最省)
                state.recent_full_outputs = new_entry
            else:
                # N>1: 合併舊的 + 新的,保留最近 N 筆
                existing = (metadata or {}).get("recent_full_outputs", {}) or {}
                merged = {**existing, **new_entry}
                # 按 turn index 數值大小排序,保留最新的 N 筆
                sorted_keys = sorted(merged.keys(), key=lambda k: int(k))
                kept_keys = sorted_keys[-RECENT_FULL_OUTPUTS_WINDOW:]
                state.recent_full_outputs = {k: merged[k] for k in kept_keys}

            logger.info(
                f"[State] recent_full_outputs updated: "
                f"turn={new_turn['turn']}, size={len(full_response)} chars, "
                f"window={RECENT_FULL_OUTPUTS_WINDOW}"
            )
        else:
            # 本輪沒有有效輸出,清空(避免保留過期資料誤導下一輪)
            state.recent_full_outputs = {}

        # 5. 儲存 metadata 到 Table Storage
        if _conv_store:
            await _conv_store.save_metadata(effective_session_id, state)

        # 6. 在 result 中帶回 session_id(供 caller 下輪帶回)
        result["session_id"] = effective_session_id
        result["skills_referenced"] = state.skills_referenced

        # ─────────────────────────────────────────────────────────
        # 7. (Phase 3 新增) Cancel 檢查 + Job Store 終態 + 通知分流
        # ─────────────────────────────────────────────────────────
        if job_id is not None and _job_store is not None:
            # 7a. 檢查在 bg task 跑期間是否被 cancel
            #
            # core_handler.run_workflow 內無法在 turn 邊界檢查 cancel flag,
            # 那部分要由 code_agent_hosted.py 的 turn loop 處理。但在這裡
            # bg task 收尾時可以再檢查一次:若使用者在 task 跑到一半 cancel,
            # 且 turn loop 已經 break 出來,本檢查可以把 Job Store 標為
            # cancelled 而非 completed。
            cancelled = await _job_store.is_cancelled(effective_session_id, job_id)

            if cancelled:
                logger.info(
                    f"[Job {job_id}] Detected cancel flag at completion, "
                    f"marking as cancelled (not completed)"
                )
                await _job_store.update(
                    effective_session_id, job_id,
                    status=JobStatus.CANCELLED.value,
                )
                # 不走 _deliver_result_or_notify (主文件 §5.7.2:cancelled
                # 不推 Teams 通知,使用者已主動取消)。但若同步 waiter 仍在
                # 等,還是要把結果交付給他 (使用者 cancel 後又留在 thread
                # 的場景)。
                if await _job_state_store.has_waiter(job_id):
                    # 給 waiter 一個 cancelled shape 的結果,讓他知道任務
                    # 是被取消而非自然完成
                    cancelled_result = {
                        "status": "cancelled",
                        "session_id": effective_session_id,
                        "job_id": job_id,
                        "message": "任務已取消",
                    }
                    await _job_state_store.set_result(job_id, cancelled_result)
                    # 2026.05.19 v1.9: cancelled 也算已交付給 waiter,標 picked_up
                    # 避免下輪同 session 撈到這筆 cancelled 的舊 job
                    await _mark_result_picked_up(effective_session_id, job_id)
                return result  # bg task 收尾退出

            # 7b. 正常完成 — 寫終態到 Job Store
            success = result.get("success", False)
            terminal_status = (
                JobStatus.COMPLETED.value if success else JobStatus.FAILED.value
            )

            # 嘗試把 result serialize 進 Job Store 的 Result 欄位,失敗也不阻斷
            # 2026.05.19 George × Claude: v1.9 — json 已移到檔頭 import,刪 inline
            try:
                result_json = json.dumps(result, ensure_ascii=False, default=str)
                # Table Storage 字串欄位有 64KB 上限,超過就只存標記
                if len(result_json) > 60_000:
                    result_json = (
                        '{"_note": "result too large to persist in Job Store, '
                        'see _conv_store metadata for details"}'
                    )
            except Exception as e:
                logger.warning(
                    f"[Job {job_id}] Failed to serialize result: {e}"
                )
                result_json = None

            update_fields = {"status": terminal_status}
            if result_json:
                update_fields["result"] = result_json
            if not success:
                # error 訊息存 stderr / response 的 fallback
                err_msg = result.get("response", "") or "(no error message)"
                update_fields["error"] = err_msg[:5000]  # 截一下避免太大

            try:
                await _job_store.update(
                    effective_session_id, job_id,
                    **update_fields,
                )
            except Exception as e:
                # 寫終態失敗不該阻斷通知 — log 並繼續走 _deliver_result_or_notify
                logger.error(
                    f"[Job {job_id}] Failed to update Job Store terminal "
                    f"state: {e}",
                    exc_info=True,
                )

            # 7c. 通知分流 (主文件 §3.5)
            # 2026.05.19 v1.9: 加 session_id 讓 picked_up 標記能寫回 Table Storage
            await _deliver_result_or_notify(
                job_id=job_id,
                session_id=effective_session_id,
                result=result,
                is_failure=(not success),
            )

        return result

    except Exception as e:
        # bg task 拋例外 — 必須更新 Job Store,避免 stuck 在 running
        logger.error(
            f"[Job {job_id}] Coding agent raised exception: {type(e).__name__}: {e}",
            exc_info=True,
        )

        # 構造 failure result
        failure_result = {
            "success": False,
            "response": f"任務執行失敗:{type(e).__name__}: {e}",
            "session_id": session_id_hint,
            "skills_referenced": [],
            "uploads": [],
        }

        if job_id is not None and _job_store is not None:
            try:
                await _job_store.update(
                    session_id_hint, job_id,
                    status=JobStatus.FAILED.value,
                    error=f"{type(e).__name__}: {e}"[:5000],
                )
            except Exception as e2:
                logger.error(
                    f"[Job {job_id}] Failed to update Job Store after "
                    f"exception: {e2}"
                )

            # 失敗也要通知 — 主文件 §8.3「失敗通知比成功通知更重要」
            # 2026.05.19 v1.9: 加 session_id 讓 picked_up 標記能寫回 Table Storage
            await _deliver_result_or_notify(
                job_id=job_id,
                session_id=session_id_hint,
                result=failure_result,
                is_failure=True,
            )

        return failure_result


async def _deliver_result_or_notify(
    job_id: str,
    session_id: str,
    result: dict,
    is_failure: bool,
) -> None:
    """
    Bg task 完成後決定走 in-memory waiter 還是 Teams 通知。

    對應主文件 v3 §3.5 設計:
    1. 第一次檢查 waiter — 有就 set_result 走同步路徑
    2. 沒有 → 進入 grace period (3 秒,給使用者重新註冊 waiter 的機會)
    3. Grace period 結束後再次檢查 — 仍沒人在等才推 Teams

    Phase 1 驗證 4 已通過此邏輯,baseline 在 phase1_race_test.py。

    2026.05.19 George × Claude: v1.9 — 加 session_id 參數 + picked_up 標記
    -----------------------------------------------------------
    走 set_result (in-memory event) 路徑 = 結果已透過同步通道交付給
    check_pending_tasks 的 waiter,等同已被使用者領取 → 標 picked_up=True,
    避免下一輪同 session 的 check_pending_tasks 又把這筆舊 job 撈回來。
    走 Teams 通知路徑 = 沒人在等,結果只存在 Job Store,等使用者下次來查 →
    picked_up 維持 False,讓 check_pending_tasks 走終態分流回傳。

    對應 v1.9 P0 修正:解決同一 session 跨輪 retry 時,新任務的 RUNNING
    被舊任務的 COMPLETED-未領取搶先撈到的 race。

    Args:
        job_id: 對應的 Job ID
        session_id: 對應的 session ID (PartitionKey,寫 picked_up 標記用)
        result: 要交付的結果 (run_coding_agent_inner 的回傳)
        is_failure: True 表示是 failure case,Teams 通知會用失敗模板
    """
    # 第一次檢查 — 同步路徑
    if await _job_state_store.has_waiter(job_id):
        logger.info(
            f"[Notify] Job {job_id} has active waiter, delivering via "
            f"in-memory event (sync path)"
        )
        await _job_state_store.set_result(job_id, result)
        # 2026.05.19 v1.9: 走 in-memory event = 已交付給 waiter = 已領取
        await _mark_result_picked_up(session_id, job_id)
        return

    # 沒人在等 — 進入 grace period
    logger.info(
        f"[Notify] Job {job_id} has no waiter, entering grace period "
        f"({GRACE_PERIOD_SECONDS}s)"
    )
    await asyncio.sleep(GRACE_PERIOD_SECONDS)

    # Grace period 結束後重新檢查 — 使用者可能在這 N 秒內呼叫了
    # check_pending_tasks 重新註冊 waiter
    if await _job_state_store.has_waiter(job_id):
        logger.info(
            f"[Notify] Job {job_id} waiter appeared during grace period, "
            f"delivering via in-memory event (grace-recovered path)"
        )
        await _job_state_store.set_result(job_id, result)
        # 2026.05.19 v1.9: 同上,grace-recovered 也算已領取
        await _mark_result_picked_up(session_id, job_id)
        return

    # 真的沒人在等 — Teams 通知
    # 2026.05.18 George × Claude: v1.8 Phase 4 — 換到真實 notifier
    # 2026.05.19 v1.9: 推 Teams 通知時 picked_up 保持 False,
    # 等使用者回 thread 觸發 check_pending_tasks 終態分流時才標記。
    await _send_teams_notification(
        job_id=job_id,
        result=result,
        is_failure=is_failure,
    )


async def _mark_result_picked_up(session_id: str, job_id: str) -> None:
    """標記 job 的 result_picked_up=True,失敗只 log 不阻斷主流程。

    2026.05.19 George × Claude: v1.9 — P0 修 Adaptive Timeout 領取漏洞。

    呼叫時機:
    - _deliver_result_or_notify 走 set_result 路徑後 (in-memory event 已交付)
    - check_pending_tasks 終態分流取回 result 後 (Table Storage 直接讀取)

    寫入失敗 (Storage 短暫故障 / 網路斷線) 的容錯:
    - 主流程已經把結果交給使用者,標記只是為了避免下次同 session 撈到舊 job
    - 失敗時 log warning,下次同 session 來查還是會撈到這筆 (走終態分流再領
      一次,使用者多看一次同樣結果不致命,比拋例外炸掉好)
    """
    if _job_store is None:
        return
    try:
        await _job_store.update(
            session_id, job_id,
            result_picked_up=True,
        )
    except Exception as e:
        logger.warning(
            f"[_mark_result_picked_up] Failed to mark job={job_id} "
            f"in session={session_id}: {e}"
        )


async def _send_teams_notification(
    job_id: str,
    result: dict,
    is_failure: bool,
) -> None:
    """
    Teams 通知 — 從 Job Store 拉 user identity,fire-and-forget POST 到
    Logic App webhook。

    2026.05.18 George × Claude: v1.8 Phase 4
    取代 Phase 3 stub。Phase 4 範圍:
    - 從 Job Store 拉 user_email / user_oid / task_description
      (create 時已寫入,這裡是唯一可靠來源 — 因為 exception 路徑下
       state 可能根本沒建立完整)
    - 組 webhook payload
    - fire-and-forget asyncio.create_task 發送 (不 await,bg task 立即釋放)
    - exponential backoff 重試 1s/2s/4s (主文件 §8 失敗策略)

    一致性語意:best-effort 通知。任務狀態以 Job Store 為準,通知遺失時
    使用者仍可在原 thread 內主動問 check_pending_tasks 取得結果。
    這個取捨在 Phase 1.5 完成 Hosted Agent 搬遷後會有更好的解法,
    本階段不做 persistent queue。

    Args:
        job_id: 對應的 Job ID
        result: 要顯示的結果 (run_coding_agent_inner 回傳)
        is_failure: True 表示是失敗情況
    """
    session_id = result.get("session_id", "")

    if not TEAMS_NOTIFY_WEBHOOK_URL:
        # 沒設定 webhook → 維持 Phase 3 stub 行為 (只 log)
        kind = "FAILURE" if is_failure else "COMPLETION"
        response_preview = (result.get("response") or "")[:200]
        logger.warning(
            f"[Teams Notification] TEAMS_NOTIFY_WEBHOOK_URL not set, "
            f"falling back to stub log. kind={kind}, job={job_id}, "
            f"session={session_id}, preview={response_preview!r}"
        )
        return

    # ── 從 Job Store 拉 user identity (補篇 §3 — 唯一可靠來源) ──
    user_email: Optional[str] = None
    user_oid: Optional[str] = None
    task_description = ""
    if _job_store is not None:
        try:
            job = await _job_store.get(session_id, job_id)
            if job is not None:
                user_email = job.user_email
                user_oid = job.user_id
                task_description = job.task_description or ""
            else:
                logger.warning(
                    f"[Teams Notification] Job {job_id} not found in Job "
                    f"Store, payload will have null user_email/user_oid"
                )
        except Exception as e:
            # Job Store 讀取失敗不阻斷通知 — 用空欄位送出,Logic App 應有
            # graceful degradation
            logger.error(
                f"[Teams Notification] Job Store read failed for "
                f"job={job_id}: {type(e).__name__}: {e}",
                exc_info=True,
            )

    # ── 組 result_summary (截至 Adaptive Card 容納範圍) ──
    if is_failure:
        result_summary = ""
        error_msg = result.get("response", "") or "未提供錯誤訊息"
        # 錯誤訊息也截一下,避免 Adaptive Card 渲染爆掉
        error_msg = error_msg[:TEAMS_NOTIFY_RESULT_SUMMARY_MAX_CHARS]
    else:
        full_response = result.get("response", "") or ""
        result_summary = full_response[:TEAMS_NOTIFY_RESULT_SUMMARY_MAX_CHARS]
        error_msg = ""

    payload = {
        "job_id": job_id,
        "session_id": session_id,
        "user_email": user_email,
        "user_oid": user_oid,
        "task_description": task_description,
        "status": "failed" if is_failure else "completed",
        "result_summary": result_summary,
        "error": error_msg if is_failure else None,
    }

    logger.info(
        f"[Teams Notification] Scheduling webhook POST for job={job_id}, "
        f"status={payload['status']}, user_email={user_email!r}"
    )

    # ── fire-and-forget:不 await,bg task 立即釋放 ──
    # 重試邏輯在 _post_webhook_with_retry 內。即使重試全失敗也只 log,
    # 不影響 Job Store 終態 (已寫 completed/failed)。
    asyncio.create_task(_post_webhook_with_retry(payload))


async def _post_webhook_with_retry(payload: dict) -> None:
    """
    對 Teams Logic App webhook 發送 POST,失敗時 exponential backoff 重試。

    2026.05.18 George × Claude: v1.8 Phase 4

    重試策略 (主文件 §8 失敗策略):
    - 最多 TEAMS_NOTIFY_MAX_ATTEMPTS=4 次嘗試 (1 次首發 + 3 次重試)
    - 重試間隔 1s / 2s / 4s exponential backoff
    - 5xx / 連線錯誤 / timeout → 重試
    - 4xx → 立即放棄 (payload 結構問題,重試也救不回來)

    這個函式跑在 asyncio.create_task 起的 fire-and-forget task 內,
    不被 caller await,失敗不影響 bg task 收尾與 Job Store 終態。

    Args:
        payload: 完整 webhook payload (含 job_id / status / result_summary 等)
    """
    job_id = payload.get("job_id", "(unknown)")
    backoff_seconds = [1, 2, 4]  # 對應第 2/3/4 次嘗試前的等待

    for attempt in range(1, TEAMS_NOTIFY_MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=TEAMS_NOTIFY_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    TEAMS_NOTIFY_WEBHOOK_URL,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )

            # 2xx → 成功,結束
            if 200 <= response.status_code < 300:
                logger.info(
                    f"[Teams Notification] Webhook POST success for "
                    f"job={job_id} on attempt {attempt}/"
                    f"{TEAMS_NOTIFY_MAX_ATTEMPTS} "
                    f"(HTTP {response.status_code})"
                )
                return

            # 4xx (除了 408 / 429) → 立即放棄
            if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
                logger.error(
                    f"[Teams Notification] Webhook POST failed with "
                    f"non-retriable {response.status_code} for job={job_id}: "
                    f"{response.text[:500]!r}. Giving up."
                )
                return

            # 5xx / 408 / 429 → 重試
            logger.warning(
                f"[Teams Notification] Webhook POST got HTTP "
                f"{response.status_code} for job={job_id} on attempt "
                f"{attempt}/{TEAMS_NOTIFY_MAX_ATTEMPTS}. "
                f"Response: {response.text[:200]!r}"
            )

        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
            # 連線錯誤 / timeout / 對方斷線 → 重試
            logger.warning(
                f"[Teams Notification] Webhook POST network error for "
                f"job={job_id} on attempt {attempt}/"
                f"{TEAMS_NOTIFY_MAX_ATTEMPTS}: {type(e).__name__}: {e}"
            )

        except Exception as e:
            # 預期外的例外 (例如 JSON 序列化問題) → 立即放棄
            logger.error(
                f"[Teams Notification] Webhook POST unexpected error for "
                f"job={job_id}: {type(e).__name__}: {e}. Giving up.",
                exc_info=True,
            )
            return

        # 還沒到最後一次 → 等 backoff 後重試
        if attempt < TEAMS_NOTIFY_MAX_ATTEMPTS:
            wait_seconds = backoff_seconds[attempt - 1]
            logger.info(
                f"[Teams Notification] Retrying job={job_id} after "
                f"{wait_seconds}s backoff..."
            )
            await asyncio.sleep(wait_seconds)

    # 所有重試耗盡 — 只 log,不寫回 Job Store (Q4 決定)
    logger.error(
        f"[Teams Notification] All {TEAMS_NOTIFY_MAX_ATTEMPTS} attempts "
        f"exhausted for job={job_id}. Notification lost. User can still "
        f"retrieve result via check_pending_tasks in the original thread."
    )


# ============================================================================
# Phase 3 NEW: cancel_pending_task / check_pending_tasks
# 2026.05.18 George × Claude: v1.7 Phase 3
#
# 對應主文件 v3 §5.3 (check_pending_tasks) / §5.7 (cancel_pending_task)。
# 這兩個函式是 mcp_server.py 的 MCP tool 對應業務邏輯,MCP tool 本身是
# 薄殼,實作集中在這裡。
# ============================================================================


async def cancel_pending_task(session_id: str) -> dict:
    """
    取消指定 session 目前 running 中的 coding job。

    對應主文件 §5.7.2 cooperative cancellation 設計。
    雙路徑取消 (Q4 已敲定):
    1. 寫 Job Store cancel flag — turn loop 下個 iteration 會看到 (跨 turn)
    2. 呼叫 executor.cancel(session_id) — 中斷正在跑的 subprocess (turn 內)

    兩個動作都要做,因為:
    - 若有 subprocess 正在跑 → executor.cancel 立即 SIGTERM → 當前 turn
      execute() 收到非零 returncode 退出 → turn loop 下個 iteration 看到
      cancel flag → break
    - 若 turn 之間沒 subprocess → executor.cancel 回 False (no-op) → turn
      loop 下個 iteration 看到 flag → break

    回傳的是「動作確認」,不等 bg task 真的結束。實際結束時間取決於
    subprocess 是否在 30 秒 grace 內優雅收尾 (詳見 code_executor.py
    cancel())。

    Args:
        session_id: 要取消的 session ID

    Returns:
        三種狀態之一:
        - {"status": "no_running_task", ...} — 找不到 running job
        - {"status": "cancelling", ...} — 已發送取消請求
        - {"status": "error", "error": "..."} — 異常情況
    """
    if _job_store is None:
        return {
            "status": "error",
            "session_id": session_id,
            "error": "Job Store not initialized — Storage credentials missing",
        }

    # 找 running job
    job = await _job_store.find_running_by_session(session_id)
    if job is None:
        return {
            "status": "no_running_task",
            "session_id": session_id,
            "message": "目前沒有進行中的任務",
        }

    job_id = job.job_id
    logger.info(
        f"[cancel_pending_task] session={session_id}, job_id={job_id}, "
        f"task={job.task_description[:50]!r}"
    )

    # 路徑 1:寫 cancel flag (turn 邊界 checkpoint 會看到)
    try:
        await _job_store.update(
            session_id, job_id,
            cancel_requested=True,
        )
    except Exception as e:
        logger.error(
            f"[cancel_pending_task] Failed to write cancel flag for "
            f"job={job_id}: {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "session_id": session_id,
            "job_id": job_id,
            "error": f"Failed to write cancel flag: {e}",
        }

    # 路徑 2:呼叫 executor.cancel(session_id) 中斷可能正在跑的 subprocess
    # 即使回 False (沒 proc 在跑) 也 OK,flag 已經寫了,turn loop 會看到
    executor_cancelled = False
    if _executor is not None:
        try:
            executor_cancelled = await _executor.cancel(session_id)
            logger.info(
                f"[cancel_pending_task] executor.cancel returned "
                f"{executor_cancelled} for session={session_id}"
            )
        except Exception as e:
            # executor.cancel 拋例外不該擋住取消流程 — flag 已經寫了
            logger.error(
                f"[cancel_pending_task] executor.cancel raised exception: {e}",
                exc_info=True,
            )

    return {
        "status": "cancelling",
        "session_id": session_id,
        "job_id": job_id,
        "task_description": job.task_description,
        "executor_cancelled": executor_cancelled,
        "message": "已請求取消,正在收尾中",
    }


async def check_pending_tasks(
    session_id: str,
    max_wait: int = ADAPTIVE_TIMEOUT_SECONDS,
) -> dict:
    """
    查詢 session 內 running 任務的狀態。若有 running job,進行 long polling
    最多 max_wait 秒等待完成。

    對應主文件 §5.3 設計。Helper Agent 在使用者問「好了嗎」時呼叫此函式。

    語意 (主文件 §5.3.2,2026.05.19 修訂):
    - 找不到「未結束 / 已結束未領取」的 job → 立即回 no_running_task
    - 找到 RUNNING job → 註冊 waiter,await event,等到完成或 timeout
      - 完成 → 回 completed result (跟同步路徑同 shape)
      - Timeout → 回 still_running (基本上等同 status: running,但代表已等過)
    - 找到 COMPLETED / FAILED 且未領取的 job → 直接回 result + 標記
      result_picked_up=True (避免重複領取)
      ⭐ 此分支處理「Adaptive Timeout 已 detach + Teams 通知已發 + 使用者
         回 thread 詢問結果」的情境。2026.05.19 P0 修正。

    §3.4.1 race window 修正也應用在這裡 — wait_for 觸發 TimeoutError 後
    先檢查 _results 是否在邊界已寫入。

    Args:
        session_id: 要查詢的 session ID
        max_wait: 最多等多少秒,預設 80 秒 (對齊 ADAPTIVE_TIMEOUT_SECONDS)

    Returns:
        多種狀態:
        - no_running_task: 沒有未結束 / 已結束未領取的 job
        - completed/failed/...: 正常完成 (走 _build_response_payload 那條 shape)
        - still_running: long polling 也沒等到,告訴使用者再等等
        - error: 異常
    """
    if _job_store is None or _job_state_store is None:
        return {
            "status": "error",
            "session_id": session_id,
            "error": "Job Store not initialized",
        }

    # 2026.05.19 George × Claude: v1.9 — P0 修 Adaptive Timeout 領取漏洞
    # 改用 find_recent_unpicked_by_session,涵蓋 RUNNING + 已結束未領取兩種,
    # 解決「使用者收 Teams 通知回 thread 後查詢卻回 no_running_task」的問題。
    job = await _job_store.find_recent_unpicked_by_session(session_id)
    if job is None:
        return {
            "status": "no_running_task",
            "session_id": session_id,
            "message": "目前沒有進行中的任務",
        }

    # 2026.05.19 P0 fix:已終態未領取 → 直接回 result + 標記領取
    # 對應 Teams 通知後使用者回 thread 取結果的路徑。
    if job.status in (JobStatus.COMPLETED.value, JobStatus.FAILED.value):
        logger.info(
            f"[check_pending_tasks] Found terminal-unpicked job={job.job_id} "
            f"in session={session_id}, status={job.status}, "
            f"delivering stored result"
        )

        # 從 Table Storage 的 result 欄位還原 result dict
        if job.result:
            try:
                result = json.loads(job.result)
            except json.JSONDecodeError as e:
                logger.error(
                    f"[check_pending_tasks] Failed to parse stored result "
                    f"for job={job.job_id}: {e}"
                )
                # 退而求其次:回最小可用 result,讓使用者至少知道狀態
                result = {
                    "success": (job.status == JobStatus.COMPLETED.value),
                    "response": (
                        job.error
                        or "(任務已完成但結果序列化異常,請聯絡管理員)"
                    ),
                    "session_id": session_id,
                    "skills_referenced": [],
                    "uploads": [],
                }
        else:
            # COMPLETED 應該都有 result;FAILED 可能只有 error。組最小 shape
            result = {
                "success": (job.status == JobStatus.COMPLETED.value),
                "response": job.error or "(任務已結束但無詳細結果)",
                "session_id": session_id,
                "skills_referenced": [],
                "uploads": [],
            }

        # 標記已領取,後續同 session 查詢不會再撈到此筆
        # 2026.05.19 v1.9: 抽成 _mark_result_picked_up 跟同步路徑共用邏輯
        await _mark_result_picked_up(session_id, job.job_id)

        return result

    # RUNNING → 既有 long poll 邏輯維持不變
    job_id = job.job_id
    logger.info(
        f"[check_pending_tasks] Found running job={job_id} in session="
        f"{session_id}, registering waiter with max_wait={max_wait}s"
    )

    event = await _job_state_store.register_waiter(job_id)
    try:
        try:
            await asyncio.wait_for(event.wait(), timeout=max_wait)
            result = await _job_state_store.cleanup_result(job_id)
            if result is None:
                # 跟 run_workflow 同樣的保險路徑
                logger.error(
                    f"[check_pending_tasks] event set but no result for "
                    f"job={job_id}, returning still_running"
                )
                return _build_still_running_response(
                    job_id, session_id, job.task_description
                )

            logger.info(
                f"[check_pending_tasks] Job {job_id} completed during wait"
            )
            return result

        except asyncio.TimeoutError:
            # §3.4.1 race window 修正
            result = await _job_state_store.cleanup_result(job_id)
            if result is not None:
                logger.info(
                    f"[check_pending_tasks] Race window caught: job {job_id} "
                    f"completed in timeout boundary"
                )
                return result

            logger.info(
                f"[check_pending_tasks] Job {job_id} still running after "
                f"{max_wait}s, returning still_running"
            )
            return _build_still_running_response(
                job_id, session_id, job.task_description
            )

    finally:
        await _job_state_store.cleanup_waiter(job_id)



def _build_still_running_response(
    job_id: str,
    session_id: str,
    task_description: str,
) -> dict:
    """組「long polling 結束但任務仍在跑」回應。

    與 _build_running_response (主文件 §5.2 detach 情境) 區分:
    - running: run_workflow 發任務後 80 秒未完成,首次告訴使用者
    - still_running: 已等過 80 秒了還沒完成,二次等待結果

    Helper Agent 可以根據 status 不同提供不同的 UX (還在跑 vs 已等了一段
    時間還在跑)。
    """
    return {
        "status": "still_running",
        "job_id": job_id,
        "session_id": session_id,
        "task_description": task_description,
        "message": "任務仍在執行中,你可以稍後再來問或等 Teams 通知",
    }


def _apply_metadata_to_state(state: ConversationState, metadata: dict):
    """將 Table Storage 中的 workflow metadata 套用到新建的 state。"""
    # 2026.04.28 George: v1.5 還原 session_id / work_dir / output_files / execution_count
    # 修正跨輪 session_id 與 work_dir 漂移的 bug:
    #   原本只還原 4 個語意欄位 (hitl_context / original_user_request /
    #   skills_referenced / recent_full_outputs),沒還原 session_id 與
    #   work_dir,導致 create_new_state() 每輪生的新 uuid 直接被當成
    #   state.session_id,連帶 work_dir 變成 /app/session_<新uuid>/
    #   後果:
    #   1. Table entity 裡的 session_id 欄位每輪被覆蓋成新 uuid,
    #      與 RowKey (caller 傳的 conversation session_id) 不一致
    #   2. 同一 conversation 跨輪產檔散落在多個 /app/session_xxx/ 目錄,
    #      CodingAgent 在 turn N+1 想讀 turn N 產的 CSV/PNG 找不到檔案
    #   3. final_script_path 指向舊 work_dir,新 work_dir 裡沒有實體檔案
    # 修正:從 metadata 還原 session_id 與 work_dir,同時 makedirs 確保目錄存在
    # (ACA replica 重啟後 ephemeral storage 會清空,需要重建空目錄)
    stored_session_id = metadata.get("session_id")
    stored_work_dir = metadata.get("work_dir")
    if stored_session_id and stored_work_dir:
        state.session_id = stored_session_id
        state.work_dir = stored_work_dir
        os.makedirs(state.work_dir, exist_ok=True)
        logger.info(
            f"[State] Restored session_id={stored_session_id}, "
            f"work_dir={stored_work_dir}"
        )

    # 還原 workflow 進度 (execution_count 用於 script_v{n}.py 命名,
    # 不還原會導致新一輪寫成 script_v1.py 蓋掉舊檔)
    state.execution_count = metadata.get("execution_count", 0)

    # 還原既有產出檔案清單,過濾掉已不存在於磁碟的 (replica 重啟後可能消失)
    output_files = metadata.get("output_files", []) or []
    state.output_files = [f for f in output_files if os.path.exists(f)]
    if len(state.output_files) < len(output_files):
        logger.info(
            f"[State] Dropped {len(output_files) - len(state.output_files)} "
            f"output_files no longer on disk (likely replica restart)"
        )

    # 既有的 4 個語意欄位
    state.hitl_context = metadata.get("hitl_context")
    state.original_user_request = metadata.get("original_user_request")
    state.skills_referenced = metadata.get("skills_referenced", [])
    # 2026.04.23 George: v1.4 還原 recent_full_outputs
    # 注意:_inject_session_context 實際上是讀 metadata 而非 state.recent_full_outputs,
    # 這裡把值帶回 state 主要是為了 save_metadata 時的一致性
    # (避免本輪跑完後沒有新 output 時,舊值不小心遺失)
    state.recent_full_outputs = metadata.get("recent_full_outputs", {}) or {}


def _inject_session_context(state: ConversationState, metadata: dict):
    """
    2026.03.13 George: 注入完整的 session context。
    取代原本的 _inject_final_code_context,提供 LLM 足夠的跨輪語境。

    注入內容:
    1. turn_history 摘要(前幾輪做了什麼)
    2. original_user_request(最初的任務目標)
    3. recent_full_outputs(上 N 輪的完整輸出,含原始資料)← 2026.04.23 v1.4 新增
    4. final_code(最近一輪的完整程式碼)

    注入機制:透過 state.add_assistant_message(..., "SessionContext")
    把整塊內容當成一則 assistant message 塞進對話歷史,LLM 會自然地
    把它當 context 參考。Helper Agent 已在讀這個 SessionContext message,
    所以 agent instructions 不需要修改。
    """
    parts = []

    # 1. Turn history(讓 LLM 知道前幾輪做了什麼)
    turn_history = metadata.get("turn_history", [])
    if turn_history:
        parts.append("## 前幾輪工作摘要")
        for turn in turn_history:
            turn_num = turn.get("turn", "?")
            user_req = turn.get("user_request", "")
            summary = turn.get("summary", "")
            parts.append(f"- Turn {turn_num}: 用戶要求「{user_req}」→ {summary}")
            output_files = turn.get("output_files", [])
            if output_files:
                parts.append(f"  產出檔案: {', '.join(output_files)}")

    # 2. Original user request(最初的任務目標,提供整體方向感)
    original_req = metadata.get("original_user_request")
    if original_req and turn_history:
        # 只在有多輪歷史時才注入,避免第一輪重複
        parts.append(f"\n## 最初的任務目標\n{original_req}")

    # 3. Recent full outputs(上 N 輪的完整輸出,含原始資料)
    # 2026.04.23 George: v1.4
    # 這塊放在 turn_history 摘要之後、final_code 之前:
    #   - 摘要是「發生了什麼」的索引
    #   - 完整輸出是「具體內容」的資料體
    #   - final_code 是「上一輪跑了哪段程式碼」
    # 三者互補,順序由「概要→資料→程式碼」遞進,符合 LLM 閱讀習慣
    recent_outputs = metadata.get("recent_full_outputs", {}) or {}
    if recent_outputs:
        parts.append("\n## 上一輪的完整輸出(含原始資料,可供本輪重用)")
        # 按 turn index 數值大小升序排列(turn 1, 2, 3...)
        for turn_idx in sorted(recent_outputs.keys(), key=lambda k: int(k)):
            output = recent_outputs[turn_idx]
            parts.append(f"### Turn {turn_idx} 完整輸出")
            parts.append(output)

    # 4. Final code(最近一輪的完整程式碼,供修改用)
    final_code = metadata.get("final_code", "")
    if final_code and final_code.strip():
        parts.append(f"\n## 最近一輪成功執行的程式碼\n```python\n{final_code}\n```")

    if parts:
        context_msg = "\n".join(parts)
        state.add_assistant_message(context_msg, "SessionContext")
        logger.info(
            f"[State] Injected session context: "
            f"{len(turn_history)} turns, "
            f"original_req={'yes' if original_req else 'no'}, "
            f"recent_full_outputs={len(recent_outputs)}, "
            f"final_code={'yes' if final_code else 'no'}"
        )


def _build_turn_summary(
    turn_number: int,
    user_request: str,
    result: dict,
    state: ConversationState,
) -> dict:
    """
    2026.03.13 George: 從 workflow result 中建構本輪的 turn summary。

    Returns:
        {
            "turn": int,
            "user_request": str (截斷至 500 字),
            "summary": str (截斷至 300 字),
            "output_files": list[str],
            "success": bool,
            "timestamp": str (ISO 8601)
        }
    """
    # 從 result 的 response 中提取摘要
    response_text = result.get("response", "")
    success = result.get("success", False)

    if success:
        # 取 response 的前幾行非空行作為摘要
        lines = response_text.strip().split("\n")
        summary_lines = [line.strip() for line in lines[:5] if line.strip()]
        summary = " ".join(summary_lines)[:300]
    else:
        summary = f"執行失敗: {response_text[:200]}"

    return {
        "turn": turn_number,
        "user_request": user_request[:500],
        "summary": summary,
        "output_files": getattr(state, "output_files", []),
        "success": success,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ============================================================================
# DEBUG & MANAGEMENT(供各 adapter 的 debug 指令使用)
# ============================================================================
# 2026.04.10 George: change debug_list_skill to list_skills, 讓它成為正式功能的一部分,並且在 sync_skills 後自動列出技能清單,方便驗證同步結果。
def list_skills() -> str:
    """列出目前可用的 Skills,包含名稱與用途描述。"""
    import yaml

    if not os.path.exists(SKILLS_DIR):
        return "⚠️ Skills 目錄不存在,可能尚未從 Blob Storage 同步。"

    skill_dirs = sorted(
        e for e in os.listdir(SKILLS_DIR)
        if os.path.isdir(os.path.join(SKILLS_DIR, e))
    )
    if not skill_dirs:
        return "⚠️ Skills 目錄為空(0 個 skill)。"

    catalog = []
    for skill_name in skill_dirs:
        skill_md = os.path.join(SKILLS_DIR, skill_name, "SKILL.md")
        name, description = skill_name, ""
        if os.path.isfile(skill_md):
            try:
                with open(skill_md, "r", encoding="utf-8") as f:
                    content = f.read()
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        meta = yaml.safe_load(parts[1])
                        name = meta.get("name", skill_name)
                        description = meta.get("description", "")
            except Exception:
                description = "(SKILL.md 解析失敗)"
        else:
            description = "(缺少 SKILL.md)"
        catalog.append((name, description))

    lines = [f"## Available Skills ({len(catalog)})\n"]
    for name, desc in catalog:
        lines.append(f"- **{name}**: {desc}")
    return "\n".join(lines)


async def sync_skills() -> str:
    global _workflow, _project_client
    try:
        count = await sync_skills_from_blob()
        # 2026.03.17 George: 重建 workflow,讓 FileAgentSkillsProvider 重新讀取 skills
        _workflow, _project_client = await create_workflow(_credential)

        # 2026.05.12 George × Claude: v1.6 Phase 0 — 重建後重新注入三個介面
        # 否則 sync 後 _workflow.executor / file_store / job_state_store
        # 會是 None,下一次 run_workflow 直接 AttributeError 或行為錯誤。
        # 這裡重用 startup() 已建立好的 globals (executor / file_store /
        # job_state_store),不重新 new 一份,確保 cancel / job state 等
        # 跨 sync 邊界的內部 state (例如 LocalSubprocessExecutor._running_procs
        # 內可能正在跑的 process refs、_pending_waiters 內未完成的 event)
        # 不會被清掉。
        #
        # 2026.05.18 George × Claude: v1.7 Phase 3
        # 注意:JobStore (_job_store) **不** re-inject 到 _workflow,因為
        # 它由 core_handler 直接持有,不掛在 workflow 上 — 設計理由是
        # JobStore 的 caller (run_workflow / cancel_pending_task /
        # check_pending_tasks) 全部在 core_handler 這層,不在 workflow
        # engine 內部。
        _workflow.executor = _executor
        _workflow.file_store = _file_store
        _workflow.job_state_store = _job_state_store
        logger.info(
            "[Phase 3] Re-injected interfaces after workflow rebuild: "
            f"executor={type(_executor).__name__}, "
            f"file_store={type(_file_store).__name__ if _file_store else 'None'}, "
            f"job_state_store={type(_job_state_store).__name__}"
        )

        sync_text = f"Skills synced: {count} skill(s). Workflow rebuilt with new skills."
        sync_text += "\n" + list_skills()
    except Exception as e:
        logger.error(f"[SyncSkills] Failed: {e}", exc_info=True)
        sync_text = f"Skills sync failed: {str(e)}"
    return sync_text

async def clear_session(session_id: str) -> str:
    """清除指定 session 的 metadata。"""
    if _conv_store:
        await _conv_store.delete_metadata(session_id)
    logger.info(f"[Request] Cleared metadata for session={session_id}")
    return "✅ 對話歷史已清除,可以開始新的對話。"


# ============================================================================
# SKILL REVIEW: Human-in-the-Loop 審核
# 2026.03.30 George: v1.3
#
# Logic App Adaptive Card callback 透過 mcp_server.py REST endpoint 呼叫,
# 最終調用這裡的 approve/reject 函式。
#
# 核心設計:pending 存的是 knowledge JSON,approve 時才做 merge。
# 確保對「當下最新」的正式 SKILL.md 做增量合併,不會後蓋前。
# ============================================================================


async def approve_pending_skill(pending_id: str, reviewer: str = "unknown") -> dict:
    """
    2026.03.30 George: v1.3 HITL 新增

    審核通過:從 pending 讀回 knowledge JSON,對當下最新的正式 skill 做 merge 寫入。

    流程:
    1. 從 Blob skills-pending/{pending_id}/metadata.json 讀回 knowledge + 決策 context
    2. 呼叫 write_knowledge_skill(knowledge, existing_skill_dir)
       → 對「當下最新」的正式 SKILL.md 做 merge + dedup
    3. 上傳寫入結果到 Blob 正式路徑
    4. sync_skills(): Blob → 本地 + rebuild workflow
    5. 刪除 Blob skills-pending/{pending_id}/
    6. 記 log
    """
    # ── ① 讀 pending metadata ──
    pending = await blob_read_pending_metadata(pending_id)
    if not pending:
        logger.error(f"[SkillReview] Pending not found: {pending_id}")
        return {"status": "error", "reason": f"Pending ID {pending_id} not found"}

    knowledge = pending.get("knowledge")
    if not knowledge:
        logger.error(f"[SkillReview] Pending metadata missing 'knowledge': {pending_id}")
        return {"status": "error", "reason": "Pending metadata corrupted (missing knowledge)"}

    skill_name = knowledge.get("skill_name", "unknown")

    # ── ② 判斷 merge 目標 ──
    existing_skill_basename = pending.get("existing_skill_dir")
    existing_skill_dir = None
    if existing_skill_basename:
        candidate = os.path.join(SKILLS_DIR, existing_skill_basename)
        if os.path.isdir(candidate):
            existing_skill_dir = candidate
        else:
            logger.warning(
                f"[SkillReview] Merge target '{existing_skill_basename}' "
                f"不存在於本地 skills/,將改為新建"
            )

    # ── ③ 對當下最新的正式 skill 做 merge(核心:避免後蓋前)──
    from skill_gatekeeper import write_knowledge_skill  # 延遲 import,避免循環依賴

    try:
        skill_dir = write_knowledge_skill(knowledge, existing_skill_dir=existing_skill_dir)
    except Exception as e:
        logger.error(f"[SkillReview] write_knowledge_skill failed: {e}")
        return {"status": "error", "reason": f"Write failed: {e}"}

    # ── ④ 上傳到 Blob 正式路徑 ──
    try:
        await blob_upload_skill(skill_name)
    except Exception as e:
        logger.warning(f"[SkillReview] Blob upload failed (local write succeeded): {e}")

    # ── ⑤ sync_skills(): Blob → 本地 + rebuild workflow ──
    await sync_skills()

    # ── ⑥ 清理 pending ──
    try:
        await blob_delete_pending(pending_id)
    except Exception as e:
        logger.warning(f"[SkillReview] Pending cleanup failed: {e}")

    logger.info(
        f"[SkillReview] ✅ APPROVED: pending_id={pending_id}, "
        f"skill={skill_name}, action={pending.get('action_type')}, "
        f"reviewer={reviewer}"
    )

    return {
        "status": "approved",
        "pending_id": pending_id,
        "skill_name": skill_name,
        "action_type": pending.get("action_type"),
        "reviewer": reviewer,
    }


async def reject_pending_skill(pending_id: str, reviewer: str = "unknown") -> dict:
    """
    2026.03.30 George: v1.3 HITL 新增

    審核拒絕:刪除 pending,正式目錄不做任何變動。
    注意:不觸發 sync_skills(),因為正式目錄沒有任何變動。
    """
    # 讀 metadata(只為了 log,讀不到也不影響 reject 操作)
    pending = await blob_read_pending_metadata(pending_id)
    skill_name = "unknown"
    if pending:
        skill_name = pending.get("knowledge", {}).get("skill_name", "unknown")

    # 刪除 pending
    try:
        await blob_delete_pending(pending_id)
    except Exception as e:
        logger.warning(f"[SkillReview] Pending cleanup failed: {e}")

    logger.info(
        f"[SkillReview] ❌ REJECTED: pending_id={pending_id}, "
        f"skill={skill_name}, reviewer={reviewer}"
    )

    return {
        "status": "rejected",
        "pending_id": pending_id,
        "skill_name": skill_name,
        "reviewer": reviewer,
    }