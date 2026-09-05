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
from collections.abc import Callable
from typing import get_args, get_origin

from fastapi import FastAPI, Request, status
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from miloco.middleware.exceptions import BaseAPIException
from miloco.schema.common_schema import NormalResponse

logger = logging.getLogger(__name__)


SYSTEM_ERROR_CODE = 9000
MAX_VALIDATION_ERRORS = 20
MAX_VALIDATION_LOCATION_PARTS = 8
MAX_VALIDATION_FIELD_NAME = 64
MAX_SCHEMA_FIELD_NAMES = 256
MAX_SCHEMA_DEPTH = 8
MAX_SYSTEM_ERROR_LOCATIONS = 5
MAX_SYSTEM_ERROR_CAUSES = 5
_TRACEBACK_SKIP_LOCATIONS: set[str] = set()
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
    "bool_parsing": "Invalid boolean",
    "list_type": "Invalid list",
    "tuple_type": "Invalid tuple",
    "dict_type": "Invalid object",
    "greater_than": "Value is below the allowed range",
    "greater_than_equal": "Value is below the allowed range",
    "less_than": "Value is above the allowed range",
    "less_than_equal": "Value is above the allowed range",
    "json_invalid": "Invalid JSON body",
    "string_pattern_mismatch": "Value does not match the required format",
    "datetime_parsing": "Invalid datetime",
    "date_parsing": "Invalid date",
    "uuid_parsing": "Invalid UUID",
    "enum": "Invalid enum value",
    "model_type": "Invalid object",
    "too_short": "Too few items",
    "too_long": "Too many items",
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


def _handle_base_api_exception(
    request: Request,
    exc: BaseAPIException,
) -> JSONResponse:
    """
    Common method for handling BaseAPIException

    Args:
        exc: BaseAPIException exception object

    Returns:
        JSONResponse: Error response
    """
    locations = _safe_traceback_locations(exc)
    logger.error(
        "Request failed - %s code=%s route=%s location=%s",
        _safe_symbol(type(exc).__name__, fallback="Exception"),
        exc.code,
        _safe_route_template(request),
        ",".join(locations) if locations else "unavailable",
    )

    return _create_error_response(
        status_code=exc.http_status, code=exc.code, message=exc.message
    )


def _safe_location_part(
    value: object,
    *,
    schema_field_names: frozenset[str],
) -> str:
    if isinstance(value, str) and value in VALIDATION_LOCATION_ROOTS:
        return value
    if isinstance(value, str) and value in schema_field_names:
        return value
    return "item" if type(value) is int else "field"


def _safe_validation_errors(
    exc: RequestValidationError,
    *,
    schema_field_names: frozenset[str] = frozenset(),
) -> list[dict[str, object]]:
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
                    _safe_location_part(
                        part,
                        schema_field_names=schema_field_names,
                    )
                    for part in raw_location[:MAX_VALIDATION_LOCATION_PARTS]
                ],
                "msg": VALIDATION_TYPE_MESSAGES[validation_type],
            }
        )
    return safe_errors


def _safe_schema_field_names(request: Request) -> frozenset[str]:
    """Collect bounded declared request fields without trusting validation locations."""

    route = request.scope.get("route")
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return frozenset()

    names: set[str] = set()
    seen_models: set[type[BaseModel]] = set()
    for parameter_group in (
        "body_params",
        "query_params",
        "path_params",
        "header_params",
        "cookie_params",
    ):
        parameters = getattr(dependant, parameter_group, ())
        if not isinstance(parameters, (list, tuple)):
            continue
        for parameter in parameters:
            _add_schema_name(names, getattr(parameter, "name", None))
            _add_schema_name(names, getattr(parameter, "alias", None))
            field_info = getattr(parameter, "field_info", None)
            _collect_model_field_names(
                getattr(field_info, "annotation", None),
                names=names,
                seen_models=seen_models,
                depth=0,
            )
    return frozenset(names)


