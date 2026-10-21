/**
 * 场景联动页——独立「场景联动」Tab 的主视图。
 *
 * 场景联动任务 = 一条 state 规则把「感知命中」直接接到两类动作（进入 / 退出各可同时配）：
 *   - 触发米家自动化场景（如识别到床上有人看书 → 开灯）
 *   - 用指定音响播报一段 TTS——文案可以是固定文本，也可以由 LLM 按画面动态生成
 *     （如每天出门前根据穿搭夸一句）
 *   - 进入：条件 query 变为满足并持续 enterDebounceSeconds（进入确认时间）→ 执行
 *   - 退出：条件不再满足并持续 exitDebounceSeconds → 执行；或配置 maxDwellSeconds 后
 *     「到期自动退出」
 * 命中 / 抗抖 / 冷却完全复用规则引擎，**不经过 agent**（后端 /api/scene-tasks）。
 * 本页负责创建 / 编辑 / 启停 / 删除，并提供「手动触发」调试入口。
 *
 * 进入本页（挂载）时会把自动化场景列表与可选音响列表都从后端重新拉一遍（force refresh）
 * ——住户刚在米家新建的场景 / 音箱不必等整页刷新就能选到。打开任意抽屉时再刷新一次，
 * 保证列表永远是打开那一刻的最新状态。
 */

import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { modalPortal } from "@/lib/modalPortal";
import {
  createSceneTask,
  deleteSceneTask,
  listSceneVoiceDevices,
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
  IconRefresh,
  IconTrash,
  IconVolume,
  IconX,
} from "@/lib/icons";
import type {
  Scene,
  SceneTask,
  SceneTaskInput,
  SceneTts,
  SceneVoiceDevice,
  ScopeCamera,
} from "@/lib/types";
import { toast } from "./Toast";

interface Props {
  tasks: SceneTask[] | undefined;
  scenes: Scene[];
  cameras: ScopeCamera[];
  loading: boolean;
  onChanged: () => void | Promise<void>;
  /** 重新拉取相机 / 场景等页面级数据（tab 挂载时与打开抽屉时调用，避开缓存）。 */
  onRefreshSources?: () => void | Promise<void>;
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

// 自动化场景下拉：value "" = 该方向不触发自动化。
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

// 音响下拉：value "" = 该方向不播报。
function SpeakerSelect({
  value,
  speakers,
  noneLabel,
  onChange,
}: {
  value: string;
  speakers: SceneVoiceDevice[];
  noneLabel: string;
  onChange: (v: string) => void;
}) {
  // 已配置的音箱不在（最新）列表里时也要能显示出来——否则住户会以为自己没配。
  const missing = value && !speakers.some((s) => s.did === value);
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className={`${inputCls} ${value ? "" : "text-text-tertiary"}`}
    >
      <option value="">{noneLabel}</option>
      {missing && <option value={value}>{value}</option>}
      {speakers.map((s) => (
        <option key={s.did} value={s.did}>
          {s.name}
          {s.roomName ? ` · ${s.roomName}` : ""}
          {s.online ? "" : " (离线)"}
        </option>
      ))}
    </select>
  );
}

// 一个方向的 TTS 草稿。
interface TtsDraft {
  deviceId: string;
  content: boolean; // true = 动态生成，false = 固定文案
  prompt: string;
  text: string;
}

interface Draft {
  description: string;
  query: string;
  sceneNotes: string;
  cameraDids: string[];
  enterSceneId: string;
  exitSceneId: string;
  enterTts: TtsDraft;
  exitTts: TtsDraft;
  cooldownMinutes: string;
  enterDebounceSeconds: string;
  exitDebounceSeconds: string;
  maxDwellSeconds: string;
  enabled: boolean;
}

const emptyTts = (): TtsDraft => ({
  deviceId: "",
  content: true,
  prompt: "",
  text: "",
});

function ttsFromView(tts: SceneTts | null | undefined): TtsDraft {
  if (!tts || !tts.deviceId) return emptyTts();
  return {
    deviceId: tts.deviceId,
    content: tts.ttsContent,
    prompt: tts.ttsPrompt ?? "",
    text: tts.ttsText ?? "",
  };
}

