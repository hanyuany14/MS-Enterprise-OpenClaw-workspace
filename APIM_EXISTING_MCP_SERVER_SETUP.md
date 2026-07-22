# Expose Existing MCP Tools Through APIM MCP Server

This guide explains how to expose the current backend MCP tools through Azure API Management MCP Servers.

Use this when you want MCP clients to call the existing MCP tools through APIM, including:

- `run_coding_workflow`
- `check_pending_tasks`
- `cancel_pending_task`
- `clear_session`
- `list_skills`
- `sync_skills`
- `list_aca_environment_variables`

This is different from importing REST OpenAPI. In this setup, APIM governs the backend MCP server directly and exposes its tools through an APIM-hosted MCP endpoint.

## Architecture

```text
MCP Client
  -> APIM MCP Server endpoint
      -> Backend ACA MCP endpoint
          -> mcp_server.py MCP tools
```

Current backend MCP endpoint:

```text
https://<your-container-app>.azurecontainerapps.io/mcp
```

Expected APIM MCP endpoint after setup:

```text
https://kurt-apim.azure-api.net/<apim-mcp-base-path>/mcp
```

Example:

```text
https://kurt-apim.azure-api.net/openclaw-code-agent-mcp/mcp
```

## Important Difference From REST API

APIM will not create individual REST routes such as:

```text
POST /aca/environment-variables
POST /tasks/check
POST /tasks/cancel
```

Instead, MCP clients call one MCP endpoint:

```text
POST /mcp
```

Then the MCP protocol calls tools by name, for example:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "list_aca_environment_variables",
    "arguments": {
      "app_name": "openclaw-helper-agent",
      "resource_group": "rg-openclaw",
      "subscription_id": "00000000-0000-0000-0000-000000000000",
      "include_system_vars": false
    }
  }
}
```

## Prerequisites

1. APIM tier supports MCP Servers.
2. Backend ACA app is reachable from APIM.
3. Backend MCP endpoint is working:

```text
https://<your-container-app>.azurecontainerapps.io/mcp
```

4. Backend MCP server supports Streamable HTTP.
5. Disable response body logging for MCP traffic. Do not configure APIM policies that read `context.Response.Body` for MCP server policies.

## Step 1: Verify Backend MCP Endpoint

Before configuring APIM, confirm the backend endpoint path.

For this codebase, `mcp_server.py` uses:

```python
app.mount("/", mcp.streamable_http_app())
```

The official MCP SDK default streamable path is `/mcp`, so the backend MCP endpoint should be:

```text
https://<your-container-app>.azurecontainerapps.io/mcp
```

If you are testing locally:

```text
http://localhost:8080/mcp
```

Do not use `/mcp/mcp` unless the code is changed to mount the MCP app at `/mcp`.

## Step 2: Create MCP Server In APIM

In Azure Portal:

1. Open your APIM instance.
2. Go to **APIs**.
3. Select **MCP Servers**.
4. Select **+ Create MCP server**.
5. Choose **Expose an existing MCP server**.

Backend MCP server:

| Field | Value |
| --- | --- |
| Existing MCP server base URL | `https://<your-container-app>.azurecontainerapps.io/mcp` |
| Transport type | `Streamable HTTP` |

New MCP server:

| Field | Suggested value |
| --- | --- |
| Name | `openclaw-code-agent-mcp` |
| Base path | `openclaw-code-agent-mcp` |
| Description | `Governed MCP endpoint for OpenClaw Code Agent tools, including workflow execution, pending task checks, cancellation, skill sync, and ACA environment variable inspection.` |

Products:

- Select the APIM product that should grant access.
- If this is internal only, use an internal product with subscription required.

Create the MCP server.

## Step 3: Confirm Tools Were Discovered

After creation:

1. Open **APIs > MCP Servers**.
2. Select `openclaw-code-agent-mcp`.
3. Open **Tools**.
4. Confirm these tools appear:

| Tool | Expected |
| --- | --- |
| `run_coding_workflow` | Yes |
| `check_pending_tasks` | Yes |
| `cancel_pending_task` | Yes |
| `clear_session` | Yes |
| `list_skills` | Yes |
| `sync_skills` | Yes |
| `list_aca_environment_variables` | Yes |

If tools do not appear, check:

- Backend URL is exactly the MCP endpoint, usually ending in `/mcp`.
- Backend supports Streamable HTTP.
- APIM can reach the ACA app.
- Backend app is running and `/health` reports ready.
- The backend MCP server is not blocked by auth that APIM is not sending.

## Step 4: Configure MCP Server Policies

Open:

```text
APIM > APIs > MCP Servers > openclaw-code-agent-mcp > Policies
```

Minimal policy:

```xml
<policies>
  <inbound>
    <base />
  </inbound>
  <backend>
    <base />
  </backend>
  <outbound>
    <base />
  </outbound>
  <on-error>
    <base />
  </on-error>
</policies>
```

Rate limit example:

