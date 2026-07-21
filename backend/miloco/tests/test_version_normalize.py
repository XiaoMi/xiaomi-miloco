# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""scripts/version_normalize.py 的契约测试.

通过 subprocess 调用脚本(脚本在 scripts/ 下,不在任何 Python 包内),验证
raw CalVer → PEP440 / npm 的翻译与格式校验,不依赖跨目录 import。
"""

import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[3] / "scripts" / "version_normalize.py"
)


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "raw,target,expected",
    [
        ("2026.6.17", "pep440", "2026.6.17"),
        ("2026.6.17", "npm", "2026.6.17"),
        ("2026.6.17-beta.1", "pep440", "2026.6.17b1"),
        ("2026.6.17-beta.1", "npm", "2026.6.17-beta.1"),
        ("2026.6.17-alpha.2", "pep440", "2026.6.17a2"),
        ("2026.6.17-rc.1", "pep440", "2026.6.17rc1"),
        ("2026.12.1", "pep440", "2026.12.1"),
    ],
)
def test_normalize_ok(raw: str, target: str, expected: str) -> None:
    r = _run(raw, "--target", target)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == expected


@pytest.mark.parametrize(
    "bad",
    [
        "2026.06.17",  # 前导零（月）
        "2026.6.07",  # 前导零（日）
        "v2026.6.17",  # 带 v 前缀
        "2026.6",  # 缺段
        "2026.6.17-beta",  # 预发布缺编号
        "2026.6.17-beta.0",  # 预发布编号从 1 起
        "1.0.0",  # 非 CalVer
    ],
)
def test_validate_rejects_illegal(bad: str) -> None:
    r = _run(bad, "--validate")
    assert r.returncode == 1, f"应拒绝 {bad!r}，实际 stdout={r.stdout!r}"


def test_validate_accepts_legal() -> None:
    assert _run("2026.6.17", "--validate").returncode == 0
    assert _run("2026.6.17-beta.3", "--validate").returncode == 0


@pytest.mark.parametrize(
    "pep,expected",
    [
        # 本地 dev（no-guess-dev + node-and-date）：post/dev 落 semver prerelease 段，
        # local(+g<hash>[.d<date>]) 落 build metadata 段，base 保留真实 tag。
        (
            "2026.7.3.post1.dev117+ge88bd38de.d20260717",
            "2026.7.3-post1.dev117+ge88bd38de.d20260717",  # dirty
        ),
        (
            "2026.7.3.post1.dev117+ge88bd38de",
            "2026.7.3-post1.dev117+ge88bd38de",  # clean
        ),
        ("2026.6.17", "2026.6.17"),  # 正式版：无后缀，原样
        ("0.0.0", "0.0.0"),  # 无 tag fallback
    ],
)
def test_pep2semver(pep: str, expected: str) -> None:
    r = _run(pep, "--pep2semver")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == expected
