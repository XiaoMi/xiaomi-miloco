# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""仓库体检：shell 脚本里变量展开的两条硬约束。

两条都是"变量插值印出来会被吃掉一截"，共用同一份扫描范围（见 `_repo_shell_scripts`）——
放在同一个文件里，是为了不让"哪些脚本受管"有两个会各自漂移的答案。

1. 紧跟非 ASCII 字符的展开必须写花括号（下面这段）；
2. 印给用户照抄的命令，里面的路径必须转义（见 `test_printed_commands_quote_their_paths`）。

--- 约束 1 ---

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

**约束 1 的覆盖范围**：只扫 git 跟踪的 `*.sh` / `*.bash`。以下不在范围内 ——

* `.github/workflows/` 的内联 `run:` 块：跑在 ubuntu runner 的 bash 5 上，不受影响
  （仓库目前没有 macos runner；哪天加了就得把那些 run: 块一起纳管）；
* Python / TS 里的 shell 字符串（`subprocess(..., shell=True)`、`bash -c "…"` 字面量）：
  macOS 的 /bin/sh 就是 bash 3.2 的 posix 模式，实测同样中招，所以它们理论上在射程内，
  只是当前全仓 0 处，没为此加扫描。

--- 约束 2 ---

判据与边界都写在 :func:`test_printed_commands_quote_their_paths` 上。与约束 1 不同，它
和 bash 版本无关（任何 shell 的分词都一样），只是同样只扫这批脚本：workflow 的 run:
块印的东西进的是 CI 日志，没人照抄。
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


# ---- 约束 2：印出来让人照抄的命令，路径必须转义 --------------------------------

# `$(_q "…")` 整段挖掉再找裸变量：不挖的话它自己内部的 "$PYTHON" 会被当成违规，
# 已经修好的写法反而长红。
_QUOTED_SPAN = re.compile(r'\$\(_q "[^"]*"\)')

# 判据拆成两半，两半都命中才算"这行印的是一条命令"：
#   · 启动器：解释器/脚本路径变量，或字面的 bash / sh / python3
#   · 命令样：一个 --flag 或一个 *.py / *.sh 文件名
# 只要前一半的话，`info "Python 解释器：$PYTHON"`、`warn "手动修：${MILOCO_HOME}/
# config.json::server.python_bin = <路径>"`（"python 路径"里的 "python "也算启动器）
# 这类**只报告、不让人粘**的行会被判违规——而它们的正确写法恰恰不是套 _q，门禁逼出来的
# 会是个错的修法，比不设门禁更坏。加上后一半，这两行连同 `echo "运行 bash 脚本 $NAME"`
# 一起放行，而四处真实的可粘命令一处不漏。
_LAUNCHER = re.compile(r"\$\{?(?:PYTHON|FETCH_MODELS)\}?\b|\b(?:bash|sh|python3?)\s+")
_COMMANDISH = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*|\S+\.(?:py|sh)\b")

_VAR = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")

# 颜色/格式常量不是路径，插到哪儿都不会被分词。
_FORMAT_VARS = {"Y", "N", "G", "R", "B", "C", "NC", "RED", "GREEN", "YELLOW", "BLUE"}

# 只看"往终端打字"的那些行。赋值、真正的调用不在此列——真调用里的 "$VAR" 有双引号保护，
# 本来就不会被分词；出事的只有印成字符串给人复制的那一份。
_EMITTER = re.compile(r"^\s*(?:warn|info|err|log|note|echo|printf)\b")


def _unquoted_printed_commands(text: str) -> list[tuple[int, str]]:
    """文本里"印了一条可粘命令、而命令里的路径是裸变量"的行，(行号从 1 起, 原文)。

    抽成函数是为了能拿合成文本喂它——判据的两侧边界只能靠反例钉，而真实仓库里不存在
    反例（存在就已经红了）。
    """
    out: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#") or not _EMITTER.match(line):
            continue
        if not (_LAUNCHER.search(line) and _COMMANDISH.search(line)):
            continue
        rest = _QUOTED_SPAN.sub("", line)
        if any(v not in _FORMAT_VARS for v in _VAR.findall(rest)):
            out.append((lineno, line.strip()))
    return out


