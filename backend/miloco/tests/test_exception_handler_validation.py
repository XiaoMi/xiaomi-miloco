# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

import json
import logging

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from miloco.middleware.exception_handler import SYSTEM_ERROR_CODE, handle_exception


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/api/test"})


def _payload(response) -> dict:
    return json.loads(response.body)


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
    assert _payload(response) == {
        "code": SYSTEM_ERROR_CODE,
        "message": "Internal server error",
        "data": None,
    }
    assert secret.encode() not in response.body


def test_system_error_log_omits_exception_text(caplog) -> None:
    secret = "private-system-log-value"

    with caplog.at_level(logging.ERROR):
        handle_exception(_request(), RuntimeError(secret))

    assert "Unhandled system error - RuntimeError" in caplog.text
    assert secret not in caplog.text
