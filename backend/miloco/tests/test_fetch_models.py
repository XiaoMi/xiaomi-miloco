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
import json
import os
import subprocess
import sys
import threading
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
    """跑 fetch_models.py，并先把继承来的 MILOCO_MODELS_* 剥干净。

    不剥的话，内网/离线开发者按 models/README.md 与 dev-guide 的建议 export 了
    MILOCO_MODELS_BASE_URL 之后，这一批测试会全线翻车：该变量对下载源是**独占替换**，
    fixture 那个 file:// 假 Release 会被整体顶掉，测试转而真的去打公司镜像 ——
    既违反本文件"不联网"的契约，`test_empty_source_list_is_usage_error`
    这类断言退出码的用例语义还会直接反转。
    """
    base = {k: v for k, v in os.environ.items() if not k.startswith("MILOCO_MODELS_")}
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        env={**base, **(env or {})},
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
