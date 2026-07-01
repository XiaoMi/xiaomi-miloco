/**
 * 宠物「自动生成外观描述」流程（镜像 EnrollFlow 的上传→处理→结果，但产物是文本）。
 * 进入即先做一次感知模型连通性预检（getOmniConfig + testOmniConfig），未配置/不可用则
 * 直接拦下并给出原因，避免走到慢 observe 才暴露模型问题。
 * 上传图/视频 → 调 observePet（后端选最优 crop + omni 按维度生成描述）→ 用户编辑确认 →
 * onDone 回传外观文本 + 物种 + 选出的头像 crop（base64）+ 头部框，由 PetDrawer 落库/裁剪设头像。
 * 无副作用：本组件只观察生成，不写库。
 */
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { getOmniConfig, observePet, testOmniConfig } from "@/api";
import { useEscClose } from "@/hooks/useEscClose";
import { IconAlert, IconX } from "@/lib/icons";
import { toast } from "./Toast";

interface Props {
  /** 是否要头部 grounding（取 features.petHeadGrounding） */
  grounding: boolean;
  onClose: () => void;
  onDone: (result: {
    appearance: string;
    species: string;
    cropB64: string;
    headBbox: number[] | null;
  }) => void;
}

type CheckState = "checking" | "ok" | "unconfigured" | "unavailable";

export function PetAutoGenFlow({ grounding, onClose, onDone }: Props) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [analyzed, setAnalyzed] = useState(false);
  const [appearance, setAppearance] = useState("");
  const [species, setSpecies] = useState("");
  const [cropB64, setCropB64] = useState("");
  const [headBbox, setHeadBbox] = useState<number[] | null>(null);
  const [note, setNote] = useState("");
  const [check, setCheck] = useState<CheckState>("checking");
  const [checkMsg, setCheckMsg] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  useEscClose(!busy, onClose);

  // 进入即预检当前生效的感知模型：未配置 / 不可用都在上传前拦下。
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const cfg = await getOmniConfig();
        if (!alive) return;
        if (!cfg.active?.has_key) {
          setCheck("unconfigured");
          return;
        }
        const res = await testOmniConfig({
          label: cfg.active.label,
          model: cfg.active.model,
          base_url: cfg.active.base_url,
        });
        if (!alive) return;
        if (res.ok) {
          setCheck("ok");
        } else {
          setCheck("unavailable");
          setCheckMsg(res.message);
        }
      } catch (e) {
        if (!alive) return;
        setCheck("unavailable");
        setCheckMsg(e instanceof Error ? e.message : "");
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const onPick = async (file: File | undefined) => {
    if (!file) return;
    setBusy(true);
    setNote("");
    try {
      const r = await observePet(file, file.name, grounding);
      if (!r.detected) {
        setAnalyzed(false);
        setNote(t("pet.noPetDetected"));
        return;
      }
      const desc = r.description ?? {};
      setAppearance(typeof desc.summary === "string" ? desc.summary : "");
      setSpecies(typeof desc.species === "string" ? desc.species : "");
      setCropB64(r.primaryCropB64);
      setHeadBbox(r.headBbox);
      setAnalyzed(true);
      setNote(r.candidates.length > 1 ? t("pet.multiPetHint") : "");
    } catch (e) {
      toast(e instanceof Error ? e.message : t("pet.observeFail"), "warn");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-[70] flex items-end md:items-center justify-center bg-black/40"
      onClick={busy ? undefined : onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        className="w-full max-w-md bg-bg-secondary border border-border rounded-t-2xl md:rounded-xl shadow-sm p-6 anim-in"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-title text-text-primary">{t("pet.autoGenTitle")}</h3>
          <button
            type="button"
            onClick={busy ? undefined : onClose}
            disabled={busy}
            className="rounded-full p-1 text-text-secondary hover:text-text-primary disabled:opacity-50"
            aria-label={t("pet.cancel")}
          >
            <IconX />
          </button>
        </div>
        <p className="text-caption text-text-tertiary mb-3">{t("pet.autoGenHint")}</p>

        {/* 感知模型预检状态：检测中 / 未配置 / 不可用 时拦下上传 */}
        {check !== "ok" && (
          <div
            className={`flex items-start gap-2 text-caption rounded-lg px-3 py-2 mb-3 ${
              check === "checking"
                ? "bg-bg-tertiary text-text-secondary"
                : "bg-error-bg text-error"
            }`}
          >
            {check !== "checking" && (
              <IconAlert width={16} height={16} className="shrink-0 mt-0.5" />
            )}
            <span>
              {check === "checking" && t("pet.modelChecking")}
              {check === "unconfigured" && t("pet.modelUnconfigured")}
              {check === "unavailable" &&
                t("pet.modelUnavailable", { msg: checkMsg || "—" })}
            </span>
          </div>
        )}

        <input
          ref={fileRef}
          type="file"
          accept="image/*,video/*"
          className="hidden"
          onChange={(e) => onPick(e.target.files?.[0])}
        />
        <button
          type="button"
          onClick={() => fileRef.current?.click()}
          disabled={busy || check !== "ok"}
          className="w-full py-2 rounded-lg bg-bg-primary border border-border text-text-secondary hover:text-text-primary disabled:opacity-60"
        >
          {busy
            ? t("pet.analyzing")
            : analyzed
              ? t("pet.reuploadMedia")
              : t("pet.uploadMedia")}
        </button>

        {note && <div className="text-caption text-text-tertiary mt-2">{note}</div>}

        {analyzed && (
          <div className="mt-4 pt-4 border-t border-border">
            {cropB64 && (
              <div className="flex justify-center mb-3">
                <img
                  src={`data:image/jpeg;base64,${cropB64}`}
                  alt=""
                  className="w-24 h-24 rounded-lg object-cover border border-border"
                />
              </div>
            )}
            <div className="text-caption text-text-secondary mb-1">
              {t("pet.drawerSpecies")}
            </div>
            <input
              value={species}
              onChange={(e) => setSpecies(e.target.value)}
              placeholder={t("pet.drawerSpeciesPlaceholder")}
              className="w-full px-3 py-2 rounded-lg bg-bg-primary border border-border focus:border-brand-primary focus:outline-none text-text-primary mb-3"
            />
            <div className="text-caption text-text-secondary mb-1">
              {t("pet.drawerAppearance")}
            </div>
            <textarea
              value={appearance}
              onChange={(e) => setAppearance(e.target.value)}
              rows={3}
              className="w-full px-3 py-2 rounded-lg bg-bg-primary border border-border focus:border-brand-primary focus:outline-none text-text-primary mb-2"
            />
            <div className="text-caption text-success mb-3">
              {t("pet.descriptionGenerated")}
            </div>
            <button
              type="button"
              onClick={() =>
                onDone({ appearance: appearance.trim(), species, cropB64, headBbox })
              }
              className="w-full py-2 rounded-lg bg-brand-primary text-white hover:bg-brand-accent"
            >
              {t("pet.useDescription")}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
