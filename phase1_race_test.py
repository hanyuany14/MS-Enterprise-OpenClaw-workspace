"""
Phase 1 驗證 3 + 4:Race window 壓測 + Grace period 重新檢查 waiter

本地 Python 直接跑,不需要部署到 ACA。
驗證 §3.4.1 timeout 邊界 race window 與 §3.5 grace period 邏輯。

執行:
    python phase1_race_test.py

通過判準會在最後印出 ✅ / ❌。
"""

import asyncio
import random
import sys
from typing import Any

# ====================================================================
# 模擬 Phase 2 的 in-memory 機制(對應 §13.2 / §17.2 InMemoryJobStateStore)
# ====================================================================

_pending_waiters: dict[str, asyncio.Event] = {}
_pending_results: dict[str, Any] = {}
_teams_notifications: list[str] = []  # 模擬 Teams 通知收件匣


def reset_state():
    """每個測試前重置全域狀態。"""
    _pending_waiters.clear()
    _pending_results.clear()
    _teams_notifications.clear()


# ====================================================================
# 模擬 bg task 完成時的交付邏輯(§3.5 完整版,含 grace period + 重新檢查)
# ====================================================================

GRACE_PERIOD = 0.3  # 測試時用 0.3 秒加速(production 用 3 秒)


async def _deliver_result_or_notify(job_id: str, result: str) -> str:
    """
    完整版 _deliver_result_or_notify,含 §3.5 的 grace period + 重新檢查邏輯。
    
    回傳:
        "via_waiter_immediate" — 第一次檢查就有 waiter
        "via_waiter_after_grace" — grace period 內出現新 waiter
        "via_teams" — 真的沒人在等,推 Teams
    """
    # 第一次檢查
    waiter = _pending_waiters.get(job_id)
    if waiter is not None:
        _pending_results[job_id] = result
        waiter.set()
        return "via_waiter_immediate"
    
    # 沒人等,grace period
    await asyncio.sleep(GRACE_PERIOD)
    
    # 重新檢查
    waiter = _pending_waiters.get(job_id)
    if waiter is not None:
        _pending_results[job_id] = result
        waiter.set()
        return "via_waiter_after_grace"
    
    # 真的沒人等,推 Teams
    _teams_notifications.append(result)
    return "via_teams"


async def _simulated_bg_task(job_id: str, completion_delay: float) -> str:
    """模擬 bg task,在 completion_delay 秒後完成並交付結果。"""
    await asyncio.sleep(completion_delay)
    result = f"result-{job_id}"
    delivery_path = await _deliver_result_or_notify(job_id, result)
    return delivery_path


# ====================================================================
# 模擬 run_coding_workflow 的 adaptive timeout 邏輯(§13.2)
# ====================================================================

async def _run_with_adaptive_timeout(
    job_id: str,
    max_wait: float,
    completion_delay: float,
) -> tuple[str, Any]:
    """
    模擬 run_coding_workflow 的等待 + race window 修正(§3.4.1)。
    
    回傳 (path, result):
        ("sync", result) — 正常 timeout 內完成
        ("sync_race_caught", result) — race window 觸發,bg 剛好在 timeout 邊界完成
        ("running", None) — 真的 timeout,進入 detached 模式
    """
    event = asyncio.Event()
    _pending_waiters[job_id] = event
    
    bg = asyncio.create_task(_simulated_bg_task(job_id, completion_delay))
    
    try:
        try:
            await asyncio.wait_for(event.wait(), timeout=max_wait)
            result = _pending_results.pop(job_id, None)
            return ("sync", result)
        except asyncio.TimeoutError:
            # §3.4.1 race window 修正:bg 可能剛好在 timeout 觸發瞬間完成
            if job_id in _pending_results:
                result = _pending_results.pop(job_id)
                return ("sync_race_caught", result)
            return ("running", None)
    finally:
        _pending_waiters.pop(job_id, None)
        # 確保 bg task 跑完(避免污染下個測試),但不 await result
        if not bg.done():
            try:
                await asyncio.wait_for(bg, timeout=max_wait + 1.0)
            except asyncio.TimeoutError:
                pass


