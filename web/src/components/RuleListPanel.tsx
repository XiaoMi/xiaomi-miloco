/**
 * 规则管理面板 — 展示已配置的规则列表，支持启停和手动触发。
 */

import { useTranslation } from "react-i18next";
import type { Rule } from "@/lib/types";
import { toggleRule, triggerRule } from "@/api";
import { toast } from "./Toast";

interface Props {
  rules: Rule[];
  loading: boolean;
  onChanged: () => void;
}

function stripTagName(name: string): { tag: string; label: string } {
  const m = name.match(/^\[([^\]]+)\]\s*(.*)/);
  return m ? { tag: m[1], label: m[2] } : { tag: "", label: name };
}

function formatDuration(rule: Rule): string | null {
  if (!rule.duration_seconds) return null;
  const secs = rule.duration_seconds;
  const ratio = rule.duration_ratio != null ? `${Math.round(rule.duration_ratio * 100)}%` : "";
  return `${secs}s${ratio ? ` / ${ratio}` : ""}`;
}

export function RuleListPanel({ rules, loading, onChanged }: Props) {
  const { t } = useTranslation();

  if (loading) {
    return (
      <div className="rounded-xl bg-bg-secondary border border-border shadow-sm p-6 anim-in">
        <h2 className="text-title text-text-primary mb-4">{t("rules.title")}</h2>
        <div className="space-y-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-24 rounded-lg bg-bg-primary animate-pulse" />
          ))}
        </div>
      </div>
    );
  }

  if (rules.length === 0) {
    return (
      <div className="rounded-xl bg-bg-secondary border border-border shadow-sm p-6 anim-in">
        <h2 className="text-title text-text-primary mb-4">{t("rules.title")}</h2>
        <p className="text-text-tertiary text-center py-8">{t("rules.empty")}</p>
      </div>
    );
  }

  const handleToggle = async (rule: Rule) => {
    try {
      await toggleRule(rule.id, !rule.enabled);
      onChanged();
    } catch (e) {
      toast(e instanceof Error ? e.message : t("common.switchFailed"), "warn");
    }
  };

  const handleTrigger = async (rule: Rule) => {
    try {
      await triggerRule(rule.id);
      toast(`${t("rules.trigger")}: ${stripTagName(rule.name).label}`, "ok");
    } catch (e) {
      toast(e instanceof Error ? e.message : t("common.switchFailed"), "warn");
    }
  };

  return (
    <div className="rounded-xl bg-bg-secondary border border-border shadow-sm p-6 anim-in">
      <h2 className="text-title text-text-primary mb-4">{t("rules.title")}</h2>
      <div className="space-y-3">
        {rules.map((rule) => {
          const { tag, label } = stripTagName(rule.name);
          const duration = formatDuration(rule);
          return (
            <div
              key={rule.id}
              className={`rounded-lg border p-4 transition-colors ${
                rule.enabled
                  ? "bg-bg-primary border-border"
                  : "bg-bg-primary border-border opacity-60"
              }`}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1.5 flex-wrap">
                    {tag && (
                      <span className="inline-flex items-center px-2 py-0.5 rounded text-caption font-medium bg-brand-soft text-brand-primary">
                        {tag}
                      </span>
                    )}
                    <span
                      className={`inline-flex items-center px-2 py-0.5 rounded text-caption ${
                        rule.mode === "event"
                          ? "bg-blue-50 text-blue-600 dark:bg-blue-900/30 dark:text-blue-400"
                          : "bg-green-50 text-green-600 dark:bg-green-900/30 dark:text-green-400"
                      }`}
                    >
                      {rule.mode === "event" ? t("rules.event") : t("rules.state")}
                    </span>
                    {duration && (
                      <span className="text-caption text-text-tertiary">
                        {duration}
                      </span>
                    )}
                  </div>
                  {label && (
                    <div className="text-body text-text-primary font-medium mb-1">
                      {label}
                    </div>
                  )}
                  <div className="text-caption text-text-tertiary line-clamp-2">
                    {rule.condition.query}
                  </div>
                  {rule.on_enter_desc && (
                    <div className="text-caption text-text-tertiary mt-1">
                      {t("rules.enterAction")}: {rule.on_enter_desc}
                    </div>
                  )}
                  {rule.action_descriptions.length > 0 && (
                    <div className="text-caption text-text-tertiary mt-1">
                      {rule.action_descriptions[0]}
                    </div>
                  )}
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <button
                    type="button"
                    onClick={() => handleTrigger(rule)}
                    className="inline-flex items-center justify-center rounded-md text-text-tertiary hover:text-text-primary hover:bg-bg-tertiary transition-colors"
                    style={{ width: 32, height: 32 }}
                    title={t("rules.trigger")}
                    disabled={!rule.enabled}
                  >
                    <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
                      <path d="M4 2l10 6-10 6V2z" />
                    </svg>
                  </button>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={rule.enabled}
                    onClick={() => handleToggle(rule)}
                    className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
                      rule.enabled ? "bg-brand-primary" : "bg-text-tertiary"
                    }`}
                  >
                    <span
                      className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                        rule.enabled ? "translate-x-6" : "translate-x-1"
                      }`}
                    />
                  </button>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
