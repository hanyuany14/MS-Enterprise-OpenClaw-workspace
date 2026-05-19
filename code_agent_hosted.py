"""
Code Agent v10.3 - Pure Coding Tool Edition
====================================================================
從「什麼都接的 Copilot endpoint」退化為「純粹的 coding tool」。

架構變更（v10.0）：
- 移除 PreflightCheckAgent — caller 已做完 intent routing，不需要意圖判斷
- CodingAgent 成為唯一對外窗口，吸收部分 Preflight 能力：
  - 當需求不明確或資訊不足時，回傳描述性文字而非硬擠 code
  - Credentials 判斷：DefaultAzureCredential 為主,缺資訊時主動告知 caller
- Workflow 簡化：User → CodingAgent → Execute → (loop) → Result
- Response schema 結構化：status="completed" | "needs_input" | "failed"
- 支援 session_id 機制：跨 invocation 延續 workflow metadata（final_code 等）
- 設計為可同時部署到 Foundry Hosted Agent 和 Custom ACA（MCP Tool）

VERSION: 10.3

2026.04.23 George: v10.3 M365 Copilot Chat 相容性修正 — 改回統一 hyperlink
  問題: v10.2 產出的 markdown inline image `![](url)` 在 M365 Copilot Chat
  不會被 render 成圖片,而是以 raw markdown 字串呈現(大段 URL 連 query string
  全部露出,視覺雜訊極高)。原因: M365 Copilot Chat 的 text response 渲染器
  並不支援 markdown image syntax,跟 Teams chat / Adaptive Card / 支援 full
  markdown 的前端行為不同。正統解法是 MCP Apps UI Widget (OpenAI Apps SDK),
  但需要 tenant 開啟 Custom App Upload + Copilot Access,且工程量較大。
  本次先做最小修補: 圖片也改成「🖼️ 檔名」的 hyperlink,跟非圖片檔案一致。
  使用者點擊會開新分頁直接看圖,不再有長串 raw URL。

  變更:
  - format_uploads_for_output() 移除 image_parts / other_parts 雙 bucket,
    圖片與非圖片統一走單行 hyperlink 格式,只差 emoji (🖼️ vs 📄/📊/📈/📦)
  - v10.2 的 `![](url)` inline image syntax 整段移除
  - IMAGE_EXTS 保留 (還要靠它判斷該用 🖼️),EXT_EMOJI 維持不變
  - UploadResult.description 欄位保留 (未來 widget 化或其他通道仍有用)

2026.04.23 George: v10.2 結構化 upload output — 圖片 inline、非圖片 link
  - 新增 IMAGE_EXTS / EXT_EMOJI 常數
  - 新增 format_uploads_for_output() helper 依副檔名分類:
    - 圖片類: 產出 `![alt](url)` + `[📥 下載 xxx.png](url)` 雙層 markdown
    - 非圖片類: 產出 `[📄 xxx.py](url) — description` (若有 description)
    - SAS URL 原封不動保留 query string (sig/se/sp 等)
  - UploadResult 新增 description 欄位 (可選,預設空字串),
    供未來 Coding Agent 產檔時自行標注用途,例如「產圖腳本」
  - CodeAgentWorkflow.run() TERMINATE_SUCCESS 分支改用新 helper,
    取代舊的 `📥 Download Links:\\n- {name}: {url}` 純文字列表。
    動機: 下游 Helper Agent / Teams chat 支援 markdown image rendering,
    inline 顯示比「複製 URL 再開分頁」順手很多。下游 agent 不再需要
    自行辨識檔案類型、套用格式化規則,符合 proxy pattern 的乾淨設計。

2026.04.15 George: v10.1 結構化 ExecutionResult + DEBUG_MODE debug bundle
  - execute_code() 改回傳 ExecutionResult dataclass (原本回傳 str)
    - .agent_message 維持原本字串格式,workflow loop 零變化
    - .raw_stdout / .raw_stderr 保留未截斷原始輸出 (供 debug bundle)
    - .error_pattern_hits 記錄每個 content_error pattern 命中次數
    - .override_applied / .succeeded_count 記錄 heuristic 決策過程
    - 新增 ExecutionStatus enum (success/content_error/failed/timeout/exception)
  - 變數分離: raw_stdout (完整) vs agent_stdout (截斷) 避免 shadowing
  - content_error pattern match 改對 raw_stdout 執行,避免截斷邊界破壞 word boundary
  - 新增 DEBUG_MODE 環境變數 (預設 false) 與 DEBUG_CONTAINER (預設 code-debug)
  - 新增 _upload_debug_bundle() helper:
    - 每次 execute 後上傳 script + bundle.json 到 code-debug container
    - 路徑格式: {session_id}/turn_{N}_script.py, turn_{N}_bundle.json
    - bundle.json 內含: raw stdout/stderr、error pattern hits、agent 原始回應、
      skills_referenced、env var keys (不含 value,避免 secret 外洩)
    - 失敗不影響主流程 (所有例外被 catch 吞掉,只記 log)
    - 透過 asyncio.to_thread 背景執行,不阻塞 agent loop

2026.03.12 George: v10.0 架構退化為 Pure Coding Tool
  - 移除 PreflightCheckAgent 及其 instructions、NextAction.PREFLIGHT
  - 移除 parse_preflight_json()、format_hitl_message()
  - 移除 MAX_PREFLIGHT_RETRY、ConversationState.preflight_retry_count
  - determine_next_action() 簡化：User → 直接 CODING
  - CodingAgent 新增 HITL 出口：回應不像 code 時視為「需要更多資訊」
  - CODING_INSTRUCTIONS 擴充：加入 Authentication Rules 和 Insufficient Info 處理
  - create_workflow() 不再建立 PreflightCheckAgent，只建 CodingAgent + Gatekeeper
  - CodeAgentWorkflow.__init__() 移除 preflight_agent 參數

基於 v9.4:
- AzureOpenAIResponsesClient (CodingAgent) + AzureAIClient (Gatekeeper)
- Skills-aware Gatekeeper 整合
- FileAgentSkillsProvider/SkillsProvider 支援
- web_search_preview tool 整合
"""

import asyncio
import subprocess
import os
import sys
import glob
import uuid
import logging
import json
import re
import traceback
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any, Optional, List, Dict
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)

from azure.identity.aio import DefaultAzureCredential as AsyncDefaultAzureCredential
from azure.identity import DefaultAzureCredential
from azure.storage.blob import (
    BlobServiceClient,
    BlobClient,
    generate_blob_sas,
    BlobSasPermissions,
)

# 2026.05.12 George: Phase 0 — 抽象介面層
# CodeExecutor / OutputFileStore 在這裡只 import 介面 + dataclass,
# 實際 executor / file_store 物件由 core_handler.startup() 注入到
# CodeAgentWorkflow.executor / .file_store。
from code_executor import (
    CodeExecutor,
    ExecutionResult,
    ExecutionStatus,
)
from output_file_store import (
    OutputFileStore,
    UploadResult,
)


# 2026.02.28 George: v8.1 agent_framework core (renamed)
# ChatAgent → Agent, ChatMessage → Message, Role 現在是 NewType(str)
from agent_framework import Agent, Message, Role

# 2026.03.10 George: v9.3 SkillsProvider rename (RC3)
try:
    from agent_framework import SkillsProvider as FileAgentSkillsProvider
    SKILLS_PROVIDER_AVAILABLE = True
    logger.info("✓ SkillsProvider available (as FileAgentSkillsProvider alias)")
except ImportError:
    try:
        # Fallback for older SDK versions
        from agent_framework import FileAgentSkillsProvider
        SKILLS_PROVIDER_AVAILABLE = True
        logger.info("✓ FileAgentSkillsProvider available (legacy)")
    except ImportError:
        SKILLS_PROVIDER_AVAILABLE = False
        logger.warning("⚠ SkillsProvider not available (upgrade agent-framework to latest)")

try:
    from agent_framework_azure_ai import AzureAIClient
    from azure.ai.projects.aio import AIProjectClient
    from azure.ai.projects.models import PromptAgentDefinition, WebSearchPreviewTool
    AZURE_AI_CLIENT_AVAILABLE = True
    logger.info("✓ AzureAIClient available (v2 Foundry)")
except ImportError as e:
    AZURE_AI_CLIENT_AVAILABLE = False
    logger.warning(f"⚠ AzureAIClient not available ({e})")

