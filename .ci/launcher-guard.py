#!/usr/bin/env python3
"""launcher 守卫：入库的签名启动器必须仍是双架构、关键 plist 字段未变。

miloco.app 是手工在 mac 上重编后入库的（见 launcher/src/miloco_launcher.c 头注释），
"源码与二进制保持同步" 这个隐性 invariant 此前没有任何机器约束。本脚本钉住其中
可确定性验证的部分（硬失败）：

1. 二进制是 Mach-O fat（cafebabe）且同时含 arm64 与 x86_64 —— 单架构重编会让另一
   架构的 mac 上 launchd 完全起不来；
2. Info.plist 可解析且 CFBundleIdentifier / Name / Executable 未变 —— bundle id
   决定 TCC 授权项的标签，换了用户得重新打勾。

改了 launcher/src 却没动 launcher/darwin 只**告警**；纯注释改动豁免 —— 那种改动
重编后字节完全相同，一律告警就成了每次都响的假阳性。硬失败会变成无法满足的红灯。

改动以 unified diff 从 stdin 传入；本地直接跑（stdin 是 tty）时跳过漂移告警。
"""

from __future__ import annotations

import plistlib
import struct
import sys
from pathlib import Path

_APP = Path(__file__).resolve().parent.parent / "launcher" / "darwin" / "miloco.app"
_BIN = _APP / "Contents" / "MacOS" / "miloco"
_PLIST = _APP / "Contents" / "Info.plist"

_FAT_MAGIC = 0xCAFEBABE
# fat header 里 cputype 的低 24 位 → 架构名
_REQUIRED_CPUS = {7: "x86_64", 12: "arm64"}
_EXPECTED_PLIST = {
    "CFBundleIdentifier": "com.xiaomi.miloco.backend",
    "CFBundleName": "miloco",
    "CFBundleExecutable": "miloco",
}


def check_fat_binary() -> list[str]:
    raw = _BIN.read_bytes()
    if len(raw) < 8:
        return [f"{_BIN} 太小，不是 Mach-O"]
    magic, nfat = struct.unpack(">II", raw[:8])
    if magic != _FAT_MAGIC:
        return [
            f"{_BIN} 不是 Mach-O fat 二进制（magic=0x{magic:08x}）——"
            "单架构包会让另一架构的 mac 上 launchd 起不来"
        ]
    # nfat 是文件里的字段，坏值会让下面的切片越界抛 struct.error —— 那正是本守卫要报的
    # 「二进制损坏」，不能变成一句 Python traceback。
    if nfat < 1 or 8 + nfat * 20 > len(raw):
        return [
            f"{_BIN} 的 fat header 声明 {nfat} 个架构，超出文件长度 —— 二进制已损坏"
        ]
    found = {
        struct.unpack(">I", raw[8 + i * 20 : 12 + i * 20])[0] & 0x00FFFFFF
        for i in range(nfat)
    }
    missing = [name for cpu, name in _REQUIRED_CPUS.items() if cpu not in found]
    return [f"{_BIN} 缺少架构：{', '.join(missing)}"] if missing else []


def check_plist() -> list[str]:
    try:
        data = plistlib.loads(_PLIST.read_bytes())
    except Exception as e:  # noqa: BLE001 — 任何解析失败都算守卫失败
        return [f"{_PLIST} 无法解析：{e}"]
    return [
        f"{_PLIST} 的 {key} 期望 {want!r}，实际 {data.get(key)!r}"
        for key, want in _EXPECTED_PLIST.items()
        if data.get(key) != want
    ]


def _is_comment_only_line(body: str) -> bool:
    """一行（已 strip）是否只可能是注释。

    两种"看着像注释的真代码"必须排除，放行它们会把真行为改动的告警吞掉：``*out = 1;``
    （解引用赋值，``*`` 打头）与 ``*/ execv(...);`` / ``/* c */ fork();``（块注释的
    开/收尾行后面还跟着代码）。
    """
    if not body or body.startswith("//"):
        return True
    if body.startswith(("/*", "*/")):
        # 块注释行：收尾符之后还有代码就是真改动
        tail = body.split("*/", 1)
        return len(tail) == 1 or not tail[1].strip()
    return body == "*" or (body.startswith("*") and body[1:2].isspace())


