/**
 * 宠物「自动生成外观」流程（镜像 EnrollFlow 的上传→处理→结果，但产物是文本）。
 * 上传图/视频 → 调 observePet（后端选最优 crop + omni 按维度生成描述）→ 用户编辑确认
 * → onDone 回传外观文本 + 选出的头像 crop（base64），由 PetDrawer 落库/设头像。
 * 无副作用：本组件只观察生成，不写库。
 */
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { observePet } from "@/api";
import { useEscClose } from "@/hooks/useEscClose";
import { IconX } from "@/lib/icons";
import { toast } from "./Toast";

interface Props {
  /** 是否要头部 grounding（取 features.petHeadGrounding） */
  grounding: boolean;
  onClose: () => void;
  onDone: (result: { appearance: string; cropB64: string }) => void;
}

export function PetAutoGenFlow({ grounding, onClose, onDone }: Props) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [analyzed, setAnalyzed] = useState(false);
  const [appearance, setAppearance] = useState("");
  const [cropB64, setCropB64] = useState("");
  const [note, setNote] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  useEscClose(!busy, onClose);

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
      const summary =
        typeof r.description?.summary === "string" ? r.description.summary : "";
      setAppearance(summary);
      setCropB64(r.primaryCropB64);
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
          disabled={busy}
          className="w-full py-2 rounded-lg bg-bg-primary border border-border text-text-secondary hover:text-text-primary disabled:opacity-60 mb-3"
        >
          {busy ? t("pet.analyzing") : t("pet.uploadMedia")}
        </button>

        {note && <div className="text-caption text-text-tertiary mb-2">{note}</div>}

        {analyzed && (
          <>
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
              onClick={() => onDone({ appearance: appearance.trim(), cropB64 })}
              className="w-full py-2 rounded-lg bg-brand-primary text-white hover:bg-brand-accent"
            >
              {t("pet.useDescription")}
            </button>
          </>
        )}
      </div>
    </div>
  );
}
