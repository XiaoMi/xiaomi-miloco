# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""集成测试:_daily_maintenance_loop 追加的两块清理(D3-T11).

直接跑一轮 cleanup 逻辑(不走 24h sleep),验证:
- meaningful_events.delete_before_days 被调用
- cleanup_snapshots 被调用
- 任一块失败不阻塞其它块(B9 强约束)
"""

import asyncio
import time
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))

    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module
    import miloco.manager as manager_module

    connector_module.db_connector = None
    connector_module.init_database()
    manager_module.Manager._instance = None
    manager_module.manager_instance = None

    yield tmp_path

    manager_module.Manager._instance = None
    manager_module.manager_instance = None
    connector_module.db_connector = None
    reset_settings()


async def _run_one_cycle():
    """运行 _daily_maintenance_loop 的一轮 body(跳过初始 60s 与末尾 86400s 等待)."""
    # 直接调 main.py 的 _daily_maintenance_loop,但 patch 掉两个 sleep
    from miloco import main as main_module

    real_sleep = asyncio.sleep
    call_count = [0]

    async def _short_sleep(secs):
        call_count[0] += 1
        if call_count[0] >= 2:
            # 第二次 sleep(末尾 86400)→ 抛 CancelledError 退出 while True
            raise asyncio.CancelledError()
        # 第一次 sleep(开头 60)→ 立即返回
        await real_sleep(0)

    with patch.object(asyncio, "sleep", side_effect=_short_sleep):
        try:
            await main_module._daily_maintenance_loop()
        except asyncio.CancelledError:
            pass


class TestCleanupLoop:
    @pytest.mark.asyncio
    async def test_deletes_old_meaningful_events(self, isolated_env):
        """旧 event(created_at > event_ttl_days)被删除."""
        import sqlite3

        from miloco.manager import get_manager

        dao = get_manager().meaningful_events_dao
        # 插一条新的 + 一条旧的(手改 created_at 31 天前)
        eid_new = str(uuid.uuid4())
        eid_old = str(uuid.uuid4())
        for eid in (eid_new, eid_old):
            dao.insert(
                event_id=eid,
                timestamp=int(time.time() * 1000),
                text="t",
                payload_json="{}",
                has_rule_hit=True,
                has_suggestion=False,
                has_asr=False,
                device_ids=["cam_a"],
            )
        # v10 起 created_at 是 INTEGER ms,直接传 ms
        old_ms = int((datetime.now() - timedelta(days=31)).timestamp() * 1000)
        conn = sqlite3.connect(str(isolated_env / "test.db"))
        try:
            conn.execute(
                "UPDATE meaningful_events SET created_at=? WHERE id=?",
                (old_ms, eid_old),
            )
            conn.commit()
        finally:
            conn.close()

        await _run_one_cycle()

        # 旧的被删,新的还在
        assert dao.get_by_id(eid_old) is None
        assert dao.get_by_id(eid_new) is not None

    @pytest.mark.asyncio
    async def test_calls_cleanup_snapshots(self, isolated_env):
        """cleanup_snapshots 被调用(验证函数名 patch)."""
        with patch(
            "miloco.perception.snapshot_writer.cleanup_snapshots"
        ) as mock_clean:
            mock_clean.return_value = {
                "deleted_by_ttl": 0,
                "deleted_by_lru": 0,
                "remaining_mb": 0,
            }
            await _run_one_cycle()
            assert mock_clean.called

    @pytest.mark.asyncio
    async def test_meaningful_events_failure_does_not_block_snapshots(self, isolated_env):
        """B9:meaningful_events 清理抛异常 → snapshots 清理仍执行."""
        from miloco.manager import get_manager

        dao = get_manager().meaningful_events_dao
        with (
            patch.object(
                dao, "delete_before_days", side_effect=RuntimeError("db down")
            ),
            patch(
                "miloco.perception.snapshot_writer.cleanup_snapshots",
                return_value={
                    "deleted_by_ttl": 0,
                    "deleted_by_lru": 0,
                    "remaining_mb": 0,
                },
            ) as mock_clean,
        ):
            # 不应抛
            await _run_one_cycle()
            # 即使前面失败,snapshots 清理仍被调用
            assert mock_clean.called

    @pytest.mark.asyncio
    async def test_snapshots_failure_does_not_block_loop(self, isolated_env):
        """B9:snapshots 清理抛异常 → 循环不崩(虽然只有一轮,验证不冒泡)."""
        with patch(
            "miloco.perception.snapshot_writer.cleanup_snapshots",
            side_effect=OSError("fs error"),
        ):
            # 不应抛 OSError
            await _run_one_cycle()

    def test_trim_malloc_arenas_noop_when_symbol_unavailable(self):
        """非 glibc(符号缺失):_trim_malloc_arenas 返回 None、不抛."""
        from miloco import main as main_module

        with patch.object(main_module, "_malloc_trim", None):
            assert main_module._trim_malloc_arenas() is None

    @pytest.mark.asyncio
    async def test_malloc_trim_failure_does_not_block_loop(self, isolated_env):
        """B9:malloc_trim 抛异常 → 维护循环不崩(异常被吞、不冒泡)."""
        from miloco import main as main_module

        boom = MagicMock(side_effect=RuntimeError("trim boom"))
        with patch.object(main_module, "_malloc_trim", boom):
            # 不应抛 RuntimeError
            await _run_one_cycle()
            assert boom.called
