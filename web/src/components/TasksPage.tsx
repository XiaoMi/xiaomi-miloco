/**
 * 任务页——独立「任务」Tab 的主视图。
 *
 * 展示 miloco 为家庭创建的持续任务（GET /api/tasks/summary?window=day）：
 *  - 列表标题旁一个「?」帮助入口：Web 端不直接建任务（真正驱动任务的感知规则由装有
 *    miloco 插件的 Agent（如 OpenClaw）接线），点开弹窗引导用户与 Agent 对话创建，
 *    并给示例话术一键复制。
 *  - 任务行：描述 + 进度摘要 + 启停开关；整行点击打开详情抽屉。
 *  - 详情抽屉：复用列表已加载的 summary 数据（含驱动规则 / 关联 / 进度），无需再拉
 *    单条全量视图。驱动规则在前（触发条件 / 执行动作结构化展示），进度可视化（进度条 /
 *    计时 / 计数），创建时间作次要信息。支持就地编辑任务描述与删除；规则与推送由 Agent
 *    管理，此处只读。
 */

import type { ReactNode } from "react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { deleteTask, setTaskEnabled, updateTaskDescription } from "@/api";
import { useEscClose } from "@/hooks/useEscClose";
import { IconHelp, IconPencil, IconTrash, IconX } from "@/lib/icons";
import { relativeTime } from "@/lib/relativeTime";
import type { Task, TaskRecordSummary } from "@/lib/types";
import { AgentPromptDialog } from "./AgentPromptDialog";
import { toast } from "./Toast";

interface Props {
  tasks: Task[] | undefined;
  loading: boolean;
  onChanged: () => void;
}

type TFn = ReturnType<typeof useTranslation>["t"];

// record 摘要 → 一行人话进度文案（列表行用）；无 record 返空串。
function recordText(record: TaskRecordSummary | null, t: TFn): string {
  if (!record) return "";
  const d = record.derived;
  const num = (k: string) => Number(d[k] ?? 0);
  if (record.kind === "progress") {
    return t("tasks.progress", {
      current: num("current"),
      target: num("target"),
      unit: String(d.unit ?? ""),
    }).trim();
  }
  if (record.kind === "duration") {
    const parts = [
      t("tasks.durationToday", { minutes: num("accumulated_minutes_today") }),
    ];
    if (num("target_minutes") > 0) {
      parts.push(t("tasks.durationTarget", { minutes: num("target_minutes") }));
    }
    if (record.activeSession) parts.push(t("tasks.timing"));
    return parts.join(" · ");
  }
  const parts = [t("tasks.eventTotal", { count: num("count_total") })];
  if ("count_today" in d) {
    parts.push(t("tasks.eventToday", { count: num("count_today") }));
  }
  return parts.join(" · ");
}

// 动作文案切分：actions_desc 每条可能用「；」串了多个动作，拆成短句便于分行阅读。
function splitActions(actionsDesc: string[]): string[] {
  return actionsDesc
    .flatMap((a) => a.split(/[；;]/))
    .map((s) => s.trim())
    .filter(Boolean);
}

// 轻量开关——on=品牌色 / off=中性边框色，无障碍 role="switch"。
function Switch({
  checked,
  disabled,
  onChange,
  label,
}: {
  checked: boolean;
  disabled?: boolean;
  onChange: () => void;
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onChange}
      className={`relative shrink-0 inline-flex h-5 w-9 items-center rounded-full transition-colors disabled:opacity-50 ${
        checked ? "bg-brand-primary" : "bg-border-strong"
      }`}
    >
      <span
        className={`inline-block h-4 w-4 rounded-full bg-white shadow-sm transition-transform ${
          checked ? "translate-x-[18px]" : "translate-x-0.5"
        }`}
      />
    </button>
  );
}

