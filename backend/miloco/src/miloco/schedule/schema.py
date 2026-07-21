# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""cron 表 + REST 契约的 pydantic 模型.

schema 见 _local/schedule-migration-plan.md § 3.4:
- dispatch_owner='internal': backend 完整管理, APScheduler 触发
- dispatch_owner='external': backend 只存引用, 触发仍走 openclaw 老通路
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Cron(BaseModel):
    """cron 表行的完整模型 (repo 层内部使用)."""

    cron_id: str
    task_id: str | None = None
    dispatch_owner: Literal["internal", "external"] = "internal"

    name: str | None = None
    kind: Literal["cron", "at", "every"] | None = None
    cron_expr: str | None = None
    at_ms: int | None = None
    every_ms: int | None = None
    anchor_ms: int | None = None
    tz: str | None = None
    message: str | None = None

    light_context: bool = False
    max_delay_seconds: int | None = None

    enabled: bool = True
    fired_at: int | None = None
    retry_attempt: int = 0
    created_at: int
    updated_at: int


class CronCreateRequest(BaseModel):
    """POST /crons body. dispatch_owner 强制 internal (external 只能走迁移脚本).

    at 型时刻用 ``at_iso`` (带时区偏移的 ISO8601, e.g. ``2026-06-11T09:00:00+08:00``)
    上送, 与 SKILL.md 里 ``time-compute`` 输出、record ``expires_at`` 同格式;
    router 边界统一解析 → 内部 Cron.at_ms 存 UTC epoch 毫秒。
    """

    model_config = {"extra": "forbid"}

    name: str = Field(..., min_length=1)
    kind: Literal["cron", "at", "every"]
    task_id: str | None = None
    message: str = Field(..., min_length=1)

    cron_expr: str | None = None
    at_iso: str | None = None
    every_ms: int | None = Field(default=None, ge=60000)
    anchor_ms: int | None = None
    tz: str | None = None

    light_context: bool = False
    max_delay_seconds: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_kind_fields(self) -> "CronCreateRequest":
        """按 kind 校验字段互斥性 (schema CHECK 层也拦, 应用层给可读 400)."""
        if self.kind == "cron":
            if self.cron_expr is None:
                raise ValueError("kind='cron' requires cron_expr")
            if any(
                x is not None for x in (self.at_iso, self.every_ms, self.anchor_ms)
            ):
                raise ValueError(
                    "kind='cron' rejects at_iso / every_ms / anchor_ms"
                )
        elif self.kind == "at":
            if self.at_iso is None:
                raise ValueError("kind='at' requires at_iso")
            if any(
                x is not None
                for x in (self.cron_expr, self.every_ms, self.anchor_ms)
            ):
                raise ValueError(
                    "kind='at' rejects cron_expr / every_ms / anchor_ms"
                )
        elif self.kind == "every":
            if self.every_ms is None:
                raise ValueError("kind='every' requires every_ms")
            if self.tz is not None:
                raise ValueError("kind='every' rejects tz (interval has no tz)")
            if any(x is not None for x in (self.cron_expr, self.at_iso)):
                raise ValueError("kind='every' rejects cron_expr / at_iso")

        if (
            self.max_delay_seconds == 0
            and self.kind != "at"
        ):
            raise ValueError(
                "max_delay_seconds=0 only allowed for kind='at' (termination)"
            )
        return self


class CronView(BaseModel):
    """GET /crons 返回的单条 cron 视图 (external 行业务字段可 NULL)."""

    cron_id: str
    task_id: str | None
    dispatch_owner: Literal["internal", "external"]
    name: str | None
    kind: Literal["cron", "at", "every"] | None
    cron_expr: str | None
    at_ms: int | None
    every_ms: int | None
    anchor_ms: int | None
    tz: str | None
    message: str | None
    light_context: bool
    max_delay_seconds: int | None
    enabled: bool
    created_at: int
    updated_at: int

    @classmethod
    def from_cron(cls, cron: Cron) -> "CronView":
        return cls(**cron.model_dump(exclude={"fired_at", "retry_attempt"}))


class CronDeleteResult(BaseModel):
    """DELETE /crons/{id} 返回."""

    deleted: bool
    agent_pending: list[dict] = Field(default_factory=list)
