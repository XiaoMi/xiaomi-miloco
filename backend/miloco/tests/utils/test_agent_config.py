# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""合并写盘的「读失败拒绝覆盖」契约测试（issue #569）。

Contract under test:
  * ``update_shared_config`` 只以完整可解析的现有文件为合并基底；文件存在但
    读不懂（损坏 / 顶层不是 JSON 对象）时抛 ``ConfigFileUnreadableError``
    并且**不落盘**，用户已有段落（``server.token`` / ``model.omni`` 等）一个字节
    都不能丢。
  * 文件缺失或为空才按空配置初始化（首启语义）。
  * ``ensure_backend_token`` 遇到读不懂的 config.json 时跳过落盘（启动不能反过来
    清空用户配置），返回内存 token。
  * 正常合并仍然保留其它段落（深合并不回退成整体覆盖）。
"""

import json

import pytest
from miloco.config.settings import reset_settings
from miloco.utils.agent_config import (
    ConfigFileUnreadableError,
    ensure_backend_token,
    update_shared_config,
)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.delenv("MILOCO_SERVER__TOKEN", raising=False)
    reset_settings()
    yield tmp_path
    reset_settings()


def _config_path(tmp_path):
    return tmp_path / "config.json"


def test_update_shared_config_preserves_other_sections(tmp_path):
    path = _config_path(tmp_path)
    path.write_text(
        json.dumps(
            {
                "server": {"token": "tok-abc", "url": "http://127.0.0.1:1810"},
                "model": {"omni": {"api_key": "sk-keep"}},
            }
        ),
        encoding="utf-8",
    )
    update_shared_config(scheduler={"enabled": False})
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["server"]["token"] == "tok-abc"
    assert data["model"]["omni"]["api_key"] == "sk-keep"
    assert data["scheduler"]["enabled"] is False


def test_update_shared_config_corrupt_config_refuses_and_preserves_bytes(tmp_path):
    """损坏文件 + 保存 → 拒绝写盘，原字节原样保留（issue #569 的核心数据丢失）。"""
    path = _config_path(tmp_path)
    original = '{ "server": { "token": "tok-half-written"'
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ConfigFileUnreadableError):
        update_shared_config(scheduler={"enabled": False})

    assert path.read_text(encoding="utf-8") == original


def test_update_shared_config_non_object_config_refuses(tmp_path):
    path = _config_path(tmp_path)
    original = "[1, 2, 3]"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ConfigFileUnreadableError):
        update_shared_config(server={"token": "x"})

    assert path.read_text(encoding="utf-8") == original


def test_update_shared_config_missing_file_initializes(tmp_path):
    path = _config_path(tmp_path)
    update_shared_config(scheduler={"enabled": True})
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"scheduler": {"enabled": True}}


def test_update_shared_config_empty_file_initializes(tmp_path):
    """空文件没有可保留的内容，按首启初始化（掉电残局可自愈）。"""
    path = _config_path(tmp_path)
    path.write_text("", encoding="utf-8")
    update_shared_config(server={"token": "tok-new"})
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"server": {"token": "tok-new"}}


def test_ensure_backend_token_corrupt_config_does_not_wipe(tmp_path):
    path = _config_path(tmp_path)
    original = "NOT JSON {{{{"
    path.write_text(original, encoding="utf-8")

    token = ensure_backend_token()

    assert token
    assert path.read_text(encoding="utf-8") == original


def test_ensure_backend_token_persists_when_config_missing(tmp_path):
    path = _config_path(tmp_path)
    token = ensure_backend_token()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["server"]["token"] == token


def test_ensure_backend_token_non_object_config_does_not_crash(tmp_path):
    """顶层非对象（[1,2,3]）：启动降级分支必须真的能起服务。

    settings 读路径对非对象 warn + {} 后，ensure_backend_token 的降级分支
    两种损坏形态都成立——不会在 bootstrap 里裸崩 ValueError。
    """
    path = _config_path(tmp_path)
    original = "[1, 2, 3]"
    path.write_text(original, encoding="utf-8")

    token = ensure_backend_token()

    assert token
    assert path.read_text(encoding="utf-8") == original


def test_update_shared_config_survives_dir_fsync_failure(tmp_path, monkeypatch):
    """目录项 fsync 失败只是保险降级：写盘成功且 reset_settings 照常执行。

    模拟部分文件系统对目录 fd 返回 EINVAL：第一次 fsync 是数据页（放行），
    第二次是目录项（抛 OSError）。rename 已成功、数据已落盘，调用方不应
    看到「写失败」，进程内 settings 缓存也必须刷新。

    目录句柄用假 fd 模拟，保证 Windows（打不开目录句柄）也能走到同一分支。
    """
    import os as _os

    from miloco.utils import agent_config

    path = _config_path(tmp_path)
    path.write_text(json.dumps({"server": {"token": "tok-old"}}), encoding="utf-8")

    real_fsync = _os.fsync
    real_open = _os.open
    real_close = _os.close
    state = {"n": 0}

    def fake_fsync(fd):
        state["n"] += 1
        if state["n"] == 1:
            return real_fsync(fd)
        raise OSError("EINVAL simulated on directory fsync")

    def fake_open(p, flags, *args, **kwargs):
        if str(p) == str(tmp_path):
            return 4242  # 假目录句柄
        return real_open(p, flags, *args, **kwargs)

    def fake_close(fd):
        if fd == 4242:
            return None
        return real_close(fd)

    monkeypatch.setattr(agent_config.os, "fsync", fake_fsync)
    monkeypatch.setattr(agent_config.os, "open", fake_open)
    monkeypatch.setattr(agent_config.os, "close", fake_close)

    merged = update_shared_config(server={"token": "tok-new"})

    assert merged["server"]["token"] == "tok-new"
    assert state["n"] >= 2  # 确实走到了目录项 fsync 失败分支
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["server"]["token"] == "tok-new"
    # reset_settings() 未被跳过：缓存刷新后立刻能读到新值
    from miloco.config.settings import get_settings

    assert get_settings().server.token == "tok-new"
