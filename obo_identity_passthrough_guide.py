"""
===============================================================================
Identity Passthrough + OBO 通用化改動方案
===============================================================================
VERSION: 1.0
2026.03.17 George

本文件說明將 MCP Server / REST API 改為支援 Identity Passthrough，
透過 OBO (On-Behalf-Of) flow 取得多組 resource token 的完整改動。

設計原則：
  ✅ 新增 resource 只需改環境變數 OBO_SCOPE_REGISTRY，不動程式碼
  ✅ OBO 失敗不阻斷 workflow（該輪可能不需要那個 resource）
  ✅ MCP tool / REST route 兩條路徑共用同一套 token extraction 邏輯
  ✅ 向後相容 — 沒有 Bearer token 時退回原本的 DefaultAzureCredential 行為

===============================================================================
檔案異動清單
===============================================================================

  新增:
    obo_helper.py .............. 通用 OBO exchange 模組（已完成）

  修改:
    mcp_server.py .............. 提取 Bearer token（middleware + contextvars）
    core_handler.py ............ 呼叫 OBO exchange，注入 tokens 到 state
    code_agent_hosted.py ....... CODING_INSTRUCTIONS 新增多 resource 連線指引

  不動:
    skills_sync.py ............. 繼續用 account key，與 user identity 無關
    conversation_store.py ...... 繼續用 account key
    skill_gatekeeper.py ........ 繼續用 Managed Identity

===============================================================================
"""


# ==========================================================================
# 1. mcp_server.py 改動
# ==========================================================================

MCP_SERVER_CHANGES = """
--- mcp_server.py 改動 ---

(A) 新增 imports（檔案頂部）:

    import contextvars

(B) 新增 contextvar（在 mcp = FastMCP(...) 之後）:

    # 2026.03.17 George: Identity Passthrough — 從 HTTP header 提取 user token
    _current_user_token: contextvars.ContextVar[str] = contextvars.ContextVar(
        "_current_user_token", default=""
    )

(C) 新增 middleware（在 app = FastAPI(...) 之後，mount 之前）:

    @app.middleware("http")
    async def extract_bearer_token(request: Request, call_next):
        \"\"\"
        2026.03.17 George: Identity Passthrough
        從 Authorization header 提取 Bearer token，
        存入 contextvars 供 MCP tool / REST handler 使用。
        \"\"\"
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            _current_user_token.set(auth_header[7:])
        else:
            _current_user_token.set("")
        response = await call_next(request)
        return response

(D) 修改 MCP tool run_coding_workflow()（在 parsed_credentials 處理後）:

        # 2026.03.17 George: Identity Passthrough — 注入 user token
        user_token = _current_user_token.get("")
        if user_token:
            if parsed_credentials is None:
                parsed_credentials = {}
            parsed_credentials["__user_token"] = user_token

(E) 修改 REST route /run（在 credentials = body.get("credentials") 之後）:

        # 2026.03.17 George: Identity Passthrough — 注入 user token
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            if credentials is None:
                credentials = {}
            credentials["__user_token"] = auth_header[7:]

(F) ⚠️ 重要：mount path 考量
    目前 mount 在 "/"：
        app.mount("/", mcp.streamable_http_app())
    
    FastAPI middleware 對 mount 的 sub-app 可能不會觸發。
    
    選項 1（推薦）：改為 mount 到 "/mcp"
        app.mount("/mcp", mcp.streamable_http_app())
        → MCP endpoint 變為 /mcp/mcp（不變）
        → middleware 對 FastAPI 自身 routes (/run, /clear) 確定生效
        → 但 /mcp/mcp 仍需測試
    
    選項 2：改用 Starlette 的 raw ASGI middleware 包在最外層
        → 確保所有路徑都經過 token extraction
        → 改動較大
    
    → 部署後需用 curl 測試 /mcp/mcp 路徑是否能拿到 token
"""


# ==========================================================================
# 2. core_handler.py 改動
# ==========================================================================

