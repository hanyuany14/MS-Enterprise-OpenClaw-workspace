"""
Code Executor - 抽象介面層 (Phase 3)
====================================================================
把「執行 Python code」這件事抽象成 Protocol,讓未來搬遷 Foundry Hosted
Agent 時可以「換實作」而不是「改架構」。

對應設計文件:
- 主文件 v3 §17.2 CodeExecutor 介面
- 主文件 v3 §5.7 Cancel 機制 (cooperative cancellation via SIGTERM + 30s grace)
- 補篇 §5.3 Phase 3 開工建議

階段劃分:
- 階段 1 (現在):LocalSubprocessExecutor — 用 asyncio.create_subprocess_exec
  在 ACA 本機跑 script。
- 階段 2 (POC 通過後):HostedAgentExecutor — 透過 Responses API 委派
  給 Foundry Hosted Agent。本階段不實作。

VERSION: 1.1
2026.05.18 George × Claude: Phase 3 — subprocess 同步→非同步重構 + cancel 實作
- subprocess.run() → asyncio.create_subprocess_exec() + wait_for(communicate())
- LocalSubprocessExecutor.execute() 加 session_id 參數,進入時把 Process
  註冊到 self._running_procs[session_id],try/finally 結束時移除。
- LocalSubprocessExecutor.cancel(session_id) 從 placeholder 改為實作:
  SIGTERM → 30s grace period → SIGKILL,對應主文件 §5.7.2。
- CodeExecutor Protocol 的 execute() 簽章也加 session_id (保持 Protocol
  與實作一致)。
- 既有 v10.x 行為 100% 保留:stdout 截斷 / content_error pattern 11 條 /
  succeeded_count override (cond_a + cond_b) / glob diff 偵測新檔案 /
  PATH 補償 / ExecutionResult 完整欄位。
- 清掉 v9.0 既有 side effect:os.environ["PATH"] = new_path (line 213)。
  George 確認沒有依賴方,清掉避免 process-level 污染。
- Timeout 路徑改為自己 kill + drain (2s 給 stream drain),對齊 stdlib
  TimeoutExpired 的清理行為。

VERSION: 1.0
2026.05.12 George × Claude: Phase 0 初版 — 從 execute_code() 抽出
  subprocess 邏輯封裝為 LocalSubprocessExecutor,維持既有行為。
"""

import asyncio
import glob
import logging
import os
import re
import sys
import traceback
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Protocol

# 2026.05.18 George × Claude: Phase 3
# subprocess module 不再被本檔案使用,改全部走 asyncio.create_subprocess_exec
# + asyncio.subprocess.Process。保留 import 註解供未來考古。
# import subprocess  # ← Phase 0 用,Phase 3 移除

logger = logging.getLogger(__name__)


# ============================================================================
# 共用常數 (從 code_agent_hosted.py 搬過來,避免循環 import)
# ============================================================================

# v9.1 stdout 截斷閾值 — 防止巨量 API 回應塞爆 agent thread context
MAX_STDOUT_LENGTH = 5000

# Phase 0 沿用 code_agent_hosted.py 既有的 OUTPUT_EXTENSIONS 集合。
# 這裡定義同一份 fallback,實際使用以 code_agent_hosted.py 為準
# (透過 dependency injection 或常數共享,Phase 0 先就地宣告)。
OUTPUT_EXTENSIONS = {
    ".py", ".md", ".txt", ".json", ".html", ".csv", ".tsv",
    ".xlsx", ".xls", ".docx", ".pdf", ".pptx",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    ".parquet", ".zip", ".log",
}


# ============================================================================
# Result / Status types
# Phase 0 保留 code_agent_hosted.py v10.1 的 ExecutionStatus / ExecutionResult
# 結構,只是搬位置。CodingAgent loop 仍消費 .agent_message 字串。
# ============================================================================

