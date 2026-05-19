"""
test_obo.py — 獨立測試 OBO token exchange
用 ROPC 或 device code flow 取得 user token，再測 OBO exchange。

使用方式：
  python test_obo.py
"""
import asyncio
import os
from msal import PublicClientApplication

import os
from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv())

# debug: 確認環境變數有沒有載入
print(f"AZURE_TENANT_ID = {os.environ.get('AZURE_TENANT_ID', '❌ NOT SET')}")
print(f"OBO_CLIENT_ID = {os.environ.get('OBO_CLIENT_ID', '❌ NOT SET')}")
print(f"OBO_CLIENT_SECRET = {os.environ.get('OBO_CLIENT_SECRET', '❌ NOT SET')[:5] if os.environ.get('OBO_CLIENT_SECRET') else '❌ NOT SET'}...")
print(f"OBO_SCOPE_REGISTRY = {os.environ.get('OBO_SCOPE_REGISTRY', '❌ NOT SET')}")



# App A（前端 client）的 Client ID
# 可以用你自己建的 test client app，或用已有的前端 app
CLIENT_APP_ID = "a0ce1274-bb4a-4e3b-8911-fc554da93dc9"
TENANT_ID = os.environ["AZURE_TENANT_ID"]
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"

# App B（你的 ACA）的 scope
BACKEND_SCOPE = f"api://{os.environ['OBO_CLIENT_ID']}/user_impersonation"
print(f"✅ BACKEND_SCOPE = {BACKEND_SCOPE}")

def get_user_token_interactive():
    """用 device code flow 取得 user token（audience = App B）"""
    app = PublicClientApplication(CLIENT_APP_ID, authority=AUTHORITY)
    flow = app.initiate_device_flow(scopes=[BACKEND_SCOPE])
    print(flow["message"])  # 會印出 "To sign in, use a web browser..."
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" in result:
        print(f"✅ Got user token, expires_in={result.get('expires_in')}s")
        return result["access_token"]
    else:
        print(f"❌ Failed: {result.get('error_description')}")
        return None


async def test_obo(user_token: str):
    """用拿到的 user token 測 OBO exchange"""
    from obo_helper import exchange_all
    tokens = await exchange_all(user_token)
    for name, token in tokens.items():
        print(f"✅ {name}: {token[:4000]}...({len(token)} chars)")
    if not tokens:
        print("❌ No tokens exchanged")


if __name__ == "__main__":

    token = get_user_token_interactive()
    if token:
        print(f"\n📋 Copy this token for curl:\n{token}\n")
        asyncio.run(test_obo(token))        