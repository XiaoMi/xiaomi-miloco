/**
 * 「家里此刻」Hero 区（v3 Mi Console 视觉）
 *
 * 整面板视觉重量最高的卡片：家人 chips + 摄像头实时画面 +
 * 全开/全关批量切换 + inUse 单卡 toggle。
 */

import type {
  CameraSchedule,
  PerceptionCamera,
  Person,
  ScopeCamera,
  UsageStats,
} from "@/lib/types";
import { PersonChip } from "./PersonChip";
import { LivePlayerPlaceholder } from "./LivePlayerPlaceholder";
import { getUsageStats } from "@/api";
import { useAsync } from "@/hooks/useAsync";
import { humanTokens } from "@/lib/formatTokens";
import { useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { IconClock, IconPlus, IconTrash, IconX } from "@/lib/icons";

interface Props {
  persons: Person[];
  /** perception 当前订阅的画面（含 channel，用于真播放）；scope 是子集映射的字典源 */
  cameras: PerceptionCamera[];
  /** 米家全集（含被禁用 / 离线），用于渲染所有摄像头卡片 + Switch */
  scopeCameras: ScopeCamera[];
  /** miot 上是否有 camera 类设备——区分两种空态 */
  miotHasCamera: boolean;
  /** 最多投喂给 miloco 的摄像头数(后端 MAX_ENABLED_CAMERAS，经 /api/miot/status 下发)。
   *  上区展示数受 connected 自然约束，此值只用于下区「启用」按钮的满额置灰判断。 */
  maxStreamCams: number;
  /** undefined → chip 渲染成 div(无 hover/点击反馈),概览页用 */
  onPersonClick?: (p: Person) => void;
  /** 点击"今日用量"小卡片跳到用量 tab。 */
  onJumpUsage?: () => void;
  /** 切换摄像头启用（PUT /api/miot/scope/cameras）；批量传 dids */
  onToggleCameras: (dids: string[], inUse: boolean) => void | Promise<void>;
  /** 设置摄像头每日感知时间段。 */
  onSetCameraSchedule: (
    did: string,
    schedule: CameraSchedule,
  ) => void | Promise<void>;
}

// 排序:已认识在前,未认识统一靠后
function sortPersons(ps: Person[]): Person[] {
  return [...ps].sort((a, b) => {
    if (a.faceEnrolled !== b.faceEnrolled) return a.faceEnrolled ? -1 : 1;
    return 0;
  });
}

export function HeroNow({
  persons,
  cameras,
  scopeCameras,
  miotHasCamera,
  maxStreamCams,
  onPersonClick,
  onJumpUsage,
  onToggleCameras,
  onSetCameraSchedule,
}: Props) {
  const { t } = useTranslation();
  const sorted = sortPersons(persons);
  // scope 是主列表（含禁用/离线/未接入）；cameras 仅用作 channel 字典。
  // useMemo 让 Map 引用稳定—— Map 每次 render 重建会让传到 CameraSection 的 prop
  // 引用变更,父级状态变（如 todayUsage 异步到达）触发的 re-render 会冲掉子组件
  // memo 优化机会。
  const channelByDid = useMemo(
    () => new Map(cameras.map((c) => [c.did, c.channel])),
    [cameras],
  );
  // 上区 = miloco **当前真正在投喂视频** 的相机。判据用后端权威字段 `connected`
  // (= MiotService._connected_camera_dids() = 感知 camera_adapter.get_connected_devices()，
  // 即真正建连、在喂解码帧给感知的那几路)，而不是 `inUse`(只是 KV 里的"想启用"意图——
  // 启用了但 LAN 拉不起来时 inUse=true 却没真投喂)。再 **按 did 稳定排序**:toggle 某路时
  // 其余卡 key+DOM 位置不变，React 复用其 iframe，不会连带把其它路的 watch 流断开重连。
  // 其余相机(未投喂:未启用 / 定时暂停 / 启用中未连上 / 超出上限)进下区「无流」横向列表。
  const { streamingCams, benchCams } = useMemo(() => {
    const byDid = (a: ScopeCamera, b: ScopeCamera) =>
      a.did < b.did ? -1 : a.did > b.did ? 1 : 0;
    const sorted = [...scopeCameras].sort(byDid);
    // 不再前端截断:connected 集天然受后端 MAX_ENABLED_CAMERAS 约束(感知接入层按 did
    // 升序截断到上限、只连前 N 路；主动 enable 超限也被 toggle_camera 挡下)，展示集即真实投喂集。
    const streaming = sorted.filter((c) => c.connected);
    const sset = new Set(streaming.map((c) => c.did));
    const bench = sorted.filter((c) => !sset.has(c.did));
    return { streamingCams: streaming, benchCams: bench };
  }, [scopeCameras]);
  // 今日 token 用量小入口（omni 计费）
  const todayUsage = useAsync<UsageStats>(
    () => getUsageStats("today"),
    [],
    { errorLabel: "" },
  );

  return (
    <section
      className="rounded-xl bg-bg-secondary border border-border shadow-sm p-5 md:p-6 anim-in"
      aria-labelledby="hero-now-title"
    >
      <div className="flex items-baseline justify-between gap-3 mb-4 flex-wrap">
        <h2
          id="hero-now-title"
          className="text-title text-text-primary inline-flex items-baseline gap-2"
        >
          {t("hero.title")}
          <span className="text-caption-mono text-text-tertiary font-normal">
            now
          </span>
        </h2>
        {todayUsage.data && (
          <button
            type="button"
            onClick={onJumpUsage}
            disabled={!onJumpUsage}
            aria-label={t("hero.usageAriaLabel")}
            title={t("hero.usageTitle")}
            className="text-caption inline-flex items-baseline gap-1.5 text-text-secondary hover:text-brand-primary transition-colors disabled:cursor-default disabled:hover:text-text-secondary"
          >
            <span>{t("hero.usageToday")}</span>
            <span className="num text-text-primary">
              {humanTokens(todayUsage.data.total_tokens)}
            </span>
            <span className="text-text-tertiary">{t("hero.usageTokens")}</span>
            {onJumpUsage && (
              <span className="text-text-tertiary" aria-hidden>→</span>
            )}
          </button>
        )}
      </div>

      {/* 家人 */}
      <SectionLabel>{t("hero.familyLabel")}</SectionLabel>
      {sorted.length === 0 ? (
        <div className="text-body text-text-secondary mb-5">
          {t("hero.familyEmpty")}
        </div>
      ) : (
        <div className="flex flex-wrap gap-2 mb-5">
          {sorted.map((p) => (
            <PersonChip
              key={p.id}
              person={p}
              onClick={onPersonClick ? () => onPersonClick(p) : undefined}
            />
          ))}
        </div>
      )}

      {/* 摄像头 */}
      <CameraSection
        scopeCameras={scopeCameras}
        streamingCams={streamingCams}
        benchCams={benchCams}
        maxStreamCams={maxStreamCams}
        miotHasCamera={miotHasCamera}
        channelByDid={channelByDid}
        onToggleCameras={onToggleCameras}
        onSetCameraSchedule={onSetCameraSchedule}
      />
    </section>
  );
}

interface CameraSectionProps {
  scopeCameras: ScopeCamera[];
  /** 上区:带实时流的相机(connected=true，≤4，按 did 稳定排序) */
  streamingCams: ScopeCamera[];
  /** 下区:无流横向列出的相机(未启用 / 定时暂停 / 超出上限) */
  benchCams: ScopeCamera[];
  /** 最多投喂数(后端 MAX_ENABLED_CAMERAS)，用于满额置灰下区「启用」 */
  maxStreamCams: number;
  miotHasCamera: boolean;
  channelByDid: Map<string, number>;
  onToggleCameras: (dids: string[], inUse: boolean) => void | Promise<void>;
  onSetCameraSchedule: (
    did: string,
    schedule: CameraSchedule,
  ) => void | Promise<void>;
}

function CameraSection({
  scopeCameras,
  streamingCams,
  benchCams,
  maxStreamCams,
  miotHasCamera,
  channelByDid,
  onToggleCameras,
  onSetCameraSchedule,
}: CameraSectionProps) {
  const { t } = useTranslation();
  const total = scopeCameras.length;
  const manualOnCount = scopeCameras.filter((c) => c.inUse).length;
  const activeCount = scopeCameras.filter((c) => c.effectiveInUse).length;
  const allOn = total > 0 && manualOnCount === total;
  const allOff = manualOnCount === 0;
  // 满额判断按后端 effective_in_use 计数:被定时暂停的相机不占实时投喂名额。
  const atCapacity = activeCount >= maxStreamCams;
  // 「全开」只能开「在线且未投喂」的——离线相机后端 toggle_camera 会整批拒绝
  // (offline_enable 校验),若把离线 did 也塞进批量 enable,会连带在线的一起失败。
  // 与下区单台开关「离线不可开」同口径。
  const enableableDids = scopeCameras
    .filter((c) => !c.inUse && c.isOnline)
    .map((c) => c.did);
  // bulkBusy 锁防"全开/全关"连点;singleBusyDids 跟踪单卡 in-flight,让住户切单卡 A
  // 时只 disable A 卡,B/C/D 仍可点。bulk 操作进行时仍 disable 所有(防交叠)。
  const [bulkBusy, setBulkBusy] = useState(false);
  const [singleBusyDids, setSingleBusyDids] = useState<Set<string>>(new Set());
  const [scheduleCam, setScheduleCam] = useState<ScopeCamera | null>(null);
  const [scheduleBusyDid, setScheduleBusyDid] = useState<string | null>(null);
  const runBulk = async (dids: string[], inUse: boolean) => {
    if (bulkBusy) return;
    setBulkBusy(true);
    try {
      await onToggleCameras(dids, inUse);
    } finally {
      setBulkBusy(false);
    }
  };
  const runSingle = async (did: string, inUse: boolean) => {
    if (bulkBusy || singleBusyDids.has(did)) return;
    setSingleBusyDids((s) => new Set(s).add(did));
    try {
      await onToggleCameras([did], inUse);
    } finally {
      setSingleBusyDids((s) => {
        const n = new Set(s);
        n.delete(did);
        return n;
      });
    }
  };
  const saveSchedule = async (did: string, schedule: CameraSchedule) => {
    if (scheduleBusyDid) return;
    setScheduleBusyDid(did);
    try {
      await onSetCameraSchedule(did, schedule);
      setScheduleCam(null);
    } finally {
      setScheduleBusyDid(null);
    }
  };

  return (
    <>
      <div className="flex items-baseline justify-between flex-wrap gap-2 mb-2">
        <div className="flex items-baseline gap-2">
          <SectionLabel>{t("hero.liveLabel")}</SectionLabel>
        </div>
        {total > 0 && (
          <div className="text-caption flex items-center gap-2 text-text-tertiary">
            <span className="num">
              {t("hero.perceivingCount", { n: activeCount })}
            </span>
            <button
              type="button"
              onClick={() => runBulk(enableableDids, true)}
              // 满额(activeCount≥上限)时置灰,跟下区单台启用开关口径一致——否则
              // >上限 的家庭点"全开"必被后端 toggle_camera 的上限校验整批拒绝。
              // 无「在线且未投喂」的相机时也置灰(全离线 / 已全开),避免空批 / 整批被拒。
              disabled={
                allOn || bulkBusy || atCapacity || enableableDids.length === 0
              }
              className="px-2 py-0.5 rounded-md bg-bg-primary border border-border hover:border-border-strong hover:text-text-primary disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {t("hero.allOn")}
            </button>
            <button
              type="button"
              onClick={() =>
                runBulk(
                  scopeCameras.filter((c) => c.inUse).map((c) => c.did),
                  false,
                )
              }
              disabled={allOff || bulkBusy}
              className="px-2 py-0.5 rounded-md bg-bg-primary border border-border hover:border-border-strong hover:text-text-primary disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {t("hero.allOff")}
            </button>
          </div>
        )}
      </div>
      {total === 0 ? (
        <div className="text-body rounded-lg bg-bg-primary border border-dashed border-border-strong text-text-secondary py-8 px-5 text-center">
          {miotHasCamera ? (
            <>
              <div className="text-warning mb-1">
                {t("hero.cameraOfflineTitle")}
              </div>
              <div>
                {t("hero.cameraOfflineHint")}
              </div>
            </>
          ) : (
            <>{t("hero.cameraEmpty")}</>
          )}
        </div>
      ) : (
        <>
          {/* 上区:最多 4 路「带实时流」。streamingCams 按 did 稳定排序——toggle 某路时
              其余卡 key+位置不变，React 复用其 iframe，不会连带把其它路的流断开重连。 */}
          {streamingCams.length > 0 ? (
            <div className="flex gap-3 overflow-x-auto snap-x snap-mandatory pb-2 -mx-1 px-1">
              {streamingCams.map((c) => (
                <CamCardWithToggle
                  key={c.did}
                  cam={c}
                  channel={channelByDid.get(c.did)}
                  bulkBusy={bulkBusy || singleBusyDids.has(c.did)}
                  onToggle={(v) => runSingle(c.did, v)}
                  onSchedule={() => setScheduleCam(c)}
                />
              ))}
            </div>
          ) : (
            <div className="text-body rounded-lg bg-bg-primary border border-dashed border-border-strong text-text-secondary py-6 px-5 text-center">
              {t("hero.noStreaming")}
            </div>
          )}
          {/* 下区:未投喂给 miloco 的相机(未启用 / 超出上限)。不拉流、不展示小窗，
              改成日志页风格的横条行:每行 摄像头信息 + 一个开关，开关直接控制是否投喂。 */}
          {benchCams.length > 0 && (
            <div className="mt-4">
              <SectionLabel>
                {atCapacity
                  ? t("hero.benchTitleFull", { n: maxStreamCams })
                  : t("hero.benchTitle")}
              </SectionLabel>
              <ul className="rounded-xl bg-bg-secondary border border-border divide-y divide-border overflow-hidden">
                {benchCams.map((c) => (
                  <BenchCamItem
                    key={c.did}
                    cam={c}
                    // 离线 + 未投喂 → 禁用(开不了);离线 + 已投喂 → 仍可点(允许关闭)。
                    // 满额时也只挡「开启未启用的」,已启用的随时可关。即:仅当
                    // 「当前未投喂 且 (离线 或 已满额)」时禁用,其余可点。
                    disabled={
                      bulkBusy ||
                      singleBusyDids.has(c.did) ||
                      (!c.inUse && (!c.isOnline || atCapacity))
                    }
                    onToggle={(v) => runSingle(c.did, v)}
                    onSchedule={() => setScheduleCam(c)}
                  />
                ))}
              </ul>
            </div>
          )}
          {scheduleCam && (
            <CameraScheduleDialog
              cam={scheduleCam}
              busy={scheduleBusyDid === scheduleCam.did}
              onClose={() => setScheduleCam(null)}
              onSave={(schedule) => saveSchedule(scheduleCam.did, schedule)}
            />
          )}
        </>
      )}
    </>
  );
}

/** 投喂开关。上区卡(浮在画面上)与下区横条行复用。on=把这一路投给 miloco。 */
function CamSwitch({
  inUse,
  name,
  disabled,
  onToggle,
}: {
  inUse: boolean;
  name: string;
  disabled: boolean;
  onToggle: (next: boolean) => void;
}) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      role="switch"
      aria-checked={inUse}
      aria-label={t(inUse ? "hero.toggleAriaInUse" : "hero.toggleAriaNotInUse", {
        name,
      })}
      title={inUse ? t("hero.toggleTitleInUse") : t("hero.toggleTitleNotInUse")}
      disabled={disabled}
      onClick={() => onToggle(!inUse)}
      className={`relative inline-flex h-[14px] w-[26px] shrink-0 rounded-full transition-colors shadow-sm focus-visible:ring-2 focus-visible:ring-brand-primary focus-visible:outline-none disabled:opacity-40 disabled:cursor-not-allowed ${
        inUse ? "bg-brand-primary" : "bg-black/60"
      }`}
    >
      <span
        className={`absolute top-0.5 left-0.5 inline-block h-2.5 w-2.5 rounded-full bg-white shadow-sm transition-transform ${
          inUse ? "translate-x-[12px]" : "translate-x-0"
        }`}
      />
    </button>
  );
}

