"""
Job State Store - 抽象介面層 (Phase 3 完整版)
====================================================================
為 Adaptive Timeout Escalation 提供 in-process 的 waiter 機制。

對應設計文件:
- 主文件 v3 §17.2 JobStateStore 介面
- 主文件 v3 §13.2 完整 Waiter 機制骨架
- 主文件 v3 §3.4.1 timeout 邊界 race window 處理
- 主文件 v3 §3.5 grace period + 重新檢查 waiter
- 補篇 §7 對 Phase 0 _pending_waiters / _pending_results dict 封裝的提醒

⚠️ 與 JobStore 的分層 (補篇 §7 已強調):
- 本檔案 (JobStateStore):**in-process 層**,per-replica dict + asyncio.Event,
  bg task 生命週期內存在。用於同步 waiter 與 Teams 通知的快速分流。
- job_store.py (JobStore):**持久層**,Table Storage,跨 replica 可見。

兩層獨立。Phase 3 的 _run_coding_agent 完成路徑會同時更新兩層:
  - JobStateStore.set_result()  → 觸發 in-memory waiter
  - JobStore.update(status=...) → 寫終態到 Table (供 audit 和跨 replica)

階段劃分:
- 階段 1 (本階段):InMemoryJobStateStore — 本檔案實作。
  使用 asyncio.Event + per-replica dict 處理同步等待 vs Teams 通知的分流。
  ACA max_replicas = 1 是這個設計的前提 (主文件 §3.4)。
- 階段 2 (POC 通過後):HostedAgentJobStateStore — 改用 Foundry Responses
  Background Mode 的 response_id polling 機制,取代 in-memory state。
  本階段內部 ~150 行 race / state-tracking 邏輯將被刪除 (主文件 §17.6
  標記為「白工」)。

關鍵設計決策:
- 不用 lock 保護 _waiters / _results dict:
  在 ACA max_replicas=1 前提下,單一 event loop 處理所有 task。
  Python 的 dict 操作 (get / pop / __setitem__) 是 atomic 的,
  且 await 點之間沒有實際的 race window。
  → 詳見 set_result 的長註解。

- asyncio.Event 不會自動清除:
  caller 必須在 finally 區塊呼叫 cleanup_waiter() 來避免 dict 洩漏。

- result 的所有權:
  set_result 寫入 _results 後,所有權交給 waiter。
  waiter 透過 cleanup_result() 取出後 _results 內就沒了。
  若 caller 因為某種原因沒來拿 (例如已 timeout 且 race window 修正路徑
  已經拿走),_results 內會有殘留,由 cleanup_waiter() 順手清掉
  (本檔案實作會在 cleanup_waiter 內也清 _results,保險起見)。

VERSION: 1.1
2026.05.12 George × Claude: Phase 0 初版 — 純空殼。
2026.05.17 George × Claude: Phase 3 — 填入完整邏輯,通過 Phase 1 race test
  baseline (phase1_race_test.py 100 次邊界壓測無遺失無重複)。
"""

import asyncio
import logging
from typing import Any, Dict, Optional, Protocol

logger = logging.getLogger(__name__)


# ============================================================================
# JobStateStore Protocol
# ============================================================================

class JobStateStore(Protocol):
    """非同步任務狀態管理的抽象介面。

    Phase 3 才會真正用到。本 Protocol 定義 §17.2 的所有方法 signature。
    """

    async def register_waiter(self, job_id: str) -> asyncio.Event:
        """登記一個同步 waiter,回傳 event 供 caller await。

        Phase 3 用途:
        - run_coding_workflow timeout 80 秒內等這個 event
        - check_pending_tasks long polling 也等這個 event
        """
        ...

    async def set_result(self, job_id: str, result: Any) -> None:
        """設定 job 結果。

        Phase 3 用途:bg task 完成時呼叫,觸發 waiter 的 event,
        並把結果存到 _pending_results 供 waiter 取用。
        """
        ...

    async def get_result(self, job_id: str) -> Optional[Any]:
        """取得 job 結果 (不移除)。

        Phase 3 用途:§3.4.1 timeout 邊界 race window 修正 —
        wait_for 拋 TimeoutError 後,先檢查結果是否在 race window
        內已被設定。
        """
        ...

    async def has_waiter(self, job_id: str) -> bool:
        """是否還有 waiter 在等。

        Phase 3 用途:§3.5 _deliver_result_or_notify 決定走
        waiter 還是 Teams 通知。
        """
        ...

    async def cleanup_waiter(self, job_id: str) -> None:
        """清除 waiter entry。

        Phase 3 用途:finally 區塊清理。
        """
        ...

    async def cleanup_result(self, job_id: str) -> Optional[Any]:
        """取出並清除結果 (atomic pop)。

        Phase 3 用途:waiter 成功收到 event 後從這裡拿結果並清掉。
        """
        ...


