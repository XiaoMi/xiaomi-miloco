"""Schemas for user-managed RTSP cameras."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator


def _validate_rtsp_url(value: str) -> str:
    cleaned = value.strip()
    try:
        parsed = urlsplit(cleaned)
        _ = parsed.port
    except ValueError as e:
        raise ValueError("invalid RTSP URL") from e
    if parsed.scheme.lower() not in {"rtsp", "rtsps"} or not parsed.hostname:
        raise ValueError("RTSP URL must use rtsp:// or rtsps:// and include a host")
    return cleaned


def _clean_camera_name(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("camera name is required")
    return cleaned


class _RtspCameraFields(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    url: str = Field(..., min_length=1, max_length=2048)

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        return _clean_camera_name(value)

    @field_validator("url")
    @classmethod
    def _clean_url(cls, value: str) -> str:
        return _validate_rtsp_url(value)


class RtspCameraCreate(_RtspCameraFields):
    pass


class RtspCameraUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    url: str | None = Field(None, min_length=1, max_length=2048)

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _clean_camera_name(value)

    @field_validator("url")
    @classmethod
    def _clean_url(cls, value: str | None) -> str | None:
        return None if value is None else _validate_rtsp_url(value)

    @model_validator(mode="after")
    def _require_change(self) -> "RtspCameraUpdate":
        if self.name is None and self.url is None:
            raise ValueError("at least one of name or url is required")
        return self


class RtspCameraRecord(_RtspCameraFields):
    did: str
    room_name: str = "RTSP"
    created_at: int
    updated_at: int

    @field_validator("did")
    @classmethod
    def _validate_did(cls, value: str) -> str:
        if not value.startswith("rtsp:") or len(value) == len("rtsp:"):
            raise ValueError("RTSP camera ID must start with rtsp:")
        return value
