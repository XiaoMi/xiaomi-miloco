/**
 * 可收起的分区卡。「模型配置」原本自己实现了一套，「Token 用量」「性能监测」没有——
 * 三张卡并排在同一页却只有一张能收，是不一致。这里把后两张接进共用实现，文案与图标
 * 三张一致；「模型配置」的卡头仍是它自己那份（改造它是另一件事），所以展开/收起的
 * 焦点行为与无障碍属性目前是两份，不是一份。
 *
 * 收起状态**不持久化**，与原「模型配置」的行为保持一致（刷新后回到展开）。收起是
 * 「我现在不想看这块」的临时动作，不是长期偏好；真要记住，三张卡应该一起改。
 *
 * 标题整行可点。收起时可以通过 `summary` 在标题旁留一行摘要，免得收起后完全看不出
 * 里面是什么状态；`meta` 则是**两种状态都显示**的一行元信息（如数据新鲜度）——那类东西
 * 是「关于这张卡的数据」而不是控件，混进工具条只会把控件挤密。
 *
 * summary / meta 都只给字号、**不给颜色**：调用方可能需要按状态上色（例如指标越界时标红），
 * 而 Tailwind 的 text-* 工具类特异性相同，在这里写死一个颜色会让调用方的覆盖变成靠
 * 产物里的先后顺序碰运气。
 */

import type { ReactNode } from "react";
import { useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { IconChevronDown, IconChevronUp } from "@/lib/icons";

export function CollapsibleCard({
  title,
  summary,
  meta,
  toolbar,
  children,
  defaultCollapsed = false,
  busy,
}: {
  title: string;
  /** 仅在收起时显示，放一行「里面现在是什么状态」。 */
  summary?: ReactNode;
  /** 展开 / 收起都显示，放一行关于这张卡数据的元信息（如「更新于 12:34」）。 */
  meta?: ReactNode;
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
          {collapsed && summary ? <span className="text-caption">{summary}</span> : null}
        </span>
        <span className="flex items-baseline gap-3 shrink-0">
          {meta ? <span className="text-caption">{meta}</span> : null}
          {/* 图标与文字都在讲**动作**：朝下 = 内容会下来，朝上 = 内容会收上去。
              原先用 Unicode 的 ▾ / ▴ 跟着 12px 文字走——那类字符的字形只占 em 的一小部分，
              实际笔画不足 4px，且粗细与基线由系统字体决定、无从控制。换成内联 SVG 后
              尺寸独立于文字：16px 比 12px 的文字重一档，但不抢戏。 */}
          <span className="text-text-tertiary text-caption inline-flex items-center gap-1">
            {collapsed ? t("common.expand") : t("common.collapse")}
            {collapsed ? (
              <IconChevronDown width={16} height={16} />
            ) : (
              <IconChevronUp width={16} height={16} />
            )}
          </span>
        </span>
      </button>

      <div id={bodyId} hidden={collapsed}>
        {toolbar ? <div className="mt-4">{toolbar}</div> : null}
        {/* 工具条与它作用的内容之间要留一道：原先有工具条时内容**一点上边距都没有**，
            筛选控件与数字/KPI 卡直接贴在一起。取 10px——窄于标题到工具条的 16px，
            于是工具条在视觉上归到它作用的那块内容上，而不是归到标题行。 */}
        <div className={toolbar ? "mt-2.5" : "mt-4"}>{children}</div>
      </div>
    </section>
  );
}
