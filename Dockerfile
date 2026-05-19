# ============================================================================
# Hosted Agent - Code Agent v10.0 (Pure Coding Tool)
# 2026.03.12 George: v10.0 架構退化為 Pure Coding Tool
# - 同一份 codebase 支援 Foundry Hosted Agent 和 Custom ACA (MCP Tool)
# - Foundry 部署：CMD ["python", "main.py"]
# - Custom ACA 部署：覆寫 CMD 為 ["python", "mcp_server.py"]（未來）
#
# 建置：docker build --platform linux/amd64 -t code-agent-hosted .
# 本地測試：docker run -p 8088:8088 --env-file .env code-agent-hosted
# ============================================================================

FROM python:3.11-slim

# 安裝系統依賴（Graphviz, CJK fonts, ODBC Driver 18 for SQL Server）
RUN apt-get update && apt-get install -y --no-install-recommends \
    graphviz \
    fonts-noto-cjk \
    curl \
    gnupg2 \
    apt-transport-https \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
       | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
       > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends \
       msodbcsql18 \
       unixodbc-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Fix TLS handshake with Fabric SQL (ODBC Driver 18 + OpenSSL 3.x → 0x2746)
# ODBC Driver reads the system openssl.cnf directly, ignoring OPENSSL_CONF env var.
# Patch the system config in-place: lower SECLEVEL from 2 to 0.
# If no CipherString line exists, append the section.
RUN if grep -q 'SECLEVEL=2' /etc/ssl/openssl.cnf; then \
        sed -i 's/@SECLEVEL=2/@SECLEVEL=0/g' /etc/ssl/openssl.cnf; \
    elif grep -q '\[system_default_sect\]' /etc/ssl/openssl.cnf; then \
        sed -i '/\[system_default_sect\]/a CipherString = DEFAULT:@SECLEVEL=0' /etc/ssl/openssl.cnf; \
    else \
        printf '\n[system_default_sect]\nCipherString = DEFAULT:@SECLEVEL=0\n' >> /etc/ssl/openssl.cnf; \
    fi
    
# 2. 安裝 Python 第三方套件 (diagrams)
RUN pip install diagrams

WORKDIR /app

# 先裝 Python 依賴（利用 Docker layer cache）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製應用程式碼
COPY aca_env_inspector.py .
COPY core_handler.py .
COPY main.py .
COPY mcp_server.py .
COPY code_agent_hosted.py .
COPY skill_gatekeeper.py .
COPY conversation_store.py .
COPY skills_sync.py .
COPY obo_helper.py .
COPY code_executor.py .
COPY output_file_store.py .
COPY job_state_store.py .
COPY job_store.py .
COPY jwt_helper.py .

# 複製 skills 目錄（build 時的 snapshot，runtime 會從 Blob sync 更新）
COPY skills/ ./skills/

# Ports: 8088 (Foundry), 8080 (HTTP API)
EXPOSE 8088 8080

# 預設入口：Foundry Hosted Agent
# Custom ACA 部署時覆寫為：CMD ["python", "mcp_server.py"]
#CMD ["python", "main.py"]
CMD ["uvicorn", "mcp_server:app", "--host", "0.0.0.0", "--port", "8080"]