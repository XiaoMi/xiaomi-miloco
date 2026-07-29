import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  getSplitModelConfig,
  updateSplitModelConfig,
  type SplitModelConfig as SplitModelConfigType,
} from "@/api";
import { toast } from "./Toast";

/** 截图模式双模型配置卡（vision_model / audio_model） */
export function SplitModelConfig() {
  const { t } = useTranslation();
  const [state, setState] = useState<SplitModelConfigType | null>(null);
  const [collapsed, setCollapsed] = useState(true); // 默认折叠
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);

  // 编辑表单
  const [visionModel, setVisionModel] = useState("");
  const [visionBaseUrl, setVisionBaseUrl] = useState("");
  const [visionApiKey, setVisionApiKey] = useState("");
  const [audioModel, setAudioModel] = useState("");
  const [audioBaseUrl, setAudioBaseUrl] = useState("");
  const [audioApiKey, setAudioApiKey] = useState("");

  useEffect(() => {
    void load();
  }, []);

  async function load() {
    try {
      setState(await getSplitModelConfig());
    } catch {
      // 静默失败——旧后端无此端点时不报错
    }
  }

  function startEdit() {
    setEditing(true);
    setVisionModel(state?.vision_model?.model ?? "");
    setVisionBaseUrl(state?.vision_model?.base_url ?? "");
    setVisionApiKey("");
    setAudioModel(state?.audio_model?.model ?? "");
    setAudioBaseUrl(state?.audio_model?.base_url ?? "");
    setAudioApiKey("");
  }

  function cancelEdit() {
    setEditing(false);
  }

  async function handleSave() {
    setSaving(true);
    try {
      const body: {
        vision_model?: { model: string; base_url: string; api_key?: string };
        audio_model?: { model: string; base_url: string; api_key?: string };
      } = {};
      // 只有填了 model 才视为配置该项
      if (visionModel.trim()) {
        const v: { model: string; base_url: string; api_key?: string } = {
          model: visionModel.trim(),
          base_url: visionBaseUrl.trim() || state?.vision_model?.base_url || "",
        };
        if (visionApiKey) v.api_key = visionApiKey;
        body.vision_model = v;
      } else if (state?.vision_model) {
        // 清空 = 删除该项（传空 model）
        body.vision_model = { model: "", base_url: "", api_key: "" };
      }
      if (audioModel.trim()) {
        const a: { model: string; base_url: string; api_key?: string } = {
          model: audioModel.trim(),
          base_url: audioBaseUrl.trim() || state?.audio_model?.base_url || "",
        };
        if (audioApiKey) a.api_key = audioApiKey;
        body.audio_model = a;
      } else if (state?.audio_model) {
        body.audio_model = { model: "", base_url: "", api_key: "" };
      }
      const updated = await updateSplitModelConfig(body);
      setState(updated);
      setEditing(false);
      toast(t("settings.saveSuccess", "已保存"), "ok");
    } catch (e) {
      toast(e instanceof Error ? e.message : t("settings.saveFailed", "保存失败"), "danger");
    } finally {
      setSaving(false);
    }
  }

  const hasVision = !!state?.vision_model?.model;
  const hasAudio = !!state?.audio_model?.model;
  const statusText = hasVision || hasAudio
    ? t("splitModel.active", "已配置（截图模式下自动启用双模型拆分）")
    : t("splitModel.inactive", "未配置（截图模式使用单 omni 模型）");

  return (
    <section className="rounded-xl bg-bg-secondary border border-border shadow-sm">
      {/* 标题栏 */}
      <button
        type="button"
        className="w-full flex items-center justify-between p-5 md:p-6 text-left"
        onClick={() => setCollapsed((c) => !c)}
      >
        <div>
          <h2 className="text-section-title">
            {t("splitModel.title", "截图模式双模型")}
          </h2>
          <p className="text-caption text-text-tertiary mt-1">
            {statusText}
          </p>
        </div>
        <svg
          className={`w-5 h-5 text-text-tertiary transition-transform ${collapsed ? "" : "rotate-180"}`}
          fill="none" viewBox="0 0 24 24" stroke="currentColor"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {/* 内容 */}
      {!collapsed && (
        <div className="px-5 pb-5 md:px-6 md:pb-6 space-y-4">
          <p className="text-caption text-text-tertiary">
            {t("splitModel.hint", "配了视觉模型和音频模型后，截图模式下自动并发调用两个专用模型并合并结果。未配的字段回退到上方 omni 模型的值。")}
          </p>

          {!editing ? (
            /* ─── 展示模式 ─── */
            <>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <ModelDisplay
                  label={t("splitModel.vision", "视觉模型")}
                  item={state?.vision_model ?? null}
                  placeholder={t("splitModel.notSet", "未配置")}
                />
                <ModelDisplay
                  label={t("splitModel.audio", "音频模型")}
                  item={state?.audio_model ?? null}
                  placeholder={t("splitModel.notSet", "未配置")}
                />
              </div>
              <button
                type="button"
                onClick={startEdit}
                className="px-4 py-2 rounded-lg text-body bg-brand-primary text-white hover:opacity-90 transition-opacity"
              >
                {t("splitModel.edit", "编辑")}
              </button>
            </>
          ) : (
            /* ─── 编辑模式 ─── */
            <>
              {/* 视觉模型 */}
              <fieldset className="space-y-2.5">
                <legend className="text-body font-medium text-text-primary">
                  {t("splitModel.vision", "视觉模型")}
                </legend>
                <input
                  className="input-field"
                  placeholder={t("splitModel.modelPlaceholder", "模型名（如 qwen-vl-plus）")}
                  value={visionModel}
                  onChange={(e) => setVisionModel(e.target.value)}
                />
                <input
                  className="input-field"
                  placeholder="Base URL（留空回退 omni）"
                  value={visionBaseUrl}
                  onChange={(e) => setVisionBaseUrl(e.target.value)}
                />
                <input
                  className="input-field"
                  type="password"
                  placeholder={t("splitModel.keyPlaceholder", "API Key（留空回退 omni）")}
                  value={visionApiKey}
                  onChange={(e) => setVisionApiKey(e.target.value)}
                />
              </fieldset>

              {/* 音频模型 */}
              <fieldset className="space-y-2.5">
                <legend className="text-body font-medium text-text-primary">
                  {t("splitModel.audio", "音频模型")}
                </legend>
                <input
                  className="input-field"
                  placeholder={t("splitModel.modelPlaceholder", "模型名（如 whisper-v3）")}
                  value={audioModel}
                  onChange={(e) => setAudioModel(e.target.value)}
                />
                <input
                  className="input-field"
                  placeholder="Base URL（留空回退 omni）"
                  value={audioBaseUrl}
                  onChange={(e) => setAudioBaseUrl(e.target.value)}
                />
                <input
                  className="input-field"
                  type="password"
                  placeholder={t("splitModel.keyPlaceholder", "API Key（留空回退 omni）")}
                  value={audioApiKey}
                  onChange={(e) => setAudioApiKey(e.target.value)}
                />
              </fieldset>

              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={handleSave}
                  disabled={saving}
                  className="px-4 py-2 rounded-lg text-body bg-brand-primary text-white hover:opacity-90 transition-opacity disabled:opacity-50"
                >
                  {saving ? t("common.saving", "保存中…") : t("common.save", "保存")}
                </button>
                <button
                  type="button"
                  onClick={cancelEdit}
                  className="px-4 py-2 rounded-lg text-body border border-border text-text-primary hover:bg-bg-primary transition-colors"
                >
                  {t("common.cancel", "取消")}
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </section>
  );
}

/** 展示模式下的单个模型信息 */
function ModelDisplay({
  label,
  item,
  placeholder,
}: {
  label: string;
  item: { model: string; base_url: string; has_key: boolean; api_key_masked: string } | null;
  placeholder: string;
}) {
  return (
    <div className="rounded-lg border border-border bg-bg-primary p-3 space-y-1">
      <div className="text-caption text-text-tertiary">{label}</div>
      {item?.model ? (
        <>
          <div className="text-body text-text-primary font-medium">{item.model}</div>
          {item.base_url && (
            <div className="text-caption text-text-tertiary truncate">{item.base_url}</div>
          )}
          {item.has_key && (
            <div className="text-caption text-text-tertiary">Key: {item.api_key_masked}</div>
          )}
        </>
      ) : (
        <div className="text-body text-text-tertiary">{placeholder}</div>
      )}
    </div>
  );
}
