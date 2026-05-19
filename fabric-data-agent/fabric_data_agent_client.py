import os
import time
import uuid
import warnings
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests
from openai import OpenAI



# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------
@dataclass
class FabricRunResult:
    """Structured result of one ask() call."""

    query: str
    answer: str
    sql_statements: List[str] = field(default_factory=list)
    sql_outputs: List[str] = field(default_factory=list)
    status: str = ""

    def to_string(self, max_output_chars: int = 4000) -> str:
        """Human-readable dump. Truncates each SQL output independently."""
        lines: List[str] = []
        lines.append("=" * 70)
        lines.append(f"QUERY : {self.query}")
        lines.append(f"STATUS: {self.status}")
        lines.append("=" * 70)

        if self.sql_statements:
            for i, sql in enumerate(self.sql_statements, start=1):
                lines.append(f"\n--- SQL #{i} ---")
                lines.append(sql.strip())
        else:
            lines.append("\n(no SQL captured — non-Lakehouse source or no tool call)")

        if self.sql_outputs:
            for i, out in enumerate(self.sql_outputs, start=1):
                lines.append(f"\n--- OUTPUT #{i} ---")
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



# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class FabricDataAgentClient:
    """
    Slim client for Fabric Data Agents with OBO bearer-token injection.
    """

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

    # ---- internal helpers -------------------------------------------------
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

    # ---- SQL / output extraction -----------------------------------------
    @staticmethod
    def _extract_sql_and_outputs(
        steps_iter,
    ) -> tuple[List[str], List[str]]:
        """
        Walk run steps and pull out:
          - SQL strings (from code_interpreter.input on tool_calls steps)
          - Output strings (from code_interpreter.outputs[*].logs)

        Skips images, raw model_dump, and other noise.
        """
        sql_list: List[str] = []
        out_list: List[str] = []

        for step in steps_iter:
            details = getattr(step, "step_details", None)
            if details is None:
                continue
            tool_calls = getattr(details, "tool_calls", None) or []
            for tc in tool_calls:
                ci = getattr(tc, "code_interpreter", None)
                if ci is None:
                    continue

                ci_input = getattr(ci, "input", None)
                if ci_input:
                    sql_list.append(str(ci_input))

                # outputs is a list of typed objects: logs / image / etc.
                for out in getattr(ci, "outputs", None) or []:
                    out_type = getattr(out, "type", None)
                    if out_type == "logs":
                        logs = getattr(out, "logs", None)
                        if logs:
                            out_list.append(str(logs))
                    # ignore image/file outputs — not useful for SQL debug

        return sql_list, out_list

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

    # ---- public API -------------------------------------------------------
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
        sql_list: List[str] = []
        out_list: List[str] = []
        answer = ""

        try:
            assistant = client.beta.assistants.create(model="not used")
            assistant_id = assistant.id

            thread = self._create_thread(thread_name)
            thread_id = thread["id"]

            client.beta.threads.messages.create(
                thread_id=thread_id, role="user", content=question
            )
            run = client.beta.threads.runs.create(
                thread_id=thread_id, assistant_id=assistant_id
            )

            start = time.time()
            while run.status in ("queued", "in_progress"):
                if time.time() - start > timeout:
                    break
                time.sleep(2)
                run = client.beta.threads.runs.retrieve(
                    thread_id=thread_id, run_id=run.id
                )
            run_status = run.status

            # SQL + outputs
            try:
                steps = client.beta.threads.runs.steps.list(
                    thread_id=thread_id, run_id=run.id, order="asc"
                )
                sql_list, out_list = self._extract_sql_and_outputs(steps.data)
            except Exception:
                # don't let debug extraction break the main flow
                pass

            # Final answer
            messages = client.beta.threads.messages.list(
                thread_id=thread_id, order="asc"
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
                sql_statements=sql_list,
                sql_outputs=out_list,
                status=run_status,
            )

        finally:
            # Always clean up thread + assistant — don't leak Foundry resources.
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
