/**
 * 行内帮助提示：一个圆圈「?」图标，鼠标悬停 / 键盘聚焦时自身高亮（品牌色）并弹出浮层说明。
 * 纯 CSS group-hover + group-focus-within，无第三方库；用于给标签补充"说明性/scoping"信息。
 *
 * tone="warning" 把「?」换成「!」并上警示色。做成同一个组件的一档而不是另开一个图标，
 * 是因为它常常要在原地替换掉「?」——两者的**外形尺寸必须一致**（都是 16px 圆），
 * 否则在宽度已经吃紧的行里（如费用说明行）换一下就会把整行挤换行。
 */
import type { ReactNode } from "react";

export function HelpTip({
  text,
  className = "",
  wide = false,
  tone = "info",
}: {
  text: ReactNode;
  className?: string;
  /** 长说明用：改成定宽换行，否则 whitespace-nowrap 会把整段拉成一行超出视口。 */
  wide?: boolean;
  /** warning：换成「!」并上警示色，用于「这个数不完整/有前提」。占位尺寸不变。 */
  tone?: "info" | "warning";
}) {
  const warn = tone === "warning";
  return (
    <span className={`relative inline-flex group align-middle ${className}`}>
      <button
        type="button"
        aria-label={typeof text === "string" ? text : undefined}
        className={`inline-flex items-center justify-center w-4 h-4 rounded-full border text-[10px] leading-none transition-colors focus:outline-none ${
          warn
            ? "border-warning text-warning hover:bg-warning-bg focus:bg-warning-bg"
            : "border-border text-text-tertiary hover:text-brand-primary hover:border-brand-primary focus:text-brand-primary focus:border-brand-primary"
        }`}
      >
        {warn ? "!" : "?"}
      </button>
      <span
        role="tooltip"
        className={`pointer-events-none absolute left-1/2 top-full z-50 mt-1.5 -translate-x-1/2 rounded-lg border border-border bg-bg-secondary px-2.5 py-1.5 text-caption font-normal leading-relaxed text-text-secondary shadow-sm opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100 ${
          wide ? "w-64 whitespace-normal text-left" : "whitespace-nowrap"
        }`}
      >
        {text}
      </span>
    </span>
  );
}
