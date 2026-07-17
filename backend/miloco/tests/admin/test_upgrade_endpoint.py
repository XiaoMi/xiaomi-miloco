"""升级检测 / 一键升级端点与 helper 的单元测试。

覆盖（对齐规格 kind-percolating-valiant.md 的验证清单）:
- 版本比较 `_latest_is_newer`：newer / equal / older / dev 后缀 / 非法串容错。
- `_deploy_kind`：版本串含 hatch-vcs 本地段（.dev/+g）→ dev；干净 CalVer tag / unknown
  → release（基于**版本串**判定，不依赖 .git 探测——见 router._deploy_kind 注释）。
- `_norm_ver`：strip 前导 v。
- `GET /upgrade/check`：release+有新版 → has_update；GitHub 不可达 → reachable=false、
  不 500、has_update=false；dev 部署即使远端更新也 has_update=false（只提示不给一键）。
- `POST /upgrade/run`：dev → 400；release → 起 detached 进程（Popen 被 mock）+ 单飞 409。
"""

import asyncio
import re
import time
from pathlib import Path
from unittest.mock import patch

import miloco.admin.router as R
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from miloco.admin.router import router


@pytest.fixture
def client(tmp_path, monkeypatch):
    from miloco.config.settings import reset_settings

    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.delenv("MILOCO_DIRECTORIES__STORAGE", raising=False)
    reset_settings()
    # 每个用例都从干净的进程内缓存 / 单飞标志开始，避免用例间串味。
    R._upgrade_check_cache = {"ts": 0.0, "data": None}
    R._upgrade_state["started_at"] = 0.0
    app = FastAPI()
    app.include_router(router, prefix="/api")
    yield TestClient(app)
    reset_settings()
    R._upgrade_check_cache = {"ts": 0.0, "data": None}
    R._upgrade_state["started_at"] = 0.0


# ─── _norm_ver ────────────────────────────────────────────────────────────────


def test_norm_ver_strips_leading_v():
    assert R._norm_ver("v2026.7.3") == "2026.7.3"
    assert R._norm_ver("2026.7.3") == "2026.7.3"
    assert R._norm_ver("") == ""


# ─── _latest_is_newer ───────────────────────────────────────────────────────────


class TestLatestIsNewer:
    def test_strictly_newer(self):
        assert R._latest_is_newer("2026.7.2", "2026.7.3") is True

    def test_equal_is_not_newer(self):
        assert R._latest_is_newer("2026.7.3", "2026.7.3") is False

    def test_older_is_not_newer(self):
        assert R._latest_is_newer("2026.7.3", "2026.7.2") is False

    def test_v_prefix_normalized_both_sides(self):
        assert R._latest_is_newer("v2026.7.2", "v2026.7.3") is True

    def test_dev_suffix_pep440_prerelease_semantics(self):
        # PEP440：`X.dev5` 是 `X` 的预发布，严格小于正式 X。故正式 tag 视为“更新”
        # （dev 部署实际不给一键，此处只验证版本序关系正确）。
        assert R._latest_is_newer("2026.7.3.dev5+g1234567", "2026.7.3") is True
        assert R._latest_is_newer("2026.7.2.dev5+g1234567", "2026.7.3") is True
        # 已在正式版之上的本地 dev 构建（领先 tag）→ 不算有更新。
        assert R._latest_is_newer("2026.7.4.dev1+g1234567", "2026.7.3") is False

    def test_invalid_version_string_is_graceful(self):
        # 非法版本串不得抛（否则 /check 会 500）；保守视为无更新。
        assert R._latest_is_newer("not-a-version", "2026.7.3") is False
        assert R._latest_is_newer("2026.7.2", "garbage!!!") is False

    def test_packaging_missing_is_graceful(self):
        # packaging 是传递依赖；即便 ImportError 也应静默降级为“无更新”。
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "packaging.version" or name.startswith("packaging"):
                raise ImportError("no packaging")
            return real_import(name, *a, **k)

        with patch("builtins.__import__", side_effect=fake_import):
            assert R._latest_is_newer("2026.7.2", "2026.7.3") is False


