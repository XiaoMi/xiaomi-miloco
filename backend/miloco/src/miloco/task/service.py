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
from miloco.middleware.exceptions import (
    BusinessException,
    ValidationException,
)
from miloco.rule.schema import SCENE_IID, RuleDirection
from miloco.task.schema import (
    BackendSyncResult,
    BackendSyncRuleResult,
    CronRef,
    PendingOp,
    RuleBrief,
    TaskActionsUpdateRequest,
    TaskBoundaryActions,
    TaskCreateRequest,
    TaskDeleteBackendSynced,
    TaskDeleteResult,
    TaskDisableResult,
    TaskFullView,
    TaskSummaryView,
    TaskUpdateRequest,
)

if TYPE_CHECKING:
    from miloco.rule.service import RuleService

logger = logging.getLogger(__name__)

_META_COLUMN_NAMES = frozenset({"description", "lifecycle", "expires_at"})

# 这些列传 null 是"清空"; 其余都是 NOT NULL, 传 null 会在 UPDATE 时撞约束。
_NULLABLE_META = frozenset({"expires_at"})

_ACTION_SLOT_NAMES = frozenset(
    {
        "on_enter_actions",
        "on_enter_desc",
        "on_exit_actions",
        "on_exit_desc",
        "on_target_actions",
        "on_target_desc",
    }
)


def _action_desc(a) -> str:
    """event 模式的动作展示串。场景没有 value/params,只按 iid 渲染会变成
    ``scene=None``,且同规则装两个场景时两行完全一样。"""
    if a.iid == SCENE_IID:
        return f"{a.iid}:{a.did}"
    return f"{a.iid}={a.value if a.value is not None else a.params}"


