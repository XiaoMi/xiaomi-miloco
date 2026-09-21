# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""``_ensure_no_proxy_for_local`` 单测。

守护两条容易回归、且回归后**静默**的性质:
1. 写 NO_PROXY 前必须先把系统代理快照进 env——否则 CPython 的
   ``getproxies_environment() or <系统代理>`` 短路会让系统代理整体消失,
   云端 API 直连、墙内不可达(症状在本地开发机上完全看不出来)。
2. 不写 RFC1918 CIDR——httpx 不做子网匹配,写了也只是无效条目。
"""

from __future__ import annotations

import os
import urllib.request

import httpx
import pytest
from httpx._utils import get_environment_proxies

# 必须在 import miloco.main **之前**快照:该模块在模块级就调用了
# _ensure_no_proxy_for_local(),import 的一瞬间就会把系统代理写进 os.environ,
# 早于任何 fixture。不在这里记下原始值,这些变量会泄漏到同一次 pytest 会话的
# 后续测试(且失败带顺序依赖,最难查那类)。
_PROXY_ENV_AT_IMPORT = {
    k: v for k, v in os.environ.items() if k.lower().endswith("_proxy")
}

from miloco.main import _ensure_no_proxy_for_local  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _restore_proxy_env_after_module():
    """本模块跑完把 *_proxy 恢复到 import 前的样子(含 import 期的副作用)。"""
    yield
    for key in [k for k in os.environ if k.lower().endswith("_proxy")]:
        del os.environ[key]
    os.environ.update(_PROXY_ENV_AT_IMPORT)


@pytest.fixture
def clean_proxy_env(monkeypatch):
    """清掉所有 *_proxy,模拟「纯系统代理、环境无代理变量」的事故环境。

    还原用显式快照而**不是** monkeypatch.delenv:被测函数直接写 os.environ、
    不经 monkeypatch,而 delenv 对**当时不存在**的 key 什么都不记录
    (pytest 的 delitem: ``if name not in dic: ...else: 记录``),于是函数新写入
    的 http_proxy / HTTPS_PROXY 等在 teardown 时无人还原,泄漏进同一次 pytest
    会话的后续测试——且失败带顺序依赖(单跑此文件绿、全量才红),最难查那类。
    这里退出时先清空所有 *_proxy 再灌回快照,与函数写了什么无关。
    """
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


def test_loopback_entries_appended(clean_proxy_env):
    _ensure_no_proxy_for_local()
    entries = os.environ["NO_PROXY"].split(",")
    assert "localhost" in entries
    assert "127.0.0.1" in entries
    assert "::1" in entries
    # 大小写两份都要写:httpx 读 no_proxy,部分工具读 NO_PROXY
    assert os.environ["no_proxy"] == os.environ["NO_PROXY"]


def test_no_cidr_entries(clean_proxy_env):
    """httpx 不做子网匹配,CIDR 条目形同虚设——不写,免得看着生效实则无效。"""
    _ensure_no_proxy_for_local()
    assert "/" not in os.environ["NO_PROXY"]


def test_system_proxy_snapshotted_before_write(clean_proxy_env):
    """🔴 回归守护:系统代理必须先快照进 env,否则写 NO_PROXY 会把它影子化。

    模拟 macOS「系统代理开着、shell 无 *_proxy 变量」——即本 PR 的事故环境。
    """
    clean_proxy_env.setattr(
        "urllib.request.getproxies",
        lambda: {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"},
    )
    _ensure_no_proxy_for_local()
    assert os.environ["http_proxy"] == "http://127.0.0.1:7897"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7897"


def test_existing_proxy_env_not_overwritten(clean_proxy_env):
    """代理本就配在 env(Linux Clash 典型):快照不该覆盖用户的显式配置。"""
    clean_proxy_env.setenv("https_proxy", "http://user-set:1080")
    clean_proxy_env.setattr(
        "urllib.request.getproxies", lambda: {"https": "http://snapshot:9999"}
    )
    _ensure_no_proxy_for_local()
    assert os.environ["https_proxy"] == "http://user-set:1080"


def test_existing_no_proxy_entries_preserved(clean_proxy_env):
    """setdefault 语义:只追加缺失项,用户已设条目原样保留。"""
    clean_proxy_env.setenv("NO_PROXY", "example.com,127.0.0.1")
    _ensure_no_proxy_for_local()
    entries = os.environ["NO_PROXY"].split(",")
    assert entries[0] == "example.com"
    assert entries.count("127.0.0.1") == 1  # 已存在的不重复追加


def test_uppercase_only_no_proxy_not_dropped(clean_proxy_env):
    """只设大写 NO_PROXY(Docker/K8s/企业代理通行写法)时,排除项不能丢。

    回归守护:CPython 折叠 *_proxy 时小写胜出,若两份各读各的现有值再各写各的,
    小写那份不含 example.com,httpx 实际采纳的就是缺了它的版本——静默丢配置。
    """
    clean_proxy_env.setenv("NO_PROXY", "api.internal.corp")
    _ensure_no_proxy_for_local()
    assert "api.internal.corp" in os.environ["no_proxy"]
    assert os.environ["no_proxy"] == os.environ["NO_PROXY"]


def test_lowercase_only_no_proxy_not_dropped(clean_proxy_env):
    """对称场景:只设小写 no_proxy 时同样不丢。"""
    clean_proxy_env.setenv("no_proxy", "api.internal.corp")
    _ensure_no_proxy_for_local()
    assert "api.internal.corp" in os.environ["NO_PROXY"]
    assert os.environ["no_proxy"] == os.environ["NO_PROXY"]


def test_getproxies_failure_does_not_break_startup(clean_proxy_env):
    """取代理抛异常时仍要写好 NO_PROXY——本函数在 import 期跑,不能拖垮启动。"""
    def boom():
        raise OSError("SystemConfiguration unavailable")

    clean_proxy_env.setattr("urllib.request.getproxies", boom)
    _ensure_no_proxy_for_local()
    assert "127.0.0.1" in os.environ["NO_PROXY"]


def test_snapshot_survives_preexisting_bare_no_proxy(clean_proxy_env):
    """env 里事先就有裸 NO_PROXY 时,快照不能被短路成空。

    回归守护:NO_PROXY 自己也是 *_proxy,折叠成 `no` 键即让
    getproxies_environment() 非空 → getproxies() 不再查系统设置。用户 .zshrc
    里 `export NO_PROXY=localhost`、或 .env 经 load_dotenv() 注入(跑在被测
    函数之前)都会命中。此时必须回退到只读系统设置的平台函数。

    刻意**不 mock getproxies**——上一版 8 条用例全把它整个 mock 掉,结构性
    抓不到这个洞;这里只 mock 系统侧函数,让真实的 env 短路逻辑参与进来。
    """
    clean_proxy_env.setenv("NO_PROXY", "preexisting.example")
    clean_proxy_env.setattr(
        "miloco.main._system_proxies",
        lambda: {"http": "http://sys:7897", "https": "http://sys:7897"},
    )
    _ensure_no_proxy_for_local()
    assert os.environ["https_proxy"] == "http://sys:7897"
    # 用户原有条目仍在
    assert "preexisting.example" in os.environ["no_proxy"]


def test_env_proxy_wins_over_system(clean_proxy_env):
    """两个协议均已显式配置时,不读取系统设置。"""
    clean_proxy_env.setenv("http_proxy", "http://env-set:1080")
    clean_proxy_env.setenv("https_proxy", "http://env-set:1080")
    clean_proxy_env.setattr(
        "miloco.main._system_proxies",
        lambda: pytest.fail("env 已有代理时不该回退问系统"),
    )
    _ensure_no_proxy_for_local()
    assert os.environ["https_proxy"] == "http://env-set:1080"


def test_all_proxy_only_user_not_overridden(clean_proxy_env):
    """只配 ALL_PROXY(纯 SOCKS 出口的通行写法)时,不能被系统代理顶掉。

    回归守护:httpx 的 get_environment_proxies 遍历 http/https/all 三个键。
    若「用户是否已配代理」的 guard 只查 http/https,系统代理会被写进
    http_proxy/https_proxy,把用户想全走 SOCKS 的意图静默改掉——与本函数
    "只追加缺失项"的原则相反。
    """
    clean_proxy_env.setenv("ALL_PROXY", "socks5://127.0.0.1:7891")
    clean_proxy_env.setattr(
        "miloco.main._system_proxies",
        lambda: {"http": "http://sys:7890", "https": "http://sys:7890"},
    )
    _ensure_no_proxy_for_local()
    assert os.environ["ALL_PROXY"] == "socks5://127.0.0.1:7891"
    assert "http_proxy" not in os.environ
    assert "https_proxy" not in os.environ


def test_module_import_applies_no_proxy_end_to_end():
    """开子进程 import miloco.main,验证注入链路真的生效。

    其余用例都直接 import 被测函数,结构上抓不到"函数写对了但没人调用"——
    backend 的注入是 main.py 里一句模块级语句,被重构掉时 11 个用例全绿,
    而失效后果比 CLI 更大(整个感知链路的本机调用重新被代理劫持)。
    与 CLI 侧 test_entrypoint_applies_no_proxy_end_to_end 对称。
    """
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    out = subprocess.run(
        [sys.executable, "-c",
         "import miloco.main, os; print(os.environ.get('NO_PROXY', ''))"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    entries = out.stdout.strip().splitlines()[-1].split(",")
    assert {"localhost", "127.0.0.1", "::1"} <= set(entries), out.stdout


def test_empty_proxy_value_treated_as_explicit_opt_out(clean_proxy_env):
    """空值 = 显式取消该 scheme 代理(curl / CPython 通行约定),不该被系统代理覆盖。

    回归守护:守门若判真值而非存在性,`export https_proxy=` 会被当成"没配",
    系统代理反手写上去——正好打掉 docstring 给出的那条退出办法。
    """
    clean_proxy_env.setenv("https_proxy", "")
    clean_proxy_env.setattr(
        "miloco.main._system_proxies", lambda: {"https": "http://sys:9999"}
    )
    _ensure_no_proxy_for_local()
    assert os.environ["https_proxy"] == ""


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

    _ensure_no_proxy_for_local()

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
    _ensure_no_proxy_for_local()
    assert get_environment_proxies() == expected


def test_httpx_client_selects_loopback_direct_and_cloud_proxy(clean_proxy_env):
    """真实 Client 的 transport 选择;不发网络请求、不依赖本机代理服务。"""
    clean_proxy_env.setenv("https_proxy", "http://proxy.invalid:7897")
    clean_proxy_env.setenv("http_proxy", "http://proxy.invalid:7897")
    _ensure_no_proxy_for_local()
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
    _ensure_no_proxy_for_local()
    assert get_environment_proxies() == {}
