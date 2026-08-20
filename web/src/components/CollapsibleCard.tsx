/**
 * 可收起的分区卡。「模型配置」原本自己实现了一套，「Token 用量」「性能监测」没有——
 * 三张卡并排在同一页却只有一张能收，是不一致。抽出来共用，顺便让展开/收起的文案、
 * 焦点行为、无障碍属性只有一份。
 *
 * 收起状态**不持久化**，与原「模型配置」的行为保持一致（刷新后回到展开）。收起是
 * 「我现在不想看这块」的临时动作，不是长期偏好；真要记住，三张卡应该一起改。
 *
 * 标题整行可点。收起时可以通过 `summary` 在标题旁留一行摘要，免得收起后完全看不出
 * 里面是什么状态。
 */

import type { ReactNode } from "react";
import { useId, useState } from "react";
import { useTranslation } from "react-i18next";

export function CollapsibleCard({
  title,
  summary,
  toolbar,
  children,
  defaultCollapsed = false,
  busy,
}: {
  title: string;
  /** 仅在收起时显示，放一行「里面现在是什么状态」。 */
  summary?: ReactNode;
  /** 展开时紧跟标题下方的一行控件（周期、刷新等）。收起时一并隐藏。 */
  toolbar?: ReactNode;
  children: ReactNode;
  defaultCollapsed?: boolean;
  /** 透传 aria-busy，供正在重取数据的卡使用。 */
  busy?: boolean;
}) {
  const { t } = useTranslation();
  const [collapsed, setCollapsed] = useState(defaultCollapsed);
  const bodyId = useId();

  return (
    <section
      className="rounded-xl bg-bg-secondary border border-border shadow-sm p-5 md:p-6"
      aria-busy={busy}
    >
      <button
        type="button"
        onClick={() => setCollapsed((c) => !c)}
        className="w-full flex items-center justify-between gap-3 text-left"
        aria-expanded={!collapsed}
        aria-controls={bodyId}
      >
        <span className="flex items-baseline gap-3 flex-wrap">
          <span className="text-section-title">{title}</span>
          {collapsed && summary ? (
            <span className="text-caption text-text-secondary">{summary}</span>
          ) : null}
        </span>
        <span className="text-text-tertiary text-caption shrink-0">
          {collapsed ? t("common.expand") : t("common.collapse")}
        </span>
      </button>

      <div id={bodyId} hidden={collapsed}>
        {toolbar ? <div className="mt-4">{toolbar}</div> : null}
        <div className={toolbar ? "" : "mt-4"}>{children}</div>
      </div>
    </section>
  );
}