// 进度条：target>0 时才有意义。label 在上、百分比在右，条在下。
function ProgressBar({ pct, label }: { pct: number; label: string }) {
  const clamped = Math.max(0, Math.min(100, pct));
  return (
    <div>
      <div className="flex items-baseline justify-between gap-2 mb-1.5">
        <span className="text-body text-text-primary num">{label}</span>
        <span className="text-caption-mono text-text-tertiary num">{clamped}%</span>
      </div>
      <div className="h-2 rounded-full bg-bg-tertiary overflow-hidden">
        <div
          className="h-full rounded-full bg-brand-primary transition-[width]"
          style={{ width: `${clamped}%` }}
        />
      </div>
    </div>
  );
}

// 两个大数字统计（event 类无目标时用）。
function StatPair({
  today,
  total,
  t,
}: {
  today: number | null;
  total: number;
  t: TFn;
}) {
  return (
    <div className="flex gap-8">
      {today !== null && (
        <div>
          <div className="text-title text-text-primary num">{today}</div>
          <div className="text-caption text-text-tertiary">
            {t("tasks.labelToday")} · {t("tasks.unitTimes")}
          </div>
        </div>
      )}
      <div>
        <div className="text-title text-text-primary num">{total}</div>
        <div className="text-caption text-text-tertiary">
          {t("tasks.labelTotal")} · {t("tasks.unitTimes")}
        </div>
      </div>
    </div>
  );
}

// 进度可视化：按 record.kind 多态。
function ProgressViz({
  record,
  t,
}: {
  record: TaskRecordSummary;
  t: TFn;
}) {
  const d = record.derived;
  const num = (k: string) => Number(d[k] ?? 0);

  let body: ReactNode;
  if (record.kind === "progress") {
    const cur = num("current");
    const tgt = num("target");
    const unit = String(d.unit ?? "");
    const pct = tgt > 0 ? Math.round((cur / tgt) * 100) : 0;
    body = <ProgressBar pct={pct} label={`${cur}/${tgt} ${unit}`.trim()} />;
  } else if (record.kind === "duration") {
    const today = num("accumulated_minutes_today");
    const tgt = num("target_minutes");
    if (tgt > 0) {
      body = (
        <ProgressBar
          pct={Math.round((today / tgt) * 100)}
          label={`${t("tasks.durationToday", { minutes: today })} · ${t(
            "tasks.durationTarget",
            { minutes: tgt },
          )}`}
        />
      );
    } else {
      body = (
        <div className="text-body text-text-primary num">
          {t("tasks.durationToday", { minutes: today })}
        </div>
      );
    }
  } else {
    const total = num("count_total");
    const today = "count_today" in d ? num("count_today") : null;
    body = <StatPair today={today} total={total} t={t} />;
  }

  return (
    <div className="space-y-2">
      {body}
      <div className="flex items-center gap-2 flex-wrap">
        {record.kind === "duration" && record.activeSession && (
          <span className="text-caption px-1.5 py-0.5 rounded bg-brand-soft text-brand-primary">
            {t("tasks.timing")}
          </span>
        )}
        {record.completed && (
          <span className="text-caption px-1.5 py-0.5 rounded bg-success-bg text-success">
            {t("tasks.completed")}
          </span>
        )}
        {record.windowRemaining && (
          <span className="text-caption text-text-tertiary num">
            {t("tasks.windowRemaining", {
              display: record.windowRemaining.display,
            })}
          </span>
        )}
      </div>
    </div>
  );
}

// 一节：小标题 + 内容。
function Section({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <section>
      <h3 className="text-caption font-semibold text-text-secondary uppercase tracking-wide mb-2">
        {title}
      </h3>
      {children}
    </section>
  );
}