function draftFromTask(task: SceneTask): Draft {
  return {
    description: task.description,
    query: task.query,
    sceneNotes: task.sceneNotes ?? "",
    cameraDids: [...task.perceiveDeviceIds],
    enterSceneId: task.enterSceneId ?? "",
    exitSceneId: task.exitSceneId ?? "",
    enterTts: ttsFromView(task.enterTts),
    exitTts: ttsFromView(task.exitTts),
    cooldownMinutes: String(task.cooldownMinutes ?? 1),
    enterDebounceSeconds: String(task.enterDebounceSeconds ?? 0),
    exitDebounceSeconds: String(task.exitDebounceSeconds),
    maxDwellSeconds: task.maxDwellSeconds ? String(task.maxDwellSeconds) : "",
    enabled: task.enabled,
  };
}

export function emptyDraft(): Draft {
  return {
    description: "",
    query: "",
    sceneNotes: "",
    cameraDids: [],
    enterSceneId: "",
    exitSceneId: "",
    enterTts: emptyTts(),
    exitTts: emptyTts(),
    cooldownMinutes: "1",
    enterDebounceSeconds: "0",
    exitDebounceSeconds: "0",
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

// 草稿里的 TTS → 提交载荷。没选音响 = 该方向不播报（null）。
function ttsPayload(t: TtsDraft): {
  deviceId: string | null;
  tts: { ttsContent: boolean; ttsPrompt: string | null; ttsText: string | null } | null;
} {
  if (!t.deviceId) return { deviceId: null, tts: null };
  return {
    deviceId: t.deviceId,
    tts: {
      ttsContent: t.content,
      // 动态模式带「播报要求」；固定模式不需要（但仍保留住户写过的文字，方便切回来）。
      ttsPrompt: t.prompt.trim() || null,
      ttsText: t.text.trim() || null,
    },
  };
}

const sameTts = (a: TtsDraft, b: SceneTts | null | undefined): boolean => {
  const bv = ttsFromView(b);
  return (
    a.deviceId === bv.deviceId &&
    a.content === bv.content &&
    a.prompt.trim() === bv.prompt.trim() &&
    a.text.trim() === bv.text.trim()
  );
};

// 编辑模式只提交真正改过的字段：PATCH 语义下显式 null = 清空该方向动作，
// 未动过的字段绝不能随请求发出去（否则会把用户没碰的配置清掉）。
function buildPatch(draft: Draft, task: SceneTask): SceneTaskInput {
  const patch: SceneTaskInput = {};
  const enterId = draft.enterSceneId || null;
  const exitId = draft.exitSceneId || null;
  const maxDwell = num(draft.maxDwellSeconds);
  const cooldown = num(draft.cooldownMinutes);
  const enterDebounce = num(draft.enterDebounceSeconds);
  const debounce = num(draft.exitDebounceSeconds);
  if (draft.description !== task.description) patch.description = draft.description;
  if (draft.query !== task.query) patch.query = draft.query;
  // 场景补充说明：trim 后比对；清空 → null（后端 PATCH 收到 null/"" 即清除）。
  if (draft.sceneNotes.trim() !== (task.sceneNotes ?? "").trim()) {
    patch.sceneNotes = draft.sceneNotes.trim() || null;
  }
  if (!sameIds(draft.cameraDids, task.perceiveDeviceIds)) {
    patch.perceiveDeviceIds = [...draft.cameraDids];
  }
  if ((enterId ?? null) !== (task.enterSceneId ?? null)) patch.enterSceneId = enterId;
  if ((exitId ?? null) !== (task.exitSceneId ?? null)) patch.exitSceneId = exitId;
  const enterTts = ttsPayload(draft.enterTts);
  if (!sameTts(draft.enterTts, task.enterTts)) {
    patch.enterTtsDeviceId = enterTts.deviceId;
    patch.enterTts = enterTts.tts;
  }
  const exitTts = ttsPayload(draft.exitTts);
  if (!sameTts(draft.exitTts, task.exitTts)) {
    patch.exitTtsDeviceId = exitTts.deviceId;
    patch.exitTts = exitTts.tts;
  }
  if (cooldown !== null && cooldown !== task.cooldownMinutes) patch.cooldownMinutes = cooldown;
  if (enterDebounce !== null && enterDebounce !== task.enterDebounceSeconds) {
    patch.enterDebounceSeconds = enterDebounce;
  }
  if (debounce !== null && debounce !== task.exitDebounceSeconds) {
    patch.exitDebounceSeconds = debounce;
  }
  if ((maxDwell ?? null) !== (task.maxDwellSeconds ?? null)) patch.maxDwellSeconds = maxDwell;
  if (draft.enabled !== task.enabled) patch.enabled = draft.enabled;
  return patch;
}

export function buildInput(draft: Draft): SceneTaskInput {
  const enterTts = ttsPayload(draft.enterTts);
  const exitTts = ttsPayload(draft.exitTts);
  return {
    description: draft.description.trim(),
    query: draft.query.trim(),
    sceneNotes: draft.sceneNotes.trim() || null,
    perceiveDeviceIds: [...draft.cameraDids],
    enterSceneId: draft.enterSceneId || null,
    exitSceneId: draft.exitSceneId || null,
    enterTtsDeviceId: enterTts.deviceId,
    enterTts: enterTts.tts,
    exitTtsDeviceId: exitTts.deviceId,
    exitTts: exitTts.tts,
    cooldownMinutes: num(draft.cooldownMinutes) ?? 1,
    enterDebounceSeconds: num(draft.enterDebounceSeconds) ?? 0,
    exitDebounceSeconds: num(draft.exitDebounceSeconds) ?? 0,
    maxDwellSeconds: num(draft.maxDwellSeconds),
    enabled: draft.enabled,
  };
}

// ── 一个方向的 TTS 编辑块 ─────────────────────────────────────────

function TtsEditor({
  draft,
  speakers,
  speakersLoading,
  title,
  deviceLabel,
  hint,
  onChange,
}: {
  draft: TtsDraft;
  speakers: SceneVoiceDevice[];
  speakersLoading: boolean;
  title: string;
  deviceLabel: string;
  hint: string;
  onChange: (patch: Partial<TtsDraft>) => void;
}) {
  const { t } = useTranslation();
  const on = !!draft.deviceId;
  return (
    <Section title={title}>
      <div className="flex items-center gap-2">
        <IconVolume
          width={15}
          height={15}
          className={on ? "text-brand-primary shrink-0" : "text-text-tertiary shrink-0"}
        />
        <div className="flex-1 min-w-0">
          <SpeakerSelect
            value={draft.deviceId}
            speakers={speakers}
            noneLabel={
              speakersLoading
                ? t("sceneTasks.speakersLoading")
                : t("sceneTasks.noTtsDevice")
            }
            onChange={(v) => onChange({ deviceId: v })}
          />
        </div>
      </div>
      <p className="text-caption text-text-tertiary leading-relaxed mt-1.5">{hint}</p>
      {speakers.length === 0 && !speakersLoading && (
        <p className="text-caption text-warning mt-1">{t("sceneTasks.noSpeakers")}</p>
      )}

      {on && (
        <div className="mt-2.5 space-y-2 rounded-lg border border-border bg-bg-primary/60 p-2.5">
          <div className="flex items-center gap-4">
            {(
              [
                [true, t("sceneTasks.ttsDynamic")],
                [false, t("sceneTasks.ttsFixed")],
              ] as const
            ).map(([val, label]) => (
              <label
                key={String(val)}
                className="inline-flex items-center gap-1.5 text-caption text-text-secondary cursor-pointer"
              >
                <input
                  type="radio"
                  name={`tts-mode-${title}`}
                  checked={draft.content === val}
                  onChange={() => onChange({ content: val })}
                  className="accent-brand-primary"
                />
                {label}
              </label>
            ))}
          </div>
          {draft.content ? (
            <>
              <label className="block">
                <span className="text-caption text-text-secondary block mb-1">
                  {deviceLabel}
                </span>
                <textarea
                  value={draft.prompt}
                  onChange={(e) => onChange({ prompt: e.target.value })}
                  rows={3}
                  maxLength={800}
                  placeholder={t("sceneTasks.ttsPromptPlaceholder")}
                  className={`${inputCls} resize-y`}
                />
              </label>
              <label className="block">
                <span className="text-caption text-text-secondary block mb-1">
                  {t("sceneTasks.ttsFallbackLabel")}
                </span>
                <input
                  value={draft.text}
                  onChange={(e) => onChange({ text: e.target.value })}
                  maxLength={200}
                  placeholder={t("sceneTasks.ttsFallbackPlaceholder")}
                  className={inputCls}
                />
              </label>
            </>
          ) : (
            <label className="block">
              <span className="text-caption text-text-secondary block mb-1">
                {t("sceneTasks.ttsTextLabel")}
              </span>
              <textarea
                value={draft.text}
                onChange={(e) => onChange({ text: e.target.value })}
                rows={2}
                maxLength={200}
                placeholder={t("sceneTasks.ttsTextPlaceholder")}
                className={`${inputCls} resize-none`}
              />
            </label>
          )}
        </div>
      )}
    </Section>
  );
}

// ── 创建 / 编辑抽屉 ──────────────────────────────────────────────

function SceneTaskDrawer({
  task,
  scenes,
  cameraOptions,
  speakers,
  speakersLoading,
  onClose,
  onChanged,
}: {
  task: SceneTask | null; // null = 新建
  scenes: Scene[];
  cameraOptions: ScopeCamera[];
  speakers: SceneVoiceDevice[];
  speakersLoading: boolean;
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
    const hasAction =
      draft.enterSceneId ||
      draft.exitSceneId ||
      draft.enterTts.deviceId ||
      draft.exitTts.deviceId;
    if (!hasAction) return t("sceneTasks.errScene");
    for (const [label, tts] of [
      ["enter", draft.enterTts],
      ["exit", draft.exitTts],
    ] as const) {
      if (!tts.deviceId) continue;
      if (tts.content && !tts.prompt.trim() && !tts.text.trim()) {
        return t("sceneTasks.errTtsPrompt", {
          side: t(`sceneTasks.${label}`),
        });
      }
      if (!tts.content && !tts.text.trim()) {
        return t("sceneTasks.errTtsText", { side: t(`sceneTasks.${label}`) });
      }
    }
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

  // 同 TaskDetailSheet：页面根 space-y-6 的 margin-top 会把遮罩推下去 24px，portal 到 body。
  return modalPortal(
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

          {/* 场景补充说明：只作用于本条规则的判定细则，随规则注入 omni 的
              「# 场景判定补充细则」段（与「设置」页的全局感知提示词正交）。 */}
          <Section title={t("sceneTasks.sceneNotesLabel")}>
            <textarea
              value={draft.sceneNotes}
              onChange={(e) => set({ sceneNotes: e.target.value })}
              rows={3}
              maxLength={8000}
              placeholder={t("sceneTasks.sceneNotesPlaceholder")}
              className={`${inputCls} resize-y`}
            />
            <p className="text-caption text-text-tertiary leading-relaxed mt-1.5">
              {t("sceneTasks.sceneNotesHint")}
            </p>
          </Section>

          {/* 进入动作：自动化场景 + TTS 可同时设置 */}
          <div className="space-y-4 rounded-xl border border-border p-3.5">
            <div className="text-body font-semibold text-text-primary">
              {t("sceneTasks.enterSection")}
            </div>
            <Section title={t("sceneTasks.enterSceneLabel")}>
              <SceneSelect
                value={draft.enterSceneId}
                scenes={scenes}
                noneLabel={t("sceneTasks.noScene")}
                onChange={(v) => set({ enterSceneId: v })}
              />
              <p className="text-caption text-text-tertiary leading-relaxed mt-1.5">
                {t("sceneTasks.enterHint")}
              </p>
            </Section>
            <TtsEditor
              draft={draft.enterTts}
              speakers={speakers}
              speakersLoading={speakersLoading}
              title={t("sceneTasks.enterTtsLabel")}
              deviceLabel={t("sceneTasks.ttsPromptLabel")}
              hint={t("sceneTasks.enterTtsHint")}
              onChange={(p) =>
                set({ enterTts: { ...draft.enterTts, ...p } })
              }
            />
          </div>

          {/* 退出动作：同上 */}
          <div className="space-y-4 rounded-xl border border-border p-3.5">
            <div className="text-body font-semibold text-text-primary">
              {t("sceneTasks.exitSection")}
            </div>
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
            <TtsEditor
              draft={draft.exitTts}
              speakers={speakers}
              speakersLoading={speakersLoading}
              title={t("sceneTasks.exitTtsLabel")}
              deviceLabel={t("sceneTasks.ttsPromptLabel")}
              hint={t("sceneTasks.exitTtsHint")}
              onChange={(p) => set({ exitTts: { ...draft.exitTts, ...p } })}
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="text-caption text-text-secondary block mb-1.5">
                {t("sceneTasks.enterDebounceLabel")}
              </span>
              <input
                type="number"
                min={0}
                value={draft.enterDebounceSeconds}
                onChange={(e) => set({ enterDebounceSeconds: e.target.value })}
                placeholder={t("sceneTasks.enterDebouncePlaceholder")}
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
            </label>
          </div>
          <p className="text-caption text-text-tertiary leading-relaxed">
            {t("sceneTasks.dwellHint")}
          </p>

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
    </div>,
  );
}

// ── 页面主视图 ────────────────────────────────────────────────────

export function SceneTasksPage({
  tasks,
  scenes,
  cameras,
  loading,
  onChanged,
  onRefreshSources,
}: Props) {
  const { t } = useTranslation();
  const [busyId, setBusyId] = useState<string | null>(null);
  const [editId, setEditId] = useState<string | null | undefined>(undefined);
  const [createOpen, setCreateOpen] = useState(false);
  const [speakers, setSpeakers] = useState<SceneVoiceDevice[]>([]);
  const [speakersLoading, setSpeakersLoading] = useState(false);
  const [refreshingSources, setRefreshingSources] = useState(false);

  // 可选音响：进入页面即从米家云刷新（后端 refresh=true），进入 / 打开抽屉也再刷一次。
  const loadSpeakers = async () => {
    setSpeakersLoading(true);
    try {
      setSpeakers(await listSceneVoiceDevices());
    } catch {
      // 拉不到音响列表不该挡住页面：退化成「暂时没有可选音响」提示，动作仍可保存。
      setSpeakers((prev) => prev);
    } finally {
      setSpeakersLoading(false);
    }
  };

  // 页面级数据（自动化场景 / 相机）刷新：与音响一起，避开前端 5s 缓存与后端旧列表。
  const refreshSources = async () => {
    setRefreshingSources(true);
    try {
      await onRefreshSources?.();
    } finally {
      setRefreshingSources(false);
    }
  };

  const refreshAll = () => {
    void loadSpeakers();
    void refreshSources();
  };

  useEffect(() => {
    // tab 挂载：自动化 + 音响列表都重新拉一遍，避免缓存里少了刚建好的场景 / 音箱。
    refreshAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 打开任意抽屉：再刷新一次（列表 = 打开那一刻的最新状态）。
  const openDrawer = (taskId: string | null) => {
    refreshAll();
    if (taskId === null) setCreateOpen(true);
    else setEditId(taskId);
  };

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
  const hasDynamicTts = list.some(
    (x) => x.enterTts?.ttsContent || x.exitTts?.ttsContent,
  );
  const dynamicWarning = hasDynamicTts && list.some((x) => !x.dynamicTtsReady);

  // 一个方向的展示文案：自动化场景名 / 播报音响名（都有则并列）。
  const actionLabel = (task: SceneTask, side: "enter" | "exit"): string => {
    const sceneId = side === "enter" ? task.enterSceneId : task.exitSceneId;
    const sceneName = side === "enter" ? task.enterSceneName : task.exitSceneName;
    const tts = side === "enter" ? task.enterTts : task.exitTts;
    const parts: string[] = [];
    if (sceneId) parts.push(sceneName ?? sceneId);
    if (tts?.deviceId) {
      const speaker = tts.deviceName ?? speakers.find((s) => s.did === tts.deviceId)?.name ?? tts.deviceId;
      parts.push(t("sceneTasks.ttsAction", { name: speaker }));
    }
    return parts.join(" + ");
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
          <div className="flex items-center gap-2 shrink-0">
            <button
              type="button"
              onClick={refreshAll}
              disabled={refreshingSources || speakersLoading}
              title={t("sceneTasks.refreshSourcesHint")}
              aria-label={t("sceneTasks.refreshSourcesHint")}
              className="inline-flex items-center gap-1.5 h-9 px-2.5 rounded-lg text-caption border border-border bg-bg-primary text-text-secondary hover:text-text-primary hover:border-border-strong transition-colors disabled:opacity-60"
            >
              <IconRefresh
                width={14}
                height={14}
                className={refreshingSources || speakersLoading ? "animate-spin" : ""}
              />
            </button>
            <button
              type="button"
              onClick={() => openDrawer(null)}
              className="inline-flex items-center gap-1.5 h-9 px-3 rounded-lg text-caption font-semibold border border-border bg-bg-primary text-text-secondary hover:text-text-primary hover:border-border-strong transition-colors"
            >
              <IconPlus width={14} height={14} />
              {t("sceneTasks.add")}
            </button>
          </div>
        </div>

        {dynamicWarning && (
          <div className="mx-5 mt-2 rounded-lg bg-warning-bg border border-warning/30 px-3 py-2 text-caption text-warning leading-relaxed">
            {t("sceneTasks.dynamicTtsUnavailable")}
          </div>
        )}

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
              onClick={() => openDrawer(null)}
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
                    onClick={() => openDrawer(task.taskId)}
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
                        {actionLabel(task, "enter") || t("sceneTasks.noScene")}
                      </span>
                      <span className="text-caption text-text-tertiary">→</span>
                      <span className="text-caption text-text-secondary">
                        {actionLabel(task, "exit") || t("sceneTasks.noScene")}
                      </span>
                      <span className="text-caption text-text-tertiary num ml-1">
                        {t("sceneTasks.cameraCount", {
                          count: task.perceiveDeviceIds.length,
                        })}
                      </span>
                      {task.enterDebounceSeconds > 0 && (
                        <span className="text-caption text-text-tertiary num">
                          {t("sceneTasks.enterDebounceBadge", {
                            seconds: task.enterDebounceSeconds,
                          })}
                        </span>
                      )}
                      {task.maxDwellSeconds != null && (
                        <span className="text-caption text-text-tertiary num">
                          {t("sceneTasks.dwellBadge", {
                            seconds: task.maxDwellSeconds,
                          })}
                        </span>
                      )}
                      {(task.enterTts?.ttsContent || task.exitTts?.ttsContent) && (
                        <span className="inline-flex items-center gap-1 text-caption text-brand-primary bg-brand-soft px-1.5 py-0.5 rounded">
                          <IconVolume width={11} height={11} />
                          {t("sceneTasks.dynamicTtsBadge")}
                        </span>
                      )}
                      {task.sceneNotes && (
                        <span className="text-caption text-text-secondary bg-bg-tertiary px-1.5 py-0.5 rounded">
                          {t("sceneTasks.sceneNotesBadge")}
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
                    onClick={() => openDrawer(task.taskId)}
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
          speakers={speakers}
          speakersLoading={speakersLoading}
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
          speakers={speakers}
          speakersLoading={speakersLoading}
          onClose={() => setEditId(undefined)}
          onChanged={onChanged}
        />
      )}
    </div>
  );
}