class ExecutionStatus(str, Enum):
    SUCCESS = "success"
    CONTENT_ERROR = "content_error"   # returncode=0 但 stdout 含 error pattern
    FAILED = "failed"                  # returncode != 0
    TIMEOUT = "timeout"
    EXCEPTION = "exception"            # subprocess 本身拋例外


@dataclass
class ExecutionResult:
    """execute() 的結構化回傳值。

    agent_message 是給 agent loop 消費的純字串(維持 v10.0 字串格式)。
    其餘欄位供 DEBUG_MODE bundle upload 與未來 eval pipeline 使用。
    """
    status: ExecutionStatus
    agent_message: str                           # agent loop 消費這個
    raw_stdout: str                              # 未截斷原始 stdout
    raw_stderr: str                              # 未截斷原始 stderr
    returncode: int
    script_path: str                             # 實際寫入的 script_v{n}.py 路徑
    script_code: str                             # 已 strip markdown fence 的純 code
    execution_count: int                         # state.execution_count 的快照
    truncated: bool = False                      # stdout 是否被截斷過
    error_pattern_hits: Dict[str, int] = field(default_factory=dict)
    override_applied: bool = False
    succeeded_count: int = 0
    # Phase 0 新增:本次執行新產出的檔案路徑列表
    # 由 executor 負責偵測(用 glob diff 邏輯),回傳給 caller 由 OutputFileStore 上傳
    new_output_files: list = field(default_factory=list)


# ============================================================================
# CodeExecutor Protocol
# ============================================================================

class CodeExecutor(Protocol):
    """執行 Python code 的抽象介面。

    Phase 0 階段只有一個實作 LocalSubprocessExecutor。
    階段 2 才會出現 HostedAgentExecutor。
    """

    async def execute(
        self,
        code: str,
        session_id: str,
        work_dir: str,
        execution_count: int,
        env_vars: Dict[str, str],
        timeout: int = 60,
    ) -> ExecutionResult:
        """執行 code,回傳結構化結果。

        Args:
            code: Python 程式碼 (可含 markdown fence,內部會 strip)
            session_id: 應用層 session ID。Phase 3 新增。
                       用於 _running_procs[session_id] 註冊,讓
                       cancel(session_id) 能找到對應的 Process 物件。
            work_dir: 執行目錄 (script 會寫到這裡,新檔案也會偵測這裡)
            execution_count: 本次是 session 的第幾次執行
                            (用於命名 script_v{N}.py)
            env_vars: 注入給 subprocess 的環境變數
                     (含 user_data 裡的 OBO tokens / API keys)
            timeout: subprocess 超時 (秒),預設 60

        Returns:
            ExecutionResult — 包含 agent_message (給 LLM 看的字串)
                              + 完整 raw_stdout/stderr 等 debug 用欄位
                              + new_output_files (本次產生的檔案路徑列表)
        """
        ...

    async def cancel(self, session_id: str) -> bool:
        """嘗試取消當前 session 正在執行的 code。

        對應主文件 §5.7.2 cooperative cancellation:
        - SIGTERM 通知 subprocess 優雅收尾
        - 30 秒 grace period
        - 逾時則 SIGKILL 強制結束

        Phase 3 caller:cancel_pending_task MCP tool。

        Returns:
            True if cancellation was issued; False if nothing to cancel.
        """
        ...


# ============================================================================
# LocalSubprocessExecutor — 階段 1 實作
# ============================================================================