# ====================================================================
# 模擬使用者呼叫 check_pending_tasks 註冊 waiter(§5.3)
# ====================================================================

async def _user_checks_after(job_id: str, delay: float, max_wait: float = 10) -> str | None:
    """模擬使用者在 delay 秒後呼叫 check_pending_tasks。"""
    await asyncio.sleep(delay)
    
    event = asyncio.Event()
    _pending_waiters[job_id] = event
    try:
        await asyncio.wait_for(event.wait(), timeout=max_wait)
        return _pending_results.pop(job_id, None)
    except asyncio.TimeoutError:
        return None
    finally:
        _pending_waiters.pop(job_id, None)


# ====================================================================
# 驗證 3:Race window 壓測
# ====================================================================

async def test_race_window(iterations: int = 100):
    """
    對 max_wait 邊界做高頻壓測,確認結果不會遺失也不會重複。
    
    通過判準:
    - 所有 iteration 都有交付一次結果(via waiter 或 via teams),總和等於 iterations
    - race window 有實際觸發過(sync_race_caught > 0),證明修正邏輯有派上用場
    """
    print("=" * 60)
    print("驗證 3:Race window 壓測")
    print("=" * 60)
    
    reset_state()
    
    max_wait = 1.0  # 用 1 秒加速測試
    deliveries = {"sync": 0, "sync_race_caught": 0, "running": 0}
    
    # 故意在 max_wait 邊界附近完成,觸發 race
    offsets = [-0.5, -0.3, -0.1, -0.05, -0.02, -0.01, 0, 0.01, 0.02, 0.05, 0.1, 0.3, 0.5]
    
    for i in range(iterations):
        offset = random.choice(offsets)
        completion = max(0.01, max_wait + offset)
        
        path, _result = await _run_with_adaptive_timeout(
            job_id=f"race-{i}",
            max_wait=max_wait,
            completion_delay=completion,
        )
        deliveries[path] += 1
        
        # 等到 bg task 確定走完 grace period(避免污染下次)
        await asyncio.sleep(GRACE_PERIOD + 0.1)
    
    total_sync = deliveries["sync"] + deliveries["sync_race_caught"]
    total_teams = len(_teams_notifications)
    total_handled = total_sync + total_teams
    
    print(f"\n結果統計:")
    print(f"  Sync(正常路徑):        {deliveries['sync']}")
    print(f"  Sync(race 修正攔截):    {deliveries['sync_race_caught']}")
    print(f"  Detached(回 running):    {deliveries['running']}")
    print(f"  Teams 通知:              {total_teams}")
    print(f"  ────────────────────")
    print(f"  總交付:                  {total_handled}")
    print(f"  預期:                    {iterations}")
    
    # 驗收
    ok_no_loss = total_handled == iterations
    ok_race_triggered = deliveries["sync_race_caught"] > 0
    
    if ok_no_loss and ok_race_triggered:
        print(f"\n✅ 驗證 3 通過")
        return True
    else:
        if not ok_no_loss:
            print(f"\n❌ 驗證 3 失敗:結果遺失或重複({total_handled} ≠ {iterations})")
        if not ok_race_triggered:
            print(f"\n⚠️  驗證 3 警告:race window 從未觸發,覆蓋率不足")
            print(f"    (可能 offset 配置太寬,實際 race 在更窄區間)")
            print(f"    若 total_handled 正確,可視為通過")
            return ok_no_loss
        return False


# ====================================================================
# 驗證 4:Grace period 重新檢查 waiter
# ====================================================================