interface CamCardProps {
  cam: ScopeCamera;
  /** PerceptionCamera 提供的真 channel；undefined = 还没拉到 / 多家庭场景无映射 */
  channel: number | undefined;
  /** 父级 bulk 操作（全开/全关）正在进行——单卡 Switch 也得 disable 防交叠 PUT */
  bulkBusy: boolean;
  onToggle: (next: boolean) => void;
  onSchedule: () => void;
}

// 上区卡只渲染「正在投喂 miloco（connected）」的相机——必然是活流，无需蒙层。
function CamCardWithToggle({
  cam,
  channel,
  bulkBusy,
  onToggle,
  onSchedule,
}: CamCardProps) {
  const { t } = useTranslation();
  return (
    <div className="snap-start shrink-0 w-[min(280px,85vw)]">
      <div className="relative">
        <LivePlayerPlaceholder
          cameraName={cam.name}
          roomName={cam.roomName}
          cameraDid={cam.did}
          channel={channel ?? 0}
        />
        <div className="absolute top-2 right-2 flex items-center gap-2">
          <button
            type="button"
            onClick={onSchedule}
            aria-label={t("hero.scheduleAria", { name: cam.name })}
            title={t("hero.scheduleTitle")}
            className={`inline-flex h-[26px] w-[26px] items-center justify-center rounded-md bg-black/55 text-white hover:bg-black/70 focus-visible:ring-2 focus-visible:ring-brand-primary focus-visible:outline-none ${
              cam.schedule.enabled ? "text-brand-primary" : ""
            }`}
          >
            <IconClock className="h-4 w-4" />
          </button>
          <CamSwitch
            inUse={cam.inUse}
            name={cam.name}
            disabled={bulkBusy}
            onToggle={onToggle}
          />
        </div>
      </div>
    </div>
  );
}

