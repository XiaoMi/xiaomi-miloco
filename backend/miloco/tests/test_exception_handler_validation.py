# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

import json
import logging
import re
from collections.abc import Callable
from typing import cast

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from miloco.middleware.exception_handler import (
    SYSTEM_ERROR_CODE,
    _create_error_response,
    _safe_traceback_locations,
    handle_exception,
    register_exception_handlers,
)
from miloco.middleware.exceptions import ResourceNotFoundException
from pydantic import BaseModel


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/api/test"})


def _payload(response) -> dict:
    return json.loads(response.body)


def test_validation_error_is_redacted_through_registered_asgi_stack() -> None:
    class Body(BaseModel):
        limit: int

    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/echo")
    async def echo(body: Body) -> dict[str, bool]:
        del body
        return {"ok": True}

    response = TestClient(app).post(
        "/echo",
        json={"limit": "sk-live-secret"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["code"] == 1002
    assert "detail" not in response.json()
    assert "sk-live-secret" not in response.text


def test_validation_response_keeps_only_public_error_fields() -> None:
    secret = "sk-private-validation-input"
    exc = RequestValidationError(
        [
            {
                "type": "value_error",
                "loc": ("body", "api_key"),
                "msg": "Value error, invalid value",
                "input": secret,
                "ctx": {"reason": secret},
                "url": "https://errors.example/private",
            }
        ]
    )

    response = handle_exception(_request(), exc)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert _payload(response) == {
        "code": 1002,
        "message": "Request parameter validation failed",
        "data": [
            {
                "type": "value_error",
                "loc": ["body", "field"],
                "msg": "Invalid value",
            }
        ],
    }
    assert secret.encode() not in response.body


def test_validation_response_replaces_unknown_type_and_location() -> None:
    secret = "private-dynamic-validation-metadata"
    exc = RequestValidationError(
        [
            {
                "type": secret,
                "loc": ("body", secret, 4),
                "msg": secret,
                "input": secret,
            }
        ]
    )

    response = handle_exception(_request(), exc)

    assert _payload(response)["data"] == [
        {
            "type": "validation_error",
            "loc": ["body", "field", "item"],
            "msg": "Invalid value",
        }
    ]
    assert secret.encode() not in response.body


def test_validation_response_bounds_error_count_and_location_depth() -> None:
    exc = RequestValidationError(
        [
            {
                "type": "missing",
                "loc": ("body", *range(12)),
                "msg": "Field required",
                "input": None,
            }
            for _ in range(25)
        ]
    )

    response = handle_exception(_request(), exc)
    errors = _payload(response)["data"]

    assert len(errors) == 20
    assert all(len(error["loc"]) == 8 for error in errors)


def test_validation_log_does_not_include_rejected_input(caplog) -> None:
    secret = "private-validation-log-value"
    exc = RequestValidationError(
        [
            {
                "type": "string_type",
                "loc": ("body", "token"),
                "msg": "Input should be a valid string",
                "input": secret,
            }
        ]
    )

    with caplog.at_level(logging.WARNING):
        handle_exception(_request(), exc)

    assert "Request validation failed" in caplog.text
    assert secret not in caplog.text


def test_system_error_response_is_fixed_and_opaque() -> None:
    secret = "database-password-private"

    response = handle_exception(_request(), RuntimeError(secret))

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    payload = _payload(response)
    assert payload["code"] == SYSTEM_ERROR_CODE
    assert payload["message"] == "Internal server error"
    assert isinstance(payload["data"], dict)
    assert re.fullmatch(r"err-[0-9a-f]{12}", payload["data"]["error_id"])
    assert secret.encode() not in response.body


def test_system_error_log_omits_exception_text(caplog) -> None:
    secret = "private-system-log-value"

    with caplog.at_level(logging.ERROR):
        response = handle_exception(_request(), RuntimeError(secret))

    response_data = _payload(response)["data"]
    assert isinstance(response_data, dict)
    error_id = response_data["error_id"]
    assert "Unhandled system error - RuntimeError" in caplog.text
    assert f"error_id={error_id}" in caplog.text
    assert secret not in caplog.text


def test_system_error_log_keeps_only_sanitized_miloco_code_location(caplog) -> None:
    try:
        _create_error_response(
            status_code=500,
            code=SYSTEM_ERROR_CODE,
            message="test",
            data={object()},
        )
    except TypeError as exc:
        with caplog.at_level(logging.ERROR):
            handle_exception(_request(), exc)

    message = caplog.messages[-1]
    assert (
        "location=miloco.middleware.exception_handler:_create_error_response:"
        in message
    )
    assert "exception_handler.py" not in message
    assert "E:\\" not in message
    assert "C:\\" not in message


def test_business_error_keeps_response_message_but_redacts_log(caplog) -> None:
    secret = "private-person-name"
    try:
        raise ResourceNotFoundException(secret)
    except ResourceNotFoundException as exc:
        with caplog.at_level(logging.ERROR):
            response = handle_exception(_request(), exc)

    assert _payload(response)["message"] == secret
    assert "code=2001" in caplog.messages[-1]
    assert secret not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_traceback_locations_skip_constant_catch_all_frame() -> None:
    namespace: dict[str, object] = {"__name__": "miloco.main"}
    exec(
        "def business_operation():\n"
        "    raise RuntimeError('private')\n"
        "def catch_all_exceptions_middleware():\n"
        "    business_operation()\n",
        namespace,
    )
    middleware = cast(Callable[[], None], namespace["catch_all_exceptions_middleware"])

    try:
        middleware()
    except RuntimeError as exc:
        locations = _safe_traceback_locations(exc)

    assert any(":business_operation:" in location for location in locations)
    assert not any(
        ":catch_all_exceptions_middleware:" in location for location in locations
    )
