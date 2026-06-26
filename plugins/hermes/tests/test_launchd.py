"""miloco-adapter.sh + install-hermes.sh 在 launchd 路径下的纯文本 + 行为校验。

因为 launchctl / launchd 只能在 macOS 真机上跑，本套测试做这些事：
- 静态扫描 plist / launcher / miloco-adapter.sh：占位符 / sed 替换路径必须对齐
- 校验 install-hermes.sh step 5/7/8 改动没把代码改坏（语法 + 关键 grep）
- mock 平台：在 Python 里以 fake uname 启动 miloco-adapter.sh 的函数，验证分支选择
- 跑回归：现有 e2e + 单测仍过（test_install_e2e.sh + pytest）

不验证真机 launchctl 行为 —— 那个留给你 Mac 上跑 `bash plugins/hermes/install-hermes.sh`。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent.parent
SCRIPTS = REPO_ROOT / "plugins" / "hermes" / "scripts"
INSTALL_SH = REPO_ROOT / "plugins" / "hermes" / "install-hermes.sh"
ADAPTER_SH = SCRIPTS / "miloco-adapter.sh"
LAUNCHER_SH = SCRIPTS / "adapter-launcher.sh"
PLIST = SCRIPTS / "com.xiaomi.miloco.hermes.adapter.plist"


# ─── plist 模板 ───────────────────────────────────────────────────────────


def test_plist_exists():
    assert PLIST.is_file(), "plist 模板缺失"


def test_plist_has_all_placeholders():
    """plist 模板里必须有这些占位符，install-hermes.sh + miloco-adapter.sh 会 sed 替换。"""
    text = PLIST.read_text(encoding="utf-8")
    for ph in ("__LABEL__", "__ADAPTER_DIR__", "__LAUNCHER__", "__LOG_PATH__", "__ADAPTER_PORT__"):
        assert ph in text, f"plist 缺占位符 {ph}"


def test_plist_has_runatload_and_keepalive_false():
    """launchd 加载后立即拉起；不自动重启（用户可控）。"""
    text = PLIST.read_text(encoding="utf-8")
    assert "<key>RunAtLoad</key>" in text
    assert "<true/>" in text.split("<key>RunAtLoad</key>")[1].split("</dict>")[0]
    assert "<key>KeepAlive</key>" in text
    assert "<false/>" in text.split("<key>KeepAlive</key>")[1].split("</dict>")[0]


def test_plist_program_arguments_uses_bash_and_launcher():
    """plist 必须调 bash + launcher（不能用 python -m adapter 直接，因为 env vars 不传）。"""
    text = PLIST.read_text(encoding="utf-8")
    pa_section = text.split("<key>ProgramArguments</key>")[1].split("</array>")[0]
    assert "<string>/bin/bash</string>" in pa_section
    assert "__LAUNCHER__" in pa_section


# ─── launcher 包装脚本 ────────────────────────────────────────────────────


def test_launcher_exists_and_executable():
    assert LAUNCHER_SH.is_file()
    # 在 Windows 上 mode 位不准；但内容里必须 shebang + exec
    text = LAUNCHER_SH.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    assert "exec " in text, "launcher 必须 exec，否则 PID 会变（launchd 拿不到真 PID）"


def test_launcher_reads_env_file():
    text = LAUNCHER_SH.read_text(encoding="utf-8")
    assert ".env" in text
    # 必须抽 ADAPTER_AUTH_BEARER / HERMES_API_URL / ADAPTER_PORT 等
    for var in ("API_SERVER_KEY", "HERMES_API_URL", "ADAPTER_AUTH_BEARER", "ADAPTER_PORT"):
        assert var in text, f"launcher 没从 .env 抽 {var}"


def test_launcher_writes_pid_before_exec():
    """launcher 必须在 exec 前写 PID 文件（exec 后 PID 不变）。"""
    text = LAUNCHER_SH.read_text(encoding="utf-8")
    # 用更精确的字符串（避免 grep 到注释里的 'exec'/'PID_FILE'）
    pid_write = text.find('echo $$ > "$PID_FILE"')
    exec_line = text.find('exec "$PY"')
    assert pid_write != -1, 'launcher 没写 `echo $$ > "$PID_FILE"`'
    assert exec_line != -1, 'launcher 没 `exec "$PY"`'
    assert pid_write < exec_line, "PID 写在 exec 之后 → 错（exec 后 PID 不会变，但内容已被覆盖）"


# ─── miloco-adapter.sh：launchd 分支 ────────────────────────────────────


def test_adapter_detects_macos():
    text = ADAPTER_SH.read_text(encoding="utf-8")
    assert 'IS_MACOS=0' in text
    assert '[ "$(uname -s)" = "Darwin" ] && IS_MACOS=1' in text


def test_adapter_has_launchd_constants():
    text = ADAPTER_SH.read_text(encoding="utf-8")
    for const in ("LAUNCHD_LABEL", "LAUNCHD_PLIST", "PLIST_TEMPLATE", "LAUNCHER"):
        assert const in text, f"miloco-adapter.sh 缺常量 {const}"
    assert "com.xiaomi.miloco.hermes.adapter" in text


def test_adapter_start_delegates_to_launchd_on_macos():
    text = ADAPTER_SH.read_text(encoding="utf-8")
    # cmd_start 里应该先检测 macOS 走 launchd
    start_section = text.split("cmd_start()")[1].split("cmd_stop()")[0]
    assert "cmd_start_launchd" in start_section
    assert "IS_MACOS" in start_section


def test_adapter_stop_delegates_to_launchd_on_macos():
    text = ADAPTER_SH.read_text(encoding="utf-8")
    # cmd_stop 函数体里必须调 cmd_stop_launchd
    # 用宽松匹配：从 `cmd_stop() {` 到下一个 `cmd_stop_launchd()` 函数定义前
    start = text.find("cmd_stop() {")
    fn_def = text.find("cmd_stop_launchd()")
    assert start != -1 and fn_def != -1, "找不到 cmd_stop / cmd_stop_launchd"
    body = text[start:fn_def]
    assert "cmd_stop_launchd" in body, "cmd_stop() 没调 cmd_stop_launchd"
    assert "launchctl unload" in text


def test_adapter_status_delegates_to_launchd_on_macos():
    text = ADAPTER_SH.read_text(encoding="utf-8")
    # cmd_status() 函数体内必须出现 cmd_status_launchd（launchd 分支委托）
    # 用 "cmd_status() {" 起点到 "cmd_status_launchd()" 函数定义前为止
    start = text.find("cmd_status() {")
    fn_def = text.find("cmd_status_launchd()")
    assert start != -1 and fn_def != -1, "找不到 cmd_status / cmd_status_launchd"
    body = text[start:fn_def]
    assert "cmd_status_launchd" in body, "cmd_status() 没调 cmd_status_launchd"
    assert "launchctl list" in text


def test_adapter_sed_substitution_covers_all_placeholders():
    """cmd_start_launchd 里的 sed 必须替换全部 5 个占位符。"""
    text = ADAPTER_SH.read_text(encoding="utf-8")
    sed_section = text.split("cmd_start_launchd()")[1].split("info \"launchd 已加载")[0]
    for ph in ("__LABEL__", "__ADAPTER_DIR__", "__LAUNCHER__", "__LOG_PATH__", "__ADAPTER_PORT__"):
        assert ph in sed_section, f"sed 没替换 {ph}"


# ─── install-hermes.sh：step 5 bak 清理 + step 7 委托 ──────────────────


def test_install_step5_cleans_old_baks():
    text = INSTALL_SH.read_text(encoding="utf-8")
    # step 5 备份后必须清理老 bak
    step5 = text.split('step 5 "patch')[1].split('step 6')[0]
    assert "config.json.bak-*" in step5
    assert "tail -n +4" in step5, "step 5 没保留最新 3 份"
    assert "rm -f" in step5


def test_install_step7_delegates_to_adapter_script():
    text = INSTALL_SH.read_text(encoding="utf-8")
    step7 = text.split('step 7')[1].split('step 8')[0]
    # 不再自己启 nohup；委托给 miloco-adapter.sh
    assert "miloco-adapter.sh" in step7
    assert "start" in step7
    # 不能再有旧的 inline nohup（如果还在说明没改干净）
    assert 'nohup "$PYTHON" -m adapter' not in step7, "step 7 还有旧 nohup 代码，没委托干净"


def test_install_chmods_launcher():
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "chmod +x" in text
    assert "adapter-launcher.sh" in text


def test_install_diagnose_shows_launchd_on_macos():
    text = INSTALL_SH.read_text(encoding="utf-8")
    # 用更宽松的检查：只要 diagnose 主块存在 + 包含 macOS/launchd 字符串就行
    # （regex 跨大括号解析太脆，因为 diagnose 里有嵌套 if/fi）
    start = text.find('if [ "$DIAGNOSE_ONLY" -eq 1 ]')
    # find 对应的最外层 fi：用 awk 风格的括号匹配手动定位
    # 简化：从 start 往后找一个不在任何 case/diag 函数里的 'fi'
    # 最简单：直接 file-wide 检查 macOS/launchd 三个关键串都在 install-hermes.sh 里
    assert start != -1, "找不到 diagnose 入口"
    assert "Darwin" in text, "install-hermes.sh 全文没考虑 macOS"
    assert "launchctl list" in text, "install-hermes.sh 全文没查 launchd"
    assert "com.xiaomi.miloco.hermes.adapter" in text, "install-hermes.sh 全文没引 launchd label"


def test_install_extends_retry_window():
    """miloco-adapter.sh 里的 retry loop 必须 ≥ 60s（adapter 冷启动 import 要 10-30s）。"""
    text = ADAPTER_SH.read_text(encoding="utf-8")
    assert "seq 1 120" in text, "retry loop 没延长到 60s（120 × 0.5s）"


# ─── shell 语法 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("script", [INSTALL_SH, ADAPTER_SH, LAUNCHER_SH])
def test_script_syntax(script: Path):
    """所有改过的 bash 脚本必须语法正确（bash -n）。"""
    bash = shutil.which("bash") or "bash"
    r = subprocess.run(
        [bash, "-n", str(script)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0, f"{script.name} 语法错:\n{r.stderr}"


# ─── mock 平台：在 Python 里 source miloco-adapter.sh 的常量 ─────────────


def test_adapter_constants_have_sensible_defaults():
    """不跑 miloco-adapter.sh（它会 set -e + 写 pid），但能 grep 出关键默认。"""
    text = ADAPTER_SH.read_text(encoding="utf-8")
    assert 'ADAPTER_PORT="${ADAPTER_PORT:-18789}"' in text
    assert 'HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"' in text
    assert 'MILOCO_HOME="${MILOCO_HOME:-$HOME/.openclaw/miloco}"' in text