# ─── _deploy_kind ────────────────────────────────────────────────────────────────


def test_deploy_kind_dev_when_version_has_local_segment():
    # hatch-vcs 对非 tag 构建写 .dev/+g 本地段 → dev（与所在目录有无 .git 无关，
    # 从而不会因 $HOME/venv 恰在某个无关 git 仓库里而把正式 wheel 误判成 dev）。
    with patch(
        "miloco.admin.router._pkg_version", return_value="2026.7.4.dev41+g1234567"
    ):
        assert R._deploy_kind() == "dev"


def test_deploy_kind_release_when_clean_version():
    with patch("miloco.admin.router._pkg_version", return_value="2026.7.3"):
        assert R._deploy_kind() == "release"


def test_deploy_kind_release_when_version_unknown():
    with patch("miloco.admin.router._pkg_version", return_value="unknown"):
        assert R._deploy_kind() == "release"


# ─── GET /upgrade/check ─────────────────────────────────────────────────────────


def test_check_release_has_update(client):
    async def fake_fetch():
        return {"tag": "v2026.7.3", "html_url": "https://gh/rel/2026.7.3"}

    with (
        # release 判定来自干净版本串 "2026.7.2"（无 .dev/+g 本地段），与 .git 无关。
        patch("miloco.admin.router._pkg_version", return_value="2026.7.2"),
        patch("miloco.admin.router._fetch_latest_release", side_effect=fake_fetch),
    ):
        resp = client.get("/api/admin/upgrade/check")
    body = resp.json()
    assert resp.status_code == 200
    d = body["data"]
    assert d["deploy_kind"] == "release"
    assert d["reachable"] is True
    assert d["current"] == "2026.7.2"
    assert d["latest"] == "2026.7.3"
    assert d["has_update"] is True
    assert d["release_url"] == "https://gh/rel/2026.7.3"


def test_check_up_to_date_no_update(client):
    async def fake_fetch():
        return {"tag": "v2026.7.2", "html_url": "https://gh/rel/2026.7.2"}

    with (
        patch("miloco.admin.router._pkg_version", return_value="2026.7.2"),
        patch("miloco.admin.router._fetch_latest_release", side_effect=fake_fetch),
    ):
        resp = client.get("/api/admin/upgrade/check")
    d = resp.json()["data"]
    assert d["reachable"] is True
    assert d["has_update"] is False


def test_check_github_unreachable_graceful(client):
    async def fake_fetch():
        return None  # 不可达 / 超时 / 解析失败

    with (
        patch("miloco.admin.router._pkg_version", return_value="2026.7.2"),
        patch("miloco.admin.router._fetch_latest_release", side_effect=fake_fetch),
    ):
        resp = client.get("/api/admin/upgrade/check")
    assert resp.status_code == 200  # 关键：不 500
    d = resp.json()["data"]
    assert d["reachable"] is False
    assert d["has_update"] is False
    assert d["latest"] is None
    assert d["release_url"] is None


def test_check_dev_deploy_never_has_update(client):
    async def fake_fetch():
        return {"tag": "v2026.7.3", "html_url": "https://gh/rel/2026.7.3"}

    with (
        # dev = 版本带 hatch-vcs 本地段
        patch(
            "miloco.admin.router._pkg_version",
            return_value="2026.7.2.dev5+g1234567",
        ),
        patch("miloco.admin.router._fetch_latest_release", side_effect=fake_fetch),
    ):
        resp = client.get("/api/admin/upgrade/check")
    d = resp.json()["data"]
    assert d["deploy_kind"] == "dev"
    assert d["has_update"] is False  # dev 只提示、不判 has_update


