/**
 * 宠物「自动生成外观描述」流程（镜像 EnrollFlow 的上传→处理→结果，但产物是文本 + 多姿态参考图）。
 * 进入即先做一次感知模型连通性预检（getOmniConfig + testOmniConfig），未配置/不可用则直接拦下。
 * 上传 1~3 张图 或 1 段视频 → 调 observePet（后端门控选 ≤3 张同一只 crop + omni 一次成型共性描述）→
 * 用户编辑确认 → onDone 回传外观文本 + 物种 + 头像主图 crop + 该 crop 头部框 + 全部候选参考图（D6）。
 * 无副作用：本组件只观察生成，不写库；落库/裁剪/上传参考图由 PetDrawer 负责。
 */
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { PetObserveCandidate, PetObserveWarning } from "@/lib/types";
import { getOmniConfig, observePet, testOmniConfig } from "@/api";
import { useEscClose } from "@/hooks/useEscClose";
import { IconAlert, IconX } from "@/lib/icons";
import { ImageZoom } from "./ImageZoom";
import { InfoNote } from "./InfoNote";
import { Spinner } from "./Spinner";
import { toast } from "./Toast";

const MAX_IMAGES = 3;
const VIDEO_RE = /\.(mp4|webm|mov|avi|mkv)$/i;
const isVideoFile = (f: File) => f.type.startsWith("video") || VIDEO_RE.test(f.name);

/** 候选的绝对质量分（conf×sharpness×area_ratio），供后端 append 按分留 top-3。 */
function candidateScore(c: PetObserveCandidate): number {
  return (c.conf ?? 0) * (c.sharpness ?? 0) * (c.areaRatio ?? 0);
}

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

/** 读单张图片文件为 dataURL（预览用）。 */
function imageDataUrl(file: File): Promise<string> {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () =>
      resolve(typeof reader.result === "string" ? reader.result : "");
    reader.onerror = () => resolve("");
    reader.readAsDataURL(file);
  });
}

export interface AutoGenDoneResult {
  appearance: string;
  species: string;
  avatarCropB64: string; // 主图（头像裁剪源）
  avatarHeadBbox: number[] | null;
  referenceCrops: { cropB64: string; score: number }[]; // D6：全部候选（可空=回退无参考图）
}

interface Props {
  /** 是否要头部 grounding（取 features.petHeadGrounding） */
  grounding: boolean;
  /** register=新增注册（默认，可改描述）；append=存量宠物补充素材（只加参考图、不动描述/头像） */
  mode?: "register" | "append";
  onClose: () => void;
  onDone: (result: AutoGenDoneResult) => void;
}

type CheckState = "checking" | "ok" | "unconfigured" | "unavailable";