# 2026.03.10 George: v9.3 CodingAgent 改用 AzureOpenAIResponsesClient
# 原因：AzureAIClient._remove_agent_level_run_options() 會比對 runtime tools
# 與 created agent tools，不同就移除。SkillsProvider 注入的 load_skill /
# read_skill_resource 不在 created agent tools 中，因此被丟棄。
# AzureOpenAIResponsesClient 直接呼叫 Responses API，支援 runtime tools 注入。
try:
    from agent_framework.azure import AzureOpenAIResponsesClient
    RESPONSES_CLIENT_AVAILABLE = True
    logger.info("✓ AzureOpenAIResponsesClient available (for CodingAgent skills)")
except ImportError:
    RESPONSES_CLIENT_AVAILABLE = False
    logger.warning("⚠ AzureOpenAIResponsesClient not available (CodingAgent skills will not work)")

try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv())
except ImportError:
    pass

# 2026.02.28 George: v8.2 Gatekeeper v2 — 本地 skills/ 寫回
try:
    from skill_gatekeeper import trigger_gatekeeper_async
    GATEKEEPER_MODULE_AVAILABLE = True
    logger.info("✓ Gatekeeper module available")
except ImportError:
    GATEKEEPER_MODULE_AVAILABLE = False
    logger.warning("⚠ Gatekeeper module not available (skill_gatekeeper.py not found)")


# ============================================================================
# CONFIGURATION
# ============================================================================

MAX_CONSECUTIVE_EXECUTION_FAILURES = 5

# 2025.03.04 George: v9.1 [BUG-1] stdout 截斷閾值
# 當 execute_code() 的 stdout 超過此長度時，截斷並標記 ⚠️ OUTPUT_TRUNCATED。
# 目的：防止巨量 API 回應（如 list models 回傳上百個 model 的 JSON）塞爆
# 後續 CodingAgent 的 thread context window。
# 保留前 2000 + 後 2000 字元，中間省略，讓 agent 仍能看到結構與尾部。
MAX_STDOUT_LENGTH = 5000

# 2026.02.28 George: v8.0 Skills 目錄設定
SKILLS_DIR = os.environ.get("SKILLS_DIR", os.path.join(os.getcwd(), "skills"))

# 2026.02.28 George: v8.0 Gatekeeper 開關
GATEKEEPER_ENABLED = os.environ.get("GATEKEEPER_ENABLED", "false").lower() == "true"

# 2026.04.15 George: v10.1 Debug mode 開關
# 開啟時，每次 execute_code() 後會將完整 debug bundle（script 原始碼、
# 未截斷 stdout/stderr、error pattern 命中統計、agent 原始回應）上傳至
# `code-debug` container，供 production 故障排查使用。
# 預設關閉以控制 Blob 成本；debug 時設為 true 即可。
DEBUG_MODE = os.environ.get("DEBUG_MODE", "false").lower() == "true"
DEBUG_CONTAINER = os.environ.get("DEBUG_CONTAINER", "code-debug")

# ----------------------------------------------------------------------------
# 2026.04.23 George: v10.3 Upload output formatting (M365 Copilot Chat 相容版)
# ----------------------------------------------------------------------------
# 目的: workflow TERMINATE_SUCCESS 分支將 upload 結果格式化為可點擊的
# hyperlink 清單,讓下游 M365 Copilot Chat 直接呈現「點開新分頁看檔案」
# 的慣例體驗。
#
# v10.3 改動動機:
#   v10.2 曾嘗試對圖片產出 markdown inline image (`![](url)`),期望能在
#   chat 中直接 render 成圖片。但 M365 Copilot Chat 的 text response
#   渲染器不支援 markdown image syntax,結果變成大段 raw markdown + SAS
#   URL 的醜字串佔滿螢幕。因此退回統一 hyperlink 策略,圖片只用 🖼️ emoji
#   暗示這是視覺檔案,實際預覽仍走「點擊 → 新分頁」。
#
# 設計:
#   - IMAGE_EXTS: 仍保留作為 emoji 分派判斷 (圖片 → 🖼️),但不再走獨立
#     格式化分支。未來若改走 MCP Apps UI Widget 也會用到這組集合。
#   - EXT_EMOJI: 非圖片檔案的視覺提示。沒命中的副檔名會 fallback 成 📄。
# ----------------------------------------------------------------------------
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}

EXT_EMOJI = {
    ".py": "📄", ".md": "📄", ".txt": "📄", ".json": "📄",
    ".pptx": "📊", ".html": "📊",
    ".xlsx": "📈", ".csv": "📈", ".parquet": "📈",
    ".docx": "📄", ".pdf": "📄",
    ".zip": "📦",
}


# ============================================================================
# SKILLS DIRECTORY SETUP
# 2026.02.28 George: v8.0 確保 skills 目錄存在
# ============================================================================

def ensure_skills_dir() -> int:
    """確保 skills 目錄存在，回傳已發現的 skill 數量"""
    os.makedirs(SKILLS_DIR, exist_ok=True)
    
    readme_path = os.path.join(SKILLS_DIR, "README.md")
    if not os.path.exists(readme_path):
        with open(readme_path, "w", encoding="utf-8") as f:
            f.write("""# Agent Skills Directory

此目錄由 FileAgentSkillsProvider 監控。
每個子目錄代表一個 Skill，必須包含 SKILL.md 檔案。

## 目錄結構範例
```
skills/
  my-skill/
    SKILL.md           ← 必要：含 YAML frontmatter
    templates/          ← 選用：參數化模板
    references/         ← 選用：補充文件
    examples/           ← 選用：使用範例
```

## SKILL.md 格式
```markdown
---
name: my-skill
description: "描述這個 skill 做什麼"
---

# My Skill
...
```

Gatekeeper 產出 Skill 後會自動寫入此目錄。
""")
    
    skill_count = 0
    for item in os.listdir(SKILLS_DIR):
        skill_path = os.path.join(SKILLS_DIR, item, "SKILL.md")
        if os.path.isfile(skill_path):
            skill_count += 1
    return skill_count


# ============================================================================
# 2026.03.09 George: v9.2 Skills 參考偵測（wrap FileAgentSkillsProvider）
# ============================================================================
# 2026.03.10 George: v9.3 移除 _wrap_skills_provider_for_tracking
# CodingAgent 改用 AzureOpenAIResponsesClient 後，runtime tools 正常運作，
# load_skill / read_skill_resource 會在 Agent 內部自動 dispatch，
# 不再需要 monkey-patch 來追蹤。
# ============================================================================


# ============================================================================
# 2026.02.28 George: v8.3 Skills 參考偵測（供 Gatekeeper 使用）
# 2026.03.10 George: v9.3 恢復比對法（v9.2 曾移除改用 tracker，現已不需要）
# ============================================================================

def _load_skill_signatures(skills_dir: str = None) -> Dict[str, List[str]]:
    """
    載入所有 skill 的 signature keywords（從 SKILL.md 的 frontmatter + 正文）。
    回傳 {skill_name: [keyword1, keyword2, ...]}
    """
    skills_dir = skills_dir or SKILLS_DIR
    signatures = {}
    
    if not os.path.exists(skills_dir):
        return signatures
    
    for item in os.listdir(skills_dir):
        skill_md = os.path.join(skills_dir, item, "SKILL.md")
        if not os.path.isfile(skill_md):
            continue
        
        try:
            with open(skill_md, "r", encoding="utf-8") as f:
                content = f.read()
            
            keywords = []
            
            if content.startswith("---"):
                end = content.find("---", 3)
                if end != -1:
                    frontmatter = content[3:end]
                    for line in frontmatter.split("\n"):
                        line = line.strip()
                        if line.startswith("name:"):
                            keywords.append(line.split(":", 1)[1].strip().strip('"\''))
                        elif line.startswith("description:"):
                            desc = line.split(":", 1)[1].strip().strip('"\'')
                            keywords.extend(
                                w.lower() for w in re.findall(r'[a-zA-Z]{4,}', desc)
                            )
            
            for match in re.finditer(r'`([^`]{10,200})`', content):
                keywords.append(match.group(1).lower())
            
            if keywords:
                signatures[item] = keywords
                
        except Exception:
            continue
    
    return signatures


