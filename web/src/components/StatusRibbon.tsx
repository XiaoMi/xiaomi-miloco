/**
 * 状态条（v3 Mi Console 视觉）
 *
 * 视觉：5px 实心点 + 3px 半透明光环 + 文字 + 可选 mono meta + 可选 CTA。
 * 取代 v2 的"大色块 chip"——dev tool 风格更克制，不抢主区注意力。
 *
 * Item 1: 看家（perception running / paused / 异常）
 * Item 2: 米家（已连 / 未连）
 */

import { type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { HomeStatus } from "@/lib/types";
import type { ServiceStatus } from "@/hooks/useServiceStatus";

type Tone = "ok" | "info" | "warn" | "danger" | "brand";

function dotClass(tone: Tone) {
  switch (tone) {
    case "ok":
      return "bg-success";
    case "info":
      return "bg-info";
    case "warn":
      return "bg-warning";
    case "danger":
      return "bg-error";
    case "brand":
      return "bg-brand-primary";
  }
}

function ringStyle(tone: Tone): string {
  // box-shadow 制造 5px dot 外层 3px 半透明光环——不用单独的 element
  switch (tone) {
    case "ok":
      return "var(--color-success-bg)";
    case "info":
      return "var(--color-info-bg)";
    case "warn":
      return "var(--color-warning-bg)";
    case "danger":
      return "var(--color-error-bg)";
    case "brand":
      return "var(--color-brand-soft)";
  }
}

function StatusDot({ tone }: { tone: Tone }) {
  return (
    <span
      aria-hidden
      className={`shrink-0 rounded-full ${dotClass(tone)}`}
      style={{
        width: 5,
        height: 5,
        boxShadow: `0 0 0 3px ${ringStyle(tone)}`,
      }}
    />
  );
}

function truncate(s: string, max: number): string {
  // 按 Unicode code point 数,不按 UTF-16 code unit。emoji / 罕见汉字（surrogate
  // pair）单字符占 2 code unit,直接 slice 可能切到 surrogate 中间产乱码或孤立
  // 替换字符。`[...s]` 走 string iterator 自动按 code point 拆。
  const chars = [...s];
  if (chars.length <= max) return s;
  return `${chars.slice(0, max - 1).join("")}…`;
}

// 互斥：cta 与 onClick 不能同时出现 —— onClick 让整个 Item 变 button，cta 自己又是
// button，同时存在会渲染出 button-in-button，触发 hydration warning + 难调试的
// click bubbling。用 union 类型在编译期挡住，调用方会被 ts 强制选其中一个。
type ItemProps = {
  tone: Tone;
  label: ReactNode;
  meta?: ReactNode;
} & (
  | { cta?: { text: string; onClick: () => void; disabled?: boolean }; onClick?: never }
  | { cta?: never; onClick: () => void }
);

function StatusItem({ tone, label, meta, cta, onClick }: ItemProps) {
  const inner = (
    <>
      <StatusDot tone={tone} />
      <span className="text-text-primary">{label}</span>
      {meta && <span className="text-text-tertiary">{meta}</span>}
      {cta && (
        <button
          type="button"
          disabled={cta.disabled}
          onClick={(e) => {
            e.stopPropagation();
            cta.onClick();
          }}
          className="ml-1 text-brand-primary hover:underline underline-offset-2 disabled:opacity-60 disabled:cursor-not-allowed disabled:no-underline"
        >
          {cta.text}
        </button>
      )}
    </>
  );

  const cls = "inline-flex items-center gap-2 text-body text-text-secondary";

  if (onClick) {
    return (
      <button
        type="button"
        onClick={onClick}
        className={`${cls} cursor-pointer text-left`}
      >
        {inner}
      </button>
    );
  }
  return (
    <div className={cls}>
      {inner}
    </div>
  );
}

interface Props {
  status: HomeStatus;
  /** 所有摄像头均已关闭（inUse=false）——感知在跑但无画面可分析 */
  allCamerasOff?: boolean;
  onConnectMiot: () => void;
  onWakeUp: () => void;
  /** 点「米家已连接」跳设备页。slim（独立 App）没有设备页 → 不传 = 该状态项不可点。 */
  onJumpDevices?: () => void;
  /** running=true 但 ready=false（如模型缺失）时点「重启引擎」：stop + start */
  onRestartEngine: () => void;
  /**
   * 后端服务（进程）状态。可不传（老调用方 / 单测）：不传就不显这一项。
   * 「看家」说的是感知引擎跑不跑，这一项说的是**后端进程在不在** —— 菜单栏里
   * 点了「停止服务」之后，感知状态那条会一直是老数据，只有这条能说清发生了什么。
   */
  service?: ServiceStatus;
}

export function StatusRibbon({
  status,
  allCamerasOff = false,
  onConnectMiot,
  onWakeUp,
  onJumpDevices,
  onRestartEngine,
  service,
}: Props) {
  const { t } = useTranslation();
  // ── Item 1:感知状态 3 态 ─────────────────────────
  // running=true && ready=true   → allCamerasOff ? 黄,待机中 : 绿,在看家
  // running=true && ready=false  → 黄,引擎跑着但缺模型/异常,可重启
  // running=false                → 蓝,主动暂停态,唤醒它
  // (backend 当前只有 stop/start 两态,没有 pausedUntil 倒计时,该字段已删)
  const watchItem = status.perception.running ? (
    status.perception.ready ? (
      <StatusItem
        tone={allCamerasOff ? "warn" : "ok"}
        label={allCamerasOff ? t("hero.watchItemStandby") : t("hero.watchItemWatching")}
      />
    ) : (
      <StatusItem
        tone="warn"
        // engineMessage 长度无 cap（backend 可能塞 traceback / i18n 长文）—— 截
        // 60 字符防 ribbon 撑爆 / 把 cta 挤下一行；完整 message 放在 title 属性
        // 里 hover 看。
        label={
          <span
            title={status.perception.engineMessage ?? undefined}
          >
            {t("hero.watchItemNotReady")}
            {status.perception.engineMessage
              ? ` · ${truncate(status.perception.engineMessage, 60)}`
              : ""}
          </span>
        }
        cta={{ text: t("hero.watchCtaRestart"), onClick: onRestartEngine }}
      />
    )
  ) : (
    <StatusItem
      tone="info"
      label={t("hero.watchItemResting")}
      cta={{ text: t("hero.watchCtaWake"), onClick: onWakeUp }}
    />
  );

  // ── Item 2：米家连接 ─────────────────────────
  const miotItem = status.miot.bound ? (
    <StatusItem
      tone="ok"
      label={
        <>
          {t("hero.miotConnected")}
          {status.miot.accountName ? ` · ${status.miot.accountName}` : ""}
        </>
      }
      meta={
        <span className="num">
          {t("hero.miotDevicesCount", { n: status.miot.devicesCount })}
        </span>
      }
      {...(onJumpDevices ? { onClick: onJumpDevices } : {})}
    />
  ) : (
    <StatusItem
      tone="warn"
      label={t("hero.miotNotConnected")}
      cta={{ text: t("hero.miotCtaConnect"), onClick: onConnectMiot }}
    />
  );

  // ── Item 3：后端服务（进程）─────────────────
  // up       → 绿，服务运行中
  // down     → 红，服务已停止（附「重试」入口；恢复后页面会自动刷新）
  // checking → 蓝，首帧探测还没回来（不让状态条先闪一下「已停止」）
  const serviceItem = !service ? null : (
    // data-miloco：给自动化/实机巡检一个稳定的锚点（launcher 的 UI 探针靠它读状态条，
    // 不靠"第几个 div"这种一改布局就失效的选择器）。
    <span data-miloco="service-status" className="inline-flex">
      {service.state === "up" ? (
        <StatusItem tone="ok" label={t("hero.serviceRunning")} />
      ) : service.state === "down" ? (
        <StatusItem
          tone="danger"
          label={t("hero.serviceStopped")}
          cta={{ text: t("hero.serviceCtaRetry"), onClick: service.recheck }}
        />
      ) : (
        <StatusItem tone="info" label={t("hero.serviceChecking")} />
      )}
    </span>
  );

  return (
    <div
      className="flex items-center gap-x-6 gap-y-2 px-5 md:px-8 py-2.5 border-b border-border bg-bg-primary flex-wrap"
      style={{ minHeight: 44 }}
    >
      {watchItem}
      {miotItem}
      {serviceItem}
    </div>
  );
}
