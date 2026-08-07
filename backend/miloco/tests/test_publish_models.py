# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""scripts/publish_models.sh 的契约测试.

这个脚本原本只是维护者手动跑的工具，测不测无所谓；但 `verify` 已经挂进 CI 的 lint
job，成了每个 PR 都会执行的代码，而它的分支要不要走到取决于**线上 Release 的状态**，
不取决于改动本身 —— 也就是说写错时暴露的时机与责任 PR 完全脱钩（表现是"全仓 lint
突然长红"或"该拦的没拦"）。这里把这些分支离线钉死。

两处手法：
  · PATH 最前面放一个假 gh，`api` 直接吐预置的资产清单 —— 不联网、不需要凭据。
  · 把脚本和 lock 一起拷进 tmp 再跑：脚本的 SCRIPT_DIR / LOCK 是按 BASH_SOURCE 算的，
    拷过去之后 refresh_lock_from_dir 改写的就是 tmp 里那份，碰不到仓库里的真 lock。
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_REAL_SCRIPT = _ROOT / "scripts" / "publish_models.sh"
_REAL_LOCK = _ROOT / "scripts" / "models.lock.json"

# 假 gh：记录每一次调用，好让测试断言"上传到底有没有真的发生"。
_FAKE_GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_CALLS"
case "$1" in
  auth) exit 0 ;;
  api)  cat "$GH_ASSETS" ;;
  release)
    case "$2" in
      view)          exit "${GH_RELEASE_VIEW_RC:-0}" ;;
      upload|create) exit 0 ;;
      *)             exit 1 ;;
    esac ;;
  *) exit 1 ;;
