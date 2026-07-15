# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Meaningful events DAO — SQLite persistence.

一次推理 = 一行 event(同窗口 N 摄像头合并 1 行;`device_ids` JSON 数组记录本行真正相关的摄像头——
有 rule/suggestion/asr 命中时收窄到其 source_device_ids,否则退化为参与本次推理的全部摄像头).
schema_version 字段(行级)标识本行按哪个 schema 版本写入,本期 INSERT 恒写 1.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from miloco.database.connector import get_db_connector
from miloco.utils.time_utils import now_ms

logger = logging.getLogger(__name__)

# 本期写入的行级 schema 版本号;ALTER TABLE 加字段时新行写 2,DAO 读老行按版本走兼容分支
_CURRENT_SCHEMA_VERSION = 1


class MeaningfulEventDao:
    """Data access object for the meaningful_events table.

    通过 manager 单例持有(`mgr.meaningful_events_dao`),禁止在调用点直接构造.
    """

    def __init__(self):
        self.db_connector = get_db_connector()

    def insert(
        self,
        *,
        event_id: str,
        timestamp: int,
        text: str,
        payload_json: str,
        has_rule_hit: bool,
        has_suggestion: bool,
        has_asr: bool,
        device_ids: list[str],
        snapshot_count: int = 0,
        rule_names: dict[str, str] | None = None,
        home_id: str | None = None,
    ) -> bool:
        """Insert a new meaningful event row(snapshot_count 初值 0,落盘后调 update_snapshot_count).

        Args:
            rule_names: {rule_id: rule_name} 反查 map(_persist 时从 rule_service 拿).
                None / 空 dict 视为无 rule(或 rule 已删拿不到 name).

        Returns:
            True 表示成功;False 表示异常(R8: INSERT 失败时,调用方仍要继续投递 agent turn)
        """
        try:
            sql = """
                INSERT INTO meaningful_events (
                    id, schema_version, timestamp, text, payload_json,
                    has_rule_hit, has_suggestion, has_asr,
                    snapshot_count, device_ids, rule_names, home_id,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            params = (
                event_id,
                _CURRENT_SCHEMA_VERSION,
                timestamp,
                text,
                payload_json,
                1 if has_rule_hit else 0,
                1 if has_suggestion else 0,
                1 if has_asr else 0,
                snapshot_count,
                json.dumps(device_ids, ensure_ascii=False),
                json.dumps(rule_names or {}, ensure_ascii=False),
                home_id,
                now_ms(),
            )
            with self.db_connector.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(sql, params)
                conn.commit()
            logger.debug("Meaningful event inserted: %s", event_id)
            return True
        except Exception as e:
            logger.error("Failed to insert meaningful event %s: %s", event_id, e)
            return False

    def update_snapshot_count(self, event_id: str, count: int) -> bool:
        """落盘完成后回填 snapshot_count 实际值(成功落 clip 的 device 数,0 ~ len(device_ids))."""
        try:
            sql = "UPDATE meaningful_events SET snapshot_count = ? WHERE id = ?"
            self.db_connector.execute_update(sql, (count, event_id))
            return True
        except Exception as e:
            logger.error("Failed to update snapshot_count for %s: %s", event_id, e)
            return False

    def query(
        self,
        *,
        since_ms: int = 0,
        before_ms: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Query events with time window + pagination,按 timestamp DESC 排序.

        Args:
            since_ms: 含,`timestamp >= since_ms`
            before_ms: 不含,`timestamp < before_ms`(None 表示无上界)
            limit: 每页条数
            offset: 分页偏移

        Returns:
            list of dict with all event fields(device_ids 已解析为 list[str])
        """
        try:
            conditions = ["timestamp >= ?"]
            params: list[Any] = [since_ms]
            if before_ms is not None:
                conditions.append("timestamp < ?")
                params.append(before_ms)

            where = "WHERE " + " AND ".join(conditions)
            sql = f"""
                SELECT id, schema_version, timestamp, text, payload_json,
                       has_rule_hit, has_suggestion, has_asr,
                       snapshot_count, device_ids, rule_names, home_id, created_at
                FROM meaningful_events
                {where}
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
            """
            params.extend([limit, offset])

            rows = self.db_connector.execute_query(sql, tuple(params))
            return [self._row_to_dict(row) for row in rows]

        except Exception as e:
            logger.error("Failed to query meaningful events: %s", e)
            return []

    def get_by_id(self, event_id: str) -> dict[str, Any] | None:
        """单行查询,主要给 events_service.locate_clip 用(验证 event 存在 + device_id 合法)."""
        try:
            sql = """
                SELECT id, schema_version, timestamp, text, payload_json,
                       has_rule_hit, has_suggestion, has_asr,
                       snapshot_count, device_ids, rule_names, home_id, created_at
                FROM meaningful_events
                WHERE id = ?
            """
            rows = self.db_connector.execute_query(sql, (event_id,))
            if not rows:
                return None
            return self._row_to_dict(rows[0])
        except Exception as e:
            logger.error("Failed to get meaningful event %s: %s", event_id, e)
            return None

    def delete_before_days(self, days: int) -> int:
        """删除 created_at 早于 N 天前的行(按 spec.md `event_ttl_days` 语义).

        created_at 现在是 INTEGER Unix ms (UTC 绝对时刻),cutoff 直接 int 比较。

        Returns:
            删除的行数
        """
        try:
            cutoff_ms = now_ms() - days * 86400_000
            sql = "DELETE FROM meaningful_events WHERE created_at < ?"
            return self.db_connector.execute_update(sql, (cutoff_ms,))
        except Exception as e:
            logger.error("Failed to delete old meaningful events: %s", e)
            return 0

    @staticmethod
    def _row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
        """SQLite row → dict;device_ids / rule_names JSON 反序列化;has_* INTEGER → bool."""
        # rule_names 字段可能在老库不存在(ALTER TABLE 兜底前的行历史不足)→ 用 dict access 但兜底
        try:
            rule_names_raw = row["rule_names"]
        except (KeyError, IndexError):
            rule_names_raw = None
        return {
            "id": row["id"],
            "schema_version": row["schema_version"],
            "timestamp": row["timestamp"],
            "text": row["text"],
            "payload_json": row["payload_json"],
            "has_rule_hit": bool(row["has_rule_hit"]),
            "has_suggestion": bool(row["has_suggestion"]),
            "has_asr": bool(row["has_asr"]),
            "snapshot_count": row["snapshot_count"],
            "device_ids": json.loads(row["device_ids"]) if row["device_ids"] else [],
            "rule_names": json.loads(rule_names_raw) if rule_names_raw else {},
            "home_id": row["home_id"],
            "created_at": row["created_at"],
        }
