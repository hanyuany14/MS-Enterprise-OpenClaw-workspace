"""
Diagnostic helper: add ONE method to FabricDataAgentClient and call it
once to find out where SQL lives in YOUR data source's run steps.

Usage:
    from fabric_data_agent_client import FabricDataAgentClient
    from diagnose_sql_location import diagnose

    client = FabricDataAgentClient(...)
    diagnose(client, "your same query that returned the orders table")

Paste the printed output back to Claude.
"""

import json
import time
import uuid
from typing import Optional


def diagnose(client, question: str, timeout: int = 120,
             thread_name: Optional[str] = None) -> None:
    """Run a question and dump full tool_call structure for each step."""
    oai = client._client()
    assistant_id = None
    thread = None
    try:
        assistant = oai.beta.assistants.create(model="not used")
        assistant_id = assistant.id
        thread = client._create_thread(thread_name)
        tid = thread["id"]

        oai.beta.threads.messages.create(
            thread_id=tid, role="user", content=question
        )
        run = oai.beta.threads.runs.create(
            thread_id=tid, assistant_id=assistant_id
        )
        start = time.time()
        while run.status in ("queued", "in_progress"):
            if time.time() - start > timeout:
                break
            time.sleep(2)
            run = oai.beta.threads.runs.retrieve(thread_id=tid, run_id=run.id)

        print(f"\n=== Run status: {run.status} ===\n")

        steps = oai.beta.threads.runs.steps.list(
            thread_id=tid, run_id=run.id, order="asc"
        )

        for i, step in enumerate(steps.data, start=1):
            print(f"\n{'=' * 70}")
            print(f"STEP #{i}  type={getattr(step, 'type', '?')}  "
                  f"status={getattr(step, 'status', '?')}")
            print("=" * 70)
            try:
                dump = step.model_dump()
            except Exception:
                dump = {"_repr": repr(step)}
            print(json.dumps(dump, indent=2, default=str)[:8000])

        # Also dump assistant message annotations — Fabric sometimes puts
        # SQL/citations there instead of in steps.
        print(f"\n{'=' * 70}")
        print("ASSISTANT MESSAGE ANNOTATIONS")
        print("=" * 70)
        msgs = oai.beta.threads.messages.list(thread_id=tid, order="asc")
        for msg in msgs.data:
            if msg.role != "assistant":
                continue
            for block in msg.content:
                text = getattr(block, "text", None)
                if not text:
                    continue
                anns = getattr(text, "annotations", None) or []
                if anns:
                    print(json.dumps(
                        [a.model_dump() if hasattr(a, "model_dump") else repr(a)
                         for a in anns],
                        indent=2, default=str
                    )[:4000])
                else:
                    print("(no annotations on this message)")

    finally:
        if thread and "id" in thread:
            try:
                oai.beta.threads.delete(thread_id=thread["id"])
            except Exception:
                pass
        if assistant_id:
            try:
                oai.beta.assistants.delete(assistant_id=assistant_id)
            except Exception:
                pass