esac
"""


class _Sandbox:
    """一份可随意改的 publish_models.sh + lock + 假 gh。"""

    def __init__(self, tmp: Path, lock: dict) -> None:
        self.dir = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)

        # 脚本按 BASH_SOURCE 定位 SCRIPT_DIR，拷贝过来即可把 LOCK 指到 tmp。
        self.script = tmp / "publish_models.sh"
        shutil.copy2(_REAL_SCRIPT, self.script)
        self.lock = tmp / "models.lock.json"
        self.write_lock(lock)

        gh = self.bin / "gh"
        gh.write_text(_FAKE_GH, encoding="utf-8")
        gh.chmod(0o755)

        self.calls = tmp / "gh_calls.txt"
        self.assets = tmp / "assets.json"
        self.assets.write_text("[]", encoding="utf-8")

    def write_lock(self, lock: dict) -> None:
        self.lock.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")

    def set_assets(self, assets: list[dict]) -> None:
        self.assets.write_text(json.dumps(assets), encoding="utf-8")

    def run(self, *args: str, **extra_env: str) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k != "MILOCO_MODELS_ALLOW_LOCK_DRIFT"}
        env.update(
            PATH=f"{self.bin}{os.pathsep}{env['PATH']}",
            GH_CALLS=str(self.calls),
            GH_ASSETS=str(self.assets),
            # 兜底：万一假 gh 没拦住，打的也不是真仓库。
            MILOCO_REPO="example/not-a-real-repo",
        )
        env.update(extra_env)
        return subprocess.run(
            ["bash", str(self.script), *args],
            capture_output=True,
            text=True,
            env=env,
        )

    def gh_calls(self) -> list[str]:
        if not self.calls.exists():
            return []
        return [ln for ln in self.calls.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def wrote_to_release(self) -> bool:
        """有没有发生过不可逆的写操作（upload / create）。"""
        return any(c.startswith(("release upload", "release create")) for c in self.gh_calls())


def _real_lock() -> dict:
    return json.loads(_REAL_LOCK.read_text(encoding="utf-8"))


def _assets_from(lock: dict) -> list[dict]:
    """按 lock 造一份"完全一致"的 Release 资产清单。"""
    return [
        {"name": f["name"], "size": f["size"], "digest": f"sha256:{f['sha256']}"}
        for f in lock["files"]
    ]


@pytest.fixture
def sandbox(tmp_path: Path) -> _Sandbox:
    return _Sandbox(tmp_path, _real_lock())


# ── verify：一致 / 不一致 ────────────────────────────────────────────────


def test_verify_passes_when_assets_match_lock(sandbox: _Sandbox) -> None:
    sandbox.set_assets(_assets_from(_real_lock()))
    r = sandbox.run("verify")
    assert r.returncode == 0, r.stderr
    assert "全等" in r.stderr


def test_verify_flags_asset_missing_from_release(sandbox: _Sandbox) -> None:
    """lock 有、Release 没有 —— 所有构建都会 404 在这个文件上。"""
    assets = _assets_from(_real_lock())
    dropped = assets.pop()["name"]
    sandbox.set_assets(assets)

    r = sandbox.run("verify")
    assert r.returncode == 1
    assert dropped in r.stderr
    assert "404" in r.stderr


def test_verify_flags_extra_asset_on_release(sandbox: _Sandbox) -> None:
    """Release 多出来的资产要报出来：upload 不删旧的，换代改名后老文件会一直挂着，
    而 refresh-lock（不带 dir）还会把它重新收编进 lock。"""
    assets = [*_assets_from(_real_lock()), {"name": "det_5C.onnx", "size": 1, "digest": "sha256:ab"}]
    sandbox.set_assets(assets)

    r = sandbox.run("verify")
    assert r.returncode == 1
    assert "det_5C.onnx" in r.stderr
    assert "换代残留" in r.stderr
    # 得给出可照抄的修法，否则维护者只知道红、不知道怎么办
    assert "delete-asset" in r.stderr


def test_verify_flags_sha256_mismatch(sandbox: _Sandbox) -> None:
    """size 相同、内容被换掉 —— 这正是可变 tag 最危险的那种漂移。"""
    assets = _assets_from(_real_lock())
    assets[0]["digest"] = "sha256:" + "0" * 64
    sandbox.set_assets(assets)

    r = sandbox.run("verify")
    assert r.returncode == 1
    assert "sha256 不符" in r.stderr


def test_verify_size_mismatch_does_not_also_report_sha256(sandbox: _Sandbox) -> None:
    """size 就不同的话 sha256 必然也不同，只报一条。

    少了那个 continue 的话同一个文件会既报 size 又报 sha256，噪音翻倍。
    """
    assets = _assets_from(_real_lock())
    assets[0]["size"] = int(assets[0]["size"]) + 1
    assets[0]["digest"] = "sha256:" + "0" * 64
    sandbox.set_assets(assets)

    r = sandbox.run("verify")
    assert r.returncode == 1
    assert "size 不符" in r.stderr
    assert "sha256 不符" not in r.stderr


# ── verify：digest 缺失 / 非 sha256 时降级为只比 size ────────────────────


def test_verify_degrades_to_size_when_digest_absent(sandbox: _Sandbox) -> None:
    """digest 是 GitHub 后加的字段，早期资产没有 —— 这种只能降级成比 size 并 warn。

    误写成 errors 的话，一个老资产就能让全仓 CI 长红。
    """
    assets = [{k: v for k, v in a.items() if k != "digest"} for a in _assets_from(_real_lock())]
    sandbox.set_assets(assets)

    r = sandbox.run("verify")
    assert r.returncode == 0, r.stderr
    assert "::warning::" in r.stderr
    assert "::error::" not in r.stderr


def test_verify_degrades_when_digest_is_not_sha256(sandbox: _Sandbox) -> None:
    assets = _assets_from(_real_lock())
    assets[0]["digest"] = "md5:" + "0" * 32
    sandbox.set_assets(assets)

    r = sandbox.run("verify")
    assert r.returncode == 0, r.stderr
    assert "::warning::" in r.stderr


# ── upload：文件集护栏必须早于不可逆的上传 ──────────────────────────────


def _tiny_lock(names: list[str]) -> dict:
    """两三个假条目的 lock，只为把文件集控制成想要的样子。"""
    return {
        "release_tag": "models",
        "base_url": "https://example.invalid/models",
        "mirrors": [],
        "files": [
            {"name": n, "size": 4, "sha256": "0" * 64, "required": True, "desc": n}
            for n in names
        ],
    }


def _models_dir(tmp: Path, names: list[str]) -> Path:
    d = tmp / "models"
    d.mkdir(exist_ok=True)
    for n in names:
        (d / n).write_bytes(b"fake")
    return d


@pytest.mark.parametrize(
    ("lock_names", "dir_names", "expect_in_stderr"),
    [
        # 少了：目录只有 2 个（下载中断 / 手动挑了几个），lock 会被无声缩表
        (["a.onnx", "b.onnx", "c.onnx"], ["a.onnx", "b.onnx"], "c.onnx"),
        # 多了：换代残留 / 误传，会被以 required=false 默默收编
        (["a.onnx"], ["a.onnx", "b.onnx"], "b.onnx"),
        # 改名换代：两个方向同时发生
        (["det_4C.onnx"], ["det_5C.onnx"], "det_5C.onnx"),
    ],
)
def test_upload_aborts_before_touching_release_on_fileset_drift(
    tmp_path: Path, lock_names: list[str], dir_names: list[str], expect_in_stderr: str
) -> None:
    """护栏必须在 gh release upload **之前**开火。

    放在上传之后的话，拒绝改表时资产已经躺在公开 Release 上、lock 还是旧的，从那一刻
    起所有人的 PR 都会被对账门禁判红；而脚本连"去 delete-asset"都说不出口 ——
    SystemExit 撞上 set -e 当场中止，善后提示根本执行不到。
    """
    sandbox = _Sandbox(tmp_path, _tiny_lock(lock_names))
    d = _models_dir(tmp_path, dir_names)

    r = sandbox.run("upload", str(d))

    assert r.returncode != 0
    assert expect_in_stderr in r.stderr
    assert "上传前中止" in r.stderr
    # 核心断言：线上资产一个字节都没被动过
    assert not sandbox.wrote_to_release(), f"护栏开火前已经写了 Release: {sandbox.gh_calls()}"


def test_upload_survives_any_gh_failure_in_trailing_verify(tmp_path: Path) -> None:
    """upload 收尾那次对账是**故意**非致命的，任何失败都不许打穿它。

    跑到那儿资产已经推上 Release、lock 也已改写落盘，两件事都撤不回来；此时唯一还
    有用的输出就是最后那句「别忘了提交 lock」。它一旦被吞掉，维护者看到的是 FATAL
    + 退出码 1，会读成"上传失败"，于是要么重跑一遍 upload，要么干脆没提交刷新后的
    lock，把 CI 的对账门禁留给下一个人踩。

    钉的是"cmd_verify 函数体内不许有 exit"这个**性质**，而不是某一处具体的 die：
    exit 在函数里退的是整个 shell，调用方那句 `if ! cmd_verify` 根本没有接的机会。
    同一个函数已经在 `gh api` 和 `need_gh` 两处先后踩过，所以这里让假 gh 在收尾阶段
    对 auth 和 api **两条路径同时**失败，把它们一起焊死。

    auth 第一次放行是必须的：cmd_upload 开头自己要查一次 gh 在不在、登录没登录。
    模拟的是"78MiB 上传 + 78MiB 重算 hash"那几分钟里 token 过期 —— gh auth status
    不是纯本地检查，它要发一次请求验 token。
    """
    names = ["a.onnx", "b.onnx"]
    sandbox = _Sandbox(tmp_path, _tiny_lock(names))
    d = _models_dir(tmp_path, names)

    counter = tmp_path / "auth_calls"
    counter.write_text("0", encoding="utf-8")
    gh = sandbox.bin / "gh"
    gh.write_text(
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_CALLS"
case "$1" in
  auth)
    n=$(cat "$GH_AUTH_N"); echo $((n + 1)) > "$GH_AUTH_N"
    if [ "$n" -eq 0 ]; then exit 0; fi          # cmd_upload 开头那次：放行
    echo "The token in keyring is invalid." >&2; exit 1 ;;
  api)     echo "gh: 502 Bad Gateway" >&2; exit 1 ;;
  release) exit 0 ;;
  *)       exit 1 ;;
esac
""",
        encoding="utf-8",
    )
    gh.chmod(0o755)

    r = sandbox.run("upload", str(d), GH_AUTH_N=str(counter))

    assert sandbox.wrote_to_release(), f"没走到上传: {sandbox.gh_calls()}\n{r.stderr}"
    assert "别忘了提交" in r.stderr, (
        f"收尾对账失败把提交提醒一起吞了 —— cmd_verify 里又混进 exit 了？\n{r.stderr}"
    )
    assert r.returncode == 0, f"收尾对账被当成致命错误: rc={r.returncode}\n{r.stderr}"


