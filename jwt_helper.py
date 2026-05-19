"""
JWT Helper — JWT payload 解析 (不驗簽章)
====================================================================
用途:從 Entra ID 發出的 Bearer token 解析 claims (oid / upn / email),
供 Job Store user_id / user_email 欄位 + Teams 通知 payload 使用。

⚠️ 安全聲明 — 不驗簽章的合理性
------------------------------------------------------------
本模組僅做 base64 decode + JSON parse,**不驗 JWT 簽章**。原因:
1. 下游 obo_exchange_all() 會用此 token 去 Entra ID 換 OBO token,
   Entra ID 端會驗簽章 — 簽章無效會直接 reject,惡意 token 無法通過
   OBO exchange。
2. 本模組只用於 audit / display purposes (寫到 Job Store user_email、
   發 Teams 通知時的收件人欄位)。即使理論上有偽造 token 進來,obo
   exchange 失敗後整個 workflow 會 fail,Teams 通知不會發出去。
3. 不引入新依賴 (PyJWT / python-jose) 來驗簽章,因為:
   a) 驗簽章需要 Entra ID JWKs endpoint 拉公鑰 + 快取邏輯,複雜度跟
      實際安全收益不成比例
   b) 真正的 identity 防線在 obo_exchange,不在這層

VERSION: 1.0
2026.05.18 George × Claude: v1.0 Phase 4 — Adaptive Timeout Escalation
  Teams 通知整合需要從 token 拿 user identity (oid + upn/email),寫進
  Job Store user_id / user_email 欄位。
"""

import base64
import json
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def _decode_jwt_payload(token: str) -> Optional[Dict]:
    """純 base64 decode JWT 中間段,回傳 payload dict。

    JWT 格式:header.payload.signature (三段以 . 分隔的 base64url)。
    我們只關心 payload 段,signature 段不驗。

    Args:
        token: 原始 JWT 字串 (不含 "Bearer " 前綴,caller 負責 strip)

    Returns:
        解析後的 payload dict;格式不合法時回 None。
    """
    if not token or not isinstance(token, str):
        return None

    parts = token.split(".")
    if len(parts) != 3:
        # 不是 JWT 格式 (例如:純隨機字串、或 Entra v1 的 opaque token)
        return None

    payload_segment = parts[1]
    # JWT 用 base64url 編碼,可能缺 padding。補 padding 到 4 的倍數。
    padding_needed = (-len(payload_segment)) % 4
    payload_segment += "=" * padding_needed

    try:
        decoded_bytes = base64.urlsafe_b64decode(payload_segment)
        payload = json.loads(decoded_bytes)
        if not isinstance(payload, dict):
            return None
        return payload
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning(f"[JWT] Failed to decode payload: {type(e).__name__}: {e}")
        return None


def parse_jwt_claims(bearer_token: str) -> Dict[str, Optional[str]]:
    """從 Entra ID JWT 解析 user identity claims。

    對應 Entra ID v2.0 token 的常見 claim 名稱:
    - oid:Entra ID Object ID (唯一,跨 tenant 穩定識別)
    - upn:User Principal Name (e.g. "alice@contoso.com")
    - preferred_username:v2.0 token 取代 upn 的欄位,通常也是 email
    - email:email claim (v2.0 token 在 personal MSA 才有)

    取 email 的優先順序:upn → preferred_username → email。
    Enterprise Entra ID token 一般 upn 一定有,fallback 只是保險。

    Args:
        bearer_token: 原始 JWT 字串 (不含 "Bearer " 前綴)。
                      caller 應已 strip 過。

    Returns:
        dict with keys:
            - "oid": str | None (Entra Object ID)
            - "user_email": str | None (upn / preferred_username / email)
        永遠回 dict,即使 parse 失敗也回 {"oid": None, "user_email": None}。
        這樣 caller 不需做 None check,直接 `.get("oid")` 即可。
    """
    empty = {"oid": None, "user_email": None}

    if not bearer_token or not isinstance(bearer_token, str):
        return empty

    # caller 可能傳了帶 "Bearer " 前綴的版本,寬容處理
    token = bearer_token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    payload = _decode_jwt_payload(token)
    if payload is None:
        return empty

    oid = payload.get("oid")
    user_email = (
        payload.get("upn")
        or payload.get("preferred_username")
        or payload.get("email")
    )

    # 確保都是 str 或 None,避免 caller 拿到怪 type
    if oid is not None and not isinstance(oid, str):
        oid = str(oid)
    if user_email is not None and not isinstance(user_email, str):
        user_email = str(user_email)

    return {"oid": oid, "user_email": user_email}
