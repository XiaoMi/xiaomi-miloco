/**
 * 「模型」页顶部的 omni 模型配置卡(可折叠,默认展开)。
 *
 * 两块:
 * - 上:**当前模型** —— 当前生效配置(model / Base URL / 打码 key);未配 key 给警告。
 * - 下:**模型列表** —— 每行 模型 | Base URL | API Key(打码),可「启用」/「删除」;
 *   「＋ 新增」展开表单(Base URL → API Key → 模型组合框 + 测试连接 + 保存)。
 *
 * 档案名对用户隐藏:内部用 `model @ base_url` 作为后端 label(唯一 id)。重复添加同
 * (model, base_url) = 更新该配置的 key(等价编辑)。后端按 label activate/delete/upsert。
 * 保存写 config.json,感知下个推理周期热生效(免重启);api_key 打码、留空=沿用原 key。
 */

import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  getOmniConfig,
  updateOmniConfig,
  activateOmniConfig,
  deleteOmniConfig,
  listOmniModels,
  testOmniConfig,
} from "@/api";
import type { OmniConfigState, OmniProfile, OmniTestResult } from "@/lib/types";
import { toast } from "./Toast";

const INPUT_CLS =
  "w-full px-3 py-2 rounded-lg bg-bg-primary border border-border " +
  "focus:border-brand-primary focus:outline-none text-text-primary num";

// omni 测试 / 模型列表的后端机器码 → i18n key;命中走前端本地化,
// 未命中(如 http_error,含动态 HTTP 细节)回退后端 message。
const OMNI_CODE_KEY: Record<string, string> = {
  ok: "usage.testOk",
  ok_model_found: "usage.testOkModelFound",
  bad_key: "usage.testBadKey",
  not_found: "usage.testNotFound",
  rejected_authed: "usage.testRejectedAuthed",
  unreachable: "usage.testUnreachable",
  no_key: "usage.testNoKey",
};

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

function Field({
  label,
  children,
  className = "",
}: {
  label: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <label className={`block ${className}`}>
      <span className="text-caption text-text-secondary mb-1 block">{label}</span>
      {children}
    </label>
  );
}