def detect_skills_referenced(
    coding_response: str,
    final_code: str,
    skills_dir: str = None,
) -> List[str]:
    """
    偵測 CodingAgent 的回應/代碼中是否參考了已知 skills。
    比對邏輯：掃描每個 skill 的 signature keywords，
    命中 >= 2 個 keywords → 視為參考。
    """
    skills_dir = skills_dir or SKILLS_DIR
    signatures = _load_skill_signatures(skills_dir)
    
    if not signatures:
        return []
    
    combined_text = (coding_response + "\n" + final_code).lower()
    referenced = []
    
    for skill_name, keywords in signatures.items():
        hit_count = sum(1 for kw in keywords if kw.lower() in combined_text)
        if hit_count >= 2:
            referenced.append(skill_name)
    
    return referenced


# ============================================================================
# CONVERSATION STATE (Persistent across HITL) - 保留 v7.4 完整邏輯
# 2026.02.28 George: v8.3 新增 skills_referenced
# ============================================================================

@dataclass
class ConversationState:
    """Maintains full conversation state across HITL interactions."""
    session_id: str
    work_dir: str
    messages: List[Message] = field(default_factory=list)
    last_speaker: str = "User"
    execution_count: int = 0
    user_data: Dict[str, str] = field(default_factory=dict)
    final_script_path: Optional[str] = None
    output_files: List[str] = field(default_factory=list)
    waiting_for_user: bool = False
    hitl_context: Optional[str] = None
    consecutive_execution_failures: int = 0
    # 2026.02.28 George: v8.0 記錄原始使用者需求（供 Gatekeeper 使用）
    original_user_request: Optional[str] = None
    # 2026.02.28 George: v8.3 追蹤本次 CodingAgent 參考了哪些 skills
    skills_referenced: List[str] = field(default_factory=list)
    # 2026.04.23 George: recent_full_outputs (既有,本檔案以下兩行為既有)
    recent_full_outputs: Dict[str, str] = field(default_factory=dict)
    # 2026.05.12 George × Claude: Phase 0 — Hosted Agent 搬遷預留欄位
    # 本階段固定空字串,不被讀寫使用;階段 2 由 HostedAgentExecutor 填入
    hosted_agent_session_id: str = ""
    hosted_agent_session_created_at: str = ""
    # 2026.05.18 George × Claude: v1.7 Phase 3 — Adaptive Timeout cancel checkpoint
    # 由 core_handler._run_coding_agent_inner (line 656 附近) 在呼叫 workflow.run()
    # 之前寫入。turn loop 用此欄位查 Job Store cancel flag (主文件 §5.7 路徑 2)。
    #
    # 三種值的語意:
    #   - None: fallback 路徑 (沒 _job_store 的 dev 環境) → turn loop checkpoint
    #           整段跳過,行為等同 Phase 0
    #   - uuid str: 正常 bg task 路徑 → turn loop 每輪檢查 cancel flag
    #
    # 注意:本欄位不會被 conversation_store metadata 持久化 (它是 per-invocation
    # 的 bg task 識別碼,跨 session 重啟沒有意義),只在 in-memory state 內生存。
    job_id: Optional[str] = None

    def add_user_message(self, text: str):
        # v8.1: Message(role, contents) — contents 是 list[str|Content]
        self.messages.append(Message("user", [text]))
        self.last_speaker = "User"
        self.waiting_for_user = False
        if self.original_user_request is None:
            self.original_user_request = text
    
    def add_assistant_message(self, text: str, author: str):
        # v8.1: Message(role, contents, *, author_name=...)
        self.messages.append(Message("assistant", [text], author_name=author))
        self.last_speaker = author
    
    def get_messages_for_agent(self, max_messages: int = 20) -> List[Message]:
        """
        回傳要傳給 LLM 的 messages。
        
        2026.03.02 George: v9.0 新增 max_messages 滑動窗口
        - 超過 max_messages 時，保留第一條（原始需求）+ 最近 N-1 條
        - 中間插入 system message 提示 LLM 有省略
        - 完整歷史保留在 self.messages（供 Gatekeeper 分析）
        """
        if len(self.messages) <= max_messages:
            return self.messages.copy()
        
        # 第一條 = 原始 user 需求
        first = self.messages[0]
        # 最近 N-1 條
        recent = self.messages[-(max_messages - 1):]
        
        skipped = len(self.messages) - max_messages
        bridge = Message("system", [
            f"[注意：中間省略了 {skipped} 條歷史訊息，"
            f"以上是原始需求，以下是最近的對話紀錄]"
        ])
        
        logger.info(
            f"[SlidingWindow] {len(self.messages)} messages → "
            f"truncated to {max_messages} (skipped {skipped})"
        )
        
        return [first, bridge] + recent
    
    def request_user_input(self, context: str):
        self.waiting_for_user = True
        self.hitl_context = context
    
    def record_execution_failure(self):
        self.consecutive_execution_failures += 1
    
    def reset_execution_failures(self):
        self.consecutive_execution_failures = 0


# ============================================================================
# STATE SERIALIZATION（Table Storage 持久化用）
# 2026.03.02 George: v9.0 新增
# ============================================================================

def state_to_dict(state: ConversationState) -> dict:
    """
    將 ConversationState 序列化為 dict（可 json.dumps）。
    Message 物件轉成 {role, text, author} 格式。
    """
    messages_list = []
    for msg in state.messages:
        text = ""
        if hasattr(msg, "contents") and msg.contents:
            text = str(msg.contents[0]) if msg.contents else ""
        elif hasattr(msg, "text"):
            text = msg.text
        
        author = getattr(msg, "author_name", None) or ""
        role = msg.role if hasattr(msg, "role") else "user"
        messages_list.append({"role": str(role), "text": text, "author": author})
    
    return {
        "session_id": state.session_id,
        "work_dir": state.work_dir,
        "messages": messages_list,
        "last_speaker": state.last_speaker,
        "execution_count": state.execution_count,
        "user_data": state.user_data,
        "final_script_path": state.final_script_path,
        "output_files": state.output_files,
        "waiting_for_user": state.waiting_for_user,
        "hitl_context": state.hitl_context,
        "consecutive_execution_failures": state.consecutive_execution_failures,
        "original_user_request": state.original_user_request,
        "skills_referenced": state.skills_referenced,
    }


def state_from_dict(d: dict) -> ConversationState:
    """
    從 dict 還原 ConversationState。
    Message 從 {role, text, author} 格式還原。
    """
    state = ConversationState(
        session_id=d.get("session_id", str(uuid.uuid4())[:8]),
        work_dir=d.get("work_dir", os.path.join(os.getcwd(), "session_restored")),
    )
    
    for msg_dict in d.get("messages", []):
        role = msg_dict.get("role", "user")
        text = msg_dict.get("text", "")
        author = msg_dict.get("author", "")
        if role == "user":
            state.messages.append(Message("user", [text]))
        elif role == "system":
            state.messages.append(Message("system", [text]))
        else:
            state.messages.append(Message("assistant", [text], author_name=author))
    
    state.last_speaker = d.get("last_speaker", "User")
    state.execution_count = d.get("execution_count", 0)
    state.user_data = d.get("user_data", {})
    state.final_script_path = d.get("final_script_path")
    state.output_files = d.get("output_files", [])
    state.waiting_for_user = d.get("waiting_for_user", False)
    state.hitl_context = d.get("hitl_context")
    state.consecutive_execution_failures = d.get("consecutive_execution_failures", 0)
    state.original_user_request = d.get("original_user_request")
    state.skills_referenced = d.get("skills_referenced", [])
    
    # 確保 work_dir 存在
    os.makedirs(state.work_dir, exist_ok=True)
    
    return state


# ============================================================================
# STATE MANAGEMENT
# ============================================================================

_current_state: Optional[ConversationState] = None

def create_new_state() -> ConversationState:
    global _current_state
    session_id = str(uuid.uuid4())[:8]
    work_dir = os.path.join(os.getcwd(), f"session_{session_id}")
    os.makedirs(work_dir, exist_ok=True)
    _current_state = ConversationState(session_id=session_id, work_dir=work_dir)
    logger.info(f"[Session] Created: {session_id}")
    return _current_state

def get_current_state() -> Optional[ConversationState]:
    return _current_state


# ============================================================================
# SPEAKER SELECTION (Rule-Based) - v10.0 移除 Preflight
# ============================================================================