def _collect_model_field_names(
    annotation: object,
    *,
    names: set[str],
    seen_models: set[type[BaseModel]],
    depth: int,
) -> None:
    if depth >= MAX_SCHEMA_DEPTH or len(names) >= MAX_SCHEMA_FIELD_NAMES:
        return
    origin = get_origin(annotation)
    if origin is not None:
        for argument in get_args(annotation):
            _collect_model_field_names(
                argument,
                names=names,
                seen_models=seen_models,
                depth=depth + 1,
            )
        return
    if not isinstance(annotation, type) or not issubclass(annotation, BaseModel):
        return
    if annotation in seen_models:
        return
    seen_models.add(annotation)
    for field_name, field_info in annotation.model_fields.items():
        if len(names) >= MAX_SCHEMA_FIELD_NAMES:
            return
        _add_schema_name(names, field_name)
        _add_schema_name(names, field_info.alias)
        _collect_model_field_names(
            field_info.annotation,
            names=names,
            seen_models=seen_models,
            depth=depth + 1,
        )


def _add_schema_name(names: set[str], value: object) -> None:
    if (
        isinstance(value, str)
        and len(value) <= MAX_VALIDATION_FIELD_NAME
        and value.isidentifier()
        and len(names) < MAX_SCHEMA_FIELD_NAMES
    ):
        names.add(value)


def _safe_symbol(value: object, *, fallback: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 160:
        return fallback
    if not all(part.isidentifier() for part in value.split(".")):
        return fallback
    return value


def _safe_route_template(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or len(path) > 256
        or not all(character.isalnum() or character in "/{}_-.:" for character in path)
    ):
        return "unavailable"
    return path


def _safe_traceback_locations(exc: BaseException) -> tuple[str, ...]:
    """Return bounded Miloco code positions without paths, source, or local values."""

    locations: list[str] = []
    for error in _exception_chain(exc):
        traceback = error.__traceback__
        while traceback is not None:
            frame = traceback.tb_frame
            module_name = _safe_symbol(frame.f_globals.get("__name__"), fallback="")
            if module_name == "miloco" or module_name.startswith("miloco."):
                function_name = _safe_symbol(
                    frame.f_code.co_name,
                    fallback="function",
                )
                location_name = f"{module_name}:{function_name}"
                location = f"{location_name}:{traceback.tb_lineno}"
                if (
                    location_name not in _TRACEBACK_SKIP_LOCATIONS
                    and location not in locations
                ):
                    locations.append(location)
            traceback = traceback.tb_next
    return tuple(locations[-MAX_SYSTEM_ERROR_LOCATIONS:])


def _safe_exception_types(exc: BaseException) -> tuple[str, ...]:
    return tuple(
        _safe_symbol(type(error).__name__, fallback="Exception")
        for error in _exception_chain(exc)
    )


def _exception_chain(exc: BaseException) -> tuple[BaseException, ...]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while (
        current is not None
        and id(current) not in seen
        and len(chain) < MAX_SYSTEM_ERROR_CAUSES
    ):
        chain.append(current)
        seen.add(id(current))
        if current.__cause__ is not None:
            current = current.__cause__
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None
    return tuple(chain)


def skip_traceback_location(function: Callable[..., object]) -> None:
    """Exclude one registered Miloco framework frame from error locations."""

    module_name = _safe_symbol(getattr(function, "__module__", None), fallback="")
    function_name = _safe_symbol(getattr(function, "__name__", None), fallback="")
    if (
        not (module_name == "miloco" or module_name.startswith("miloco."))
        or not function_name
    ):
        raise ValueError("traceback skip location must be a Miloco function")
    _TRACEBACK_SKIP_LOCATIONS.add(f"{module_name}:{function_name}")


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
        validation_errors = _safe_validation_errors(
            exc,
            schema_field_names=_safe_schema_field_names(request),
        )
        logger.warning(
            "Request validation failed route=%s: %s",
            _safe_route_template(request),
            validation_errors,
        )
        return _create_error_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code=1002,  # Parameter validation failure error code, consistent with ValidationException
            message="Request parameter validation failed",
            data=validation_errors,
        )

    # 2. Handle other custom API exceptions
    if isinstance(exc, BaseAPIException):
        return _handle_base_api_exception(request, exc)

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
    cause_types = _safe_exception_types(exc)
    logger.error(
        "Unhandled system error - %s error_id=%s causes=%s route=%s location=%s",
        exc_type,
        error_id,
        ",".join(cause_types),
        _safe_route_template(request),
        ",".join(locations) if locations else "unavailable",
    )
    return _create_error_response(
        status_code=500,
        code=SYSTEM_ERROR_CODE,
        message="Internal server error",
        data={"error_id": error_id},
    )
