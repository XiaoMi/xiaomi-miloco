/**
 * 轻量拨动开关——track + knob，on=品牌色 / off=中性边框色，无障碍 role="switch"。
 * 概览任务启停、家庭成员宠物启停等复用同一款式。
 */
interface Props {
  checked: boolean;
  disabled?: boolean;
  onChange: () => void;
  /** 无障碍标签；同时作为悬停 title（可放较长的说明）。 */
  label: string;
}

export function Switch({ checked, disabled, onChange, label }: Props) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onChange}
      className={`relative shrink-0 inline-flex h-5 w-9 items-center rounded-full transition-colors disabled:opacity-50 ${
        checked ? "bg-brand-primary" : "bg-border-strong"
      }`}
    >
      <span
        className={`inline-block h-4 w-4 rounded-full bg-white shadow-sm transition-transform ${
          checked ? "translate-x-[18px]" : "translate-x-0.5"
        }`}
      />
    </button>
  );
}