class NextAction(str, Enum):
    CODING = "CodingAgent"
    EXECUTE = "Execute"
    TERMINATE_SUCCESS = "TerminateSuccess"
    TERMINATE_HITL = "TerminateHITL"

CODE_PATTERNS = [
    r'^import\s+\w+', r'^from\s+\w+\s+import', r'^def\s+\w+\s*\(',
    r'^class\s+\w+', r'^\s*print\s*\(', r'^\s*if\s+__name__',
    r'^\s*with\s+', r'^\s*for\s+\w+\s+in', r'^\s*try\s*:',
]

def looks_like_code(text: str) -> bool:
    if not text:
        return False
    lines = text.strip().split('\n')
    code_count = sum(1 for line in lines[:20] 
                     if any(re.match(p, line.strip()) for p in CODE_PATTERNS))
    return code_count >= 2

def determine_next_action(state: ConversationState, last_response: str) -> tuple[NextAction, Optional[str]]:
    """
    Determine next action based on conversation state.
    
    v10.0: 移除 PreflightCheckAgent。
    - User input → 直接 CODING（caller 已做完 intent routing）
    - CodingAgent 回應不像 code → 視為「需要更多資訊」的結構化回覆，直接 return
    """
    last_speaker = state.last_speaker
    logger.info(f"[Selector] last_speaker={last_speaker}")

    # User input → 直接進 CodingAgent（不再經過 Preflight）
    if last_speaker == "User":
        if state.hitl_context:
            state.hitl_context = None
        return NextAction.CODING, None
    
    # After CodingAgent
    if last_speaker == "CodingAgent":
        if looks_like_code(last_response):
            return NextAction.EXECUTE, None
        # v10.0: CodingAgent 回應不像 code — 可能是「需要更多資訊」或其他描述性回覆
        # 作為 Tool，直接把 CodingAgent 的回覆當結果 return 給 caller
        # caller 的 LLM 會根據 response 內容決定是呈現結果還是追問用戶
        logger.info(f"[Selector] CodingAgent response is not code, returning as needs_input")
        state.request_user_input("coding_needs_info")
        return NextAction.TERMINATE_HITL, last_response
    
    # After code execution
    if last_speaker == "CodeExecutor":
        if "✅" in last_response or "SUCCESS" in last_response:
            state.reset_execution_failures()
            return NextAction.TERMINATE_SUCCESS, last_response
        
        if "❌" in last_response or "FAILED" in last_response or "Error" in last_response:
            state.record_execution_failure()
            logger.error(f"[Selector] Execution failed, consecutive_failures={state.consecutive_execution_failures}/{MAX_CONSECUTIVE_EXECUTION_FAILURES}")
            
            if state.consecutive_execution_failures >= MAX_CONSECUTIVE_EXECUTION_FAILURES:
                msg = (
                    f"程式碼已連續執行失敗 {state.consecutive_execution_failures} 次，無法自動修復。\n\n"
                    f"最後的錯誤訊息：\n{last_response[-500:]}\n\n"
                    "可能的原因：\n"
                    "1. 缺少必要的套件或依賴\n"
                    "2. 需要特定的環境設定\n"
                    "3. 需求描述可能有歧義\n\n"
                    "請檢查上述錯誤訊息，提供更多資訊或調整您的需求。"
                )
                state.request_user_input("execution_failures")
                state.reset_execution_failures()
                return NextAction.TERMINATE_HITL, msg
            return NextAction.CODING, None
        
        # Ambiguous result (no ✅/❌ markers)
        state.record_execution_failure()
        logger.warning(
            f"[Selector] CodeExecutor response has no ✅/❌ markers — "
            f"treating as ambiguous result, consecutive_failures="
            f"{state.consecutive_execution_failures}/{MAX_CONSECUTIVE_EXECUTION_FAILURES}"
        )
        if state.consecutive_execution_failures >= MAX_CONSECUTIVE_EXECUTION_FAILURES:
            msg = (
                f"程式碼已連續產生不明確的執行結果 {state.consecutive_execution_failures} 次。\n\n"
                f"最後的回應：\n{last_response[-500:]}\n\n"
                "請檢查需求描述或提供更多資訊。"
            )
            state.request_user_input("ambiguous_execution")
            state.reset_execution_failures()
            return NextAction.TERMINATE_HITL, msg
        return NextAction.CODING, None
    
    return NextAction.TERMINATE_SUCCESS, None



# ============================================================================
# 2026.04.23 George: v10.3 UPLOAD OUTPUT FORMATTING (M365 Copilot Chat 相容版)
# ----------------------------------------------------------------------------
# 把 list[UploadResult] 格式化為下游 agent / 用戶直接可用的 markdown。
#
# v10.3 策略 (相對於 v10.2):
#   - 所有檔案 (無論圖片或非圖片) 統一走「emoji + 檔名」的 hyperlink 格式,
#     圖片用 🖼️ emoji 提示是視覺檔案,其餘依 EXT_EMOJI map 分派。
#   - 移除 v10.2 的 `![alt](url)` inline image syntax — M365 Copilot Chat
#     不 render markdown image,只會把整段原文顯示出來,反而造成視覺雜訊。
#   - 排序上不再區分圖片/非圖片區塊,維持 uploads list 的原始順序
#     (upload_results() 產出順序: final_script 先, 其他 output files 依
#     state.output_files 順序),讓用戶看到的順序可預測。
#   - SAS URL 完整保留 query string (sig/se/sp/sv/sr),
#     任何截斷或改寫都會導致下游 403 (Azure Storage 會驗 HMAC signature)。
#
# 未來若 tenant 開啟 Custom App Upload + Copilot Access 授權,
# 可改走 MCP Apps UI Widget (OpenAI Apps SDK) 在 MCP tool response 層
# 回傳 ui.resourceUri 指向一個最小 HTML widget,才能在 M365 Copilot Chat
# 中真正做到 inline 圖片渲染。
# ----------------------------------------------------------------------------

def format_uploads_for_output(uploads: List["UploadResult"]) -> str:
    """將 upload 結果格式化為下游可直接 relay 的 markdown hyperlink 清單。

    每個檔案產出一行: `[emoji 檔名](sas_url)` 或 `[emoji 檔名](url) — description`
    (若 UploadResult.description 非空)。
    SAS URL 原封保留,不做任何修改。
    """
    if not uploads:
        return ""

    lines: List[str] = []
    for u in uploads:
        filename = os.path.basename(u.original_path)
        ext = os.path.splitext(filename)[1].lower()

        # 2026.04.23 George: v10.3 圖片與非圖片統一 hyperlink,只差 emoji
        emoji = "🖼️" if ext in IMAGE_EXTS else EXT_EMOJI.get(ext, "📄")
        line = f"[{emoji} {filename}]({u.sas_url})"
        if u.description:
            line += f" — {u.description}"
        lines.append(line)

    return "\n".join(lines)


# ============================================================================
# 2026.04.15 George: v10.1 DEBUG BUNDLE UPLOAD
# ----------------------------------------------------------------------------
# 當 DEBUG_MODE=true 時,每次 execute_code() 結束後上傳完整 debug bundle:
#   - script 原始碼 (.py)
#   - bundle metadata (.json): raw_stdout/raw_stderr、error pattern 命中、
#     agent 原始回應、status、截斷狀態等
#
# 設計原則:
#   1. 失敗時不阻塞 agent loop (所有例外吞掉只記 log)
#   2. 與 upload_results() 使用不同 container (code-debug),
#      避免污染 user-facing output 合約
#   3. blob 路徑以 session_id 為 prefix,方便事後 list/清理
#   4. bundle.json 不含任何 env var value (避免 secret 外洩),
#      只記錄 key 名稱作為存在性 audit
# ============================================================================

