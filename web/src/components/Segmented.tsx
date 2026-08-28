/** 通用分段控件（tab 风格）。用量页的周期、时间粒度、图表着色口径，以及顶栏语言切换都用它。 */
export function Segmented<T extends string | number>({
  options,
  value,
  onChange,
  ariaLabel,
}: {
  options: { key: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
  ariaLabel: string;
}) {
  return (
    <div
      className="flex gap-1 bg-bg-primary rounded-lg p-1"
      role="tablist"
      aria-label={ariaLabel}
    >
      {options.map((o) => {
        const on = value === o.key;
        return (
          <button
            key={o.key}
            type="button"
            role="tab"
            aria-selected={on}
            onClick={() => onChange(o.key)}
            // whitespace-nowrap：英文档位（"Last 30 days"）在窄屏会被挤成两行、
            // 把整排药丸的高度撑乱；宁可让整排换行，不要标签从词中间断开。
            className={`text-caption px-3 py-1 rounded-lg transition-colors whitespace-nowrap ${
              on
                ? "bg-bg-secondary text-text-primary shadow-sm"
                : "text-text-secondary hover:text-text-primary"
            }`}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
