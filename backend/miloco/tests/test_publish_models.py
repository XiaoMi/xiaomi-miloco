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


def test_verify_tolerates_a_lock_entry_without_size(sandbox: _Sandbox) -> None:
    """lock 的 size 是**可选**字段，verify 不能拿它下标取。

    下载侧对它明确容忍：_check_spec 把写成 null 的 size 删掉（"与整条不写同义"），
    _is_ready 只认 sha256 —— 也就是说一条只有 name + sha256 的记录是完全合法的输入。
    这里若写 `w["size"]`，手工新增模型时先不填 size 就会吐一段 KeyError traceback，与
    本脚本"出错就一行中文 + 非 0"的口径正相反：维护者看到 Python 栈，第一反应是脚本
    坏了或 gh 认证过期，不会想到是自己少打了一个字段。而 verify 已挂在 CI 的 lint job
    上，崩的是每个 PR。
    """
    lock = _real_lock()
    sandbox.set_assets(_assets_from(lock))  # 先按完整 lock 造资产
    lock["files"][0].pop("size")
    sandbox.write_lock(lock)

    r = sandbox.run("verify")
    assert "Traceback" not in r.stderr, f"缺一个可选字段就吐 traceback：\n{r.stderr}"
    assert r.returncode == 0, r.stderr
    assert "跳过大小比对" in r.stderr
    assert "::error::" not in r.stderr


def test_verify_refuses_when_neither_size_nor_digest_can_be_compared(
    sandbox: _Sandbox,
) -> None:
    """size 与 sha256 一个都没比上时判红——这条记录等于没对账。

    与上一条配对：容忍缺 size 之后，若再顺着把"digest 也没有"降级成 warning，verify
    就会对一条**什么都没校验过**的记录亮绿，而它退 0 的含义是"线上资产与 lock 一致"。
    那是拿一句假话换绿灯，正好是本脚本 fail-closed 方向的反面。措辞也得跟着变：这次
    连 size 都没比，不能沿用"本次只比对了 size"。
    """
    lock = _real_lock()
    assets = [{k: v for k, v in a.items() if k != "digest"} for a in _assets_from(lock)]
    sandbox.set_assets(assets)
    lock["files"][0].pop("size")
    sandbox.write_lock(lock)

    r = sandbox.run("verify")
    assert "Traceback" not in r.stderr, r.stderr
    assert r.returncode == 1, f"两个字段都没比上却退 0：\n{r.stderr}"
    assert "没有任何可比对的字段" in r.stderr
    assert lock["files"][0]["name"] in r.stderr


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


@pytest.mark.parametrize(
    ("lock_names", "dir_names", "expect_in_stderr"),
    [
        (["a.onnx", "b.onnx", "c.onnx"], ["a.onnx", "b.onnx"], "c.onnx"),
        (["a.onnx"], ["a.onnx", "b.onnx"], "b.onnx"),
        (["det_4C.onnx"], ["det_5C.onnx"], "det_5C.onnx"),
    ],
)
def test_refresh_lock_alone_also_refuses_fileset_drift(
    tmp_path: Path, lock_names: list[str], dir_names: list[str], expect_in_stderr: str
) -> None:
    """``refresh-lock`` 单独跑时护栏也要开火 —— 这是**另一份**护栏。

    脚本里有两处同样的文件集比对：一处在 cmd_upload 里（必须早于 gh release upload，
    由 test_upload_aborts_before_touching_release_on_fileset_drift 钉住），一处在
    refresh_lock_from_dir 里。upload 走的是前者、且在那儿就中止了，所以上面那批用例
    对后者一个字节都碰不到 —— 把 refresh_lock_from_dir 里那 10 行整段删掉，本文件其余
    用例全绿（实测过）。而 refresh-lock 子命令走的正是后者，且它不经过 cmd_upload：

      · ``refresh-lock <dir>``  直接调 refresh_lock_from_dir；
      · ``refresh-lock``（不带 dir）先 gh release download 全量到临时目录再调它 ——
        换代残留正是从这条路进来的（upload 只 --clobber 同名资产，从不删旧的）。

    护栏没了的后果是静默改表：少了就无声缩表（剩下的模型从此不再下发，线上表现是
    "某天起某功能悄悄降级"，没有任何失败点），多了就以 required=false 收编进 lock。
    两种都不报错、都会把结果写进磁盘，所以只能靠"退非零 + lock 原样"来钉。
    """
    sandbox = _Sandbox(tmp_path, _tiny_lock(lock_names))
    before = sandbox.lock.read_text(encoding="utf-8")
    d = _models_dir(tmp_path, dir_names)

    r = sandbox.run("refresh-lock", str(d))

    assert r.returncode != 0, f"护栏没开火: {r.stdout}\n{r.stderr}"
    assert expect_in_stderr in r.stderr
    assert "拒绝静默改表" in r.stderr
    # 核心断言：lock 一个字节都没被改写。少了缩表 / 多了收编都是**写盘**动作，
    # 只断言退出码的话，"先写后报错"这种半吊子实现照样能骗过去。
    assert sandbox.lock.read_text(encoding="utf-8") == before, "lock 已被改写"
    # 逃生口仍在（与 upload 那条同一个环境变量），否则换代根本做不了
    r2 = sandbox.run("refresh-lock", str(d), MILOCO_MODELS_ALLOW_LOCK_DRIFT="1")
    assert r2.returncode == 0, r2.stderr
    got = {f["name"] for f in json.loads(sandbox.lock.read_text(encoding="utf-8"))["files"]}
    assert got == set(dir_names)


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


