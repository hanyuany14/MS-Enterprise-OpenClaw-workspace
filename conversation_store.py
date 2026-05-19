"""
Conversation Store - Azure Table Storage (Lightweight Mode)
====================================================================
v4.3 Phase 0 — 為階段 2 搬遷 Hosted Agent 預留欄位:
- 新增 hosted_agent_session_id 欄位 (本階段固定空字串)
- 新增 hosted_agent_session_created_at 欄位 (本階段固定空字串)
- 為什麼放同一張 table:1-to-1 對應、生命週期一致、每次 load metadata
  都會用到 (避免額外 round-trip)、跟既有 output_files_json 等欄位
  的設計哲學一致 (對應 design doc §4.4)

v4.2 新增 recent_full_outputs（N=1 sliding window）：
- 儲存 workflow metadata（session state 延續用）
- 新增 recent_full_outputs_json 欄位（上 N 輪的完整輸出，預設 N=1）
- 用於解決 turn summary 資訊不足的問題（例如 Data Agent 回 markdown table
  被截成 300 字後，下一輪 Helper Agent 看不到完整資料）
- conv_id / session_id 由 caller 提供

Table 結構 - conversationstates:
- PartitionKey: "sessions"
- RowKey: conversation_id (= session_id)
- metadata 欄位：session_id, work_dir, hitl_context, original_user_request,
  execution_count, final_code, output_files_json, skills_referenced_json,
  turn_history_json, recent_full_outputs_json,
  hosted_agent_session_id, hosted_agent_session_created_at (v4.3 新增)

VERSION: 4.3
2026.05.12 George × Claude: v4.3 Phase 0 — 預留 Hosted Agent 搬遷欄位
- 新增 hosted_agent_session_id (str, 預設空字串)
- 新增 hosted_agent_session_created_at (str, ISO 8601 格式, 預設空字串)
- load_metadata / save_metadata 支援讀寫這兩個欄位
- 本階段值都是空字串,不會影響既有邏輯
- 階段 2 啟用時,hosted_agent_session_id 對應 Foundry Hosted Agent 平台
  的 agent_session_id,用於跨 turn 還原 sandbox state ($HOME / /files)
- 為何不另開 mapping table:1-to-1 對應、生命週期一致、避免額外 round-trip

2026.04.23 George: v4.2 新增 recent_full_outputs
- 新增 recent_full_outputs_json 欄位（dict 結構，key=turn_index, value=完整輸出）
- load_metadata / save_metadata 支援讀寫
- 30KB size cap（單欄位），超過則截斷並附警告字串
- N=1 sliding window 由 core_handler 控制，store 只負責存取
- 為何用 dict 而非單一字串：未來擴充到 N>1 時不用改 schema

2026.03.13 George: v4.1 累積式 turn history
- 新增 turn_history_json 欄位（累積式 turn 摘要）
- load_metadata / save_metadata 支援 turn_history 讀寫
- turn_history 結構: [{turn, user_request, summary, output_files, timestamp}, ...]

2026.03.12 George: v4.0 輕量化
- 移除 messages_json 欄位（退化為 tool 後 caller 管理對話）
- 移除 preflight_retry_count 欄位（PreflightCheckAgent 已移除)
- 精簡 save_metadata / load_metadata

2026.03.03 George: v3.0 混合對話管理
2026.03.02 George: v2.0 方案 C 改造
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from azure.data.tables import TableServiceClient, TableClient
from azure.core.exceptions import ResourceNotFoundError, ResourceExistsError
from azure.core.credentials import AzureNamedKeyCredential

logger = logging.getLogger(__name__)

TABLE_NAME = os.environ.get("CONVERSATION_TABLE_NAME", "conversationstates")

# 2026.04.23 George: recent_full_outputs 單欄位 size cap
# Table Storage entity 總大小上限 1MB，單一屬性上限 64KB
# 這裡留 30KB 是給其他欄位（final_code、turn_history 等）預留空間
MAX_RECENT_FULL_OUTPUTS_BYTES = 30000
# 單一 output 被截斷時保留的前綴大小
RECENT_FULL_OUTPUT_TRUNCATE_HEAD = 10000


class ConversationStore:
    """Azure Table Storage - workflow metadata + messages 備援"""

    def __init__(self, account_name: str, account_key: str):
        self.account_name = account_name
        self.account_key = account_key
        self._table_client: Optional[TableClient] = None
        self._credential = AzureNamedKeyCredential(account_name, account_key)
        self._service: Optional[TableServiceClient] = None

    def _get_service(self) -> TableServiceClient:
        if self._service is None:
            self._service = TableServiceClient(
                endpoint=f"https://{self.account_name}.table.core.windows.net",
                credential=self._credential,
            )
        return self._service

    def _get_table_client(self) -> TableClient:
        if self._table_client is None:
            self._table_client = self._get_service().get_table_client(TABLE_NAME)
        return self._table_client

    async def ensure_table(self):
        """確保 Table 存在"""
        try:
            client = self._get_table_client()
            client.create_table()
            logger.info(f"Created table: {TABLE_NAME}")
        except ResourceExistsError:
            logger.info(f"Table already exists: {TABLE_NAME}")
        except Exception as e:
            logger.warning(f"Failed to create table: {e}")

    # ================================================================
    # METADATA（含 turn_history + recent_full_outputs）
    # ================================================================

    async def load_metadata(self, conversation_id: str) -> Optional[dict]:
        """
        v4.2: 載入 workflow metadata（含 turn_history + recent_full_outputs）。
        2026.04.23 George: 新增 recent_full_outputs_json 讀取
        2026.03.13 George: 新增 turn_history_json 讀取
        """
        try:
            client = self._get_table_client()
            entity = client.get_entity(
                partition_key="sessions",
                row_key=conversation_id,
            )

            metadata = {
                "session_id": entity.get("session_id", ""),
                "work_dir": entity.get("work_dir", ""),
                "hitl_context": entity.get("hitl_context") or None,
                "original_user_request": entity.get("original_user_request") or None,
                "execution_count": entity.get("execution_count", 0),
                "final_code": entity.get("final_code", ""),
                "output_files_json": entity.get("output_files_json", "[]"),
                "skills_referenced_json": entity.get("skills_referenced_json", "[]"),
                "turn_history_json": entity.get("turn_history_json", "[]"),
                # 2026.04.23 George: v4.2
                "recent_full_outputs_json": entity.get(
                    "recent_full_outputs_json", "{}"
                ),
                # 2026.05.12 George × Claude: v4.3 Phase 0 — Hosted Agent 預留欄位
                # 本階段值固定空字串,不被讀寫使用,只是預先在 schema 留位子。
                # 階段 2 啟用時:
                #   hosted_agent_session_id      = Foundry Agent Service 的 agent_session_id
                #   hosted_agent_session_created_at = ISO 8601 timestamp,用於判斷
                #                                     是否接近 30 天 expiry,需重建 sandbox
                "hosted_agent_session_id": entity.get("hosted_agent_session_id", ""),
                "hosted_agent_session_created_at": entity.get(
                    "hosted_agent_session_created_at", ""
                ),
            }

            # JSON 欄位反序列化
            try:
                metadata["output_files"] = json.loads(metadata.pop("output_files_json"))
            except (json.JSONDecodeError, TypeError):
                metadata["output_files"] = []

            try:
                metadata["skills_referenced"] = json.loads(
                    metadata.pop("skills_referenced_json")
                )
            except (json.JSONDecodeError, TypeError):
                metadata["skills_referenced"] = []

            # 2026.03.13 George: 反序列化 turn_history
            try:
                metadata["turn_history"] = json.loads(
                    metadata.pop("turn_history_json")
                )
            except (json.JSONDecodeError, TypeError):
                metadata["turn_history"] = []

            # 2026.04.23 George: v4.2 反序列化 recent_full_outputs
            try:
                metadata["recent_full_outputs"] = json.loads(
                    metadata.pop("recent_full_outputs_json")
                )
            except (json.JSONDecodeError, TypeError):
                metadata["recent_full_outputs"] = {}

            logger.info(
                f"Loaded metadata: conv={conversation_id}, "
                f"exec_count={metadata.get('execution_count', 0)}, "
                f"turns={len(metadata.get('turn_history', []))}, "
                f"recent_full_outputs={len(metadata.get('recent_full_outputs', {}))}"
            )
            return metadata

        except ResourceNotFoundError:
            logger.info(f"No existing metadata for conv={conversation_id}")
            return None
        except Exception as e:
            logger.error(f"Failed to load metadata: {e}")
            return None

    async def save_metadata(
        self, conversation_id: str, state, max_messages: int = 30
    ) -> bool:
        """
        v4.2: 儲存 workflow metadata（含 turn_history + recent_full_outputs）。
        2026.04.23 George: 新增 recent_full_outputs_json 寫入
        2026.03.13 George: 新增 turn_history_json 寫入
        """
        try:
            # 讀取 final_code
            final_code = ""
            if (
                hasattr(state, "final_script_path")
                and state.final_script_path
                and os.path.exists(state.final_script_path)
            ):
                try:
                    with open(state.final_script_path, "r", encoding="utf-8") as f:
                        final_code = f.read()
                    if len(final_code.encode("utf-8")) > 50000:
                        final_code = final_code[:15000] + "\n\n# ... (truncated) ...\n"
                        logger.warning(
                            f"final_code truncated for conv={conversation_id}"
                        )
                except Exception as e:
                    logger.warning(f"Failed to read final_script: {e}")

            # 2026.03.13 George: 序列化 turn_history，並做大小保護
            turn_history_str = json.dumps(
                getattr(state, "turn_history", []), ensure_ascii=False
            )
            # Table Storage entity 屬性上限 64KB，預留空間給其他欄位
            if len(turn_history_str.encode("utf-8")) > 50000:
                # 保留第一輪 + 最近幾輪，直到 < 50KB
                history = getattr(state, "turn_history", [])
                while len(history) > 2:
                    turn_history_str = json.dumps(
                        [history[0]] + history[-(len(history) - 2):],
                        ensure_ascii=False,
                    )
                    if len(turn_history_str.encode("utf-8")) <= 50000:
                        break
                    history = [history[0]] + history[-(len(history) - 2):]
                logger.warning(
                    f"turn_history truncated for conv={conversation_id}, "
                    f"kept {len(history)} turns"
                )

            # 2026.04.23 George: v4.2 序列化 recent_full_outputs，並做大小保護
            recent_outputs = getattr(state, "recent_full_outputs", {}) or {}
            recent_outputs_str = json.dumps(recent_outputs, ensure_ascii=False)

            if len(recent_outputs_str.encode("utf-8")) > MAX_RECENT_FULL_OUTPUTS_BYTES:
                # 對每個超大 output 做截斷（保留 head + 截斷警告）
                truncated = {}
                for turn_idx, output in recent_outputs.items():
                    if (
                        isinstance(output, str)
                        and len(output.encode("utf-8")) > RECENT_FULL_OUTPUT_TRUNCATE_HEAD
                    ):
                        truncated[turn_idx] = (
                            output[:RECENT_FULL_OUTPUT_TRUNCATE_HEAD]
                            + "\n\n⚠️ [FULL_OUTPUT_TRUNCATED: "
                            + f"exceeded {MAX_RECENT_FULL_OUTPUTS_BYTES // 1000}KB size limit]"
                        )
                    else:
                        truncated[turn_idx] = output
                recent_outputs_str = json.dumps(truncated, ensure_ascii=False)
                logger.warning(
                    f"recent_full_outputs truncated for conv={conversation_id}, "
                    f"original_size={len(json.dumps(recent_outputs, ensure_ascii=False).encode('utf-8'))} bytes"
                )

            client = self._get_table_client()
            client.upsert_entity({
                "PartitionKey": "sessions",
                "RowKey": conversation_id,
                "session_id": getattr(state, "session_id", ""),
                "work_dir": getattr(state, "work_dir", ""),
                "hitl_context": getattr(state, "hitl_context", "") or "",
                "original_user_request": getattr(state, "original_user_request", "") or "",
                "execution_count": getattr(state, "execution_count", 0),
                "final_code": final_code,
                "output_files_json": json.dumps(
                    getattr(state, "output_files", []), ensure_ascii=False
                ),
                "skills_referenced_json": json.dumps(
                    getattr(state, "skills_referenced", []), ensure_ascii=False
                ),
                "turn_history_json": turn_history_str,
                # 2026.04.23 George: v4.2
                "recent_full_outputs_json": recent_outputs_str,
                # 2026.05.12 George × Claude: v4.3 Phase 0 — Hosted Agent 預留欄位
                # 本階段值固定空字串。階段 2 啟用時由 HostedAgentExecutor
                # 在第一次建立 hosted session 後寫入,後續 turn 從這裡讀回
                # agent_session_id 以還原 sandbox。
                "hosted_agent_session_id": getattr(
                    state, "hosted_agent_session_id", ""
                ) or "",
                "hosted_agent_session_created_at": getattr(
                    state, "hosted_agent_session_created_at", ""
                ) or "",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            logger.info(
                f"Saved metadata: conv={conversation_id}, "
                f"exec_count={getattr(state, 'execution_count', 0)}, "
                f"final_code={'yes' if final_code else 'no'}, "
                f"turns={len(getattr(state, 'turn_history', []))}, "
                f"recent_full_outputs={'yes (' + str(len(recent_outputs)) + ' turn)' if recent_outputs else 'no'}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to save metadata: {e}")
            return False

    async def delete_metadata(self, conversation_id: str) -> bool:
        """刪除指定 conversation 的 metadata"""
        try:
            client = self._get_table_client()
            client.delete_entity(
                partition_key="sessions",
                row_key=conversation_id,
            )
            logger.info(f"Deleted metadata: conv={conversation_id}")
            return True
        except ResourceNotFoundError:
            logger.info(f"No metadata to delete: conv={conversation_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete metadata: {e}")
            return False