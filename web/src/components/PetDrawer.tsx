/**
 * 宠物编辑抽屉（镜像 PersonDrawer）——本抽屉即「编辑器」：
 * - 新增：名字 / 物种 / 外观描述 + 头像（点头像上传，或「自动生成外观描述」）；三者齐全才可保存。
 *   提交时建花名册 → 写外观为 member_persona → commit 渲染「## 宠物」段 → 传头像。
 * - 编辑（从宠物档案卡「编辑」进入）：名字 / 物种 / 头像即时可改，删除就在同屏。
 * 头像无论上传还是自动生成，都先经 AvatarCropEditor 手动确认/微调裁剪（grounding 头部作默认框），
 * 产物是裁好的方图 blob，走现有 avatar 上传端点（后端只存不裁）。
 * 外观（member_persona）新增时录入；编辑时回填其条目内容、保存时就地更新该条目并 commit。
 */
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { HomeEntries, Pet } from "@/lib/types";
import {
  addHomeEntry,
  commitHomeProfile,
  createPet,
  deletePet,
  updateHomeEntry,
  updatePet,
  uploadPetAvatar,
  uploadPetReferenceCrops,
} from "@/api";
import { PetAvatar } from "@/components/PetAvatar";
import { useEscClose } from "@/hooks/useEscClose";
import { IconCamera, IconCheck, IconTrash, IconX } from "@/lib/icons";
import { AvatarCropEditor } from "./AvatarCropEditor";
import { PetAutoGenFlow, type AutoGenDoneResult } from "./PetAutoGenFlow";
import { InfoNote } from "./InfoNote";
import { toast } from "./Toast";

/** 裸 base64（JPEG，无 data: 前缀）→ Blob，供参考 crop 上传。 */
function b64ToBlob(b64: string, type = "image/jpeg"): Blob {
  const bin = atob(b64);
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  return new Blob([arr], { type });
}

interface Props {
  pet: Pet | null; // null = 新增
  open: boolean;
  grounding: boolean; // features.petHeadGrounding
  entries?: HomeEntries; // 家庭档案，用于回填/更新宠物外观（member_persona）
  petCount?: number; // 现有宠物数（新增态 ≥3 时提示"建议不超过 3"——正好 3 只再加即超）
  onClose: () => void;
  onChanged: () => void;
}

// 待裁剪的源：上传的文件 或 自动生成的 crop（base64）+ 头部框
type CropSource = {
  source: { file: File } | { b64: string };
  initialBox: number[] | null;
};

