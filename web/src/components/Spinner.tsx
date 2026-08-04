/** 转圈加载指示（CSS animate-spin）。默认 14px；顶边品牌色，其余描边色。 */
export function Spinner({ className = "w-3.5 h-3.5" }: { className?: string }) {
  return (
    <span
      aria-hidden
      className={`inline-block shrink-0 rounded-full border-2 border-border border-t-brand-primary animate-spin ${className}`}
    />
  );
}