/** 下区横条行（日志页风格）：摄像头信息 + 投喂开关，无小窗。开关 on → 升入上区投喂。 */
function BenchCamItem({
  cam,
  disabled,
  onToggle,
  onSchedule,
}: {
  cam: ScopeCamera;
  disabled: boolean;
  onToggle: (next: boolean) => void;
  onSchedule: () => void;
}) {
  const { t } = useTranslation();
  return (
    <li className="px-4 py-3 flex items-center justify-between gap-3 hover:bg-bg-tertiary transition-colors">
      <div className="min-w-0">
        {/* 离线相机名字淡化,跟开关禁用呼应——离线就别让住户以为点一下能投喂。 */}
        <div
          className={`text-body truncate ${
            cam.isOnline ? "text-text-primary" : "text-text-tertiary"
          }`}
        >
          {cam.name}
        </div>
        {(!cam.isOnline || cam.roomName || cam.schedulePaused) && (
          <div className="text-caption text-text-tertiary truncate">
            {!cam.isOnline && (
              <span className="text-warning">{t("hero.benchOffline")}</span>
            )}
            {!cam.isOnline && cam.roomName ? " · " : ""}
            {cam.roomName}
            {cam.schedulePaused && (
              <>
                {(cam.roomName || !cam.isOnline) ? " · " : ""}
                <span className="text-brand-primary">
                  {t("hero.schedulePaused")}
                </span>
              </>
            )}
          </div>
        )}
      </div>
      <div className="flex items-center gap-3 shrink-0">
        <button
          type="button"
          onClick={onSchedule}
          aria-label={t("hero.scheduleAria", { name: cam.name })}
          title={t("hero.scheduleTitle")}
          className={`inline-flex h-[28px] w-[28px] items-center justify-center rounded-md border border-border bg-bg-primary text-text-tertiary hover:border-border-strong hover:text-text-primary focus-visible:ring-2 focus-visible:ring-brand-primary focus-visible:outline-none ${
            cam.schedule.enabled ? "text-brand-primary border-brand-primary/40" : ""
          }`}
        >
          <IconClock className="h-4 w-4" />
        </button>
        <CamSwitch
          inUse={cam.inUse}
          name={cam.name}
          disabled={disabled}
          onToggle={onToggle}
        />
      </div>
    </li>
  );
}