CORE_HANDLER_CHANGES = """
--- core_handler.py 改動 ---

(A) 新增 import（檔案頂部）:

    from obo_helper import exchange_all as obo_exchange_all

(B) 修改 run_workflow()（在 credentials 處理段落，替換原本的 block）:

    原本:
        # 注入 credentials 為環境變數（如果有的話）
        if credentials:
            state.user_data.update(credentials)
            logger.info(f"[State] Injected {len(credentials)} credentials as env vars")

    改為:
        # 注入 credentials 為環境變數
        if credentials:
            # 2026.03.17 George: Identity Passthrough — 提取 user token，做 OBO exchange
            user_token = credentials.pop("__user_token", None)
            if user_token:
                try:
                    obo_tokens = await obo_exchange_all(user_token)
                    if obo_tokens:
                        state.user_data.update(obo_tokens)
                        logger.info(
                            f"[OBO] Injected {len(obo_tokens)} resource tokens: "
                            f"{list(obo_tokens.keys())}"
                        )
                except ValueError as e:
                    # OBO 設定不完整（缺 env vars）— log warning 但不阻斷
                    logger.warning(f"[OBO] Skipped (config incomplete): {e}")
                except Exception as e:
                    logger.error(f"[OBO] Unexpected error during exchange: {e}")

            # 剩餘的 credentials 照舊注入（向後相容）
            if credentials:
                state.user_data.update(credentials)
                logger.info(f"[State] Injected {len(credentials)} credentials as env vars")
"""


# ==========================================================================
# 3. code_agent_hosted.py 改動
# ==========================================================================

CODE_AGENT_CHANGES = """
--- code_agent_hosted.py 改動 ---

在 CODING_INSTRUCTIONS 的 "AUTHENTICATION RULES" 段落之後，
"SKILLS USAGE" 段落之前，新增以下內容:

OBO RESOURCE ACCESS (Identity Passthrough):
The execution environment may have pre-injected access tokens for various Azure resources,
obtained via On-Behalf-Of (OBO) flow using the caller's identity.

Available token environment variables (check with os.environ.get()):
- FABRIC_SQL_ACCESS_TOKEN  → Microsoft Fabric SQL Database
- GRAPH_ACCESS_TOKEN       → Microsoft Graph API
- AI_SEARCH_ACCESS_TOKEN   → Azure AI Search

1. Microsoft Fabric SQL Database:
   If FABRIC_SQL_ACCESS_TOKEN is available:
   ```
   import struct, pyodbc, os
   token = os.environ["FABRIC_SQL_ACCESS_TOKEN"]
   token_bytes = token.encode("UTF-16-LE")
   token_struct = struct.pack(f'<I{len(token_bytes)}s', len(token_bytes), token_bytes)
   conn = pyodbc.connect(
       "Driver={ODBC Driver 18 for SQL Server};"
       f"Server={server_endpoint};"
       f"Database={database_name};"
       "Encrypt=Yes;TrustServerCertificate=No",
       attrs_before={1256: token_struct}
   )
   ```

2. Microsoft Graph API:
   If GRAPH_ACCESS_TOKEN is available:
   ```
   import requests, os
   headers = {"Authorization": f"Bearer {os.environ['GRAPH_ACCESS_TOKEN']}"}
   resp = requests.get("https://graph.microsoft.com/v1.0/me", headers=headers)
   ```

3. Azure AI Search:
   If AI_SEARCH_ACCESS_TOKEN is available:
   ```
   from azure.search.documents import SearchClient
   from azure.core.credentials import AzureKeyCredential
   # 注意：AI Search SDK 也支援 TokenCredential，
   # 但因為已有 OBO token，可直接用 Bearer header:
   import requests, os
   headers = {
       "Authorization": f"Bearer {os.environ['AI_SEARCH_ACCESS_TOKEN']}",
       "Content-Type": "application/json"
   }
   ```

4. If a required token is NOT available:
   Respond with [NEEDS_INFO] explaining that the resource requires
   user authentication (Identity Passthrough), and the caller should
   include an Authorization header with a valid Bearer token.

5. Server endpoints and database names should come from the user's request
   or from environment variables (e.g. FABRIC_SQL_SERVER, FABRIC_SQL_DATABASE).
"""


# ==========================================================================
# 4. 環境變數設定（ACA / .env）
# ==========================================================================