export function PetAutoGenFlow({
  grounding,
  mode = "register",
  onClose,
  onDone,
}: Props) {
  const { t } = useTranslation();
  const isAppend = mode === "append";
  const [busy, setBusy] = useState(false);
  const [analyzed, setAnalyzed] = useState(false);
  const [appearance, setAppearance] = useState("");
  const [species, setSpecies] = useState("");
  const [candidates, setCandidates] = useState<PetObserveCandidate[]>([]);
  // 整幅回退合成的兜底候选（后端 candidates=[]、只有整幅 primary_crop）：可作头像/预览，但**不入参考图**
  // （后端该路径明示"不产参考 crop"，整幅未裁图不该污染识别参照）。
  const [synthesized, setSynthesized] = useState(false);
  const [sel, setSel] = useState(0); // 选中/主图候选下标
  const [srcPreviews, setSrcPreviews] = useState<string[]>([]); // 观测图（每个上传源）
  const [warnings, setWarnings] = useState<PetObserveWarning[]>([]);
  const [refsInconsistent, setRefsInconsistent] = useState(false);
  const [refsAck, setRefsAck] = useState(false);
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

  const onPick = async (fileList: FileList | null) => {
    const files = fileList ? Array.from(fileList) : [];
    if (files.length === 0) return;
    // D2 客户端校验：>3 张 / 视频与图混选 / 多个视频 → 提示重选，不发起 observe
    const videos = files.filter(isVideoFile);
    if (videos.length > 0 && files.length > 1) {
      toast(t("pet.videoNotMixed"), "warn");
      return;
    }
    if (videos.length === 0 && files.length > MAX_IMAGES) {
      toast(t("pet.tooManyFiles", { max: MAX_IMAGES }), "warn");
      return;
    }
    setBusy(true);
    setNote("");
    // 观测图预览（纯客户端）：图片=所选图，视频=首帧
    const previews = await Promise.all(
      files.map((f) => (isVideoFile(f) ? firstFrameDataUrl(f) : imageDataUrl(f))),
    ).catch(() => files.map(() => ""));
    setSrcPreviews(previews);
    try {
      const r = await observePet(files, grounding);
      if (!r.detected) {
        setAnalyzed(false);
        setCandidates([]);
        setNote(t("pet.noPetDetected"));
        return;
      }
      const desc = r.description ?? {};
      setAppearance(typeof desc.summary === "string" ? desc.summary : "");
      setSpecies(typeof desc.species === "string" ? desc.species : "");
      // 候选：优先用后端候选全集（D6）；回退无候选时用 primary crop 兜底作单个主图
      const synth = r.candidates.length === 0;
      const cands: PetObserveCandidate[] = synth
        ? [
            {
              trackId: null,
              speciesGuess: "",
              cropB64: r.primaryCropB64,
              headBbox: r.headBbox,
            },
          ]
        : r.candidates;
      setSynthesized(synth);
      setCandidates(cands);
      setSel(synth ? 0 : Math.min(r.primaryIndex, cands.length - 1));
      setWarnings(r.warnings ?? []);
      setRefsInconsistent(Boolean(r.refsInconsistent));
      setRefsAck(false);
      setAnalyzed(true);
    } catch (e) {
      toast(e instanceof Error ? e.message : t("pet.observeFail"), "warn");
    } finally {
      setBusy(false);
    }
  };

  const confirm = () => {
    const primary = candidates[sel];
    if (!primary) return;
    onDone({
      appearance: appearance.trim(),
      species,
      avatarCropB64: primary.cropB64,
      avatarHeadBbox: primary.headBbox ?? null,
      // D6：候选全集作参考图。整幅回退合成的兜底候选**不入**参考图（后端"不产参考 crop"），只留作头像。
      referenceCrops: synthesized
        ? []
        : candidates.map((c) => ({ cropB64: c.cropB64, score: candidateScore(c) })),
    });
  };

  // 观测图仅在能可靠映射到候选时展示：单一源（视频/单图，全部候选共享）或候选数==源数（多图 1:1）；
  // 否则（多图但部分被门控丢弃，映射不可靠）不展示观测图、只展示样本 crop，避免张冠李戴。
  const srcReliable =
    srcPreviews.length === 1 || srcPreviews.length === candidates.length;
  const srcFor = (i: number) =>
    (!srcReliable ? "" : srcPreviews.length === 1 ? srcPreviews[0] : srcPreviews[i]) ??
    "";
  const canConfirm = analyzed && candidates.length > 0 && (!refsInconsistent || refsAck);

  return (
    <div
      className="fixed inset-0 z-[70] flex items-end md:items-center justify-center bg-black/40"
      onClick={busy ? undefined : onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        className="w-full max-w-md bg-bg-secondary border border-border rounded-t-2xl md:rounded-xl shadow-sm p-6 anim-in max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-title text-text-primary">
            {isAppend ? t("pet.addMaterial") : t("pet.autoGenTitle")}
          </h3>
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
        <p className="text-caption text-text-tertiary mb-3">
          {isAppend ? t("pet.addMaterialHint") : t("pet.autoGenHint")}
        </p>

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
          multiple
          className="hidden"
          onChange={(e) => {
            onPick(e.target.files);
            e.target.value = ""; // 允许再次选同一批
          }}
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
              : t("pet.uploadMediaMulti")}
        </button>

        {/* D5：候选选项卡（左）+ 选中项并排预览 [观测图 | 样本]（右）。全部候选都会作参考图（D6）。 */}
        {candidates.length > 0 && (
          <div
            className={`mt-3 transition-all ${busy ? "opacity-40 grayscale" : ""}`}
          >
            <div className="flex items-start gap-3">
              {candidates.length > 1 && (
                <div className="flex flex-col gap-1.5 shrink-0">
                  {candidates.map((c, i) => (
                    <button
                      key={i}
                      type="button"
                      onClick={() => setSel(i)}
                      className={`relative rounded-md overflow-hidden border-2 ${
                        i === sel ? "border-brand-primary" : "border-border"
                      }`}
                      aria-label={t("pet.candidateNth", { n: i + 1 })}
                    >
                      <img
                        src={`data:image/jpeg;base64,${c.cropB64}`}
                        alt=""
                        className="h-12 w-12 object-cover"
                      />
                      {i === sel && (
                        <span className="absolute bottom-0 inset-x-0 bg-brand-primary text-white text-[10px] leading-tight text-center">
                          {t("pet.primaryBadge")}
                        </span>
                      )}
                    </button>
                  ))}
                </div>
              )}
              <div className="flex items-start justify-center gap-4 flex-1">
                {srcFor(sel) && (
                  <figure className="flex flex-col items-center gap-1">
                    <ImageZoom
                      src={srcFor(sel)}
                      thumbClass="h-24 w-auto max-w-[8rem] rounded-lg border border-border object-contain"
                    />
                    <figcaption className="text-caption text-text-tertiary">
                      {t("pet.previewSource")}
                    </figcaption>
                  </figure>
                )}
                <figure className="flex flex-col items-center gap-1">
                  <ImageZoom
                    src={`data:image/jpeg;base64,${candidates[sel].cropB64}`}
                    thumbClass="h-24 w-auto max-w-[8rem] rounded-lg border border-border object-contain"
                  />
                  <figcaption className="text-caption text-text-tertiary">
                    {t("pet.previewDetected")}
                  </figcaption>
                </figure>
              </div>
            </div>
            <div className="text-caption text-text-tertiary mt-2">
              {t("pet.candidatesHint", { count: candidates.length })}
            </div>
          </div>
        )}

        {note && <div className="text-caption text-text-tertiary mt-2">{note}</div>}

        {/* D4：warnings 黄叹号卡（建议类不阻断）。refs_inconsistent 交由下方软确认单独承载，不重复显示。 */}
        {warnings.filter((w) => w.type !== "refs_inconsistent").length > 0 && (
          <div className="mt-3 space-y-1.5">
            {warnings
              .filter((w) => w.type !== "refs_inconsistent")
              .map((w, i) => (
              <div
                key={i}
                className="flex items-start gap-2 text-caption rounded-lg bg-warning-bg border border-warning text-warning px-3 py-2"
              >
                <IconAlert width={16} height={16} className="shrink-0 mt-0.5" />
                <span>{t(`pet.warn.${w.type}`, { defaultValue: w.message })}</span>
              </div>
            ))}
          </div>
        )}

        {/* D4：refs_inconsistent 二次软确认（勾选后才允许使用） */}
        {refsInconsistent && (
          <label className="flex items-start gap-2 mt-3 text-caption text-warning cursor-pointer">
            <input
              type="checkbox"
              checked={refsAck}
              onChange={(e) => setRefsAck(e.target.checked)}
              className="mt-0.5"
            />
            <span>{t("pet.refsInconsistentConfirm")}</span>
          </label>
        )}

        {!isAppend && <InfoNote className="mt-3">{t("pet.poseGuide")}</InfoNote>}

        {analyzed && candidates.length > 0 && (
          <div
            className={`mt-4 pt-4 border-t border-border transition-all ${
              busy ? "opacity-40 grayscale pointer-events-none" : ""
            }`}
          >
            {!isAppend && (
              <>
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
              </>
            )}
            <button
              type="button"
              onClick={confirm}
              disabled={!canConfirm}
              className="w-full py-2 rounded-lg bg-brand-primary text-white hover:bg-brand-accent disabled:opacity-60"
            >
              {isAppend ? t("pet.useMaterial") : t("pet.useDescription")}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
