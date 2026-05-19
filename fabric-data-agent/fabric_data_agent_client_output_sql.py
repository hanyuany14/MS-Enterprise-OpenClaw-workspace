"""
Fabric Data Agent Client (slim, NL2SQL-aware).

Targets DataWarehouse / SQLDatabase data sources. Extracts:
  - user question
  - SQL actually executed (from analyze.database.execute)
  - query output (from analyze.database.execute)
  - final assistant answer

Ignores fewshots-loading, filename-generation, nl2code variations,
and raw step metadata.
"""

import json
import os
import re
import time
import uuid
import warnings
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests
from openai import OpenAI

warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    message=r".*Assistants API is deprecated.*",
)


# --- function names Fabric Data Agent emits in run steps -------------------
FN_EXECUTE = "analyze.database.execute"        # has both SQL (args.code) and result (output)
FN_NL2CODE = "analyze.database.nl2code"        # SQL candidates before selection
FN_TRACE   = "trace.analyze_data_warehouse"    # external trace view, output only


@dataclass
class SqlExecution:
    """One executed SQL call."""
    sql: str
    output: str
    source: str          # which function surfaced this (execute / trace)
    datasource: str = ""  # e.g. "dw1 (DataWarehouse)"


@dataclass
class FabricRunResult:
    query: str
    answer: str
    executions: List[SqlExecution] = field(default_factory=list)
    status: str = ""

    def to_string(self, max_output_chars: int = 4000) -> str:
        lines: List[str] = []
        lines.append("=" * 70)
        lines.append(f"QUERY : {self.query}")
        lines.append(f"STATUS: {self.status}")
        lines.append("=" * 70)

        if not self.executions:
            lines.append("\n(no SQL execution captured)")
        else:
            for i, ex in enumerate(self.executions, start=1):
                lines.append(f"\n--- EXECUTION #{i} "
                             f"[{ex.source}] {ex.datasource} ---")
                lines.append("SQL:")
                lines.append(ex.sql.strip() or "(empty)")
                lines.append("\nOUTPUT:")
                out = ex.output or "(empty)"
                if len(out) > max_output_chars:
                    head = out[:max_output_chars]
                    omitted = len(out) - max_output_chars
                    lines.append(head)
                    lines.append(f"... [truncated: {omitted} chars omitted, "
                                 f"total {len(out)} chars]")
                else:
                    lines.append(out)

        lines.append("\n--- ANSWER ---")
        lines.append(self.answer)
        lines.append("=" * 70)
        return "\n".join(lines)