def test_printed_commands_quote_their_paths() -> None:
    """脚本印给用户照抄的命令，里面的路径必须过 `_q`（printf %q），不能裸插值。

    这几个变量装的都是**用户机器上**的路径：`$PYTHON` 由 venv 探测得到、`$FETCH_MODELS`
    由脚本自身位置推导、`$MILOCO_HOME` 来自 `$HOME`、`$HERE` 来自 `$BASH_SOURCE`。带空格
    在 macOS 上是常态（用户名带空格的 home、`~/Library/Mobile Documents/`）。

    裸插值印出来的那条命令，用户粘回终端时 shell 会从空格处切词：

        补齐：/Users/li ming/.venv/bin/python /Users/li ming/fetch_models.py --strict \\
              --dest /Users/li ming/.openclaw/miloco/models

    轻则第一个 token 变成 `/Users/li` 直接报 No such file；重的那种更难查——解释器路径
    侥幸没空格而 `--dest` 只收到半截（`/Users/li`），78MB 落到一个错的目录、命令还退 0，
    用户以为补齐了，重跑安装照旧提示缺模型，而手里唯一的线索就是这条看着没问题的命令。

    `printf %q` 而不是手写引号：对不含特殊字符的路径原样输出（常见情况零噪声，本仓库
    `local-ci.sh` 那处就是），有空格时才转义，且 macOS 自带的 bash 3.2 就支持——这些
    脚本的 shebang 是 `env bash`，在 macOS 上跑到的正是 3.2。

    这个类在本 PR 的 review 里被抓到两轮（先 `fetch_models.py` 印的补齐命令，再
    `install-hermes.sh` 印的三条），且顺着类扫一遍还能再翻出 review 没提的第四处
    （失败陷阱里那句 `bash $HERE/install-hermes.sh`）——人扫会漏，所以留一道门禁。
    """
    offenders: list[str] = []
    for path in _repo_shell_scripts():
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(_ROOT)
        offenders += [f"{rel}:{n}: {ln}" for n, ln in _unquoted_printed_commands(text)]

    assert not offenders, (
        "以下位置印了一条给人照抄的命令，而命令里的路径是裸变量：路径带空格时，用户粘回"
        "终端会被 shell 切词（第一个 token 变成 /Users/li 报 No such file，或者 --dest "
        '只收到半截、文件落到错的目录还退 0）。改成 $(_q "$VAR")：\n  '
        + "\n  ".join(offenders)
        + "\n如果这行其实**不是**让人粘的命令（只是报告一个路径），那就别让它同时长得像"
        "命令——把 --flag / *.py / *.sh 从文案里去掉，本门禁自然放行。"
    )


# 每对样本只差一件事，所以对应的断言分辨的就是那一件事。
_PRINTED_CASES = (
    (True, 'warn "补齐：$PYTHON $FETCH_MODELS --strict --dest $MILOCO_HOME/models"'),
    (True, 'echo "修复：重跑 bash $HERE/install-hermes.sh（幂等，自动 recover）"'),
    (False, 'warn "补齐：$(_q "$PYTHON") $(_q "$FETCH_MODELS") --strict"'),
    (False, 'info "Python 解释器：$PYTHON"'),
    (False, 'warn "手动修：${MILOCO_HOME}/config.json::server.python_bin = <python 路径>"'),
    (False, 'echo "运行 bash 脚本 $NAME 前先确认"'),
    (False, 'echo " 修法：先看 $HERE/INSTALL_KNOWN_ISSUES.md"'),
    (False, 'log "构建集不含 miloco（--packages=${PACKAGES}），跳过模型打包"'),
    (False, 'FIX="$PYTHON $FETCH_MODELS --strict --dest $MILOCO_HOME/models"'),
)


def test_printed_command_check_separates_commands_from_prose() -> None:
    """两个方向都钉死：真·可粘命令必判违规，只提路径的文案必放行。

    上面那条契约只跑真实仓库，而真实仓库里两侧的反例都不存在（违规的已经修完，文案那侧
    存在就是红的），所以它一个字也证明不了判据的边界还在。而这条判据是**启发式**的，
    松一格就漏掉真实缺陷，紧一格就把一堆纯文案判成违规、逼出"给报告路径的行套 _q"这种
    错修法——两侧都得有反例压着。

    最后一条（`FIX="$PYTHON …"` 赋值）钉的是"只看打印行"这个前提：赋值里的 `"$VAR"`
    有双引号保护、不会被分词，把它算成违规等于要求给所有变量赋值套转义。
    """
    for want_bad, line in _PRINTED_CASES:
        got = bool(_unquoted_printed_commands(line))
        assert got == want_bad, (
            f"{'漏判' if want_bad else '误判'}：{line}\n"
            + (
                "这是一条要让人粘回终端的命令，路径带空格就会被切词，必须判违规。"
                if want_bad
                else "这行不是让人粘的命令，正确写法不是套 _q；判成违规会逼出错的修法。"
            )
        )
