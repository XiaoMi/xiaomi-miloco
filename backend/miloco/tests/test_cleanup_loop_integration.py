# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""集成测试:_log_cleanup_loop 追加的两块清理(D3-T11).

直接跑一轮 cleanup 逻辑(不走 24h sleep),验证:
- meaningful_events.delete_before_days 被调用
- cleanup_snapshots 被调用
- 任一块失败不阻塞其它块(B9 强约束)
"""

import asyncio
import time
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

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
    """运行 _log_cleanup_loop 的一轮 body(跳过初始 60s 与末尾 86400s 等待)."""
    # 直接调 main.py 的 _log_cleanup_loop,但 patch 掉两个 sleep
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
            await main_module._log_cleanup_loop()
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

    @pytest.mark.asyncio
    async def test_obs_cleanup_owned_by_worker_thread(self, isolated_env):
        """回归护栏:obs 连接必须在工作线程内建、内关,回收也在工作线程跑。

        这是一条纯时序不变量,helper 的护栏一条都覆盖不到它(五条单测全跑在 miloco.db
        上,碰不到 _cleanup_obs_db 的时序)。三种回归都能全绿逃过 CI,只有这条能 red:
        - 把 obs_connect 挪回线程函数外(建在事件循环线程、只把语句放进 to_thread):
          关服 cancel 撞上工作线程还在 fetch 搬页的窗口时,事件循环线程会 close 掉正
          在用的连接,抛 "Cannot operate on a closed database"。
        - 把 incremental_vacuum 挪回 await 之后:把最多 40MB 的阻塞 I/O 压回事件循环。
        - 把 obs 那次 incremental_vacuum 整块删掉(它外面裹着一层 try/except,看起来
          像可选的):obs.db 从此只 DELETE 不回收,退回本 PR 要修的 bug,而且是在两个
          库里空闲页更多的那个上。
        """
        import threading

        import miloco.database.connector as connector_module
        import miloco.main as main_module
        from miloco.observability.metrics_db import connect as real_connect

        loop_tid = threading.get_ident()
        connect_tids: list[int] = []
        close_tids: list[int] = []
        obs_vacuum_tids: list[int] = []
        miloco_vacuum_tids: list[int] = []

        # sqlite3.Connection 是 C 类型、无 __dict__,不能直接给实例赋 close,
        # 用薄代理转发、只在 close 上挂记录。
        class _TrackingConn:
            def __init__(self, conn):
                self._conn = conn

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def close(self):
                close_tids.append(threading.get_ident())
                self._conn.close()

        def _tracking_connect(path):
            connect_tids.append(threading.get_ident())
            return _TrackingConn(real_connect(path))

        real_vacuum = connector_module.incremental_vacuum

        def _tracking_vacuum(conn, *args, **kwargs):
            # 一轮 loop body 里 incremental_vacuum 被调两次(obs 一次、miloco.db 一次)。
            # 共用一个桶时,obs 那次被整块删掉后 miloco 那次仍能让断言全绿,护栏形同
            # 虚设 —— obs 连接是 _TrackingConn、miloco 的是裸 sqlite3.Connection,
            # 按这个天然判别标记分桶,两个调用点各断各的。
            bucket = (
                obs_vacuum_tids
                if isinstance(conn, _TrackingConn)
                else miloco_vacuum_tids
            )
            bucket.append(threading.get_ident())
            return real_vacuum(conn, *args, **kwargs)

        # incremental_vacuum 在 main.py 是函数内 import,patch 到 connector 模块属性上。
        with (
            patch.object(main_module, "obs_connect", side_effect=_tracking_connect),
            patch.object(
                connector_module, "incremental_vacuum", side_effect=_tracking_vacuum
            ),
        ):
            await _run_one_cycle()

        assert connect_tids, "obs cleanup 整段没跑到(perf.enabled 被关?)"
        assert loop_tid not in connect_tids, (
            f"obs 连接建在事件循环线程上了: {connect_tids} vs loop {loop_tid}"
        )
        assert connect_tids == close_tids, (
            f"建连/关连不在同一线程: connect={connect_tids} close={close_tids}"
        )
        assert obs_vacuum_tids, "obs 回收没跑到(incremental_vacuum 调用被删?)"
        assert loop_tid not in obs_vacuum_tids, (
            f"obs 回收跑在事件循环线程上了: {obs_vacuum_tids} vs loop {loop_tid}"
        )
        assert obs_vacuum_tids == connect_tids, (
            f"obs 回收不在建连的那个线程上: vacuum={obs_vacuum_tids} connect={connect_tids}"
        )
        assert miloco_vacuum_tids and loop_tid not in miloco_vacuum_tids, (
            f"miloco.db 回收跑在事件循环线程上了: {miloco_vacuum_tids} vs loop {loop_tid}"
        )