def test_upload_sees_models_reached_through_symlinks(tmp_path: Path) -> None:
    """同一个目录的两个读者要给出同一个文件集——符号链接也算数。

    维护者侧有两处读模型目录：refresh-lock 那段 Python 用 `p.is_file()`（跟随链接），
    upload 用 `find -type f`（不加 -L 时判的是链接**自身**的类型，一个链接都不匹配）。
    口径不一致的表现是：模型放在外部盘、目录里只留链接时，refresh-lock 正常刷出全部
    条目，upload 却报"没有 .onnx / .json 模型文件"——看起来像脚本认不出明明在那儿的
    文件，而 `ls` 和 `refresh-lock` 都说它们在。
    """
    names = ["a.onnx", "b.onnx"]
    sandbox = _Sandbox(tmp_path, _tiny_lock(names))
    store_root = tmp_path / "store"
    store_root.mkdir()
    store = _models_dir(store_root, names)
    linked = tmp_path / "linked"
    linked.mkdir()
    for n in names:
        (linked / n).symlink_to(store / n)
    sandbox.set_assets([])

    r = sandbox.run("upload", str(linked))

    assert "没有 .onnx / .json 模型文件" not in r.stderr, (
        f"目录里 {len(names)} 个链接指向真实模型，upload 却说一个都没有：\n{r.stderr}"
    )
    assert sandbox.wrote_to_release(), f"没发生上传: {sandbox.gh_calls()}\n{r.stderr}"

    # 上传前那份"我要传这些、各多大"的人工复核清单也要报真实大小：du 不加 -L 时
    # 报的是链接自身的占用（macOS 打 0B、Linux 打 0），整份清单等于废掉。
    # 不写死单位，只要求不是 0 开头 —— 各平台的块大小与单位写法都不一样。
    listed: dict[str, str] = {}
    for ln in (r.stdout + r.stderr).splitlines():
        parts = ln.split()
        if len(parts) == 2 and parts[1] in names:
            listed[parts[1]] = parts[0]
    assert set(listed) == set(names), f"上传清单没列全: {listed}\n{r.stderr}"
    assert not any(v.startswith("0") for v in listed.values()), (
        f"上传清单把链接的大小报成 0（du 少了 -L）: {listed}"
    )


def test_upload_drift_can_be_forced_with_env(tmp_path: Path) -> None:
    """真在增删模型时要有逃生口，否则换代根本做不了。"""
    sandbox = _Sandbox(tmp_path, _tiny_lock(["a.onnx"]))
    d = _models_dir(tmp_path, ["a.onnx", "b.onnx"])
    sandbox.set_assets([])

    r = sandbox.run("upload", str(d), MILOCO_MODELS_ALLOW_LOCK_DRIFT="1")

    assert sandbox.wrote_to_release(), f"逃生口没放行: {sandbox.gh_calls()}\n{r.stderr}"
    refreshed = json.loads(sandbox.lock.read_text(encoding="utf-8"))
    assert {f["name"] for f in refreshed["files"]} == {"a.onnx", "b.onnx"}


