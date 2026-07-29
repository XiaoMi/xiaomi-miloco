# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Device property history DAO — SQLite persistence for mips property pushes.

一次 `properties_changed` 推送 = N 行(每个属性条目一行,共享 ts_ms)。value 序列化为
JSON 存 value_json,bool/数值/字符串/null 都能无损回读。保留期由 DAO 在写路径顺带
清理(见 `_maybe_prune`),不依赖外部定时任务。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from miloco.database.connector import get_db_connector

logger = logging.getLogger(__name__)

# 每写入这么多行触发一次过期清理。写路径顺带清理避免独立定时器;推送频率低
# (整屋每分钟几十条),间隔内的过期行只是多占几 KB,无正确性影响。
_PRUNE_EVERY_N_INSERTS = 500


class DevicePropHistoryDao:
    """Data access object for the device_prop_history table.

    通过 manager 单例持有(`mgr.device_prop_history_dao`),禁止在调用点直接构造。
    """

    def __init__(self, retention_days: int = 30):
        self.db_connector = get_db_connector()
        self._retention_days = max(1, retention_days)
        self._inserts_since_prune = _PRUNE_EVERY_N_INSERTS  # 首写即触发一次清理

    def insert_changes(
        self, did: str, changes: list[tuple[int, int, Any]], ts_ms: int
    ) -> bool:
        """Insert one push's property entries (each = (siid, piid, value)).

        Returns True on success; False 只记 log 不抛——丢一条历史行不值得让
        推送回调链路冒泡异常。
        """
        if not changes:
            return True
        try:
            rows = [
                (did, siid, piid, json.dumps(value, ensure_ascii=False), ts_ms)
                for siid, piid, value in changes
            ]
            with self.db_connector.get_connection() as conn:
                conn.executemany(
                    "INSERT INTO device_prop_history "
                    "(did, siid, piid, value_json, ts_ms) VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
                conn.commit()
            self._inserts_since_prune += len(rows)
            self._maybe_prune()
            return True
        except Exception as e:
            logger.error("insert device_prop_history failed did=%s: %s", did, e)
            return False

    def query(
        self,
        did: str,
        *,
        siid: int | None = None,
        piid: int | None = None,
        since_ms: int | None = None,
        until_ms: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query history rows, newest first.

        siid/piid 要么都传(单属性时间线)要么都不传(整设备时间线);只传一个按
        整设备处理(piid 单独无意义)。value_json 解析失败的行原样返回字符串。
        """
        clauses = ["did = ?"]
        params: list[Any] = [did]
        if siid is not None and piid is not None:
            clauses.append("siid = ? AND piid = ?")
            params.extend([siid, piid])
        if since_ms is not None:
            clauses.append("ts_ms >= ?")
            params.append(since_ms)
        if until_ms is not None:
            clauses.append("ts_ms <= ?")
            params.append(until_ms)
        params.append(max(1, min(limit, 1000)))
        sql = (
            "SELECT did, siid, piid, value_json, ts_ms FROM device_prop_history "
            f"WHERE {' AND '.join(clauses)} ORDER BY ts_ms DESC LIMIT ?"
        )
        rows = self.db_connector.execute_query(sql, tuple(params))
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                value = json.loads(r["value_json"])
            except (TypeError, ValueError):
                value = r["value_json"]
            out.append(
                {
                    "did": r["did"],
                    "siid": r["siid"],
                    "piid": r["piid"],
                    "iid": f"prop.{r['siid']}.{r['piid']}",
                    "value": value,
                    "ts": r["ts_ms"],
                }
            )
        return out

    def _maybe_prune(self) -> None:
        if self._inserts_since_prune < _PRUNE_EVERY_N_INSERTS:
            return
        self._inserts_since_prune = 0
        try:
            from miloco.utils.time_utils import now_ms

            cutoff = now_ms() - self._retention_days * 86400_000
            deleted = self.db_connector.execute_update(
                "DELETE FROM device_prop_history WHERE ts_ms < ?", (cutoff,)
            )
            if deleted:
                logger.info(
                    "device_prop_history pruned %d rows older than %dd",
                    deleted,
                    self._retention_days,
                )
        except Exception as e:
            logger.warning("device_prop_history prune failed: %s", e)