def _action_desc_short(a) -> str:
    """state 模式的动作展示串:只要形态,不带 payload。

    值可能是整段 TTS 文案,而前端按「；」把摘要串切成短句显示
    (TasksPage.splitActions),文案里的分号会把一条动作切成几行残句。
    """
    return f"{a.iid}:{a.did}" if a.iid == SCENE_IID else a.iid


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
        self.repo.create_task(
            task_id=req.task_id,
            description=req.description,
            lifecycle=req.lifecycle,
            expires_at=req.expires_at,
        )

    def update_meta(self, task_id: str, req: TaskUpdateRequest) -> bool:
        """partial 改 description / lifecycle / 到期时刻。

        约束判的是**这次改动落地之后**的组合: permanent 带到期时刻是自相矛盾的
        配置, 而两个字段可以分两次请求改 —— 只看本次传进来的那个, 「先把 lifecycle
        改成 permanent」和「先把到期时刻塞给一个 permanent 的 task」都能穿过去。
        """
        updates = {
            name: getattr(req, name)
            for name in req.model_fields_set
            if name in _META_COLUMN_NAMES
        }
        if not updates:
            raise ValidationException("至少要传一个可改字段")
        # 点名 NOT NULL 的列会漏掉下一列 —— 反过来白名单可清空的那些。
        nulled = sorted(
            k for k, v in updates.items() if v is None and k not in _NULLABLE_META
        )
        if nulled:
            raise ValidationException(f"{', '.join(nulled)} 不能清空")

        current = self.repo.get_full_view(task_id)
        if current is None:
            return False
        after_lifecycle = updates.get("lifecycle", current.get("lifecycle"))
        after_expiry = (
            updates["expires_at"] if "expires_at" in updates else current.get("expires_at")
        )
        if after_expiry and after_lifecycle != "temporary":
            raise ValidationException(
                "expires_at 只能配 lifecycle=temporary: 改成 permanent 要同时清掉"
                "到期时刻 (--clear-expires-at)"
            )
        return self.repo.update_meta(task_id, updates)

    def get_description(self, task_id: str) -> str | None:
        """按 task_id 取任务描述（住户日志「所属任务」用）。"""
        return self.repo.get_description(task_id)

    def set_boundary_actions(self, task_id: str, req: TaskActionsUpdateRequest) -> bool:
        """写 task 的边界动作槽 —— 多 rule 的 task 唯一能改动作的路径。

        rule 侧的动作 flag 在多条 rule 争同一个槽时不透传 (从一条 rule 单向覆盖会
        冲掉另一条的动作), 那种 task 只能从这里改。

        写完必须重新配置: runner 手里的动作快照是内存副本, 不刷新的话改了不生效,
        而 CLI 已经返回成功 —— 正是"静默不生效"那种最难查的形态。

        配达标动作要先有 duration record + 阈值: 达标规则是这三样齐备的派生物, 缺
        任何一样都只是配了个永远不响的通知。
        """
        slots = {
            name: getattr(req, name)
            for name in req.model_fields_set
            if name in _ACTION_SLOT_NAMES
        }
        if not slots:
            return self.repo.get_description(task_id) is not None
        # 槽是最小写入单位: 同槽两列互斥, 而选槽时静态优先
        # (runner._select_task_slot)。只写传进来的那一列, 用户把动作从设备直控改成
        # Agent 文案时残留的静态列会继续赢 —— 请求返回成功、task get 显示新文案、
        # 实际下发的还是旧的设备动作。补全放在服务层, CLI 与 HTTP 直连同受约束。
        for prefix in ("on_enter", "on_exit", "on_target"):
            actions_key, desc_key = f"{prefix}_actions", f"{prefix}_desc"
            if actions_key in slots and desc_key not in slots:
                slots[desc_key] = None
            elif desc_key in slots and actions_key not in slots:
                slots[actions_key] = None
        if slots.get("on_target_actions") or slots.get("on_target_desc"):
            if self._rule_service is None:
                raise BusinessException("rule service 未就绪，无法校验达标配置")
            self._rule_service.require_duration_target(task_id)
            self._rule_service.require_exit_path_for_target(task_id)
        if not self.repo.set_boundary_actions(task_id, **slots):
            return False
        if self._rule_service is not None:
            self._rule_service.reconfigure_task(task_id)
        return True

    def get_full_view(self, task_id: str) -> TaskFullView | None:
        raw = self.repo.get_full_view(task_id)
        if raw is None:
            return None
        return self._to_full_view(raw)

    def list_for_dedupe(self) -> list[TaskFullView]:
        return [self._to_full_view(raw) for raw in self.repo.list_all()]

    def list_summary(self, window: str) -> list[TaskSummaryView]:
        """一次性出所有 task 的完整状态 (基础 + rule_briefs + cron_refs + record 摘要)。

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
            # 达标规则由服务端维护, 不给用户看 —— 与 GET /rules 同口径。前端把每条
            # rule_brief 渲染成可编辑卡片、保存走 PATCH, 而 PATCH 达标规则会被拒,
            # 露出去等于在住户界面上摆一张存不下去的卡片。
            if rule.resolved_direction is RuleDirection.MILESTONE:
                continue
            rule_briefs.append(
                RuleBrief(
                    rule_id=rule.id,
                    query=rule.condition.query,
                    direction=rule.resolved_direction.value,
                    actions_desc=self._rule_actions_desc(rule),
                )
            )
        return TaskFullView(
            task_id=raw["task_id"],
            description=raw["description"],
            status=raw["status"],
            paused_at=raw["paused_at"],
            created_at=raw["created_at"],
            lifecycle=raw.get("lifecycle") or "permanent",
            # 用 [] 不用 .get(): 少 SELECT 一列时立刻 KeyError, 而 .get() 会把
            # "这一列没查"静默变成"这个 task 没到期时刻", 列表接口整片回 null。
            expires_at=raw["expires_at"],
            runtime_state=self._runtime_state(raw["task_id"]),
            last_decision=self._last_decision(raw["task_id"]),
            rule_briefs=rule_briefs,
            cron_refs=[CronRef(**c) for c in raw["cron_refs"]],
            # 六个槽跟基础字段同一行取出来, 恒有 —— 之前写成可选的, 前端那句
            # if (task.actions) 就成了永真判断, 拿不到"这个 task 没配动作"这件事。
            actions=TaskBoundaryActions(**raw["actions"]),
        )

    def _runtime_state(self, task_id: str) -> str:
        """模式此刻开着还是关着。内存派生, 取不到就按 off (§4.2 重启也从 off 起)。

        rule_service 未注入 (老库启动路径 / 单测) 或该 task 未被状态机接管时都是
        off —— 未接管的 task 本来就没有运行态这个概念。
        """
        if self._rule_service is None:
            return "off"
        try:
            sm = self._rule_service.runner_state_machine
        except Exception:  # noqa: BLE001
            return "off"
        if sm is None:
            return "off"
        return sm.runtime_state(task_id).value

    def _last_decision(self, task_id: str) -> dict | None:
        if self._rule_service is None:
            return None
        try:
            tracker = self._rule_service.decision_tracker
        except Exception:  # noqa: BLE001
            return None
        return tracker.summary(task_id) if tracker is not None else None

    @staticmethod
    def _rule_actions_desc(rule) -> list[str]:
        """rule 动作摘要 — 单方向的 rule 取"动作 / 描述", session 取两个槽。

        按 direction 而不是 mode 分: exit 型的 mode 也是 event, 它的动作同样填在
        ``actions`` / ``action_descriptions`` 上。
        """
        if rule.resolved_direction is not RuleDirection.SESSION:
            if rule.actions:
                return [_action_desc(a) for a in rule.actions]
            return list(rule.action_descriptions)
        out: list[str] = []
        if rule.on_enter_actions:
            out.extend(
                f"on_enter:{_action_desc_short(a)}" for a in rule.on_enter_actions
            )
        if rule.on_enter_desc:
            out.append(f"on_enter:{rule.on_enter_desc}")
        if rule.on_exit_actions:
            out.extend(
                f"on_exit:{_action_desc_short(a)}" for a in rule.on_exit_actions
            )
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

        # **不写 rule.enabled** —— 它是用户意图, 不是 task.status 的镜像 (§19.9)。
        # 覆写它会让「用户手动关掉的那一条」在 task 重新 enable 时被错误打开。
        # 生效与否由派生量「有效启用」= rule.enabled AND task 未停用 决定, 刷新
        # 它是 rule_service 的事 —— runner 内存里那份不刷, 停用就只写了 DB、规则
        # 会继续触发到进程重启为止 (get_enabled_rule_ids 明确不走 DB)。
        active = target_status == "active"
        rule_results: list[BackendSyncRuleResult] = []
        refreshed = self._rule_service is not None
        if refreshed:
            try:
                self._rule_service.apply_task_status(task_id, active)
            except Exception as e:  # noqa: BLE001
                logger.warning("apply_task_status failed for task=%s: %s", task_id, e)
                refreshed = False
        for rule in self.rule_repo.list_by_task(task_id):
            rule_results.append(
                BackendSyncRuleResult(
                    rule_id=rule.id, result="ok" if refreshed else "fail"
                )
            )

        # cron 联动: internal 改 cron.enabled + apply_enabled_state (函数内部
        # 已双向, disabled 会 _remove_job); external 产 agent_pending 让 skill
        # 处理 openclaw 侧。跟 router._toggle_enabled 对称。
        from miloco.config import get_settings
        from miloco.schedule.repo import CronRepo
        from miloco.schedule.runner import get_runner

        cron_repo = CronRepo()
        cron_enabled = target_status == "active"
        cron_action = "disable" if target_status == "paused" else "enable"
        schedule_enabled = get_settings().schedule.enabled

        agent_pending: list[PendingOp] = []
        for cron in cron_repo.list_by_task(task_id):
            if cron.dispatch_owner == "internal":
                cron_repo.set_enabled(cron.cron_id, cron_enabled)
                if schedule_enabled:
                    updated = cron_repo.get(cron.cron_id)
                    if updated is not None:
                        try:
                            get_runner().apply_enabled_state(updated)
                        except Exception as e:  # noqa: BLE001
                            logger.warning(
                                "apply_enabled_state failed for %s: %s",
                                cron.cron_id, e,
                            )
            else:
                agent_pending.append(
                    PendingOp(kind="cron", ref=cron.cron_id, action=cron_action)
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
            # task 维度的内存态与 rule 维度是两份, 清 rule 不连带清 task
            try:
                self._rule_service.forget_task(task_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("forget_task failed for task=%s: %s", task_id, e)

        # cron 联动: internal 调 runner.remove_job 清 in-memory job; external
        # 产 agent_pending 让 skill 处理 openclaw 侧。跟 router.delete_cron 对称。
        # kill switch off 时跳过 in-memory 操作 (DB 行已 CASCADE 删, 下次启动
        # rebuild 时 dispatch_owner='internal' 过滤已看不到孤儿行)。
        from miloco.config import get_settings
        from miloco.schedule.runner import get_runner

        schedule_enabled = get_settings().schedule.enabled
        agent_pending: list[PendingOp] = []
        for c in cron_refs:
            if c["dispatch_owner"] == "internal":
                if schedule_enabled:
                    try:
                        get_runner().remove_job(c["cron_id"])
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "remove_job failed for %s: %s", c["cron_id"], e
                        )
            else:
                agent_pending.append(
                    PendingOp(kind="cron", ref=c["cron_id"], action="remove")
                )

        return TaskDeleteResult(
            task_id=task_id,
            backend_synced=TaskDeleteBackendSynced(rules_deleted=rule_ids),
            agent_pending=agent_pending,
        )