def test_verify_subcommand_still_hard_fails_without_gh(tmp_path: Path) -> None:
    """把 need_gh 从 cmd_verify 挪到 case 分派之后，直接跑 verify 的门禁强度不能变。

    CI 的 lint job 跑的就是 `publish_models.sh verify`，它必须仍以非 0 退出，
    否则那道对账门禁等于被静默摘掉。
    """
    sandbox = _Sandbox(tmp_path, _tiny_lock(["a.onnx"]))
    gh = sandbox.bin / "gh"
    gh.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "$GH_CALLS"\n'
        'echo "gh: not logged in" >&2; exit 1\n',
        encoding="utf-8",
    )
    gh.chmod(0o755)

    r = sandbox.run("verify")

    assert r.returncode != 0, f"gh 不可用时 verify 却判绿:\n{r.stderr}"


def test_upload_proceeds_when_fileset_matches_lock(tmp_path: Path) -> None:
    """文件集一致时不该被护栏拦下 —— 否则常规的"重传同一批模型"就用不了了。"""
    names = ["a.onnx", "b.onnx"]
    sandbox = _Sandbox(tmp_path, _tiny_lock(names))
    d = _models_dir(tmp_path, names)
    # 刻意让假 Release 空着：本用例只关心"护栏没拦住上传"，末尾那次 cmd_verify 会把两个
    # 资产都报成"Release 上缺失"并 return 1 —— 那是非致命的，各由
    # test_verify_passes_when_assets_match_lock（对账通过）和
    # test_upload_survives_any_gh_failure_in_trailing_verify（失败不打穿）单独覆盖。
    sandbox.set_assets([])

    r = sandbox.run("upload", str(d))

    assert sandbox.wrote_to_release(), f"文件集一致却没上传: {sandbox.gh_calls()}\n{r.stderr}"
    # lock 被重算：size 应从占位的 4 变成真实字节数
    refreshed = json.loads(sandbox.lock.read_text(encoding="utf-8"))
    assert {f["name"] for f in refreshed["files"]} == set(names)
    assert all(f["size"] == len(b"fake") for f in refreshed["files"])