def _upload_debug_bundle(
    state: ConversationState,
    exec_result: "ExecutionResult",
    coding_response_raw: Optional[str] = None,
    container: str = None,
) -> Optional[str]:
    """同步上傳 debug bundle,回傳 script blob URL (無 SAS) 或 None。

    在 agent loop 中應該包裝在 asyncio.to_thread 內執行,避免阻塞。
    所有例外都被 catch 並只記 log,不會影響主流程。
    """
    if not DEBUG_MODE:
        return None
    
    container = container or DEBUG_CONTAINER
    account_name = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME")
    account_key = os.environ.get("AZURE_STORAGE_ACCOUNT_KEY")
    if not account_name or not account_key:
        logger.warning("[DebugBundle] AZURE_STORAGE_ACCOUNT_* not set, skip upload")
        return None
    
    try:
        blob_service = BlobServiceClient(
            account_url=f"https://{account_name}.blob.core.windows.net",
            credential=account_key
        )
        container_client = blob_service.get_container_client(container)
        try:
            container_client.get_container_properties()
        except Exception:
            container_client.create_container()
        
        turn = exec_result.execution_count
        prefix = f"{state.session_id}/turn_{turn:03d}"
        
        # 1. 上傳 script 原始碼
        script_blob_name = f"{prefix}_script.py"
        script_blob_client = container_client.get_blob_client(script_blob_name)
        script_blob_client.upload_blob(
            exec_result.script_code.encode("utf-8"),
            overwrite=True,
        )
        
        # 2. 組 bundle metadata
        # 注意:env_keys_present 只記 key 名稱,不記 value,避免 secret 外洩
        bundle = {
            "session_id": state.session_id,
            "turn": turn,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "status": exec_result.status.value,
            "returncode": exec_result.returncode,
            "script_path": exec_result.script_path,
            "script_length_chars": len(exec_result.script_code),
            "raw_stdout": exec_result.raw_stdout,
            "raw_stdout_length": len(exec_result.raw_stdout),
            "raw_stderr": exec_result.raw_stderr,
            "raw_stderr_length": len(exec_result.raw_stderr),
            "agent_message": exec_result.agent_message,
            "truncated": exec_result.truncated,
            "error_pattern_hits": exec_result.error_pattern_hits,
            "override_applied": exec_result.override_applied,
            "succeeded_count": exec_result.succeeded_count,
            "coding_response_raw": coding_response_raw or "",
            "coding_response_length": len(coding_response_raw) if coding_response_raw else 0,
            "skills_referenced": list(state.skills_referenced),
            "original_user_request": state.original_user_request,
            "consecutive_execution_failures": state.consecutive_execution_failures,
            "env_keys_present": sorted(list(state.user_data.keys())),
        }
        
        bundle_blob_name = f"{prefix}_bundle.json"
        bundle_blob_client = container_client.get_blob_client(bundle_blob_name)
        bundle_blob_client.upload_blob(
            json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8"),
            overwrite=True,
        )
        
        logger.info(
            f"[DebugBundle] uploaded turn={turn} status={exec_result.status.value} "
            f"→ {container}/{prefix}_*"
        )
        return script_blob_client.url
    
    except Exception as e:
        # 絕對不能因為 debug upload 失敗影響主流程
        logger.warning(f"[DebugBundle] upload failed (non-fatal): {e}")
        return None


# ============================================================================
# GATEKEEPER ASYNC TRIGGER
# 2026.02.28 George: v8.2 已遷移至 skill_gatekeeper.py
# 入口函式: trigger_gatekeeper_async() 由 import 引入
# 觸發點: CodeAgentWorkflow.run() 的 TERMINATE_SUCCESS 分支
# v8.3: 新增 skills_referenced 參數傳遞
# ============================================================================


# ============================================================================
# AGENT DEFINITIONS - 保留 v7.4 Instructions
# ============================================================================

# v10.0: PREFLIGHT_INSTRUCTIONS 已移除 — caller 負責 intent routing

CODING_INSTRUCTIONS = """
You are an expert Python coding assistant with selective web search capability.
You are deployed as a **coding tool** — the caller has already determined this is a coding task.

ROLE: Generate executable Python code, and fix code when execution errors occur.
You can search the web for current information, documentation, or examples when needed.

RULES:
1. When you have enough information: Respond ONLY with executable Python code.
2. ABSOLUTELY NO explanations, markdown formatting, or preamble when outputting code.
3. DO NOT wrap code in ```python``` blocks. Output RAW text only.
4. The code MUST print the final output/result to stdout.
5. 🚫 **STRICT PACKAGE POLICY:** DO NOT attempt to install any third-party packages using `pip`, `os.system`, or `subprocess`. The environment uses a pre-built Docker image with locked dependencies.

SKILL-FIRST CHECK:
Before responding with [NEEDS_INFO], ALWAYS check if any available skill matches the request.
- Check if a relevant skill exists and call load_skill first.
- Only respond with [NEEDS_INFO] if NO skill matches AND you truly lack the required information.

HANDLING INSUFFICIENT INFORMATION:
When the request is unclear or missing critical information needed to write code:
- Do NOT attempt to write code that will certainly fail.
- Instead, respond with a clear, concise message explaining what information you need.
- Format: Start your response with "[NEEDS_INFO]:" followed by what's missing.
- Example: "[NEEDS_INFO]: I need the Azure subscription ID and resource group name to proceed. Please also clarify whether you want to list all VMs or only running ones."
- Keep it short and actionable — the caller will relay this to the user.

AUTHENTICATION RULES:
1. Default: Always use DefaultAzureCredential() for Azure services.
   This works for Azure Blob, Table Storage, AI Services, Key Vault, etc.
   The execution environment has Managed Identity configured.
2. User-provided credentials: If the task input includes environment variables
   or explicit credentials, use them. They are available via os.environ.
3. When credentials are missing: If the task requires access to a service that
   cannot use DefaultAzureCredential (e.g., third-party APIs, specific API keys),
   respond with "[NEEDS_INFO]:" explaining what credentials are needed,
   rather than writing code that will fail with 401/403.

SKILLS USAGE:
You may have access to Agent Skills — reusable packages of domain expertise.
At the start of each run, you will see a list of available skills (name + description).
- If a skill is relevant to the current task, call `load_skill` to retrieve its full instructions BEFORE writing code.
- If the loaded skill references additional resources (templates, references), call `read_skill_resource` to fetch them.
- Follow the skill instructions to produce higher-quality, domain-aware code.
- If no skill is relevant, proceed normally without loading any skills.

WHEN TO USE WEB SEARCH (web_search_preview tool):
- To retrieve the latest documentation for library syntax or API changes.
- To research specific error messages (Tracebacks) and find verified fixes.
- To fetch real-time data (finance, weather, etc.) or verify public URL endpoints.
- The web search tool is available as a built-in hosted tool. You do NOT need to write code to perform web searches — just decide to search when needed, and the tool will be invoked automatically.

ALWAYS START WITH:
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ERROR FIXING LOGIC:
- **Priority 1 (ModuleNotFoundError):** If you encounter this error or realize a required package is missing, **STOP IMMEDIATELY**. Output ONLY the following message format:
  `[MISSING_DEPENDENCY_ERROR]: The package '[package_name]' is not pre-installed in the environment. Please contact the system administrator to update the DockerFile.`
- Priority 2 (Internal): For common errors (Logic, Typos, Standard Exceptions), fix immediately.
- Priority 3 (Search): For complex/version-specific errors (API changes, obscure Tracebacks), search the web to find the latest verified solution.
- Output the COMPLETE corrected script as a single unit.

IMPORTANT - For matplotlib charts with Chinese text, always start with:
import matplotlib.pyplot as plt
plt.rcParams['font.sans-serif'] = ['Noto Sans CJK JP']
plt.rcParams['axes.unicode_minus'] = False

IMPORTANT - For saving files:
- Save output files (charts, data) in the CURRENT WORKING DIRECTORY
- Use relative paths like 'chart.png', not absolute paths
- Always call plt.savefig() before plt.show() for charts"
"""

# 2026.02.28 George: v8.2 GatekeeperAgent 指令
GATEKEEPER_INSTRUCTIONS = """You are a Skill Gatekeeper agent. Your job is to analyze CodingAgent execution histories and extract reusable technical knowledge.

You will receive a prompt containing:
1. The user's original request
2. The final successful code
3. Error messages encountered during execution

You must respond ONLY with a JSON object (no other text) containing extracted knowledge.

RULES:
- Extract only reusable, generalizable knowledge (not user-specific business logic)
- Focus on: error patterns, root causes, solutions, and applicable conditions
- If no valuable knowledge exists, return {"skip": true, "reason": "..."}
- Always respond with valid JSON only
"""


# ============================================================================
# MAIN WORKFLOW
# 2026.02.28 George: v8.3 新增 _background_tasks 管理 + skills_referenced 追蹤
# ============================================================================


