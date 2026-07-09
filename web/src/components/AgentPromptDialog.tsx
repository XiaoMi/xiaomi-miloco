/**
 * 「和 Agent 说这段话」弹窗——Web 端无法直接创建有效任务（真正驱动任务的感知规则
 * 由 Agent 接线），故统一引导用户把一段自然语言指令复制发给 Miloco 的 Agent。
 *
 * 复用场景：
 *  - 任务页「查看示例话术」引导卡
 *  - 家庭档案里作息习惯条目的「转为任务」
 *
 * 话术可编辑（textarea），提供「复制」到剪贴板（失败降级为选中文本让用户手动复制）。
 */

import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useEscClose } from "@/hooks/useEscClose";
import { IconCheck, IconX } from "@/lib/icons";
import { toast } from "./Toast";

interface Props {
  /** 预填话术（可编辑）。 */
  initialText: string;
  onClose: () => void;
  /** 覆盖标题/说明，缺省用通用文案。 */
  title?: string;
  hint?: string;
}

export function AgentPromptDialog({ initialText, onClose, title, hint }: Props) {
  const { t } = useTranslation();
  const [text, setText] = useState(initialText);
  const [copied, setCopied] = useState(false);
  const taRef = useRef<HTMLTextAreaElement>(null);
  useEscClose(true, onClose);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      toast(t("tasks.copied"), "ok");
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      // 无 clipboard 权限（http / 老浏览器）：选中文本让用户手动 Ctrl/Cmd+C。
      taRef.current?.select();
      toast(t("tasks.copyFail"), "warn");
    }
  };

  return (
    <div
      className="fixed inset-0 z-[70] flex items-end md:items-center justify-center bg-black/40 backdrop-blur-sm"
      onClick={(e) => {
        e.stopPropagation();
        onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="agent-prompt-title"
        className="flex w-full max-h-[85vh] flex-col bg-bg-secondary border border-border rounded-t-2xl md:max-w-md md:rounded-2xl shadow-lg anim-in"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3 px-5 pt-5 pb-3">
          <div className="min-w-0">
            <h2
              id="agent-prompt-title"
              className="text-title font-semibold text-text-primary"
            >
              {title ?? t("tasks.promptDialogTitle")}
            </h2>
            <p className="text-caption text-text-tertiary mt-1">
              {hint ?? t("tasks.promptDialogHint")}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={t("family.close")}
            className="shrink-0 p-1.5 -mr-1.5 rounded-md text-text-tertiary hover:text-text-primary hover:bg-bg-tertiary transition-colors"
          >
            <IconX width={18} height={18} />
          </button>
        </div>

        <div className="px-5 pb-5 overflow-y-auto">
          <textarea
            ref={taRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={4}
            className="w-full resize-none rounded-lg bg-bg-primary border border-border px-3 py-2.5 text-body text-text-primary focus:outline-none focus:border-border-strong"
          />
          <div className="flex justify-end mt-3">
            <button
              type="button"
              onClick={copy}
              className="inline-flex items-center gap-1.5 text-body px-4 py-2 rounded-lg font-semibold bg-brand-primary text-white hover:opacity-90 transition-opacity"
            >
              {copied ? <IconCheck width={16} height={16} /> : null}
              {copied ? t("tasks.copied") : t("tasks.copy")}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