def test_check_uses_cache_within_ttl(client):
    calls = {"n": 0}

    async def fake_fetch():
        calls["n"] += 1
        return {"tag": "v2026.7.3", "html_url": "https://gh/rel"}

    with (
        patch("miloco.admin.router._pkg_version", return_value="2026.7.2"),
        patch("miloco.admin.router._fetch_latest_release", side_effect=fake_fetch),
    ):
        client.get("/api/admin/upgrade/check")
        client.get("/api/admin/upgrade/check")
    assert calls["n"] == 1  # 第二次命中服务端缓存，不再打 GitHub


def test_check_force_bypasses_fresh_cache(client):
    # 用户手动「检查更新」：force=true 跳过仍新鲜的缓存、强制现查一次。
    calls = {"n": 0}

    async def fake_fetch():
        calls["n"] += 1
        return {"tag": "v2026.7.3", "html_url": "https://gh/rel"}

    with (
        patch("miloco.admin.router._pkg_version", return_value="2026.7.2"),
        patch("miloco.admin.router._fetch_latest_release", side_effect=fake_fetch),
    ):
        client.get("/api/admin/upgrade/check")  # fetch #1，填缓存
        client.get("/api/admin/upgrade/check")  # 命中缓存，不 fetch
        client.get("/api/admin/upgrade/check?force=true")  # 跳过缓存 → fetch #2
    assert calls["n"] == 2


# ─── POST /upgrade/dismiss（已确认版本存后端，非浏览器）──────────────────────────


def test_dismiss_persists_and_check_returns_it(client):
    async def fake_fetch():
        return {"tag": "v2026.7.3", "html_url": "https://gh/rel"}

    with (
        patch("miloco.admin.router._pkg_version", return_value="2026.7.2"),
        patch("miloco.admin.router._fetch_latest_release", side_effect=fake_fetch),
    ):
        # 未 dismiss 时 check.dismissed 为空
        assert client.get("/api/admin/upgrade/check").json()["data"]["dismissed"] is None
        # 关 banner → POST dismiss（v 前缀被规整掉）
        resp = client.post("/api/admin/upgrade/dismiss?version=v2026.7.3")
        assert resp.status_code == 200
        assert resp.json()["data"]["dismissed"] == "2026.7.3"
        # check 现每次新读，带回已确认版本
        assert (
            client.get("/api/admin/upgrade/check").json()["data"]["dismissed"]
            == "2026.7.3"
        )


def test_dismiss_read_write_roundtrip_and_reset(client, tmp_path):
    # 存后端文件（MILOCO_HOME 下）；彻底删除 MILOCO_HOME 即清零——模拟"重装=干净"。
    assert R._read_dismissed() is None
    R._write_dismissed("v2026.7.3")
    assert R._read_dismissed() == "v2026.7.3"  # _write 存原样，check 端点侧做 _norm_ver
    (tmp_path / "upgrade_dismissed").unlink()
    assert R._read_dismissed() is None


# ─── POST /upgrade/run ──────────────────────────────────────────────────────────


def _seed_cache_newer():
    """把 /check 服务端缓存填成"远端有更新"，让 /run 的 has_update 前置校验通过。"""
    R._upgrade_check_cache = {
        "ts": 1.0,
        "data": {"tag": "v2026.7.3", "html_url": "https://gh/rel/2026.7.3"},
    }


def test_run_rejected_on_dev_deploy(client):
    _seed_cache_newer()
    with patch(
        "miloco.admin.router._pkg_version", return_value="2026.7.2.dev5+g1234567"
    ):  # dev
        resp = client.post("/api/admin/upgrade/run")
    assert resp.status_code == 400


def test_run_rejected_when_no_newer_release(client):
    # release 部署但已是最新（缓存 latest == current）→ 400，不触发无谓重装。
    R._upgrade_check_cache = {
        "ts": 1.0,
        "data": {"tag": "v2026.7.3", "html_url": "https://gh/rel"},
    }
    with patch("miloco.admin.router._pkg_version", return_value="2026.7.3"):
        resp = client.post("/api/admin/upgrade/run")
    # 400（非 409）：与"已在升级中"区分——前端 409 接管进度、400 提示失败。
    assert resp.status_code == 400