```xml
<policies>
  <inbound>
    <base />
    <rate-limit-by-key calls="30"
                       renewal-period="60"
                       counter-key="@(context.Subscription?.Key ?? context.Request.IpAddress)" />
  </inbound>
  <backend>
    <base />
  </backend>
  <outbound>
    <base />
  </outbound>
  <on-error>
    <base />
  </on-error>
</policies>
```

Do not use policies that read or buffer the MCP response body, for example:

```xml
@(context.Response.Body.As<string>())
```

That can break MCP streaming behavior.

## Step 5: Authentication

Do not require APIM subscription keys for this setup.

In APIM, set:

```text
Subscription required: Off
```

If the MCP tool needs user identity, forward the end-user bearer token.

Use this when MCP tools need OBO or user identity passthrough.

Client sends:

```text
Authorization: Bearer <user-token>
```

APIM should not remove or replace this header.

If you need to explicitly forward a token stored in a named value, use a policy like:

```xml
<policies>
  <inbound>
    <base />
    <set-header name="Authorization" exists-action="override">
      <value>Bearer {{backend-user-token}}</value>
    </set-header>
  </inbound>
  <backend>
    <base />
  </backend>
  <outbound>
    <base />
  </outbound>
  <on-error>
    <base />
  </on-error>
</policies>
```

For this codebase:

- `run_coding_workflow` can use bearer token passthrough.
- `check_pending_tasks` and `cancel_pending_task` do not need OBO token.
- `list_aca_environment_variables` uses Azure permissions available to the backend runtime, normally managed identity or configured Azure credentials.

## Step 6: Add MCP Server To A Client

Use the APIM MCP Server URL shown in the APIM MCP Servers blade.

Expected shape:

```text
https://kurt-apim.azure-api.net/openclaw-code-agent-mcp/mcp
```

Example VS Code `.vscode/mcp.json` without subscription key:

```json
{
  "servers": {
    "openclaw-code-agent": {
      "type": "http",
      "url": "https://kurt-apim.azure-api.net/openclaw-code-agent-mcp/mcp"
    }
  }
}
```

If the client supports explicit bearer auth:

```json
{
  "servers": {
    "openclaw-code-agent": {
      "type": "http",
      "url": "https://kurt-apim.azure-api.net/openclaw-code-agent-mcp/mcp",
      "headers": {
        "Authorization": "Bearer ${input:user-token}"
      }
    }
  },
  "inputs": [
    {
      "id": "user-token",
      "type": "promptString",
      "description": "User bearer token",
      "password": true
    }
  ]
}
```

## Step 7: Test Tool Discovery

Use MCP Inspector or an MCP-capable client.

Expected discovery result should include:

```text
run_coding_workflow
check_pending_tasks
cancel_pending_task
clear_session
list_skills
sync_skills
list_aca_environment_variables
```

## Step 8: Test `list_aca_environment_variables`

Ask the MCP client to call:

```text
list_aca_environment_variables
```

Arguments:

```json
{
  "app_name": "openclaw-helper-agent",
  "resource_group": "rg-openclaw",
  "subscription_id": "00000000-0000-0000-0000-000000000000",
  "include_system_vars": false
}
```

Expected response shape:

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
      "FABRIC_SQL_CONNECTION_STRING",
      "OBO_CLIENT_ID"
    ]
  }
}
```

## Step 9: Test Long Task Tools

First call:

```text
run_coding_workflow
```

Arguments:

```json
{
  "request": "幫我產生一個需要較久執行的資料分析腳本",
  "session_id": "",
  "credentials": "{}"
}
```

If response status is `running`, save `session_id`, then call:

```text
check_pending_tasks
```

Arguments:

```json
{
  "session_id": "<session-id>",
  "max_wait": 80
}
```

To cancel:

```text
cancel_pending_task
```

Arguments:

```json
{
  "session_id": "<session-id>"
}
```

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Tools list is empty | Wrong backend URL | Use backend URL ending in `/mcp`. |
| Backend returns 404 | Used `/mcp/mcp` but current code exposes `/mcp` | Switch to `/mcp`. |
| 401 from backend | APIM is not forwarding required auth | Preserve or set `Authorization` header. |
| MCP stream fails | APIM policy or diagnostics buffers response body | Disable response body logging and do not read `context.Response.Body`. |
| `list_aca_environment_variables` fails with Azure permission error | Backend identity lacks Container Apps Reader | Assign required Azure RBAC to backend identity. |
| REST APIs work but MCP does not | Imported OpenAPI REST API only | Create APIM MCP Server using "Expose an existing MCP server". |

## When To Use REST OpenAPI Instead

Use REST OpenAPI import when the caller is:

- Logic App
- Power Automate
- A normal HTTP client
- A system that cannot speak MCP

Use APIM MCP Server when the caller is:

- VS Code GitHub Copilot agent mode
- MCP Inspector
- Claude / ChatGPT / other MCP-compatible clients
- An agent runtime that can call MCP tools

If a tool must be available as both REST and MCP, add a REST wrapper endpoint in `mcp_server.py` and expose that endpoint through OpenAPI.
