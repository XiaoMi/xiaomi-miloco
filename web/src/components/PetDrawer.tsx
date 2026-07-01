/**
 * 宠物详情抽屉（镜像 PersonDrawer）。
 * - 新增：名字 / 物种 / 外观描述 + 头像（上传或「自动生成」）；提交时建花名册 →
 *   写外观为 member_persona → commit 渲染「## 宠物」段 → 传头像。
 * - 编辑：改名 / 物种 / 换头像 / 删（二次确认，后端联动清档案条目）。
 * 外观仅在新增时录入（编辑外观走档案面板 / 重新自动生成），避免在此追踪条目 id。
 */
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { Pet } from "@/lib/types";
import {
  addHomeEntry,
  commitHomeProfile,
  createPet,
  deletePet,
  updatePet,
  uploadPetAvatar,
} from "@/api";
import { PetAvatar } from "@/components/PetAvatar";
import { useEscClose } from "@/hooks/useEscClose";
import { IconCheck, IconX } from "@/lib/icons";
import { PetAutoGenFlow } from "./PetAutoGenFlow";
import { toast } from "./Toast";

interface Props {
  pet: Pet | null; // null = 新增
  open: boolean;
  grounding: boolean; // features.petHeadGrounding
  onClose: () => void;
  onChanged: () => void;
}

function b64ToBlob(b64: string, type = "image/jpeg"): Blob {
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  return new Blob([bytes], { type });
}

export function PetDrawer({ pet, open, grounding, onClose, onChanged }: Props) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [species, setSpecies] = useState("");
  const [appearance, setAppearance] = useState("");
  const [editing, setEditing] = useState(false);
  const [confirmingDel, setConfirmingDel] = useState(false);
  const [busy, setBusy] = useState(false);
  const [autoGen, setAutoGen] = useState(false);
  const [avatarBlob, setAvatarBlob] = useState<Blob | null>(null);
  const [avatarName, setAvatarName] = useState("avatar.jpg");
  const [avatarPreview, setAvatarPreview] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const guardedClose = busy ? () => {} : onClose;
  useEscClose(open && !busy && !autoGen, guardedClose);

  const isNew = pet == null;

  useEffect(() => {
    if (open) {
      setName(pet?.name ?? "");
      setSpecies(pet?.species ?? "");
      setAppearance("");
      setEditing(pet == null);
      setConfirmingDel(false);
      setAutoGen(false);
      setAvatarBlob(null);
      setAvatarName("avatar.jpg");
    }
  }, [open, pet]);

  // 上传 / 裁剪的头像 blob → 预览 objectURL（随 blob 变化重建 + 清理）
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

  if (autoGen) {
    return (
      <PetAutoGenFlow
        grounding={grounding}
        onClose={() => setAutoGen(false)}
        onDone={({ appearance: appr, cropB64 }) => {
          if (appr) setAppearance(appr);
          if (cropB64) {
            setAvatarBlob(b64ToBlob(cropB64));
            setAvatarName("avatar.jpg");
          }
          setAutoGen(false);
        }}
      />
    );
  }

  const pickAvatar = (file: File | undefined) => {
    if (!file) return;
    setAvatarBlob(file);
    setAvatarName(file.name || "avatar.jpg");
  };

  const submit = async () => {
    if (!name.trim()) return;
    setBusy(true);
    try {
      if (isNew) {
        const created = await createPet({
          name: name.trim(),
          species: species.trim(),
        });
        if (avatarBlob) await uploadPetAvatar(created.id, avatarBlob, avatarName);
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

  return (
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

        {/* 头像 + 上传 / 自动生成 */}
        <div className="flex flex-col items-center gap-2 mb-4">
          {avatarPreview ? (
            <img
              src={avatarPreview}
              alt=""
              className="w-24 h-24 rounded-full object-cover border border-border"
            />
          ) : !isNew ? (
            <PetAvatar pet={pet} size={96} />
          ) : (
            <span
              className="w-24 h-24 rounded-full flex items-center justify-center"
              style={{ background: "var(--color-bg-tertiary)", fontSize: 40 }}
            >
              🐾
            </span>
          )}
          {editing && (
            <div className="flex gap-2">
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
                className="text-caption px-3 py-1 rounded-lg bg-bg-primary border border-border text-text-secondary hover:text-text-primary"
              >
                {t("pet.uploadAvatar")}
              </button>
              {isNew && (
                <button
                  type="button"
                  onClick={() => setAutoGen(true)}
                  className="text-caption px-3 py-1 rounded-lg bg-bg-primary border border-border text-text-secondary hover:text-text-primary"
                >
                  {t("pet.autoGenTitle")}
                </button>
              )}
            </div>
          )}
        </div>

        {/* 基本信息 / 编辑 */}
        {editing ? (
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
            {isNew && (
              <Field label={t("pet.drawerAppearance")}>
                <textarea
                  value={appearance}
                  onChange={(e) => setAppearance(e.target.value)}
                  placeholder={t("pet.drawerAppearancePlaceholder")}
                  rows={3}
                  className="w-full px-3 py-2 rounded-lg bg-bg-primary border border-border focus:border-brand-primary focus:outline-none text-text-primary"
                />
              </Field>
            )}
          </div>
        ) : (
          pet && (
            <div className="text-center mb-4">
              <div className="text-text-primary">{pet.name}</div>
              {pet.species && (
                <div className="text-caption text-text-secondary">{pet.species}</div>
              )}
            </div>
          )
        )}

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

        {/* 动作按钮 */}
        {!confirmingDel && (
          <div className="flex flex-col gap-2">
            {editing ? (
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => {
                    if (isNew) {
                      onClose();
                    } else {
                      setName(pet?.name ?? "");
                      setSpecies(pet?.species ?? "");
                      setAvatarBlob(null);
                      setEditing(false);
                    }
                  }}
                  className="flex-1 py-2 rounded-lg bg-bg-primary border border-border text-text-secondary"
                >
                  {t("pet.cancel")}
                </button>
                <button
                  type="button"
                  onClick={submit}
                  disabled={!name.trim() || busy}
                  className="flex-1 py-2 rounded-lg bg-brand-primary text-white hover:bg-brand-accent disabled:opacity-60"
                >
                  <IconCheck className="inline mr-1" />
                  {busy ? t("pet.saving") : t("pet.save")}
                </button>
              </div>
            ) : (
              !isNew && (
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => setEditing(true)}
                    className="flex-1 py-2 rounded-lg bg-bg-primary border border-border text-text-secondary hover:text-text-primary"
                  >
                    {t("pet.rename")}
                  </button>
                  <button
                    type="button"
                    onClick={() => setConfirmingDel(true)}
                    className="flex-1 py-2 rounded-lg bg-bg-primary border border-border text-error hover:bg-error-bg"
                  >
                    {t("pet.delete")}
                  </button>
                </div>
              )
            )}
          </div>
        )}
      </div>
    </div>
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
