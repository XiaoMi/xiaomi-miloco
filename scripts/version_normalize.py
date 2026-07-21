#!/usr/bin/env python3
"""版本号规范化单一源：raw CalVer → PEP440 / npm，附格式校验。

raw 形如：2026.6.17 | 2026.6.17-beta.1 | 2026.6.17-alpha.2 | 2026.6.17-rc.1
- npm 形式 = raw 原样（已是合法 semver 三段 + prerelease）
- PEP440 形式：正式版同 raw；预发布 alpha→a / beta→b / rc→rc，去分隔符（-beta.1 → b1）

build.sh 与 .github/workflows/release.yml 共用本脚本，确保规范化逻辑只有一处，
不再因两处正则分叉而漂移。

月/日仅校验非前导零与形态，不做日历合法性强校验（2026.13.45 也通过）——刻意决策，
对齐 OpenClaw、避免过度工程；版本号的"日期"本质是单调递增标签。

用法:
  python3 version_normalize.py <raw> --target pep440|npm   # 输出规范化串
  python3 version_normalize.py <raw> --validate            # 仅校验，非法 exit 1
  python3 version_normalize.py <pep440> --pep2semver       # PEP440 dev 版 → 合法 semver（本地 npm 装配占位）
"""

import re
import sys

# 月/日不补零（拒绝前导零）；预发布段可选
_RE = re.compile(r"^(\d{4})\.([1-9]\d?)\.([1-9]\d?)(?:-(alpha|beta|rc)\.([1-9]\d*))?$")
_PEP = {"alpha": "a", "beta": "b", "rc": "rc"}


def parse(raw: str) -> "re.Match[str]":
    m = _RE.match(raw)
    if not m:
        sys.exit(
            f"非法版本号: {raw!r}（要求 YYYY.M.D 且月/日不补零，预发布形如 -beta.1）"
        )
    return m


def to_npm(raw: str) -> str:
    parse(raw)
    return raw


def to_pep440(raw: str) -> str:
    m = parse(raw)
    base = f"{m.group(1)}.{m.group(2)}.{m.group(3)}"
    if not m.group(4):
        return base
    return f"{base}{_PEP[m.group(4)]}{m.group(5)}"


# 完整 PEP440（含 pre/post/dev/local）→ semver：本地 dev 的 npm 包版本用它，让 web / openclaw
# 的 npm 版本也带真实 base（如 2026.7.3-post1.dev117+ge88bd38），而非丢了 base 的 0.0.0 占位。
# 注：semver 无 post 概念，post/dev 一并落到 prerelease 段（'-' 引导、'.' 分隔），故该 dev 版
# 在 semver 里排序 < 正式版；本项目 npm 包（web 不发布、openclaw 走本地 tgz 装）不依赖 npm 版本
# 排序，可接受。仅用于本地 dev；发布走 to_npm（raw CalVer 原样）。
_PEP_FULL = re.compile(
    r"^(?P<rel>\d+\.\d+\.\d+)"
    r"(?P<pre>(?:a|b|rc)\d+)?"
    r"(?P<post>\.post\d+)?"
    r"(?P<dev>\.dev\d+)?"
    r"(?:\+(?P<local>.+))?$"
)


def pep440_to_semver(pep: str) -> str:
    m = _PEP_FULL.match(pep)
    if not m:
        sys.exit(f"无法转 semver 的 PEP440 版本: {pep!r}")
    ids = []
    if m.group("pre"):
        ids.append(m.group("pre"))
    if m.group("post"):
        ids.append(m.group("post")[1:])  # 去掉前导 '.'
    if m.group("dev"):
        ids.append(m.group("dev")[1:])
    sem = m.group("rel")
    if ids:
        sem += "-" + ".".join(ids)
    if m.group("local"):
        sem += "+" + m.group("local")
    return sem


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        sys.exit("用法: version_normalize.py <raw> --target pep440|npm | --validate | --pep2semver")
    raw = argv[1]
    if "--pep2semver" in argv:
        print(pep440_to_semver(raw))
    elif "--validate" in argv:
        parse(raw)
        print(f"✓ {raw}")
    elif "--target" in argv:
        target = argv[argv.index("--target") + 1]
        if target == "pep440":
            print(to_pep440(raw))
        elif target == "npm":
            print(to_npm(raw))
        else:
            sys.exit(f"未知 target: {target!r}（pep440|npm）")
    else:
        sys.exit("缺少 --target pep440|npm 或 --validate")


if __name__ == "__main__":
    main(sys.argv)
