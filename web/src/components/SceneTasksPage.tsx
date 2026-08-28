/**
 * 场景联动页——独立「场景联动」Tab 的主视图。
 *
 * 场景联动任务 = 一条 state 规则把「感知命中」直接接到「米家自动化场景」：
 *   - 进入：条件 query 变为满足 → 触发进入场景（如识别到床上有人看书 → 开灯）
 *   - 退出：条件不再满足并持续 exitDebounceSeconds → 触发退出场景（如关灯）；
 *     或配置 maxDwellSeconds 后「到期自动退出」（如开灯 1 分钟后自动关灯）
 * 命中 / 抗抖 / 冷却完全复用规则引擎，**不经过 agent**（后端 /api/scene-tasks）。
 * 本页负责创建 / 编辑 / 启停 / 删除，并提供「手动触发」调试入口。
 */

import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  createSceneTask,
  deleteSceneTask,
  setSceneTaskEnabled,
  triggerSceneTask,
  updateSceneTask,
} from "@/api";
import { useEscClose } from "@/hooks/useEscClose";
import {
  IconCheck,
  IconHelp,
  IconPencil,
  IconPlus,
  IconTrash,
  IconX,
} from "@/lib/icons";
import type { Scene, SceneTask, SceneTaskInput, ScopeCamera } from "@/lib/types";
import { toast } from "./Toast";

interface Props {
  tasks: SceneTask[] | undefined;
  scenes: Scene[];
  cameras: ScopeCamera[];
  loading: boolean;
  onChanged: () => void | Promise<void>;
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

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="text-caption font-semibold text-text-secondary uppercase tracking-wide mb-2">
        {title}
      </h3>
      {children}
    </section>
  );
}

// 表单输入框统一样式。
const inputCls =
  "w-full rounded-lg bg-bg-primary border border-border px-3 py-2 text-body text-text-primary focus:outline-none focus:border-brand-primary";

// 场景下拉：value "" = 该方向不联动。
function SceneSelect({
  value,
  scenes,
  noneLabel,
  onChange,
}: {
  value: string;
  scenes: Scene[];
  noneLabel: string;
  onChange: (v: string) => void;
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className={`${inputCls} ${value ? "" : "text-text-tertiary"}`}
    >
      <option value="">{noneLabel}</option>
      {scenes.map((s) => (
        <option key={s.id} value={s.id}>
          {s.name}
        </option>
      ))}
    </select>
  );
}

// 表单草稿（数字字段以字符串承载，保存时统一解析）。
interface Draft {
  description: string;
  query: string;
  cameraDids: string[];
  enterSceneId: string;
  exitSceneId: string;
  cooldownMinutes: string;
  exitDebounceSeconds: string;
  maxDwellSeconds: string;
  enabled: boolean;
}

function draftFromTask(task: SceneTask): Draft {
  return {
    description: task.description,
    query: task.query,
    cameraDids: [...task.perceiveDeviceIds],
    enterSceneId: task.enterSceneId ?? "",
    exitSceneId: task.exitSceneId ?? "",
    cooldownMinutes: String(task.cooldownMinutes ?? 5),
    exitDebounceSeconds: String(task.exitDebounceSeconds),
    maxDwellSeconds: task.maxDwellSeconds ? String(task.maxDwellSeconds) : "",
    enabled: task.enabled,
  };
}

function emptyDraft(): Draft {
  return {
    description: "",
    query: "",
    cameraDids: [],
    enterSceneId: "",
    exitSceneId: "",
    cooldownMinutes: "5",
    exitDebounceSeconds: "60",
    maxDwellSeconds: "",
    enabled: true,
  };
}

const num = (s: string): number | null => {
  const n = parseInt(s, 10);
  return Number.isFinite(n) ? n : null;
};

const sameIds = (a: string[], b: string[]): boolean =>
  a.length === b.length && a.every((x, i) => x === b[i]);

