"""Hermes 插件集成测试——替换已删除的 test_install_e2e.sh。

测试全链路：配置写入 → 适配器加载 → send_turn → trace 读写。
不依赖真实 Hermes daemon。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

# ── 配置写入 / 读取 ─────────────────────────────────────────────────────────

def test_config_write_and_read(tmp_path, monkeypatch):
    """模拟 install-hermes.sh 写 config.json + 验证。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))

    config = {
        "agent": {
            "platform": "hermes",
            "webhook_url": "http://127.0.0.1:1810/miloco/webhook",
            "auth_bearer": "test-bearer-abc123",
        },
        "omni": {"model": "test-model"},
        "server": {"port": 1810},
    }
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")

    # 读回验证
    loaded = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert loaded["agent"]["platform"] == "hermes"
    assert loaded["agent"]["webhook_url"].endswith("/miloco/webhook")
    assert loaded["agent"]["auth_bearer"] == "test-bearer-abc123"


# ── 4.7 写 directories.models：把脚本里那段真代码抠出来跑 ────────────────────

_INSTALLER = Path(__file__).resolve().parents[1] / "install-hermes.sh"
_PY_HEREDOC = re.compile(r"<<'PY'[^\n]*\n(.*?)\n^PY$", re.S | re.M)


def _models_config_snippet() -> str:
    """抠出 install-hermes.sh 里 4.7 那段写 `directories.models` 的 Python。

    上面那条 `test_config_write_and_read` 是**手写**一份 JSON 再读回来，它证明的是
    "json 模块能用"——脚本里那段真代码写成什么样它都绿。这次修掉的 bug（无条件覆盖
    用户配好的 `directories.models`）正是它接不住的形状：把用户放外置盘 / 多 worktree
    共享的模型目录顶成默认值，而本脚本抬头写的是"幂等：再跑一次不会破坏现有配置"。
    所以这里不复述逻辑，直接跑那段字节——复述一遍的测试跟着一起改就一起错。
    """
    # 认 `cfg.pop("models"` 而不是 `"directories"`：后者在"新建 config.json"那段模板里
    # 也出现（`"directories": {}`），会一口气命中两段。也刻意不认 `d.get("models")` ——
    # 那正是本次要钉的那个判空分支，拿它当路标的话，"分支被删掉"会表现为**抠不出来**
    # 而不是下面几条断言变红，等于用一条"找不到代码"的报错替掉了"行为错了"的报错。
    blocks = [
        b
        for b in _PY_HEREDOC.findall(_INSTALLER.read_text(encoding="utf-8"))
        if 'cfg.pop("models"' in b
    ]
    assert len(blocks) == 1, f"抠不出唯一那段（命中 {len(blocks)} 段）；脚本改了就同步改这里的判据"
    return blocks[0]


