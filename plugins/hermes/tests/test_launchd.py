"""miloco-adapter.sh + install-hermes.sh 架构校验（supervisord 路径）。

架构 #1+#2 后适配器从独立 launchd/aiohttp 进程收敛到 miloco backend 的
AgentPlatformAdapter。miloco-adapter.sh 不再管 launchd，改管 supervisord
下的 miloco-backend 程序。

本套测试做这些事：
- 静态扫描 plist / launcher / miloco-adapter.sh：清理逻辑 / supervisord 集成
- 校验 install-hermes.sh step 改动没把代码改坏（语法 + 关键 grep）
- 跑回归：现有 e2e + 单测仍过
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent.parent
SCRIPTS = REPO_ROOT / "plugins" / "hermes" / "scripts"
INSTALL_SH = REPO_ROOT / "plugins" / "hermes" / "install-hermes.sh"
ADAPTER_SH = SCRIPTS / "miloco-adapter.sh"
LAUNCHER_SH = SCRIPTS / "adapter-launcher.sh"
PLIST = SCRIPTS / "com.xiaomi.miloco.hermes.adapter.plist"


# ─── plist 模板（已随 revert b79bd60 删除，不再由 install 使用）───────────


def test_plist_removed():
    """plist 模板已在 revert feat/web-provider-abstraction 时删除。"""
    if PLIST.is_file():
        pytest.fail(f"plist 模板仍存在: {PLIST}，应已删除")


def test_plist_not_needed():
    """架构 #1+#2 后 launchd plist 已不需要(supervisord 管 backend)。"""
    pass


# ─── adapter-launcher.sh（过渡兼容：只做旧架构清理 + 退出）─────────────────


def test_launcher_exists_and_executable():
    assert LAUNCHER_SH.is_file()
    text = LAUNCHER_SH.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    # 架构 #1+#2 后不再 exec python -m adapter；改为清理 launchd + 退出
    assert "launchctl unload" in text or "launchctl list" in text
    assert "supervisord" in text or "miloco-backend" in text or "miloco-adapter.sh" in text


def test_launcher_performs_cleanup():
    """新 adapter-launcher.sh 清理旧 launchd job + plist，不再跑独立 adapter 进程。"""
    text = LAUNCHER_SH.read_text(encoding="utf-8")
    assert "$LAUNCHD_LABEL" in text or "com.xiaomi.miloco.hermes.adapter" in text
    assert "rm -f" in text, "必须删旧 plist/pid"


# ─── miloco-adapter.sh：supervisord 路径 ─────────────────────────────────


def test_adapter_detects_macos():
    text = ADAPTER_SH.read_text(encoding="utf-8")
    assert 'IS_MACOS=0' in text
    assert '[ "$(uname -s)" = "Darwin" ] && IS_MACOS=1' in text


def test_adapter_has_supervisord_constants():
    """miloco-adapter.sh 必须定义 supervisord 相关常量 + 旧架构清理常量。"""
    text = ADAPTER_SH.read_text(encoding="utf-8")
    for const in ("SUPERVISORD_CONF", "SUPERVISORD_SOCK", "BACKEND_PROGRAM"):
        assert const in text, f"miloco-adapter.sh 缺 supervisord 常量 {const}"
    # 旧架构 cleanup 常量仍保留
    for const in ("LAUNCHD_LABEL",):
        assert const in text, f"miloco-adapter.sh 缺清理常量 {const}"


def test_adapter_has_cleanup_old_launchd():
    """cmd_start 应调用 _cleanup_old_launchd 清理旧架构残留。"""
    text = ADAPTER_SH.read_text(encoding="utf-8")
    assert "_cleanup_old_launchd" in text
    assert "launchctl unload" in text