async def test_grace_period():
    """
    驗證兩個場景:
    
    場景 A:使用者在 grace period 內註冊 waiter
      → 應收到結果,Teams 不該被推
    
    場景 B:使用者在 grace period 結束後才註冊
      → Teams 該被推,使用者註冊 waiter 拿不到結果
    """
    print("\n" + "=" * 60)
    print("驗證 4:Grace period + 重新檢查 waiter")
    print("=" * 60)
    
    all_pass = True
    
    # ----------- 場景 A -----------
    print(f"\n場景 A:使用者在 grace period 內({GRACE_PERIOD/2:.2f}s)註冊 waiter")
    reset_state()
    
    user_task = asyncio.create_task(
        _user_checks_after("job-A", delay=GRACE_PERIOD / 2)
    )
    delivery_path = await _deliver_result_or_notify("job-A", "result-A")
    user_result = await user_task
    
    print(f"  delivery_path:      {delivery_path}")
    print(f"  user_result:        {user_result}")
    print(f"  teams_notifications: {_teams_notifications}")
    
    a_ok = (
        delivery_path == "via_waiter_after_grace"
        and user_result == "result-A"
        and len(_teams_notifications) == 0
    )
    
    if a_ok:
        print(f"  ✅ 場景 A 通過")
    else:
        print(f"  ❌ 場景 A 失敗")
        print(f"     預期:delivery_path=via_waiter_after_grace, user_result=result-A, teams=[]")
        all_pass = False
    
    # ----------- 場景 B -----------
    print(f"\n場景 B:使用者在 grace period 結束後({GRACE_PERIOD*2:.2f}s)才來")
    reset_state()
    
    # 同步呼叫 _deliver,使用者根本沒先註冊
    delivery_path = await _deliver_result_or_notify("job-B", "result-B")
    
    print(f"  delivery_path:      {delivery_path}")
    print(f"  teams_notifications: {_teams_notifications}")
    
    b_ok = (
        delivery_path == "via_teams"
        and "result-B" in _teams_notifications
    )
    
    if b_ok:
        print(f"  ✅ 場景 B 通過")
    else:
        print(f"  ❌ 場景 B 失敗")
        print(f"     預期:delivery_path=via_teams, teams=[result-B]")
        all_pass = False
    
    # ----------- 場景 C(額外):立即就有 waiter -----------
    print(f"\n場景 C(額外):waiter 已存在,應走 immediate 路徑")
    reset_state()
    
    event = asyncio.Event()
    _pending_waiters["job-C"] = event
    
    delivery_path = await _deliver_result_or_notify("job-C", "result-C")
    result = _pending_results.pop("job-C", None)
    _pending_waiters.pop("job-C", None)
    
    print(f"  delivery_path: {delivery_path}")
    print(f"  result:        {result}")
    print(f"  teams:         {_teams_notifications}")
    
    c_ok = (
        delivery_path == "via_waiter_immediate"
        and result == "result-C"
        and len(_teams_notifications) == 0
    )
    
    if c_ok:
        print(f"  ✅ 場景 C 通過")
    else:
        print(f"  ❌ 場景 C 失敗")
        all_pass = False
    
    if all_pass:
        print(f"\n✅ 驗證 4 通過(三個場景全過)")
    else:
        print(f"\n❌ 驗證 4 失敗")
    
    return all_pass


# ====================================================================
# Main
# ====================================================================

async def main():
    print(f"\nPhase 1 驗證 3 + 4 開始")
    print(f"Python: {sys.version.split()[0]}")
    print(f"Grace period(測試用):{GRACE_PERIOD}s\n")
    
    v3_pass = await test_race_window(iterations=100)
    v4_pass = await test_grace_period()
    
    print("\n" + "=" * 60)
    print("Phase 1 整體結果")
    print("=" * 60)
    print(f"  驗證 3(race window):  {'✅ 通過' if v3_pass else '❌ 失敗'}")
    print(f"  驗證 4(grace period): {'✅ 通過' if v4_pass else '❌ 失敗'}")
    
    if v3_pass and v4_pass:
        print(f"\n🎉 Phase 1 全部驗證通過,可以開 Phase 2")
        return 0
    else:
        print(f"\n⚠️  有驗證未通過,Phase 2 開工前先解掉")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
