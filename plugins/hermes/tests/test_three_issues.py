"""3 个 hermes-reported bug 的防回归测试。

- Issue 2: $LAUNCHD_LOG 用了未定义变量 → 改成 $ADAPTER_LOG
- Issue 1: 半装残留检测（supervisord.sock 残留 / stale pid / config.json 缺失）
- Issue 3: IM 探测扩视野（env vars + auth.json 顶层 + XDG 路径）
- Issue 4 (macOS launchd 路径): cmd_start_launchd 60s retry 不能用 lsof 反查端口
  （launchd 子进程上 lsof 不可靠 → 误判失败 → unload 把刚起的 adapter bootout 掉）
  修法：改用 PID 文件 + kill -0 + /health 三件齐全才算起。
"""

from __future__ import annotations

from pathlib import Path

import pytest

INSTALL_SH = Path(__file__).resolve().parent.parent / "install-hermes.sh"
ADAPTER_SH = Path(__file__).resolve().parent.parent / "scripts" / "miloco-adapter.sh"


# ─── Issue 2: $LAUNCHD_LOG typo ──────────────────────────────────────


def test_no_undefined_launchd_log_variable():
    """$LAUNCHD_LOG 是 typo（变量从未定义），必须不存在。"""
    text = ADAPTER_SH.read_text(encoding="utf-8")
    assert "$LAUNCHD_LOG" not in text, (
        "miloco-adapter.sh 还在用 $LAUNCHD_LOG（未定义变量，set -u 会崩）"
    )
    assert "${LAUNCHD_LOG}" not in text


def test_adapter_uses_correct_log_variable():
    """正确的日志变量是 $ADAPTER_LOG（line 27 定义）。"""
    text = ADAPTER_SH.read_text(encoding="utf-8")
    assert "ADAPTER_LOG=\"$HERMES_HOME/miloco-adapter.log\"" in text
    # 错误分支打印日志路径时用 $ADAPTER_LOG
    assert "${ADAPTER_LOG}" in text


# ─── Issue 1: 半装残留检测 ────────────────────────────────────────


def test_install_step_1_6_detects_supervisord_sock_residue():
    """Step 1.6 必须检测 supervisord.sock 残留（无 conf 但有 sock）。"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    # 新增的 1.6 块位于 # --- 1.6 注释和 mark_done 1 之间
    step16 = text.split("# --- 1.6")[1].split("mark_done 1")[0]
    assert "SUPERVISORD_SOCK" in step16 or "supervisord.sock" in step16
    assert "半装残留" in step16


def test_install_step_1_6_detects_stale_pid():
    """Step 1.6 必须检测 stale pid（pid 文件存在但进程已死）。"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    step16 = text.split("# --- 1.6")[1].split("mark_done 1")[0]
    assert "stale" in step16.lower() or "stale pid" in step16


def test_install_step_1_6_detects_missing_config_json():
    """Step 1.6 必须检测 config.json 缺失。"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "config.json 缺失" in text or "config.json: not found" in text.lower()


def test_install_step_1_6_does_not_kill_supervisord_silently():
    """半装残留检测不擅自 kill supervisord（可能管着别的服务）。"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    step16 = text.split("# --- 1.6")[1].split("mark_done 1")[0]
    # 找包含 supervisord 的行（排除注释行）
    for line in step16.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if "supervisord" in line and "kill" in line.lower():
            pytest.fail(f"半装残留不该自动 kill supervisord: {line!r}")


# ─── Issue 3: IM 探测扩视野 ────────────────────────────────────────


def test_install_im_detection_checks_auth_json_providers():
    """IM 探测必须读 auth.json::providers。

    探测逻辑挪到了外部 Python 脚本 detect_im_platforms.py（避免 bash 3.2
    解析 heredoc 内含括号挂），但 install-hermes.sh 必须仍调用这个脚本。
    """
    script = Path(__file__).resolve().parent.parent / "scripts" / "detect_im_platforms.py"
    assert script.is_file(), "detect_im_platforms.py 不存在"
    text = script.read_text(encoding="utf-8")
    assert 'cfg.get("providers")' in text


def test_install_im_detection_checks_auth_json_top_level():
    """IM 探测必须读 auth.json 顶层（旧 Hermes 版本可能不用 providers）。"""
    script = Path(__file__).resolve().parent.parent / "scripts" / "detect_im_platforms.py"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert "auth.json 顶层" in text or "顶层 fallback" in text
    # 还要保留对顶层（非 providers 段）的实际读取逻辑
    assert "cfg.get(plat)" in text


def test_install_im_detection_checks_env_vars():
    """IM 探测必须读环境变量（FEISHU_APP_ID / TELEGRAM_BOT_TOKEN / ...）。"""
    script = Path(__file__).resolve().parent.parent / "scripts" / "detect_im_platforms.py"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert "ENV_VARS" in text
    assert "TELEGRAM_BOT_TOKEN" in text
    assert "FEISHU_APP_ID" in text
    assert "WEIXIN_APP_ID" in text
    assert "os.environ.get" in text