def test_refresh_lock_keeps_a_required_entry_required_when_the_key_is_missing(
    tmp_path: Path,
) -> None:
    """同名旧条目漏写 ``required`` 时不许被翻成可选。

    「lock 条目由人手写/手补」是这套流程**文档化的正常路径**（refresh 里那句"新增项
    记得手工把 required / desc 与 resource_validator.py 对齐"）。而 fetch_models 缺键
    fail-closed 判必需、这边缺键判可选的话，漏写的那阵子一切都是绿的（下载器一直当它
    必需，没有任何信号提示写漏了），下一次 refresh 才把它静默翻成可选 —— 文件集护栏
    只比名字集合，看不见"同名但少一个键"这种漂移。

    翻面的代价落在不看 required 之外那层严格开关的调用上。``--strict`` 会把可选失败
    并进必需，所以带它的调用（build.sh、两个 workflow、local-ci.sh、install-hermes.sh
    的门禁与补齐）扛得住这次翻面；扛不住的是**判据里 required 仍然当真**的那些：
    ``--required-only`` 直接把它跳过不下，不带 --strict 的 ``--check``（ci.yml 那步
    下载）判绿，感知侧 resource_validator 也据此决定缺了它算不算 PREREQ_MISSING。
    一个本该必需的模型就这么从"判红"降级成"没人管"，而全程零信号。

    反方向一并钉住：显式写了 ``required: false`` 的条目必须原样保留，别为了修这条
    就把所有东西都翻成必需。
    """
    lock = _tiny_lock(["a.onnx", "b.onnx"])
    for f in lock["files"]:
        if f["name"] == "a.onnx":
            del f["required"]  # 手工补 lock 时漏写
        else:
            f["required"] = False  # 显式可选，必须原样留着
    sandbox = _Sandbox(tmp_path, lock)
    d = _models_dir(tmp_path, ["a.onnx", "b.onnx"])

    r = sandbox.run("refresh-lock", str(d))

    # 文件集全等，护栏本就不该开火 —— 翻面正是发生在这条"看起来什么都没变"的路径上
    assert r.returncode == 0, r.stderr
    got = {f["name"]: f["required"] for f in json.loads(sandbox.lock.read_text("utf-8"))["files"]}
    assert got == {"a.onnx": True, "b.onnx": False}, r.stderr


def test_refresh_lock_keeps_a_required_entry_required_when_the_key_is_null(
    tmp_path: Path,
) -> None:
    """``required: null`` 与"整条不写"同义，同样不许被翻成可选。

    上一条钉的是"漏写键"，这条钉的是"写了 null"——两者在 ``.get`` 面前**不是**一回事：
    默认值只在键不存在时生效，键在、值是 None 时原样返回 None，于是 ``bool(None)``
    把它判成可选。缺键那条 fail-closed 兜得住，写成 null 反而兜不住，正好是相反的结论。

    下载器那边已经把这两者显式合流了（_check_spec 把值为 None 的 size / required
    整个键删掉，再走 fail-closed 默认），所以这不是"要不要多防一手"，而是同一份 lock
    在生成侧与消费侧读出两个相反的 required —— 而 refresh 是**写**方，翻面会落盘。

    先占位、回头再填是手写 lock 时很自然的写法，两个键都撞得上。
    """
    lock = _tiny_lock(["a.onnx", "b.onnx"])
    for f in lock["files"]:
        if f["name"] == "a.onnx":
            f["required"] = None  # 占位待填
        else:
            f["required"] = False  # 显式可选，必须原样留着
    sandbox = _Sandbox(tmp_path, lock)
    d = _models_dir(tmp_path, ["a.onnx", "b.onnx"])

    r = sandbox.run("refresh-lock", str(d))

    assert r.returncode == 0, r.stderr
    got = {f["name"]: f["required"] for f in json.loads(sandbox.lock.read_text("utf-8"))["files"]}
    assert got == {"a.onnx": True, "b.onnx": False}, r.stderr


