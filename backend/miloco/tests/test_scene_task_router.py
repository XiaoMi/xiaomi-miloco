# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Scene task router e2e 集成测试（/api/scene-tasks）。

mock manager.scene_task_service，验证路由层：请求解析（pydantic 校验）、
响应包装（NormalResponse）、错误映射（404 → HTTPException）。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from miloco.middleware.exceptions import ResourceNotFoundException
from miloco.scene_task.schema import SceneTaskView


@pytest.fixture
def isolated_app(monkeypatch):
    """仅挂 scene_task_router 的 minimal app；manager.scene_task_service 用 mock 注入。"""
    # 测试环境可能配了 service token → 强制清空让 verify_token bypass
    monkeypatch.setenv("MILOCO_SERVER__TOKEN", "")
    from miloco.config import reset_settings

    reset_settings()
    import miloco.manager as manager_module

    manager_module.Manager._instance = None
    manager_module.manager_instance = None
    m = manager_module.get_manager()

    svc = MagicMock()
    svc.list = AsyncMock(
        return_value=[
            SceneTaskView(
                task_id='scene_task_1',
                description='床上看书自动开灯',
                status='active',
                created_at='2026-01-01T00:00:00+08:00',
                rule_id='rule-1',
                enabled=True,
                query='有人在床上看书',
                perceive_device_ids=['cam-001'],
                enter_scene_id='scene-1',
                enter_scene_name='开灯',
                exit_scene_id='scene-2',
                exit_scene_name='关灯',
                cooldown_minutes=5,
                exit_debounce_seconds=60,
                max_dwell_seconds=60,
            )
        ],
    )
    svc.create = AsyncMock(side_effect=NotImplementedError)
    svc.update = AsyncMock(side_effect=NotImplementedError)
    svc.enable = MagicMock()
    svc.disable = MagicMock()
    svc.delete = MagicMock()
    svc.trigger = AsyncMock()
    m._scene_task_service = svc

    from miloco.middleware.exception_handler import handle_exception
    from miloco.scene_task.router import router as scene_task_router

    app = FastAPI()

    @app.middleware('http')
    async def _catch_all(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001
            return handle_exception(request, exc)

    app.include_router(scene_task_router, prefix='/api')

    yield app, svc

    manager_module.Manager._instance = None
    manager_module.manager_instance = None
    reset_settings()


@pytest.fixture
def client(isolated_app):
    app, _ = isolated_app
    return TestClient(app)


@pytest.fixture
def svc(isolated_app):
    _, svc = isolated_app
    return svc


def test_list_scene_tasks(client, svc):
    r = client.get('/api/scene-tasks')
    assert r.status_code == 200
    body = r.json()
    assert body['code'] == 0
    tasks = body['data']
    assert len(tasks) == 1
    assert tasks[0]['task_id'] == 'scene_task_1'
    assert tasks[0]['enter_scene_name'] == '开灯'
    svc.list.assert_awaited_once()


def test_create_scene_task_parses_request(client, svc):
    payload = {
        'description': '沙发上有人触发观影模式',
        'perceive_device_ids': ['cam-001'],
        'query': '有人在沙发上看电视',
        'enter_scene_id': 'scene-movie',
        'exit_scene_id': None,
        'cooldown_minutes': 3,
        'enter_debounce_seconds': 15,
        'exit_debounce_seconds': 30,
        'max_dwell_seconds': 120,
    }
    svc.create = AsyncMock(
        return_value=SceneTaskView(
            task_id='scene_new',
            description='沙发上有人触发观影模式',
            status='active',
            created_at='2026-01-01T00:00:00+08:00',
            rule_id='rule-new',
            enabled=True,
            query='有人在沙发上看电视',
            perceive_device_ids=['cam-001'],
            enter_scene_id='scene-movie',
            cooldown_minutes=3,
            enter_debounce_seconds=15,
            exit_debounce_seconds=30,
            max_dwell_seconds=120,
        ),
    )
    r = client.post('/api/scene-tasks', json=payload)
    assert r.status_code == 200
    assert r.json()['data']['task_id'] == 'scene_new'
    svc.create.assert_awaited_once()
    req = svc.create.await_args.args[0]
    assert req.query == '有人在沙发上看电视'
    assert req.enter_scene_id == 'scene-movie'
    assert req.max_dwell_seconds == 120
    assert req.enter_debounce_seconds == 15


def test_create_rejects_no_scene(client, svc):
    payload = {
        'description': '缺场景',
        'perceive_device_ids': ['cam-001'],
        'query': '有人在看书',
        'enter_scene_id': None,
        'exit_scene_id': None,
    }
    r = client.post('/api/scene-tasks', json=payload)
    assert r.status_code == 422
    svc.create.assert_not_awaited()


def test_update_scene_task(client, svc):
    svc.update = AsyncMock(
        return_value=SceneTaskView(
            task_id='scene_task_1',
            description='床上看书自动开灯',
            status='active',
            created_at='2026-01-01T00:00:00+08:00',
            rule_id='rule-1',
            enabled=True,
            query='有人在床上看书',
            perceive_device_ids=['cam-001'],
            enter_scene_id='scene-A',
            exit_scene_id='scene-2',
            exit_debounce_seconds=60,
        ),
    )
    r = client.patch(
        '/api/scene-tasks/scene_task_1',
        json={'enter_scene_id': 'scene-A'},
    )
    assert r.status_code == 200
    svc.update.assert_awaited_once()
    req = svc.update.await_args.args[1]
    assert req.enter_scene_id == 'scene-A'


def test_enable_disable_trigger_delete(client, svc):
    assert client.post('/api/scene-tasks/scene_task_1/enable').status_code == 200
    svc.enable.assert_called_once_with('scene_task_1')
    assert client.post('/api/scene-tasks/scene_task_1/disable').status_code == 200
    svc.disable.assert_called_once_with('scene_task_1')
    svc.trigger = AsyncMock(
        return_value=MagicMock(model_dump=MagicMock(return_value={'event': 'ENTERED'})),
    )
    assert client.post('/api/scene-tasks/scene_task_1/trigger').status_code == 200
    svc.trigger.assert_awaited_once_with('scene_task_1')
    svc.delete = MagicMock(
        return_value=MagicMock(model_dump=MagicMock(return_value={})),
    )
    assert client.delete('/api/scene-tasks/scene_task_1').status_code == 200
    svc.delete.assert_called_once_with('scene_task_1')


def test_missing_task_returns_404(client, svc):
    svc.update = AsyncMock(
        side_effect=ResourceNotFoundException('scene_task_not_found: nope'),
    )
    r = client.patch('/api/scene-tasks/nope', json={'query': 'x'})
    # ResourceNotFoundException 走业务错误码（HTTP 200 + code=2001），与其余 router 同口径
    assert r.status_code == 200
    assert r.json()['code'] == 2001