// 编辑模式只提交真正改过的字段：PATCH 语义下显式 null = 清空该方向场景，
// 未动过的字段绝不能随请求发出去（否则会把用户没碰的配置清掉）。
function buildPatch(draft: Draft, task: SceneTask): SceneTaskInput {
  const patch: SceneTaskInput = {};
  const enterId = draft.enterSceneId || null;
  const exitId = draft.exitSceneId || null;
  const maxDwell = num(draft.maxDwellSeconds);
  const cooldown = num(draft.cooldownMinutes);
  const debounce = num(draft.exitDebounceSeconds);
  if (draft.description !== task.description) patch.description = draft.description;
  if (draft.query !== task.query) patch.query = draft.query;
  if (!sameIds(draft.cameraDids, task.perceiveDeviceIds)) {
    patch.perceiveDeviceIds = [...draft.cameraDids];
  }
  if ((enterId ?? null) !== (task.enterSceneId ?? null)) patch.enterSceneId = enterId;
  if ((exitId ?? null) !== (task.exitSceneId ?? null)) patch.exitSceneId = exitId;
  if (cooldown !== null && cooldown !== task.cooldownMinutes) patch.cooldownMinutes = cooldown;
  if (debounce !== null && debounce !== task.exitDebounceSeconds) {
    patch.exitDebounceSeconds = debounce;
  }
  if ((maxDwell ?? null) !== (task.maxDwellSeconds ?? null)) patch.maxDwellSeconds = maxDwell;
  if (draft.enabled !== task.enabled) patch.enabled = draft.enabled;
  return patch;
}

function buildInput(draft: Draft): SceneTaskInput {
  return {
    description: draft.description.trim(),
    query: draft.query.trim(),
    perceiveDeviceIds: [...draft.cameraDids],
    enterSceneId: draft.enterSceneId || null,
    exitSceneId: draft.exitSceneId || null,
    cooldownMinutes: num(draft.cooldownMinutes) ?? 5,
    exitDebounceSeconds: num(draft.exitDebounceSeconds) ?? 60,
    maxDwellSeconds: num(draft.maxDwellSeconds),
    enabled: draft.enabled,
  };
}

// ── 创建 / 编辑抽屉 ──────────────────────────────────────────────

