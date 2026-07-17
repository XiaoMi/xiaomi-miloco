/**
 * 家人详情抽屉。
 * - 创建模式：新增家人（仅名字 + 家庭角色）
 * - 编辑模式：改名 / 换头像（悬停→裁剪→保存时上传）/ 恢复默认头像 / 删（二次确认）/ 录入身份
 *
 * 头像：显式头像落 avatars/persons/<id>.<ext>（展示层，与识别数据分离）；上传经
 * AvatarCropEditor 裁 256×256，随「保存」提交。「恢复默认头像」清显式头像→读取回落
 * tier_a face[0]。头像变更仅编辑态可提交（picking/restore 会进入编辑态露出保存按钮）。
 */

import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { PerceptionCamera, Person } from "@/lib/types";
import {
  createPerson,
  deletePerson,
  deletePersonAvatar,
  updatePerson,
  uploadPersonAvatar,
} from "@/api";
import { PersonAvatar } from "@/components/PersonAvatar";
import { useEscClose } from "@/hooks/useEscClose";
import { IconCamera, IconCheck, IconX } from "@/lib/icons";
import { AvatarCropEditor } from "./AvatarCropEditor";
import { EnrollFlow } from "./EnrollFlow";
import { toast } from "./Toast";

interface Props {
  person: Person | null; // null = 新增模式
  open: boolean;
  // 打开即直接进入身份录入（成员档案头部「录入身份」入口用）；仅对未录入成员生效。
  startEnrolling?: boolean;
  cameras: PerceptionCamera[];
  onClose: () => void;
  onChanged: () => void;
}

