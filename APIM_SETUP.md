# APIM MCP 設定手冊

本手冊只說明如何把現有 MCP Server expose 到 APIM。
目前不需要設定 REST API，也不需要做 API -> MCP 轉換。

## 固定設定值

| 項目 | 值 |
| --- | --- |
| Backend MCP URL | `https://coding-tool-contoso.lemonocean-38f621a9.eastus2.azurecontainerapps.io/mcp` |
| APIM MCP server name | `mc-openclaw-mcpuse-mcp` |
| APIM MCP base path | `mc-openclaw-mcpuse-mcp` |
| APIM MCP URL | `https://kurt-apim.azure-api.net/mc-openclaw-mcpuse-mcp/mcp` |
| Subscription required | `Off` |

## 設定步驟

### 1. 建立 MCP Server

APIM Portal：

```text
APIM > APIs > MCP Servers > + Create MCP server
```

選：

```text
Expose an existing MCP server
```

### 2. 依畫面欄位填值

#### Backend MCP server

| 畫面欄位 | 請填入 |
| --- | --- |
| MCP server base url | `https://coding-tool-contoso.lemonocean-38f621a9.eastus2.azurecontainerapps.io/mcp` |

#### New MCP server

| 畫面欄位 | 請填入 |
| --- | --- |
| Display name | `mc-openclaw-mcpuse-mcp` |
| Name | `mc-openclaw-mcpuse-mcp` |
| Base path | `mc-openclaw-mcpuse-mcp` |
| Description | `OpenClaw Code Agent MCP server` |


本案要求 **Subscription required 必須關閉**。

填完後按：

```text
Create
```

### 3. 關閉 Subscription Required

建立後到：

```text
APIM > APIs > MCP Servers > mc-openclaw-mcpuse-mcp > Settings
```

確認：

| 欄位 | 值 |
| --- | --- |
| Subscription required | `Off` |

## Policy

先使用最小 policy，確認 MCP 串接正常。

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

如果之後需要提前在 APIM 驗證身份，可以再加 `validate-jwt`。
若有使用 `Authorization: Bearer <token>`，請保留原本 `Authorization` header 往 backend 傳，不要改寫成 `Ocp-Apim-Subscription-Key`。


## 使用 MCP Inspector 測試

MCP endpoint：

```text
https://kurt-apim.azure-api.net/mc-openclaw-mcpuse-mcp/mcp
```

### 1. 安裝 Node.js

先確認本機有 Node.js：

```powershell
node -v
npx -v
```

如果沒有，請先安裝 Node.js LTS。

### 2. 啟動 MCP Inspector

```powershell
npx @modelcontextprotocol/inspector
```

啟動後終端機會顯示 MCP Inspector 的本機網址，通常是：

```text
http://localhost:6274
```

用瀏覽器打開該網址。

### 3. 在 MCP Inspector 填入連線資訊

在 MCP Inspector 裡新增 server，填：

| 欄位 | 值 |
| --- | --- |
| Transport type | `Streamable HTTP`|
| URL | `https://kurt-apim.azure-api.net/mc-openclaw-mcpuse-mcp/mcp` |

如果 APIM policy 尚未要求 JWT，不需要填 header。
如果之後 APIM 加上 JWT 驗證，Headers 加：

```json
{
  "Authorization": "Bearer <user-token>"
}
```

### 4. 測試 tools/list

連線後，點：

```text
Connect
```

再到 tools 區域確認可以看到：

```text
run_coding_workflow
check_pending_tasks
cancel_pending_task
clear_session
list_skills
sync_skills
list_aca_environment_variables
```

### 5. 測試呼叫 list_skills

選 tool：

```text
list_skills
```

Arguments 留空：

```json
{}
```

執行後應回傳 skills 清單。

### 6. 測試呼叫 list_aca_environment_variables

選 tool：

```text
list_aca_environment_variables
```

Arguments：

```json
{
  "app_name": "openclaw-helper-agent",
  "resource_group": "rg-openclaw",
  "subscription_id": "00000000-0000-0000-0000-000000000000",
  "include_system_vars": false
}
```

請把 `app_name`、`resource_group`、`subscription_id` 改成客戶實際環境。

## MCP Client 連線資訊

```json
{
  "servers": {
    "openclaw-code-agent": {
      "type": "http",
      "url": "https://kurt-apim.azure-api.net/mc-openclaw-mcpuse-mcp/mcp",
      "headers": {
        "Authorization": "Bearer <user-token>"
      }
    }
  }
}
```

## 常見錯誤

| 錯誤 | 原因 | 解法 |
| --- | --- | --- |
| `same Path 'mc-openclaw-mcpuse'` | MCP base path 跟既有 REST API path 衝突 | MCP base path 改成 `mc-openclaw-mcpuse-mcp` |
| `406 Not Acceptable` | MCP client 沒送正確 `Accept` header | Client 需支援 MCP Streamable HTTP，送 `Accept: application/json, text/event-stream` |
| Tools list 看不到 tools | Backend URL 填錯或 backend MCP 不通 | Backend URL 要填 `https://coding-tool-contoso.lemonocean-38f621a9.eastus2.azurecontainerapps.io/mcp` |
| 要找 approve/reject tool 但沒有 | 這兩個功能不走 MCP | 保留 REST callback 或 Logic App 流程 |
