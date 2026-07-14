/**
 * 「和 Agent 说这段话」弹窗——Web 端无法直接创建有效任务（真正驱动任务的感知规则
 * 由 Agent 接线），故统一引导用户与装有 miloco 插件的 Agent 对话来创建任务。
 *
 * 只做引导：一段说明 + 几条示例话术（只读，让用户知道怎么开口），不提供复制 / 编辑，
 * 用户照着示例用自己的话跟 Agent 说即可。
 */

import { useTranslation } from "react-i18next";
import { useEscClose } from "@/hooks/useEscClose";
import { IconX } from "@/lib/icons";

interface Props {
  title: string;
  hint: string;
  /** 示例话术，只读展示，供用户参考怎么跟 Agent 描述诉求。 */
  examples: string[];
  onClose: () => void;
}

export function AgentPromptDialog({ title, hint, examples, onClose }: Props) {
  const { t } = useTranslation();
  useEscClose(true, onClose);

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
              {title}
            </h2>
            <p className="text-caption text-text-tertiary mt-1 leading-relaxed">
              {hint}
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
          <ul className="flex flex-col gap-2">
            {examples.map((ex, i) => (
              <li
                key={i}
                className="flex gap-2.5 rounded-lg bg-bg-primary border border-border px-3.5 py-2.5"
              >
                <span className="mt-[7px] h-1.5 w-1.5 rounded-full bg-brand-primary shrink-0" />
                <span className="text-body text-text-secondary leading-relaxed break-words">
                  {ex}
                </span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}