export function PetDrawer({
  pet,
  open,
  grounding,
  entries,
  petCount = 0,
  onClose,
  onChanged,
}: Props) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [species, setSpecies] = useState("");
  const [appearance, setAppearance] = useState("");
  // 存量宠物外观所在的 member_persona 条目 id（保存时更新它）；null = 尚无外观条目。
  const [apprEntryId, setApprEntryId] = useState<string | null>(null);
  const [confirmingDel, setConfirmingDel] = useState(false);
  const [busy, setBusy] = useState(false);
  const [autoGen, setAutoGen] = useState<false | "register" | "append">(false);
  // 待保存的参考 crop（D6 候选全集）——新增时随保存整组 replace 落库；补充素材走 append 即时落库。
  const [refCrops, setRefCrops] = useState<{ cropB64: string; score: number }[]>([]);
  const [crop, setCrop] = useState<CropSource | null>(null);
  const [avatarBlob, setAvatarBlob] = useState<Blob | null>(null);
  const [avatarName, setAvatarName] = useState("avatar.jpg");
  const [avatarPreview, setAvatarPreview] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const guardedClose = busy ? () => {} : onClose;
  useEscClose(open && !busy && !autoGen && !crop, guardedClose);

  const isNew = pet == null;
  // 三项校验：名字/物种/外观齐全才可保存（新增与编辑一致）。
  const canSave = Boolean(name.trim() && species.trim() && appearance.trim());

  useEffect(() => {
    if (open) {
      setName(pet?.name ?? "");
      setSpecies(pet?.species ?? "");
      // 编辑存量宠物时，回填其外观（首个 member_persona 条目），并记住其 id 供保存时更新。
      const apprEntry = pet
        ? (entries?.profile ?? []).find(
            (e) => e.subjectId === pet.id && e.type === "member_persona",
          )
        : undefined;
      setAppearance(apprEntry?.content ?? "");
      setApprEntryId(apprEntry?.id ?? null);
      setConfirmingDel(false);
      setAutoGen(false);
      setRefCrops([]);
      setCrop(null);
      setAvatarBlob(null);
      setAvatarName("avatar.jpg");
    } else {
      // 关闭即清头像暂存（趁抽屉渲染 null、屏幕不可见时清）——否则编辑宠物 A 裁图后取消、
      // 再开 B 会先闪现 A 上次裁的图一帧。
      setCrop(null);
      setAvatarBlob(null);
    }
    // 仅在打开/切换宠物时回填；entries 变更不重置用户正在编辑的内容。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, pet]);

  // 裁好的头像 blob → 预览 objectURL（随 blob 变化重建 + 清理）
  useEffect(() => {
    if (!avatarBlob) {
      setAvatarPreview(null);
      return;
    }
    const url = URL.createObjectURL(avatarBlob);
    setAvatarPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [avatarBlob]);

  if (!open) return null;

  // 补充素材（D3）：存量宠物追加参考图——即时以 append 落库（不动描述/头像）。
  const onAppendMaterial = async (result: AutoGenDoneResult) => {
    setAutoGen(false);
    if (!pet || result.referenceCrops.length === 0) return;
    setBusy(true);
    try {
      await uploadPetReferenceCrops(
        pet.id,
        result.referenceCrops.map((c) => ({
          blob: b64ToBlob(c.cropB64),
          score: c.score,
        })),
        "append",
      );
      toast(t("pet.materialAppended", { count: result.referenceCrops.length }), "ok");
      onChanged();
    } catch (e) {
      toast(e instanceof Error ? e.message : t("pet.saveFail"), "warn");
    } finally {
      setBusy(false);
    }
  };

  if (autoGen) {
    return (
      <PetAutoGenFlow
        grounding={grounding}
        mode={autoGen}
        onClose={() => setAutoGen(false)}
        onDone={
          autoGen === "append"
            ? onAppendMaterial
            : ({ appearance: appr, species: sp, avatarCropB64, avatarHeadBbox, referenceCrops }) => {
                if (appr) setAppearance(appr);
                if (sp) setSpecies((cur) => (cur.trim() ? cur : sp)); // 物种自动回填（不覆盖已填）
                setRefCrops(referenceCrops); // D6：全部候选待保存时整组落库
                setAutoGen(false);
                if (avatarCropB64)
                  setCrop({ source: { b64: avatarCropB64 }, initialBox: avatarHeadBbox });
              }
        }
      />
    );
  }

  const pickAvatar = (file: File | undefined) => {
    if (fileRef.current) fileRef.current.value = ""; // 允许再次选同一文件
    if (!file) return;
    setCrop({ source: { file }, initialBox: null });
  };

  const submit = async () => {
    if (!canSave) return;
    setBusy(true);
    try {
      if (isNew) {
        const created = await createPet({
          name: name.trim(),
          species: species.trim(),
        });
        if (avatarBlob) await uploadPetAvatar(created.id, avatarBlob, avatarName);
        // D6：注册时把 observe 选出的候选全集整组落库为参考图（③ 多姿态参照图）
        if (refCrops.length > 0) {
          await uploadPetReferenceCrops(
            created.id,
            refCrops.map((c) => ({ blob: b64ToBlob(c.cropB64), score: c.score })),
            "replace",
          );
        }
        if (appearance.trim()) {
          await addHomeEntry({
            type: "member_persona",
            content: appearance.trim(),
            subjectId: created.id,
            subjectName: name.trim(),
          });
          await commitHomeProfile();
        }
      } else {
        await updatePet(pet.id, { name: name.trim(), species: species.trim() });
        if (avatarBlob) await uploadPetAvatar(pet.id, avatarBlob, avatarName);
        // 外观：更新已有 member_persona 条目（同步 subjectName 以防改名后展示名陈旧），
        // 无则新增；随后 commit 让「## 宠物」段重渲染。
        const appr = appearance.trim();
        if (apprEntryId) {
          await updateHomeEntry(apprEntryId, {
            content: appr,
            subjectName: name.trim(),
          });
          await commitHomeProfile();
        } else if (appr) {
          await addHomeEntry({
            type: "member_persona",
            content: appr,
            subjectId: pet.id,
            subjectName: name.trim(),
          });
          await commitHomeProfile();
        }
      }
      onChanged();
      onClose();
    } catch (e) {
      toast(e instanceof Error ? e.message : t("pet.saveFail"), "warn");
    } finally {
      setBusy(false);
    }
  };

  const doDelete = async () => {
    if (!pet) return;
    setBusy(true);
    try {
      await deletePet(pet.id);
      onChanged();
      onClose();
    } catch (e) {
      toast(e instanceof Error ? e.message : t("pet.deleteFail"), "warn");
    } finally {
      setBusy(false);
    }
  };

  const avatarInner = avatarPreview ? (
    <img src={avatarPreview} alt="" className="w-full h-full object-cover" />
  ) : !isNew ? (
    <PetAvatar pet={pet} size={96} />
  ) : (
    <span
      className="w-full h-full flex items-center justify-center"
      style={{ background: "var(--color-bg-tertiary)", fontSize: 40 }}
    >
      🐾
    </span>
  );

  return (
    <>
      <div
        className="fixed inset-0 z-[60] flex items-end md:items-center justify-center bg-black/40"
        onClick={guardedClose}
      >
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="pet-drawer-title"
          className="w-full max-w-md bg-bg-secondary border border-border rounded-t-2xl md:rounded-xl shadow-sm p-6 anim-in"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="flex items-center justify-between mb-4">
            <h3 id="pet-drawer-title" className="text-title text-text-primary">
              {isNew ? t("pet.addPet") : pet.name}
            </h3>
            <button
              type="button"
              onClick={guardedClose}
              disabled={busy}
              className="rounded-full p-1 text-text-secondary hover:text-text-primary disabled:opacity-50"
              aria-label={t("pet.cancel")}
            >
              <IconX />
            </button>
          </div>

          {isNew && petCount >= 3 && (
            <InfoNote className="mb-4">{t("pet.countHint")}</InfoNote>
          )}

          {/* 头像：hover 出相机蒙版点击上传（走裁剪）；新增态另有「自动生成外观描述」 */}
          <div className="flex flex-col items-center gap-2 mb-4">
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={(e) => pickAvatar(e.target.files?.[0])}
            />
            <button
              type="button"
              onClick={() => fileRef.current?.click()}
              aria-label={t("pet.changeAvatar")}
              title={t("pet.changeAvatar")}
              className="relative group w-24 h-24 rounded-full overflow-hidden border border-border"
            >
              {avatarInner}
              <span className="absolute inset-0 flex items-center justify-center bg-black/45 text-white opacity-0 group-hover:opacity-100 transition-opacity">
                <IconCamera width={22} height={22} />
              </span>
            </button>
            <button
              type="button"
              onClick={() => setAutoGen(isNew ? "register" : "append")}
              className="text-body px-4 py-2 rounded-lg bg-bg-primary border border-border text-text-secondary hover:text-text-primary hover:border-border-strong transition-colors"
            >
              {isNew ? t("pet.autoGenTitle") : t("pet.addMaterial")}
            </button>
          </div>

          {/* 基本信息（始终可编辑）：名字 / 物种；外观仅新增时录入 */}
          <div className="space-y-3 mb-4">
            <Field label={t("pet.drawerName")}>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={t("pet.drawerNamePlaceholder")}
                autoFocus
                className="w-full px-3 py-2 rounded-lg bg-bg-primary border border-border focus:border-brand-primary focus:outline-none text-text-primary"
              />
            </Field>
            <Field label={t("pet.drawerSpecies")}>
              <input
                value={species}
                onChange={(e) => setSpecies(e.target.value)}
                placeholder={t("pet.drawerSpeciesPlaceholder")}
                className="w-full px-3 py-2 rounded-lg bg-bg-primary border border-border focus:border-brand-primary focus:outline-none text-text-primary"
              />
            </Field>
            <Field label={t("pet.drawerAppearance")}>
              <textarea
                value={appearance}
                onChange={(e) => setAppearance(e.target.value)}
                placeholder={t("pet.drawerAppearancePlaceholder")}
                rows={3}
                className="w-full px-3 py-2 rounded-lg bg-bg-primary border border-border focus:border-brand-primary focus:outline-none text-text-primary"
              />
            </Field>
          </div>

          {/* 删除二次确认 */}
          {confirmingDel && (
            <div className="rounded-lg bg-error-bg border border-error p-3 mb-3">
              <div className="text-error text-center mb-2.5">
                {t("pet.confirmDeletePet", { name: pet?.name })}
              </div>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => setConfirmingDel(false)}
                  disabled={busy}
                  className="flex-1 py-2 rounded-lg bg-bg-secondary border border-border text-text-primary disabled:opacity-60"
                >
                  {t("pet.cancel")}
                </button>
                <button
                  type="button"
                  onClick={doDelete}
                  disabled={busy}
                  className="flex-1 py-2 rounded-lg bg-error text-white hover:opacity-90 disabled:opacity-60"
                >
                  {busy ? t("pet.deleting") : t("pet.confirmDelete")}
                </button>
              </div>
            </div>
          )}

          {/* 动作：取消 / 保存一行；存量宠物的删除在分隔线下弱化呈现 */}
          {!confirmingDel && (
            <div className="flex flex-col">
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={guardedClose}
                  disabled={busy}
                  className="flex-1 py-2 rounded-lg bg-bg-primary border border-border text-text-secondary disabled:opacity-60"
                >
                  {t("pet.cancel")}
                </button>
                <button
                  type="button"
                  onClick={submit}
                  disabled={!canSave || busy}
                  className="flex-1 py-2 rounded-lg bg-brand-primary text-white hover:bg-brand-accent disabled:opacity-60"
                >
                  <IconCheck className="inline mr-1" />
                  {busy ? t("pet.saving") : t("pet.save")}
                </button>
              </div>
              {!isNew && (
                <div className="mt-4 pt-3 border-t border-border">
                  <button
                    type="button"
                    onClick={() => setConfirmingDel(true)}
                    disabled={busy}
                    className="w-full py-2 rounded-lg text-error hover:bg-error-bg disabled:opacity-60 flex items-center justify-center gap-1.5"
                  >
                    <IconTrash width={16} height={16} />
                    {t("pet.delete")}
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {crop && (
        <AvatarCropEditor
          source={crop.source}
          initialBox={crop.initialBox}
          onCancel={() => setCrop(null)}
          onConfirm={(blob) => {
            setAvatarBlob(blob);
            setAvatarName("avatar.jpg");
            setCrop(null);
          }}
        />
      )}
    </>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="text-caption text-text-secondary mb-1">{label}</div>
      {children}
    </div>
  );
}
