# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""仓库体检：shell 脚本里紧跟非 ASCII 字符的变量展开必须写花括号。

macOS 自带的 /bin/bash（3.2.57，Apple 那份补丁版）在 UTF-8 locale 下会把紧跟变量名的
非 ASCII 字符首字节当成变量名的一部分：

    $ v=hi; echo "A（$v）B"          # LC_ALL=C.UTF-8, bash 3.2.57
    bash: v?: unbound variable      # 查的是 "v\\xef" 而不是 "v"

不限于中文：`$vé` / `$v🙂` / `$v°C` 一样中招，判据就是「下一个字符不是 ASCII」。
LC_ALL=C 反而正常（单字节模式下遇到 >=0x80 的字节就停止取名），所以这不是靠 locale
能绕开的，唯一可靠写法是 `"${VAR}中文"`。

两种失败形态：

* 开了 `set -u`（本仓库跟踪的 shell 脚本目前全都开了）—— 直接 unbound variable 打断
  脚本，而且往往发生在"本来要打印一句人话"的错误分支上，把真报错顶掉；
* 没开 `set -u` —— 不报错，变量取空、后随字符首字节被吞掉，静默输出乱码（`A（??B`），
  更难发现。

docker 里的 bash 3.2 / 4.4 / 5.2 都不复现，只有 Apple 那份会。

**覆盖范围**：只扫 git 跟踪的 `*.sh` / `*.bash`。以下不在范围内 ——

* `.github/workflows/` 的内联 `run:` 块：跑在 ubuntu runner 的 bash 5 上，不受影响
  （仓库目前没有 macos runner；哪天加了就得把那些 run: 块一起纳管）；
* Python / TS 里的 shell 字符串（`subprocess(..., shell=True)`、`bash -c "…"` 字面量）：
  macOS 的 /bin/sh 就是 bash 3.2 的 posix 模式，实测同样中招，所以它们理论上在射程内，
  只是当前全仓 0 处，没为此加扫描。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]

# 只查命名变量：$1 / $# / $? 这类特殊参数 bash 只吃一个字符，紧跟非 ASCII 也不会跑偏。
_BARE_VAR_BEFORE_NON_ASCII = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*(?=[^\x00-\x7f])")

# 无 git 时的兜底扫描要跳过的目录（第三方 / 生成物）
_SKIP_PARTS = {"node_modules", ".venv", "dist", "build", ".git"}


def _repo_shell_scripts() -> list[Path]:
    """仓库里**被 git 跟踪的** shell 脚本。

    走 git ls-files 而不是 rglob：rglob 会把 .gitignore 掉的东西也扫进来（例如 .claude/
    下各人自己的小工具），等于拿仓库门禁去管别人机器上的私货，且脚本数会随各人工作树
    浮动。git 不可用时（源码包解压、没有 .git）退回 rglob + 跳过清单。
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(_ROOT), "ls-files", "-z", "*.sh", "*.bash"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
        tracked = [_ROOT / rel for rel in out.split("\0") if rel]
        # 只留工作树里真实存在的（git 记录里可能有刚被删还没提交的）
        found = sorted(p for p in tracked if p.is_file())
        if found:
            return found
    except (OSError, subprocess.SubprocessError):
        pass
    # 后缀要和上面的 ls-files 一致：兜底只扫 *.sh 的话，将来有人加 foo.bash 会在源码包
    # 场景下静默逃过门禁（不像"一个文件都没扫到"会被 test_shell_scripts_found 报红）。
    # 比对 parts 前先 relative_to：否则 _ROOT 自身的祖先目录名也参与匹配，仓库 checkout
    # 在某个叫 build/ 的目录下就会把一切都跳过。
    return sorted(
        p
        for pat in ("*.sh", "*.bash")
        for p in _ROOT.rglob(pat)
        if not _SKIP_PARTS.intersection(p.relative_to(_ROOT).parts)
    )


def test_shell_scripts_found() -> None:
    """先确认扫得到脚本，否则下面那条断言会因为"一个文件都没扫"而假绿。"""
    scripts = _repo_shell_scripts()
    assert len(scripts) >= 10, f"只扫到 {len(scripts)} 个脚本，路径推导可能坏了：{_ROOT}"
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
        "以下位置的变量紧跟非 ASCII 字符（中文 / emoji / 带音标字母都算），macOS 自带 "
        "bash 3.2 会把它连进变量名：set -u 下脚本炸在这行，没 set -u 则静默输出乱码。"
        "改成 ${VAR} 即可：\n  " + "\n  ".join(offenders)
    )