def test_upload_drift_can_be_forced_with_env(tmp_path: Path) -> None:
    """真在增删模型时要有逃生口，否则换代根本做不了。"""
    sandbox = _Sandbox(tmp_path, _tiny_lock(["a.onnx"]))
    d = _models_dir(tmp_path, ["a.onnx", "b.onnx"])
    sandbox.set_assets([])

    r = sandbox.run("upload", str(d), MILOCO_MODELS_ALLOW_LOCK_DRIFT="1")

    assert sandbox.wrote_to_release(), f"逃生口没放行: {sandbox.gh_calls()}\n{r.stderr}"
    refreshed = json.loads(sandbox.lock.read_text(encoding="utf-8"))
    assert {f["name"] for f in refreshed["files"]} == {"a.onnx", "b.onnx"}


def test_upload_rejects_empty_dir(tmp_path: Path) -> None:
    sandbox = _Sandbox(tmp_path, _tiny_lock(["a.onnx"]))
    empty = tmp_path / "empty"
    empty.mkdir()

    r = sandbox.run("upload", str(empty))

    assert r.returncode != 0
    assert not sandbox.wrote_to_release()


# ── 杂项 ────────────────────────────────────────────────────────────────


def test_unknown_subcommand_fails(sandbox: _Sandbox) -> None:
    r = sandbox.run("nope")
    assert r.returncode != 0
    assert "未知子命令" in r.stderr


def test_help_prints_usage_without_truncation(sandbox: _Sandbox) -> None:
    """--help 打的是抬头注释块。用 awk 到"第一个非 # 行"为止而不是写死行号，
    往抬头补一条说明才不会把尾巴无声截掉。"""
    r = sandbox.run("--help")
    assert r.returncode == 0
    for sub in ("upload", "refresh-lock", "verify"):
        assert sub in r.stderr
    # 注释前缀应已剥掉
    assert not any(ln.startswith("#") for ln in r.stderr.splitlines())
