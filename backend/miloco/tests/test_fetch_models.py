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
import http.server
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

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


def _clean_env() -> dict[str, str]:
    """继承环境里会改变本文件测试语义的变量，一律剥掉。

    两类，都只在**某些人的机器上**发作，所以不隔离的话表现为"CI 绿、我这儿红"：

    1. ``MILOCO_MODELS_*``：内网/离线开发者按 models/README.md 与 dev-guide 的建议
       export 了 MILOCO_MODELS_BASE_URL 之后，这一批测试会全线翻车 —— 该变量对下载源是
       **独占替换**，fixture 那个 file:// 假 Release 会被整体顶掉，测试转而真的去打公司
       镜像；既违反本文件"不联网"的契约，`test_empty_source_list_is_usage_error`
       这类断言退出码的用例语义还会直接反转。

    2. ``*_proxy`` / ``*_PROXY``：脚本用 urllib 默认 opener，它带 ProxyHandler()，
       于是 http://127.0.0.1:<port> 那几个用例（起本地 HTTP server 验截断续传 / 416）
       会把请求发给公司代理，而代理解析不了回环地址。实测：设上代理变量后正好
       test_truncated_response_keeps_part_for_resume、
       test_part_survives_process_exit_so_next_run_resumes、
       test_range_start_past_asset_end_discards_part_instead_of_looping 三条翻红。

    ``no_proxy=*`` 不是多余的兜底：光剥环境变量只能让 getproxies_environment() 变空，
    而 darwin 上 getproxies() 会接着落到 getproxies_macosx_sysconf() —— 在"系统设置里
    配了代理、shell 里没 export"的机器上照样中招。塞回一个 no_proxy=* 让环境这条路
    重新非空且旁路一切，两条路径就都封住了。

    注意隔离只做在测试侧，不动下载器：生产链路上代理是**必要**的（公司网 / 内网机器
    要靠它才够得着 GitHub），给下载器写死一个不带代理的 opener 会把这批人直接断网。
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("MILOCO_MODELS_") and not k.lower().endswith("_proxy")
    }
    env["no_proxy"] = "*"
    return env


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """跑 fetch_models.py，环境按 `_clean_env` 隔离。"""
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        env={**_clean_env(), **(env or {})},
    )


def _make_truncating_handler(
    body: bytes, *, sent: int
) -> type[http.server.BaseHTTPRequestHandler]:
    """造一个"声明 Content-Length 却只发一部分就关连接"的服务端。

    这是跨境链路上最常见的失败形态（干净 FIN 截断），也是 file:// 假 Release 造不出来的
    唯一一类 —— 所以这一条测试破例起个真 HTTP 服务，仍然只绑 127.0.0.1、不出本机。
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler 的约定命名
            start = 0
            rng = self.headers.get("Range", "")
            if rng.startswith("bytes="):
                start = int(rng.removeprefix("bytes=").split("-")[0] or 0)
            chunk = body[start:]
            self.send_response(206 if start else 200)
            self.send_header("Content-Length", str(len(chunk)))
            if start:
                self.send_header(
                    "Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}"
                )
            self.end_headers()
            self.wfile.write(chunk[:sent])  # 少发一截就撒手
            self.close_connection = True

        def log_message(self, *_args: object) -> None:
            pass  # 别把请求日志喷进 pytest 输出

    return Handler


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


