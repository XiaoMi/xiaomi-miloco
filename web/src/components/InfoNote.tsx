/** 行内静态提示：圆圈「!」+ 说明文字（次要色）。用于常驻的软性引导 / 提示。 */
import type { ReactNode } from "react";

export function InfoNote({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`flex items-start gap-1.5 text-caption text-text-tertiary ${className}`}
    >
      <span className="mt-px inline-flex w-4 h-4 shrink-0 items-center justify-center rounded-full border border-border text-[10px] leading-none">
        !
      </span>
      <span>{children}</span>
    </div>
  );
}
