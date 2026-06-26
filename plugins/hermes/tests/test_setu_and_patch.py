"""防回归测试：
- Issue 1+2: 全 fork bash 文件不能有 unbraced $VAR 接 CJK 的 set -u 隐患
- Issue 3b: install-hermes.sh step 8.5 必须 patch ~/.hermes/config.yaml 的 plugins.disabled
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent.parent
INSTALL_SH = REPO_ROOT / "plugins" / "hermes" / "install-hermes.sh"
ADAPTER_SH = REPO_ROOT / "plugins" / "hermes" / "scripts" / "miloco-adapter.sh"
LAUNCHER_SH = REPO_ROOT / "plugins" / "hermes" / "scripts" / "adapter-launcher.sh"
E2E_SH = REPO_ROOT / "plugins" / "hermes" / "tests" / "test_install_e2e.sh"


# ─── Issue 1+2: set -u scan ──────────────────────────────────────────


DANGEROUS_PATTERN = re.compile(
    r"(?<![{\\])"      # not preceded by { or \
    r"\$([A-Za-z_][A-Za-z0-9_]*)"  # $NAME without braces
    r"(?![A-Za-z0-9_}\\])"          # not followed by varname chars
    r"(?P<after>.)"                  # capture next char
)


def _is_comment(line: str) -> bool:
    """判断一行是否 bash 注释（行首 # 或 tab + #）。"""
    stripped = line.lstrip()
    return stripped.startswith("#")


@pytest.mark.parametrize(
    "script",
    [INSTALL_SH, ADAPTER_SH, LAUNCHER_SH, E2E_SH],
    ids=["install-hermes.sh", "miloco-adapter.sh", "adapter-launcher.sh", "test_install_e2e.sh"],
)
def test_no_unbraced_var_followed_by_cjk(script: Path):
    """全 fork bash 文件里，禁止有 unbraced $VAR 紧接 CJK / 全角符号 的 set -u 隐患。

    注释行除外（不会被执行）。

    修法：把 $VAR 改成 ${VAR}，让 bash 明确 varname 边界。
    """
    dangerous = []
    with script.open(encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if _is_comment(line):
                continue
            for m in DANGEROUS_PATTERN.finditer(line):
                c = m.group("after")
                if ord(c) > 127:  # CJK / 全角符号
                    dangerous.append(f"  L{i}: U+{ord(c):04X} ({c!r}) | {line.rstrip()}")
    assert not dangerous, (
        f"{script.name} 有 {len(dangerous)} 处 set -u 隐患（macOS bash 把 CJK 当 varname 字符）：\n"
        + "\n".join(dangerous)
        + "\n\n修法：把 $VAR 改成 ${VAR}（带花括号）。"
    )


# ─── Issue 3b: config.yaml patch step ───────────────────────────────


def test_install_has_step_8_5():
    """install-hermes.sh 必须有 step 8.5（兜底清 hermes namespace disable 漏写）。"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert 'mark_done 8.5' in text, "install-hermes.sh 缺 mark_done 8.5"
    assert 'plugins.disabled' in text, "install-hermes.sh 缺 plugins.disabled 处理"


def test_step_8_5_is_idempotent():
    """step 8.5 的 Python 块必须是幂等的：disabled 列表里没 miloco 时不动。"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    # 找 step 8.5 的 python heredoc
    assert 'new_disabled = [d for d in disabled if not str(d).startswith("miloco")]' in text


def test_step_8_5_safe_when_yaml_missing():
    """PyYAML 没装 / config.yaml 不存在时，step 8.5 必须 silent skip（不致命）。"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    # 用 2>/dev/null || true 兜底
    assert '"$PYTHON" - <<\'PY\' 2>/dev/null || true' in text


def test_step_8_5_safe_when_config_yaml_missing():
    """config.yaml 不存在 → sys.exit(0) 静默退出。"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    # python heredoc 里必须 short-circuit 文件不存在的情况
    assert "p.exists()" in text
    assert "if not p.exists():\n    sys.exit(0)" in text


def test_step_8_5_safe_when_yaml_parse_fails():
    """yaml.safe_load 抛异常时也要 silent skip。"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "except Exception:" in text
    assert "sys.exit(0)" in text


# ─── shell 语法 ──────────────────────────────────────────────────────


@pytest.mark.parametrize("script", [INSTALL_SH, ADAPTER_SH, LAUNCHER_SH, E2E_SH])
def test_script_syntax(script: Path):
    bash = shutil.which("bash") or "bash"
    r = subprocess.run(
        [bash, "-n", str(script)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0, f"{script.name} 语法错:\n{r.stderr}"