export function PersonDrawer({
  person,
  open,
  startEnrolling = false,
  cameras,
  onClose,
  onChanged,
}: Props) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [role, setRole] = useState("");
  const [editing, setEditing] = useState(false);
  const [enrolling, setEnrolling] = useState(false);
  const [confirmingDel, setConfirmingDel] = useState(false);
  const [busy, setBusy] = useState(false);
  // 头像暂存：avatarBlob=待上传新图；removeAvatar=待清显式头像（恢复默认）。二者互斥，随保存提交。
  const [avatarBlob, setAvatarBlob] = useState<Blob | null>(null);
  const [avatarPreview, setAvatarPreview] = useState<string | null>(null);
  const [removeAvatar, setRemoveAvatar] = useState(false);
  const [crop, setCrop] = useState<{ file: File } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  // submit/delete 跑期间挡关闭:scrim/ESC/X 都禁,避免 dialog 关掉但 await 还在跑
  // → 成功 reload 时住户已退出看到莫名刷新,失败 toast 又弹到无 dialog 上下文。
  const guardedClose = busy ? () => {} : onClose;
  useEscClose(open && !busy && !crop, guardedClose);

  useEffect(() => {
    if (open) {
      setName(person?.name ?? "");
      setRole(person?.role ?? "");
      setEditing(person == null); // 新增模式默认编辑
      // 从档案头部「录入身份 / 补充身份样本」入口打开即进流程；新增模式不触发。
      setEnrolling(!!person && startEnrolling);
      setConfirmingDel(false);
      setAvatarBlob(null);
      setRemoveAvatar(false);
      setCrop(null);
    } else {
      setConfirmingDel(false);
    }
  }, [open, person, startEnrolling]);

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

  // 录入态只显示 EnrollFlow 单层，不在它身后再叠一层 PersonDrawer 弹窗。
  // 直达录入（startEnrolling）下取消 / 完成都整体关闭，回到档案面板；
  // 从编辑弹窗进入的则退回编辑弹窗。
  if (enrolling && person) {
    return (
      <EnrollFlow
        person={person}
        cameras={cameras}
        onClose={() => (startEnrolling ? onClose() : setEnrolling(false))}
        onDone={() => {
          setEnrolling(false);
          onChanged();
          if (startEnrolling) onClose();
        }}
      />
    );
  }

  const isNew = person == null;

  const pickAvatar = (file: File | undefined) => {
    if (fileRef.current) fileRef.current.value = ""; // 允许再次选同一文件
    if (!file) return;
    setCrop({ file });
  };

  const restoreDefault = () => {
    setAvatarBlob(null);
    setRemoveAvatar(true);
    setEditing(true); // 露出「保存」以提交
  };

  const submit = async () => {
    if (!name.trim()) return;
    setBusy(true);
    try {
      if (isNew) {
        await createPerson({ name: name.trim(), role: role.trim() || undefined });
      } else {
        await updatePerson(person.id, {
          name: name.trim(),
          // 发空串 = 显式清空家庭角色；后端按是否带 role 字段区分"未传(不改)"与"传空(清空)"
          role: role.trim(),
        });
        // 头像变更随保存提交：新图上传，或恢复默认（清显式头像→回落 face[0]）。
        if (avatarBlob) await uploadPersonAvatar(person.id, avatarBlob, "avatar.jpg");
        else if (removeAvatar) await deletePersonAvatar(person.id);
      }
      onChanged();
      onClose();
    } catch (e) {
      toast(e instanceof Error ? e.message : t("family.saveFail"), "warn");
    } finally {
      setBusy(false);
    }
  };

  const doDelete = async () => {
    if (!person) return;
    setBusy(true);
    try {
      await deletePerson(person.id);
      onChanged();
      onClose();
    } catch (e) {
      toast(e instanceof Error ? e.message : t("family.deleteFail"), "warn");
    } finally {
      setBusy(false);
    }
  };

  // 头像内容：待上传新图预览 > （恢复默认时按清空后状态预览）> 当前头像。
  const avatarInner = avatarPreview ? (
    <img src={avatarPreview} alt="" className="w-full h-full object-cover" />
  ) : person ? (
    <PersonAvatar
      person={removeAvatar ? { ...person, avatarExt: null } : person}
      size={96}
    />
  ) : null;

  return (
    <>
      <div
        className="fixed inset-0 z-[60] flex items-end md:items-center justify-center bg-black/40"
        onClick={guardedClose}
      >
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="person-drawer-title"
          className="w-full max-w-md bg-bg-secondary border border-border rounded-t-2xl md:rounded-xl shadow-sm p-6 anim-in"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="flex items-center justify-between mb-4">
            <h3
              id="person-drawer-title"
              className="text-title text-text-primary"
            >
              {isNew ? t("family.addPerson") : person.name}
            </h3>
            <button
              type="button"
              onClick={guardedClose}
              disabled={busy}
              className="rounded-full p-1 text-text-secondary hover:text-text-primary disabled:opacity-50"
              aria-label={t("family.close")}
            >
              <IconX />
            </button>
          </div>

          {/* 头像：hover 出相机蒙版点击上传（走裁剪）；有显式头像时可「恢复默认」 */}
          {!isNew && person && (
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
                aria-label={t("family.changeAvatar")}
                title={t("family.changeAvatar")}
                className="relative group w-24 h-24 rounded-full overflow-hidden border border-border"
              >
                {avatarInner}
                <span className="absolute inset-0 flex items-center justify-center bg-black/45 text-white opacity-0 group-hover:opacity-100 transition-opacity">
                  <IconCamera width={22} height={22} />
                </span>
              </button>
              {person.avatarExt && !avatarBlob && !removeAvatar && (
                <button
                  type="button"
                  onClick={restoreDefault}
                  className="text-caption text-text-secondary hover:text-text-primary"
                >
                  {t("family.restoreDefaultAvatar")}
                </button>
              )}
            </div>
          )}

          {/* 基本信息 / 编辑 */}
          {editing ? (
            <div className="space-y-3 mb-4">
              <Field label={t("family.drawerName")}>
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder={t("family.drawerNamePlaceholder")}
                  autoFocus
                  className="w-full px-3 py-2 rounded-lg bg-bg-primary border border-border focus:border-brand-primary focus:outline-none text-text-primary"
                />
              </Field>
              <Field label={t("family.drawerRole")}>
                <input
                  value={role}
                  onChange={(e) => setRole(e.target.value)}
                  placeholder={t("family.drawerRolePlaceholder")}
                  className="w-full px-3 py-2 rounded-lg bg-bg-primary border border-border focus:border-brand-primary focus:outline-none text-text-primary"
                />
              </Field>
            </div>
          ) : (
            person && (
              <div className="text-center mb-4">
                <div className="text-text-primary">{person.name}</div>
                {person.role && (
                  <div className="text-caption text-text-secondary">
                    {person.role}
                  </div>
                )}
              </div>
            )
          )}

          {/* 删除二次确认态 */}
          {confirmingDel && (
            <div className="rounded-lg bg-error-bg border border-error p-3 mb-3">
              <div className="text-error text-center mb-2.5">
                {t("family.confirmDeletePerson", { name: person?.name })}
              </div>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => setConfirmingDel(false)}
                  disabled={busy}
                  className="flex-1 py-2 rounded-lg bg-bg-secondary border border-border text-text-primary disabled:opacity-60"
                >
                  {t("family.cancel")}
                </button>
                <button
                  type="button"
                  onClick={doDelete}
                  disabled={busy}
                  className="flex-1 py-2 rounded-lg bg-error text-white hover:opacity-90 disabled:opacity-60"
                >
                  {busy ? t("family.deleting") : t("family.confirmDelete")}
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
                        // 退出编辑态时把 name/role/头像暂存回滚，
                        // 避免下次再进编辑看到上次未保存的脏值
                        setName(person?.name ?? "");
                        setRole(person?.role ?? "");
                        setAvatarBlob(null);
                        setRemoveAvatar(false);
                        setEditing(false);
                      }
                    }}
                    className="flex-1 py-2 rounded-lg bg-bg-primary border border-border text-text-secondary"
                  >
                    {t("family.cancel")}
                  </button>
                  <button
                    type="button"
                    onClick={submit}
                    disabled={!name.trim() || busy}
                    className="flex-1 py-2 rounded-lg bg-brand-primary text-white hover:bg-brand-accent disabled:opacity-60"
                  >
                    <IconCheck className="inline mr-1" />
                    {busy ? t("family.saving") : t("family.save")}
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
                      {t("family.rename")}
                    </button>
                    <button
                      type="button"
                      onClick={() => setConfirmingDel(true)}
                      className="flex-1 py-2 rounded-lg bg-bg-primary border border-border text-error hover:bg-error-bg"
                    >
                      {t("family.delete")}
                    </button>
                  </div>
                )
              )}
            </div>
          )}
        </div>
      </div>

      {crop && (
        <AvatarCropEditor
          source={crop}
          initialBox={null}
          onCancel={() => setCrop(null)}
          onConfirm={(blob) => {
            setAvatarBlob(blob);
            setRemoveAvatar(false);
            setCrop(null);
            setEditing(true); // 露出「保存」
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