// 详情抽屉：驱动规则在前，进度可视化，创建时间作次要信息；就地改描述 / 删除。
function TaskDetailSheet({
  task,
  onClose,
  onChanged,
}: {
  task: Task;
  onClose: () => void;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(task.description);
  const [confirmDel, setConfirmDel] = useState(false);
  const [busy, setBusy] = useState(false);
  // 编辑 / 删除确认态下，ESC 先退回浏览态而非关整个抽屉。
  useEscClose(true, () => {
    if (editing) setEditing(false);
    else if (confirmDel) setConfirmDel(false);
    else onClose();
  });

  const paused = task.status === "paused";

  const saveDesc = async () => {
    const next = draft.trim();
    if (!next || next === task.description) {
      setEditing(false);
      setDraft(task.description);
      return;
    }
    setBusy(true);
    try {
      await updateTaskDescription(task.taskId, next);
      toast(t("tasks.descUpdated"), "ok");
      setEditing(false);
      onChanged();
      onClose();
    } catch (e) {
      toast(e instanceof Error ? e.message : t("family.operationFail"), "warn");
    } finally {
      setBusy(false);
    }
  };

  const doDelete = async () => {
    setBusy(true);
    try {
      await deleteTask(task.taskId);
      toast(t("tasks.deleted"), "ok");
      onChanged();
      onClose();
    } catch (e) {
      toast(e instanceof Error ? e.message : t("family.operationFail"), "warn");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-[65] flex items-end md:items-center justify-center bg-black/40 backdrop-blur-sm"
      onClick={(e) => {
        e.stopPropagation();
        if (!busy) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="task-detail-title"
        className="flex w-full max-h-[85vh] flex-col bg-bg-secondary border border-border rounded-t-2xl md:max-w-md md:rounded-2xl shadow-lg anim-in"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 头部 */}
        <div className="flex items-start justify-between gap-3 px-5 pt-5 pb-4 border-b border-border shrink-0">
          <div className="min-w-0 flex-1">
            <div className="text-caption text-text-tertiary mb-1">
              {t("tasks.detailTitle")}
            </div>
            {editing ? (
              <textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                rows={2}
                maxLength={200}
                autoFocus
                placeholder={t("tasks.descPlaceholder")}
                className="w-full resize-none rounded-lg bg-bg-primary border border-border px-3 py-2 text-body text-text-primary focus:outline-none focus:border-brand-primary"
              />
            ) : (
              <>
                <h2
                  id="task-detail-title"
                  className="text-title font-semibold text-text-primary break-words"
                >
                  {task.description}
                </h2>
                <span
                  className={`inline-block mt-2 text-caption px-1.5 py-0.5 rounded ${
                    paused
                      ? "bg-bg-tertiary text-text-tertiary"
                      : "bg-success-bg text-success"
                  }`}
                >
                  {paused ? t("tasks.statusPaused") : t("tasks.statusActive")}
                </span>
              </>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={t("family.close")}
            className="shrink-0 p-1.5 -mr-1.5 rounded-md text-text-tertiary hover:text-text-primary hover:bg-bg-tertiary transition-colors"
          >
            <IconX width={18} height={18} />
          </button>
        </div>

        {/* 主体：驱动规则在前 → 进度 → 创建时间（次要） */}
        <div className="px-5 py-5 overflow-y-auto space-y-6">
          <Section title={t("tasks.rulesTitle")}>
            {task.ruleBriefs.length > 0 ? (
              <div className="space-y-3">
                {task.ruleBriefs.map((r) => {
                  const actions = splitActions(r.actionsDesc);
                  return (
                    <div
                      key={r.ruleId}
                      className="rounded-xl bg-bg-primary border border-border overflow-hidden"
                    >
                      <div className="px-3.5 py-3 border-b border-border">
                        <div className="text-caption text-text-tertiary mb-1">
                          {t("tasks.triggerCondition")}
                        </div>
                        <div className="text-body text-text-primary leading-relaxed break-words">
                          {r.query}
                        </div>
                      </div>
                      <div className="px-3.5 py-3">
                        <div className="text-caption text-text-tertiary mb-1.5">
                          {t("tasks.ruleActions")}
                        </div>
                        {actions.length > 0 ? (
                          <ul className="space-y-1.5">
                            {actions.map((a, i) => (
                              <li
                                key={i}
                                className="flex gap-2 text-body text-text-secondary leading-relaxed"
                              >
                                <span className="mt-[7px] h-1.5 w-1.5 rounded-full bg-brand-primary shrink-0" />
                                <span className="break-words">{a}</span>
                              </li>
                            ))}
                          </ul>
                        ) : (
                          <div className="text-caption text-text-tertiary">
                            {t("tasks.ruleActionsEmpty")}
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })}
                <p className="text-caption text-text-tertiary">
                  {t("tasks.rulesManagedHint")}
                </p>
              </div>
            ) : (
              <div className="text-caption text-text-tertiary">
                {t("tasks.noRules")}
              </div>
            )}
          </Section>

          {task.record && (
            <Section title={t("tasks.progressLabel")}>
              <ProgressViz record={task.record} t={t} />
            </Section>
          )}

          <div className="text-caption text-text-tertiary num pt-1">
            {t("tasks.createdLabel")} {relativeTime(task.createdAt)}
          </div>
        </div>

        {/* 底部操作 */}
        <div className="flex items-center justify-between gap-2 px-4 py-3 border-t border-border shrink-0">
          {editing ? (
            <>
              <span />
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => {
                    setEditing(false);
                    setDraft(task.description);
                  }}
                  disabled={busy}
                  className="h-9 px-4 rounded-lg text-caption text-text-secondary hover:text-text-primary hover:bg-bg-tertiary transition-colors disabled:opacity-60"
                >
                  {t("family.cancel")}
                </button>
                <button
                  type="button"
                  onClick={saveDesc}
                  disabled={busy}
                  className="h-9 px-4 rounded-lg text-caption font-semibold bg-brand-primary text-white hover:bg-brand-accent transition-colors disabled:opacity-60"
                >
                  {busy ? t("family.saving") : t("family.save")}
                </button>
              </div>
            </>
          ) : confirmDel ? (
            <>
              {/* 删除是级联且不可逆（连带清理规则 / 记录），确认态显式给出后果警示。 */}
              <span className="flex-1 min-w-0 text-caption text-error break-words line-clamp-2">
                {t("tasks.confirmDeleteMessage", { desc: task.description })}
              </span>
              <div className="flex items-center gap-2 shrink-0">
                <button
                  type="button"
                  onClick={() => setConfirmDel(false)}
                  disabled={busy}
                  className="h-9 px-4 rounded-lg text-caption text-text-secondary hover:text-text-primary hover:bg-bg-tertiary transition-colors disabled:opacity-60"
                >
                  {t("family.cancel")}
                </button>
                <button
                  type="button"
                  onClick={doDelete}
                  disabled={busy}
                  className="h-9 px-4 rounded-lg text-caption font-semibold bg-error text-white hover:opacity-90 transition-opacity disabled:opacity-60"
                >
                  {busy ? t("family.deleting") : t("family.confirmDelete")}
                </button>
              </div>
            </>
          ) : (
            <>
              <button
                type="button"
                onClick={() => setConfirmDel(true)}
                className="inline-flex items-center gap-1.5 h-9 px-4 rounded-lg text-caption text-text-tertiary hover:text-error hover:bg-error-bg transition-colors"
              >
                <IconTrash width={15} height={15} />
                {t("tasks.delete")}
              </button>
              <button
                type="button"
                onClick={() => {
                  setDraft(task.description);
                  setEditing(true);
                }}
                className="inline-flex items-center gap-1.5 h-9 px-4 rounded-lg text-caption font-semibold bg-bg-secondary border border-border text-text-secondary hover:text-text-primary hover:border-border-strong transition-colors"
              >
                <IconPencil width={15} height={15} />
                {t("tasks.editDescription")}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

export function TasksPage({ tasks, loading, onChanged }: Props) {
  const { t } = useTranslation();
  const [busyId, setBusyId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Task | null>(null);
  const [helpOpen, setHelpOpen] = useState(false);

  const run = async (taskId: string, fn: () => Promise<void>, okMsg: string) => {
    setBusyId(taskId);
    try {
      await fn();
      toast(okMsg, "ok");
      onChanged();
    } catch (e) {
      toast(e instanceof Error ? e.message : t("family.operationFail"), "warn");
    } finally {
      setBusyId(null);
    }
  };

  const list = tasks ?? [];
  const empty = !loading && list.length === 0;

  return (
    <div className="space-y-6">
      <section
        className="rounded-xl bg-bg-secondary border border-border shadow-sm anim-in"
        aria-labelledby="tasks-list-title"
      >
        <div className="flex items-start justify-between gap-2 px-5 pt-4 pb-1">
          <div className="min-w-0">
            <h2
              id="tasks-list-title"
              className="text-title text-text-primary inline-flex items-baseline gap-2"
            >
              {t("tasks.title")}
              <span className="text-caption-mono text-text-tertiary font-normal num">
                {t("tasks.count", { count: list.length })}
              </span>
            </h2>
            <p className="text-caption text-text-tertiary mt-0.5">
              {t("tasks.hint")}
            </p>
          </div>
          {/* Web 端不直接建任务（感知规则由 Agent 接线），故按钮带「?」暗示这是引导 */}
          <button
            type="button"
            onClick={() => setHelpOpen(true)}
            title={t("tasks.howToTitle")}
            className="shrink-0 inline-flex items-center gap-1.5 h-9 px-3 rounded-lg text-caption font-semibold border border-border bg-bg-primary text-text-secondary hover:text-text-primary hover:border-border-strong transition-colors"
          >
            {t("tasks.addTask")}
            <IconHelp width={14} height={14} className="text-text-tertiary" />
          </button>
        </div>

        {loading && !tasks ? (
          <div className="text-body text-text-secondary py-10 px-5 text-center">
            <span className="inline-flex items-center gap-2">
              <span className="inline-block w-2 h-2 rounded-full bg-text-tertiary animate-pulse" />
              {t("family.loading")}
            </span>
          </div>
        ) : empty ? (
          <div className="py-10 px-5 text-center">
            <div className="text-body text-text-secondary">{t("tasks.empty")}</div>
            <div className="text-caption text-text-tertiary mt-1">
              {t("tasks.emptyHint")}
            </div>
            <button
              type="button"
              onClick={() => setHelpOpen(true)}
              className="mt-4 inline-flex items-center gap-1.5 text-caption px-3 py-1.5 rounded-md border border-border bg-bg-primary text-text-secondary hover:text-text-primary hover:border-border-strong transition-colors"
            >
              <IconHelp width={15} height={15} />
              {t("tasks.viewExamples")}
            </button>
          </div>
        ) : (
          <div className="px-5 pt-2 pb-4 divide-y divide-border">
            {list.map((task) => {
              const paused = task.status === "paused";
              const summary = recordText(task.record, t);
              const busy = busyId === task.taskId;
              return (
                <div key={task.taskId} className="group flex items-center gap-3 py-3">
                  <button
                    type="button"
                    onClick={() => setDetail(task)}
                    className="min-w-0 flex-1 text-left rounded-md -mx-2 px-2 py-1 hover:bg-bg-tertiary/50 transition-colors"
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <span
                        className={`text-body truncate ${
                          paused ? "text-text-tertiary" : "text-text-primary"
                        }`}
                      >
                        {task.description}
                      </span>
                      {task.record?.completed && (
                        <span className="shrink-0 text-caption px-1.5 py-0.5 rounded bg-success-bg text-success">
                          {t("tasks.completed")}
                        </span>
                      )}
                    </div>
                    {summary && (
                      <div className="text-caption text-text-tertiary mt-0.5 num">
                        {summary}
                      </div>
                    )}
                  </button>

                  <Switch
                    checked={!paused}
                    disabled={busy}
                    label={paused ? t("tasks.enable") : t("tasks.pause")}
                    onChange={() =>
                      run(
                        task.taskId,
                        () => setTaskEnabled(task.taskId, paused),
                        paused ? t("tasks.enabled") : t("tasks.paused"),
                      )
                    }
                  />
                </div>
              );
            })}
          </div>
        )}
      </section>

      {detail && (
        <TaskDetailSheet
          task={detail}
          onClose={() => setDetail(null)}
          onChanged={onChanged}
        />
      )}

      {helpOpen && (
        <AgentPromptDialog
          title={t("tasks.howToTitle")}
          hint={t("tasks.howToBody")}
          examples={[
            t("tasks.example1"),
            t("tasks.example2"),
            t("tasks.example3"),
          ]}
          onClose={() => setHelpOpen(false)}
        />
      )}
    </div>
  );
}