function CameraScheduleDialog({
  cam,
  busy,
  onClose,
  onSave,
}: {
  cam: ScopeCamera;
  busy: boolean;
  onClose: () => void;
  onSave: (schedule: CameraSchedule) => void | Promise<void>;
}) {
  const { t } = useTranslation();
  const [enabled, setEnabled] = useState(cam.schedule.enabled);
  const [windows, setWindows] = useState(
    cam.schedule.windows.length > 0
      ? cam.schedule.windows.map((w) => ({ ...w }))
      : [{ start: "08:00", end: "20:00" }],
  );
  const [error, setError] = useState<string | null>(null);

  const updateWindow = (
    index: number,
    key: "start" | "end",
    value: string,
  ) => {
    setWindows((items) =>
      items.map((item, i) => (i === index ? { ...item, [key]: value } : item)),
    );
  };
  const removeWindow = (index: number) => {
    setWindows((items) => items.filter((_, i) => i !== index));
  };
  const submit = async () => {
    const nextWindows = windows;
    if (enabled && nextWindows.length === 0) {
      setError(t("hero.scheduleNeedWindow"));
      return;
    }
    if (enabled && nextWindows.some((w) => !w.start || !w.end)) {
      setError(t("hero.scheduleInvalid"));
      return;
    }
    setError(null);
    await onSave({
      enabled: enabled && nextWindows.length > 0,
      windows: nextWindows,
    });
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 px-4">
      <div className="w-full max-w-md rounded-xl border border-border bg-bg-secondary shadow-xl">
        <div className="flex items-center justify-between gap-3 border-b border-border px-5 py-4">
          <div className="min-w-0">
            <h3 className="text-subtitle text-text-primary truncate">
              {t("hero.scheduleDialogTitle")}
            </h3>
            <div className="text-caption text-text-tertiary truncate">
              {cam.name}
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={t("common.close")}
            className="inline-flex h-8 w-8 items-center justify-center rounded-md text-text-tertiary hover:bg-bg-tertiary hover:text-text-primary focus-visible:ring-2 focus-visible:ring-brand-primary focus-visible:outline-none"
          >
            <IconX className="h-4 w-4" />
          </button>
        </div>
        <div className="space-y-4 px-5 py-4">
          <label className="flex items-center justify-between gap-4">
            <span className="text-body text-text-primary">
              {t("hero.scheduleEnabled")}
            </span>
            <button
              type="button"
              role="switch"
              aria-checked={enabled}
              onClick={() => setEnabled((v) => !v)}
              className={`relative inline-flex h-[18px] w-[34px] shrink-0 rounded-full transition-colors focus-visible:ring-2 focus-visible:ring-brand-primary focus-visible:outline-none ${
                enabled ? "bg-brand-primary" : "bg-black/35"
              }`}
            >
              <span
                className={`absolute top-0.5 left-0.5 inline-block h-3.5 w-3.5 rounded-full bg-white shadow-sm transition-transform ${
                  enabled ? "translate-x-4" : "translate-x-0"
                }`}
              />
            </button>
          </label>
          <div className="space-y-2">
            {windows.map((window, index) => (
              <div
                key={`${index}-${window.start}-${window.end}`}
                className="grid grid-cols-[1fr_auto_1fr_auto] items-center gap-2"
              >
                <input
                  type="time"
                  value={window.start}
                  disabled={!enabled}
                  onChange={(e) =>
                    updateWindow(index, "start", e.currentTarget.value)
                  }
                  className="min-w-0 rounded-md border border-border bg-bg-primary px-3 py-2 text-body text-text-primary disabled:opacity-50"
                />
                <span className="text-text-tertiary">-</span>
                <input
                  type="time"
                  value={window.end}
                  disabled={!enabled}
                  onChange={(e) =>
                    updateWindow(index, "end", e.currentTarget.value)
                  }
                  className="min-w-0 rounded-md border border-border bg-bg-primary px-3 py-2 text-body text-text-primary disabled:opacity-50"
                />
                <button
                  type="button"
                  onClick={() => removeWindow(index)}
                  disabled={!enabled || windows.length <= 1}
                  aria-label={t("hero.scheduleRemoveWindow")}
                  title={t("hero.scheduleRemoveWindow")}
                  className="inline-flex h-9 w-9 items-center justify-center rounded-md border border-border text-text-tertiary hover:border-border-strong hover:text-text-primary disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <IconTrash className="h-4 w-4" />
                </button>
              </div>
            ))}
            <button
              type="button"
              onClick={() =>
                setWindows((items) => [
                  ...items,
                  { start: "08:00", end: "20:00" },
                ])
              }
              disabled={!enabled}
              className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-caption text-text-secondary hover:border-border-strong hover:text-text-primary disabled:cursor-not-allowed disabled:opacity-40"
            >
              <IconPlus className="h-4 w-4" />
              {t("hero.scheduleAddWindow")}
            </button>
          </div>
          {cam.schedulePaused && (
            <div className="text-caption text-brand-primary">
              {t("hero.schedulePausedDetail", {
                time: formatScheduleTime(cam.nextScheduleChangeAt),
              })}
            </div>
          )}
          {enabled && !cam.inUse && (
            <div className="text-caption text-text-tertiary">
              {t("hero.scheduleMasterOffHint")}
            </div>
          )}
          {error && <div className="text-caption text-warning">{error}</div>}
        </div>
        <div className="flex items-center justify-end gap-2 border-t border-border px-5 py-4">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-border px-3 py-2 text-body text-text-secondary hover:border-border-strong hover:text-text-primary"
          >
            {t("common.cancel")}
          </button>
          <button
            type="button"
            onClick={submit}
            disabled={busy}
            className="rounded-md bg-brand-primary px-3 py-2 text-body text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? t("common.saving") : t("common.save")}
          </button>
        </div>
      </div>
    </div>
  );
}

function formatScheduleTime(value: string | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString(undefined, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** 卡片内的小节标签——caption 字号 + tertiary 色,
 *  HeroNow 内复用。 */
function SectionLabel({ children }: { children: ReactNode }) {
  return (
    <div className="text-caption text-text-tertiary mb-2">
      {children}
    </div>
  );
}