def test_run_rejected_when_latest_unknown(client):
    # 从未成功 check（缓存空）→ 无法确认有更新 → 保守 400，引导前端先 check。
    R._upgrade_check_cache = {"ts": 0.0, "data": None}
    with patch("miloco.admin.router._pkg_version", return_value="2026.7.2"):
        resp = client.post("/api/admin/upgrade/run")
    assert resp.status_code == 400


def test_run_starts_detached_and_singleflight(client):
    launched = {"n": 0, "argv": None}

    class FakePopen:
        def __init__(self, *a, **k):
            launched["n"] += 1
            launched["argv"] = a[0] if a else k.get("args")
            # 断言 detached 关键参数就位
            assert k.get("start_new_session") is True
            assert k.get("stdin") is not None

    _seed_cache_newer()
    with (
        patch("miloco.admin.router._pkg_version", return_value="2026.7.2"),  # release
        patch("miloco.admin.router.subprocess.Popen", FakePopen),
    ):
        resp1 = client.post("/api/admin/upgrade/run")
        assert resp1.status_code == 200
        assert resp1.json()["data"]["started"] is True
        assert resp1.json()["data"]["target"] == "2026.7.3"
        # 单飞：TTL 内二次触发被拒。
        resp2 = client.post("/api/admin/upgrade/run")
        assert resp2.status_code == 409
    assert launched["n"] == 1  # 只起了一个进程

    # 断言 detached 脚本的关键契约（回归保护——这些是进度/终态/安全的命脉）：
    argv = launched["argv"]
    assert argv[:2] == ["bash", "-lc"]
    script = argv[2]
    assert "export MILOCO_LANG=zh" in script  # 日志语言钉死 → 进度解析跨 locale 可靠
    assert "--agent-prepare" in script and "--agent-finish" in script
    assert "AGENT_UPGRADE_DONE" in script  # 终态标记：前端只认它判完成
    assert "AGENT_UPGRADE_FAILED" in script
    # 末尾无条件 service start：兜底 install.py 在 agent 流程 atexit 停掉的服务——这是
    # 升级成功后把 backend 拉回来的唯一机制，缺它则每次成功升级都留下死掉的服务。
    assert "miloco-cli service start" in script
    # 路径经 shlex.quote（URL 是常量 https://…，quote 后不带引号但含 ://）
    assert "https://github.com/XiaoMi/xiaomi-miloco" in script
    # 回归：curl 下载失败也必须落到 AGENT_UPGRADE_FAILED（终态契约，快失败）——
    # 不能用 set -e（会在写标记前提前中止），curl 须被 `|| rc=$?` 收进 rc。
    assert "set -e" not in script
    assert re.search(r"curl -fsSL \S+ -o \S+ \|\| rc=\$\?", script)


def test_run_singleflight_self_heals_after_ttl(client):
    # 单飞标志超过 TTL 视为上次尝试已失败，允许重新触发（时间戳自愈，防永久 409）。
    launched = {"n": 0}

    class FakePopen:
        def __init__(self, *a, **k):
            launched["n"] += 1

    _seed_cache_newer()
    R._upgrade_state["started_at"] = time.time() - (R._UPGRADE_SINGLEFLIGHT_TTL_S + 5)
    with (
        patch("miloco.admin.router._pkg_version", return_value="2026.7.2"),
        patch("miloco.admin.router.subprocess.Popen", FakePopen),
    ):
        resp = client.post("/api/admin/upgrade/run")
    assert resp.status_code == 200
    assert launched["n"] == 1