/** 通用组合框:可输入(自由文本)+ ▾ 展开面板点选。输入时按子串过滤;已选/空时点 ▾ 看全部。 */
function ComboBox({
  value,
  onChange,
  options,
  placeholder,
  ariaLabel,
}: {
  value: string;
  onChange: (v: string) => void;
  options: string[];
  placeholder?: string;
  ariaLabel?: string;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const h = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [open]);
  const q = value.trim().toLowerCase();
  const exact = options.some((o) => o.toLowerCase() === q);
  const list = !q || exact ? options : options.filter((o) => o.toLowerCase().includes(q));
  return (
    <div className="relative" ref={ref}>
      <input
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        placeholder={placeholder}
        className={INPUT_CLS + " pr-9"}
        autoComplete="off"
        aria-label={ariaLabel}
      />
      {options.length > 0 && (
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="absolute right-2 top-1/2 -translate-y-1/2 text-text-tertiary px-1 text-xl leading-none"
          aria-label={t("usage.expandOptions")}
        >
          ▾
        </button>
      )}
      {open && list.length > 0 && (
        <ul className="absolute left-0 right-0 mt-1 z-20 max-h-60 overflow-auto rounded-lg bg-bg-secondary border border-border shadow-md py-1">
          {list.map((o) => (
            <li key={o}>
              <button
                type="button"
                onClick={() => {
                  onChange(o);
                  setOpen(false);
                }}
                className="w-full text-left px-3 py-2 text-caption text-text-primary hover:bg-bg-primary num"
              >
                {o}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function UsageOmniConfig() {
  const { t } = useTranslation();
  const [state, setState] = useState<OmniConfigState | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(false); // 默认展开

  // 新增表单
  const [adding, setAdding] = useState(false);
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelsMsg, setModelsMsg] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<OmniTestResult | null>(null);

  // 编辑状态（与 adding 互斥）
  const [editingLabel, setEditingLabel] = useState<string | null>(null);
  const [editingProfile, setEditingProfile] = useState<OmniProfile | null>(null);

  useEffect(() => {
    void load();
  }, []);

  async function load() {
    try {
      setState(await getOmniConfig());
      setLoadErr(null);
    } catch (e) {
      setLoadErr(e instanceof Error ? e.message : t("usage.configLoadError"));
    }
  }

  const profiles = state?.profiles ?? [];
  const active = state?.active;
  const hasKey = active?.has_key ?? false;
  // 新增表单里同 (model, base_url) 是否已存(→ 改为更新该条)
  const existing = profiles.find(
    (p) => p.base_url === baseUrl.trim() && p.model === model.trim(),
  );

  function startAdd() {
    setEditingLabel(null); // 与编辑互斥
    setEditingProfile(null);
    setAdding(true);
    setBaseUrl("");
    setApiKey("");
    setModel("");
    setModels([]);
    setModelsMsg(null);
    setTestResult(null);
  }

  function startEdit(p: OmniProfile) {
    setAdding(false); // 与新增互斥
    setEditingLabel(p.label);
    setEditingProfile(p);
    setBaseUrl(p.base_url);
    setApiKey(""); // API Key 不回显，用户留空=沿用原 key
    setModel(p.model);
    setModels([]);
    setModelsMsg(null);
    setTestResult(null);
  }

  async function fetchModels(bu: string, key: string) {
    if (!bu.trim()) return;
    setModelsLoading(true);
    setModelsMsg(null);
    try {
      const res = await listOmniModels({ base_url: bu.trim(), api_key: key.trim() || undefined });
      if (res.ok) {
        setModels(res.models);
        if (!res.models.length) setModelsMsg(t("usage.modelsEmptyResult"));
      } else {
        setModels([]);
        const k = res.code ? OMNI_CODE_KEY[res.code] : undefined;
        setModelsMsg(k ? t(k) : res.message || t("usage.modelsFetchFailed"));
      }
    } catch (e) {
      setModels([]);
      setModelsMsg(e instanceof Error ? e.message : t("usage.modelsFetchFailed"));
    } finally {
      setModelsLoading(false);
    }
  }

  async function onSave() {
    const bu = baseUrl.trim();
    const m = model.trim();
    const isEditing = !!editingLabel;
    if (!bu || !m) {
      toast(t("usage.baseUrlModelRequired"), "warn");
      return;
    }
    // 编辑模式：允许不填 api_key（沿用原 key）
    // 新增模式：检查是否已有 key（existing.has_key）
    if (!apiKey.trim() && !editingProfile?.has_key && !existing?.has_key) {
      toast(t("usage.apiKeyRequired"), "warn");
      return;
    }
    setSaving(true);
    try {
      // 编辑模式：label 基于新值，original_label 记录原档案名
      // 新增模式：label = `${model} @ ${base_url}`
      const s = await updateOmniConfig({
        label: editingLabel ? `${m} @ ${bu}` : (existing ? existing.label : `${m} @ ${bu}`),
        model: m,
        base_url: bu,
        api_key: apiKey.trim() || undefined,
        original_label: editingLabel || (existing ? existing.label : undefined),
        activate: false, // 只入列表；启用由模型列表的「启用」负责
      });
      setState(s);
      setAdding(false);
      setEditingLabel(null); // 清空编辑状态
      setEditingProfile(null);
      toast(isEditing ? t("usage.editSuccess") : t("usage.saveSuccess"), "ok");
    } catch (e) {
      toast(e instanceof Error ? e.message : t("usage.saveFailed"), "danger");
    } finally {
      setSaving(false);
    }
  }

  async function onTest() {
    const bu = baseUrl.trim();
    const m = model.trim();
    if (!bu || !m) {
      toast(t("usage.baseUrlModelRequired"), "warn");
      return;
    }
    if (!apiKey.trim() && !editingProfile?.has_key && !existing?.has_key) {
      toast(t("usage.apiKeyRequiredBeforeTest"), "warn");
      return;
    }
    setTesting(true);
    setTestResult(null);
    try {
      const res = await testOmniConfig({
        label: editingLabel || (existing ? existing.label : ""),
        model: m,
        base_url: bu,
        api_key: apiKey.trim() || undefined,
      });
      setTestResult(res);
    } catch (e) {
      setTestResult({ ok: false, message: e instanceof Error ? e.message : t("usage.testFailed") });
    } finally {
      setTesting(false);
    }
  }

  async function onActivate(p: OmniProfile) {
    try {
      setState(await activateOmniConfig({ label: p.label }));
      toast(t("usage.activateSuccess"), "ok");
    } catch (e) {
      toast(e instanceof Error ? e.message : t("usage.activateFailed"), "danger");
    }
  }

  async function onDelete(p: OmniProfile) {
    if (!window.confirm(t("usage.deleteConfirm", { model: p.model, host: hostOf(p.base_url) }))) return;
    try {
      setState(await deleteOmniConfig({ label: p.label }));
      toast(t("usage.deleteSuccess"), "ok");
    } catch (e) {
      toast(e instanceof Error ? e.message : t("usage.deleteFailed"), "danger");
    }
  }

  return (
    <section
      className="rounded-xl bg-bg-secondary border border-border shadow-sm p-5 md:p-6"
      aria-labelledby="usage-omni-config-title"
    >
      <button
        type="button"
        onClick={() => setCollapsed((c) => !c)}
        className="w-full flex items-center justify-between gap-3 text-left"
        aria-expanded={!collapsed}
      >
        <span className="flex items-baseline gap-3 flex-wrap">
          <span id="usage-omni-config-title" className="text-section-title">
            {t("usage.configTitle")}
          </span>
          {collapsed && active && (
            <span className="text-caption text-text-secondary num">
              {t("usage.currentPrefix")}
              {hasKey ? `${active.model} · ${hostOf(active.base_url)}` : t("usage.noApiKeyConfigured")}
            </span>
          )}
        </span>
        <span className="text-text-tertiary text-caption shrink-0">
          {collapsed ? t("usage.expand") : t("usage.collapse")}
        </span>
      </button>

      {!collapsed && (
        <div className="mt-4">
          {loadErr ? (
            <div className="text-error text-center py-6">{loadErr}</div>
          ) : !state || !active ? (
            <div className="text-text-secondary text-center py-6">{t("usage.loading")}</div>
          ) : (
            <>
              {/* 未配 key 才给警告;当前生效在列表里用橙色行 + 「当前模型」标记,不再单开字段 */}
              {!hasKey && (
                <div className="text-caption text-warning bg-warning-bg rounded-lg px-3 py-2 mb-3">
                  {t("usage.noKeyWarning")}
                </div>
              )}

              {/* ── 模型列表 ── */}
              <div className="overflow-x-auto -mx-5 md:-mx-6">
                <table className="w-full text-caption whitespace-nowrap">
                  <thead>
                    <tr className="text-text-secondary border-b border-border">
                      <th className="text-left px-5 md:px-6 py-2">{t("usage.colModel")}</th>
                      <th className="text-left px-3 py-2">{t("usage.baseUrlLabel")}</th>
                      <th className="text-left px-3 py-2">{t("usage.colApiKey")}</th>
                      <th className="text-right px-5 md:px-6 py-2">{t("usage.colAction")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {profiles.length === 0 ? (
                      <tr>
                        <td
                          colSpan={4}
                          className="px-5 md:px-6 py-5 text-center text-text-tertiary"
                        >
                          {t("usage.emptyProfiles")}
                        </td>
                      </tr>
                    ) : (
                      profiles.map((p) => (
                        <tr
                          key={p.label}
                          className={`border-b border-border last:border-b-0 ${
                            p.active ? "bg-brand-soft" : ""
                          }`}
                        >
                          <td className="px-5 md:px-6 py-2.5 num text-text-primary">
                            {p.model}
                          </td>
                          <td className="px-3 py-2.5 num text-text-tertiary">{p.base_url}</td>
                          <td className="px-3 py-2.5 num text-text-tertiary">
                            {p.has_key ? p.api_key_masked : t("usage.notConfigured")}
                          </td>
                          <td className="px-5 md:px-6 py-2.5 text-right whitespace-nowrap">
                            {p.active ? (
                              <span className="inline-block rounded-md px-2.5 py-1 bg-brand-primary text-white align-middle mr-3">
                                {t("usage.currentModel")}
                              </span>
                            ) : (
                              <button
                                type="button"
                                onClick={() => onActivate(p)}
                                className="hover:bg-brand-soft text-brand-primary border border-brand-primary rounded-md px-2.5 py-1 mr-3"
                              >
                                {t("usage.activate")}
                              </button>
                            )}
                            {/* 编辑按钮 */}
                            <button
                              type="button"
                              onClick={() => startEdit(p)}
                              className="text-text-tertiary hover:text-brand-primary mr-3"
                            >
                              {t("usage.edit")}
                            </button>
                            <button
                              type="button"
                              onClick={() => onDelete(p)}
                              className="text-text-tertiary hover:text-error"
                            >
                              {t("usage.delete")}
                            </button>
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>

              {/* 新增按钮放列表下方(新增即追加到列表末尾) */}
              {!adding && !editingLabel && (
                <button
                  type="button"
                  onClick={startAdd}
                  className="mt-3 text-caption text-text-secondary hover:text-brand-primary inline-flex items-center gap-1"
                >
                  <span className="text-lg leading-none">＋</span> {t("usage.addModel")}
                </button>
              )}

              {/* ── 新增/编辑表单 ── */}
              {(adding || editingLabel) && (
                <div className="mt-4 rounded-lg bg-bg-primary border border-border p-4 grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-4 items-start">
                  <div className="md:col-span-2 text-caption text-text-secondary">
                    {editingLabel
                      ? t("usage.editFormHint")
                      : t("usage.addFormHint")}
                  </div>
                  <Field label={t("usage.baseUrlLabel")} className="md:col-span-2">
                    <input
                      value={baseUrl}
                      onChange={(e) => {
                        setBaseUrl(e.target.value);
                        setTestResult(null);
                      }}
                      onBlur={() => fetchModels(baseUrl, apiKey)}
                      placeholder={t("usage.baseUrlPlaceholder")}
                      className={INPUT_CLS}
                    />
                  </Field>
                  <Field label={t("usage.apiKeyLabel")}>
                    <input
                      type="password"
                      value={apiKey}
                      onChange={(e) => {
                        setApiKey(e.target.value);
                        setTestResult(null);
                      }}
                      onBlur={() => fetchModels(baseUrl, apiKey)}
                      placeholder={
                        editingProfile?.has_key || existing?.has_key
                          ? t("usage.apiKeyPlaceholderExisting")
                          : t("usage.apiKeyPlaceholderNew")
                      }
                      autoComplete="off"
                      className={INPUT_CLS}
                    />
                  </Field>
                  <Field label={t("usage.modelLabel")}>
                    <ComboBox
                      value={model}
                      onChange={(v) => {
                        setModel(v);
                        setTestResult(null);
                      }}
                      options={models}
                      placeholder={
                        modelsLoading
                          ? t("usage.modelComboPlaceholderLoading")
                          : t("usage.modelComboPlaceholder")
                      }
                      ariaLabel={t("usage.modelLabel")}
                    />
                    <span className="text-caption text-text-tertiary mt-1 block">
                      {modelsLoading
                        ? t("usage.modelsFetching")
                        : modelsMsg
                          ? modelsMsg
                          : models.length
                            ? t("usage.modelsCount", { n: models.length })
                            : t("usage.modelsHint")}
                    </span>
                  </Field>
                  <div className="md:col-span-2 pt-1 flex items-center gap-3 flex-wrap">
                    <button
                      type="button"
                      onClick={onSave}
                      disabled={saving || testing}
                      className="px-5 py-2 rounded-lg bg-brand-primary text-white hover:opacity-90 disabled:opacity-60"
                    >
                      {saving ? t("usage.saving") : t("usage.save")}
                    </button>
                    <button
                      type="button"
                      onClick={onTest}
                      disabled={saving || testing}
                      className="px-5 py-2 rounded-lg bg-bg-secondary border border-border text-text-primary hover:border-brand-primary disabled:opacity-60"
                    >
                      {testing ? t("usage.testing") : t("usage.testConnection")}
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setAdding(false);
                        setEditingLabel(null);
                        setEditingProfile(null);
                        setTestResult(null);
                      }}
                      disabled={saving || testing}
                      className="px-3 py-2 rounded-lg text-caption text-text-tertiary hover:text-text-primary"
                    >
                      {t("usage.cancel")}
                    </button>
                    {testResult && (
                      <span
                        className={`text-caption ${testResult.ok ? "text-success" : "text-error"}`}
                      >
                        {testResult.ok ? "✓" : "✗"}{" "}
                        {testResult.code && OMNI_CODE_KEY[testResult.code]
                          ? t(OMNI_CODE_KEY[testResult.code])
                          : testResult.message}
                        {testResult.latency_ms != null ? ` · ${testResult.latency_ms}ms` : ""}
                      </span>
                    )}
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </section>
  );
}