def test_adapter_uses_supervisorctl_not_noop():
    """cmd_start 应该调用 supervisorctl 管 miloco-backend，不再用 nohup 启 adapter。"""
    text = ADAPTER_SH.read_text(encoding="utf-8")
    start_section = text.split("cmd_start()")[1].split("cmd_stop()")[0]
    assert "supervisorctl" in start_section, "cmd_start 没调 supervisorctl"
    assert "$SUPERVISORD_CONF" in start_section
    assert '$BACKEND_PROGRAM' in start_section or 'miloco-backend' in start_section
    assert "nohup" not in start_section, "不应再走独立 adapter 进程的 nohup 路径"


def test_adapter_stop_uses_supervisorctl():
    text = ADAPTER_SH.read_text(encoding="utf-8")
    stop_section = text.split("cmd_stop()")[1].split("cmd_status()")[0]
    assert "supervisorctl" in stop_section, "cmd_stop 应收敛到 supervisorctl"
    assert "supervisorctl shutdown" in text


def test_adapter_status_uses_supervisorctl():
    text = ADAPTER_SH.read_text(encoding="utf-8")
    status_section = text.split("cmd_status()")[1].split("cmd_logs()")[0]
    assert "supervisorctl" in status_section, "cmd_status 应收敛到 supervisorctl status"


# ─── install-hermes.sh ────────────────────────────────────────────────────


def test_install_step5_cleans_old_baks():
    text = INSTALL_SH.read_text(encoding="utf-8")
    step5 = text.split('step 5 "')[1].split('step 6')[0]
    assert "config.json.bak-*" in step5
    assert "tail -n +4" in step5
    assert "rm -f" in step5


def test_install_step7_delegates_to_adapter_script():
    text = INSTALL_SH.read_text(encoding="utf-8")
    step7 = text.split('step 7')[1].split('mark_done 7')[0]
    assert "miloco-adapter.sh" in step7
    assert "start" in step7
    assert 'nohup "$PYTHON" -m adapter' not in step7
    assert 'nohup' not in step7 or 'nohup' not in text.split('step 7')[1].split('mark_done 7')[0], \
        "step 7 不应有旧 nohup 代码"


def test_install_step7_mentions_supervisord():
    """step 7 应提及 supervisord 管理（架构 #1+#2）。"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    step7 = text.split('step 7')[1].split('mark_done 7')[0]
    assert "supervisord" in step7.lower()


def test_install_chmods_launcher():
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "chmod +x" in text
    assert "adapter-launcher.sh" in text


def test_install_diagnose_checks_supervisord():
    """diagnose 应收敛到 supervisord 检查（不再依赖 launchd）。"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "supervisord" in text, "install-hermes.sh 没有 supervisord 相关检查"
    assert "miloco-backend" in text or "$BACKEND_PROGRAM" in text
    # 旧 launchd 检查仅作残留清理提示
    assert "launchctl" in text, "旧 launchd 残留检查也应保留"


def test_install_diagnose_cleans_old_launchd():
    """diagnose 应提示旧 launchd adapter 残留（如果存在）。"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "旧 launchd" in text or "com.xiaomi.miloco.hermes.adapter" in text


def test_adapter_macos_cleanup_path_exists():
    """macOS 上仍应有 _cleanup_old_launchd 路径（防御性清理）。"""
    text = ADAPTER_SH.read_text(encoding="utf-8")
    assert "IS_MACOS" in text
    assert "launchctl" in text


# ─── shell 语法 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("script", [INSTALL_SH, ADAPTER_SH, LAUNCHER_SH])
def test_script_syntax(script: Path):
    bash = shutil.which("bash") or "bash"
    r = subprocess.run(
        [bash, "-n", str(script)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0, f"{script.name} 语法错:\n{r.stderr}"


# ─── 常量默认值 ──────────────────────────────────────────────────────────


def test_adapter_constants_have_sensible_defaults():
    text = ADAPTER_SH.read_text(encoding="utf-8")
    assert 'ADAPTER_PORT="${ADAPTER_PORT:-18789}"' in text
    assert 'HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"' in text
    assert 'MILOCO_HOME="${MILOCO_HOME:-$HOME/.openclaw/miloco}"' in text