def test_run_singleflight_released_on_terminal_marker(client):
    # 快失败自愈：单飞标志仍在 TTL 内，但 upgrade.log 已写下终态标记（AGENT_UPGRADE_FAILED）→
    # 上次尝试已彻底结束、无进程在跑 → 放行重试（200），而非硬锁满 25min 后才可再试。
    launched = {"n": 0}

    class FakePopen:
        def __init__(self, *a, **k):
            launched["n"] += 1

    _seed_cache_newer()
    R._upgrade_state["started_at"] = time.time()  # 刚"失败"，远在 TTL 内
    _write_upgrade_log(
        "▸ 正在下载 miloco 安装包...\nAGENT_UPGRADE_FAILED rc=6\n"  # curl 连不上 → 终态
    )
    with (
        patch("miloco.admin.router._pkg_version", return_value="2026.7.2"),
        patch("miloco.admin.router.subprocess.Popen", FakePopen),
    ):
        resp = client.post("/api/admin/upgrade/run")
    assert resp.status_code == 200  # 终态证据 → 提前解除单飞
    assert launched["n"] == 1


def test_run_singleflight_held_while_upgrade_in_progress(client):
    # 反向保护：单飞 TTL 内且日志处于**非终态**（installing，慢升级仍在跑）→ 必须仍 409，
    # 不得起第二个 install.sh 抢下载缓存/日志/重启（不变量⑦保护的正是这一场景）。
    launched = {"n": 0}

    class FakePopen:
        def __init__(self, *a, **k):
            launched["n"] += 1

    _seed_cache_newer()
    R._upgrade_state["started_at"] = time.time()
    _write_upgrade_log(
        "▸ 正在下载 miloco 安装包...\n✓ miloco 安装包下载完成\n安装 miloco 服务端...\n"
    )
    with (
        patch("miloco.admin.router._pkg_version", return_value="2026.7.2"),
        patch("miloco.admin.router.subprocess.Popen", FakePopen),
    ):
        resp = client.post("/api/admin/upgrade/run")
    assert resp.status_code == 409  # 非终态 → 单飞照旧生效
    assert launched["n"] == 0  # 未起第二个进程


def test_run_launch_failure_releases_singleflight(client):
    # 回归：起进程失败（Popen 抛错）时必须释放单飞。场景 = 「快失败后重试」——上一轮已写终态
    # 标记、_upgrade_state["started_at"] 仍在 TTL 内，本次经终态短路放行进入启动段，但 open("w") 先把
    # 日志截断（终态标记丢失 → phase 退回 starting=非终态），Popen 随即抛错。若不释放单飞，
    # _upgrade_state["started_at"] 仍指向上一轮、日志又已非终态 → 后续 /run 恒 409 锁死用户，却根本没有
    # 进程在跑。启动失败即把 _upgrade_state["started_at"] 归零，允许立即重试。
    class RaisingPopen:
        def __init__(self, *a, **k):
            raise OSError("bash not found")

    _seed_cache_newer()
    R._upgrade_state["started_at"] = time.time()  # 上一轮尝试仍在 TTL 内
    _write_upgrade_log("▸ 正在下载 miloco 安装包...\nAGENT_UPGRADE_FAILED rc=6\n")  # 终态
    with (
        patch("miloco.admin.router._pkg_version", return_value="2026.7.2"),
        patch("miloco.admin.router.subprocess.Popen", RaisingPopen),
    ):
        resp = client.post("/api/admin/upgrade/run")
    assert resp.status_code == 500  # 启动失败 → 500
    assert R._upgrade_state["started_at"] == 0.0  # 未起进程 → 单飞被释放，不会锁死后续重试

    # 证明确实解锁：紧接着一次正常 /run（Popen 成功）应能立即起进程、返回 200。
    launched = {"n": 0}

    class FakePopen:
        def __init__(self, *a, **k):
            launched["n"] += 1

    with (
        patch("miloco.admin.router._pkg_version", return_value="2026.7.2"),
        patch("miloco.admin.router.subprocess.Popen", FakePopen),
    ):
        resp2 = client.post("/api/admin/upgrade/run")
    assert resp2.status_code == 200
    assert launched["n"] == 1


# ─── GET /upgrade/status（阶段解析）─────────────────────────────────────────────


