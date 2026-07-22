# API Documentation

This document summarizes the public APIs exposed by this codebase. It is based on `mcp_server.py` as the active HTTP/MCP entry point, plus the Foundry adapter behavior in `main.py`.

## Overview

The service exposes three integration surfaces:

| Surface | Entry point | Purpose |
| --- | --- | --- |
| REST API | `mcp_server.py` | HTTP JSON endpoints for clients that do not speak MCP. |
| MCP tools | `mcp_server.py` | Standard MCP Streamable HTTP tools for MCP-capable agents. |
| Foundry adapter | `main.py` | Azure AI AgentServer / Foundry Hosted Agent protocol handler. |

Default HTTP port: `8080`, configurable with `HTTP_PORT`.

Base URL examples:

```text
http://localhost:8080
https://<your-container-app>.azurecontainerapps.io
```

Authentication:

- REST `/run` reads `Authorization: Bearer <token>` and injects it into workflow credentials as `__user_token`.
- A FastAPI middleware also extracts bearer tokens into context for downstream handlers.
- Endpoint-level authorization is not implemented in this file; ingress, APIM, ACA, or upstream platform policy is expected to enforce access control.

## REST API

### POST `/run`

Runs the coding workflow.

Request body:

```json
{
  "request": "幫我寫一個 Azure Blob 上傳腳本",
  "session_id": "abc-123",
  "credentials": {
    "API_KEY": "xxx",
    "ENDPOINT": "https://example.com"
  }
}
```

Fields:

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `request` | string | Yes | Natural-language task request. |
| `session_id` | string | No | Existing session ID. Omit or empty means create a new session. |
| `credentials` | object or JSON string | No | Runtime credentials/environment values. Non-JSON strings are ignored. |

Successful response:

```json
{
  "status": "completed",
  "response": "...",
  "session_id": "abc-123",
  "session_hint": "帶回此 session_id 以延續對話上下文（turn history + final code）",
  "skills_referenced": [],
  "turn_count": 1,
  "uploads": [
    {
      "filename": "output.xlsx",
      "url": "https://..."
    }
  ]
}
```

Possible `status` values:

| Status | Meaning |
| --- | --- |
| `completed` | Workflow finished successfully. |
| `needs_input` | Workflow needs more information from the user. |
| `failed` | Workflow failed. |
| `running` | Long-running task has been detached and is still running. |
| `rejected` | New request was rejected, usually because another task is already running. |
| `cancelled` | Task was cancelled. |
| `error` | Internal check/cancel/storage error. |

Errors:

| HTTP status | Response |
| --- | --- |
| `400` | `{"status":"failed","error":"Invalid JSON: ..."}` |
| `400` | `{"status":"failed","error":"Missing 'request' field"}` |
| `500` | `{"status":"failed","error":"..."}` |

Example:

```bash
curl -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"request":"產生一個讀取 CSV 並畫圖的 Python 腳本"}'
```

### POST `/clear`

Clears session metadata and conversation history.

Request body:

```json
{
  "session_id": "abc-123"
}
```

Response:

```json
{
  "status": "completed",
  "response": "..."
}
```

Errors:

| HTTP status | Response |
| --- | --- |
| `400` | `{"status":"failed","error":"Invalid JSON"}` |
| `400` | `{"status":"failed","error":"Missing 'session_id'"}` |

### GET `/skills`

Lists locally available skills.

Response:

```json
{
  "status": "completed",
  "response": "- **skill_name**: description\n..."
}
```

### POST `/sync`

Forces skill synchronization from Azure Blob Storage to local runtime.

Request body: none.

Response:

```json
{
  "status": "completed",
  "response": "..."
}
```

### POST `/api/skills/approve`

Human-in-the-loop callback endpoint for approving a pending skill update.

Request body:

```json
{
  "pending_id": "pending-20260326-a1b2c3",
  "reviewer": "george@contoso.com"
}
```

Fields:

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `pending_id` | string | Yes | Pending skill review ID. |
| `reviewer` | string | No | Reviewer identity. Defaults to `unknown`. |

Response:

The response is returned directly from `core_handler.approve_pending_skill(...)`.

Typical success shape:

