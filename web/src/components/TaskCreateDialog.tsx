import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { createCameraTask } from "@/api";
import { useEscClose } from "@/hooks/useEscClose";
import { feedDid } from "@/lib/cameraChannel";
import { IconX } from "@/lib/icons";
import type { ScopeCamera } from "@/lib/types";
import { toast } from "./Toast";

interface Props {
  cameras: ScopeCamera[];
  onClose: () => void;
  onCreated: () => void | Promise<void>;
}

function cameraKey(camera: ScopeCamera): string {
  return `${camera.did}:${camera.channel}`;
}

export function TaskCreateDialog({ cameras, onClose, onCreated }: Props) {
  const { t } = useTranslation();
  const selectableCameras = useMemo(
    () => cameras.filter((camera) => camera.inUse),
    [cameras],
  );
  const [description, setDescription] = useState("");
  const [query, setQuery] = useState("");
  const [actionDescription, setActionDescription] = useState(() =>
    t("tasks.createActionDefault"),
  );
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [busy, setBusy] = useState(false);

  useEscClose(!busy, onClose);

  const ready =
    description.trim().length > 0 &&
    query.trim().length > 0 &&
    actionDescription.trim().length > 0 &&
    selected.size > 0;

  const toggleCamera = (key: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const submit = async () => {
    if (!ready || busy) return;
    const perceiveDeviceIds = selectableCameras
      .filter((camera) => selected.has(cameraKey(camera)))
      .map((camera) =>
        feedDid(camera.did, camera.channel, camera.channelCount > 1),
      );
    if (perceiveDeviceIds.length === 0) return;

    setBusy(true);
    try {
      await createCameraTask({
        description: description.trim(),
        query: query.trim(),
        actionDescription: actionDescription.trim(),
        perceiveDeviceIds,
      });
    } catch (error) {
      toast(
        error instanceof Error ? error.message : t("family.operationFail"),
        "warn",
      );
      return;
    } finally {
      setBusy(false);
    }

    // 创建已经成功后先关表单，刷新失败也不能把用户留在可重复提交的状态。
    toast(t("tasks.created"), "ok");
    onClose();
    try {
      await onCreated();
    } catch (error) {
      toast(
        error instanceof Error ? error.message : t("family.operationFail"),
        "warn",
      );
    }
  };

  return (
    <div
      className="fixed inset-0 z-[70] flex items-end md:items-center justify-center bg-black/40 backdrop-blur-sm"
      onClick={(event) => {
        event.stopPropagation();
        if (!busy) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="task-create-title"
        className="flex w-full max-h-[90vh] flex-col bg-bg-secondary border border-border rounded-t-2xl md:max-w-lg md:rounded-2xl shadow-lg anim-in"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3 px-5 pt-5 pb-3">
          <div className="min-w-0">
            <h2
              id="task-create-title"
              className="text-title font-semibold text-text-primary"
            >
              {t("tasks.createTitle")}
            </h2>
            <p className="text-caption text-text-tertiary mt-1 leading-relaxed">
              {t("tasks.createIntro")}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            aria-label={t("family.close")}
            className="shrink-0 p-1.5 -mr-1.5 rounded-md text-text-tertiary hover:text-text-primary hover:bg-bg-tertiary transition-colors disabled:opacity-50"
          >
            <IconX width={18} height={18} />
          </button>
        </div>

        <form
          className="px-5 pb-5 overflow-y-auto space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <label className="block">
            <span className="block text-caption font-semibold text-text-secondary mb-1.5">
              {t("tasks.descriptionLabel")}
            </span>
            <input
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              maxLength={200}
              disabled={busy}
              placeholder={t("tasks.descPlaceholder")}
              className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-body text-text-primary placeholder:text-text-tertiary outline-none focus:border-brand-primary disabled:opacity-60"
            />
          </label>

          <label className="block">
            <span className="block text-caption font-semibold text-text-secondary mb-1.5">
              {t("tasks.triggerCondition")}
            </span>
            <textarea
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              disabled={busy}
              rows={3}
              placeholder={t("tasks.triggerPlaceholder")}
              className="w-full resize-y rounded-lg border border-border bg-bg-primary px-3 py-2 text-body text-text-primary placeholder:text-text-tertiary outline-none focus:border-brand-primary disabled:opacity-60"
            />
            <span className="block text-caption text-text-tertiary mt-1">
              {t("tasks.triggerPhrasingHint")}
            </span>
          </label>

          <fieldset disabled={busy}>
            <legend className="text-caption font-semibold text-text-secondary mb-1.5">
              {t("tasks.cameraLabel")}
            </legend>
            {selectableCameras.length === 0 ? (
              <div className="rounded-lg border border-border bg-bg-primary px-3 py-3 text-caption text-text-tertiary">
                {t("tasks.noActiveCameras")}
              </div>
            ) : (
              <div className="rounded-lg border border-border bg-bg-primary divide-y divide-border">
                {selectableCameras.map((camera) => {
                  const key = cameraKey(camera);
                  const suffix =
                    camera.channelCount > 1
                      ? ` · ${t("tasks.cameraChannel", { channel: camera.channel + 1 })}`
                      : "";
                  return (
                    <label
                      key={key}
                      className="flex items-center gap-2.5 px-3 py-2.5 cursor-pointer"
                    >
                      <input
                        type="checkbox"
                        checked={selected.has(key)}
                        onChange={() => toggleCamera(key)}
                        className="accent-brand-primary"
                      />
                      <span className="min-w-0 text-body text-text-primary truncate">
                        {camera.roomName ? `${camera.roomName} · ` : ""}
                        {camera.name}
                        {suffix}
                      </span>
                    </label>
                  );
                })}
              </div>
            )}
            {selected.size === 0 && selectableCameras.length > 0 && (
              <span className="block text-caption text-text-tertiary mt-1">
                {t("tasks.cameraRequired")}
              </span>
            )}
          </fieldset>

          <label className="block">
            <span className="block text-caption font-semibold text-text-secondary mb-1.5">
              {t("tasks.actionDescriptionLabel")}
            </span>
            <textarea
              value={actionDescription}
              onChange={(event) => setActionDescription(event.target.value)}
              disabled={busy}
              rows={2}
              placeholder={t("tasks.actionDescriptionPlaceholder")}
              className="w-full resize-y rounded-lg border border-border bg-bg-primary px-3 py-2 text-body text-text-primary placeholder:text-text-tertiary outline-none focus:border-brand-primary disabled:opacity-60"
            />
          </label>

          <div className="flex justify-end gap-2 pt-1">
            <button
              type="button"
              onClick={onClose}
              disabled={busy}
              className="h-9 px-4 rounded-lg text-caption font-semibold border border-border bg-bg-primary text-text-secondary hover:text-text-primary disabled:opacity-50"
            >
              {t("family.cancel")}
            </button>
            <button
              type="submit"
              disabled={!ready || busy}
              className="h-9 px-4 rounded-lg text-caption font-semibold bg-brand-primary text-white hover:opacity-90 disabled:opacity-40"
            >
              {busy ? t("tasks.creating") : t("tasks.create")}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