def _write_upgrade_log(text: str) -> None:
    from miloco.config import get_settings

    log_dir = Path(get_settings().directories.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "upgrade.log").write_text(text, encoding="utf-8")


def test_status_idle_when_no_log(client):
    d = client.get("/api/admin/upgrade/status").json()["data"]
    assert d["phase"] == "idle"
    assert "running" not in d  # running 已移除（重启后进程内标志丢失，不可靠）
    assert "percent" not in d  # percent 已移除，不再有虚假百分比


def test_status_downloading_when_download_marker_is_latest(client):
    # 关键回归：install.py 先打印「安装核心组件」标题、再打印「正在下载」。按出现位置最靠后
    # 取阶段（rfind）→ 下载标记在后 → downloading（旧的"列表顺序"实现会误判成 installing）。
    _write_upgrade_log(
        "2/3 安装核心组件\n安装 miloco 服务和命令行工具\n"
        "▸ 正在下载 miloco 安装包...\n  miloco-linux-x86_64-2026.7.3.tar.gz 12/68 MB\n"
    )
    d = client.get("/api/admin/upgrade/status").json()["data"]
    assert d["phase"] == "downloading"


def test_status_installing_when_install_marker_is_latest(client):
    # 下载完成后出现更靠后的安装标记 → installing
    _write_upgrade_log(
        "▸ 正在下载 miloco 安装包...\n  miloco-linux-x86_64-2026.7.3.tar.gz\n"
        "✓ miloco 安装包下载完成\n安装 miloco 服务端...\n"
    )
    d = client.get("/api/admin/upgrade/status").json()["data"]
    assert d["phase"] == "installing"


def test_status_done_marker(client):
    # 脚本末尾 echo 的终态标记（位置在最后）→ done，前端只认它判完成
    _write_upgrade_log("安装 miloco 服务端\n解压感知模型\nAGENT_UPGRADE_DONE\n")
    d = client.get("/api/admin/upgrade/status").json()["data"]
    assert d["phase"] == "done"


def test_status_failed_marker(client):
    _write_upgrade_log("安装 miloco 服务端\nAGENT_UPGRADE_FAILED rc=1\n")
    d = client.get("/api/admin/upgrade/status").json()["data"]
    assert d["phase"] == "failed"


def test_status_starting_when_log_empty(client):
    _write_upgrade_log("some unrelated preamble\n")
    d = client.get("/api/admin/upgrade/status").json()["data"]
    assert d["phase"] == "starting"


# ─── 跨文件契约守护：进度锚点 vs 安装器文案 ──────────────────────────────────────


def _find_installer_zh_json():
    """从 router.py 向上找 scripts/i18n/zh.json（对目录层级不做硬编码假设）。"""
    for base in Path(R.__file__).resolve().parents:
        cand = base / "scripts" / "i18n" / "zh.json"
        if cand.exists():
            return cand
    return None


def test_upgrade_stage_anchors_exist_in_installer_zh_json():
    # _read_upgrade_phase 靠 grep install.py 打印的中文日志文案判阶段，这些文案归属
    # scripts/i18n/zh.json——二者是隐式的跨文件契约。install.py 一旦改文案，下载/安装步会
    # **静默**失准（升级不坏：终态标记 AGENT_UPGRADE_* + 连不上判重启仍可靠，只是步骤高亮
    # 漂移）。此测试把该耦合显式化，让安装器改文案时 CI 直接失败、提醒同步 _UPGRADE_STAGES。
    zh_path = _find_installer_zh_json()
    if zh_path is None:
        pytest.skip("scripts/i18n/zh.json 不在源码树内（非源码运行环境），跳过契约守护")
    zh_text = zh_path.read_text(encoding="utf-8")
    # 只校验语言相关的中文锚点；.tar.gz / AGENT_* 是语言无关标记，不属该契约。
    cn_anchors = [m for m, _ph in R._UPGRADE_STAGES if not m.isascii()]
    assert cn_anchors, "预期 _UPGRADE_STAGES 含中文锚点，实为空——契约守护失效"
    missing = [m for m in cn_anchors if m not in zh_text]
    assert not missing, (
        "以下进度锚点已不在 scripts/i18n/zh.json 中，install.py 文案疑似变更，"
        f"请同步更新 router._UPGRADE_STAGES：{missing}"
    )


