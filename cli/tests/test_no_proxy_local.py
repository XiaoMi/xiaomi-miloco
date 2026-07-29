"""``miloco_cli.client.ensure_no_proxy_for_local`` 单测。

与 backend 的 test_no_proxy_local.py 对称——两个包互不依赖、各留一份实现,
若只有一侧有回归守护,另一侧被改坏时 CI 全绿、故障表现却是 miloco-cli 打
127.0.0.1:1810 又开始走代理返 502。
"""

from __future__ import annotations

import os

import pytest

# 在 import 被测模块**之前**快照:虽然 client.py 已不再自带 import 副作用
# (调用挪到了 miloco_cli.main),但本文件的子进程用例会 import main、而 main
# 一 import 就写 env;保留快照+还原,免得泄漏进同会话后续测试。
_PROXY_ENV_AT_IMPORT = {
    k: v for k, v in os.environ.items() if k.lower().endswith("_proxy")
}

from miloco_cli.client import ensure_no_proxy_for_local  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _restore_proxy_env_after_module():
    yield
    for key in [k for k in os.environ if k.lower().endswith("_proxy")]:
        del os.environ[key]
    os.environ.update(_PROXY_ENV_AT_IMPORT)


@pytest.fixture
def clean_proxy_env(monkeypatch):
    """清掉所有 *_proxy;退出时按快照还原(被测函数直接写 os.environ,
    monkeypatch 对当时不存在的 key 不记录、无从还原)。"""
    saved = {k: v for k, v in os.environ.items() if k.lower().endswith("_proxy")}
    for key in saved:
        del os.environ[key]
    yield monkeypatch
    for key in [k for k in os.environ if k.lower().endswith("_proxy")]:
        del os.environ[key]
    os.environ.update(saved)


def test_loopback_appended_and_cases_identical(clean_proxy_env):
    ensure_no_proxy_for_local()
    entries = os.environ["NO_PROXY"].split(",")
    assert {"localhost", "127.0.0.1", "::1"} <= set(entries)
    assert os.environ["no_proxy"] == os.environ["NO_PROXY"]


def test_no_cidr_entries(clean_proxy_env):
    ensure_no_proxy_for_local()
    assert "/" not in os.environ["NO_PROXY"]


def test_uppercase_only_no_proxy_not_dropped(clean_proxy_env):
    """只设大写 NO_PROXY 时排除项不能丢(CPython 折叠时小写胜出)。"""
    clean_proxy_env.setenv("NO_PROXY", "api.internal.corp")
    ensure_no_proxy_for_local()
    assert "api.internal.corp" in os.environ["no_proxy"]
    assert os.environ["no_proxy"] == os.environ["NO_PROXY"]


def test_snapshot_survives_preexisting_bare_no_proxy(clean_proxy_env):
    """env 里事先有裸 NO_PROXY 时,快照不能被 getproxies 的 env 短路成空。"""
    clean_proxy_env.setenv("NO_PROXY", "preexisting.example")
    clean_proxy_env.setattr(
        "miloco_cli.client._system_proxies",
        lambda: {"https": "http://sys:7897"},
    )
    ensure_no_proxy_for_local()
    assert os.environ["https_proxy"] == "http://sys:7897"
    assert "preexisting.example" in os.environ["no_proxy"]


def test_all_proxy_only_user_not_overridden(clean_proxy_env):
    """只配 ALL_PROXY(纯 SOCKS 出口)时不被系统代理顶掉。"""
    clean_proxy_env.setenv("ALL_PROXY", "socks5://127.0.0.1:7891")
    clean_proxy_env.setattr(
        "miloco_cli.client._system_proxies", lambda: {"http": "http://sys:7890"}
    )
    ensure_no_proxy_for_local()
    assert "http_proxy" not in os.environ


def test_getproxies_failure_does_not_break_startup(clean_proxy_env):
    def boom():
        raise OSError("SystemConfiguration unavailable")

    clean_proxy_env.setattr("urllib.request.getproxies", boom)
    ensure_no_proxy_for_local()
    assert "127.0.0.1" in os.environ["NO_PROXY"]


def test_entrypoint_applies_no_proxy_end_to_end():
    """开子进程跑真实入口点,验证整条链路真的生效。

    其余用例都直接 import 被测函数,结构上抓不到"函数写对了但没人调用"这类
    失效——CLI 侧的注入此前挂在 commands/scope.py 的模块级 import 副作用上
    (18 个命令模块里唯一一个那么写的),把它改惰性就整条静默失效,退化表现
    正是本修复要消灭的 502。这里用子进程还原真实执行路径:干净 env 起
    miloco_cli.main,再看 NO_PROXY 有没有被写进去。
    """
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    out = subprocess.run(
        [sys.executable, "-c",
         "import miloco_cli.main, os; print(os.environ.get('NO_PROXY', ''))"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    entries = out.stdout.strip().split(",")
    assert {"localhost", "127.0.0.1", "::1"} <= set(entries), out.stdout
