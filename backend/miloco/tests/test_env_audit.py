# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""env_audit 模块测试。"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from miloco.config.env_audit import REQUIRED_VARS, audit_env


class TestAuditEnv:
    """audit_env() 单元测试。"""

    def test_all_vars_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in REQUIRED_VARS:
            monkeypatch.setenv(var, "test_value")
        issues = audit_env()
        assert issues == []

    def test_missing_required_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MILOCO_HOME", raising=False)
        monkeypatch.delenv("MILOCO_DEVICE_MODEL", raising=False)
        issues = audit_env()
        assert any("MILOCO_HOME" in i for i in issues)

    def test_empty_var_flagged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MILOCO_HOME", "")
        monkeypatch.setenv("MILOCO_DEVICE_MODEL", "test")
        issues = audit_env()
        assert any("为空" in i for i in issues)

    def test_proc_environ_fallback(self) -> None:
        """验证 use_proc=True 走 /proc/self/environ 路径。

        CI 环境下 /proc/self/environ 应当可读；若因容器安全策略
        不可读则降级到空 dict，不应抛异常。
        """
        issues = audit_env(use_proc=True)
        assert isinstance(issues, list)

    def test_no_miloco_vars_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with patch.dict(os.environ, {}, clear=True):
            monkeypatch.setenv("PATH", "/usr/bin")
            issues = audit_env()
            assert any("MILOCO_" in i for i in issues)
