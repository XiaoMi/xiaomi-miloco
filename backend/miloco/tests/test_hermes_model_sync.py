# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""行为体检：install-hermes.sh step 4.7 必须「按内容同步」而不是「已存在就跳过」。

从真脚本里抠出 4.7 的同步与收尾段跑，而不是在测试里复制一份实现——复制会造出第二份
各自漂走的副本，正是本测试要防的那类问题。抠取的起止是代码行而非注释，改动那两行会
让 ``_section`` 断言先红。

不覆盖 ``MODEL_SRC`` 的解析（fork 源目录优先、包内 fallback）：抠取从解析之后开始，
源目录由桩直接给。
"""

from __future__ import annotations

import json
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "plugins" / "hermes" / "install-hermes.sh"

# 陪衬文件：只要不在必需清单里就行，用来验证「目录里有 .onnx」不足以放行。
_NOT_REQUIRED = ("some-other-model.onnx", "some-tokenizer.json")

# 与真脚本头部对齐：locale 钉死 UTF-8、解释器带 python 兜底。抠段来跑的前提是环境等价，
# 差一项就会让失败红在环境上而不是被测行为上。
_PRELUDE = textwrap.dedent(
    """
    set -euo pipefail
    export LANG=C.UTF-8 LC_ALL=C.UTF-8
    info() { echo "[i] $*"; }
    warn() { echo "[w] $*"; }
    err()  { echo "[e] $*" >&2; }
    mark_done() { :; }
    MILOCO_HOME="$1"
    MODEL_SRC="$2"
    PYTHON="$(command -v python3 || command -v python)"
    """
)


def _section() -> str:
    text = _SCRIPT.read_text(encoding="utf-8")
    m = re.search(
        r'^\[ -d "\$MILOCO_HOME/models" \].*?^mark_done 4\.7$', text, re.S | re.M
    )
    assert m, f"{_SCRIPT} 里找不到 step 4.7 的同步段（起止那两行变了？）"
    return m.group(0)


def _required_models() -> tuple[str, ...]:
    """清单读被测段自己声明的那份——它与 MODELS 一致由 test_hermes_required_models 守。

    在这里改用 MODELS 派生的话，「MODELS 多一个必需模型」会让本文件里所有
    ``returncode == 0`` 的用例一起红，而要改的其实是 shell 那份手抄清单。
    """
    m = re.search(r'^REQUIRED_MODELS="([^"]*)"$', _section(), re.M)
    assert m, "抠出的 4.7 段里找不到 REQUIRED_MODELS 赋值"
    return tuple(m.group(1).split())


_REQUIRED = _required_models()


def _run(
    tmp_path: Path,
    src: dict[str, str] | None,
    dest: dict[str, str],
    config: dict | None = None,
) -> subprocess.CompletedProcess[str]:
    """src=None 表示源目录不存在。返回跑完 4.7 段的结果。"""
    home = tmp_path / "home"
    (home / "models").mkdir(parents=True)
    for rel, content in dest.items():
        p = home / "models" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    if config is not None:
        (home / "config.json").write_text(json.dumps(config), encoding="utf-8")

    src_dir = tmp_path / "src"
    if src is not None:
        src_dir.mkdir()
        for rel, content in src.items():
            p = src_dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)

    script = tmp_path / "seg.sh"
    script.write_text(_PRELUDE + _section(), encoding="utf-8")
    return subprocess.run(
        ["bash", str(script), str(home), str(src_dir)],
        capture_output=True,
        text=True,
    )


@pytest.fixture
def models_dir(tmp_path: Path) -> Path:
    return tmp_path / "home" / "models"


def test_content_differs_overwrites(tmp_path: Path, models_dir: Path) -> None:
    """内容不同必须覆盖——判据换回「目标已存在就跳过」时这条红。"""
    r = _run(tmp_path, {m: "NEW" for m in _REQUIRED}, {m: "OLD" for m in _REQUIRED})
    assert r.returncode == 0, r.stderr
    assert all((models_dir / m).read_text() == "NEW" for m in _REQUIRED)


def test_content_same_not_rewritten(tmp_path: Path, models_dir: Path) -> None:
    """内容一致不重写——比对换成无条件 cp 时这条红。"""
    same = {m: "SAME" for m in _REQUIRED}
    r = _run(tmp_path, same, same)
    assert r.returncode == 0, r.stderr
    assert "写入 0 个" in r.stdout


def test_missing_required_model_exits_1(tmp_path: Path) -> None:
    """缺必需模型必须以 1 退出并点名——收尾闸的循环被删掉时这条红。"""
    present, absent = _REQUIRED[0], _REQUIRED[-1]
    r = _run(tmp_path, {present: "NEW", **{m: "NEW" for m in _NOT_REQUIRED}}, {})
    assert r.returncode == 1
    assert absent in r.stderr


def test_non_required_files_alone_are_not_enough(tmp_path: Path) -> None:
    """目录里只有非必需的 .onnx 也要退出 1——判据换回「有任意 .onnx」时这条红。"""
    r = _run(tmp_path, {m: "NEW" for m in _NOT_REQUIRED}, {})
    assert r.returncode == 1
    assert all(m in r.stderr for m in _REQUIRED)


def test_zero_length_required_model_counts_as_missing(tmp_path: Path) -> None:
    """长度为 0 的必需模型算缺失——``-s`` 换成 ``-f`` 时这条红。"""
    empty = _REQUIRED[0]
    dest = {m: "OK" for m in _REQUIRED}
    dest[empty] = ""
    r = _run(tmp_path, None, dest)
    assert r.returncode == 1
    assert empty in r.stderr


def test_recurses_subdirs_and_hidden_files(tmp_path: Path, models_dir: Path) -> None:
    """子目录与隐藏文件一并同步——``find`` 换回只扫顶层的 glob 时这条红。"""
    r = _run(
        tmp_path,
        {**{m: "NEW" for m in _REQUIRED}, "sub/extra.bin": "X", ".hidden": "H"},
        {},
    )
    assert r.returncode == 0, r.stderr
    assert (models_dir / "sub" / "extra.bin").read_text() == "X"
    assert (models_dir / ".hidden").read_text() == "H"


def test_symlinked_source_is_followed(tmp_path: Path, models_dir: Path) -> None:
    """源目录里的 symlink 要跟随——``find`` 少了 ``-L`` 时这条红。"""
    real = tmp_path / "real"
    real.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    for m in _REQUIRED:
        (real / m).write_text("R")
        (src / m).symlink_to(real / m)
    home = tmp_path / "home"
    (home / "models").mkdir(parents=True)
    script = tmp_path / "seg.sh"
    script.write_text(_PRELUDE + _section(), encoding="utf-8")
    r = subprocess.run(
        ["bash", str(script), str(home), str(src)], capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr
    assert all((models_dir / m).read_text() == "R" for m in _REQUIRED)


def test_stale_tmp_files_cleaned(tmp_path: Path, models_dir: Path) -> None:
    """上次被打断留下的 ``*.tmp.<pid>`` 要清掉，真模型不动——清理那行被删时这条红。"""
    r = _run(
        tmp_path,
        {m: "NEW" for m in _REQUIRED},
        {f"{_REQUIRED[0]}.tmp.1234": "JUNK", "sub/x.onnx.tmp.99": "JUNK"},
    )
    assert r.returncode == 0, r.stderr
    assert not (models_dir / f"{_REQUIRED[0]}.tmp.1234").exists()
    assert not (models_dir / "sub" / "x.onnx.tmp.99").exists()
    assert (models_dir / _REQUIRED[0]).read_text() == "NEW"


def test_stale_tmp_cleaned_even_without_source(
    tmp_path: Path, models_dir: Path
) -> None:
    """源目录找不到时也清残留——清理挪进同步分支里面时这条红。"""
    r = _run(
        tmp_path,
        None,
        {**{m: "OLD" for m in _REQUIRED}, f"{_REQUIRED[-1]}.tmp.77": "JUNK"},
    )
    assert r.returncode == 0, r.stderr
    assert not (models_dir / f"{_REQUIRED[-1]}.tmp.77").exists()
    assert (models_dir / _REQUIRED[-1]).read_text() == "OLD"


def test_successful_sync_leaves_no_tmp(tmp_path: Path, models_dir: Path) -> None:
    """正常同步完不留临时文件——``mv`` 那步被删时这条红。"""
    r = _run(tmp_path, {m: "NEW" for m in _REQUIRED}, {})
    assert r.returncode == 0, r.stderr
    assert not list(models_dir.glob("*.tmp.*"))


def test_models_elsewhere_warns_instead_of_exiting(tmp_path: Path) -> None:
    """生效目录在别处时只警告不中止——那道分支被删时这条红（会变成 exit 1）。"""
    r = _run(tmp_path, None, {}, config={"directories": {"models": "/data/models"}})
    assert r.returncode == 0, r.stderr
    assert "/data/models" in r.stdout


def test_models_elsewhere_warned_even_when_default_dir_is_complete(
    tmp_path: Path,
) -> None:
    """默认目录齐了也要提醒生效目录在别处——提醒挪回缺模型分支里面时这条红。"""
    r = _run(
        tmp_path,
        {m: "NEW" for m in _REQUIRED},
        {},
        config={"directories": {"models": "/data/models"}},
    )
    assert r.returncode == 0, r.stderr
    assert "/data/models" in r.stdout
    assert "还缺" not in r.stdout


@pytest.mark.parametrize("configured", ["models", "ABS_DEFAULT"])
def test_configured_default_dir_still_hard_fails(
    tmp_path: Path, configured: str
) -> None:
    """配的就是默认目录（相对或绝对写法）时照旧硬失败——判据只看字段非空时这条红。"""
    if configured == "ABS_DEFAULT":
        configured = str(tmp_path / "home" / "models")
    r = _run(tmp_path, None, {}, config={"directories": {"models": configured}})
    assert r.returncode == 1
    assert all(m in r.stderr for m in _REQUIRED)
