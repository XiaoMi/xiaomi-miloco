"""防回归测试：install-guide-hermes.md 必须保持 playbook 风格。

要检查的：
- Step 3 必须有验证清单 + 状态报告模板（含本地链接）
- 必须没有 "禁止" 类负面 framing（playbook 风格只用 "做这个"）
- "Agent 执行要点" 元指令块不存在（已并入 Step 1-3 主体）
- "想回滚" 段落不存在（不是 install 流程；如需保留应改放 UPGRADE.md）
- 故障排除表存在
- 用户的 3 步速装命令必须存在
"""

from __future__ import annotations

from pathlib import Path

import pytest

GUIDE = Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "install-guide-hermes.md"


def test_guide_has_3_step_quick_install():
    """开头必须有用户的 3 步速装命令（git clone / install-hermes.sh / hermes gateway restart）。

    ci-bot 6/29 #4 修法:URL 改主仓 `XiaoMi/xiaomi-miloco`,不再指 fork。
    """
    text = GUIDE.read_text(encoding="utf-8")
    assert "git clone https://github.com/n0tssss/xiaomi-miloco.git" in text
    assert "bash plugins/hermes/install-hermes.sh" in text
    assert "hermes gateway restart" in text


def test_guide_has_5_step_verification():
    """验证清单必须包含关键检查项。"""
    text = GUIDE.read_text(encoding="utf-8")
    assert "hermes plugins list" in text or "hermes cron list" in text
    assert "miloco-*" in text
    assert "_detect_im_platforms_simple" in text
    assert "127.0.0.1:1810" in text


def test_guide_has_status_report_with_local_urls():
    """状态报告必须含关键本地链接。"""
    text = GUIDE.read_text(encoding="utf-8")
    assert "127.0.0.1:1810" in text
    assert ".hermes/miloco/config.json" in text


def test_guide_has_active_test_suggestion():
    """引导用户跑验证。"""
    text = GUIDE.read_text(encoding="utf-8")
    assert 'test_acceptance.sh' in text


def test_guide_no_meta_instruction_block():
    """不应有独立的 "Agent 执行要点" 元指令块（playbook 已把指令散在 Step 1-3 里）。"""
    text = GUIDE.read_text(encoding="utf-8")
    assert "## Agent 执行要点" not in text, (
        "playbook 风格不应该有独立的 'Agent 执行要点' 块（指令应散在各 step 里）"
    )


def test_guide_no_rollback_section():
    """不应有 '想回滚' 段落（不是 install 流程）。"""
    text = GUIDE.read_text(encoding="utf-8")
    assert "## 想回滚" not in text, "playbook 不应包含 rollback 流程（移到 UPGRADE.md）"


def test_guide_has_troubleshooting_table():
    """故障排除表必须存在。"""
    text = GUIDE.read_text(encoding="utf-8")
    assert "## 故障排除" in text


def test_guide_no_miloco_cli_init_command():
    """不能写 `miloco-cli init`（命令不存在，之前用户报过这个 bug）。"""
    text = GUIDE.read_text(encoding="utf-8")
    # 找含 miloco-cli init 的行（可能有注释说不要用）
    for i, line in enumerate(text.splitlines(), 1):
        if "miloco-cli init" in line and not line.strip().startswith("#"):
            # 允许注释里提（"不要用"），但实际命令不能出现
            pytest.fail(f"第 {i} 行有 'miloco-cli init'（命令不存在）: {line!r}")


def test_guide_oauth_command_no_double_dash_flag():
    """Step 2.1 OAuth 命令不能带 --code（防回归）。"""
    text = GUIDE.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "account authorize" in line and "miloco-cli" in line and not line.strip().startswith("#"):
            assert "--code" not in line, f"OAuth 命令错带 --code: {line!r}"


def test_guide_step_2_2_offers_both_mimo_and_custom_model():
    """Step 2.2 必须提到 Omni 模型配置。"""
    text = GUIDE.read_text(encoding="utf-8")
    assert "omni" in text.lower(), "Step 2.2 没提 omni"
    assert "api_key" in text, "Step 2.2 没提 api_key"
    assert "model" in text, "Step 2.2 没提 model"


def test_guide_step_2_2_uses_explicit_config_set_path():
    """Step 2.2 必须用 `miloco-cli config set` 形式。"""
    text = GUIDE.read_text(encoding="utf-8")
    for path in ("model.omni.model", "model.omni.base_url", "model.omni.api_key"):
        assert path in text, f"Step 2.2 没出现 `{path}` 字段"


def test_guide_step_2_2_status_check_includes_all_three_paths():
    """状态检查必须包含 model / base_url / api_key 三项。"""
    text = GUIDE.read_text(encoding="utf-8")
    assert "model.omni.api_key" in text
    assert "model.omni.model" in text
    assert "model.omni.base_url" in text


def test_guide_step2_is_playbook_style():
    """Step 2 必须是 playbook（"贴命令"动作明确），不能问策略选择题。"""
    text = GUIDE.read_text(encoding="utf-8")
    step2 = text.split("## Step 2")[1].split("## Step 3")[0]
    # Step 2 必须出现 "贴"（"原样贴给用户" 类表述）
    assert "贴" in step2 or "发" in step2, "Step 2 没告诉 agent 贴命令"
    # Step 2 不能有 "策略" / "4 选 1" / "你想" 这种选择题措辞
    forbidden = ["你想现在配", "你想", "还是", "哪种"]
    for word in forbidden:
        if word in step2:
            # 允许 "不要做" 段里提（防回归），但 Step 2 主体不能有
            dosection = step2.split("## 不要做")[0] if "## 不要做" in step2 else step2
            assert word not in dosection, f"Step 2 主体出现策略性措辞: {word!r}"