```json
{
  "status": "approved",
  "pending_id": "pending-20260326-a1b2c3",
  "reviewer": "george@contoso.com"
}
```

Status codes:

| HTTP status | Meaning |
| --- | --- |
| `200` | Skill was approved. |
| `404` | `core_handler` returned a non-`approved` status, usually pending item not found. |
| `400` | Invalid JSON or missing `pending_id`. |
| `500` | Internal error. |

### POST `/api/skills/reject`

Human-in-the-loop callback endpoint for rejecting a pending skill update.

Request body:

```json
{
  "pending_id": "pending-20260326-a1b2c3",
  "reviewer": "george@contoso.com"
}
```

Response:

The response is returned directly from `core_handler.reject_pending_skill(...)`.

Status codes:

| HTTP status | Meaning |
| --- | --- |
| `200` | Reject operation completed. |
| `400` | Invalid JSON or missing `pending_id`. |
| `500` | Internal error. |

### GET `/health`

Health check endpoint for ACA liveness/readiness probes.

Response:

```json
{
  "status": "healthy",
  "service": "code-agent-mcp",
  "version": "10.0",
  "transport": "streamable-http",
  "endpoints": {
    "mcp": "/mcp/mcp",
    "rest": "/run",
    "health": "/health"
  }
}
```

Notes:

- `status` is `healthy` when `core_handler.is_ready()` is true; otherwise `starting`.
- The hardcoded health response currently reports MCP as `/mcp/mcp`, but the current code mounts `mcp.streamable_http_app()` at `/`, and the MCP SDK default streamable path is `/mcp`. That means the effective MCP endpoint is expected to be `/mcp` unless the mount path changes.

## MCP API

MCP server name: `code-agent`.

Transport: Streamable HTTP, JSON response mode, stateless HTTP.

Effective endpoint from current code:

```text
POST /mcp
```

MCP client config example:

```json
{
  "mcpServers": {
    "code-agent": {
      "type": "streamable-http",
      "url": "https://<your-container-app>.azurecontainerapps.io/mcp"
    }
  }
}
```

The file comments and `/health` payload still mention `/mcp/mcp` in some places. The current executable code uses:

```python
app.mount("/", mcp.streamable_http_app())
```

The official MCP SDK default `streamable_http_path` is `/mcp`, so `/mcp` is the expected endpoint for this version of the code.

### Tool `run_coding_workflow`

Runs the coding workflow through MCP.

Arguments:

| Argument | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `request` | string | Yes | | Natural-language coding request. |
| `session_id` | string | No | `""` | Existing session ID. Empty means new session. |
| `credentials` | string | No | `""` | JSON string of credentials. Must parse to an object. |

Return type: JSON string.

Example return:

```json
{
  "status": "completed",
  "response": "...",
  "session_id": "abc-123",
  "session_hint": "帶回此 session_id 以延續對話上下文（turn history + final code）",
  "skills_referenced": [],
  "turn_count": 1,
  "uploads": []
}
```

Invalid credentials response:

```json
{
  "status": "failed",
  "error": "Invalid credentials JSON: ..."
}
```

### Tool `check_pending_tasks`

Checks whether a long-running task exists for a session and optionally long-polls for completion.

Arguments:

| Argument | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `session_id` | string | Yes | | Session ID to inspect. |
| `max_wait` | integer | No | `80` | Max seconds to wait. Values below `1` become `1`; values above `300` become `300`. |

Return type: JSON string.

Possible statuses:

| Status | Meaning |
| --- | --- |
| `completed` | Task completed successfully. |
| `needs_input` | Task completed but needs user input. |
| `failed` | Task failed or request was invalid. |
| `no_running_task` | No running task exists for this session. |
| `still_running` | Long polling timed out but the task is still running. |
| `cancelled` | Task was cancelled. |

Missing session response:

```json
{
  "status": "failed",
  "error": "Missing 'session_id'"
}
```

### Tool `cancel_pending_task`

Requests cancellation of a running long task for a session.

Arguments:

| Argument | Type | Required | Description |
| --- | --- | --- | --- |
| `session_id` | string | Yes | Session ID whose running task should be cancelled. |

Return type: JSON string.

Possible statuses:

