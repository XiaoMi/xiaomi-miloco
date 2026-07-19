# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""schedule 模块 — cron 表 + APScheduler 运行时。

模块边界:
- ``schema``  : Cron 模型 + REST 请求/响应 pydantic
- ``repo``    : CronRepo — cron 表 CRUD (含 mark_fired 单事务)
- ``runner``  : APScheduler + _fire + rebuild_scheduler_from_db + listeners
- ``router``  : /api/crons REST 端点
"""
