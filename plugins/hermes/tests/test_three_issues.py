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
    """架构 #1+#2 后日志从独立 adapter log 收敛到 backend log 目录。"""
    text = ADAPTER_SH.read_text(encoding="utf-8")
    assert "ADAPTER_LOG=\"$HERMES_HOME/miloco-adapter.log\"" in text, (
        "仍需定义 ADAPTER_LOG（旧架构兼容）"
    )
    assert "MILOCO_HOME/log" in text or "/log/miloco" in text, (
        "应引用 backend 日志目录"
    )


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
    """半装残留检测不擅自 kill supervisord（可能管着别的服务）。

    Zirconi 6/25 B5 修法:把 "supervisord -c /dev/null shutdown" 这条**无效提示**改成
    "miloco-cli service stop" 或 "ps aux | grep supervisord, kill <PID>" —— 都是给用户
    手动操作,不是脚本自动 kill。所以测试要排除 warn/echo/info 提示行,只看真正的
    执行语句。
    """
    import re
    text = INSTALL_SH.read_text(encoding="utf-8")
    step16 = text.split("# --- 1.6")[1].split("mark_done 1")[0]
    # 找包含 supervisord 的非注释行
    for line in step16.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # 跳过纯提示行（warn / info / echo + 用户指令 / -z 等提示文案）
        if re.match(r'^\s*(warn|info|echo)\s', line):
            continue
        # 引号里的 kill 是用户提示(manual 操作),不是脚本执行
        if "kill" in line.lower() and "supervisord" in line:
            # 排除:warn/info 提示行 + 纯字串提示(在引号里)
            if any(tok in line for tok in ['"', "'", 'warn "', 'info "', 'echo "']):
                continue
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
    """架构 #1+#2 后不再有独立 launchd adapter 进程;start 走 supervisorctl。
    cmd_start_launchd 函数已收敛到 _cleanup_old_launchd + supervisorctl。
    """
    text = ADAPTER_SH.read_text(encoding="utf-8")
    if "cmd_start_launchd" in text:
        # 如仍有，必须不含 lsof
        start = text.find("cmd_start_launchd() {")
        rest = text[start:]
        body_end = rest.find("\ncmd_")
        body = rest[: body_end if body_end > 0 else len(rest)]
        assert "lsof" not in body
    else:
        # 新架构：supervisord 管理，无 launchd retry 循环
        assert "supervisorctl" in text


def test_supervisorctl_start_checks_running():
    """cmd_start 应检查 supervisord 和 backend 运行状态，而非依赖端口轮询。"""
    text = ADAPTER_SH.read_text(encoding="utf-8")
    start_section = text.split("cmd_start()")[1].split("cmd_stop()")[0]
    assert "_sv_running" in start_section or "supervisorctl" in start_section
    assert "supervisorctl start" in start_section or "supervisorctl status" in start_section