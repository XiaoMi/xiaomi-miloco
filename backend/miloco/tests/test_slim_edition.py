"""slim（独立 App）发行版测试。

slim 的定义：只保留 rule_only 场景触发 + web 管理页，不注册身份/宠物/家庭档案路由，
不启动 agent dispatcher / 定时任务 / ReID 补齐，从而可以完全不依赖 onnxruntime /
scipy / tokenizers / pi_heif 与任何 ONNX 模型运行。

这里同时守住两条底线：
1. ``MILOCO_EDITION`` 环境变量优先于 ``settings.app.edition``，非法值静默忽略；
2. slim 下 ``import miloco.main`` + 构造 ``PerceptionEngine(rule_only=True)`` 在**屏蔽
   全部重依赖**的解释器里也必须成功（用子进程做，避免 pytest 进程里已被别的用例导入）。
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from miloco.admin.router import router as admin_router

# 子进程里屏蔽的顶层包 = slim 版刻意不安装的依赖。
_BLOCKED_TOP_LEVEL = (
    "onnxruntime",
    "scipy",
    "tokenizers",
    "pi_heif",
    "fastmcp",
    "mcp",
    "zeroconf",
    "hf_xet",
    "huggingface_hub",
)

_BOOT_SCRIPT = f"""
import importlib.abc
import sys

BLOCKED = {set(_BLOCKED_TOP_LEVEL)!r}


class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        top = fullname.split(".")[0]
        if top in BLOCKED:
            raise ImportError("BLOCKED-" + top)
        return None


sys.meta_path.insert(0, Blocker())

from miloco.edition import get_edition

assert get_edition() == "slim", get_edition()

import miloco.main as m

# 身份/宠物/家庭档案三套路由在 slim 下必须**没被 import**（它们的 import 链会拉 scipy）。
for mod in ("miloco.person.router", "miloco.pet.router", "miloco.home_profile.router"):
    assert mod not in sys.modules, mod
assert "scipy" not in sys.modules
assert "onnxruntime" not in sys.modules

# 场景触发所需的路由仍在。
route_paths = {{getattr(r, "path", "") for r in m.app.routes}}
assert any(p.startswith("/api/scene") for p in route_paths), sorted(route_paths)[:30]

from miloco.perception.engine.api import PerceptionEngine
from miloco.perception.engine.config import PerceptionConfig

engine = PerceptionEngine(PerceptionConfig(rule_only=True))
assert engine._identity_lib is None
assert engine._tier_u_pool is None
assert engine._embedder is None

print("SLIM_BOOT_OK")
"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    from miloco.config.settings import reset_settings

    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_EDITION", "slim")
    reset_settings()
    app = FastAPI()
    app.include_router(admin_router, prefix="/api")
    yield TestClient(app)
    reset_settings()


# ─── edition 判定 ─────────────────────────────────────────────────────────────


def test_edition_env_wins_over_settings(monkeypatch):
    from miloco.edition import get_edition, is_slim_edition

    monkeypatch.delenv("MILOCO_EDITION", raising=False)
    assert get_edition() == "full"
    assert is_slim_edition() is False

    monkeypatch.setenv("MILOCO_EDITION", "SLIM")
    assert get_edition() == "slim"
    assert is_slim_edition() is True

    # 非法值当未设置，回落到 full（保守：宁可按完整版处理）。
    monkeypatch.setenv("MILOCO_EDITION", "mini")
    assert get_edition() == "full"


def test_edition_from_settings_field(monkeypatch):
    """无环境变量时读 settings.app.edition（App 之外的部署方式也能选 slim）。"""
    from miloco.config.settings import reset_settings

    monkeypatch.delenv("MILOCO_EDITION", raising=False)
    monkeypatch.setenv("MILOCO_APP__EDITION", "slim")
    reset_settings()
    try:
        from miloco.edition import get_edition

        assert get_edition() == "slim"
    finally:
        monkeypatch.delenv("MILOCO_APP__EDITION", raising=False)
        reset_settings()


def test_slim_home_falls_back_to_application_support(monkeypatch):
    """slim 且未设 MILOCO_HOME 时数据根走 macOS 规范位置，不再是 ~/.openclaw/miloco。"""
    from miloco.utils.paths import miloco_home

    monkeypatch.delenv("MILOCO_HOME", raising=False)
    monkeypatch.delenv("MILOCO_EDITION", raising=False)
    full_home = miloco_home()
    assert full_home.parts[-2:] == (".openclaw", "miloco")

    monkeypatch.setenv("MILOCO_EDITION", "slim")
    slim_home = miloco_home()
    assert slim_home.parts[-2:] == ("Application Support", "Miloco")

    # 显式 MILOCO_HOME 永远优先（launcher 就是靠这个注入）。
    monkeypatch.setenv("MILOCO_HOME", "/tmp/miloco-explicit")
    assert str(miloco_home()) == "/tmp/miloco-explicit"


# ─── 无重依赖启动 ─────────────────────────────────────────────────────────────


def test_slim_boot_without_heavy_deps(tmp_path):
    """屏蔽 onnxruntime/scipy/tokenizers/... 后整条 slim 启动链仍必须成立。"""
    env = {
        "MILOCO_EDITION": "slim",
        "MILOCO_HOME": str(tmp_path),
        "MILOCO_SERVER__TOKEN": "",
        "PATH": "/usr/bin:/bin",
    }
    proc = subprocess.run(
        [sys.executable, "-c", _BOOT_SCRIPT],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "SLIM_BOOT_OK" in proc.stdout


# ─── admin 端点 ───────────────────────────────────────────────────────────────


def test_edition_endpoint_reports_slim_capabilities(client):
    d = client.get("/api/admin/edition").json()["data"]
    assert d["edition"] == "slim"
    assert d["slim"] is True
    caps = d["capabilities"]
    for name in ("identity", "pet", "home_profile", "tasks", "schedule", "one_click_upgrade"):
        assert caps[name] is False, name
    assert caps["rule_only"] is True


def test_status_includes_edition(client):
    d = client.get("/api/admin/status").json()["data"]
    assert d["edition"] == "slim"


def test_upgrade_check_short_circuits_in_app(client, monkeypatch):
    """独立 App 不联网查 GitHub，也不提供一键升级。"""

    async def _boom():
        raise AssertionError("slim 下不应访问 GitHub releases API")

    monkeypatch.setattr("miloco.admin.router._fetch_latest_release", _boom)
    d = client.get("/api/admin/upgrade/check").json()["data"]
    assert d["deploy_kind"] == "app"
    assert d["has_update"] is False
    assert d["release_url"]


def test_upgrade_run_rejected_in_app(client):
    r = client.post("/api/admin/upgrade/run")
    assert r.status_code == 400
    assert "独立 App" in r.json()["detail"]


def test_deploy_kind_is_app_in_slim(client, monkeypatch):
    import miloco.admin.router as R

    assert R._deploy_kind() == "app"
    monkeypatch.delenv("MILOCO_EDITION", raising=False)
    assert R._deploy_kind() in ("release", "dev")