# ─── /check 缓存（正/负）────────────────────────────────────────────────────────


def test_check_negative_caches_failure(client):
    calls = {"n": 0}

    async def fake_fetch():
        calls["n"] += 1
        return None  # 不可达

    with (
        patch("miloco.admin.router._pkg_version", return_value="2026.7.2"),
        patch("miloco.admin.router._fetch_latest_release", side_effect=fake_fetch),
    ):
        client.get("/api/admin/upgrade/check")
        client.get("/api/admin/upgrade/check")
    assert calls["n"] == 1  # 失败也进负缓存，短 TTL 内不重复打 GitHub


def test_check_keeps_last_good_on_transient_failure(client):
    # 已有好结果后 GitHub 抖动（fetch=None）：保留上次好值，不清空 → banner 不会因一次
    # 不可达而消失（has_update 仍为 true）。
    seq = [
        {"tag": "v2026.7.3", "html_url": "u"},  # 首次成功
        None,  # 第二次抖动
    ]

    async def fake_fetch():
        return seq.pop(0)

    with (
        patch("miloco.admin.router._pkg_version", return_value="2026.7.2"),
        patch("miloco.admin.router._fetch_latest_release", side_effect=fake_fetch),
    ):
        r1 = client.get("/api/admin/upgrade/check").json()["data"]
        assert r1["has_update"] is True and r1["reachable"] is True
        # 强制过期以触发第二次 fetch（返回 None）
        R._upgrade_check_cache["ts"] = time.time() - R._UPGRADE_CHECK_TTL_S - 5
        r2 = client.get("/api/admin/upgrade/check").json()["data"]
    assert r2["has_update"] is True  # 抖动没清空好值
    assert r2["latest"] == "2026.7.3"


def test_check_refetches_after_ttl(client):
    calls = {"n": 0}

    async def fake_fetch():
        calls["n"] += 1
        return {"tag": "v2026.7.3", "html_url": "u"}

    with (
        patch("miloco.admin.router._pkg_version", return_value="2026.7.2"),
        patch("miloco.admin.router._fetch_latest_release", side_effect=fake_fetch),
    ):
        client.get("/api/admin/upgrade/check")  # fetch #1
        R._upgrade_check_cache["ts"] = time.time() - R._UPGRADE_CHECK_TTL_S - 5
        client.get("/api/admin/upgrade/check")  # 过期 → fetch #2
    assert calls["n"] == 2


# ─── _fetch_latest_release 真实分支（httpx 被 mock）────────────────────────────


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        if isinstance(self._resp, Exception):
            raise self._resp
        return self._resp


def _patch_httpx(resp):
    return patch(
        "miloco.admin.router.httpx.AsyncClient", lambda *a, **k: _FakeClient(resp)
    )


def test_fetch_latest_release_ok():
    with _patch_httpx(_FakeResp(200, {"tag_name": "v2026.7.3", "html_url": "u"})):
        assert asyncio.run(R._fetch_latest_release()) == {
            "tag": "v2026.7.3",
            "html_url": "u",
        }


def test_fetch_latest_release_non_200_returns_none():
    with _patch_httpx(_FakeResp(403, {})):  # 限流 / 权限
        assert asyncio.run(R._fetch_latest_release()) is None


def test_fetch_latest_release_missing_tag_returns_none():
    with _patch_httpx(_FakeResp(200, {"html_url": "u"})):
        assert asyncio.run(R._fetch_latest_release()) is None


def test_fetch_latest_release_exception_returns_none():
    with _patch_httpx(RuntimeError("network down")):
        assert asyncio.run(R._fetch_latest_release()) is None