ENV_VARS_NEEDED = """
--- 環境變數設定 ---

# === OBO 基礎設定 ===
AZURE_TENANT_ID=<your-tenant-id>
OBO_CLIENT_ID=<app-registration-client-id>
OBO_CLIENT_SECRET=<app-registration-client-secret>

# === Scope Registry ===
# JSON 格式，key=環境變數名稱, value=resource scope
# 新增 resource 只需在此加一行，不動程式碼
OBO_SCOPE_REGISTRY={"FABRIC_SQL_ACCESS_TOKEN":"https://database.windows.net/.default","GRAPH_ACCESS_TOKEN":"https://graph.microsoft.com/.default","AI_SEARCH_ACCESS_TOKEN":"https://search.azure.com/.default"}

# === Resource-specific endpoints（供 CodingAgent 生成程式碼時使用）===
FABRIC_SQL_SERVER=your-workspace.datawarehouse.fabric.microsoft.com
FABRIC_SQL_DATABASE=your-database-name
AI_SEARCH_ENDPOINT=https://your-search.search.windows.net
AI_SEARCH_INDEX_NAME=your-index

# === 現有設定（不變）===
AZURE_AI_PROJECT_ENDPOINT=...
AZURE_AI_MODEL_DEPLOYMENT_NAME=...
AZURE_STORAGE_ACCOUNT_NAME=...
AZURE_STORAGE_ACCOUNT_KEY=...
"""


# ==========================================================================
# 5. Entra ID App Registration 設定
# ==========================================================================

ENTRA_ID_SETUP = """
--- Entra ID App Registration 設定 ---

1. 建立或使用現有的 App Registration（代表這個 ACA 應用）
   - 記下 Application (client) ID → OBO_CLIENT_ID
   - 建立 Client Secret → OBO_CLIENT_SECRET
   - 記下 Directory (tenant) ID → AZURE_TENANT_ID

2. API Permissions（Delegated）:
   - Microsoft Graph → User.Read（基本，通常已有）
   - Azure SQL Database → user_impersonation（Fabric SQL 用）
   - Azure AI Search → 如需要，加 user_impersonation

3. Expose an API:
   - 設定 Application ID URI（例如 api://<client-id>）
   - 新增 scope: api://<client-id>/access_as_user
   - Authorized client applications: 加入 MCP Client / APIM 的 App ID

4. 確認 MCP Client / APIM 的 App Registration:
   - API Permissions 中加入此 ACA app 的 scope
   - 這樣 client 取得的 token 的 aud 才會是此 ACA app，
     OBO exchange 才能成功

5. Docker Image 額外需求:
   - 確保已安裝 ODBC Driver 18 for SQL Server
   - 確保已安裝 pyodbc
   - Dockerfile 範例:
     RUN curl https://packages.microsoft.com/keys/microsoft.asc | apt-key add - \\
         && curl https://packages.microsoft.com/config/debian/12/prod.list > /etc/apt/sources.list.d/mssql-release.list \\
         && apt-get update \\
         && ACCEPT_EULA=Y apt-get install -y msodbcsql18 unixodbc-dev \\
         && pip install pyodbc
"""


# ==========================================================================
# 6. 測試驗證步驟
# ==========================================================================

TESTING_STEPS = """
--- 測試驗證步驟 ---

Step 1: 確認 middleware token extraction
    curl -X POST https://your-aca.../run \\
        -H "Authorization: Bearer <test-token>" \\
        -H "Content-Type: application/json" \\
        -d '{"request": "print(os.environ.get(\\\"FABRIC_SQL_ACCESS_TOKEN\\\", \\\"NOT_SET\\\"))"}'
    
    → 預期：response 中應看到 token value 或 OBO error（而非 NOT_SET）

Step 2: 確認 MCP 路徑的 middleware
    # 用 MCP client 或 curl 測試 /mcp/mcp
    # 確認 OBO exchange log 有出現
    # 如果沒有 → middleware 未對 mounted sub-app 生效，需調整 mount path

Step 3: 端到端測試 Fabric SQL
    MCP Client:
    tools/call run_coding_workflow
    request: "查詢 Fabric SQL Database 中 dbo.Sales 表的前 10 筆資料"
    
    → 預期：CodingAgent 生成使用 FABRIC_SQL_ACCESS_TOKEN 的 pyodbc 程式碼
    → 預期：execute_code 成功執行並回傳查詢結果

Step 4: 端到端測試 Graph API
    request: "用 Microsoft Graph API 查詢我的 profile 資訊"
    
    → 預期：CodingAgent 生成使用 GRAPH_ACCESS_TOKEN 的 requests 程式碼

Step 5: 無 token 時的降級行為
    # 不帶 Authorization header 發請求
    request: "查詢 Fabric SQL 的資料"
    
    → 預期：CodingAgent 回傳 [NEEDS_INFO] 說明需要 user authentication
"""
