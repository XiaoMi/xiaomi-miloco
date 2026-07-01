/**
 * 家庭任务面板——家庭 tab 内、家庭档案卡下方。
 *
 * 展示 miloco 为家庭创建的持续任务（GET /api/tasks/summary?window=day）：每条一行，
 * 显示描述 + record 进度摘要（progress/duration/event 三态多态文案）+ 完成标记；
 * 行尾开关快捷启停（POST enable/disable），「更多」菜单触发删除（居中弹窗二次确认，
 * 连带清理规则与记录）。任务由 Agent 创建管理，本面板只做查看与启停/删除。
 */

import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { deleteTask, setTaskEnabled } from "@/api";
import { useEscClose } from "@/hooks/useEscClose";
import { IconMore, IconTrash } from "@/lib/icons";
import type { Task, TaskRecordSummary } from "@/lib/types";
import { Switch } from "./Switch";
import { toast } from "./Toast";

interface Props {
  tasks: Task[] | undefined;
  loading: boolean;
  onChanged: () => void;
}

type TFn = ReturnType<typeof useTranslation>["t"];

// record 摘要 → 一行人话进度文案；无 record 返空串（任务未挂记录，只显描述）。
function recordText(record: TaskRecordSummary | null, t: TFn): string {
  if (!record) return "";
  const d = record.derived;
  const num = (k: string) => Number(d[k] ?? 0);
  if (record.kind === "progress") {
    return t("family.taskProgress", {
      current: num("current"),
      target: num("target"),
      unit: String(d.unit ?? ""),
    }).trim();
  }
  if (record.kind === "duration") {
    const parts = [
      t("family.taskDurationToday", { minutes: num("accumulated_minutes_today") }),
    ];
    if (num("target_minutes") > 0) {
      parts.push(t("family.taskDurationTarget", { minutes: num("target_minutes") }));
    }
    if (record.activeSession) parts.push(t("family.taskTiming"));
    return parts.join(" · ");
  }
  const parts = [t("family.taskEventTotal", { count: num("count_total") })];
  if ("count_today" in d) {
    parts.push(t("family.taskEventToday", { count: num("count_today") }));
  }
  return parts.join(" · ");
}

// 行尾「更多」菜单：kebab 图标点开下拉，当前只含删除。点外/ESC 关闭。
function MoreMenu({
  disabled,
  onDelete,
  t,
}: {
  disabled?: boolean;
  onDelete: () => void;
  t: TFn;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEscClose(open, () => setOpen(false));
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  return (
    <div ref={ref} className="relative shrink-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t("family.taskMore")}
        className="p-1.5 rounded-md text-text-tertiary hover:text-text-primary hover:bg-bg-tertiary transition-colors disabled:opacity-50"
      >
        <IconMore width={18} height={18} />
      </button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 top-full mt-1 z-20 min-w-[120px] rounded-lg bg-bg-secondary border border-border shadow-md overflow-hidden py-1"
        >
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              onDelete();
            }}
            className="w-full flex items-center gap-2 px-3 py-2 text-left text-caption text-error hover:bg-error-bg transition-colors"
          >
            <IconTrash width={15} height={15} />
            {t("family.taskDelete")}
          </button>
        </div>
      )}
    </div>
  );
}

export function TaskListPanel({ tasks, loading, onChanged }: Props) {
  const { t } = useTranslation();
  // 行内动作进行中的 taskId（禁用该行控件防重复点）；待删除确认的任务。
  const [busyId, setBusyId] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<Task | null>(null);
  const [deleting, setDeleting] = useState(false);

  useEscClose(!!pendingDelete && !deleting, () => setPendingDelete(null));

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

  const confirmDelete = async () => {
    if (!pendingDelete) return;
    const task = pendingDelete;
    setDeleting(true);
    try {
      await deleteTask(task.taskId);
      toast(t("family.taskDeleted"), "ok");
      setPendingDelete(null);
      onChanged();
    } catch (e) {
      toast(e instanceof Error ? e.message : t("family.operationFail"), "warn");
    } finally {
      setDeleting(false);
    }
  };

  const list = tasks ?? [];
  const empty = !loading && list.length === 0;

  return (
    <section
      className="rounded-xl bg-bg-secondary border border-border shadow-sm anim-in"
      aria-labelledby="task-list-title"
    >
      <div className="px-5 pt-4 pb-1">
        <h2
          id="task-list-title"
          className="text-title text-text-primary inline-flex items-baseline gap-2"
        >
          {t("family.taskTitle")}
          <span className="text-caption-mono text-text-tertiary font-normal num">
            {t("family.taskCount", { count: list.length })}
          </span>
        </h2>
      </div>
      <p className="px-5 pb-2 text-caption text-text-tertiary">
        {t("family.taskHint")}
      </p>

      {loading && !tasks ? (
        <div className="text-body text-text-secondary py-10 px-5 text-center">
          <span className="inline-flex items-center gap-2">
            <span className="inline-block w-2 h-2 rounded-full bg-text-tertiary animate-pulse" />
            {t("family.loading")}
          </span>
        </div>
      ) : empty ? (
        <div className="text-body text-text-secondary py-10 px-5 text-center">
          {t("family.taskEmpty")}
          <div className="text-caption text-text-tertiary mt-1">
            {t("family.taskEmptyHint")}
          </div>
        </div>
      ) : (
        <div className="px-5 pb-4 divide-y divide-border">
          {list.map((task) => {
            const paused = task.status === "paused";
            const summary = recordText(task.record, t);
            const busy = busyId === task.taskId;
            return (
              <div key={task.taskId} className="group flex items-center gap-3 py-3">
                <div className="min-w-0 flex-1">
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
                        {t("family.taskCompleted")}
                      </span>
                    )}
                  </div>
                  {summary && (
                    <div className="text-caption text-text-tertiary mt-0.5 num">
                      {summary}
                    </div>
                  )}
                </div>

                <Switch
                  checked={!paused}
                  disabled={busy}
                  label={paused ? t("family.taskEnable") : t("family.taskPause")}
                  onChange={() =>
                    run(
                      task.taskId,
                      () => setTaskEnabled(task.taskId, paused),
                      paused ? t("family.taskEnabled") : t("family.taskPaused"),
                    )
                  }
                />
                <MoreMenu
                  disabled={busy}
                  onDelete={() => setPendingDelete(task)}
                  t={t}
                />
              </div>
            );
          })}
        </div>
      )}

      {pendingDelete && (
        <div
          className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40"
          onClick={deleting ? undefined : () => setPendingDelete(null)}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="task-del-title"
            className="w-[90%] max-w-sm bg-bg-secondary border border-border rounded-2xl shadow-lg p-6 anim-in"
            onClick={(e) => e.stopPropagation()}
          >
            <h2
              id="task-del-title"
              className="text-title font-semibold text-text-primary mb-2"
            >
              {t("family.taskConfirmDeleteTitle")}
            </h2>
            <p className="text-body text-text-secondary mb-5">
              {t("family.taskConfirmDeleteMessage", {
                desc: pendingDelete.description,
              })}
            </p>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setPendingDelete(null)}
                disabled={deleting}
                className="text-body px-4 py-2 rounded-lg bg-bg-primary border border-border text-text-primary hover:border-border-strong disabled:opacity-60"
              >
                {t("family.cancel")}
              </button>
              <button
                type="button"
                onClick={confirmDelete}
                disabled={deleting}
                className="text-body px-4 py-2 rounded-lg font-semibold bg-error text-white hover:opacity-90 disabled:opacity-60"
              >
                {deleting ? t("family.deleting") : t("family.confirmDelete")}
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