def parse_diff(stdin_text: str) -> tuple[set[str], bool]:
    """从 unified diff 解析出 (改动文件集, launcher/src 的改动是否纯注释)。

    文件路径**只认 ``diff --git`` 头行**：它对每个文件都输出（binary / 新增 / 删除 /
    改名都在内），而 ``+++ b/`` 对 binary 文件根本不出现（本仓的
    ``launcher/.gitattributes`` 把 vendored bundle 标成 binary，diff 只给一行
    "Binary files ... differ"）、对删除文件则是 ``+++ /dev/null``。只认 ``+++ b/``
    会让"改了 src + 在 mac 上重编入库"这个**完全正确**的流程被判成"没重编"，告警
    100% 误报 —— 而误报久了没人再看，等于守卫不存在。
    """
    files: set[str] = set()
    current = ""
    src_only_comments = True
    src_seen = False
    for line in stdin_text.splitlines():
        if line.startswith("diff --git "):
            # 形如 "diff --git a/X b/Y"；含空格的路径两侧带引号，去掉即可
            current = line.split(" b/", 1)[1].rstrip('"') if " b/" in line else ""
            if current:
                files.add(current)
            continue
        if line.startswith("+++ b/"):
            current = line[6:]
            files.add(current)
            continue
        if current.startswith("launcher/src/") and line[:1] in "+-":
            if line.startswith(("+++", "---")):
                continue
            src_seen = True
            if not _is_comment_only_line(line[1:].strip()):
                src_only_comments = False
    return files, (src_seen and src_only_comments)


def wants_drift_warning(diff: str) -> bool:
    """该 diff 是否该报"改了 src 却没重编入库"。

    纯注释豁免是必要的：只改 .c 注释时重编产物字节完全相同、``launcher/darwin`` 永远
    不会有 diff，若一律告警就成了每次都响的假阳性。
    """
    files, comment_only = parse_diff(diff)
    touched_src = any(p.startswith("launcher/src/") for p in files)
    touched_darwin = any(p.startswith("launcher/darwin/") for p in files)
    return touched_src and not touched_darwin and not comment_only


def read_stdin() -> str | None:
    """CI 传来的 diff；本地跑（tty）返回 None，只跳过漂移告警。"""
    if sys.stdin.isatty():
        return None
    return sys.stdin.read()


_DARWIN_BINARY = (
    "diff --git a/launcher/darwin/miloco.app/Contents/MacOS/miloco"
    " b/launcher/darwin/miloco.app/Contents/MacOS/miloco\n"
    "new file mode 100755\n"
    "Binary files /dev/null and"
    " b/launcher/darwin/miloco.app/Contents/MacOS/miloco differ\n"
)


def _src_diff(added: str, path: str = "launcher/src/miloco_launcher.c") -> str:
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,0 +2,1 @@\n{added}\n"
    )


# (名字, diff, 期望是否告警)
_SELF_TEST_CASES: list[tuple[str, str, bool]] = [
    (
        "vendored binary 对守卫可见（不可见则合法重编必误报）",
        _DARWIN_BINARY,
        False,
    ),
    (
        "改 src 行为 + 重编入库 binary → 不告警",
        _src_diff("+int x = 1;") + _DARWIN_BINARY,
        False,
    ),
    ("改 src 行为、没重编 → 告警", _src_diff("+int x = 1;"), True),
    ("只改 src 注释 → 不告警（重编后字节相同）", _src_diff("+// note"), False),
    ("`*out = 1;` 是解引用赋值，不是注释续行", _src_diff("+*out = 1;"), True),
    ("`*/ execv(...);` 收尾行带代码 → 告警", _src_diff("+*/ execv(path, argv);"), True),
    (
        "删除 src 下的源文件 → 告警（+++ 是 /dev/null，只有 diff --git 认得它）",
        "diff --git a/launcher/src/old.c b/launcher/src/old.c\n"
        "deleted file mode 100644\n--- a/launcher/src/old.c\n+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n-int x;\n",
        True,
    ),
]


def self_test() -> int:
    """把守卫的判断钉在合成 diff 上（``.ci/`` 没有 pytest 基建，故自带，CI 每轮跑）。

    这一层被**同一个失效模式咬过两次**：先是纯注释通配过宽、把 ``*out = 1;`` 这类真代码
    当成注释续行；后是文件识别只认 ``+++ b/``、而 vendored bundle 被 .gitattributes 标成
    binary 后压根没有那一行 —— 于是"改 src + 重编入库"这个完全正确的流程 100% 误报。
    两次都是"守卫静默失效/静默乱响"，靠人眼发现。样本因此常驻。
    """
    failed = 0
    for name, diff, expected in _SELF_TEST_CASES:
        got = wants_drift_warning(diff)
        if got != expected:
            print(f"::error::self-test 失败: {name}（期望告警={expected}，实际={got}）")
            failed += 1
    if not failed:
        print(f"launcher 守卫 self-test 通过（{len(_SELF_TEST_CASES)} 例）")
    return 1 if failed else 0


def main() -> int:
    if "--self-test" in sys.argv[1:]:
        return self_test()

    problems = check_fat_binary() + check_plist()
    for problem in problems:
        print(f"::error::{problem}")
    if problems:
        return 1

    diff = read_stdin()
    if diff is not None and wants_drift_warning(diff):
        print(
            "::warning::launcher/src 改了但 launcher/darwin 没动 —— 若改的是行为，"
            "需在 mac 上重编 + 重签后重新入库（步骤见 src/miloco_launcher.c 头注释）"
        )
    print("launcher 守卫通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