| Status | Meaning |
| --- | --- |
| `cancelling` | Cancellation request was sent. |
| `no_running_task` | No running task exists for this session. |
| `failed` | Request was invalid or cancellation failed. |

### Tool `clear_session`

Clears a session through MCP.

Arguments:

| Argument | Type | Required | Description |
| --- | --- | --- | --- |
| `session_id` | string | Yes | Session ID to clear. |

Response:

```json
{
  "status": "completed",
  "response": "..."
}
```

### Tool `list_skills`

Lists runtime skills.

Arguments: none.

Response:

```json
{
  "status": "completed",
  "response": "- **skill_name**: description\n..."
}
```

### Tool `sync_skills`

Forces runtime skill sync from Azure Blob Storage.

Arguments: none.

Response:

```json
{
  "status": "completed",
  "response": "..."
}
```

### Tool `list_aca_environment_variables`

Lists environment variable names for the current revision of an Azure Container App.

Arguments:

| Argument | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `app_name` | string | Yes | | Azure Container App name. |
| `resource_group` | string | No | `""` | Resource group. Empty uses `ACA_INSPECTOR_DEFAULT_RG`. |
| `subscription_id` | string | No | `""` | Subscription ID. Empty uses `ACA_INSPECTOR_DEFAULT_SUB`. |
| `include_system_vars` | boolean | No | `false` | Whether to include system/runtime variables such as `CONTAINER_APP_*`, `PATH`, and `PORT`. |

Response:

```json
{
  "status": "completed",
  "data": {
    "app_name": "openclaw-helper-agent",
    "resource_group": "rg-openclaw",
    "revision": "openclaw-helper-agent--abc123",
    "total_count": 23,
    "system_vars_excluded": true,
    "variable_names": [
      "AZURE_AI_FOUNDRY_PROJECT_ENDPOINT",
      "AZURE_AI_FOUNDRY_API_KEY",
      "FABRIC_SQL_CONNECTION_STRING",
      "OBO_CLIENT_ID"
    ]
  }
}
```

Failure:

```json
{
  "status": "failed",
  "error": "..."
}
```

## Foundry Hosted Agent Adapter

`main.py` exposes an Azure AI AgentServer / Foundry hosted-agent protocol handler, not a standalone FastAPI route.

Runtime behavior:

- Extracts the latest user message from Foundry `context.request.input`.
- Extracts a stable conversation ID from `conversation.id`, `conversation_id`, metadata, context conversation, or `response_id`.
- Uses the conversation ID as `session_id` when calling `core_handler.run_workflow(...)`.
- Supports both streaming and non-streaming responses.

Special text commands:

| Command | Behavior |
| --- | --- |
| `/debug skills` | Returns `core_handler.list_skills()`. |
| `/sync skills` | Runs `core_handler.sync_skills()`. |
| `/clear` | Clears the current conversation/session. |

Streaming mode:

- Emits keep-alive text every `KEEPALIVE_INTERVAL` seconds while the workflow is running.
- Default `KEEPALIVE_INTERVAL`: `2.0`.

## Common Response Payload

REST `/run` and MCP workflow-related tools share the `_build_response_payload(...)` response structure.

Completed or needs-input shape:

```json
{
  "status": "completed",
  "response": "...",
  "session_id": "abc-123",
  "session_hint": "帶回此 session_id 以延續對話上下文（turn history + final code）",
  "skills_referenced": [],
  "turn_count": 1,
  "uploads": [
    {
      "filename": "file.ext",
      "url": "https://..."
    }
  ]
}
```

Pass-through statuses:

The following statuses are returned directly from `core_handler` without remapping:

```text
running
rejected
cancelling
cancelled
no_running_task
still_running
error
```

## Source Map

| API | Source |
| --- | --- |
| MCP tools | `mcp_server.py`, functions decorated with `@mcp.tool()` |
| REST routes | `mcp_server.py`, functions decorated with `@app.get(...)` / `@app.post(...)` |
| Common response builder | `mcp_server.py`, `_build_response_payload(...)` |
| Foundry adapter | `main.py`, `agent_run(context)` |
| Docker default entrypoint | `Dockerfile`, `CMD ["uvicorn", "mcp_server:app", "--host", "0.0.0.0", "--port", "8080"]` |
