# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Unified exception handling middleware
Provides exception handling mechanisms:
1. HTTP middleware: Intercepts all HTTP request exceptions
2. WebSocket exception handling: Handles WebSocket connection exceptions
3. Global handler: Handles all types of exceptions
"""

import logging
import secrets

from fastapi import FastAPI, Request, status
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from miloco.middleware.exceptions import BaseAPIException
from miloco.schema.common_schema import NormalResponse

logger = logging.getLogger(__name__)


SYSTEM_ERROR_CODE = 9000
MAX_VALIDATION_ERRORS = 20
MAX_VALIDATION_LOCATION_PARTS = 8
MAX_SYSTEM_ERROR_LOCATIONS = 5
TRACEBACK_SKIP_LOCATIONS = frozenset({"miloco.main:catch_all_exceptions_middleware"})
VALIDATION_LOCATION_ROOTS = frozenset({"body", "query", "path", "header", "cookie"})
VALIDATION_TYPE_MESSAGES = {
    "missing": "Field required",
    "extra_forbidden": "Unexpected field",
    "literal_error": "Invalid literal value",
    "value_error": "Invalid value",
    "assertion_error": "Invalid value",
    "string_type": "Invalid string",
    "string_too_short": "String value is too short",
    "string_too_long": "String value is too long",
    "int_type": "Invalid integer",
    "int_parsing": "Invalid integer",
    "float_type": "Invalid number",
    "float_parsing": "Invalid number",
    "bool_type": "Invalid boolean",
    "list_type": "Invalid list",
    "tuple_type": "Invalid tuple",
    "dict_type": "Invalid object",
    "greater_than": "Value is below the allowed range",
    "greater_than_equal": "Value is below the allowed range",
    "less_than": "Value is above the allowed range",
    "less_than_equal": "Value is above the allowed range",
    "validation_error": "Invalid value",
}


def _create_error_response(
    status_code: int, code: int, message: str, data=None
) -> JSONResponse:
    """
    Create unified error response

    Args:
        status_code: HTTP status code
        code: Business error code
        message: Error message
        data: Optional additional data

    Returns:
        JSONResponse: Formatted error response
    """
    response_data = NormalResponse(code=code, message=message, data=data)
    return JSONResponse(status_code=status_code, content=response_data.model_dump())


def _handle_base_api_exception(exc: BaseAPIException) -> JSONResponse:
    """
    Common method for handling BaseAPIException

    Args:
        exc: BaseAPIException exception object

    Returns:
        JSONResponse: Error response
    """
    locations = _safe_traceback_locations(exc)
    logger.error(
        "Request failed - %s code=%s location=%s",
        _safe_symbol(type(exc).__name__, fallback="Exception"),
        exc.code,
        ",".join(locations) if locations else "unavailable",
    )

    return _create_error_response(
        status_code=exc.http_status, code=exc.code, message=exc.message
    )


def _safe_location_part(value: object) -> str:
    if isinstance(value, str) and value in VALIDATION_LOCATION_ROOTS:
        return value
    return "item" if type(value) is int else "field"


def _safe_validation_errors(exc: RequestValidationError) -> list[dict[str, object]]:
    """Return bounded validation metadata without request values or context."""

    safe_errors: list[dict[str, object]] = []
    for error in exc.errors()[:MAX_VALIDATION_ERRORS]:
        raw_type = error.get("type")
        validation_type = (
            raw_type
            if isinstance(raw_type, str) and raw_type in VALIDATION_TYPE_MESSAGES
            else "validation_error"
        )
        raw_location = error.get("loc", ())
        if not isinstance(raw_location, (list, tuple)):
            raw_location = ()
        safe_errors.append(
            {
                "type": validation_type,
                "loc": [
                    _safe_location_part(part)
                    for part in raw_location[:MAX_VALIDATION_LOCATION_PARTS]
                ],
                "msg": VALIDATION_TYPE_MESSAGES[validation_type],
            }
        )
    return safe_errors


def _safe_symbol(value: object, *, fallback: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 160:
        return fallback
    if not all(part.isidentifier() for part in value.split(".")):
        return fallback
    return value


def _safe_traceback_locations(exc: Exception) -> tuple[str, ...]:
    """Return bounded Miloco code positions without paths, source, or local values."""

    locations: list[str] = []
    traceback = exc.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        module_name = _safe_symbol(frame.f_globals.get("__name__"), fallback="")
        if module_name == "miloco" or module_name.startswith("miloco."):
            function_name = _safe_symbol(frame.f_code.co_name, fallback="function")
            location_name = f"{module_name}:{function_name}"
            if location_name not in TRACEBACK_SKIP_LOCATIONS:
                locations.append(f"{location_name}:{traceback.tb_lineno}")
        traceback = traceback.tb_next
    return tuple(locations[-MAX_SYSTEM_ERROR_LOCATIONS:])


def register_exception_handlers(app: FastAPI) -> None:
    """Install handlers that FastAPI would otherwise consume before middleware."""

    app.add_exception_handler(RequestValidationError, handle_exception)


def handle_exception(request: Request, exc: Exception) -> JSONResponse:
    """
    Unified exception handling function - handles all exceptions

    This function handles:
    - RequestValidationError (Pydantic validation errors)
    - Custom API exceptions (authentication, authorization, business exceptions, etc.)
    - FastAPI HTTPException
    - Other system-level exceptions

    Args:
        exc: Exception object
        request: FastAPI request object

    Returns:
        JSONResponse: Unified error response
    """
    # 1. Special handling for RequestValidationError (Pydantic validation errors)
    if isinstance(exc, RequestValidationError):
        validation_errors = _safe_validation_errors(exc)
        logger.warning("Request validation failed: %s", validation_errors)
        return _create_error_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code=1002,  # Parameter validation failure error code, consistent with ValidationException
            message="Request parameter validation failed",
            data=validation_errors,
        )

    # 2. Handle other custom API exceptions
    if isinstance(exc, BaseAPIException):
        return _handle_base_api_exception(exc)

    # 3. Handle FastAPI HTTPException (fallback handling)
    if isinstance(exc, FastAPIHTTPException):
        logger.warning("FastAPI HTTP error - %s: %s", exc.status_code, exc.detail)
        return _create_error_response(
            status_code=exc.status_code,
            code=1000,  # General HTTP error code, consistent with HTTPException base class
            message=str(exc.detail),
        )

    # 4. Handle other exceptions (system exceptions) - final fallback
    exc_type = _safe_symbol(type(exc).__name__, fallback="Exception")
    error_id = f"err-{secrets.token_hex(6)}"
    locations = _safe_traceback_locations(exc)
    logger.error(
        "Unhandled system error - %s error_id=%s location=%s",
        exc_type,
        error_id,
        ",".join(locations) if locations else "unavailable",
    )
    return _create_error_response(
        status_code=500,
        code=SYSTEM_ERROR_CODE,
        message="Internal server error",
        data={"error_id": error_id},
    )