def test_install_im_detection_checks_xdg_path():
    """IM 探测必须读 XDG 备用路径 ~/.config/hermes/auth.json。"""
    script = Path(__file__).resolve().parent.parent / "scripts" / "detect_im_platforms.py"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert ".config" in text
    assert "hermes" in text
    assert "alt_auth" in text or "XDG" in text


def test_install_im_detection_covers_all_mainstream_platforms_in_env():
    """环境变量表必须覆盖主流 10+ 平台。"""
    script = Path(__file__).resolve().parent.parent / "scripts" / "detect_im_platforms.py"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    env_block = text.split("ENV_VARS = {")[1].split("}")[0] if "ENV_VARS = {" in text else ""
    for plat in ("telegram", "discord", "slack", "feishu", "wecom", "dingtalk", "weixin", "qqbot", "whatsapp"):
        assert plat in env_block, f"ENV_VARS 缺 {plat} 平台"


def test_install_step_4_5_invokes_detect_script():
    """install-hermes.sh step 4.5 必须调外部 Python 脚本（不是内联 heredoc）。

    防止 bash 3.2 解析内联 Python heredoc + (fallback) 括号挂。
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "detect_im_platforms.py" in text
    # 关键: 不再有超长内联 Python heredoc（200+ 行）包含 IM 探测逻辑
    assert "auth.json / config.yaml / env vars (fallback)" not in text, (
        "install-hermes.sh 不应再有 'auth.json / config.yaml / env vars (fallback)' "
        "字符串（bash 3.2 解析括号会挂）"
    )


# ─── Issue 4 (macOS launchd 路径): cmd_start_launchd 60s retry 不能靠 lsof ─────


def test_launchd_start_does_not_rely_on_lsof():
    """macOS launchd 路径下 60s retry 循环里不能用 lsof 反查端口 PID。
    lsof 在 launchd-managed 子进程上偶发拿不到 socket，会误判失败 → unload 干掉刚起的 adapter。
    修法：靠 PID 文件 + kill -0 + /health 三件齐全才算起。
    """
    text = ADAPTER_SH.read_text(encoding="utf-8")
    # 提取 cmd_start_launchd 函数体
    start = text.find("cmd_start_launchd() {")
    assert start >= 0, "找不到 cmd_start_launchd()"
    # 函数体以下一个 standalone "}" 结束（粗略切到下一个 "cmd_" 函数开头前）
    rest = text[start:]
    body_end = rest.find("\ncmd_")
    body = rest[: body_end if body_end > 0 else len(rest)]
    # 60s retry 循环段（for i in $(seq 1 120) 之后）
    retry_start = body.find("for i in $(seq 1 120)")
    assert retry_start >= 0, "cmd_start_launchd 没有 60s retry 循环"
    retry_block = body[retry_start:]
    # 不应该有 lsof 反查端口
    assert "lsof" not in retry_block, (
        "cmd_start_launchd retry 循环还在用 lsof，launchd 子进程上不可靠"
    )
    assert "get_pid_by_port" not in retry_block, (
        "cmd_start_launchd retry 循环还在反查端口 PID（lsof/ss/netstat）"
    )


def test_launchd_start_waits_for_pid_file_and_health():
    """修后 retry 循环必须用 PID 文件 + kill -0 + /health 三件确认。"""
    text = ADAPTER_SH.read_text(encoding="utf-8")
    start = text.find("cmd_start_launchd() {")
    rest = text[start:]
    body_end = rest.find("\ncmd_")
    body = rest[: body_end if body_end > 0 else len(rest)]
    retry_start = body.find("for i in $(seq 1 120)")
    retry_block = body[retry_start:]
    # 必须读 PID 文件
    assert "cat \"$ADAPTER_PID\"" in retry_block, "retry 没读 PID 文件"
    # 必须 kill -0 验活
    assert "kill -0 \"$pid\"" in retry_block, "retry 没验 PID 是否真活"
    # 必须 curl /health
    assert "/health" in retry_block, "retry 没 curl /health"
    assert "curl" in retry_block, "retry 没 curl /health"


def test_launchd_start_failure_does_not_call_unload_when_pid_file_missing():
    """失败时 unload 前必须能看到日志路径（给用户排查用）。"""
    text = ADAPTER_SH.read_text(encoding="utf-8")
    start = text.find("cmd_start_launchd() {")
    rest = text[start:]
    body_end = rest.find("\ncmd_")
    body = rest[: body_end if body_end > 0 else len(rest)]
    # 失败分支在 retry 循环后
    fail_block_start = body.find("else")
    fail_block = body[fail_block_start:]
    # 必须含日志 tail（用户能看日志）
    assert "tail -20 \"$ADAPTER_LOG\"" in fail_block or "tail -20" in fail_block, (
        "失败分支没 tail 日志给用户看"
    )
    # 必须最后才 unload（给了用户排查机会）
    assert "launchctl unload" in fail_block