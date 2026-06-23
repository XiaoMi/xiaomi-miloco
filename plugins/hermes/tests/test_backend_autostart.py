"""install-hermes.sh 的 backend 自动拉起 + --diagnose 文案测试。

覆盖：
- --no-start-backend flag 解析
- Step 1 后端自动 service start + /health 探测
- --diagnose 模式 backend 状态显示 PID/端口 + 修复提示
- install-guide-hermes.md Step 1.6 存在 + 关键警告文案
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent.parent
SCRIPTS = REPO_ROOT / "plugins" / "hermes" / "scripts"
INSTALL_SH = REPO_ROOT / "plugins" / "hermes" / "install-hermes.sh"
GUIDE_MD = REPO_ROOT / "scripts" / "install-guide-hermes.md"


# ─── --no-start-backend flag ────────────────────────────────────────────


def test_help_text_mentions_no_start_backend():
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "--no-start-backend" in text


def test_no_start_backend_flag_parsed():
    """--no-start-backend 必须设 NO_START_BACKEND=1，跳过自动 service start。"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert 'NO_START_BACKEND=0' in text
    assert '--no-start-backend) NO_START_BACKEND=1' in text


def test_no_start_backend_gate_in_step1():
    """Step 1.5 自动 service start 必须受 NO_START_BACKEND gate 控制。"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    # 找到 Step 1 后的 backend 拉起代码段
    step1 = text.split('step 1 "前置检查')[1].split('mark_done 1')[0]
    assert 'if [ "$NO_START_BACKEND" -eq 0 ]' in step1
    assert "service start" in step1


# ─── Step 1 后端自动拉起 ──────────────────────────────────────────────


def test_step1_autostart_calls_service_start():
    """Step 1.5 必须调 miloco-cli service start（不是 service restart / kill）。"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    step1 = text.split('step 1 "前置检查')[1].split('mark_done 1')[0]
    assert "miloco-cli service start" in step1


def test_step1_autostart_waits_for_health():
    """Step 1.5 必须探测 /health（不能用 service status，因为 status 输出格式不稳定）。"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    step1 = text.split('step 1 "前置检查')[1].split('mark_done 1')[0]
    assert "127.0.0.1:1810/health" in step1
    assert "seq 1 30" in step1


def test_step1_autostart_warns_on_failure():
    """service start 失败时必须 warn 提示手动修，不能默默失败。"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    step1 = text.split('step 1 "前置检查')[1].split('mark_done 1')[0]
    assert "service start 失败" in step1 or "service start\" 失败" in step1


def test_step1_autostart_handles_already_running():
    """backend 已在跑时不要重复 start。"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    step1 = text.split('step 1 "前置检查')[1].split('mark_done 1')[0]
    assert "miloco backend 已在跑" in step1


# ─── --diagnose backend 文案 ─────────────────────────────────────────


def test_diagnose_mentions_atexit_cause():
    """--diagnose 模式 backend 没跑时，必须提到 upstream atexit 是原因（教育用户）。"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    diagnose = text.split('if [ "$DIAGNOSE_ONLY" -eq 1 ]')[1:][0]
    # 用宽松匹配（diagnose 里有嵌套 if/fi，regex 难）
    assert "upstream install 退出时 atexit" in diagnose


def test_diagnose_suggests_install_hermes_autostart():
    """--diagnose 必须告诉用户 install-hermes.sh 会自动拉起（不是只说 service start）。"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    diagnose = text.split('if [ "$DIAGNOSE_ONLY" -eq 1 ]')[1:][0]
    assert "install-hermes.sh 会自动拉起" in diagnose


def test_diagnose_shows_pid_and_port_when_running():
    """backend 在跑时 --diagnose 应显示 PID + 端口（不只是 "在跑"）。"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    # 找 backend 在跑的 diag 调用，detail 部分必须含 PID= 和端口=
    # 用更简单的检查：整个 install-hermes.sh 里 diagnose 模式必须能 grep 到 PID=$ML_PID
    assert 'ML_PID="$(echo "$ML_OUT" | grep -oE' in text
    assert 'PID=$ML_PID' in text
    assert "端口=$ML_PORT" in text


# ─── install-guide-hermes.md Step 1.6 ────────────────────────────────


def test_guide_has_step_1_6():
    """必须新增 Step 1.6（backend 常驻 preflight），否则用户又踩坑。"""
    text = GUIDE_MD.read_text(encoding="utf-8")
    assert "### 1.6" in text
    assert "后端服务常驻" in text


def test_guide_step_1_6_warns_about_502():
    """Step 1.6 必须明确说：跳过这步 OAuth 会 502 假错误。"""
    text = GUIDE_MD.read_text(encoding="utf-8")
    step16 = text.split("### 1.6")[1].split("---")[0]
    assert "atexit" in step16, "Step 1.6 没提 upstream atexit 杀 backend"
    assert "502" in step16, "Step 1.6 没提 502 假错误"
    assert "service start" in step16


def test_guide_step_1_6_mentions_no_start_backend():
    """Step 1.6 必须提到 fork 的 --no-start-backend flag（用户能跳过自动 start）。"""
    text = GUIDE_MD.read_text(encoding="utf-8")
    step16 = text.split("### 1.6")[1].split("---")[0]
    assert "--no-start-backend" in step16


# ─── Step 2.1 OAuth 命令格式（防止回归） ──────────────────────────────


def test_guide_oauth_command_no_double_dash_flag():
    """Step 2.1 的 miloco-cli account authorize 命令不能用 --code 这种 flag（不存在）。"""
    text = GUIDE_MD.read_text(encoding="utf-8")
    # 找所有 account authorize 行
    import re as _re
    lines_with_auth = [
        line for line in text.splitlines()
        if "account authorize" in line and "miloco-cli" in line
    ]
    assert lines_with_auth, "install-guide-hermes.md 里没找到 account authorize 命令"
    for line in lines_with_auth:
        assert "--code" not in line, f"Step 2.1 命令错带 --code flag: {line!r}"


def test_guide_oauth_command_uses_positional_arg():
    """account authorize 是位置参数，命令里 base64 必须直接跟在命令后面（不放在 <...> 里也别加 flag）。"""
    text = GUIDE_MD.read_text(encoding="utf-8")
    # 应该匹配 `miloco-cli account authorize <...>` 形式（占位符里可以有空格/中文）
    assert re.search(
        r"miloco-cli\s+account\s+authorize\s+<",
        text,
    ), "install-guide-hermes.md 里 account authorize 没以位置参数形式给出"


def test_readme_oauth_command_no_double_dash_flag():
    """README.md 里的 OAuth 命令也不能带 --code。"""
    text = (REPO_ROOT / "plugins" / "hermes" / "README.md").read_text(encoding="utf-8")
    for line in text.splitlines():
        if "account authorize" in line and "miloco-cli" in line:
            assert "--code" not in line, f"README.md 命令错带 --code: {line!r}"


# ─── shell 语法 ───────────────────────────────────────────────────────


@pytest.mark.parametrize("script", [INSTALL_SH])
def test_script_syntax(script: Path):
    bash = shutil.which("bash") or "bash"
    r = subprocess.run(
        [bash, "-n", str(script)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0, f"{script.name} 语法错:\n{r.stderr}"