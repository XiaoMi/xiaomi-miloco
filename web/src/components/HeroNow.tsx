/**
 * 「家里此刻」Hero 区（v3 Mi Console 视觉）
 *
 * 整面板视觉重量最高的卡片：家人 chips + 摄像头实时画面 +
 * 全开/全关批量切换 + inUse 单卡 toggle。
 */

import type {
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
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "./Toast";
import { IconAlert } from "@/lib/icons";
import {
  cameraStatus,
  CAMERA_GRACE_MS,
  switchBlockedReasonKey,
  type CameraStatus,
} from "@/lib/cameraStatus";

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
  // 其余相机(未投喂:未启用 / 启用中未连上 / 超出上限)进下区「无流」横向列表。
  const { streamingCams, benchCams } = useMemo(() => {
    const byDid = (a: ScopeCamera, b: ScopeCamera) =>
      a.did < b.did ? -1 : a.did > b.did ? 1 : 0;
    const sorted = [...scopeCameras].sort(byDid);
    // 不再前端截断:connected 集天然受后端 MAX_ENABLED_CAMERAS 约束(感知接入层按 did
    // 升序截断到上限、只连前 N 路；主动 enable 超限也被 toggle_camera 挡下)，展示集即真实投喂集。
    // 上区 = 派生态 perceiving(云端在线 + 已启用 + 已订阅出流),用 cameraStatus 单一真相源,
    // 与下区标签 / 开关门控同口径。perceiving 判定不涉及 grace,now 传 0 即可。
    const streaming = sorted.filter(
      (c) => cameraStatus(c, { now: 0 }).kind === "perceiving",
    );
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
      />
    </section>
  );
}

interface CameraSectionProps {
  scopeCameras: ScopeCamera[];
  /** 上区:带实时流的相机(inUse=true，≤4，按 did 稳定排序) */
  streamingCams: ScopeCamera[];
  /** 下区:无流横向列出的相机(未启用 / 超出上限) */
  benchCams: ScopeCamera[];
  /** 最多投喂数(后端 MAX_ENABLED_CAMERAS)，用于满额置灰下区「启用」 */
  maxStreamCams: number;
  miotHasCamera: boolean;
  channelByDid: Map<string, number>;
  onToggleCameras: (dids: string[], inUse: boolean) => void | Promise<void>;
}

function CameraSection({
  scopeCameras,
  streamingCams,
  benchCams,
  maxStreamCams,
  miotHasCamera,
  channelByDid,
  onToggleCameras,
}: CameraSectionProps) {
  const { t } = useTranslation();
  const total = scopeCameras.length;
  const activeCount = scopeCameras.filter((c) => c.inUse).length;
  const allOn = total > 0 && activeCount === total;
  const allOff = activeCount === 0;
  // 满额判断按 inUse 计数(与后端 toggle_camera 上限校验同口径):已启用的相机即便
  // 掉线仍保留 inUse、占名额(允许态不被强制改),要腾名额得显式关掉它。
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
  // 每台的启用时刻:给下区「已启用·未出流」的 grace 窗口用(刚点开的几秒算接入中,
  // 别立刻甩锅跨 LAN);关闭时清掉。
  const [enabledAt, setEnabledAt] = useState<Map<string, number>>(new Map());
  // now 只驱动下区诊断文案的 grace 判定(perceiving / 上下区划分不看它)。列表 poll
  // 之外,再给「grace 内的相机」按到点时刻定一次时,让文案准时从「接入中」切到诊断。
  const [now, setNow] = useState(() => Date.now());
  const markEnabled = (dids: string[], inUse: boolean) => {
    setEnabledAt((m) => {
      const n = new Map(m);
      const ts = Date.now();
      for (const d of dids) {
        if (inUse) n.set(d, ts);
        else n.delete(d);
      }
      return n;
    });
  };
  const runBulk = async (dids: string[], inUse: boolean) => {
    if (bulkBusy) return;
    setBulkBusy(true);
    markEnabled(dids, inUse);
    try {
      await onToggleCameras(dids, inUse);
    } finally {
      setBulkBusy(false);
    }
  };
  const runSingle = async (did: string, inUse: boolean) => {
    if (bulkBusy || singleBusyDids.has(did)) return;
    setSingleBusyDids((s) => new Set(s).add(did));
    markEnabled([did], inUse);
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
  // grace 内相机到点后触发一次重渲染,让诊断文案从「接入中」切到跨 LAN / 未开 LAN 判定,
  // 不必等下一次列表 poll。只给最近一个到点时刻定时;now 纳入依赖——setNow 触发后 effect
  // 重跑、为下一台(交错启用、到点更晚者)重排,否则较晚者会永远停在「接入中」文案。
  useEffect(() => {
    const deadlines = benchCams
      .filter((c) => c.inUse && !c.connected)
      .map((c) => enabledAt.get(c.did))
      .filter((ts): ts is number => ts !== undefined)
      .map((ts) => ts + CAMERA_GRACE_MS - Date.now())
      .filter((ms) => ms > 0);
    if (deadlines.length === 0) return;
    const timer = setTimeout(
      () => setNow(Date.now()),
      Math.min(...deadlines) + 50,
    );
    return () => clearTimeout(timer);
  }, [benchCams, enabledAt, now]);

  return (
    <>
      <div className="flex items-baseline justify-between flex-wrap gap-2 mb-2">
        <div className="flex items-baseline gap-2">
          <SectionLabel>{t("hero.liveLabel")}</SectionLabel>
        </div>
        {total > 0 && (
          <div className="text-caption flex items-center gap-2 text-text-tertiary">
            <span className="num">
              {/* 「在感知」计数与上区 live 卡同口径:数 perceiving(streamingCams),不数
                  inUse 意图——否则跨 LAN 相机被启用后 inUse 虚增,顶部说「N 个在感知」而
                  上区空、下区「未出流」,三处自相矛盾。 */}
              {t("hero.perceivingCount", { n: streamingCams.length })}
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
                {benchCams.map((c) => {
                  const status = cameraStatus(c, {
                    now,
                    enabledAt: enabledAt.get(c.did),
                  });
                  // 「不可开」理由(仅未启用时):离线 / 满额。bulk/single in-flight 走 busy
                  // (瞬态真禁),不在此列。逻辑抽到 switchBlockedReasonKey 便于单测。
                  const blockedReasonKey = switchBlockedReasonKey({
                    inUse: c.inUse,
                    canEnable: status.canEnable,
                    atCapacity,
                  });
                  return (
                    <BenchCamItem
                      key={c.did}
                      cam={c}
                      status={status}
                      busy={bulkBusy || singleBusyDids.has(c.did)}
                      blockedReasonKey={blockedReasonKey}
                      onToggle={(v) => runSingle(c.did, v)}
                    />
                  );
                })}
              </ul>
            </div>
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
  busy,
  blockedReasonKey,
  onToggle,
}: {
  inUse: boolean;
  name: string;
  /** 瞬态忙（bulk / single 操作进行中）：真禁、忽略点击、不提示。 */
  busy: boolean;
  /** 语义不可开（离线 / 满额，仅未启用时）：置灰但仍可点 → toast 理由；已启用为空。 */
  blockedReasonKey?: string;
  onToggle: (next: boolean) => void;
}) {
  const { t } = useTranslation();
  const blocked = !!blockedReasonKey;
  const dim = busy || blocked;
  // 桌面 hover 点缀:语义不可开时在鼠标入场处弹原因气泡,与点击 toast 互补——hover 即时看
  // 原因(悬停就有),点击给明确反馈 + 触屏兜底。fixed 定位锚定入场点(只 onMouseEnter 记一次、
  // 不跟随光标,省逐像素重渲染),不被下区 ul 的 overflow-hidden 裁掉;触屏无 hover,只走 toast。
  const [tip, setTip] = useState<{ x: number; y: number } | null>(null);
  return (
    <>
      <button
        type="button"
        role="switch"
        aria-checked={inUse}
        aria-disabled={dim}
        aria-label={t(inUse ? "hero.toggleAriaInUse" : "hero.toggleAriaNotInUse", {
          name,
        })}
        // 语义不可开时把原生 title 让位给自定义气泡(避免双 tooltip);其余保留原提示。
        title={
          blocked
            ? undefined
            : t(inUse ? "hero.toggleTitleInUse" : "hero.toggleTitleNotInUse")
        }
        // native disabled 只给瞬态忙(吞点击可接受);语义不可开不能用 native disabled——
        // 否则 click 被吞、触屏弹不出提示。改成可点,点了走 toast 说明为何不可开。
        disabled={busy}
        onClick={() => {
          if (busy) return;
          if (blocked) {
            toast(t(blockedReasonKey), "warn");
            return;
          }
          onToggle(!inUse);
        }}
        onMouseEnter={
          blocked ? (e) => setTip({ x: e.clientX, y: e.clientY }) : undefined
        }
        onMouseLeave={() => setTip(null)}
        className={`relative inline-flex h-[14px] w-[26px] shrink-0 rounded-full transition-colors shadow-sm focus-visible:ring-2 focus-visible:ring-brand-primary focus-visible:outline-none ${
          dim ? "opacity-40 cursor-not-allowed" : ""
        } ${inUse ? "bg-brand-primary" : "bg-black/60"}`}
      >
        <span
          className={`absolute top-0.5 left-0.5 inline-block h-2.5 w-2.5 rounded-full bg-white shadow-sm transition-transform ${
            inUse ? "translate-x-[12px]" : "translate-x-0"
          }`}
        />
      </button>
      {blocked && tip && (
        <div
          role="tooltip"
          className="fixed z-[90] w-56 max-w-[70vw] -translate-x-1/2 -translate-y-full rounded-lg border border-warning bg-warning-bg text-warning text-caption px-2.5 py-1.5 shadow-md pointer-events-none"
          style={{ left: tip.x, top: tip.y - 10 }}
        >
          {t(blockedReasonKey)}
        </div>
      )}
    </>
  );
}

interface CamCardProps {
  cam: ScopeCamera;
  /** PerceptionCamera 提供的真 channel；undefined = 还没拉到 / 多家庭场景无映射 */
  channel: number | undefined;
  /** 父级 bulk 操作（全开/全关）正在进行——单卡 Switch 也得 disable 防交叠 PUT */
  bulkBusy: boolean;
  onToggle: (next: boolean) => void;
}

// 上区卡只渲染「正在投喂 miloco（connected）」的相机——必然是活流，无需蒙层。
function CamCardWithToggle({ cam, channel, bulkBusy, onToggle }: CamCardProps) {
  return (
    <div className="snap-start shrink-0 w-[min(280px,85vw)]">
      <div className="relative">
        <LivePlayerPlaceholder
          cameraName={cam.name}
          roomName={cam.roomName}
          cameraDid={cam.did}
          channel={channel ?? 0}
        />
        <div className="absolute top-2 right-2">
          <CamSwitch
            inUse={cam.inUse}
            name={cam.name}
            busy={bulkBusy}
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
  status,
  busy,
  blockedReasonKey,
  onToggle,
}: {
  cam: ScopeCamera;
  status: CameraStatus;
  busy: boolean;
  blockedReasonKey?: string;
  onToggle: (next: boolean) => void;
}) {
  const { t } = useTranslation();
  const offline = status.kind === "offline";
  // 状态词按严重度染色:error 红(离线) / warning 黄(未出流异常) / muted 灰(正常态·接入中)。
  const toneClass =
    status.tone === "error"
      ? "text-error"
      : status.tone === "warning"
        ? "text-warning"
        : "text-text-secondary";
  return (
    <li className="px-4 py-3 flex flex-col gap-2 hover:bg-bg-tertiary transition-colors">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          {/* 离线相机名字淡化,跟开关禁用呼应——离线就别让住户以为点一下能投喂。 */}
          <div
            className={`text-body truncate ${
              offline ? "text-text-tertiary" : "text-text-primary"
            }`}
          >
            {cam.name}
          </div>
          {(status.labelKey || cam.roomName) && (
            <div className="text-caption text-text-tertiary truncate mt-0.5">
              {status.labelKey && (
                <span className={toneClass}>
                  {t(status.labelKey)}
                  {/* 接入中:主标签后接内联转圈,让住户感知系统正在尝试拉流。 */}
                  {status.connecting && (
                    <span className="ml-1 inline-block w-2.5 h-2.5 align-[-1px] border-2 border-border border-t-text-tertiary rounded-full animate-spin" />
                  )}
                </span>
              )}
              {status.labelKey && cam.roomName ? " · " : ""}
              {cam.roomName}
            </div>
          )}
        </div>
        <CamSwitch
          inUse={cam.inUse}
          name={cam.name}
          busy={busy}
          blockedReasonKey={blockedReasonKey}
          onToggle={onToggle}
        />
      </div>
      {/* 「未出流」的诊断说明(为什么没画面 + 怎么办)是「当前状态的解释」,与设备名 / 状态
          分层,单独成一个软底提示框;接入中不出框(状态词 + 转圈已够)。 */}
      {status.diagKey && (
        <div className="flex items-start gap-2 rounded-lg bg-warning-bg border border-warning/30 text-warning text-caption px-3 py-2">
          <IconAlert width={14} height={14} className="shrink-0 mt-0.5" />
          <span>{t(status.diagKey)}</span>
        </div>
      )}
    </li>
  );
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