# ============================================================================
# InMemoryJobStateStore — Phase 3 完整實作
# ============================================================================

class InMemoryJobStateStore:
    """In-memory + asyncio.Event 實作的 JobStateStore。

    部署前提:
    - ACA Container App max_replicas = 1。
      因為 _pending_waiters 是 per-replica dict,bg task 跟同步 waiter
      必須在同一個 process 內才能透過 asyncio.Event 通訊。
    - 跨 replica 的 fallback 走 Job Store 輪詢,屬於主文件 §16 未來工作。

    使用 pattern (對應主文件 §13.2):
        # ── run_coding_workflow / check_pending_tasks ──
        event = await job_state.register_waiter(job_id)
        try:
            try:
                await asyncio.wait_for(event.wait(), timeout=80)
                result = await job_state.cleanup_result(job_id)
                return _build_completed_response(result)
            except asyncio.TimeoutError:
                # §3.4.1 race window 修正
                result = await job_state.cleanup_result(job_id)
                if result is not None:
                    return _build_completed_response(result)
                return _build_running_response(job_id)
        finally:
            await job_state.cleanup_waiter(job_id)

        # ── _run_coding_agent (bg task) ──
        # 完成後分流:
        if await job_state.has_waiter(job_id):
            await job_state.set_result(job_id, result)   # 走 in-memory 路徑
        else:
            await asyncio.sleep(3)                        # grace period
            if await job_state.has_waiter(job_id):        # 重新檢查
                await job_state.set_result(job_id, result)
            else:
                await teams_notifier.send_completion_card(...)
    """

    def __init__(self):
        # waiter event 字典:job_id → asyncio.Event
        # register_waiter 建立,cleanup_waiter 清除。
        # 同一 job_id 重複 register 會回傳同一個 Event,讓「同步 waiter」
        # 與「bg task 完成」兩端能共用同一個 event 物件。
        self._waiters: Dict[str, asyncio.Event] = {}

        # result 字典:job_id → result payload (任意型別)
        # set_result 寫入,cleanup_result 取出 (atomic pop)。
        # cleanup_waiter 也會順手清,避免殘留 (見下方註解)。
        self._results: Dict[str, Any] = {}

        logger.info(
            "[JobStateStore] InMemoryJobStateStore initialized "
            "(Phase 3 — full implementation)"
        )

    async def register_waiter(self, job_id: str) -> asyncio.Event:
        """登記一個 waiter,回傳 asyncio.Event 供 caller await。

        若 job_id 已經有 event (例如同 session 內 run_coding_workflow 起
        bg task 後 timeout,使用者轉而呼叫 check_pending_tasks 來等),
        回傳同一個 Event 物件,讓兩個 waiter 共用 (二者 wait_for 同個
        event,bg task 完成時 set 一次,兩邊都收到)。

        實務上 §5.5 並行控制保證同一 session 同時只有一個 running job,
        但 same job_id 重複 register 是合法情境 (例如 race window 內的
        重複註冊),程式碼必須允許。

        Phase 3 caller:
        - run_coding_workflow 起 bg task 後 (主文件 §13.2 步驟 3)
        - check_pending_tasks 找到 running job 後 (主文件 §13.3)
        """
        event = self._waiters.get(job_id)
        if event is None:
            event = asyncio.Event()
            self._waiters[job_id] = event
            logger.debug(f"[JobStateStore] register_waiter: new event for {job_id}")
        else:
            # 同 job_id 已有 event — 多個 waiter 共用,沒問題
            logger.debug(
                f"[JobStateStore] register_waiter: reusing existing event "
                f"for {job_id} (multiple waiters)"
            )
        return event

    async def set_result(self, job_id: str, result: Any) -> None:
        """寫入結果並觸發 waiter event。

        ⚠️ 並發語意說明 (這段值得多花一點注意力):
        本 method 沒有 lock,但在 ACA max_replicas=1 + 單一 event loop 前提下
        是安全的。理由:
        1. self._results[job_id] = result 是單一 bytecode op,atomic
        2. event.set() 是 asyncio 內建保證 thread-safe (對 single-thread
           event loop 而言天然安全)
        3. 在 set 完 _results 與 event.set() 之間,沒有任何 await 點。
           其他 coroutine 不會被 scheduled 進來插隊。

        Phase 3 caller:_run_coding_agent 的 _deliver_result_or_notify
        分支 (主文件 §3.5 / §13.2)。

        Args:
            job_id: 對應的 job ID。
            result: 任意 payload,通常是 core_handler.run_workflow() 的
                result dict (含 success / response / uploads / 等)。

        Note: 若 register_waiter 還沒被呼叫 (例如極端 race),這裡會建立
        一個新 event 並 set 它。下個 register_waiter(job_id) 會拿到這個
        已 set 過的 event,await wait() 立刻 return,結果可從 _results 取。
        這設計讓 set_result 不依賴 register_waiter 的呼叫順序。
        """
        # 先寫 result,再 set event。
        # 順序很重要:waiter 在 event.set() 後會立刻被喚醒去 cleanup_result,
        # 若 event 先 set,result 還沒寫,waiter 會拿到 None。
        self._results[job_id] = result

        event = self._waiters.get(job_id)
        if event is None:
            # 沒人在等 (waiter 尚未註冊或已被清除),建一個已 set 的 event。
            # 未來如果有人來 register_waiter(job_id),會拿到這個 already-set
            # event,wait() 立刻 return。
            event = asyncio.Event()
            self._waiters[job_id] = event
            logger.debug(
                f"[JobStateStore] set_result: no waiter exists for {job_id}, "
                f"created pre-set event"
            )

        event.set()
        logger.debug(f"[JobStateStore] set_result: result set for {job_id}")

    async def get_result(self, job_id: str) -> Optional[Any]:
        """取得 result 但不移除。

        Phase 3 用途:主要供 §3.4.1 race window 修正路徑檢查
        「timeout 觸發瞬間是否剛好有 result 被寫入」。
        實務上 caller 在拿到 result 之後幾乎都會接著呼叫 cleanup_result()
        把它清掉,所以這個 method 在 hot path 上用得不多。

        Returns:
            result 物件,沒有則 None。
        """
        return self._results.get(job_id)

    async def has_waiter(self, job_id: str) -> bool:
        """檢查是否有 waiter 在等。

        ⚠️ 注意:這個方法只看 _waiters dict,**不看 _results**。
        - 一個 waiter 存在但 event 尚未 set:正常等待中,回 True
        - 一個 waiter 存在且 event 已 set (但 caller 還沒來 cleanup):
          也回 True (caller 即將取 result,不該再去推 Teams)
        - waiter 不存在 (尚未 register 或已 cleanup):回 False

        Phase 3 caller:_run_coding_agent 的 _deliver_result_or_notify
        決定走 waiter 還是 Teams 通知 (主文件 §3.5)。
        """
        return job_id in self._waiters

    async def cleanup_waiter(self, job_id: str) -> None:
        """清除 waiter entry。

        Phase 3 caller:run_coding_workflow / check_pending_tasks 的
        finally 區塊。確保不論是 timeout 還是正常完成,waiter dict 不
        累積殘留。

        ⚠️ 順手清 _results:
        若 caller 走 race window 修正路徑拿走 result 後直接 return,
        finally 區塊會呼叫到本 method。此時 _results[job_id] 可能還在
        (race window 修正路徑後 caller 用的是 cleanup_result 取走,
        但保險起見也檢查一次)。
        """
        self._waiters.pop(job_id, None)
        # 順手清 result。正常路徑 caller 已用 cleanup_result 取走,這裡
        # pop 不到也沒事 (default=None)。
        leaked_result = self._results.pop(job_id, None)
        if leaked_result is not None:
            # 真有殘留 = caller 流程有 bug,寫個 warning 方便排查
            logger.warning(
                f"[JobStateStore] cleanup_waiter found leaked result for "
                f"{job_id} (caller did not call cleanup_result before "
                f"cleanup_waiter)"
            )

    async def cleanup_result(self, job_id: str) -> Optional[Any]:
        """取出並清除結果 (atomic pop)。

        Phase 3 caller:
        - waiter 收到 event.set() 後,從這裡拿結果 (主文件 §13.2 完成路徑)
        - timeout 邊界 race window 修正路徑 (§3.4.1):wait_for 拋
          TimeoutError 後檢查是否剛好有 result。

        Returns:
            result 物件,沒有則 None。
        """
        return self._results.pop(job_id, None)