def test_refresh_lock_still_defaults_a_brand_new_file_to_optional(tmp_path: Path) -> None:
    """真正新增的文件仍默认可选 —— fail-closed 只适用于"同名旧条目缺键"。

    新增走的是护栏显式放行（要 MILOCO_MODELS_ALLOW_LOCK_DRIFT=1）那条路，人已经在
    环里、且被提示要手工对齐 required；默认判必需反而会让一个刚进来、还没人确认过的
    文件立刻变成能判红全部构建的硬依赖。
    """
    sandbox = _Sandbox(tmp_path, _tiny_lock(["a.onnx"]))
    d = _models_dir(tmp_path, ["a.onnx", "new.onnx"])

    r = sandbox.run("refresh-lock", str(d), MILOCO_MODELS_ALLOW_LOCK_DRIFT="1")

    assert r.returncode == 0, r.stderr
    got = {f["name"]: f["required"] for f in json.loads(sandbox.lock.read_text("utf-8"))["files"]}
    assert got == {"a.onnx": True, "new.onnx": False}, r.stderr


def test_upload_rejects_empty_dir(tmp_path: Path) -> None:
    sandbox = _Sandbox(tmp_path, _tiny_lock(["a.onnx"]))
    empty = tmp_path / "empty"
    empty.mkdir()

    r = sandbox.run("upload", str(empty))

    assert r.returncode != 0
    assert not sandbox.wrote_to_release()


# ── 坏 lock：一行中文，不是 traceback ────────────────────────────────────

# 两个分支各自 refresh 过 lock，合并出来的工作区里这个文件必然是非法 JSON。
_CONFLICTED = "<<<<<<< HEAD\n{}\n=======\n{}\n>>>>>>> other\n"


@pytest.mark.parametrize("sub", ["upload", "refresh-lock", "verify"])
def test_broken_lock_reports_one_line_instead_of_a_traceback(tmp_path: Path, sub: str) -> None:
    """坏 lock 在**每个**子命令上都必须是一行中文 + 非 0，不许吐 traceback。

    参数化的这一轴才是要害。lock 一共有 4 处读取点，只把顶层那次 `TAG=` 延后、再按
    "这个子命令用不用得上 tag"决定要不要校验的话，`refresh-lock <dir>` 恰好被豁免掉
    （它确实不需要 tag），可 refresh_lock_from_dir 内部照样 json.loads 同一份文件 ——
    traceback 原样还在，而"合并完先跑一次 refresh-lock"正是最容易撞上冲突标记的那条路。
    所以校验按"要不要读 lock"收在同一个入口，而不是按"要不要 TAG"分叉。

    口径对齐的是 fetch_models.py：同一份坏 lock 交给它是一行中文 + 非 0，交给这边却是
    traceback 的话，CI lint job 红出来读的人第一反应是"脚本崩了"而不是"清单坏了"。
    """
    sandbox = _Sandbox(tmp_path, _tiny_lock(["a.onnx"]))
    sandbox.lock.write_text(_CONFLICTED, encoding="utf-8")
    args = [sub, str(_models_dir(tmp_path, ["a.onnx"]))] if sub != "verify" else [sub]

    r = sandbox.run(*args)

    assert r.returncode != 0
    assert "Traceback" not in r.stderr, r.stderr
    assert "lock" in r.stderr
    # 坏 lock 不许走到任何不可逆的写操作
    assert not sandbox.wrote_to_release(), sandbox.gh_calls()


def test_empty_release_tag_is_rejected_too(tmp_path: Path) -> None:
    """`release_tag: ""` 是另一条分支：JSON 合法、取值成功，但拼出来的 URL 会指向别处。

    与上面那条分开：那条走的是"python 退非 0 → die"，这条走的是取到空串后的显式判空。
    """
    sandbox = _Sandbox(tmp_path, _tiny_lock(["a.onnx"]))
    sandbox.lock.write_text('{"release_tag": "", "files": []}', encoding="utf-8")

    r = sandbox.run("verify")

    assert r.returncode != 0
    assert "Traceback" not in r.stderr, r.stderr
    assert "release_tag" in r.stderr


def test_help_still_works_with_a_broken_lock(tmp_path: Path) -> None:
    """--help 不读 lock，就不该被 lock 拖下水。

    原来那次 `TAG=` 在顶层求值，位置既早于 die 的定义也早于 case 分派，于是 lock 一坏，
    连"这脚本怎么用"都问不出来 —— 而人在这个时候恰恰最需要看一眼用法。
    """
    sandbox = _Sandbox(tmp_path, _tiny_lock(["a.onnx"]))
    sandbox.lock.write_text(_CONFLICTED, encoding="utf-8")

    r = sandbox.run("--help")

    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr
    assert "refresh-lock" in r.stderr  # 用法正文真的打出来了


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
