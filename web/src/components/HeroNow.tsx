/**
 * 「家里此刻」Hero 区（v3 Mi Console 视觉）
 *
 * 整面板视觉重量最高的卡片：家人 chips + 摄像头实时画面 +
 * 感知设备列表（v2：per-camera × per-modality 矩阵）。
 *
 * 上方"实时画面"区只展示 connected=true 的相机，卡片上不再有开关（v2 改动）。
 * 下方"感知设备列表"统一管理所有米家摄像头的视频 / 音频感知开关。
 */

import type {
  Person,
  ScopeCamera,
  UsageStats,
} from "@/lib/types";
import { PersonChip } from "./PersonChip";
import { LivePlayerPlaceholder } from "./LivePlayerPlaceholder";
import { PerceptionDeviceTable } from "./PerceptionDeviceTable";
import { getUsageStats } from "@/api";
import { useAsync } from "@/hooks/useAsync";
import { humanTokens } from "@/lib/formatTokens";
import { useMemo, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

interface Props {
  persons: Person[];
  /** 米家全集（含被禁用 / 离线），用于渲染所有摄像头卡片 + Switch */
  scopeCameras: ScopeCamera[];
  /** miot 上是否有 camera 类设备——区分两种空态 */
  miotHasCamera: boolean;
  /** 后端 MAX_ENABLED_CAMERAS,经 /api/miot/status 下发。下沉到 PerceptionDeviceTable 用作 atCapacity 检测。 */
  maxStreamCams: number;
  /** 占位 prop,v2 后表格自己管自己的 toggle,这里保留签名只供上层触发 reload 用 */
  onToggleCameras?: () => void;
  /** undefined → chip 渲染成 div(无 hover/点击反馈),概览页用 */
  onPersonClick?: (p: Person) => void;
  /** 点击"今日用量"小卡片跳到用量 tab。 */
  onJumpUsage?: () => void;
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
  scopeCameras,
  miotHasCamera,
  maxStreamCams,
  onToggleCameras,
  onPersonClick,
  onJumpUsage,
}: Props) {
  const { t } = useTranslation();
  const sorted = sortPersons(persons);
  // 实时画面：任一模态开启即显示预览。全拆后按通道返多行，需按 did 去重。
  const streamingCams = useMemo(() => {
    const byDid = (a: ScopeCamera, b: ScopeCamera) =>
      a.did < b.did ? -1 : a.did > b.did ? 1 : 0;
    const seen = new Set<string>();
    return [...scopeCameras]
      .filter((c) => (c.videoEnabled || c.audioEnabled) && !seen.has(c.did) && seen.add(c.did))
      .sort(byDid);
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

      {/* 摄像头实时画面区（v2:卡上无开关,只展示 connected=true 的相机） */}
      <SectionLabel>{t("hero.liveLabel")}</SectionLabel>
      <CameraSection
        streamingCams={streamingCams}
        miotHasCamera={miotHasCamera}
      />
      {/* 感知设备列表（v2）：always-all 米家摄像头 + per-modality 开关 + 批量按钮 */}
      <PerceptionDeviceTable
        cameras={scopeCameras}
        maxEnabledCameras={maxStreamCams}
        onChanged={() => onToggleCameras?.()}
      />
    </section>
  );
}

interface CameraSectionProps {
  /** 上区:带实时流的相机(video 解码真连上,connected=true)。按 did 稳定排序。 */
  streamingCams: ScopeCamera[];
  miotHasCamera: boolean;
}

/** 上区实时画面区：只展示 connected=true 的相机，卡片右上角的开关已移除（v2 表格统一管）。 */
function CameraSection({
  streamingCams,
  miotHasCamera,
}: CameraSectionProps) {
  const { t } = useTranslation();

  if (streamingCams.length === 0) {
    if (miotHasCamera) {
      return (
        <div className="text-body rounded-lg bg-bg-primary border border-dashed border-border-strong text-text-secondary py-6 px-5 text-center">
          <div className="text-warning mb-1">
            {t("hero.cameraOfflineTitle")}
          </div>
          <div>{t("hero.cameraOfflineHint")}</div>
        </div>
      );
    }
    return (
      <div className="text-body rounded-lg bg-bg-primary border border-dashed border-border-strong text-text-secondary py-6 px-5 text-center">
        {t("hero.cameraEmpty")}
      </div>
    );
  }
  return (
    <div className="flex gap-3 overflow-x-auto snap-x snap-mandatory pb-2 -mx-1 px-1">
      {streamingCams.map((c) => (
        <CamCard key={`${c.did}|${c.channel}`} cam={c} channel={c.channel} />
      ))}
    </div>
  );
}

interface CamCardProps {
  cam: ScopeCamera;
  /** 通道号（ScopeCamera 自带）；用于 LivePlayer 取流 */
  channel: number;
}

/** 上区卡只渲染「正在投喂 miloco（connected）」的相机——必然是活流，无需蒙层。
 *  v2 移除了 CamSwitch（开关统一搬到 PerceptionDeviceTable）。 */
function CamCard({ cam, channel }: CamCardProps) {
  return (
    <div className="snap-start shrink-0 w-[min(280px,85vw)]">
      <div className="relative">
        <LivePlayerPlaceholder
          cameraName={cam.name}
          roomName={cam.roomName}
          cameraDid={cam.did}
          channel={channel ?? 0}
        />
      </div>
    </div>
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