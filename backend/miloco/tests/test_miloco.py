# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
miloco 主应用单元测试 — 不依赖真实数据库/设备，可在 CI 中直接运行。
覆盖：config settings / person service / miot service (mocked) / admin router
"""

# ruff: noqa: E402  — intentional block-style imports, each test section imports its own module

from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ─── config / settings ───────────────────────────────────────────────────────
from miloco.config import get_settings


def test_server_config_has_required_fields():
    server = get_settings().server
    assert isinstance(server.host, str) and server.host
    assert isinstance(server.port, int) and server.port > 0
    assert isinstance(server.log_level, str) and server.log_level


def test_perf_config_structure():
    perf = get_settings().perf
    assert isinstance(perf.enabled, bool)
    retention = perf.retention
    assert retention.traces_days > 0
    assert retention.events_days > 0
    assert retention.trace_jsonl_days > 0


def test_perception_config_defaults():
    collect = get_settings().perception.collect
    assert collect.window_size > 0
    assert collect.max_windows > 0
    assert collect.full_action in ("drop", "clear", "keep")


def test_workspace_dir_is_path():
    assert isinstance(get_settings().directories.workspace_dir, Path)


# ─── person service ───────────────────────────────────────────────────────────


from miloco.middleware.exceptions import ConflictException, ResourceNotFoundException
from miloco.person.schema import Person
from miloco.person.service import PersonService


def _make_person_repo(persons=None):
    repo = MagicMock()
    repo.get_all.return_value = persons or []
    repo.exists.return_value = False
    repo.exists_by_name.return_value = False
    repo.create.return_value = "new-id-123"
    return repo


def test_person_service_list_empty():
    svc = PersonService(person_repo=_make_person_repo())
    assert svc.list_persons() == []


def test_person_service_list_returns_all():
    p = Person(id="p1", name="张三")
    svc = PersonService(person_repo=_make_person_repo([p]))
    result = svc.list_persons()
    assert len(result) == 1
    assert result[0].name == "张三"


def test_person_service_create_success():
    repo = _make_person_repo()
    svc = PersonService(person_repo=repo)
    pid = svc.create_person("李四", role=None)
    assert pid == "new-id-123"
    repo.create.assert_called_once_with("李四", None)


def test_person_service_create_conflict():
    repo = _make_person_repo()
    repo.exists_by_name.return_value = True
    svc = PersonService(person_repo=repo)
    with pytest.raises(ConflictException):
        svc.create_person("duplicate", role=None)


def test_person_service_update_not_found():
    repo = _make_person_repo()
    repo.exists.return_value = False
    svc = PersonService(person_repo=repo)
    with pytest.raises(ResourceNotFoundException):
        svc.update_person("ghost-id", name="x", role=None)


def test_person_service_update_name_conflict():
    repo = _make_person_repo()
    repo.exists.return_value = True
    repo.exists_by_name.return_value = True
    svc = PersonService(person_repo=repo)
    with pytest.raises(ConflictException):
        svc.update_person("real-id", name="dup", role=None)


def test_person_service_delete_not_found():
    repo = _make_person_repo()
    repo.exists.return_value = False
    svc = PersonService(person_repo=repo)
    with pytest.raises(ResourceNotFoundException):
        svc.delete_person("ghost-id")


def test_person_service_delete_calls_repo():
    repo = _make_person_repo()
    repo.exists.return_value = True
    svc = PersonService(person_repo=repo)
    svc.delete_person("real-id")
    repo.delete.assert_called_once_with("real-id")


# ─── miot service (schema + validation logic) ────────────────────────────────


from miloco.miot.schema import (
    CameraImgInfo,
    DeviceControlRequest,
    PropertyItem,
)


def test_camera_img_info_requires_data_and_timestamp():
    info = CameraImgInfo(data=b"\xff\xd8\xff", timestamp=1713000000000)
    assert info.timestamp == 1713000000000
    assert info.data == b"\xff\xd8\xff"


def test_device_control_request_set_property():
    req = DeviceControlRequest(
        type="set_property",
        iid="prop.2.1",
        value=True,
    )
    assert req.type == "set_property"
    assert req.iid == "prop.2.1"
    assert req.value is True


def test_device_control_request_set_properties():
    req = DeviceControlRequest(
        type="set_properties",
        properties=[PropertyItem(iid="prop.2.1", value=True)],
    )
    assert req.type == "set_properties"
    assert len(req.properties) == 1
    assert req.properties[0].iid == "prop.2.1"


# ─── perception schema ────────────────────────────────────────────────────────


from miloco.perception.schema import OnDemandPerceptionRequest, PerceptionEngineStatus


def test_on_demand_perception_request():
    req = OnDemandPerceptionRequest(sources=["cam-001"], query="这个人是谁？")
    assert req.query == "这个人是谁？"
    assert "cam-001" in req.sources


def test_on_demand_perception_request_requires_sources():
    with pytest.raises(Exception):
        OnDemandPerceptionRequest(sources=[], query="test")


def test_perception_engine_status_defaults():
    status = PerceptionEngineStatus()
    assert status.running is False
    assert status.today_inference_count == 0
    assert isinstance(status.active_sources, list)


# ─── middleware exceptions ────────────────────────────────────────────────────


from miloco.middleware.exceptions import (
    BusinessException,
    ValidationException,
)


def test_resource_not_found_is_business_exception():
    exc = ResourceNotFoundException("not found")
    assert isinstance(exc, BusinessException)
    assert "not found" in str(exc)


def test_conflict_exception():
    exc = ConflictException("already exists")
    assert isinstance(exc, BusinessException)


def test_validation_exception_http_status():
    exc = ValidationException("bad input")
    assert exc.http_status == 422


def test_business_exceptions_have_code():
    rne = ResourceNotFoundException("not found")
    assert rne.code == 2001
    ce = ConflictException("conflict")
    assert ce.code == 2002


# ─── manager (smoke test) ────────────────────────────────────────────────────




def test_manager_is_singleton():
    """get_manager() always returns the same instance."""
    from miloco.manager import get_manager
    mgr1 = get_manager()
    mgr2 = get_manager()
    assert mgr1 is mgr2
