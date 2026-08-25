/**
 * 「清空用量数据」确认弹窗。形制与模型配置里的删除弹窗一致。
 *
 * 为什么从行内确认改成弹窗，两个原因：
 *  - 行内确认把后果感压得太轻——这是本页唯一不可恢复的操作，却和「切周期」长得差不多。
 *  - 行内确认会把承载焦点的按钮连带卸掉：点「清空数据」后那个 <button> 被换成 <span>，
 *    焦点掉回 <body>，下一次 Tab 从整份文档开头重新开始，键盘用户根本走不到确认按钮，
 *    而且没有任何朗读提示告诉他弹出了什么。弹窗则能把焦点收进来、Esc 收起、结束后归位。
 */

import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { IconX } from "@/lib/icons";
import { toast } from "./Toast";
import type { ClearScope } from "./UsageClearMenu";

export function UsageClearDialog({
  scope,
  clear,
  onCleared,
  onClose,
}: {
  /** 要清的作用域（时间范围 × 目标），决定弹窗复述的是「全部」「某个时段」还是某一项。 */
  scope: ClearScope;
  /**
   * fromDate 是本弹窗**已经写给用户看**的那一天（YYYY-MM-DD），要原样发给后端：
   * 日表按盒子的时区归日，这句话按浏览器的时区算，两者能差一天。不传的话
   * 「说了哪天」与「删了哪天」就可能不是同一天，而这句提示的全部意义就在于此。
   */
  clear: (s: ClearScope, fromDate: string | null) => Promise<void>;
  onCleared: () => void;
  onClose: () => void;
}) {
  const { t, i18n } = useTranslation();
  const [busy, setBusy] = useState(false);
  const cancelRef = useRef<HTMLButtonElement | null>(null);

  /**
   * 日表被连带删除的那一天：**同一个 Date 同时产出给人看的和发给后端的两种写法**。
   * 分开各算一次就等于给了它们分歧的机会，而这句提示的全部价值就是「说的那天
   * 就是删的那天」。全清没有边界日，故为 null。
   */
  const boundary = (() => {
    if (scope.sinceMs == null) return null;
    const d = new Date(scope.sinceMs);
    const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
      d.getDate(),
    ).padStart(2, "0")}`;
    return { iso, label: d.toLocaleDateString(i18n.language === "en" ? "en-US" : "zh-CN") };
  })();

  // 打开时焦点落「取消」——破坏性操作不把默认焦点放在执行键上
  useEffect(() => {
    cancelRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  async function doClear() {
    setBusy(true);
    try {
      await clear(scope, boundary?.iso ?? null);
      toast(t("usage.clearSuccess"), "ok");
      onCleared();
    } catch (e) {
      toast(e instanceof Error ? e.message : t("usage.clearFailed"), "danger");
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-[70] flex items-end md:items-center justify-center
                 bg-black/40 backdrop-blur-sm p-0 md:p-5"
      onClick={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="usage-clear-title"
        className="modal-surface w-full md:max-w-[440px] rounded-t-2xl md:rounded-xl
                   border border-border shadow-lg p-5 md:p-6"
      >
        <div className="flex items-start justify-between gap-3 mb-3">
          <h2 id="usage-clear-title" className="text-title font-semibold">
            {t(scope.target ? "usage.clearDialogTitleTarget" : "usage.clearDialogTitle")}
          </h2>
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            aria-label={t("common.close")}
            className="shrink-0 rounded-full p-1 text-text-secondary hover:text-text-primary
                       hover:bg-bg-tertiary disabled:opacity-50 transition-colors"
          >
            <IconX />
          </button>
        </div>

        {/* 作用域：定点时把「模型名 + Base URL」摆出来（样式同明细的模型列），
            否则「这一项」指的是谁只能靠记忆。 */}
        <div className="text-body text-text-primary bg-bg-primary rounded-lg px-3 py-2
                        flex items-center gap-2 flex-wrap">
          {scope.target ? (
            <>
              <span className="text-caption text-text-secondary">
                {t("usage.clearScopePrefix")}
              </span>
              <span className="num">{scope.target.model}</span>
              {scope.target.baseUrl ? (
                <span className="num text-caption px-1.5 rounded border border-border
                                 text-text-tertiary break-all">
                  {scope.target.baseUrl}
                </span>
              ) : (
                <span className="text-caption px-1.5 rounded border border-dashed
                                 border-border text-text-tertiary">
                  {t("usage.modelUrlLegacy")}
                </span>
              )}
              <span className="text-caption text-text-secondary">
                {scope.sinceMs == null ? t("usage.clearScopeAllTime") : `· ${scope.label}`}
              </span>
            </>
          ) : scope.sinceMs == null ? (
            t("usage.clearScopeAll")
          ) : (
            t("usage.clearScopeSince", { label: scope.label })
          )}
        </div>
        {/* 日聚合表只有天粒度：跨天范围会连带删掉边界当天更早的记录。
            这是日聚合的固有精度损失，必须说出来——否则就是悄悄多删。 */}
        {boundary && (
          <p className="text-caption text-text-secondary mt-2">
            {t(scope.target ? "usage.clearDailyCaveatTarget" : "usage.clearDailyCaveat", {
              date: boundary.label,
            })}
          </p>
        )}
        {scope.target && (
          <p className="text-caption text-text-secondary mt-2">
            {t("usage.clearTargetOnlyNote")}
          </p>
        )}
        <p className="text-caption text-error bg-error-bg rounded-lg px-3 py-2 mt-3">
          {t("usage.clearDialogWarn")}
        </p>

        <div className="mt-6 flex justify-end gap-2">
          <button
            ref={cancelRef}
            type="button"
            onClick={onClose}
            disabled={busy}
            className="text-body px-4 py-2 rounded-lg bg-bg-primary border border-border
                       text-text-primary hover:border-border-strong disabled:opacity-60
                       transition-colors"
          >
            {t("usage.cancel")}
          </button>
          <button
            type="button"
            onClick={doClear}
            disabled={busy}
            className="text-body px-4 py-2 rounded-lg bg-error text-text-inverse
                       hover:brightness-95 disabled:opacity-60 transition-[filter,opacity]"
          >
            {busy ? t("usage.clearing") : t("usage.clearConfirm")}
          </button>
        </div>
      </div>
    </div>
  );
}