class FabricDataAgentClient:
    """Slim client for Fabric Data Agents with OBO bearer-token injection."""

    API_VERSION = "2024-05-01-preview"

    def __init__(self, tenant_id: str, data_agent_url: str, token: str):
        if not tenant_id:
            raise ValueError("tenant_id is required")
        if not data_agent_url:
            raise ValueError("data_agent_url is required")
        if not token:
            raise ValueError("token is required")
        self.tenant_id = tenant_id
        self.data_agent_url = data_agent_url
        self.token = token

    def _client(self) -> OpenAI:
        return OpenAI(
            api_key="unused",
            base_url=self.data_agent_url,
            default_query={"api-version": self.API_VERSION},
            default_headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "ActivityId": str(uuid.uuid4()),
            },
        )

    def _private_thread_base(self) -> str:
        url = self.data_agent_url
        if "aiskills" in url:
            url = url.replace("aiskills", "dataagents")
        return url.removesuffix("/openai").replace(
            "/aiassistant", "/__private/aiassistant"
        )

    def _create_thread(self, thread_name: Optional[str]) -> Dict[str, Any]:
        if thread_name is None:
            thread_name = f"external-client-thread-{uuid.uuid4()}"
        encoded = quote(thread_name, safe="")
        url = f'{self._private_thread_base()}/threads/fabric?tag="{encoded}"'
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "ActivityId": str(uuid.uuid4()),
        }
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code in (401, 403):
            raise RuntimeError(
                f"Auth failed ({resp.status_code}). Check OBO token Fabric scope. "
                f"Body: {resp.text}"
            )
        resp.raise_for_status()
        return resp.json()

    # ---- extraction ------------------------------------------------------
    @staticmethod
    def _strip_sql_fences(code: str) -> str:
        """Fabric wraps SQL in ```sql ... ``` inside the `code` argument."""
        if not code:
            return ""
        m = re.match(r"^\s*```(?:sql)?\s*(.*?)\s*```\s*$", code, re.DOTALL)
        return m.group(1) if m else code

    @staticmethod
    def _safe_json_loads(s: Any) -> Dict[str, Any]:
        if not isinstance(s, str):
            return {}
        try:
            return json.loads(s)
        except Exception:
            return {}

    def _extract_executions(self, steps_iter) -> List[SqlExecution]:
        """
        Pull SQL + output from analyze.database.execute calls.
        Falls back to trace.analyze_data_warehouse output-only if no execute seen.
        """
        executions: List[SqlExecution] = []
        trace_fallbacks: List[SqlExecution] = []

        for step in steps_iter:
            details = getattr(step, "step_details", None)
            if details is None:
                continue
            for tc in getattr(details, "tool_calls", None) or []:
                fn = getattr(tc, "function", None)
                if fn is None:
                    continue
                name = getattr(fn, "name", "") or ""
                args = self._safe_json_loads(getattr(fn, "arguments", None))
                output = getattr(fn, "output", "") or ""

                if name == FN_EXECUTE:
                    sql = self._strip_sql_fences(args.get("code", ""))
                    ds = args.get("datasource_name", "")
                    ds_type = args.get("datasource_type", "")
                    executions.append(SqlExecution(
                        sql=sql,
                        output=output,
                        source=FN_EXECUTE,
                        datasource=f"{ds} ({ds_type})" if ds else "",
                    ))
                elif name == FN_TRACE:
                    trace_fallbacks.append(SqlExecution(
                        sql="",
                        output=output,
                        source=FN_TRACE,
                    ))

        return executions if executions else trace_fallbacks

    @staticmethod
    def _extract_answer(messages_iter) -> str:
        parts: List[str] = []
        for msg in messages_iter:
            if msg.role != "assistant":
                continue
            for block in msg.content:
                text = getattr(block, "text", None)
                value = getattr(text, "value", None) if text else None
                if value:
                    parts.append(value)
        return "\n".join(parts) if parts else ""

    # ---- public API ------------------------------------------------------
    def ask(
        self,
        question: str,
        timeout: int = 120,
        thread_name: Optional[str] = None,
    ) -> FabricRunResult:
        if not question.strip():
            raise ValueError("Question cannot be empty")

        client = self._client()
        thread: Optional[Dict[str, Any]] = None
        assistant_id: Optional[str] = None
        run_status = "unknown"
        executions: List[SqlExecution] = []
        answer = ""

        try:
            assistant = client.beta.assistants.create(model="not used")
            assistant_id = assistant.id

            thread = self._create_thread(thread_name)
            tid = thread["id"]

            client.beta.threads.messages.create(
                thread_id=tid, role="user", content=question
            )
            run = client.beta.threads.runs.create(
                thread_id=tid, assistant_id=assistant_id
            )

            start = time.time()
            while run.status in ("queued", "in_progress"):
                if time.time() - start > timeout:
                    break
                time.sleep(2)
                run = client.beta.threads.runs.retrieve(
                    thread_id=tid, run_id=run.id
                )
            run_status = run.status

            try:
                steps = client.beta.threads.runs.steps.list(
                    thread_id=tid, run_id=run.id, order="asc"
                )
                executions = self._extract_executions(steps.data)
            except Exception:
                pass

            messages = client.beta.threads.messages.list(
                thread_id=tid, order="asc"
            )
            answer = self._extract_answer(messages.data)
            if not answer:
                answer = (
                    f"(no assistant response; run status: {run_status})"
                    if run_status not in ("queued", "in_progress")
                    else f"(timed out after {timeout}s)"
                )

            return FabricRunResult(
                query=question,
                answer=answer,
                executions=executions,
                status=run_status,
            )

        finally:
            if thread and "id" in thread:
                try:
                    client.beta.threads.delete(thread_id=thread["id"])
                except Exception:
                    pass
            if assistant_id:
                try:
                    client.beta.assistants.delete(assistant_id=assistant_id)
                except Exception:
                    pass


def main() -> None:
    client = FabricDataAgentClient(
        tenant_id=os.environ["AZURE_TENANT_ID"],
        data_agent_url=os.environ["DATA_AGENT_URL"],
        token=os.environ["FABRIC_ACCESS_TOKEN"],
    )
    result = client.ask(
        "retrieve the last two months of weekly Orders growth rate "
        "results from the backend database"
    )
    print(result.to_string(max_output_chars=4000))


if __name__ == "__main__":
    main()
