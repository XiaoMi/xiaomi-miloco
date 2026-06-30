"""log_pack 单测：打包内容、体量保护、LRU。

固定 storage="." (默认),workspace_dir = MILOCO_HOME 顶级。fixture 清 settings
lru_cache 让 setenv 后重新初始化,与生产环境对齐。
"""
import gzip
import sqlite3
import tarfile
from pathlib import Path

import pytest
from miloco.admin import log_pack


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path, monkeypatch):
    """每个测试隔离 MILOCO_HOME 并强制 settings 重读(避免 lru_cache 污染)。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.delenv("MILOCO_DIRECTORIES__STORAGE", raising=False)
    from miloco.config.settings import reset_settings
    reset_settings()
    yield
    reset_settings()


_EVT_ID = "evt-20260529"


def _seed_miloco_home(home: Path) -> None:
    """造完整的源数据。workspace_dir = MILOCO_HOME 顶级(storage=".")。"""
    (home / "log").mkdir(parents=True, exist_ok=True)
    (home / "snapshots" / _EVT_ID).mkdir(parents=True, exist_ok=True)
    (home / "trace" / "agent" / "20260529").mkdir(parents=True, exist_ok=True)

    # observability.db (workspace_dir 顶级)
    db = home / "observability.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()

    # omni trace (事件级,落在 snapshot_root/{event_id}/omni_trace.json.gz)
    (home / "snapshots" / _EVT_ID / "omni_trace.json.gz").write_bytes(
        gzip.compress(b'{"schema_version":1,"calls":[]}')
    )
    # agent jsonl
    (home / "trace" / "agent" / "20260529" / "run1__q.jsonl.gz").write_bytes(
        gzip.compress(b'{"r":1}\n')
    )
    # backend log (workspace_dir / "log" = MILOCO_HOME / "log")
    (home / "log" / "node_events.log").write_text("evt\n")


def test_build_log_pack_full(tmp_path, monkeypatch):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    _seed_miloco_home(tmp_path)

    result = log_pack.build_log_pack()

    pack_path = Path(result["path"])
    assert pack_path.exists()
    assert pack_path.parent == tmp_path / "packs"
    assert pack_path.name.startswith("log-pack-") and pack_path.suffix == ".gz"

    with tarfile.open(pack_path, "r:gz") as tar:
        names = tar.getnames()
    assert "observability.db" in names
    assert f"trace/omni/{_EVT_ID}/omni_trace.json.gz" in names
    assert "trace/agent/20260529/run1__q.jsonl.gz" in names
    assert "log/node_events.log" in names
    assert "metadata.json" in names

    comps = result["components"]
    assert comps["observability_db"]["present"] is True
    assert comps["trace_omni"]["present"] is True and comps["trace_omni"]["files"] == 1
    assert comps["trace_agent"]["present"] is True and comps["trace_agent"]["files"] == 1
    assert comps["backend_log"]["present"] is True
    assert "openclaw_plugin_log" not in comps
    assert result["evicted"] == []


def test_omni_trace_glob_skips_clips(tmp_path, monkeypatch):
    """snapshot_root 下同事件目录还会有 clip.mp4/m4a(~MB);log-pack 只 glob
    omni_trace.json.gz,clip 不进包,避免撞 MAX_TOTAL_BYTES。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    _seed_miloco_home(tmp_path)
    # 在同事件目录下造 clip 文件,模拟生产形态
    event_dir = tmp_path / "snapshots" / _EVT_ID
    (event_dir / "cam_a").mkdir(parents=True)
    (event_dir / "cam_a" / "clip.mp4").write_bytes(b"\x00" * 1024)

    result = log_pack.build_log_pack()

    with tarfile.open(result["path"], "r:gz") as tar:
        names = tar.getnames()
    assert f"trace/omni/{_EVT_ID}/omni_trace.json.gz" in names
    # clip 不应入包
    assert not any("clip.mp4" in n for n in names)
    assert not any("cam_a" in n for n in names)
    # comps.trace_omni.size 只计 trace 字节,不含 clip
    assert result["components"]["trace_omni"]["files"] == 1
    assert result["components"]["trace_omni"]["size"] < 1024  # 不含 1KB 的 clip


def test_build_log_pack_partial(tmp_path, monkeypatch):
    """缺组件不报错,跳过缺项。"""
    db = tmp_path / "observability.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()

    result = log_pack.build_log_pack()

    assert result["components"]["observability_db"]["present"] is True
    assert "openclaw_plugin_log" not in result["components"]
    assert result["components"]["trace_omni"]["present"] is False


def test_build_log_pack_sqlite_consistent(tmp_path, monkeypatch):
    """SQLite 在线备份: tar 内的 db 能正常打开并查询。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    _seed_miloco_home(tmp_path)

    result = log_pack.build_log_pack()
    extract_dir = tmp_path / "extract"
    extract_dir.mkdir()
    with tarfile.open(result["path"], "r:gz") as tar:
        tar.extract("observability.db", path=extract_dir)
    conn = sqlite3.connect(extract_dir / "observability.db")
    assert conn.execute("SELECT x FROM t").fetchone() == (1,)
    conn.close()


def test_build_log_pack_size_exceeded(tmp_path, monkeypatch):
    """总和 > 500MB -> 抛 LogPackSizeExceeded,异常带各组件 size。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setattr(log_pack, "MAX_TOTAL_BYTES", 100)  # 把限额拉低到 100B
    _seed_miloco_home(tmp_path)

    with pytest.raises(log_pack.LogPackSizeExceeded) as exc:
        log_pack.build_log_pack()
    info = exc.value.info
    assert info["limit_bytes"] == 100
    assert info["estimated_size_bytes"] > 100
    assert "observability_db" in info["components"]


def test_lru_keeps_only_two(tmp_path, monkeypatch):
    """连续打包 3 次 -> packs/ 只剩 2 个最新; evicted 含最旧。"""
    import time as _t
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    _seed_miloco_home(tmp_path)

    r1 = log_pack.build_log_pack()
    _t.sleep(1.1)  # 文件名秒粒度 + mtime 差异
    r2 = log_pack.build_log_pack()
    _t.sleep(1.1)
    r3 = log_pack.build_log_pack()

    packs = sorted((tmp_path / "packs").glob("log-pack-*.tar.gz"))
    assert len(packs) == 2
    assert Path(r1["path"]) not in packs
    assert Path(r2["path"]) in packs
    assert Path(r3["path"]) in packs
    assert Path(r1["path"]).as_posix() in r3["evicted"]


def test_tempfile_cleanup_on_failure(tmp_path, monkeypatch):
    """打包中途异常 -> tempdir 与 tempfile 都清理干净。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    _seed_miloco_home(tmp_path)

    def _boom(*a, **kw):
        raise RuntimeError("simulated tar error")
    monkeypatch.setattr(log_pack.tarfile, "open", _boom)

    with pytest.raises(RuntimeError):
        log_pack.build_log_pack()

    # packs 目录不存在 / 为空; 系统 tempdir 不堆积也由 TemporaryDirectory 上下文保证
    packs = tmp_path / "packs"
    assert not packs.exists() or list(packs.iterdir()) == []