function SceneTaskDrawer({
  task,
  scenes,
  cameraOptions,
  onClose,
  onChanged,
}: {
  task: SceneTask | null; // null = 新建
  scenes: Scene[];
  cameraOptions: ScopeCamera[];
  onClose: () => void;
  onChanged: () => void | Promise<void>;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState<Draft>(() =>
    task ? draftFromTask(task) : emptyDraft(),
  );
  const [busy, setBusy] = useState(false);
  const [confirmDel, setConfirmDel] = useState(false);
  const editing = task !== null;

  useEscClose(true, () => {
    if (busy) return;
    if (confirmDel) setConfirmDel(false);
    else onClose();
  });

  const set = (patch: Partial<Draft>) => setDraft((d) => ({ ...d, ...patch }));

  const toggleCamera = (did: string) => {
    setDraft((d) => ({
      ...d,
      cameraDids: d.cameraDids.includes(did)
        ? d.cameraDids.filter((x) => x !== did)
        : [...d.cameraDids, did],
    }));
  };

  const validate = (): string | null => {
    if (!draft.description.trim()) return t("sceneTasks.errName");
    if (!draft.query.trim()) return t("sceneTasks.errQuery");
    if (draft.cameraDids.length === 0) return t("sceneTasks.errCamera");
    if (!draft.enterSceneId && !draft.exitSceneId) return t("sceneTasks.errScene");
    return null;
  };

  const save = async () => {
    const err = validate();
    if (err) {
      toast(err, "warn");
      return;
    }
    setBusy(true);
    try {
      if (editing && task) {
        const patch = buildPatch(draft, task);
        if (Object.keys(patch).length > 0) await updateSceneTask(task.taskId, patch);
        toast(t("sceneTasks.saved"), "ok");
      } else {
        await createSceneTask(buildInput(draft));
        toast(t("sceneTasks.created"), "ok");
      }
      await onChanged();
      onClose();
    } catch (e) {
      toast(e instanceof Error ? e.message : t("family.operationFail"), "warn");
    } finally {
      setBusy(false);
    }
  };

  const doDelete = async () => {
    if (!task) return;
    setBusy(true);
    try {
      await deleteSceneTask(task.taskId);
      toast(t("sceneTasks.deleted"), "ok");
      await onChanged();
      onClose();
    } catch (e) {
      toast(e instanceof Error ? e.message : t("family.operationFail"), "warn");
    } finally {
      setBusy(false);
    }
  };

  const doTrigger = async () => {
    if (!task) return;
    setBusy(true);
    try {
      await triggerSceneTask(task.taskId);
      toast(t("sceneTasks.triggered"), "ok");
    } catch (e) {
      toast(e instanceof Error ? e.message : t("sceneTasks.triggerFail"), "warn");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-[65] flex items-end md:items-center justify-center bg-black/40 backdrop-blur-sm"
      onClick={(e) => {
        e.stopPropagation();
        if (busy) return;
        if (confirmDel) setConfirmDel(false);
        else onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="scene-task-drawer-title"
        className="flex w-full max-h-[90vh] flex-col bg-bg-secondary border border-border rounded-t-2xl md:max-w-lg md:rounded-2xl shadow-lg anim-in"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3 px-5 pt-5 pb-4 border-b border-border shrink-0">
          <div className="min-w-0 flex-1">
            <h2
              id="scene-task-drawer-title"
              className="text-title font-semibold text-text-primary"
            >
              {editing ? t("sceneTasks.editTitle") : t("sceneTasks.createTitle")}
            </h2>
            <p className="text-caption text-text-tertiary mt-0.5">
              {t("sceneTasks.drawerHint")}
            </p>
          </div>
          <button
            type="button"
            onClick={() => {
              if (confirmDel) setConfirmDel(false);
              else onClose();
            }}
            disabled={busy}
            aria-label={t("family.close")}
            className="shrink-0 p-1.5 -mr-1.5 rounded-md text-text-tertiary hover:text-text-primary hover:bg-bg-tertiary transition-colors disabled:opacity-50"
          >
            <IconX width={18} height={18} />
          </button>
        </div>

        <div className="px-5 py-5 overflow-y-auto space-y-5">
          <Section title={t("sceneTasks.nameLabel")}>
            <input
              value={draft.description}
              onChange={(e) => set({ description: e.target.value })}
              maxLength={200}
              placeholder={t("sceneTasks.namePlaceholder")}
              className={inputCls}
            />
          </Section>

          <Section title={t("sceneTasks.camerasLabel")}>
            {cameraOptions.length === 0 ? (
              <div className="text-caption text-text-tertiary">
                {t("sceneTasks.noCameras")}
              </div>
            ) : (
              <div className="flex flex-wrap gap-1.5">
                {cameraOptions.map((c) => {
                  const on = draft.cameraDids.includes(c.did);
                  return (
                    <button
                      key={c.did}
                      type="button"
                      onClick={() => toggleCamera(c.did)}
                      className={`inline-flex items-center gap-1.5 text-caption px-2.5 py-1.5 rounded-full border transition-colors ${
                        on
                          ? "bg-brand-soft border-brand-primary text-brand-primary"
                          : "border-border bg-bg-primary text-text-secondary hover:border-border-strong"
                      }`}
                    >
                      {on && <IconCheck width={12} height={12} />}
                      <span className="max-w-[160px] truncate">
                        {c.name}
                        {c.roomName ? ` · ${c.roomName}` : ""}
                      </span>
                    </button>
                  );
                })}
              </div>
            )}
            <p className="text-caption text-text-tertiary mt-1.5">
              {t("sceneTasks.camerasHint")}
            </p>
          </Section>

          <Section title={t("sceneTasks.conditionLabel")}>
            <textarea
              value={draft.query}
              onChange={(e) => set({ query: e.target.value })}
              rows={2}
              maxLength={200}
              placeholder={t("sceneTasks.conditionPlaceholder")}
              className={`${inputCls} resize-none`}
            />
            <p className="text-caption text-text-tertiary leading-relaxed mt-1.5">
              {t("sceneTasks.conditionHint")}
            </p>
          </Section>

          <div className="grid grid-cols-1 gap-4">
            <Section title={t("sceneTasks.enterSceneLabel")}>
              <SceneSelect
                value={draft.enterSceneId}
                scenes={scenes}
                noneLabel={t("sceneTasks.noScene")}
                onChange={(v) => set({ enterSceneId: v })}
              />
            </Section>

            <Section title={t("sceneTasks.exitSceneLabel")}>
              <SceneSelect
                value={draft.exitSceneId}
                scenes={scenes}
                noneLabel={t("sceneTasks.noScene")}
                onChange={(v) => set({ exitSceneId: v })}
              />
              <p className="text-caption text-text-tertiary leading-relaxed mt-1.5">
                {t("sceneTasks.exitHint")}
              </p>
            </Section>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="text-caption text-text-secondary block mb-1.5">
                {t("sceneTasks.cooldownLabel")}
              </span>
              <input
                type="number"
                min={1}
                value={draft.cooldownMinutes}
                onChange={(e) => set({ cooldownMinutes: e.target.value })}
                className={inputCls}
              />
            </label>
            <label className="block">
              <span className="text-caption text-text-secondary block mb-1.5">
                {t("sceneTasks.debounceLabel")}
              </span>
              <input
                type="number"
                min={0}
                value={draft.exitDebounceSeconds}
                onChange={(e) => set({ exitDebounceSeconds: e.target.value })}
                className={inputCls}
              />
            </label>
          </div>

          <label className="block">
            <span className="text-caption text-text-secondary block mb-1.5">
              {t("sceneTasks.dwellLabel")}
            </span>
            <input
              type="number"
              min={1}
              value={draft.maxDwellSeconds}
              onChange={(e) => set({ maxDwellSeconds: e.target.value })}
              placeholder={t("sceneTasks.dwellPlaceholder")}
              className={inputCls}
            />
            <p className="text-caption text-text-tertiary leading-relaxed mt-1.5">
              {t("sceneTasks.dwellHint")}
            </p>
          </label>

          <div className="flex items-center justify-between">
            <span className="text-body text-text-primary">{t("sceneTasks.enabledLabel")}</span>
            <Switch
              checked={draft.enabled}
              label={draft.enabled ? t("sceneTasks.on") : t("sceneTasks.off")}
              onChange={() => set({ enabled: !draft.enabled })}
            />
          </div>
        </div>

        <div className="flex items-center justify-between gap-2 px-4 py-3 border-t border-border shrink-0">
          {confirmDel ? (
            <>
              <span className="flex-1 min-w-0 text-caption text-error break-words line-clamp-2">
                {t("sceneTasks.confirmDelete", { name: task?.description ?? "" })}
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
          ) : editing ? (
            <>
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  onClick={doTrigger}
                  disabled={busy}
                  title={t("sceneTasks.triggerHint")}
                  className="inline-flex items-center gap-1 h-9 px-3 rounded-lg text-caption text-text-tertiary hover:text-brand-primary hover:bg-brand-soft transition-colors disabled:opacity-60"
                >
                  <IconHelp width={14} height={14} />
                  {t("sceneTasks.trigger")}
                </button>
                <button
                  type="button"
                  onClick={() => setConfirmDel(true)}
                  disabled={busy}
                  className="inline-flex items-center gap-1.5 h-9 px-3 rounded-lg text-caption text-text-tertiary hover:text-error hover:bg-error-bg transition-colors disabled:opacity-60"
                >
                  <IconTrash width={15} height={15} />
                  {t("sceneTasks.delete")}
                </button>
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={onClose}
                  disabled={busy}
                  className="h-9 px-4 rounded-lg text-caption text-text-secondary hover:text-text-primary hover:bg-bg-tertiary transition-colors disabled:opacity-60"
                >
                  {t("family.cancel")}
                </button>
                <button
                  type="button"
                  onClick={save}
                  disabled={busy}
                  className="h-9 px-4 rounded-lg text-caption font-semibold bg-brand-primary text-white hover:bg-brand-accent transition-colors disabled:opacity-60"
                >
                  {busy ? t("family.saving") : t("family.save")}
                </button>
              </div>
            </>
          ) : (
            <>
              <span />
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={onClose}
                  disabled={busy}
                  className="h-9 px-4 rounded-lg text-caption text-text-secondary hover:text-text-primary hover:bg-bg-tertiary transition-colors disabled:opacity-60"
                >
                  {t("family.cancel")}
                </button>
                <button
                  type="button"
                  onClick={save}
                  disabled={busy}
                  className="h-9 px-4 rounded-lg text-caption font-semibold bg-brand-primary text-white hover:bg-brand-accent transition-colors disabled:opacity-60"
                >
                  {busy ? t("family.saving") : t("sceneTasks.create")}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
);
}

// ── 页面主视图 ────────────────────────────────────────────────────

export function SceneTasksPage({ tasks, scenes, cameras, loading, onChanged }: Props) {
  const { t } = useTranslation();
  const [busyId, setBusyId] = useState<string | null>(null);
  const [editId, setEditId] = useState<string | null | undefined>(undefined);
  const [createOpen, setCreateOpen] = useState(false);

  // 多通道相机按物理 did 去重（规则按整台相机绑定）。
  const cameraOptions = useMemo(() => {
    const seen = new Set<string>();
    return cameras.filter((c) => {
      if (seen.has(c.did)) return false;
      seen.add(c.did);
      return true;
    });
  }, [cameras]);

  const list = tasks ?? [];
  const empty = !loading && list.length === 0;
  const editing = editId !== undefined ? (list.find((x) => x.taskId === editId) ?? null) : undefined;

  const sceneName = (t: SceneTask, key: "enterSceneId" | "exitSceneId") => {
    const id = key === "enterSceneId" ? t.enterSceneId : t.exitSceneId;
    const name = key === "enterSceneId" ? t.enterSceneName : t.exitSceneName;
    return name ?? id ?? "";
  };

  const toggle = async (task: SceneTask) => {
    setBusyId(task.taskId);
    try {
      await setSceneTaskEnabled(task.taskId, !task.enabled);
      toast(task.enabled ? t("sceneTasks.paused") : t("sceneTasks.enabled"), "ok");
      onChanged();
    } catch (e) {
      toast(e instanceof Error ? e.message : t("family.operationFail"), "warn");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="space-y-6">
      <section
        className="rounded-xl bg-bg-secondary border border-border shadow-sm anim-in"
        aria-labelledby="scene-tasks-title"
      >
        <div className="flex items-start justify-between gap-2 px-5 pt-4 pb-1">
          <div className="min-w-0">
            <h2
              id="scene-tasks-title"
              className="text-title text-text-primary inline-flex items-baseline gap-2"
            >
              {t("sceneTasks.title")}
              <span className="text-caption-mono text-text-tertiary font-normal num">
                {t("sceneTasks.count", { count: list.length })}
              </span>
            </h2>
            <p className="text-caption text-text-tertiary mt-0.5">
              {t("sceneTasks.hint")}
            </p>
          </div>
          <button
            type="button"
            onClick={() => setCreateOpen(true)}
            className="shrink-0 inline-flex items-center gap-1.5 h-9 px-3 rounded-lg text-caption font-semibold border border-border bg-bg-primary text-text-secondary hover:text-text-primary hover:border-border-strong transition-colors"
          >
            <IconPlus width={14} height={14} />
            {t("sceneTasks.add")}
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
            <div className="text-body text-text-secondary">{t("sceneTasks.empty")}</div>
            <div className="text-caption text-text-tertiary mt-1">
              {t("sceneTasks.emptyHint")}
            </div>
            <button
              type="button"
              onClick={() => setCreateOpen(true)}
              className="mt-4 inline-flex items-center gap-1.5 text-caption px-3 py-1.5 rounded-md border border-border bg-bg-primary text-text-secondary hover:text-text-primary hover:border-border-strong transition-colors"
            >
              <IconPlus width={15} height={15} />
              {t("sceneTasks.add")}
            </button>
          </div>
        ) : (
          <div className="px-5 pt-2 pb-4 divide-y divide-border">
            {list.map((task) => {
              const busy = busyId === task.taskId;
              const paused = task.status === "paused" || !task.enabled;
              return (
                <div key={task.taskId} className="group flex items-center gap-3 py-3">
                  <button
                    type="button"
                    onClick={() => setEditId(task.taskId)}
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
                      <span
                        className={`shrink-0 text-caption px-1.5 py-0.5 rounded ${
                          paused
                            ? "bg-bg-tertiary text-text-tertiary"
                            : "bg-success-bg text-success"
                        }`}
                      >
                        {paused ? t("sceneTasks.statusPaused") : t("sceneTasks.statusActive")}
                      </span>
                    </div>
                    <div className="flex items-center gap-1.5 mt-1 flex-wrap">
                      <span className="text-caption text-text-secondary">
                        {sceneName(task, "enterSceneId") || t("sceneTasks.noScene")}
                      </span>
                      <span className="text-caption text-text-tertiary">→</span>
                      <span className="text-caption text-text-secondary">
                        {sceneName(task, "exitSceneId") || t("sceneTasks.noScene")}
                      </span>
                      <span className="text-caption text-text-tertiary num ml-1">
                        {t("sceneTasks.cameraCount", {
                          count: task.perceiveDeviceIds.length,
                        })}
                      </span>
                      {task.maxDwellSeconds != null && (
                        <span className="text-caption text-text-tertiary num">
                          {t("sceneTasks.dwellBadge", {
                            seconds: task.maxDwellSeconds,
                          })}
                        </span>
                      )}
                    </div>
                    <div className="text-caption text-text-tertiary truncate mt-0.5">
                      {task.query}
                    </div>
                  </button>

                  <Switch
                    checked={task.enabled}
                    disabled={busy}
                    label={task.enabled ? t("sceneTasks.pause") : t("sceneTasks.enable")}
                    onChange={() => toggle(task)}
                  />
                  <button
                    type="button"
                    onClick={() => setEditId(task.taskId)}
                    aria-label={t("family.edit")}
                    className="shrink-0 p-2 rounded-md text-text-tertiary hover:text-text-primary hover:bg-bg-tertiary transition-colors"
                  >
                    <IconPencil width={16} height={16} />
                  </button>
                </div>
              );
            })}
          </div>
        )}
      </section>

      {createOpen && (
        <SceneTaskDrawer
          task={null}
          scenes={scenes}
          cameraOptions={cameraOptions}
          onClose={() => setCreateOpen(false)}
          onChanged={onChanged}
        />
      )}
      {editing !== undefined && editing !== null && (
        <SceneTaskDrawer
          key={editing.taskId}
          task={editing}
          scenes={scenes}
          cameraOptions={cameraOptions}
          onClose={() => setEditId(undefined)}
          onChanged={onChanged}
        />
      )}
    </div>
  );
}