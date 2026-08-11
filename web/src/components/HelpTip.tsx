/**
 * 行内帮助提示：一个圆圈「?」图标，鼠标悬停 / 键盘聚焦时自身高亮（品牌色）并弹出浮层说明。
 * 纯 CSS group-hover + group-focus-within，无第三方库；用于给标签补充"说明性/scoping"信息。
 */
import type { ReactNode } from "react";

export function HelpTip({
  text,
  className = "",
}: {
  text: ReactNode;
  className?: string;
}) {
  return (
    <span className={`relative inline-flex group align-middle ${className}`}>
      <button
        type="button"
        aria-label={typeof text === "string" ? text : undefined}
        className="inline-flex items-center justify-center w-4 h-4 rounded-full border border-border text-[10px] leading-none text-text-tertiary transition-colors hover:text-brand-primary hover:border-brand-primary focus:text-brand-primary focus:border-brand-primary focus:outline-none"
      >
        ?
      </button>
      <span
        role="tooltip"
        className="pointer-events-none absolute left-1/2 top-full z-50 mt-1.5 -translate-x-1/2 whitespace-nowrap rounded-lg border border-border bg-bg-secondary px-2.5 py-1.5 text-caption font-normal text-text-secondary shadow-sm opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100"
      >
        {text}
      </span>
    </span>
  );
}