class LocalSubprocessExecutor:
    """本機 subprocess 執行 (從 code_agent_hosted.py execute_code() 搬出來)。

    Phase 0 (零變更):
    - subprocess.run() 的呼叫參數、env 處理、PATH 補償、glob diff
      偵測新檔案、stdout 截斷、content_error pattern 判斷、succeeded
      override 等,全部維持 v10.x 既有行為。

    Phase 3 (本版本):
    - subprocess.run() → asyncio.create_subprocess_exec() + wait_for(communicate())。
      動機:cancel 機制的前置條件 — subprocess.run() 是 blocking call,
      無法從外部中斷;改用 async subprocess 才能讓 cancel(session_id) 在
      另一個 coroutine 中觸發 SIGTERM。
    - execute() 簽章加 session_id,內部把 Process 物件註冊到
      self._running_procs[session_id],try/finally 結束時移除。
    - cancel() 從 placeholder 改為實作 SIGTERM → 30s grace → SIGKILL。
    - 清掉 os.environ["PATH"] = new_path 這行 v9.0 既有 process-level
      side effect (env["PATH"] 仍保留,給 subprocess 用)。

    其他既有行為 100% 保留:
    - script 命名 script_v{N}.py
    - stdout > 5000 chars 截斷 (頭尾各 2000)
    - content_error pattern 11 條
    - succeeded_count override (cond_a: ≥3 且 >error*5,cond_b: ≥1 且 ≤2)
    - glob diff 偵測新檔案 (篩 OUTPUT_EXTENSIONS)
    - ExecutionResult 各狀態的 agent_message 格式
    """

    def __init__(self):
        # Phase 3: _running_procs key=session_id, value=asyncio.subprocess.Process
        # execute() 進入時註冊,try/finally 結束時移除,cancel() 從這裡查 Process。
        # 同一 session 重入 execute() (理論上不會發生,因為 turn loop 序列化)
        # 走 overwrite 策略並 log warning,見 execute() 內註解。
        self._running_procs: Dict[str, asyncio.subprocess.Process] = {}

    async def execute(
        self,
        code: str,
        session_id: str,
        work_dir: str,
        execution_count: int,
        env_vars: Dict[str, str],
        timeout: int = 60,
    ) -> ExecutionResult:
        """執行 code,回傳結構化結果。

        Phase 3 變更摘要 (與 Phase 0 對照):
        1. 簽章新增 session_id (第二個位置參數),用於 _running_procs 註冊。
        2. subprocess.run() → asyncio.create_subprocess_exec() + wait_for。
        3. timeout 路徑改為手動 kill + drain (取代 stdlib 自動處理)。
        4. 進入時把 Process 註冊到 _running_procs,try/finally 結束時移除。
        5. os.environ["PATH"] mutation 已移除 (Step 3 內)。

        其他既有 v10.x 行為 100% 保留 (見 class docstring)。
        """
        # ──────────────────────────────────────────────────────────
        # Step 1: Strip markdown fence (與既有邏輯相同)
        # ──────────────────────────────────────────────────────────
        code = code.strip()
        for prefix in ["```python", "```"]:
            if code.startswith(prefix):
                code = code[len(prefix):]
        if code.endswith("```"):
            code = code[:-3]
        code = code.strip()

        # ──────────────────────────────────────────────────────────
        # Step 2: 寫入 script 檔案
        # 命名規則 script_v{N}.py 維持既有,供 debug bundle 與 audit 用
        # ──────────────────────────────────────────────────────────
        script_path = os.path.join(work_dir, f"script_v{execution_count}.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(code)

        # ──────────────────────────────────────────────────────────
        # Step 3: 組 env (PATH 補償 + user_data 注入)
        # 維持 v9.0 的 Linux-only PATH 處理 (Hosted Agent 只跑 Linux)
        # ──────────────────────────────────────────────────────────
        env = os.environ.copy()
        env.update(env_vars)  # OBO tokens / user-provided credentials

        potential_paths = ["/usr/bin", "/usr/local/bin"]
        valid_extra_paths = [p for p in potential_paths if os.path.exists(p)]
        if valid_extra_paths:
            new_path = os.pathsep.join(valid_extra_paths) + os.pathsep + env.get("PATH", "")
            env["PATH"] = new_path
            # 2026.05.18 George × Claude: Phase 3
            # 移除 v9.0 既有的 `os.environ["PATH"] = new_path` mutation。
            # 該 line 是 process-level side effect,會影響 ACA 內所有 coroutine。
            # George 確認沒有依賴方,此處清掉避免污染。給 subprocess 用的
            # env["PATH"] (上面那行) 已經正確設定,實際 subprocess 行為不變。

        # ──────────────────────────────────────────────────────────
        # Step 4: 記錄 work_dir 執行前的檔案集合 (供 glob diff)
        # ──────────────────────────────────────────────────────────
        files_before = set(glob.glob(os.path.join(work_dir, '*')))

        # ──────────────────────────────────────────────────────────
        # Step 5: 共用 result factory (與既有 _make_result 相同)
        # 確保 ExecutionResult 欄位完整,避免遺漏 script_code / execution_count
        # ──────────────────────────────────────────────────────────
        def _make_result(
            status: ExecutionStatus,
            agent_message: str,
            raw_stdout: str = "",
            raw_stderr: str = "",
            returncode: int = -1,
            truncated: bool = False,
            error_pattern_hits: Optional[Dict[str, int]] = None,
            override_applied: bool = False,
            succeeded_count: int = 0,
            new_output_files: Optional[list] = None,
        ) -> ExecutionResult:
            return ExecutionResult(
                status=status,
                agent_message=agent_message,
                raw_stdout=raw_stdout,
                raw_stderr=raw_stderr,
                returncode=returncode,
                script_path=script_path,
                script_code=code,
                execution_count=execution_count,
                truncated=truncated,
                error_pattern_hits=error_pattern_hits or {},
                override_applied=override_applied,
                succeeded_count=succeeded_count,
                new_output_files=new_output_files or [],
            )

        # ──────────────────────────────────────────────────────────
        # Step 6: 跑 subprocess + 處理各種結果
        # 2026.05.18 George × Claude: Phase 3 — 從 subprocess.run 改成
        # asyncio.create_subprocess_exec + wait_for(communicate())。
        #
        # 重構動機 (對應主文件 §5.7 cancel 機制):
        # - subprocess.run() 是 blocking call,無法從外部中斷。
        # - 改 asyncio 後,cancel(session_id) 可從另一個 coroutine 中
        #   呼叫 proc.terminate() 觸發 SIGTERM 中斷正在跑的 subprocess。
        #
        # 既有 v10.x 行為 100% 保留:
        # - returncode 判讀邏輯不變
        # - stdout 截斷 (5000 char,頭尾各 2000)
        # - content_error pattern 11 條
        # - succeeded_count override (cond_a + cond_b)
        # - glob diff 偵測新檔案
        # - 各狀態 agent_message 格式
        #
        # _running_procs lifecycle 管理:
        # - 進入時註冊 self._running_procs[session_id] = proc
        # - try/finally 結束時無條件 pop,避免 leak
        # - 同 session 重入 (理論上不會發生,turn loop 序列化) 走 overwrite
        #   並 log warning;這代表上游 bug,但 executor 不主動拒絕
        # ──────────────────────────────────────────────────────────

        # 同 session 重入檢查 (理論上不會發生)
        if session_id in self._running_procs:
            logger.warning(
                f"[Execution] session={session_id} already has a running proc "
                f"in _running_procs (overwriting). This indicates upstream "
                f"concurrency control failure — should not happen if §5.5 "
                f"parallel control is honored."
            )

        proc = None  # 給 except 路徑使用,確保 NameError 不會發生
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, script_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=work_dir,
                env=env,
            )
            self._running_procs[session_id] = proc

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                # ──────────────────────────────────────────────────
                # TIMEOUT 路徑:對齊 stdlib subprocess.run(timeout=...) 行為
                # stdlib 在 TimeoutExpired 時會自動 kill + drain,並把
                # partial output 塞進 exception 物件。asyncio 沒有這個自動
                # 行為,需要手動處理。
                # ──────────────────────────────────────────────────
                logger.warning(
                    f"[Execution] v{execution_count} subprocess timeout after "
                    f"{timeout}s, killing and draining streams"
                )

                # Kill (SIGKILL,不像 cancel 路徑給 grace period — 已 timeout
                # 不該再拖)
                try:
                    proc.kill()
                except ProcessLookupError:
                    # 罕見:proc 在 kill() 與檢查之間自己結束了
                    pass

                # Drain stream (2 秒上限,對齊 stdlib 行為)
                partial_stdout = ""
                partial_stderr = ""
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        proc.communicate(),
                        timeout=2.0,
                    )
                    partial_stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
                    partial_stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
                except asyncio.TimeoutError:
                    # Drain 也 timeout — partial output 拿不到了
                    logger.warning(
                        f"[Execution] v{execution_count} stream drain timed "
                        f"out after kill, partial output unavailable"
                    )

                return _make_result(
                    status=ExecutionStatus.TIMEOUT,
                    agent_message=f"❌ EXECUTION FAILED: Timeout after {timeout}s",
                    raw_stdout=partial_stdout,
                    raw_stderr=partial_stderr,
                    returncode=-1,
                )

            # 正常完成 (returncode 0 或非 0,但沒 timeout)
            raw_stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
            raw_stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

            # 偵測本次新產生的 output 檔案 (glob diff)
            files_after = set(glob.glob(os.path.join(work_dir, '*')))
            new_files = [
                f for f in (files_after - files_before)
                if os.path.splitext(f)[1].lower() in OUTPUT_EXTENSIONS
            ]

            if proc.returncode == 0:
                # ──────────────────────────────────────────────────
                # returncode == 0 分支: 處理 stdout 截斷 + content_error 判斷
                # ──────────────────────────────────────────────────
                agent_stdout = raw_stdout
                stdout_truncated = False
                if len(agent_stdout) > MAX_STDOUT_LENGTH:
                    # v9.1 截斷邏輯:保留頭尾各 2000 字元,中間省略
                    head_len = 2000
                    tail_len = 2000
                    omitted = len(raw_stdout) - head_len - tail_len
                    agent_stdout = (
                        raw_stdout[:head_len]
                        + f"\n\n... ⚠️ OUTPUT TRUNCATED: 省略了中間 {omitted} 字元 "
                        + f"(原始長度 {len(raw_stdout)} 字元) ...\n\n"
                        + raw_stdout[-tail_len:]
                    )
                    stdout_truncated = True
                    logger.warning(
                        f"[Execution] stdout truncated: "
                        f"{len(raw_stdout)} → {len(agent_stdout)} chars"
                    )

                # v9.1/v10.2 content_error pattern 偵測
                # 完整保留既有 11 個 pattern (HTTP code、Error:、failed 等)
                content_error_patterns = [
                    r'"error":',
                    r'"status":\s*".*error"',
                    r'\bHTTP[/ ]\d(?:\.\d)?\s+(?:4\d{2}|5\d{2})\b',
                    r'\b(?:status[_ ]?code|statusCode|status|code)\s*[:=]\s*(?:4\d{2}|5\d{2})\b',
                    r'\b(?:4\d{2}|5\d{2})\s+(?:Unauthorized|Forbidden|Not\s+Found|Internal\s+Server\s+Error|Bad\s+Request|Bad\s+Gateway|Service\s+Unavailable|Gateway\s+Timeout|Conflict|Too\s+Many\s+Requests)\b',
                    r'Error:',
                    r'\bfailed\b',
                    r'\bexception\b',
                    r'Missing dependencies?:',
                    r'ModuleNotFoundError',
                    r'ImportError',
                ]

                # 對 raw_stdout 做 match (v10.1 修正:避免截斷邊界破壞 word boundary)
                pattern_hits: Dict[str, int] = {}
                for p in content_error_patterns:
                    matches = re.findall(p, raw_stdout, re.IGNORECASE)
                    if matches:
                        pattern_hits[p] = len(matches)

                has_content_error = bool(pattern_hits)
                error_hit_count = sum(pattern_hits.values())

                # v10.2 succeeded override 邏輯
                # 場景:list models 回 100 筆 succeeded,某個 model 名稱碰巧含 error 字
                # 場景:Fabric Data Agent run completed,但 debug dump 的 GUID 湊出 HTTP code
                success_signal_patterns = [
                    r'"status":\s*"succeeded"',
                    r'"status":\s*"completed"',
                    r'✅\s*Final status:\s*completed',
                    r'"state":\s*"Succeeded"',
                    r'"provisioningState":\s*"Succeeded"',
                ]
                succeeded_count = 0
                override_applied = False
                if has_content_error:
                    for p in success_signal_patterns:
                        succeeded_count += len(re.findall(p, raw_stdout, re.IGNORECASE))

                    # 條件 A: 大量 succeeded 輾壓 error
                    cond_a = succeeded_count >= 3 and succeeded_count > error_hit_count * 5
                    # 條件 B: 明確權威成功訊號 + 低雜訊 (v10.2 新增)
                    cond_b = succeeded_count >= 1 and error_hit_count <= 2

                    if cond_a or cond_b:
                        logger.info(
                            f"[Execution] Overriding content_error "
                            f"(cond_a={cond_a}, cond_b={cond_b}): "
                            f"succeeded_count={succeeded_count}, "
                            f"error_hits={error_hit_count}, "
                            f"treating as normal API response"
                        )
                        has_content_error = False
                        override_applied = True

                if has_content_error:
                    # CONTENT_ERROR: returncode=0 但 stdout 含未被 override 的 error 訊號
                    return _make_result(
                        status=ExecutionStatus.CONTENT_ERROR,
                        agent_message=f"❌ EXECUTION COMPLETED BUT WITH ERRORS:\n\n{agent_stdout}",
                        raw_stdout=raw_stdout,
                        raw_stderr=raw_stderr,
                        returncode=0,
                        truncated=stdout_truncated,
                        error_pattern_hits=pattern_hits,
                        succeeded_count=succeeded_count,
                        new_output_files=new_files,
                    )

                # SUCCESS path
                truncated_note = " (⚠️ OUTPUT_TRUNCATED)" if stdout_truncated else ""
                return _make_result(
                    status=ExecutionStatus.SUCCESS,
                    agent_message=f"✅ EXECUTION SUCCESSFUL{truncated_note}:\n\n{agent_stdout}",
                    raw_stdout=raw_stdout,
                    raw_stderr=raw_stderr,
                    returncode=0,
                    truncated=stdout_truncated,
                    error_pattern_hits=pattern_hits,
                    override_applied=override_applied,
                    succeeded_count=succeeded_count,
                    new_output_files=new_files,
                )
            else:
                # FAILED: returncode != 0
                full_error = raw_stderr or raw_stdout or "Unknown error"
                truncated_error = (
                    f"...\n{full_error[-500:]}" if len(full_error) > 500 else full_error
                )
                logger.error(f"[Execution] v{execution_count} ❌ FAILED")
                return _make_result(
                    status=ExecutionStatus.FAILED,
                    agent_message=f"❌ EXECUTION FAILED (code {proc.returncode}):\n\n{truncated_error}",
                    raw_stdout=raw_stdout,
                    raw_stderr=raw_stderr,
                    returncode=proc.returncode,
                    truncated=False,
                    new_output_files=new_files,
                )

        except Exception as e:
            # 涵蓋 create_subprocess_exec 本身可能拋的例外
            # (FileNotFoundError、PermissionError 等),以及上面正常路徑
            # 內未預期的例外。
            # 注意:asyncio.TimeoutError 已經在內層 try 處理掉,不會傳到這裡。
            logger.error(
                f"[Execution] v{execution_count} exception during subprocess "
                f"execution: {type(e).__name__}: {e}",
                exc_info=True,
            )
            return _make_result(
                status=ExecutionStatus.EXCEPTION,
                agent_message=f"❌ EXECUTION FAILED: {str(e)}",
                raw_stderr=traceback.format_exc(),
                returncode=-1,
            )
        finally:
            # 無條件清掉 _running_procs[session_id],避免 leak。
            # 即使 cancel() 已經在另一個 coroutine 中先 pop 走,這裡 pop
            # default=None 也安全。
            self._running_procs.pop(session_id, None)

    async def cancel(self, session_id: str) -> bool:
        """Cooperative cancellation:SIGTERM → 30s grace → SIGKILL。

        對應主文件 §5.7.2 設計:
        - 先 SIGTERM 讓 subprocess 有機會優雅收尾 (e.g. flush 檔案、釋放
          資源)
        - 30 秒 grace period
        - 逾時 SIGKILL 強制結束,Job Store 端附 `cancel_warning:
          subprocess_force_killed` (主文件 §5.7.4),由 caller 處理

        Phase 3 caller:cancel_pending_task MCP tool。雙路徑設計
        (主文件 §13.4 + 本檔案 Q4):
        - 路徑 1:execute() 正在跑,本 method 中斷它,觸發 execute() 的
          asyncio.TimeoutError 或 returncode != 0 路徑,讓 turn loop 收到
          結果並收尾
        - 路徑 2:turn 間沒 proc 在跑,本 method 回 False (no-op),cancel
          flag 已由 caller 寫入 Job Store,turn loop 下個 iteration 的
          checkpoint 會偵測到並退出

        ⚠️ 並發語意:
        本 method 與 execute() 是不同的 coroutine。proc.terminate() / kill()
        是 atomic 的 syscall,沒有 race 問題;_running_procs[session_id] 的
        get/pop 也是 atomic 的 dict op。execute() 的 finally 區塊也會 pop
        _running_procs[session_id],兩邊都 pop default=None,沒有 race。

        Args:
            session_id: 要取消的 session ID。

        Returns:
            True 表示有發送中斷訊號(SIGTERM 或 SIGKILL),
            False 表示沒有找到對應的 running proc (no-op)。
        """
        proc = self._running_procs.get(session_id)
        if proc is None:
            logger.info(
                f"[Cancel] session={session_id}: no running proc found "
                f"(already finished or never started)"
            )
            return False

        # 若 proc 已經自己結束 (returncode 不為 None),也視為 no-op
        if proc.returncode is not None:
            logger.info(
                f"[Cancel] session={session_id}: proc already exited with "
                f"returncode={proc.returncode}"
            )
            return False

        # 1. SIGTERM (優雅收尾請求)
        try:
            proc.terminate()
            logger.info(
                f"[Cancel] session={session_id}: SIGTERM sent (pid={proc.pid})"
            )
        except ProcessLookupError:
            # Proc 在 terminate() 與檢查之間自己結束了
            logger.info(
                f"[Cancel] session={session_id}: proc already gone before "
                f"SIGTERM (ProcessLookupError)"
            )
            return False

        # 2. 30 秒 grace period
        try:
            await asyncio.wait_for(proc.wait(), timeout=30.0)
            logger.info(
                f"[Cancel] session={session_id}: proc exited gracefully "
                f"after SIGTERM (returncode={proc.returncode})"
            )
            return True
        except asyncio.TimeoutError:
            # 3. SIGKILL (強制結束,subprocess 沒有優雅收尾的權利了)
            logger.warning(
                f"[Cancel] session={session_id}: SIGTERM grace expired after "
                f"30s, sending SIGKILL (pid={proc.pid})"
            )
            try:
                proc.kill()
            except ProcessLookupError:
                # Proc 在 SIGTERM 與 SIGKILL 之間結束了
                logger.info(
                    f"[Cancel] session={session_id}: proc exited between "
                    f"SIGTERM and SIGKILL"
                )
                return True

            # 等 SIGKILL 生效 (一般 < 1s)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
                logger.info(
                    f"[Cancel] session={session_id}: proc force-killed "
                    f"(returncode={proc.returncode})"
                )
            except asyncio.TimeoutError:
                # SIGKILL 都不行?系統有問題,記 error
                logger.error(
                    f"[Cancel] session={session_id}: proc did not exit even "
                    f"after SIGKILL — system may be unresponsive"
                )

            return True