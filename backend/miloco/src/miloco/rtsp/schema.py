"""Schemas for user-managed RTSP cameras."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class RtspCameraCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    url: str = Field(..., min_length=1, max_length=2048)

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("camera name is required")
        return cleaned

    @field_validator("url")
    @classmethod
    def _validate_rtsp_url(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned.lower().startswith(("rtsp://", "rtsps://")):
            raise ValueError("RTSP URL must start with rtsp:// or rtsps://")
        return cleaned


class RtspCameraUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    url: str | None = Field(None, min_length=1, max_length=2048)

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("camera name is required")
        return cleaned

    @field_validator("url")
    @classmethod
    def _validate_rtsp_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned.lower().startswith(("rtsp://", "rtsps://")):
            raise ValueError("RTSP URL must start with rtsp:// or rtsps://")
        return cleaned


class RtspCameraRecord(BaseModel):
    did: str
    name: str
    url: str
    room_name: str = "RTSP"
    created_at: int
    updated_at: int


class RtspCameraState(RtspCameraRecord):
    source: str = "rtsp"
    is_online: bool = False
    in_use: bool = True
    connected: bool = False
