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
import { ImageZoom } from "./ImageZoom";
import { InfoNote } from "./InfoNote";
import { Spinner } from "./Spinner";
import { toast } from "./Toast";

/**
 * 纯客户端提取视频首帧为 dataURL（不经后端）：<video> 加载元数据 → seek 到起始 →
 * 绘到离屏 canvas → toDataURL。失败则 reject，调用方降级为不显示预览。
 */
function firstFrameDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const video = document.createElement("video");
    video.muted = true;
    video.playsInline = true;
    video.preload = "auto";
    const finish = (result: string | null, err?: Error) => {
      URL.revokeObjectURL(url);
      result ? resolve(result) : reject(err ?? new Error("frame extract failed"));
    };
    video.onloadedmetadata = () => {
      try {
        video.currentTime = Math.min(0.1, (video.duration || 1) / 2);
      } catch {
        /* seek 不支持则等 onseeked/loadeddata 不触发，由 onerror 兜底 */
      }
    };
    video.onseeked = () => {
      try {
        const canvas = document.createElement("canvas");
        canvas.width = video.videoWidth || 320;
        canvas.height = video.videoHeight || 240;
        const ctx = canvas.getContext("2d");
        if (!ctx) return finish(null);
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
        finish(canvas.toDataURL("image/jpeg", 0.8));
      } catch (e) {
        finish(null, e as Error);
      }
    };
    video.onerror = () => finish(null, new Error("video decode failed"));
    video.src = url;
  });
}

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
  const [srcPreview, setSrcPreview] = useState(""); // 上传展示区：图片=所选图 / 视频=首帧
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
    // 上传展示区预览（纯客户端）：图片显示所选图，视频提取并显示首帧
    if (file.type.startsWith("video")) {
      firstFrameDataUrl(file)
        .then(setSrcPreview)
        .catch(() => setSrcPreview(""));
    } else {
      const reader = new FileReader();
      reader.onload = () =>
        setSrcPreview(typeof reader.result === "string" ? reader.result : "");
      reader.onerror = () => setSrcPreview("");
      reader.readAsDataURL(file);
    }
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
            {check === "checking" && <Spinner className="w-3.5 h-3.5 mt-0.5" />}
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
          className="w-full py-2 rounded-lg bg-bg-primary border border-border text-text-secondary hover:text-text-primary disabled:opacity-60 flex items-center justify-center gap-2"
        >
          {busy && <Spinner />}
          {busy
            ? t("pet.analyzing")
            : analyzed
              ? t("pet.reuploadMedia")
              : t("pet.uploadMedia")}
        </button>

        {srcPreview && (
          <div
            className={`mt-3 flex items-start justify-center gap-4 transition-all ${
              busy ? "opacity-40 grayscale" : ""
            }`}
          >
            <figure className="flex flex-col items-center gap-1">
              <ImageZoom
                src={srcPreview}
                thumbClass="h-24 w-auto max-w-[10rem] rounded-lg border border-border object-contain"
              />
              <figcaption className="text-caption text-text-tertiary">
                {t("pet.previewSource")}
              </figcaption>
            </figure>
            {cropB64 && (
              <figure className="flex flex-col items-center gap-1">
                <ImageZoom
                  src={`data:image/jpeg;base64,${cropB64}`}
                  thumbClass="h-24 w-auto max-w-[10rem] rounded-lg border border-border object-contain"
                />
                <figcaption className="text-caption text-text-tertiary">
                  {t("pet.previewDetected")}
                </figcaption>
              </figure>
            )}
          </div>
        )}

        {note && <div className="text-caption text-text-tertiary mt-2">{note}</div>}

        <InfoNote className="mt-3">{t("pet.poseGuide")}</InfoNote>

        {analyzed && (
          <div
            className={`mt-4 pt-4 border-t border-border transition-all ${
              busy ? "opacity-40 grayscale pointer-events-none" : ""
            }`}
          >
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
