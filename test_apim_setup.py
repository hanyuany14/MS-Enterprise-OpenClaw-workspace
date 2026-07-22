"""
Quick APIM setup smoke test for mc-openclaw-mcpuse.

Usage:
  python test_apim_setup.py

Optional env vars:
  APIM_BASE_URL=https://kurt-apim.azure-api.net/mc-openclaw-mcpuse
  APIM_MCP_BASE_URL=https://kurt-apim.azure-api.net/mc-openclaw-mcpuse-mcp
  APIM_BEARER_TOKEN=<token>
  TEST_MCP_ONLY=true
  TEST_RUN_WORKFLOW=true
"""

from __future__ import annotations

import json
import os
import sys
import uuid

import requests


BASE_URL = os.getenv(
    "APIM_BASE_URL",
    "https://kurt-apim.azure-api.net/mc-openclaw-mcpuse",
).rstrip("/")
MCP_BASE_URL = os.getenv(
    "APIM_MCP_BASE_URL",
    "https://kurt-apim.azure-api.net/mc-openclaw-mcpuse-mcp",
).rstrip("/")
BEARER_TOKEN = os.getenv("APIM_BEARER_TOKEN", "")
TEST_MCP_ONLY = os.getenv("TEST_MCP_ONLY", "false").lower() == "true"
TEST_RUN_WORKFLOW = os.getenv("TEST_RUN_WORKFLOW", "false").lower() == "true"


def headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if BEARER_TOKEN:
        h["Authorization"] = f"Bearer {BEARER_TOKEN}"
    return h


def mcp_headers() -> dict[str, str]:
    h = headers()
    h["Accept"] = "application/json, text/event-stream"
    return h


def print_result(name: str, ok: bool, status: int | None, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    status_text = f" HTTP {status}" if status is not None else ""
    print(f"[{mark}] {name}{status_text} {detail}".rstrip())


def request_json(method: str, path: str, body: dict | None = None, timeout: int = 30):
    url = f"{BASE_URL}{path}"
    return requests.request(method, url, headers=headers(), json=body, timeout=timeout)


def request_mcp_json(body: dict, timeout: int = 30):
    url = f"{MCP_BASE_URL}/mcp"
    return requests.post(url, headers=mcp_headers(), json=body, timeout=timeout)


def test_health() -> bool:
    try:
        r = request_json("GET", "/health")
        data = safe_json(r)
        ok = r.status_code == 200 and data.get("status") in ("healthy", "starting")
        print_result("GET /health", ok, r.status_code, data.get("status", ""))
        return ok
    except Exception as e:
        print_result("GET /health", False, None, str(e))
        return False


def test_skills() -> bool:
    try:
        r = request_json("GET", "/skills")
        data = safe_json(r)
        ok = r.status_code == 200 and data.get("status") == "completed"
        print_result("GET /skills", ok, r.status_code)
        return ok
    except Exception as e:
        print_result("GET /skills", False, None, str(e))
        return False


def test_clear() -> bool:
    try:
        r = request_json("POST", "/clear", {"session_id": f"smoke-{uuid.uuid4()}"})
        data = safe_json(r)
        ok = r.status_code == 200 and data.get("status") == "completed"
        print_result("POST /clear", ok, r.status_code)
        return ok
    except Exception as e:
        print_result("POST /clear", False, None, str(e))
        return False


def test_sync() -> bool:
    try:
        r = request_json("POST", "/sync", {})
        data = safe_json(r)
        ok = r.status_code == 200 and data.get("status") == "completed"
        print_result("POST /sync", ok, r.status_code)
        return ok
    except Exception as e:
        print_result("POST /sync", False, None, str(e))
        return False


def test_run_workflow() -> bool:
    if not TEST_RUN_WORKFLOW:
        print_result("POST /run", True, None, "SKIPPED; set TEST_RUN_WORKFLOW=true to run")
        return True
    try:
        r = request_json(
            "POST",
            "/run",
            {
                "request": "請回覆一個最簡單的 Python hello world 程式，不要執行外部服務。",
                "session_id": f"smoke-{uuid.uuid4()}",
                "credentials": {},
            },
            timeout=120,
        )
        data = safe_json(r)
        ok = r.status_code == 200 and data.get("status") in (
            "completed",
            "needs_input",
            "running",
        )
        print_result("POST /run", ok, r.status_code, data.get("status", ""))
        return ok
    except Exception as e:
        print_result("POST /run", False, None, str(e))
        return False


def test_callback_validation(path: str) -> bool:
    try:
        r = request_json("POST", path, {})
        data = safe_json(r)
        ok = r.status_code == 400 and data.get("status") == "error"
        print_result(f"POST {path} validation", ok, r.status_code)
        return ok
    except Exception as e:
        print_result(f"POST {path} validation", False, None, str(e))
        return False


def test_mcp_tools_list() -> bool:
    try:
        init_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {
                    "name": "apim-smoke-test",
                    "version": "1.0.0",
                },
            },
        }
        r1 = request_mcp_json(init_body)
        if r1.status_code not in (200, 202):
            print_result(
                f"POST {MCP_BASE_URL}/mcp initialize",
                False,
                r1.status_code,
                r1.text[:120],
            )
            return False

        initialized_body = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
        request_mcp_json(initialized_body)

        list_body = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }
        r2 = request_mcp_json(list_body)
        data = safe_json(r2)
        tools = data.get("result", {}).get("tools", [])
        names = {t.get("name") for t in tools if isinstance(t, dict)}
        expected = {
            "run_coding_workflow",
            "check_pending_tasks",
            "cancel_pending_task",
            "clear_session",
            "list_skills",
            "sync_skills",
            "list_aca_environment_variables",
        }
        ok = r2.status_code == 200 and expected.issubset(names)
        missing = sorted(expected - names)
        detail = "missing=" + ",".join(missing) if missing else f"tools={len(names)}"
        print_result(f"POST {MCP_BASE_URL}/mcp tools/list", ok, r2.status_code, detail)
        return ok
    except Exception as e:
        print_result(f"POST {MCP_BASE_URL}/mcp tools/list", False, None, str(e))
        return False


def safe_json(response: requests.Response) -> dict:
    try:
        return response.json()
    except Exception:
        return {"_raw": response.text}


def main() -> int:
    print(f"APIM_BASE_URL={BASE_URL}")
    print(f"APIM_MCP_BASE_URL={MCP_BASE_URL}")

    if TEST_MCP_ONLY:
        checks = [
            test_mcp_tools_list(),
        ]
    else:
        checks = [
            test_health(),
            test_skills(),
            test_clear(),
            test_sync(),
            test_run_workflow(),
            test_callback_validation("/api/skills/approve"),
            test_callback_validation("/api/skills/reject"),
            test_mcp_tools_list(),
        ]

    passed = sum(1 for x in checks if x)
    total = len(checks)
    print(f"\nResult: {passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
