# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""scripts/fetch_models.py 的契约测试.

模型不进 git，由该脚本按 scripts/models.lock.json 锁定的 sha256 从 Release 拉取
（见 scripts/publish_models.sh）。脚本在 scripts/ 下、不在任何 Python 包内，
故一律用 subprocess 调，和 test_version_normalize.py 一个路子。

用 file:// 当"Release 源"：不联网、无外部依赖，同时真的走完
「拼 URL → 流式下载 → sha256 校验 → 原子改名」全链路。
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "scripts" / "fetch_models.py"
_REAL_LOCK = _ROOT / "scripts" / "models.lock.json"
_MANIFEST = _ROOT / "scripts" / "manifest.json"

# 假模型内容：只要"有字节、sha 能算"就够，脚本对格式无任何假设。
_REQUIRED = b"pretend this is det_4C.onnx" * 64
_OPTIONAL = b"pretend this is silero_vad.onnx" * 32


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


@pytest.fixture
def fake_release(tmp_path: Path) -> tuple[Path, Path, Path]:
    """造一个 file:// 的假 Release + 指向它的 lock，返回 (lock, 源目录, 目标目录)。"""
    src = tmp_path / "release"
    src.mkdir()
    (src / "req.onnx").write_bytes(_REQUIRED)
    (src / "opt.onnx").write_bytes(_OPTIONAL)

    lock = tmp_path / "models.lock.json"
    lock.write_text(
        json.dumps(
            {
                "release_tag": "models",
                "base_url": src.as_uri(),
                "mirrors": [],
                "files": [
                    {
                        "name": "req.onnx",
                        "size": len(_REQUIRED),
                        "sha256": _sha(_REQUIRED),
                        "required": True,
                        "desc": "必需",
                    },
                    {
                        "name": "opt.onnx",
                        "size": len(_OPTIONAL),
                        "sha256": _sha(_OPTIONAL),
                        "required": False,
                        "desc": "可选",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return lock, src, tmp_path / "dest"


# ─── 下载 / 校验主路径 ────────────────────────────────────────────────────────


def test_fetch_downloads_and_verifies(fake_release: tuple[Path, Path, Path]) -> None:
    lock, _src, dest = fake_release
    r = _run("--lock", str(lock), "--dest", str(dest))
    assert r.returncode == 0, r.stderr
    assert (dest / "req.onnx").read_bytes() == _REQUIRED
    assert (dest / "opt.onnx").read_bytes() == _OPTIONAL
    # 原子改名的中间态不许留下（否则下次 Range 续传会从脏文件接着写）
    assert list(dest.glob("*.part")) == []


def test_second_run_is_offline(fake_release: tuple[Path, Path, Path]) -> None:
    """已就绪的文件只做 sha256 校验、不再取源——把源删掉仍应成功。"""
    lock, src, dest = fake_release
    assert _run("--lock", str(lock), "--dest", str(dest)).returncode == 0

    for f in src.iterdir():
        f.unlink()
    r = _run("--lock", str(lock), "--dest", str(dest))
    assert r.returncode == 0, r.stderr
    assert "已就绪" in r.stderr


def test_corrupted_local_file_is_redownloaded(
    fake_release: tuple[Path, Path, Path],
) -> None:
    """本地内容被改坏（size 相同、hash 不同）也要认出来并重下。"""
    lock, _src, dest = fake_release
    assert _run("--lock", str(lock), "--dest", str(dest)).returncode == 0

    tampered = bytes(len(_REQUIRED))  # 等长全零
    (dest / "req.onnx").write_bytes(tampered)
    r = _run("--lock", str(lock), "--dest", str(dest))
    assert r.returncode == 0, r.stderr
    assert (dest / "req.onnx").read_bytes() == _REQUIRED


def test_base_url_env_overrides_lock(
    fake_release: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """MILOCO_MODELS_BASE_URL 优先于 lock 的 base_url（内网镜像 / 离线源）。"""
    lock, src, dest = fake_release
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    for f in src.iterdir():
        (mirror / f.name).write_bytes(f.read_bytes())
        f.unlink()  # 掐掉 lock 里的源，逼它只能走 env

    r = _run(
        "--lock",
        str(lock),
        "--dest",
        str(dest),
        env={"MILOCO_MODELS_BASE_URL": mirror.as_uri()},
    )
    assert r.returncode == 0, r.stderr
    assert (dest / "req.onnx").read_bytes() == _REQUIRED


def test_dest_env_is_honored(fake_release: tuple[Path, Path, Path]) -> None:
    lock, _src, dest = fake_release
    r = _run("--lock", str(lock), env={"MILOCO_MODELS_DEST": str(dest)})
    assert r.returncode == 0, r.stderr
    assert (dest / "req.onnx").is_file()


# ─── 失败语义：必需 vs 可选、strict ──────────────────────────────────────────


def test_sha_mismatch_fails_and_leaves_no_part(
    fake_release: tuple[Path, Path, Path],
) -> None:
    """源内容与 lock 不符 → exit 1，且不许把坏文件落到最终路径。"""
    lock, src, dest = fake_release
    (src / "req.onnx").write_bytes(b"wrong bytes")

    r = _run("--lock", str(lock), "--dest", str(dest), "--only", "req.onnx")
    assert r.returncode == 1
    assert "sha256 不符" in r.stderr
    assert not (dest / "req.onnx").exists()
    assert list(dest.glob("*.part")) == []


def test_missing_optional_is_degraded_not_fatal(
    fake_release: tuple[Path, Path, Path],
) -> None:
    """可选模型拉不到只降级（与 resource_validator 的语义一致），必需模型照常就绪。"""
    lock, src, dest = fake_release
    (src / "opt.onnx").unlink()

    r = _run("--lock", str(lock), "--dest", str(dest))
    assert r.returncode == 0, r.stderr
    assert (dest / "req.onnx").is_file()
    assert not (dest / "opt.onnx").exists()
    assert "降级" in r.stderr


def test_strict_makes_optional_fatal(fake_release: tuple[Path, Path, Path]) -> None:
    """打 release tarball 用 --strict：终端用户要拿到完整能力，缺可选也算失败。"""
    lock, src, dest = fake_release
    (src / "opt.onnx").unlink()

    r = _run("--lock", str(lock), "--dest", str(dest), "--strict")
    assert r.returncode == 1
    assert "opt.onnx" in r.stderr


def test_required_only_skips_optional(fake_release: tuple[Path, Path, Path]) -> None:
    lock, src, dest = fake_release
    (src / "opt.onnx").unlink()  # 真跳过的话，源里没有也不该有任何抱怨

    r = _run("--lock", str(lock), "--dest", str(dest), "--required-only")
    assert r.returncode == 0, r.stderr
    assert (dest / "req.onnx").is_file()
    assert not (dest / "opt.onnx").exists()


# ─── --check（只校验不下载）────────────────────────────────────────────────


def test_check_reports_missing_without_downloading(
    fake_release: tuple[Path, Path, Path],
) -> None:
    lock, _src, dest = fake_release
    dest.mkdir()

    r = _run("--lock", str(lock), "--dest", str(dest), "--check")
    assert r.returncode == 1
    assert not (dest / "req.onnx").exists(), "--check 不许下载"

    assert _run("--lock", str(lock), "--dest", str(dest)).returncode == 0
    assert _run("--lock", str(lock), "--dest", str(dest), "--check").returncode == 0


def test_check_optional_only_needs_strict_to_fail(
    fake_release: tuple[Path, Path, Path],
) -> None:
    lock, _src, dest = fake_release
    assert _run("--lock", str(lock), "--dest", str(dest)).returncode == 0
    (dest / "opt.onnx").unlink()

    assert _run("--lock", str(lock), "--dest", str(dest), "--check").returncode == 0
    r = _run("--lock", str(lock), "--dest", str(dest), "--check", "--strict")
    assert r.returncode == 1
    assert "opt.onnx" in r.stderr


# ─── 用法错误：exit 2（与"模型缺失"的 exit 1 区分开）─────────────────────


def test_unknown_only_is_usage_error(fake_release: tuple[Path, Path, Path]) -> None:
    lock, _src, dest = fake_release
    r = _run("--lock", str(lock), "--dest", str(dest), "--only", "nope.onnx")
    assert r.returncode == 2
    assert "nope.onnx" in r.stderr


def test_bad_lock_is_usage_error(tmp_path: Path) -> None:
    bad = tmp_path / "broken.json"
    bad.write_text("{not json", encoding="utf-8")
    assert _run("--lock", str(bad), "--dest", str(tmp_path / "d")).returncode == 2
    assert _run("--lock", str(tmp_path / "missing.json")).returncode == 2


def test_empty_source_list_is_usage_error(tmp_path: Path) -> None:
    """lock 没有可用源时要明确报错，而不是"0 个文件全部就绪"式的假成功。"""
    lock = tmp_path / "models.lock.json"
    lock.write_text(
        json.dumps({"base_url": "", "mirrors": [], "files": [
            {"name": "x.onnx", "size": 1, "sha256": _sha(b"x"), "required": True}
        ]}),
        encoding="utf-8",
    )
    r = _run("--lock", str(lock), "--dest", str(tmp_path / "d"))
    assert r.returncode == 2


# ─── 仓库内 lock 自身的体检 ─────────────────────────────────────────────────


def test_real_lock_is_wellformed() -> None:
    """scripts/models.lock.json 是模型的唯一真源，字段缺一个就整条链路失效。"""
    lock = json.loads(_REAL_LOCK.read_text(encoding="utf-8"))

    assert lock["release_tag"]
    assert lock["base_url"].startswith("https://")
    assert lock["release_tag"] in lock["base_url"], "base_url 应指向 release_tag 对应的 Release"
    assert isinstance(lock.get("mirrors"), list)
    for m in lock["mirrors"]:
        assert m.startswith("https://"), m
        assert m.endswith("/" + lock["release_tag"]), f"{m} 未指向 {lock['release_tag']} tag"

    names = set()
    for spec in lock["files"]:
        assert spec["name"] not in names, f"lock 里 {spec['name']} 重复"
        names.add(spec["name"])
        assert spec["size"] > 0
        assert len(spec["sha256"]) == 64, spec["name"]
        int(spec["sha256"], 16)  # 必须是十六进制
        assert isinstance(spec["required"], bool)
        assert spec.get("desc")

    required = {s["name"] for s in lock["files"] if s["required"]}
    # 这两个是感知主链路（检测 + ReID）的硬依赖，见 perception/engine/resource_validator.py
    assert {"det_4C.onnx", "human_body_reid_v2.onnx"} <= required


def test_lock_sources_match_manifest_sites() -> None:
    """lock 的 base_url + mirrors 必须与 scripts/manifest.json 的 download.sites 同源。

    终端用户下大包走 manifest.sites（install.py），开发者/CI 下模型走 lock —— 两份清单
    指的是同一批 GitHub 加速镜像。这里钉死映射关系：加了新镜像只改 manifest 会漏掉模型
    下载，国内网络下就又变成"只能直连 github"。
    """
    lock = json.loads(_REAL_LOCK.read_text(encoding="utf-8"))
    sites = json.loads(_MANIFEST.read_text(encoding="utf-8"))["download"]["sites"]
    tag = lock["release_tag"]

    expected = [f"{s.rstrip('/')}/{tag}" for s in sites]
    assert [lock["base_url"], *lock["mirrors"]] == expected, (
        "lock 的下载源与 manifest.json 的 download.sites 不一致，"
        "改了一边就同步另一边（顺序也要一致：直连优先，镜像兜底）"
    )
