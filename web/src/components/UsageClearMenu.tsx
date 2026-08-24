/**
 * 「清空数据」入口：垃圾桶图标 → 气泡里选清理范围 → 交给确认弹窗。
 *
 * 为什么不是一个写着「清空数据」的文字按钮（原做法）：它当时紧贴「刷新」，
 * 间距 12px、视觉重量完全相同——一个安全高频、一个不可逆，误触代价不对称，
 * 而且没有任何「这一步不可逆」的暗示。改成无框图标后两者不再是同一类东西；
 * 多出来的一次展开也把「手滑就点到」变成「要先找到再点」。
 *
 * 顺带补了一个原先没有的能力：**按时间范围清**。后端此前只有全清。
 *
 * 气泡是绝对定位的——这里可以：工具条不在任何 overflow 容器里。
 * （明细表里就不行，那边在 overflow-x:auto 内，浮层会被裁，故那边用的是跨列展开行。）
 */

import { useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { IconTrash } from "@/lib/icons";

/** 一个可选范围。sinceMs=null 表示全部。 */
export interface ClearScope {
  key: "24h" | "7d" | "all";
  label: string;
  sinceMs: number | null;
}

export function UsageClearMenu({ onPick }: { onPick: (s: ClearScope) => void }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const menuId = useId();

  // 点外面 / Esc 关闭。Esc 同时把焦点还给触发按钮，否则键盘用户会掉到 body。
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        wrapRef.current?.querySelector("button")?.focus();
      }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // 时刻在点击那一下才取，不在渲染时取——渲染可能发生在几分钟前
  const scopes = (): ClearScope[] => {
    const now = Date.now();
    return [
      { key: "24h", label: t("usage.clearRange24h"), sinceMs: now - 24 * 3600_000 },
      { key: "7d", label: t("usage.clearRange7d"), sinceMs: now - 7 * 24 * 3600_000 },
      { key: "all", label: t("usage.clearRangeAll"), sinceMs: null },
    ];
  };

  return (
    <div className="relative inline-flex" ref={wrapRef}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-label={t("usage.clearData")}
        title={t("usage.clearData")}
        className={`inline-flex items-center justify-center w-[30px] h-[30px] rounded-md
                    border transition-colors ${
                      open
                        ? "border-error text-error bg-error-bg"
                        : "border-transparent text-text-tertiary hover:text-error hover:border-error"
                    }`}
      >
        <IconTrash width={15} height={15} />
      </button>

      {open && (
        <div
          id={menuId}
          role="menu"
          className="absolute right-0 top-[calc(100%+8px)] z-50 min-w-[196px] rounded-xl
                     border border-border bg-bg-secondary shadow-lg p-2"
        >
          <div className="px-1.5 pb-1.5 text-caption text-text-tertiary">
            {t("usage.clearMenuTitle")}
          </div>
          {scopes().map((s, i) => (
            <div key={s.key}>
              {/* 「全部」与另两档不是一类，用分隔线划开并上警示色 */}
              {i === 2 && <div className="h-px bg-border my-1.5 mx-1" />}
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setOpen(false);
                  onPick(s);
                }}
                className={`w-full text-left text-body px-2.5 py-1.5 rounded-md transition-colors ${
                  s.key === "all"
                    ? "text-error hover:bg-error-bg"
                    : "text-text-secondary hover:text-text-primary hover:bg-bg-primary"
                }`}
              >
                {s.label}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
