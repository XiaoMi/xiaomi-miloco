"""仓库体检：shell 脚本里紧跟中文的变量展开必须写花括号。

macOS 自带的 /bin/bash（3.2.57，Apple 那份补丁版）在 UTF-8 locale 下会把紧跟变量名的
多字节字符首字节当成变量名的一部分：

    $ v=hi; echo "A（$v）B"          # LC_ALL=C.UTF-8, bash 3.2.57
    bash: v?: unbound variable      # 查的是 "v\xef" 而不是 "v"

`set -u` 下这会直接把脚本打断（本仓库的 shell 脚本基本都开了 `set -euo pipefail`），
而且往往发生在"本来要打印一句人话"的错误分支上，把真正的报错替换成一句莫名其妙的
unbound variable。LC_ALL=C 反而正常 —— 所以这不是靠 locale 能绕开的，唯一可靠写法是
`"${VAR}中文"`。docker 里的 bash 3.2 / 4.4 / 5.2 都不复现，只有 Apple 那份会。

这个测试只管纯 ASCII 边界上的坑，不碰注释（注释不参与展开）。
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]

# 只查命名变量：$1 / $# / $? 这类特殊参数 bash 只吃一个字符，紧跟中文也不会跑偏。
_BARE_VAR_BEFORE_NON_ASCII = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*(?=[^\x00-\x7f])")

# 第三方/生成物不体检
_SKIP_PARTS = {"node_modules", ".venv", "dist", "build", ".git"}


def _repo_shell_scripts() -> list[Path]:
    return sorted(
        p
        for p in _ROOT.rglob("*.sh")
        if not _SKIP_PARTS.intersection(p.parts)
    )


def test_shell_scripts_found() -> None:
    """先确认扫得到脚本，否则下面那条断言会因为"一个文件都没扫"而假绿。"""
    scripts = _repo_shell_scripts()
    assert len(scripts) >= 10, f"只扫到 {len(scripts)} 个 .sh，路径推导可能坏了：{_ROOT}"
    names = {p.name for p in scripts}
    assert {"build.sh", "local-ci.sh", "install-hermes.sh"} <= names, names


def test_no_bare_var_before_non_ascii() -> None:
    offenders: list[str] = []
    for path in _repo_shell_scripts():
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if _BARE_VAR_BEFORE_NON_ASCII.search(line):
                rel = path.relative_to(_ROOT)
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "以下位置的变量紧跟非 ASCII 字符，macOS 自带 bash 3.2 会把它连进变量名，"
        "set -u 下脚本会炸在这行。改成 ${VAR} 即可：\n  " + "\n  ".join(offenders)
    )
