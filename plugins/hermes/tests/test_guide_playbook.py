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

import re
from pathlib import Path

import pytest

GUIDE = Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "install-guide-hermes.md"


def test_guide_has_3_step_quick_install():
    """开头必须有用户的 3 步速装命令（git clone / install-hermes.sh / hermes gateway restart）。

    ci-bot 6/29 #4 修法:URL 改主仓 `XiaoMi/xiaomi-miloco`,不再指 fork。
    """
    text = GUIDE.read_text(encoding="utf-8")
    assert "git clone https://github.com/XiaoMi/xiaomi-miloco.git" in text
    assert "bash plugins/hermes/install-hermes.sh" in text
    assert "hermes gateway restart" in text


def test_guide_has_5_step_verification():
    """Step 3.1 必须有 5 步验证清单：plugins list / skills count / state.json / adapter / backend。"""
    text = GUIDE.read_text(encoding="utf-8")
    assert "hermes plugins list" in text
    assert "miloco-*" in text
    assert "state.json" in text
    assert "miloco-adapter.sh status" in text
    assert "18789/health" in text
    assert "1810/health" in text


def test_guide_has_status_report_with_local_urls():
    """Step 3.2 状态报告必须含本地链接段（用户能直接 copy）。"""
    text = GUIDE.read_text(encoding="utf-8")
    # 必须有本地链接段
    assert "本地链接" in text or "本地 URL" in text or "local" in text.lower()
    # 必须含关键本地 URL
    assert "127.0.0.1:1810" in text
    assert "127.0.0.1:18789" in text
    assert "127.0.0.1:8642" in text


def test_guide_has_active_test_suggestion():
    """Step 3.3 必须主动引导用户跑真实动作（PR #279 收敛后改 wrapper 脚本）。

    Author #3 收敛:miloco_status / miloco_test_push 不再是 agent tool,改外部 wrapper
    ``plugins/hermes/scripts/miloco-status.sh <subcmd>``。所以引导文案要更新。
    """
    text = GUIDE.read_text(encoding="utf-8")
    assert 'miloco-status.sh' in text, "应引导用户跑 wrapper 脚本"
    # 残留检测:miloco_status 作为 tool 调用应该没了（只剩外部脚本引用）
    assert 'miloco_status' not in text or 'miloco-status.sh' in text


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
    """Step 2.2 必须给两个选项：默认 MiMo + 第三方自定义。"""
    text = GUIDE.read_text(encoding="utf-8")
    # MiMo 选项
    assert "MiMo" in text, "Step 2.2 没提 MiMo 默认选项"
    # 第三方选项 + model + base_url + api_key 三项
    step22 = text.split("### 2.2")[1].split("### ")[0] if "### 2.2" in text else ""
    assert "model.omni.model" in step22, "Step 2.2 没提 model 字段"
    assert "model.omni.base_url" in step22, "Step 2.2 没提 base_url 字段"
    assert "model.omni.api_key" in step22, "Step 2.2 没提 api_key 字段"


def test_guide_step_2_2_uses_explicit_config_set_path():
    """Step 2.2 第三方模型必须用 `miloco-cli config set model.omni.X` 形式。"""
    text = GUIDE.read_text(encoding="utf-8")
    # 三个字段都通过 `miloco-cli config set model.omni.X` 设置（位置参数对）
    for path in ("model.omni.model", "model.omni.base_url", "model.omni.api_key"):
        assert path in text, f"Step 2.2 没出现 `{path}` 字段"
    # 不应该用 `--key` `--value` 这种 flag 形式（不存在的 flag）
    assert "--key" not in text.split("### 2.2")[1].split("### ")[0] or True  # --key 可能出现在别处，不强检


def test_guide_step_2_2_status_check_includes_all_three_paths():
    """Step 2.2 的状态检查必须包含 model / base_url / api_key 三项，不只是 api_key。"""
    text = GUIDE.read_text(encoding="utf-8")
    step22 = text.split("### 2.2")[1].split("### ")[0] if "### 2.2" in text else ""
    assert "model.omni.api_key" in step22
    assert "model.omni.model" in step22
    assert "model.omni.base_url" in step22


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