def test_download_and_check_agree_on_a_self_contradictory_lock(tmp_path: Path) -> None:
    """lock 里 size 与 sha256 描述的不是同一份字节时，两条路径必须给同一个结论。

    下载落地只认 sha，``--check`` 若额外先比 size，CI 上紧挨着的那两步就会一绿一红：
    ``--dest X`` 打"校验通过"退 0，``--check --strict --dest X`` 立刻判"缺失或校验
    不通过"退 1，而报错给的修法正是刚跑成功的上一步 —— 重跑多少次都是同一个结果，
    文案还把人指向 Release 和网络，真正坏的是 lock 自己。
    """
    body = b"y" * 512
    src = tmp_path / "release"
    src.mkdir()
    (src / "m.onnx").write_bytes(body)

    lock = tmp_path / "models.lock.json"
    lock.write_text(
        json.dumps(
            {
                "base_url": src.as_uri(),
                "mirrors": [],
                "files": [
                    {
                        "name": "m.onnx",
                        "size": len(body) + 1,  # ← 与 sha256 自相矛盾（手改 lock 敲错一位）
                        "sha256": _sha(body),
                        "required": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    dest = tmp_path / "dest"

    got = _run("--lock", str(lock), "--dest", str(dest))
    assert got.returncode == 0, got.stderr

    checked = _run("--lock", str(lock), "--check", "--strict", "--dest", str(dest))
    assert checked.returncode == 0, (
        f"下载判绿、--check 判红 —— 两条路径判据又分家了：\n{checked.stderr}"
    )


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


def test_base_url_env_is_exclusive_not_merely_first_in_line(
    fake_release: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """换源是**独占替换**：env 源缺文件时就该失败，不许悄悄回落到 lock 的公网源。

    上一条用例把 lock 的源目录掐掉了，所以它证明的其实只是"lock 的源死了还能走 env"
    —— 一个 ``return [env, *lock_urls]`` 的实现（优先而非独占）会一字不差地照样绿。
    可这两种语义的差别正是这个变量存在的理由：内网/离线部署要的是"别再去碰外网"，
    prepend 实现会在镜像少一个文件时把请求漏到 github.com 去，而那台机器可能根本
    不该有出网能力 —— 更糟的是它会**成功**，于是没有任何人知道刚才出过网。

    判别姿势：env 源是空的，而 lock 的源**完好无损**。
      · 独占（现状）：req.onnx 下不到 → 退 1，dest 里什么都没有。
      · 优先+兜底：从 lock 的源拿到了 → 退 0。
    """
    lock, src, dest = fake_release
    empty_mirror = tmp_path / "empty-mirror"
    empty_mirror.mkdir()
    assert (src / "req.onnx").is_file(), "前提：lock 的源仍然完好，兜底一旦发生就必然成功"

    r = _run(
        "--lock", str(lock), "--dest", str(dest),
        env={"MILOCO_MODELS_BASE_URL": empty_mirror.as_uri()},
    )
    assert r.returncode == 1, f"回落到了 lock 的源（换源不再独占）\n{r.stdout}\n{r.stderr}"
    assert not (dest / "req.onnx").exists(), "文件来自 lock 的源 —— 请求漏到了公网那侧"
    # 失败文案里的"源：…"要如实只列本次真正用过的源。把 lock 的源一并印出来，会让
    # 排查的人以为公网也试过了（"连 GitHub 都下不到"），而其实一次都没碰过。
    assert empty_mirror.as_uri() in r.stderr
    assert src.as_uri() not in r.stderr


def test_base_url_env_accepts_a_bare_path(
    fake_release: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """换源变量给挂载目录的裸路径要能用 —— 这是离线场景最自然的写法。

    不兜底成 file:// 的话，值原样拼进 URL，``urllib.request.Request`` 抛的是
    **ValueError**：它不在下载循环那个 ``(URLError, OSError, TimeoutError)`` 里，
    会一路穿出 main —— 打 traceback、退 1（文档定义为"必需模型缺失"）而不是文件头
    承诺的 2，且剩下的文件不再尝试。
    """
    lock, src, dest = fake_release
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    for f in src.iterdir():
        (mirror / f.name).write_bytes(f.read_bytes())
        f.unlink()

    r = _run(  # 裸路径，不带任何 scheme
        "--lock", str(lock), "--dest", str(dest),
        env={"MILOCO_MODELS_BASE_URL": str(mirror)},
    )
    assert r.returncode == 0, r.stderr
    assert (dest / "req.onnx").read_bytes() == _REQUIRED
    assert "Traceback" not in r.stderr


def test_unsupported_env_scheme_is_usage_error(
    fake_release: tuple[Path, Path, Path],
) -> None:
    """换源变量给了取不了的 scheme → 用法错误退 2，且不许是 traceback。

    退 1 会被上层按"网络不好 / 模型不齐"分流（插件安装器就打"下载失败（网络？）"），
    而用户明明是变量写错了。
    """
    lock, _src, dest = fake_release
    r = _run(
        "--lock", str(lock), "--dest", str(dest),
        env={"MILOCO_MODELS_BASE_URL": "ftp://mirror.example.com/models"},
    )
    assert r.returncode == 2, r.stderr
    assert "Traceback" not in r.stderr
    assert "MILOCO_MODELS_BASE_URL" in r.stderr


def test_bare_path_in_lock_is_a_lock_error_not_a_download_failure(
    tmp_path: Path,
) -> None:
    """lock 里的源写成裸路径 → 退 2 报 lock 坏了，**不**跟着兜底成 file://。

    与 env 那侧刻意不同：lock 是提交进仓库、由 publish_models.sh 生成的产物，写成
    裸路径就是坏了，而"坏 lock 退 2"是本脚本已有的契约。跟着兜底的话，一个一眼能
    定位的配置错误会变成"从一个不存在的本地目录下载失败"——退 1、文案还劝人换源。
    """
    lock = tmp_path / "models.lock.json"
    lock.write_text(
        json.dumps({"base_url": "/mnt/nas/models", "mirrors": [], "files": [
            {"name": "a.onnx", "size": 1, "sha256": _sha(b"a"), "required": True}
        ]}),
        encoding="utf-8",
    )
    r = _run("--lock", str(lock), "--dest", str(tmp_path / "dest"))
    assert r.returncode == 2, r.stderr
    assert "Traceback" not in r.stderr
    assert "lock" in r.stderr


def test_dest_env_is_honored(fake_release: tuple[Path, Path, Path], tmp_path: Path) -> None:
    """不传 --dest 时 MILOCO_MODELS_DEST 要生效。

    这条**必须**不传 --dest —— 传了就跟 test_dest_flag_beats_dest_env 完全重合，而
    env 单独生效这条分支（README / dev-guide 教内网用户用的正是它）就没人钉了。

    代价是回归时脚本会退回 _DEFAULT_DEST，也就是**本仓库里那个 gitignore 掉的**
    perception/models/ —— 两个假模型直接落进开发者的工作目录，且 git status 看不见。
    所以这里跑的是脚本的一份**副本**：_DEFAULT_DEST 由 `__file__` 上溯推出，副本放在
    tmp_path/scripts/ 下，回退目标就跟着落到 tmp_path 里，回归时脏的是 tmp 而不是仓库。
    副本是 shutil.copy2 现拷的，测的仍是真代码。

    顺带把断言补成两个方向：env 被采纳 **且** 回退目标没被碰过。只断言前者的话，
    "env 和默认值都写一遍"这种实现也能过。
    """
    lock, _src, dest = fake_release
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    script = script_dir / "fetch_models.py"
    shutil.copy2(_SCRIPT, script)
    # 与脚本里 _DEFAULT_DEST 的推法一致：_SCRIPTS_DIR.parent / backend/miloco/src/...
    fallback = tmp_path / "backend" / "miloco" / "src" / "miloco" / "perception" / "models"

    r = subprocess.run(
        [sys.executable, str(script), "--lock", str(lock)],
        capture_output=True,
        text=True,
        env={**_clean_env(), "MILOCO_MODELS_DEST": str(dest)},
    )

    assert r.returncode == 0, r.stderr
    assert (dest / "req.onnx").is_file()
    assert not fallback.exists(), "MILOCO_MODELS_DEST 被忽略，落到了默认目录"


def test_dest_flag_beats_dest_env(
    fake_release: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """--dest 必须压过 MILOCO_MODELS_DEST。

    这条分支一旦回归，build.sh / CI 那些显式传 --dest 的调用就会把模型写到 env 指的
    地方去：校验的目录与打包的目录悄悄分家，而源码里那个 models/ 已被 gitignore，
    git status 也看不见写歪的文件。
    """
    lock, _src, dest = fake_release
    decoy = tmp_path / "decoy"

    r = _run(
        "--lock", str(lock), "--dest", str(dest), env={"MILOCO_MODELS_DEST": str(decoy)}
    )
    assert r.returncode == 0, r.stderr
    assert (dest / "req.onnx").is_file()
    assert not decoy.exists(), "--dest 被 MILOCO_MODELS_DEST 顶掉了"


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


def _one_file_lock(tmp_path: Path, spec: dict) -> Path:
    lock = tmp_path / "models.lock.json"
    lock.write_text(
        json.dumps({"base_url": (tmp_path / "release").as_uri(), "mirrors": [], "files": [spec]}),
        encoding="utf-8",
    )
    return lock


@pytest.mark.parametrize(
    "spec",
    [
        pytest.param({"name": "a.onnx", "size": 5}, id="sha256-缺失"),
        pytest.param({"name": "a.onnx", "size": 5, "sha256": 12345}, id="sha256-不是字符串"),
        pytest.param({"name": "a.onnx", "size": 5, "sha256": ""}, id="sha256-空串"),
        pytest.param({"name": "a.onnx", "size": 5, "sha256": _sha(b"x")[:20]}, id="sha256-截断"),
        pytest.param({"name": "a.onnx", "size": 5, "sha256": "z" * 64}, id="sha256-非十六进制"),
        pytest.param({"name": "a.onnx", "size": "5", "sha256": _sha(b"x")}, id="size-是字符串"),
        pytest.param({"name": "a.onnx", "size": -1, "sha256": _sha(b"x")}, id="size-负数"),
        # name 的类型：`"/" in name` 对 list / dict 这类支持 in 的类型**不报错**，于是
        # 一路滑到 name.startswith(".") 才抛 AttributeError —— 而那个异常不在 main 读
        # lock 的捕获元组里，穿出去就是 traceback + 退 1，正好是本用例最要害的那一轴
        # （1 = "没下到，重试一下"，而重试永远修不好一份手写坏了的 lock）。
        # 拿 int 当对照：它撞的是 TypeError，本来就在元组里、早就是干净的退 2。
        pytest.param({"name": ["a.onnx"], "size": 5, "sha256": _sha(b"x")}, id="name-是列表"),
        pytest.param({"name": {"a.onnx": 1}, "size": 5, "sha256": _sha(b"x")}, id="name-是字典"),
        pytest.param({"name": 123, "size": 5, "sha256": _sha(b"x")}, id="name-是整数"),
    ],
)
def test_structurally_valid_lock_with_a_bad_key_is_usage_error(
    tmp_path: Path, spec: dict
) -> None:
    """lock 能解析、但下载器依赖的键坏了 —— 必须是"一行中文 + 退 2"，不许 traceback。

    退出码这一轴是要害，不只是"别难看"：本脚本的 1 表示"必需模型缺失"，
    install-hermes.sh 的四分支门禁照这个含义提示用户"没下到、稍后重试"，而重试
    多少次都没用，坏的是本地这份 lock。把校验从 main 那个 try 里挪走即变红。

    size 那两条单独有意义：--check 压根不碰 size，所以 CI 的 `--check --strict`
    门禁会照常绿，等真正下载那步才炸 —— 一绿一红出现在紧挨着的两个 step 上。
    这里刻意不带 --check，走的就是会炸的那条路径。
    """
    (tmp_path / "release").mkdir()
    dest = tmp_path / "d"
    dest.mkdir()
    # 文件按 lock 的名字放在 dest 里：让"本地已就绪"这条最早的路径（_is_ready）先跑到，
    # 这也是 CI 缓存命中时的形态 —— 一次网络请求都还没发就炸了。
    (dest / "a.onnx").write_bytes(b"hello")

    r = _run("--lock", str(_one_file_lock(tmp_path, spec)), "--dest", str(dest))
    assert r.returncode == 2, f"rc={r.returncode}\n{r.stdout}\n{r.stderr}"
    assert "Traceback" not in r.stderr, r.stderr
    assert "lock" in r.stderr


def test_uppercase_sha256_in_lock_still_matches(tmp_path: Path) -> None:
    """lock 里的摘要写成大写要照常认，不能判成"校验不通过"。

    比对用的是 hexdigest() 的小写输出加 `==`，而 Windows 的 `certutil -hashfile`
    输出就是大写 —— 不归一化的话每个文件都判不通过，表现成"永远下不完"的重下循环
    （每次重下都成功、每次校验都失败），而单看每一步都很正常。
    """
    (tmp_path / "release").mkdir()
    dest = tmp_path / "d"
    dest.mkdir()
    (dest / "a.onnx").write_bytes(b"hello")
    spec = {"name": "a.onnx", "size": 5, "sha256": _sha(b"hello").upper()}

    r = _run("--lock", str(_one_file_lock(tmp_path, spec)), "--dest", str(dest))
    assert r.returncode == 0, f"rc={r.returncode}\n{r.stdout}\n{r.stderr}"
    assert "已就绪" in r.stderr  # 进度都走 _log → stderr


def test_empty_selection_is_usage_error(fake_release: tuple[Path, Path, Path]) -> None:
    """`--only <可选> --required-only` 选出空集时要报错，不能"空循环 → exit 0"。

    CI / build 拿退出码当门禁，"什么都没做却说成功"等于门禁被静默摘掉。
    """
    lock, _src, dest = fake_release
    r = _run(
        "--lock", str(lock), "--dest", str(dest), "--only", "opt.onnx", "--required-only"
    )
    assert r.returncode == 2
    assert not dest.exists()


def test_missing_required_key_is_treated_as_required(tmp_path: Path) -> None:
    """lock 条目漏写 required 时 fail-closed 当必需，而不是悄悄降级成可选。"""
    src = tmp_path / "release"
    src.mkdir()
    lock = tmp_path / "models.lock.json"
    lock.write_text(
        json.dumps(
            {
                "base_url": src.as_uri(),
                "mirrors": [],
                # 故意不写 required
                "files": [{"name": "gone.onnx", "size": 3, "sha256": _sha(b"abc")}],
            }
        ),
        encoding="utf-8",
    )
    r = _run("--lock", str(lock), "--dest", str(tmp_path / "d"))
    assert r.returncode == 1, r.stderr
    assert "必需模型未就绪" in r.stderr


def test_null_required_is_treated_as_required(tmp_path: Path) -> None:
    """`"required": null` 要跟上面"漏写 required"同一个结论：必需。

    比漏写更险 —— `.get("required", True)` 的默认值只在**键不存在**时生效，键在、
    值是 None 时原样返回 None，`bool(None)` 即 False，必需模型就被静默降级成可选。
    而 build 的 --strict、CI 门禁、install-hermes 的就绪判据全照 required 判，
    于是少一个必需模型的包能一路退 0 发出去：没有 traceback、没有红，没人收到信号。
    """
    src = tmp_path / "release"
    src.mkdir()
    lock = tmp_path / "models.lock.json"
    lock.write_text(
        json.dumps(
            {
                "base_url": src.as_uri(),
                "mirrors": [],
                "files": [
                    {"name": "gone.onnx", "size": 3, "sha256": _sha(b"abc"), "required": None}
                ],
            }
        ),
        encoding="utf-8",
    )
    r = _run("--lock", str(lock), "--dest", str(tmp_path / "d"))
    assert r.returncode == 1, f"rc={r.returncode}（0 = 被当成可选放行了）\n{r.stderr}"
    assert "必需模型未就绪" in r.stderr


def test_null_size_behaves_like_a_missing_size_key(tmp_path: Path) -> None:
    """`"size": null` 要跟"整条不写 size"同义，不能炸成 traceback。

    size 按设计就是可选键（不写只是不显示百分比），但下游取值是
    `spec.get("size", 0)`，默认值只在键缺失时生效 —— null 会原样漏进 `_human()`
    比大小 → TypeError → 退 **1**，而 1 在本脚本契约里是"必需模型缺失"，
    install-hermes.sh 会照这个含义提示"稍后重试"，重试多少次都没用。
    `--quiet` 也救不了：f-string 的参数在调用 `_log` 之前就已经求值了。
    """
    (tmp_path / "release").mkdir()
    dest = tmp_path / "d"
    dest.mkdir()
    (dest / "a.onnx").write_bytes(b"hello")
    spec = {"name": "a.onnx", "size": None, "sha256": _sha(b"hello")}

    r = _run("--lock", str(_one_file_lock(tmp_path, spec)), "--dest", str(dest))
    assert r.returncode == 0, f"rc={r.returncode}\n{r.stdout}\n{r.stderr}"
    assert "Traceback" not in r.stderr, r.stderr
    assert "已就绪" in r.stderr


def test_path_traversal_in_lock_name_is_rejected(tmp_path: Path) -> None:
    """lock 里的文件名直接拼进 dest 路径和 URL，不许含路径分隔符。

    信任边界本是"lock 在仓库里"，但 --lock 可指任意文件，`dest / "../../evil"`
    会静静逃出 dest。
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    lock = tmp_path / "models.lock.json"
    lock.write_text(
        json.dumps(
            {
                "base_url": outside.as_uri(),
                "mirrors": [],
                "files": [
                    {
                        "name": "../../evil.onnx",
                        "size": 3,
                        "sha256": _sha(b"abc"),
                        "required": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    r = _run("--lock", str(lock), "--dest", str(tmp_path / "d" / "e"))
    assert r.returncode == 2
    assert not (tmp_path / "evil.onnx").exists()


def _make_chunked_truncating_handler(
    body: bytes,
) -> type[http.server.BaseHTTPRequestHandler]:
    """分块传输（chunked）传到一半断开的服务端。

    与 `_make_truncating_handler` 是**不同**的一类：那边有 Content-Length，截断由
    _stream 的"写入字节数 != 自报长度"检查抓住、抛 OSError；这边没有 Content-Length
    （chunked 的定义就是长度未知），那道检查的 declared 是 None、根本无从触发，
    截断唯一的表现形式是 resp.read() 抛 http.client.IncompleteRead。
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler 的约定命名
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            half = body[: len(body) // 2]
            # 先报一个比实际发出去的大得多的块长度，再只发一半就撒手：
            # 客户端读到 EOF 时块还没收齐 → IncompleteRead。
            self.wfile.write(f"{len(body):x}\r\n".encode())
            self.wfile.write(half)
            self.close_connection = True

        def log_message(self, *_args: object) -> None:
            pass

    return Handler


def test_chunked_truncation_is_an_error_not_a_traceback_and_still_falls_over(
    tmp_path: Path,
) -> None:
    """分块传输截断要走正常失败路径，且**不许打断镜像降级**。

    http.client.IncompleteRead 继承的是 HTTPException，既不是 OSError 也不是
    URLError（URLError 只包住**建连**阶段；读响应体时 http.client 抛的原样穿出来）。
    没把它列进 _fetch_one 的捕获元组时，代价远不止一条 traceback 顶替中文错误 ——
    异常会穿出 `for url in urls` 整个循环，**镜像降级彻底失效**：直连一次分块截断就
    带走整轮，后面 3 个镜像一个都不会试。而这恰恰是镜像最该出场的场景（跨境链路
    传到一半被掐）。国内用户看到的是"直连一挂就全挂"，而 lock 里明明配了镜像。

    所以这条用两个源来钉：第一个 chunked 截断，第二个是好的 file:// 源。
    只断言"没有 traceback"是不够的 —— 那样把 IncompleteRead 改成在循环外捕获、
    打一行中文再退，同样能过，而镜像照样没试。真正的判据是**文件下到了**。
    """
    body = b"z" * 8192
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), _make_chunked_truncating_handler(body)
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()

    good = tmp_path / "mirror"
    good.mkdir()
    (good / "c.onnx").write_bytes(body)
    try:
        lock = tmp_path / "models.lock.json"
        lock.write_text(
            json.dumps(
                {
                    "base_url": f"http://127.0.0.1:{server.server_address[1]}",
                    "mirrors": [good.as_uri()],
                    "files": [
                        {
                            "name": "c.onnx",
                            "size": len(body),
                            "sha256": _sha(body),
                            "required": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        dest = tmp_path / "dest"
        r = _run("--lock", str(lock), "--dest", str(dest))
    finally:
        server.shutdown()
        server.server_close()

    assert "Traceback" not in r.stderr, f"IncompleteRead 穿出来了：\n{r.stderr}"
    assert r.returncode == 0, f"镜像降级没兜住分块截断：\n{r.stderr}"
    assert (dest / "c.onnx").read_bytes() == body


def test_truncated_response_keeps_part_for_resume(tmp_path: Path) -> None:
    """连接在 Content-Length 未满时被干净关闭 → 报"提前关闭"并保住 .part。

    http.client 对这种截断是静默返回 b""。不比对长度的话它会被误报成"sha256 不符"，
    而那条路径会删掉 .part，下一轮不带 Range 从 0 重来 —— 跨境链路上每次断在同一
    位置就成了永远下不完，诊断还被指向"镜像被投毒"。
    """
    body = b"x" * 4096
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), _make_truncating_handler(body, sent=1024)
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        lock = tmp_path / "models.lock.json"
        lock.write_text(
            json.dumps(
                {
                    "base_url": f"http://127.0.0.1:{server.server_address[1]}",
                    "mirrors": [],
                    "files": [
                        {
                            "name": "trunc.onnx",
                            "size": len(body),
                            "sha256": _sha(body),
                            "required": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        dest = tmp_path / "dest"
        r = _run("--lock", str(lock), "--dest", str(dest))
    finally:
        server.shutdown()
        server.server_close()

    assert r.returncode == 1
    assert "提前关闭" in r.stderr, r.stderr
    assert "sha256 不符" not in r.stderr, "截断被误报成 hash 不符"
    # .part 必须留着，否则续传无从谈起（进程退出后也不清，见下一条测试）
    assert "待续传" in r.stderr


def test_part_survives_process_exit_so_next_run_resumes(tmp_path: Path) -> None:
    """所有源试完仍失败时，稳定名 `.part` 必须活过进程退出 —— 下一次调用接着下。

    这是 `_open_part` 那把 flock 换来的东西：它专门为了"稳定名可跨轮续传"付了复杂度，
    收尾若无条件 unlink，跨调用续传就只在 Ctrl-C 时有效（KeyboardInterrupt 不在那个
    except 里、也走不到收尾那行）。而真正需要它的场景——限速链路每次断在同一位置——
    反而每次从 0 重来，表现成"重跑多少次都不涨"。

    这里把那个症状直接跑出来：服务端每次只发 1024 字节就撒手，body 4096。单源 3 次
    重试各续 1024 → 第一次调用停在 3072、退 1；保住 .part 的话第二次调用只需再补
    1024 就能凑满并通过 sha256。删了 .part 的实现在第二次调用只能再走一遍 0→3072，
    永远退 1。
    """
    body = b"x" * 4096
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), _make_truncating_handler(body, sent=1024)
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        lock = tmp_path / "models.lock.json"
        lock.write_text(
            json.dumps(
                {
                    "base_url": f"http://127.0.0.1:{server.server_address[1]}",
                    "mirrors": [],
                    "files": [
                        {
                            "name": "resume.onnx",
                            "size": len(body),
                            "sha256": _sha(body),
                            "required": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        dest = tmp_path / "dest"

        first = _run("--lock", str(lock), "--dest", str(dest))
        part = dest / "resume.onnx.part"
        assert first.returncode == 1
        # 3 次重试各续 1024：进程内续传本来就有，这里确认它没在收尾被抹掉
        assert part.is_file(), "稳定名 .part 被删了，下一次只能从 0 重来"
        assert part.stat().st_size == 3072, part.stat().st_size
        assert not (dest / "resume.onnx").exists()

        second = _run("--lock", str(lock), "--dest", str(dest))
    finally:
        server.shutdown()
        server.server_close()

    # 第二次只需再补 1024 —— 这一段正是"跨调用续传"，.part 被删就永远走不到
    assert second.returncode == 0, second.stderr
    assert (dest / "resume.onnx").read_bytes() == body
    assert list(dest.glob("*.part")) == [], "成功后不许留下 .part"


def _make_range_rejecting_handler(
    body: bytes,
) -> type[http.server.BaseHTTPRequestHandler]:
    """按 RFC 行事的服务端：Range 起点越过资产末尾时回 416，不带 Range 则整段发。

    GitHub 就是这么回的。造它是为了钉住"续传起点非法"这第三类失败——它既不是
    "源内容变了"（sha 不符），也不是"连接断了"（截断），处置和后者正好相反。
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler 的约定命名
            rng = self.headers.get("Range", "")
            if rng.startswith("bytes="):
                start = int(rng.removeprefix("bytes=").split("-")[0] or 0)
                if start >= len(body):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(body)}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                chunk = body[start:]
                self.send_response(206)
                self.send_header("Content-Length", str(len(chunk)))
                self.send_header(
                    "Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}"
                )
                self.end_headers()
                self.wfile.write(chunk)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    return Handler


def test_range_start_past_asset_end_discards_part_instead_of_looping(
    tmp_path: Path,
) -> None:
    """续传起点越界（HTTP 416）必须归零重下，不能当成"连接断了"保住 .part。

    416 是**我们自己攒下的状态**造成的失败：保住 .part 意味着每次重试、每个源、
    以及之后每一次调用都从同一个偏移量原样复现同一个 416，永不自愈；日志还会打
    "已保留 X 待续传"，把人指向"网络/镜像有问题"——和唯一的解法正好反向。

    触发条件在这里如实搭出来：lock 记 2327（换代后没 refresh），线上资产实际 2000，
    目录里留着一份 2100 字节的 .part —— 落在 [2000, 2327) 这个窗口里，_stream 开头
    那道脏文件守卫（offset >= lock size 才截断）挡不住。
    """
    body = b"x" * 2000
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), _make_range_rejecting_handler(body)
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        lock = tmp_path / "models.lock.json"
        lock.write_text(
            json.dumps(
                {
                    "base_url": f"http://127.0.0.1:{server.server_address[1]}",
                    "mirrors": [],
                    "files": [
                        {
                            "name": "stale.onnx",
                            "size": 2327,  # 比线上资产长：换代后 lock 没跟着刷
                            "sha256": _sha(body),
                            "required": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / "stale.onnx.part").write_bytes(b"x" * 2100)

        r = _run("--lock", str(lock), "--dest", str(dest))
    finally:
        server.shutdown()
        server.server_close()

    assert r.returncode == 0, f"416 没自愈，卡死在同一个偏移量：\n{r.stderr}"
    assert (dest / "stale.onnx").read_bytes() == body
    assert list(dest.glob("*.part")) == [], "成功后不许留下 .part"
    assert "416" in r.stderr, f"没把 416 单独认出来:\n{r.stderr}"
    assert "待续传" not in r.stderr, "416 被当成截断，日志把人指向网络问题"


# ─── 共享 dest：.part 的 flock 与 inode ─────────────────────────────────────


def _load_script_module() -> Any:
    """把脚本当模块导进来。

    本文件其余用例一律走 subprocess（见模块 docstring），这一条是例外：
    "丢弃 .part 之后锁还在不在"是个 inode 级的性质，跨进程断言只能靠时序去撞，
    必然 flaky。直接调函数把性质钉死。
    """
    spec = importlib.util.spec_from_file_location("_fetch_models_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_discarding_part_keeps_inode_so_flock_still_guards_it(tmp_path: Path) -> None:
    """丢弃 .part 内容必须保留 inode —— flock 锁的是 inode，不是路径。

    换成 unlink 的话：目录项没了，锁挂在一个没有名字的 inode 上，别的进程在
    _open_part 里于同一路径新建 inode 就能抢锁成功。两边于是同时往同一个 .part 写，
    交错出来的字节永远校验不过，而报出来的是"sha256 不符"——诊断被指向"镜像被投毒"，
    实际网络和镜像都是好的（_stream docstring 明确想避免的那种误导）。
    """
    fcntl = pytest.importorskip("fcntl", reason="Windows 上 _open_part 本就退化成无锁")
    mod = _load_script_module()

    part = tmp_path / "det_4C.onnx.part"
    part.write_bytes(b"stale oversized junk" * 64)
    holder = open(part, "a+b")  # 模拟 _open_part 抢到稳定名
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    ino_before = part.stat().st_ino
    try:
        mod._discard_part(part)

        # 内容清空（下一轮 _stream stat 得 0 → "wb" 整段重写），但还是同一个 inode
        assert part.is_file()
        assert part.stat().st_size == 0
        assert part.stat().st_ino == ino_before

        # 关键断言：锁仍然守得住这个路径，"另一个进程"抢不到
        rival = open(part, "a+b")
        try:
            with pytest.raises(OSError):
                fcntl.flock(rival.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            rival.close()
    finally:
        holder.close()

    # 文件已不在时不许抛：重试路径上它可能已被别处清掉
    mod._discard_part(tmp_path / "never-existed.part")


def test_local_source_failure_retries_without_backing_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """file:// 源上取不到文件时不许干等 —— 重试照留，退避必须跳过。

    "等一会儿再试"能改变结果的前提是失败来自链路抖动；本地源上读不到就是读不到，
    退避是纯粹的白等。而唯一会撞上它的调用方恰好是静默的：install-hermes.sh 拿旁边
    那份 checkout 当 file:// 源跑同步，源目录只有一部分模型是常态（fork 的 checkout
    本就如此），缺的每个文件白等 1s+2s —— 5 个里缺 4 个就是 12s 零输出，卡在两条 info
    之间，看起来像脚本挂了，而这 12s 对结果毫无贡献（那几个本来就由后面联网那趟去补）。

    这条和 test_discarding_part_keeps_inode… 一样走进程内：跨进程只能拿墙钟去撞
    "有没有 sleep 满 3 秒"，阈值定多少都是在赌 CI 的负载。直接看 sleep 有没有被调。
    """
    mod = _load_script_module()

    slept: list[float] = []
    monkeypatch.setattr(mod.time, "sleep", lambda s: slept.append(s))

    src = tmp_path / "checkout"  # 空源目录：lock 里的名字一个都取不到
    src.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    spec = {"name": "det_4C.onnx", "size": 3, "sha256": _sha(b"abc"), "required": True}

    ok = mod._fetch_one(spec, dest, [src.resolve().as_uri()], force=False, quiet=True)

    assert ok is False, "文件本就不存在，不该报成功"
    assert slept == [], f"file:// 源上不该退避，实际睡了 {slept}"

    # 退避跳了，重试没跳：本地源也可能撞上 EIO / 半路被换掉，那时重试仍有意义。
    slept.clear()
    attempts = 0
    real_stream = mod._stream

    def counting_stream(url: str, *a: Any, **kw: Any) -> None:
        nonlocal attempts
        attempts += 1
        real_stream(url, *a, **kw)

    monkeypatch.setattr(mod, "_stream", counting_stream)
    mod._fetch_one(spec, dest, [src.resolve().as_uri()], force=False, quiet=True)
    assert attempts == mod._MAX_RETRIES, f"重试次数不该变，实际 {attempts}"
    assert slept == []

    # 反过来，网络源上退避必须照旧 —— 别为了修上面那条把重试打成无退避连打。
    slept.clear()
    mod._fetch_one(spec, dest, ["http://127.0.0.1:1/nope"], force=False, quiet=True)
    assert slept == [1, 2], f"http 源仍须 1s/2s 退避，实际 {slept}"


def test_sha_mismatch_still_leaves_no_part_behind(
    fake_release: tuple[Path, Path, Path],
) -> None:
    """截断代替删除之后，收尾仍不许留下 .part 垃圾。

    _discard_part 把文件留在原地（0 字节），靠 _fetch_one 收尾那段"空文件没有续传
    价值"把壳清掉。这条是防回归：别为了保住锁而攒下一地 0 字节 .part。
    """
    lock, src, dest = fake_release
    (src / "req.onnx").write_bytes(b"wrong bytes")

    r = _run("--lock", str(lock), "--dest", str(dest), "--only", "req.onnx")
    assert r.returncode == 1
    assert list(dest.glob("*.part")) == [], "sha 不符收尾后不许留下 .part"


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


def test_lock_matches_resource_validator_models() -> None:
    """lock 与 resource_validator.MODELS 必须**双向**等价（文件名 + 必需性）。

    lock 决定"下什么"，MODELS 决定"缺什么算降级/算 MODELS_MISSING"。两边各自都对、
    合起来错的姿势有两种：lock 少一条 → 运行时按 MODELS 判缺、报 models_missing；
    lock 多一条 → 白下一个没人读的文件，还被打进 release tarball。
    publish_models.sh 的 refresh-lock 是全量重写 lock 的，这条断言就是它的安全网。
    """
    from miloco.perception.engine.resource_validator import MODELS

    lock = json.loads(_REAL_LOCK.read_text(encoding="utf-8"))
    assert {(f["name"], bool(f["required"])) for f in lock["files"]} == {
        (m.name, not m.optional) for m in MODELS
    }, "scripts/models.lock.json 与 resource_validator.MODELS 不一致（文件名或必需性）"


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


# 全仓调用方：凡把本脚本当"下模型"用的可执行入口都要在列。漏一个就等于这条契约
# 对它不生效，所以下面还有一条断言钉死"每个文件都真的匹配到了调用"。
# 只收可执行入口，不收文档：README / dev-guide / troubleshooting 里的 `--dest` 示例
# 是给人手敲的，退出码不喂给任何门禁，强行要求 --strict 只会制造无意义的文档改动。
_FETCH_CALLERS = (
    "scripts/build.sh",
    "scripts/local-ci.sh",
    "plugins/hermes/install-hermes.sh",
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
)


def _fetch_invocations(text: str) -> list[tuple[int, str]]:
    """文件里"调用 fetch_models.py 且指定了 --dest"的行，(行号从 1 起, 原文)。

    整行注释剔掉：注释不执行，也不会被用户复制。用 --dest 当"这是一次调用"的判据，
    是因为所有真实调用点都显式传它（``MILOCO_MODELS_DEST`` 优先级更低，几个调用方
    都不敢依赖），而 `FETCH_MODELS=...` 这类赋值、以及只提脚本名的 info 文案都不带。
    """
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        if "fetch_models.py" not in line and "FETCH_MODELS" not in line:
            continue
        if "--dest" in line:
            out.append((i, line))
    return out


def test_every_fetch_caller_matches_its_own_gate_strength() -> None:
    """调用 fetch_models.py 的两头判据必须同强度：要么带 --strict，要么后面有严格门禁。

    不带 --strict 时"只有可选模型没下到"是退 **0** 的（main 里 failed_optional 不并进
    failed_required）。所以拿退出码当"齐没齐"唯一信号的调用方，一旦判入口用了
    `--check --strict` 而补齐时忘了 --strict，整条兜底就静默失效：`if !` 恒不成立，
    一条告警都不打，而语义去重与 VAD 已经降级了。这正是本 PR 修掉的那个真实缺陷
    （install-hermes.sh 的下载调用），而 install-hermes 的控制流没有任何自动化覆盖 ——
    这条契约就是它唯一的网。

    两类合法的例外，都必须自己**结构上**站得住，不接受"我知道我在干什么"：

    1. 退出码被显式丢弃（``|| true``）——install-hermes 拿旁边 checkout 当 file:// 源
       做本地同步的那一步。源目录缺几个模型是常态，这步本就不做判定，判定交给它后面
       那道按同一份 lock 复判的门禁。
    2. **后面**另有一步 ``--check --strict``——ci.yml 刻意把"没拉到"与"拉到的不对"
       拆成两步（也让不完整状态进不了 actions/cache，因为 save 在门禁之后）。

    第 2 条只认"在后面"，不认"在前面"，这正是本测试的判别力所在：install-hermes 的
    models_ready 也跑 ``--check --strict``，但它在下载**之前**、判的是要不要进这个分支，
    下载完之后没有任何复判。把它算成豁免，本 PR 修的那个 bug 就会照旧绿着过。
    """
    seen_files = []
    for rel in _FETCH_CALLERS:
        path = _ROOT / rel
        assert path.is_file(), f"{rel} 不存在（挪了位置就同步这里的清单）"
        text = path.read_text(encoding="utf-8")
        calls = _fetch_invocations(text)
        if calls:
            seen_files.append(rel)

        # 该文件里最后一道 `--check --strict` 的位置：例外 2 要求它在调用**之后**。
        last_gate = max(
            (i for i, ln in calls if "--check" in ln and "--strict" in ln), default=-1
        )

        for lineno, line in calls:
            if "--strict" in line:
                continue
            if "|| true" in line:
                continue
            assert lineno < last_gate, (
                f"{rel}:{lineno} 调用 fetch_models.py 却不带 --strict，"
                f"退出码也没被丢弃，后面也没有 `--check --strict` 复判：\n"
                f"    {line.strip()}\n"
                "只缺可选模型时这行会退 0，而判入口那侧照旧判不齐 —— 兜底静默失效。"
            )

    # 清单没烂：每个列出的文件都真的还在调 fetch_models.py。否则删掉/改写了调用点，
    # 上面的循环会空转着绿，这条契约就悄悄不设防了。
    assert seen_files == list(_FETCH_CALLERS), (
        f"这些文件已不再调用 fetch_models.py --dest，清单该更新："
        f"{sorted(set(_FETCH_CALLERS) - set(seen_files))}"
    )
