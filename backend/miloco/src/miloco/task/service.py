# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""TaskService — task SSOT 业务编排层 (v2)。

职责:
- 调 TaskRepo 做 task 表 CRUD
- 联动 RuleRepo: disable/enable 改 rule.enabled; delete 走 FK CASCADE
- list / get 时实时回查 rule 表生成 rule_briefs (task 量级 < 100, N+1 接受)
- 把 cron 操作汇总为 agent_pending 返回, 让 agent 落地
"""

import logging
from typing import TYPE_CHECKING

from miloco.database.rule_repo import RuleRepo
from miloco.database.task_repo import TaskNotFound, TaskRepo
from miloco.task.schema import (
    BackendSyncResult,
    BackendSyncRuleResult,
    PendingOp,
    RuleBrief,
    TaskCreateRequest,
    TaskDeleteBackendSynced,
    TaskDeleteResult,
    TaskDisableResult,
    TaskFullView,
    TaskLinkEntry,
    TaskSummaryView,
    TaskUpdateRequest,
)

if TYPE_CHECKING:
    from miloco.rule.service import RuleService

logger = logging.getLogger(__name__)


class TaskService:
    def __init__(
        self,
        rule_repo: RuleRepo | None = None,
        rule_service: "RuleService | None" = None,
    ):
        self.repo = TaskRepo()
        self.rule_repo = rule_repo or RuleRepo()
        # rule_service 用于 delete_task 清 RuleRunner 内存态; 由 manager 注入
        # 避免循环依赖 (rule/service.py 也依赖 task_repo)。为 None 时跳过内存态清
        # 理 (老库启动路径 / 单测场景), FK CASCADE 已清 DB 侧, 内存态残留由重启
        # 时 init_rule_service 全量重建修复。
        self._rule_service = rule_service

    def create_task(self, req: TaskCreateRequest) -> None:
        """仅插 task 占位行; rule / cron 关联挂载由后续 endpoint 完成。"""
        self.repo.create_task(task_id=req.task_id, description=req.description)

    def update_description(self, task_id: str, req: TaskUpdateRequest) -> bool:
        return self.repo.update_description(task_id, req.description)

    def get_full_view(self, task_id: str) -> TaskFullView | None:
        raw = self.repo.get_full_view(task_id)
        if raw is None:
            return None
        return self._to_full_view(raw)

    def list_for_dedupe(self) -> list[TaskFullView]:
        return [self._to_full_view(raw) for raw in self.repo.list_all()]

    def list_summary(self, window: str) -> list[TaskSummaryView]:
        """一次性出所有 task 的完整状态 (基础 + rule_briefs + links + record 摘要)。

        左连接语义: 以 task 为主表, 没绑 record 的 task 也返 (record=None), 不丢行。
        TaskRecordService 是无状态轻服务, 内部实例化即可, 不进 Manager 单例。
        """
        from miloco.task_record.service import TaskRecordService

        task_views = self.list_for_dedupe()
        record_map = TaskRecordService().list_active_summaries(window)
        return [
            TaskSummaryView(
                **view.model_dump(),
                record=record_map.get(view.task_id),
            )
            for view in task_views
        ]

    def _to_full_view(self, raw: dict) -> TaskFullView:
        rule_briefs: list[RuleBrief] = []
        for rule in self.rule_repo.list_by_task(raw["task_id"]):
            rule_briefs.append(
                RuleBrief(
                    rule_id=rule.id,
                    query=rule.condition.query,
                    actions_desc=self._rule_actions_desc(rule),
                )
            )
        return TaskFullView(
            task_id=raw["task_id"],
            description=raw["description"],
            status=raw["status"],
            paused_at=raw["paused_at"],
            created_at=raw["created_at"],
            rule_briefs=rule_briefs,
            links=[TaskLinkEntry(**link) for link in raw["links"]],
        )

    @staticmethod
    def _rule_actions_desc(rule) -> list[str]:
        """rule 动作摘要 — event/state 模式下各按"动作 / 描述"路径各取一份。"""
        if rule.mode.value == "event":
            if rule.actions:
                return [
                    f"{a.iid}={a.value if a.value is not None else a.params}"
                    for a in rule.actions
                ]
            return list(rule.action_descriptions)
        out: list[str] = []
        if rule.on_enter_actions:
            out.extend(f"on_enter:{a.iid}" for a in rule.on_enter_actions)
        if rule.on_enter_desc:
            out.append(f"on_enter:{rule.on_enter_desc}")
        if rule.on_exit_actions:
            out.extend(f"on_exit:{a.iid}" for a in rule.on_exit_actions)
        if rule.on_exit_desc:
            out.append(f"on_exit:{rule.on_exit_desc}")
        return out

    def disable_task(self, task_id: str) -> TaskDisableResult:
        return self._toggle_task(task_id, target_status="paused")

    def enable_task(self, task_id: str) -> TaskDisableResult:
        return self._toggle_task(task_id, target_status="active")

    def _toggle_task(self, task_id: str, target_status: str) -> TaskDisableResult:
        meta_result = self.repo.set_status(task_id, target_status)
        if meta_result == "not_found":
            raise TaskNotFound(f"task {task_id!r} not found")

        rule_results: list[BackendSyncRuleResult] = []
        for rule in self.rule_repo.list_by_task(task_id):
            rule.enabled = target_status == "active"
            ok = self.rule_repo.update(rule)
            rule_results.append(
                BackendSyncRuleResult(
                    rule_id=rule.id, result="ok" if ok else "fail"
                )
            )

        cron_action = "disable" if target_status == "paused" else "enable"
        full = self.repo.get_full_view(task_id)
        agent_pending: list[PendingOp] = []
        for link in full["links"]:
            if link["kind"] == "cron":
                agent_pending.append(
                    PendingOp(kind="cron", ref=link["ref"], action=cron_action)
                )

        return TaskDisableResult(
            task_id=task_id,
            status=target_status,
            backend_synced=BackendSyncResult(
                meta_status=meta_result, rules=rule_results
            ),
            agent_pending=agent_pending,
        )

    def delete_task(
        self, task_id: str, reason: str = "completed"
    ) -> TaskDeleteResult | None:
        """删 task (v2 · 单事务):

        BEGIN IMMEDIATE (拿写锁避免并发 rule/cron INSERT 竞态)
        → 事务内预读 rule_ids / cron_ids (拿到的清单就是 CASCADE 会清的完整集合)
        → INSERT task_terminate_log + prune 30 天旧行 + DELETE task (FK CASCADE 清 rule/cron/task_record_*)
        → COMMIT
        → 事务外循环清 RuleRunner._rules 内存态 (FK CASCADE 不同步内存)
        → 事务外产 cron agent_pending 让 skill/agent 处理 openclaw 侧

        竞态说明: id 预读若放到事务外 (BEGIN 之前), 并发 rule/cron INSERT 到本
        task 的 DB 行会被事务内 CASCADE 清掉, 但预读清单里没有新行的 id → 内存
        态漏清永久泄露。BEGIN IMMEDIATE 拿到写锁后再 SELECT 保证清单完整。
        """
        from miloco.database.connector import get_db_connector
        from miloco.task_record.schema import TerminateReason
        from miloco.task_record.service import (
            TaskNotFoundError,
            TaskRecordService,
        )

        full = self.repo.get_full_view(task_id)
        if full is None:
            return None

        try:
            reason_enum = TerminateReason(reason)
        except ValueError:
            reason_enum = TerminateReason.COMPLETED

        record_service = TaskRecordService()
        rule_ids: list[str] = []
        cron_refs: list[dict] = []

        with get_db_connector().get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                # 事务内预读 (拿写锁后再 SELECT, 清单完整)
                rule_ids = [
                    r["id"]
                    for r in cursor.execute(
                        "SELECT id FROM rule WHERE task_id=?", (task_id,)
                    ).fetchall()
                ]
                cron_refs = [
                    {
                        "cron_id": c["cron_id"],
                        "dispatch_owner": c["dispatch_owner"],
                    }
                    for c in cursor.execute(
                        "SELECT cron_id, dispatch_owner FROM cron WHERE task_id=?",
                        (task_id,),
                    ).fetchall()
                ]

                try:
                    record_service.write_terminate_log_in_tx(
                        cursor, task_id, reason_enum
                    )
                except TaskNotFoundError:
                    pass  # task 已被外层 get_full_view 排除, 兜底保留
                record_service.prune_terminate_log_in_tx(cursor)

                # FK CASCADE 会一并清 rule / cron / task_record_*
                TaskRepo.delete_task_in_tx(cursor, task_id)

                conn.commit()
            except Exception:
                conn.rollback()
                raise

        # 事务外: RuleRunner._rules 内存 dict 清理 (FK CASCADE 不同步内存)
        if self._rule_service is not None:
            for rid in rule_ids:
                try:
                    self._rule_service.remove_rule_from_runner(rid)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "remove_rule_from_runner failed for rid=%s: %s", rid, e
                    )

        # cron agent_pending: internal 走 backend in-memory remove (阶段 3 接入),
        # external 走 skill/agent 处理 openclaw 侧。source 字段留待 schema 扩展。
        agent_pending: list[PendingOp] = [
            PendingOp(kind="cron", ref=c["cron_id"], action="remove")
            for c in cron_refs
        ]

        return TaskDeleteResult(
            task_id=task_id,
            backend_synced=TaskDeleteBackendSynced(rules_deleted=rule_ids),
            agent_pending=agent_pending,
        )
