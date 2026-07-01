# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Rule data access object
Handles CRUD operations for rule and rule_log tables
"""

import json
import logging
import sqlite3
import uuid
from datetime import datetime
from typing import Any

from miloco.database.connector import get_db_connector
from miloco.rule.schema import (
    Rule,
    RuleAction,
    RuleCondition,
    RuleExecuteResult,
    RuleLifecycle,
    RuleLog,
    RuleLogKind,
    RuleMode,
)
from miloco.utils.time_utils import ms_to_iso_local, now_ms

logger = logging.getLogger(__name__)

# DB DEFAULT 是历史值 0.8(connector.py:219/425 ALTER + CREATE DDL),与应用层
# 当前默认 settings.rule.default_duration_ratio=0.6 不同——刻意保留:
# (1) 正常路径 service._fill_default_duration_ratio 已经按 settings 回填,
#     DB DEFAULT 实际只在"绕过 service 直写 repo"时(测试 / 维护脚本)被命中;
# (2) 改 DB DEFAULT 需要给已存在的 rule 表加 migration(ALTER 改列默认 SQLite 不支持,
#     得重建表) → 引入风险但用户感知零,与"DB migration 必须幂等"原则相悖。
# 本常量与 DDL 同值是为了避免 IntegrityError;改 DDL 时必须同步改这里。
_DURATION_RATIO_DB_FALLBACK = 0.8


class RuleRepo:
    """Rule data access object"""

    def __init__(self):
        self.db_connector = get_db_connector()

    def _dict_to_rule(self, data: dict[str, Any]) -> Rule:
        """Convert database row to Rule object (V3 schema)."""
        condition_data = json.loads(data["condition"]) if data.get("condition") else {}
        condition = RuleCondition(**condition_data)

        def _load_actions(col: str) -> list[RuleAction]:
            raw = data.get(col)
            if not raw:
                return []
            return [RuleAction(**a) for a in json.loads(raw)]

        action_descriptions = (
            json.loads(data["action_descriptions"])
            if data.get("action_descriptions")
            else []
        )

        return Rule(
            id=data["id"],
            name=data["name"],
            task_id=data["task_id"] if data.get("task_id") is not None else "",
            mode=RuleMode(data.get("mode") or RuleMode.EVENT.value),
            lifecycle=RuleLifecycle(
                data.get("lifecycle") or RuleLifecycle.PERMANENT.value
            ),
            enabled=bool(data["enabled"]),
            condition=condition,
            actions=_load_actions("actions"),
            action_descriptions=action_descriptions,
            on_enter_actions=_load_actions("on_enter_actions"),
            on_enter_desc=data.get("on_enter_desc"),
            on_exit_actions=_load_actions("on_exit_actions"),
            on_exit_desc=data.get("on_exit_desc"),
            on_target_desc=data.get("on_target_desc"),
            terminate_when=data.get("terminate_when"),
            exit_debounce_seconds=int(data.get("exit_debounce_seconds") or 60),
            duration_seconds=(
                int(data["duration_seconds"])
                if data.get("duration_seconds") is not None
                else None
            ),
            duration_ratio=(
                float(data["duration_ratio"])
                if data.get("duration_ratio") is not None
                else 0.8
            ),
            created_at=ms_to_iso_local(data.get("created_at")),
            updated_at=ms_to_iso_local(data.get("updated_at")),
        )

    def create(self, rule: Rule) -> str | None:
        """Create a new rule + auto-link task_link(kind='rule') in single transaction.

        方案 P 关键改造（spec §7.3）：``INSERT rule`` 与 ``INSERT task_link``
        在同一笔 SQL 事务内完成；任一失败 ``ROLLBACK``，避免孤儿 rule / 孤儿
        task_link。``rule.task_id`` 不存在时由 task_link FK 兜底（service 层
        会在调用前先 404 检查）。
        """
        try:
            rule_id = str(uuid.uuid4())
            current_time = now_ms()

            condition_json = rule.condition.model_dump(mode="json")
            sql = """
                INSERT INTO rule (
                    id, name, task_id, mode, lifecycle, enabled,
                    condition, actions, action_descriptions,
                    on_enter_actions, on_enter_desc,
                    on_exit_actions, on_exit_desc,
                    on_target_desc,
                    terminate_when, exit_debounce_seconds,
                    duration_seconds, duration_ratio,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            params = (
                rule_id,
                rule.name,
                rule.task_id,
                rule.mode.value,
                rule.lifecycle.value,
                rule.enabled,
                json.dumps(condition_json),
                json.dumps([a.model_dump(mode="json") for a in rule.actions]),
                json.dumps(rule.action_descriptions),
                json.dumps(
                    [a.model_dump(mode="json") for a in rule.on_enter_actions]
                ),
                rule.on_enter_desc,
                json.dumps(
                    [a.model_dump(mode="json") for a in rule.on_exit_actions]
                ),
                rule.on_exit_desc,
                rule.on_target_desc,
                rule.terminate_when,
                rule.exit_debounce_seconds,
                rule.duration_seconds,
                rule.duration_ratio
                if rule.duration_ratio is not None
                else _DURATION_RATIO_DB_FALLBACK,
                current_time,
                current_time,
            )

            with self.db_connector.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("BEGIN")
                try:
                    cursor.execute(sql, params)
                    if rule.task_id:
                        cursor.execute(
                            "INSERT INTO task_link "
                            "(task_id, link_kind, link_ref) VALUES (?, 'rule', ?)",
                            (rule.task_id, rule_id),
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

            on_enter = (
                "static" if rule.on_enter_actions
                else "dynamic" if rule.on_enter_desc else "none"
            )
            on_exit = (
                "static" if rule.on_exit_actions
                else "dynamic" if rule.on_exit_desc else "none"
            )
            logger.info(
                "Rule created: id=%s name=%s task_id=%s mode=%s lifecycle=%s "
                "duration_seconds=%s duration_ratio=%s exit_debounce_seconds=%s "
                "on_enter=%s on_exit=%s terminate_when=%s",
                rule_id, rule.name, rule.task_id, rule.mode.value,
                rule.lifecycle.value, rule.duration_seconds, rule.duration_ratio,
                rule.exit_debounce_seconds, on_enter, on_exit,
                bool(rule.terminate_when),
            )
            return rule_id

        except (ValueError, TypeError, KeyError, AttributeError) as e:
            logger.error(
                "Error creating rule: name=%s, error=%s", rule.name, e, exc_info=True
            )
            return None

    def get_by_id(self, rule_id: str) -> Rule | None:
        """Get rule by ID"""
        try:
            sql = "SELECT * FROM rule WHERE id = ?"
            results = self.db_connector.execute_query(sql, (rule_id,))
            if results:
                return self._dict_to_rule(results[0])
            return None
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            logger.error("Error querying rule: id=%s, error=%s", rule_id, e)
            return None

    def get_by_name(self, name: str) -> Rule | None:
        """Get rule by name"""
        try:
            sql = "SELECT * FROM rule WHERE name = ?"
            results = self.db_connector.execute_query(sql, (name,))
            if results:
                return self._dict_to_rule(results[0])
            return None
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            logger.error("Error querying rule: name=%s, error=%s", name, e)
            return None

    def get_all(self, enabled_only: bool = False) -> list[Rule]:
        """Get all rules, optionally filtered by enabled status.

        A single corrupted row (e.g. unknown enum value after a future schema
        change) is logged and skipped rather than failing the whole query, so
        backend startup (init_rule_service → get_all) survives partial damage.
        """
        try:
            if enabled_only:
                sql = "SELECT * FROM rule WHERE enabled = 1 ORDER BY created_at DESC"
            else:
                sql = "SELECT * FROM rule ORDER BY created_at DESC"
            results = self.db_connector.execute_query(sql)
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            logger.error("Error retrieving rules: error=%s", e)
            raise e

        rules: list[Rule] = []
        for row in results:
            try:
                rules.append(self._dict_to_rule(row))
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "Skipping corrupted rule row id=%s: %s",
                    row.get("id"),
                    e,
                )
        logger.debug("Retrieved %s rules", len(rules))
        return rules

    def update(self, rule: Rule) -> bool:
        """Full update of a rule.

        rule 表是 task 归属的 SSOT，task_link(kind='rule') 行是冗余索引，
        task_id 变更时须一笔事务同步，避免 `task get` 与 `rule get` 视图分裂。
        task_link 用 UPSERT 兜住历史遗留孤儿（rule 存在但对应 task_link 缺失）
        场景——迁移或补建都在同一条 SQL 里完成。
        """
        try:
            condition_json = rule.condition.model_dump(mode="json")
            sql = """
                UPDATE rule
                SET name = ?, task_id = ?, mode = ?, lifecycle = ?,
                    enabled = ?, condition = ?, actions = ?, action_descriptions = ?,
                    on_enter_actions = ?, on_enter_desc = ?,
                    on_exit_actions = ?, on_exit_desc = ?,
                    on_target_desc = ?,
                    terminate_when = ?, exit_debounce_seconds = ?,
                    duration_seconds = ?, duration_ratio = ?,
                    updated_at = ?
                WHERE id = ?
            """
            params = (
                rule.name,
                rule.task_id,
                rule.mode.value,
                rule.lifecycle.value,
                rule.enabled,
                json.dumps(condition_json),
                json.dumps([a.model_dump(mode="json") for a in rule.actions]),
                json.dumps(rule.action_descriptions),
                json.dumps(
                    [a.model_dump(mode="json") for a in rule.on_enter_actions]
                ),
                rule.on_enter_desc,
                json.dumps(
                    [a.model_dump(mode="json") for a in rule.on_exit_actions]
                ),
                rule.on_exit_desc,
                rule.on_target_desc,
                rule.terminate_when,
                rule.exit_debounce_seconds,
                rule.duration_seconds,
                rule.duration_ratio
                if rule.duration_ratio is not None
                else _DURATION_RATIO_DB_FALLBACK,
                now_ms(),
                rule.id,
            )

            with self.db_connector.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("BEGIN")
                try:
                    cursor.execute(sql, params)
                    affected = cursor.rowcount
                    if affected > 0 and rule.task_id:
                        cursor.execute(
                            "INSERT INTO task_link "
                            "(task_id, link_kind, link_ref) "
                            "VALUES (?, 'rule', ?) "
                            "ON CONFLICT(link_kind, link_ref) "
                            "DO UPDATE SET task_id = excluded.task_id",
                            (rule.task_id, rule.id),
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

            if affected > 0:
                logger.info("Rule updated: id=%s", rule.id)
                return True
            logger.warning("Rule not found for update: id=%s", rule.id)
            return False
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            sqlite3.IntegrityError,
        ) as e:
            logger.error("Error updating rule: id=%s, error=%s", rule.id, e)
            return False

    def delete(self, rule_id: str) -> bool:
        """Delete a rule by ID（own connection 版本）。"""
        try:
            sql = "DELETE FROM rule WHERE id = ?"
            affected = self.db_connector.execute_update(sql, (rule_id,))
            if affected > 0:
                logger.info("Rule deleted: id=%s", rule_id)
                return True
            logger.warning("Rule not found for deletion: id=%s", rule_id)
            return False
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            logger.error("Error deleting rule: id=%s, error=%s", rule_id, e)
            return False

    @staticmethod
    def delete_in_tx(cursor, rule_id: str) -> bool:
        """外层事务版本：用 caller 提供的 cursor 删 rule，不 own connection。"""
        cursor.execute("DELETE FROM rule WHERE id = ?", (rule_id,))
        return cursor.rowcount > 0

    def exists(self, rule_id: str) -> bool:
        """Check if a rule exists"""
        try:
            sql = "SELECT COUNT(*) as count FROM rule WHERE id = ?"
            results = self.db_connector.execute_query(sql, (rule_id,))
            return bool(results and results[0]["count"] > 0)
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            logger.error("Error checking rule existence: id=%s, error=%s", rule_id, e)
            return False

    def exists_by_name(self, name: str, exclude_id: str | None = None) -> bool:
        """Check if a rule with the given name exists (optionally excluding an ID)"""
        try:
            if exclude_id is not None:
                sql = "SELECT COUNT(*) as count FROM rule WHERE name = ? AND id != ?"
                params = (name, exclude_id)
            else:
                sql = "SELECT COUNT(*) as count FROM rule WHERE name = ?"
                params = (name,)
            results = self.db_connector.execute_query(sql, params)
            return bool(results and results[0]["count"] > 0)
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            logger.error("Error checking rule name: name=%s, error=%s", name, e)
            return False

    def count_all(self) -> int:
        """Get total rule count"""
        try:
            sql = "SELECT COUNT(*) as count FROM rule"
            results = self.db_connector.execute_query(sql)
            return results[0]["count"] if results else 0
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            logger.error("Error counting rules: error=%s", e)
            return 0

    def count_enabled(self) -> int:
        """Get enabled rule count"""
        try:
            sql = "SELECT COUNT(*) as count FROM rule WHERE enabled = 1"
            results = self.db_connector.execute_query(sql)
            return results[0]["count"] if results else 0
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            logger.error("Error counting enabled rules: error=%s", e)
            return 0


class RuleLogRepo:
    """Rule log data access object"""

    def __init__(self):
        self.db_connector = get_db_connector()

    def _dict_to_log(self, data: dict[str, Any]) -> RuleLog:
        """Convert database row to RuleLog object (V3 schema)."""
        execute_result = None
        if data.get("execute_result"):
            execute_result_data = json.loads(data["execute_result"])
            execute_result = RuleExecuteResult(**execute_result_data)

        kind_raw = data.get("kind") or RuleLogKind.RULE_TRIGGER_SUCCESS.value

        return RuleLog(
            id=data["id"],
            timestamp=data["timestamp"],
            kind=RuleLogKind(kind_raw),
            rule_id=data["rule_id"],
            rule_name=data["rule_name"],
            rule_query=data["rule_query"],
            trigger_context=data.get("trigger_context", ""),
            execute_result=execute_result,
        )

    def create(self, log: RuleLog) -> str | None:
        """Create a new log entry. Returns ID or None on failure."""
        try:
            if log.id is None:
                log.id = str(uuid.uuid4())

            sql = """
                INSERT INTO rule_log (
                    id, timestamp, kind, rule_id, rule_name, rule_query,
                    trigger_context, execute_result, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            params = (
                log.id,
                log.timestamp,
                log.kind.value,
                log.rule_id,
                log.rule_name,
                log.rule_query,
                log.trigger_context,
                json.dumps(log.execute_result.model_dump(mode="json"))
                if log.execute_result
                else None,
                now_ms(),
            )

            with self.db_connector.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(sql, params)
                conn.commit()

            logger.info("Rule log created: id=%s, rule_id=%s", log.id, log.rule_id)
            return log.id

        except (ValueError, TypeError, KeyError, AttributeError) as e:
            logger.error(
                "Error creating rule log: rule_id=%s, error=%s", log.rule_id, e
            )
            return None

    def get_all(
        self,
        limit: int | None = None,
        offset: int | None = None,
        after_ts: int | None = None,
        before_ts: int | None = None,
        kind: RuleLogKind | None = None,
    ) -> list[RuleLog]:
        """Get all logs with optional pagination, time and kind filtering.

        Args:
            limit: Max number of logs to return.
            offset: Pagination offset.
            after_ts: Only return logs with timestamp > after_ts (millisecond Unix).
            before_ts: Only return logs with timestamp < before_ts (used by
                cursor-paging clients to fetch the next older page).
            kind: Only return logs of the given kind.
        """
        try:
            clauses: list[str] = []
            params_list: list[int | str] = []

            if after_ts is not None:
                clauses.append("timestamp > ?")
                params_list.append(after_ts)
            if before_ts is not None:
                clauses.append("timestamp < ?")
                params_list.append(before_ts)
            if kind is not None:
                clauses.append("kind = ?")
                params_list.append(kind.value)

            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            sql = f"SELECT * FROM rule_log {where} ORDER BY timestamp DESC"

            if limit and offset is not None:
                sql += " LIMIT ? OFFSET ?"
                params_list.extend([limit, offset])
            elif limit:
                sql += " LIMIT ?"
                params_list.append(limit)

            results = self.db_connector.execute_query(sql, tuple(params_list))
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            logger.error("Error retrieving rule logs: error=%s", e)
            return []

        logs: list[RuleLog] = []
        for row in results:
            try:
                logs.append(self._dict_to_log(row))
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "Skipping corrupted rule_log row id=%s: %s",
                    row.get("id"),
                    e,
                )
        return logs

    def get_by_rule_id(
        self,
        rule_id: str,
        limit: int | None = None,
        after_ts: int | None = None,
        before_ts: int | None = None,
        kind: RuleLogKind | None = None,
    ) -> list[RuleLog]:
        """Get logs for a specific rule with optional time / kind filtering."""
        try:
            clauses = ["rule_id = ?"]
            params_list: list[int | str] = [rule_id]

            if after_ts is not None:
                clauses.append("timestamp > ?")
                params_list.append(after_ts)
            if before_ts is not None:
                clauses.append("timestamp < ?")
                params_list.append(before_ts)
            if kind is not None:
                clauses.append("kind = ?")
                params_list.append(kind.value)

            where = f"WHERE {' AND '.join(clauses)}"
            sql = f"SELECT * FROM rule_log {where} ORDER BY timestamp DESC"

            if limit:
                sql += " LIMIT ?"
                params_list.append(limit)

            results = self.db_connector.execute_query(sql, tuple(params_list))
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            logger.error("Error retrieving rule logs: rule_id=%s, error=%s", rule_id, e)
            return []

        logs: list[RuleLog] = []
        for row in results:
            try:
                logs.append(self._dict_to_log(row))
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "Skipping corrupted rule_log row id=%s: %s",
                    row.get("id"),
                    e,
                )
        return logs

    def count_all(
        self,
        after_ts: int | None = None,
        before_ts: int | None = None,
        kind: RuleLogKind | None = None,
    ) -> int:
        """Total log count, optionally filtered by after_ts / before_ts / kind."""
        try:
            clauses: list[str] = []
            params_list: list[int | str] = []
            if after_ts is not None:
                clauses.append("timestamp > ?")
                params_list.append(after_ts)
            if before_ts is not None:
                clauses.append("timestamp < ?")
                params_list.append(before_ts)
            if kind is not None:
                clauses.append("kind = ?")
                params_list.append(kind.value)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            sql = f"SELECT COUNT(*) as count FROM rule_log {where}"
            results = self.db_connector.execute_query(sql, tuple(params_list))
            return results[0]["count"] if results else 0
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            logger.error("Error counting rule logs: error=%s", e)
            return 0

    def count_by_rule_id(
        self,
        rule_id: str,
        after_ts: int | None = None,
        before_ts: int | None = None,
        kind: RuleLogKind | None = None,
    ) -> int:
        """Log count for a specific rule, optionally filtered by after_ts / before_ts / kind."""
        try:
            clauses = ["rule_id = ?"]
            params_list: list[int | str] = [rule_id]
            if after_ts is not None:
                clauses.append("timestamp > ?")
                params_list.append(after_ts)
            if before_ts is not None:
                clauses.append("timestamp < ?")
                params_list.append(before_ts)
            if kind is not None:
                clauses.append("kind = ?")
                params_list.append(kind.value)
            where = f"WHERE {' AND '.join(clauses)}"
            sql = f"SELECT COUNT(*) as count FROM rule_log {where}"
            results = self.db_connector.execute_query(sql, tuple(params_list))
            return results[0]["count"] if results else 0
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            logger.error("Error counting rule logs: rule_id=%s, error=%s", rule_id, e)
            return 0

    def delete_by_id(self, log_id: str) -> bool:
        """Delete a log entry by ID"""
        try:
            sql = "DELETE FROM rule_log WHERE id = ?"
            affected = self.db_connector.execute_update(sql, (log_id,))
            if affected > 0:
                logger.info("Rule log deleted: id=%s", log_id)
                return True
            return False
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            logger.error("Error deleting rule log: id=%s, error=%s", log_id, e)
            return False

    def delete_by_rule_id(self, rule_id: str) -> bool:
        """Delete all logs for a given rule"""
        try:
            sql = "DELETE FROM rule_log WHERE rule_id = ?"
            affected = self.db_connector.execute_update(sql, (rule_id,))
            if affected > 0:
                logger.info(
                    "Rule logs deleted for rule_id=%s, count=%s", rule_id, affected
                )
                return True
            return False
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            logger.error("Error deleting rule logs: rule_id=%s, error=%s", rule_id, e)
            return False

    def delete_before_days(self, keep_days: int) -> int:
        """Delete logs older than keep_days"""
        try:
            cutoff_timestamp = int(
                (datetime.now().timestamp() - keep_days * 24 * 3600) * 1000
            )
            sql = "DELETE FROM rule_log WHERE timestamp < ?"
            affected = self.db_connector.execute_update(sql, (cutoff_timestamp,))
            logger.info(
                "Deleted %d rule log records older than %d days", affected, keep_days
            )
            return affected
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            logger.error("Error deleting rule logs before %d days: %s", keep_days, e)
            return 0

    def update_execute_result(
        self, log_id: str, execute_result: RuleExecuteResult
    ) -> bool:
        """Update execute_result for a log entry"""
        try:
            sql = "UPDATE rule_log SET execute_result = ? WHERE id = ?"
            params = (json.dumps(execute_result.model_dump(mode="json")), log_id)
            affected = self.db_connector.execute_update(sql, params)
            if affected > 0:
                logger.info("Rule log execute_result updated: id=%s", log_id)
                return True
            return False
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            logger.error("Error updating rule log: id=%s, error=%s", log_id, e)
            return False
