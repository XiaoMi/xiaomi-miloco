"""``miloco_cli.client.ensure_no_proxy_for_local`` 单测。

与 backend 的 test_no_proxy_local.py 对称——两个包互不依赖、各留一份实现,
若只有一侧有回归守护,另一侧被改坏时 CI 全绿、故障表现却是 miloco-cli 打
127.0.0.1:1810 又开始走代理返 502。
"""

from __future__ import annotations

import os
import urllib.request

import httpx
import pytest
from httpx._utils import get_environment_proxies

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
    # 不读取开发机系统代理;具体场景再显式注入系统设置。
    for name in ("getproxies_macosx_sysconf", "getproxies_registry"):
        monkeypatch.setattr(urllib.request, name, lambda: {}, raising=False)
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


def test_empty_proxy_value_treated_as_explicit_opt_out(clean_proxy_env):
    """空值 = 显式取消该 scheme 代理(curl / CPython 通行约定),不该被系统代理覆盖。

    与 backend::test_empty_proxy_value_treated_as_explicit_opt_out 对称:两侧是
    逐行镜像的实现,守门只在一侧钉住的话,另一侧退回真值判断时 CI 全绿。
    """
    clean_proxy_env.setenv("https_proxy", "")
    clean_proxy_env.setattr(
        "miloco_cli.client._system_proxies", lambda: {"https": "http://sys:9999"}
    )
    ensure_no_proxy_for_local()
    assert os.environ["https_proxy"] == ""


def test_env_proxy_wins_over_system(clean_proxy_env):
    """两个协议均已显式配置时,不读取系统设置。"""
    clean_proxy_env.setenv("http_proxy", "http://env-set:1080")
    clean_proxy_env.setenv("https_proxy", "http://env-set:1080")
    clean_proxy_env.setattr(
        "miloco_cli.client._system_proxies",
        lambda: pytest.fail("env 已有代理时不该回退问系统"),
    )
    ensure_no_proxy_for_local()
    assert os.environ["https_proxy"] == "http://env-set:1080"


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


@pytest.mark.parametrize(
    ("proxy_env", "expected_proxies"),
    [
        ({}, {"http://": "http://sys:7897", "https://": "http://sys:7897"}),
        ({"http_proxy": ""}, {"https://": "http://sys:7897"}),
        ({"HTTP_PROXY": ""}, {"https://": "http://sys:7897"}),
        ({"https_proxy": ""}, {"http://": "http://sys:7897"}),
        ({"HTTPS_PROXY": ""}, {"http://": "http://sys:7897"}),
        ({"http_proxy": "http://user:1080"},
         {"http://": "http://user:1080", "https://": "http://sys:7897"}),
        ({"https_proxy": "http://user:1080"},
         {"http://": "http://sys:7897", "https://": "http://user:1080"}),
        ({"http_proxy": "", "https_proxy": ""}, {}),
        ({"HTTP_PROXY": "http://upper:1080", "http_proxy": ""},
         {"https://": "http://sys:7897"}),
        ({"HTTPS_PROXY": "http://upper:1080", "https_proxy": "http://lower:1080"},
         {"http://": "http://sys:7897", "https://": "http://lower:1080"}),
        ({"ALL_PROXY": "socks5://user:1080"}, {"all://": "socks5://user:1080"}),
        ({"all_proxy": "socks5://user:1080"}, {"all://": "socks5://user:1080"}),
        ({"ALL_PROXY": ""}, {}),
        ({"all_proxy": "", "https_proxy": "http://user:1080"},
         {"https://": "http://user:1080"}),
        ({"ALL_PROXY": "socks5://user:1080", "https_proxy": "http://user:1081"},
         {"all://": "socks5://user:1080", "https://": "http://user:1081"}),
    ],
    ids=["system", "empty-http", "empty-HTTP", "empty-https", "empty-HTTPS",
         "partial-http", "partial-https", "both-empty", "lowercase-empty-wins",
         "lowercase-value-wins", "ALL-socks", "all-socks", "empty-ALL",
         "empty-all-explicit-https", "ALL-with-explicit-https"],
)
def test_httpx_proxy_routes(clean_proxy_env, proxy_env, expected_proxies):
    """只替换系统设置来源,让真实 urllib 和 httpx 解析最终环境及路由。"""
    for name in ("getproxies_macosx_sysconf", "getproxies_registry"):
        clean_proxy_env.setattr(
            urllib.request, name,
            lambda: {"http": "http://sys:7897", "https": "http://sys:7897"},
            raising=False,
        )
    for key, value in proxy_env.items():
        clean_proxy_env.setenv(key, value)
    clean_proxy_env.setenv("NO_PROXY", "upper.example,localhost")
    clean_proxy_env.setenv("no_proxy", "lower.example,localhost")

    ensure_no_proxy_for_local()

    # 完整字典相等也守护条数,避免通配条目意外把整张代理表清空。
    expected = {
        **expected_proxies,
        "all://*upper.example": None,
        "all://*lower.example": None,
        "all://localhost": None,
        "all://127.0.0.1": None,
        "all://[::1]": None,
    }
    assert get_environment_proxies() == expected
    for key, value in proxy_env.items():
        assert os.environ[key] == value
    # 启动初始化重复执行不会改变已经确定的出口。
    ensure_no_proxy_for_local()
    assert get_environment_proxies() == expected


def test_httpx_client_selects_loopback_direct_and_cloud_proxy(clean_proxy_env):
    """真实 Client 的 transport 选择;不发网络请求、不依赖本机代理服务。"""
    clean_proxy_env.setenv("https_proxy", "http://proxy.invalid:7897")
    clean_proxy_env.setenv("http_proxy", "http://proxy.invalid:7897")
    ensure_no_proxy_for_local()
    with httpx.Client() as client:
        for scheme in ("http", "https"):
            for host in ("localhost", "127.0.0.1", "[::1]"):
                url = httpx.URL(f"{scheme}://{host}:1810")
                assert client._transport_for_url(url) is client._transport
            cloud = httpx.URL(f"{scheme}://cloud.example")
            assert client._transport_for_url(cloud) is not client._transport


def test_httpx_explicit_wildcard_bypasses_all(clean_proxy_env):
    """用户主动指定 NO_PROXY=* 时,仍应允许整表为空。"""
    clean_proxy_env.setenv("https_proxy", "http://user:1080")
    clean_proxy_env.setenv("NO_PROXY", "*")
    ensure_no_proxy_for_local()
    assert get_environment_proxies() == {}
