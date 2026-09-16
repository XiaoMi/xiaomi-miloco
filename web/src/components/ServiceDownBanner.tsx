/**
 * 「后端服务已停止」横幅。
 *
 * 菜单栏点「停止服务」之后，内置网页还开着但后端已经没了：页面上所有数据都是老进程
 * 留下的快照，任何操作都会失败。这时候必须有一条明确的话说清「是服务停了」，否则
 * 住户只会以为 App 坏了（或者更糟：以为家里的状态就是这样）。
 *
 * 恢复不用住户操作 —— useServiceStatus 每 2s 探一次，服务回来即自动刷新整页；
 * 「立即重试」只是给想马上确认的人一个手动入口。
 */

import { useTranslation } from "react-i18next";

import type { ServiceStatus } from "@/hooks/useServiceStatus";

export function ServiceDownBanner({
  service,
  pollSeconds = 2,
}: {
  service: ServiceStatus;
  /** 与 useServiceStatus 的 intervalMs 对齐，用于文案里的「每 N 秒」。 */
  pollSeconds?: number;
}) {
  const { t } = useTranslation();
  if (service.state !== "down") return null;

  return (
    <div
      role="status"
      aria-live="polite"
      data-miloco="service-down-banner"
      className="w-full px-4 py-2 flex items-center justify-between gap-3 shrink-0 bg-error-bg text-error border-b border-error"
    >
      <div className="text-caption flex items-baseline gap-2 flex-wrap">
        <span className="font-semibold">{t("app.serviceDown")}</span>
        <span>{t("app.serviceDownHint", { seconds: pollSeconds })}</span>
      </div>
      <button
        type="button"
        onClick={service.recheck}
        className="text-caption px-3 py-1 rounded border border-current hover:opacity-80 shrink-0"
      >
        {t("app.serviceRetry")}
      </button>
    </div>
  );
}