class CodeAgentWorkflow:
    """
    Manual orchestration workflow with full conversation history.
    v10.0: 移除 preflight_agent，CodingAgent 為唯一的 LLM agent。
    """
    
    def __init__(self, coding_agent: Agent, gatekeeper_agent: Agent = None):
        self.coding_agent = coding_agent
        self.gatekeeper_agent = gatekeeper_agent
        self._background_tasks: List[asyncio.Task] = []

        # 2026.05.12 George: Phase 0 — 抽象介面層 attributes
        # 預設 None,由 core_handler.startup() 在 workflow 建立後 setattr 注入。
        # 為什麼預設 None 而非建立預設 LocalSubprocessExecutor:
        # - BlobOutputFileStore 需要 account_name / account_key,沒帳號時
        #   應為 None,這個語意必須由 core_handler 統一決定
        # - 為了三個 attribute 行為一致,全部預設 None,startup 注入後才有值
        self.executor: Optional[CodeExecutor] = None
        self.file_store: Optional[OutputFileStore] = None
        # Phase 0: 注入 InMemoryJobStateStore 空殼,Phase 3 才被使用
        self.job_state_store = None
    
    async def await_background_tasks(self):
        """
        2026.02.28 George: v8.3
        等待所有背景 tasks 完成。在程式結束前呼叫，確保 Gatekeeper 跑完。
        """
        if not self._background_tasks:
            return
        
        pending = [t for t in self._background_tasks if not t.done()]
        if pending:
            logger.info(f"[Workflow] 等待 {len(pending)} 個背景任務完成...")
            results = await asyncio.gather(*pending, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.info(f"[Workflow] 背景任務 {i} 失敗（不影響使用者）: {result}")
        
        self._background_tasks.clear()
    
    async def run(self, user_input: str, state: ConversationState = None) -> Dict:
        if state is None:
            state = get_current_state() or create_new_state()
        
        state.add_user_message(user_input)
        
        logger.info(f"\n{'='*80}")
        logger.info(f"Query: {user_input}")
        logger.info(f"History: {len(state.messages)} messages")
        logger.info(f"{'='*80}")
        
        max_rounds = 20
        last_response = user_input
        # 2026.03.10 George: v9.3 記錄 CodingAgent 回應（供 Gatekeeper skills 偵測）
        last_coding_response = ""
        
        for round_num in range(max_rounds):
            # ──────────────────────────────────────────────────────────────
            # 2026.05.18 George × Claude: v1.7 Phase 3 — Cancel checkpoint
            # ──────────────────────────────────────────────────────────────
            # 對應主文件 §5.7 cooperative cancellation 的「路徑 2」(turn 邊界):
            # cancel_pending_task 把 cancel_requested flag 寫進 Job Store 後,
            # 下個 turn 在這裡偵測,break 出 turn loop。
            #
            # 路徑 1 (executor.cancel SIGTERM) 在 LocalSubprocessExecutor 內處理
            # 正在跑的 subprocess。路徑 2 處理 turn 之間沒 subprocess 時的 cancel。
            # 兩條路徑互補,都會抵達同一個 break 點。
            #
            # break 之後落到 core_handler._run_coding_agent_inner Step 7a
            # (core_handler.py line 722-755):
            #   - 用 _job_store.is_cancelled 二次檢查 cancel flag
            #   - 把 Job Store 標為 CANCELLED (不是 COMPLETED/FAILED)
            #   - 不推 Teams 通知 (使用者已主動取消)
            #   - 若仍有 waiter,交付 {"status": "cancelled", ...} shape 結果
            # 所以本檔 workflow.run() 落到 line ~1153 回傳的 "Maximum rounds reached"
            # 訊息在 cancel 場景下會被 Step 7a override,使用者看不到。
            #
            # state.job_id is None 時 (fallback 路徑,沒 _job_store):整段跳過,
            # turn loop 行為等同 Phase 0,完全無 regression。
            # ──────────────────────────────────────────────────────────────
            if state.job_id is not None:
                # lazy import 避免 module-level 引用 _job_store 在 import time
                # 拿到 None (startup() 在 module import 完成後才 inject global)。
                # 不能用 `from core_handler import _job_store` — 會 capture
                # import time 的值 (= None)。
                import core_handler
                if core_handler._job_store is not None:
                    cancelled = await core_handler._job_store.is_cancelled(
                        state.session_id, state.job_id
                    )
                    if cancelled:
                        logger.info(
                            f"[Cancel] Job {state.job_id} cancel flag detected "
                            f"at turn boundary (round={round_num}, "
                            f"execution_count={state.execution_count}), "
                            f"breaking out of turn loop"
                        )
                        break

            action, message = determine_next_action(state, last_response)
            logger.info(f"\n[Round {round_num}] Action: {action.value}")
            
            if action == NextAction.TERMINATE_SUCCESS:
                # 2026.05.12 George: Phase 0 — 改用注入的 file_store
                # 既有 upload_results(state) 在沒有 Storage 帳號時 return [],
                # 維持這個行為:file_store=None 時 uploads=[],不 crash。
                uploads: List[UploadResult] = []
                if state.final_script_path and self.file_store:
                    try:
                        uploads = await self.file_store.put_files(
                            session_id=state.session_id,
                            final_script_path=state.final_script_path,
                            output_files=state.output_files,
                        )
                    except Exception as e:
                        logger.error(f"[Upload] Error: {e}")
                elif state.final_script_path and self.file_store is None:
                    logger.warning(
                        "[Upload] file_store not configured, skipping upload"
                    )


                # 2026.04.23 George: v10.2 改用 format_uploads_for_output()
                # 舊版是: "\n\n📥 Download Links:\n- {name}: {url}" 純文字列表,
                # 下游 agent 只看到一坨 URL,無法區分圖片/非圖片,也無法 inline 顯示。
                # 新版產出的 markdown 已經是下游可直接 relay 的最終格式,
                # Helper Agent 只需要「原封保留」即可。
                final_msg = message or last_response
                if uploads:
                    formatted_uploads = format_uploads_for_output(uploads)
                    if formatted_uploads:
                        final_msg += "\n\n" + formatted_uploads
                
                # 2026.03.10 George: v9.3 Gatekeeper 觸發
                if (GATEKEEPER_ENABLED 
                    and GATEKEEPER_MODULE_AVAILABLE 
                    and state.final_script_path 
                    and state.execution_count > 0):
                    
                    # 讀取最終代碼，偵測 skills 參考（比對法，供 Gatekeeper 使用）
                    final_code = ""
                    try:
                        with open(state.final_script_path, "r", encoding="utf-8") as f:
                            final_code = f.read()
                    except Exception:
                        pass
                    
                    skills_ref = detect_skills_referenced(
                        last_coding_response, final_code
                    )
                    state.skills_referenced = skills_ref
                    
                    # 2026.03.25 George: 改善 log — 無論有無命中都印，加入 error count
                    # 對齊 Gatekeeper 的 log 格式，方便交叉比對
                    _err_count = sum(
                        1 for m in state.messages
                        if getattr(m, 'author_name', None) == "CodeExecutor"
                        and "✅" not in (
                            m.text if hasattr(m, 'text') and m.text
                            else str(m.contents[0]) if hasattr(m, 'contents') and m.contents
                            else ""
                        )
                    )
                    logger.info(
                        f"[Skills] 最終偵測: referenced={skills_ref}, "
                        f"errors={_err_count}, "
                        f"execution_count={state.execution_count}"
                    )
                    
                    async def _gatekeeper_with_blob_sync():
                        """Gatekeeper 執行 + 寫回後自動 sync to Blob Storage"""
                        result = await trigger_gatekeeper_async(
                            state, 
                            user_input, 
                            gatekeeper_agent=self.gatekeeper_agent,
                            skills_referenced=skills_ref,
                        )
                        if result and hasattr(result, "skill_written") and result.skill_written:
                            try:
                                from skills_sync import sync_skill_to_blob
                                await sync_skill_to_blob(result.skill_written)   
                            except Exception as e:
                                logger.warning(f"[Skills] Blob sync failed (non-blocking): {e}")
                        return result

                    
                    task = asyncio.create_task(_gatekeeper_with_blob_sync())
                    self._background_tasks.append(task)
                    self._background_tasks = [
                        t for t in self._background_tasks if not t.done()
                    ]
                
                return {"success": True, "response": final_msg, "uploads": uploads, "needs_input": False, "session_id": state.session_id}
            
            elif action == NextAction.TERMINATE_HITL:
                state.add_assistant_message(message, "Assistant")
                return {"success": True, "response": message, "uploads": [], "needs_input": True}
            
            elif action == NextAction.CODING:
                logger.info(f"[CodingAgent]:")
                response = await self._call_agent(self.coding_agent, state)
                logger.info(f"\n{response[:300]}...")
                state.add_assistant_message(response, "CodingAgent")
                last_response = response
                last_coding_response = response
                
                # 2026.03.25 George: 即時偵測 skills reference（不等到成功才偵測）
                # 原本 detect_skills_referenced 只在 TERMINATE_SUCCESS 才呼叫，
                # 導致失敗場景完全看不到 CodingAgent 參考了哪些 skills，無法 debug
                # 「skill 指引有誤 → 代碼一直失敗」的問題。
                # 改為每次 CodingAgent 回應後立刻偵測，確保每一輪都有 log。
                _round_skills = detect_skills_referenced(response, "")
                if _round_skills:
                    state.skills_referenced = _round_skills
                    logger.info(f"[Skills] Round {round_num} referenced: {_round_skills}")
            
            elif action == NextAction.EXECUTE:
                logger.info(f"[CodeExecutor]: Executing...")
                # 2026.05.12 George: Phase 0 — 改用注入的 executor
                # 注意 1: 既有 execute_code() 是 sync function,executor.execute() 是 async,
                #   所以這邊改用 await。Phase 0 唯一刻意的 async 化,理由是 Phase 3 需要
                #   跟 asyncio.Event 配合做 cooperative cancellation。
                # 注意 2: 既有 execute_code() 在函式內部對 state.execution_count++、
                #   寫入 state.final_script_path / state.output_files。Phase 0 改成
                #   把這些 side effect 提到 caller 端做,降低 executor 跟 state 雙向耦合。
                if self.executor is None:
                    raise RuntimeError(
                        "CodeAgentWorkflow.executor not injected. "
                        "Did core_handler.startup() run successfully?"
                    )

                # Phase 0: ++ 在 caller 端,並把當前 count 傳給 executor 用於命名 script
                state.execution_count += 1
                exec_result = await self.executor.execute(
                    code=last_response,
                    session_id=state.session_id,   # ← Phase 3 新增
                    work_dir=state.work_dir,
                    execution_count=state.execution_count,
                    env_vars=state.user_data,
                    timeout=60,
                )

                # Phase 0: 把 side effect 寫回 state (原本由 execute_code 內部做)
                # 注意只在 SUCCESS 才更新 final_script_path / output_files,
                # 失敗時 state 不該被「半成功」的結果污染 — 維持原本 v10.x 行為
                if exec_result.status == ExecutionStatus.SUCCESS:
                    state.final_script_path = exec_result.script_path
                    state.output_files = exec_result.new_output_files

                response = exec_result.agent_message
                if len(response) > 500:
                    logger.info(f"...\n{response[-500:]}")
                else:
                    logger.info(response)
                state.add_assistant_message(response, "CodeExecutor")
                last_response = response

                # DEBUG_MODE 背景上傳 debug bundle (與既有相同)
                if DEBUG_MODE:
                    asyncio.create_task(
                        asyncio.to_thread(
                            _upload_debug_bundle,
                            state,
                            exec_result,
                            last_coding_response,
                        )
                    )


        return {"success": False, "response": "Maximum rounds reached", "uploads": [], "needs_input": False}
    
    async def _call_agent(self, agent: Agent, state: ConversationState) -> str:
        """Call an agent with full conversation history.
        
        2026.03.11 George: v9.5 加入 per-call diagnostics
        - 每次呼叫前 log agent name、skills on disk、message count
        - stream-event dump 維持 DEBUG（需要時才開）
        - 呼叫後 log response 長度摘要
        
        2026.03.10 George: v9.3 簡化為回傳 str
        - 移除 _skills_loaded_tracker（已不需要 monkey-patch）
        - CodingAgent 使用 AzureOpenAIResponsesClient，load_skill / read_skill_resource
          在 Agent 內部自動 dispatch，不需要外部追蹤
        """
        messages = state.get_messages_for_agent()
        
        # 2026.03.11 George: v9.5 呼叫前 diagnostics
        agent_name = getattr(agent, 'name', 'Unknown')
        
        default_opts = getattr(agent, 'default_options', {})
        tools_in_opts = default_opts.get('tools', [])
        
        # 直接印 tool 內容（dict 的話印 type/name key）
        tools_info = []
        for t in (tools_in_opts or []):
            if isinstance(t, dict):
                # 印關鍵欄位，不要印整個 dict（可能很大）
                tools_info.append(
                    t.get('type', t.get('name', str(list(t.keys()))))
                )
            else:
                tools_info.append(type(t).__name__)
        
        providers_info = []
        if hasattr(agent, 'context_providers') and agent.context_providers:
            for cp in agent.context_providers:
                cp_name = type(cp).__name__
                details = {}

                # 1) 直接讀 SKILLS_DIR 磁碟內容（最可靠）
                if os.path.isdir(SKILLS_DIR):
                    skills_on_disk = sorted([
                        d for d in os.listdir(SKILLS_DIR)
                        if os.path.isfile(os.path.join(SKILLS_DIR, d, "SKILL.md"))
                    ])
                    details['on_disk'] = skills_on_disk

                # 2) 嘗試讀 provider 內部已載入的 skills
                internal_skills = getattr(cp, '_skills', None)
                if internal_skills:
                    if isinstance(internal_skills, dict):
                        details['loaded'] = list(internal_skills.keys())
                    elif isinstance(internal_skills, list):
                        details['loaded'] = [
                            getattr(s, 'name', str(s)[:50]) for s in internal_skills
                        ]

                # 3) 嘗試讀 provider 產生的 tools
                internal_tools = getattr(cp, '_tools', None)
                if internal_tools:
                    details['tools'] = [
                        getattr(t, 'name', type(t).__name__) for t in internal_tools
                    ]

                providers_info.append(f"{cp_name}({details})")


        logger.info(
            f"[Agent-Call] {agent_name}, "
            f"tools={tools_info}, "
            f"providers={providers_info or 'none'}, "
            f"messages={len(messages)}"
        )

        response_text = ""
        try:
            stream = agent.run(messages, stream=True)
            async for update in stream:
                text = update.text or ""
                if text:
                    response_text += text
                    logger.info(text)
        except Exception as e:
            logger.error(f"\n[Error] Agent call failed: {e}")
            response_text = f"Error: {str(e)}"
        
        # 2026.03.11 George: v9.5 呼叫後摘要
        logger.info(
            f"[Agent-Done] {agent_name}, "
            f"response_length={len(response_text)}"
        )
        
        return response_text


# ============================================================================
# SETUP
# 2026.02.28 George: v8.0 改版 - 新版 SDK + Skills / 舊版 fallback
# ============================================================================

async def create_workflow(credential: AsyncDefaultAzureCredential) -> tuple[CodeAgentWorkflow, Any]:
    """
    Create the workflow with agents.
    
    2026.02.28 George: v8.0
    - 使用 AzureAIClient (v2 Foundry) + FileAgentSkillsProvider 支援 Skills
    - ChatAgent → Agent, run_stream → run(stream=True)
    """
    logger.info("Creating agents...")
    
    project_endpoint = os.environ.get("AZURE_AI_PROJECT_ENDPOINT")
    model_deployment = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME")
    # 2026.03.10 George: v9.3 CodingAgent 可用獨立 deployment name
    # 優先順序：AZURE_OPENAI_RESPONSES_DEPLOYMENT_NAME → 共用 model_deployment
    # 用途：CodingAgent 可指向較強的模型（如 gpt-4.1），
    # Gatekeeper 維持較便宜的模型（如 gpt-4.1-mini）
    coding_deployment = (
        os.environ.get("AZURE_OPENAI_RESPONSES_DEPLOYMENT_NAME")
        or model_deployment
    )
    if not project_endpoint or not (model_deployment or coding_deployment):
        raise ValueError(
            "AZURE_AI_PROJECT_ENDPOINT and at least one of "
            "AZURE_AI_MODEL_DEPLOYMENT_NAME / AZURE_OPENAI_RESPONSES_DEPLOYMENT_NAME required"
        )
    
    # 2026.02.28 George: v8.1 準備 Skills Provider
    skills_provider = None
    if SKILLS_PROVIDER_AVAILABLE:
        skill_count = ensure_skills_dir()
        skills_provider = FileAgentSkillsProvider(skill_paths=Path(SKILLS_DIR))
        logger.info(f"  ✓ FileAgentSkillsProvider ({skill_count} skills in {SKILLS_DIR})")
    else:
        logger.warning("  ⚠ Skills not available, running without skills")

    # ─── 共用設定 ───
    sync_cred = DefaultAzureCredential()
    project_client = AIProjectClient(endpoint=project_endpoint, credential=sync_cred)
    tools = [WebSearchPreviewTool()]
    
    # ─── AzureAIClient builder（Gatekeeper 用）───
    # v10.0: PreflightCheckAgent 已移除，AzureAIClient 僅供 Gatekeeper 使用
    # AzureAIClient 使用 Foundry server-side agent，不支援 runtime tools 注入，
    # 但 Gatekeeper 不需要 skills，所以沒有影響。
    async def get_agent_via_foundry(name: str, instructions: str, context_providers=None) -> Agent:
        """使用 AzureAIClient (Foundry) 建立 Agent — 不支援 runtime tools。"""
        version = "1"
        async for agent in project_client.agents.list(kind="prompt"):
            if agent.name == name:
                version = agent.versions.latest.version
                break
        else:
            await project_client.agents.create_version(
                agent_name=name,
                definition=PromptAgentDefinition(
                    model=model_deployment, instructions=instructions, tools=tools
                ),
            )        

        client = AzureAIClient(
            project_client=project_client,
            model_deployment_name=model_deployment,
            agent_name=name,
            agent_version=version,
        )
        if hasattr(client, 'warn_runtime_tools_and_structure_changed'):
            client.warn_runtime_tools_and_structure_changed = True
        
        agent = Agent(
            client=client,
            instructions=instructions,
            name=name,
            description=name,
            context_providers=context_providers,
        )
        # 移除 default_options 中的空 tools/tool_choice，避免觸發 AzureAIClient warning
        agent.default_options.pop("tools", None)
        agent.default_options.pop("tool_choice", None)
        return agent
    
    # ─── AzureOpenAIResponsesClient builder（CodingAgent 用）───
    # 2026.03.10 George: v9.3 CodingAgent 改用 AzureOpenAIResponsesClient
    # 原因：DIAG 確認 SkillsProvider 正確注入 load_skill / read_skill_resource
    # 到 chat_options['tools']，但 AzureAIClient._remove_agent_level_run_options()
    # 比對 runtime tools ≠ created agent tools (WebSearchPreviewTool)，直接丟棄。
    # AzureOpenAIResponsesClient 直接呼叫 Responses API，完整支援 runtime tools，
    # 與官方 basic_skill.py / code_skill.py sample 一致。
    async def get_coding_agent(name: str, instructions: str, context_providers=None) -> Agent:
        """使用 AzureOpenAIResponsesClient 建立 Agent — 支援 runtime tools（skills）+ web search。"""
        if not RESPONSES_CLIENT_AVAILABLE:
            logger.warning(
                f"  ⚠ AzureOpenAIResponsesClient not available, "
                f"falling back to AzureAIClient for {name} (skills will not work)"
            )
            return await get_agent_via_foundry(name, instructions, context_providers)
        
        client = AzureOpenAIResponsesClient(
            project_endpoint=project_endpoint,
            deployment_name=coding_deployment,  # 2026.03.10 v9.3: 獨立 deployment
            credential=credential,  # 使用傳入的 async credential
        )
        
        # 2026.03.10 George: v9.3 加入 web_search_preview tool
        # CodingAgent instructions 中有 "WHEN TO USE WEB SEARCH" 段落，
        # 需要實際注入 web search tool 才能生效。
        # AzureOpenAIResponsesClient.get_web_search_tool() 底層為 web_search_preview，
        # 由 Azure 平台透過 Grounding with Bing Search 處理，無需額外設定 Bing resource。
        web_search_tool = client.get_web_search_tool()
        
        agent = Agent(
            client=client,
            instructions=instructions,
            name=name,
            description=name,
            tools=[web_search_tool],
            context_providers=context_providers,
        )
        logger.info(
            f"  ✓ {name} using AzureOpenAIResponsesClient "
            f"(deployment={coding_deployment}, web_search=✓, skills=✓)"
        )

        return agent
    
    # ─── 建立 Agents ───
    
    # v10.0: PreflightCheckAgent 已移除 — caller 負責 intent routing
    
    # CodingAgent 使用 AzureOpenAIResponsesClient + skills + web search
    coding_providers = [skills_provider] if skills_provider else None
    coding = await get_coding_agent("CodingAgent", CODING_INSTRUCTIONS, context_providers=coding_providers)
    
    # GatekeeperAgent（知識萃取用，可選）→ AzureAIClient
    gatekeeper = None
    if GATEKEEPER_ENABLED and GATEKEEPER_MODULE_AVAILABLE:
        try:
            gatekeeper = await get_agent_via_foundry("GatekeeperAgent", GATEKEEPER_INSTRUCTIONS)
            logger.info("  ✓ GatekeeperAgent (AzureAIClient)")
        except Exception as e:
            logger.error(f"  ⚠ GatekeeperAgent creation failed (will use rule-based fallback): {e}")
    
    logger.info("✓ Workflow ready\n")
    return CodeAgentWorkflow(coding, gatekeeper_agent=gatekeeper), project_client

# ============================================================================
# MAIN（保留 --run 單次執行模式，方便本地測試）
# 2026.03.02 George: v9.0 移除 interactive_mode 入口
# ============================================================================

async def main():
    logger.info("=" * 80)
    logger.info("CODE AGENT v10.3 - Pure Coding Tool Edition")
    logger.info("=" * 80)
    
    for var in ["AZURE_AI_PROJECT_ENDPOINT", "AZURE_STORAGE_ACCOUNT_NAME"]:
        status = "✓" if os.environ.get(var) else "✗"
        logger.info(f"  {var}: {status}")
    
    logger.info(f"  MAX_CONSECUTIVE_EXECUTION_FAILURES: {MAX_CONSECUTIVE_EXECUTION_FAILURES}")
    logger.info(f"  MAX_STDOUT_LENGTH: {MAX_STDOUT_LENGTH}")
    logger.info(f"  SKILLS_DIR: {SKILLS_DIR}")
    logger.info(f"  GATEKEEPER_ENABLED: {GATEKEEPER_ENABLED}")
    logger.info(f"  DEBUG_MODE: {DEBUG_MODE} (container={DEBUG_CONTAINER})")
    logger.info(f"  SDK: AzureOpenAIResponsesClient (CodingAgent) + AzureAIClient (Gatekeeper)")
    logger.info(f"  AZURE_AI_MODEL_DEPLOYMENT_NAME: {os.environ.get('AZURE_AI_MODEL_DEPLOYMENT_NAME', '(not set)')}")
    logger.info(f"  AZURE_OPENAI_RESPONSES_DEPLOYMENT_NAME: {os.environ.get('AZURE_OPENAI_RESPONSES_DEPLOYMENT_NAME', '(not set)')}")
    
    credential = None
    client_to_close = None
    workflow = None
    
    try:
        credential = AsyncDefaultAzureCredential()
        workflow, client_to_close = await create_workflow(credential)
        
        if len(sys.argv) > 2 and sys.argv[1] == '--run':
            query = sys.argv[2]
            state = create_new_state()
            result = await workflow.run(query, state)
            logger.info(f"Result: {result['response']}")
        else:
            logger.info(
                "No --run argument provided. "
                "In Hosted Agent mode, use main.py as entry point. "
                "For local testing: python code_agent_hosted.py --run \"your query\""
            )
    
    except Exception as e:
        logger.error(f"Error: {e}")
        traceback.print_exc()
    finally:
        if workflow:
            try:
                await workflow.await_background_tasks()
            except Exception as e:
                logger.warning(f"[Workflow] 等待背景任務時發生錯誤: {e}")
        
        if client_to_close:
            try:
                if hasattr(client_to_close, 'close'):
                    await client_to_close.close()
            except Exception:
                pass
        if credential:
            try:
                await credential.close()
            except Exception:
                pass
    
    logger.info("DONE")


if __name__ == "__main__":
    asyncio.run(main())