def _run_snippet(home: Path) -> str:
    """按脚本里的调用方式跑：`"$PYTHON" - "$MILOCO_HOME"`，argv[1] 是 home。"""
    r = subprocess.run(
        [sys.executable, "-c", _models_config_snippet(), str(home)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_models_dir_written_when_absent(tmp_path):
    """没配过的人：填上默认值，且不碰同级的其它 directories.* 子键。"""
    (tmp_path / "config.json").write_text(
        json.dumps({"directories": {"static": "/opt/static"}, "server": {"port": 1810}}),
        encoding="utf-8",
    )
    out = _run_snippet(tmp_path)

    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert cfg["directories"]["models"] == f"{tmp_path}/models"
    assert cfg["directories"]["static"] == "/opt/static", "同级子键被整段盖掉了"
    assert cfg["server"]["port"] == 1810
    assert "directories.models" in out


def test_models_dir_preserved_and_mismatch_reported(tmp_path):
    """配过的人：路径原样保留，并且必须把"模型其实下到了别处"说出来。

    只保留不提示同样不合格——模型这一步落在 `$MILOCO_HOME/models`（`models_ready` 判的、
    下载器 `--dest` 给的都是它，与配置无关），感知运行时读的却是这个键。两处不一致时屏幕上
    不同时出现这两条，用户拿到的就是"安装成功"加上首次 perceive 的 `models_missing`，
    中间没有一个字解释为什么。
    """
    (tmp_path / "config.json").write_text(
        json.dumps({"directories": {"models": "/mnt/big/models"}}), encoding="utf-8"
    )
    out = _run_snippet(tmp_path)

    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert cfg["directories"]["models"] == "/mnt/big/models", "用户配好的模型目录被顶掉了"
    assert "/mnt/big/models" in out and f"{tmp_path}/models" in out, f"两个目录没同时印出来：{out!r}"


def test_relative_models_dir_is_not_a_false_mismatch(tmp_path):
    """相对路径 "models" 与默认同址，不该报"两个目录"。

    解析口径抄 `DirectorySettings.models_dir`：绝对路径直接用，相对路径相对 `$MILOCO_HOME`。
    照字符串比就会在这里误报，而误报的提示是"把模型放到 models 或清空该键"——用户照做
    只会更糊涂。
    """
    (tmp_path / "config.json").write_text(
        json.dumps({"directories": {"models": "models"}}), encoding="utf-8"
    )
    out = _run_snippet(tmp_path)

    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert cfg["directories"]["models"] == "models"
    assert "注意" not in out, f"相对路径被误报成两个目录：{out!r}"


def test_dead_toplevel_models_key_cleaned(tmp_path):
    """主线版本写的顶层 `models` 死键要清掉；`directories` 整个缺失时要建出来。

    顶层键在 `MilocoSettings`（`extra="ignore"`）下连报错都没有，静默丢弃、全仓无消费者，
    但它长得就像"模型目录在这儿配"——改了没反应最难自查。
    """
    (tmp_path / "config.json").write_text(
        json.dumps({"models": "/old/dead/key", "agent": {"platform": "hermes"}}),
        encoding="utf-8",
    )
    _run_snippet(tmp_path)

    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert "models" not in cfg
    assert cfg["directories"]["models"] == f"{tmp_path}/models"
    assert cfg["agent"]["platform"] == "hermes"


# ── 适配器加载（用 importlib 模拟 backend loader） ──────────────────────────

def test_hermes_adapter_module_loads():
    """HermesAdapter 模块可正常 import。"""
    from miloco_plugin_pkg.hermes_adapter import adapter as ha

    assert hasattr(ha, "Adapter")
    assert hasattr(ha.Adapter, "name")
    assert hasattr(ha.Adapter, "send_turn")
    assert hasattr(ha.Adapter, "read_trace_meta")
    assert hasattr(ha.Adapter, "build_system")
    assert ha.Adapter.name == "hermes"


def test_hermes_adapter_instantiable():
    from miloco_plugin_pkg.hermes_adapter import adapter as ha
    inst = ha.Adapter()
    assert inst.name == "hermes"


# ── trace 读写全链路（文件 IPC） ────────────────────────────────────────────

def test_trace_full_write_read_cycle(tmp_path, monkeypatch):
    """trace.py 常写 → 读取，验证文件 IPC 链路。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    from miloco_plugin_pkg import trace as tr

    tr._turns.clear()
    tr._trace_links.clear()

    sess = "miloco:test-ipc"
    tr.register_trace_link(sess, "trace-abc")
    tr._hk_pre_llm_call(sess, "hello world", [], True, "m", "p")
    tr._hk_post_llm_call(sess, "hi", "resp", [], "m", "p", duration_ms=100)
    tr._hk_on_session_end(sess, True, False, "m", "p")

    today_dirs = list((tmp_path / "trace" / "agent").glob("*"))
    assert len(today_dirs) == 1
    meta_files = list(today_dirs[0].glob("*.meta.json"))
    assert len(meta_files) == 1, f"应该写 meta.json: {list(today_dirs[0].iterdir())}"

    meta = json.loads(meta_files[0].read_text(encoding="utf-8"))
    assert meta["run_id"] == sess
    assert meta["trace_id"] == "trace-abc"
    assert meta["query"] == "hello world"
    assert meta["success"] is True
    assert "jsonl_path" in meta
    assert meta["jsonl_path"] is not None

    gz_files = list(today_dirs[0].glob("*.jsonl.gz"))
    assert len(gz_files) == 1


def test_trace_pop_done_turn_gives_meta(tmp_path, monkeypatch):
    """pop_done_turn 返回完整 meta 给 backend adapter 读。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    from miloco_plugin_pkg import trace as tr

    tr._turns.clear()
    tr._trace_links.clear()

    sess = "miloco:test-pop"
    tr.register_trace_link(sess, "trace-abc")
    tr._hk_pre_llm_call(sess, "test query", [], True, "m", "p")
    tr._hk_on_session_end(sess, True, False, "m", "p")

    meta = tr.pop_done_turn(sess)
    assert meta is not None
    assert meta["run_id"] == sess
    assert meta["trace_id"] == "trace-abc"
    assert "llm_call_count" in meta
    assert "tool_call_count" in meta

    assert tr.pop_done_turn(sess) is None


# ── notify 三级 fallback ────────────────────────────────────────────────────

def test_notify_resolve_target_runtime_fallback(tmp_path, monkeypatch):
    """resolve_notify_target 三级 fallback：无 state.json → 扫 auth.json → needsBind。"""
    from miloco_plugin_pkg import tools_notify as tn

    class _FakeCtx:
        manifest = None

    monkeypatch.setattr(tn, "load_state", lambda ctx: {})
    monkeypatch.setattr(tn, "_detect_im_platforms_simple", lambda: [])
    result = tn.resolve_notify_target(_FakeCtx)
    # 无 state.json 且无 auth.json → needsBind=True
    assert result["needsBind"] is True
    assert "hint" in result


def test_notify_resolve_target_with_state_json(tmp_path, monkeypatch):
    """有 state.json::deliver.target → 直接用。"""
    from miloco_plugin_pkg import tools_notify as tn

    class _FakeCtx:
        manifest = None

    monkeypatch.setattr(tn, "load_state",
                        lambda ctx: {"deliver": {"target": "feishu:oc_xxx"}})
    monkeypatch.setattr(tn, "_detect_im_platforms_simple", lambda: [])
    result = tn.resolve_notify_target(_FakeCtx)
    assert result["target"] == "feishu:oc_xxx"
    assert result["needsBind"] is False
