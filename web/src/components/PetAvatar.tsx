/**
 * 宠物圆形头像。
 * - 有头像(pet.avatarExt 非空)：fetch /api/identity/pets/{id}/avatar 的 blob 作 <img>
 *   （同 PersonAvatar：<img> 挂不了 Bearer header，故走 fetch+blob URL，两环境一致）
 * - 否则：淡底 + 🐾 占位（与人类头像视觉区分）
 */
import { useEffect, useState } from "react";
import type { Pet } from "@/lib/types";
import { authHeaders } from "@/api/register";

interface Props {
  pet: Pet;
  size?: number;
}

export function PetAvatar({ pet, size = 34 }: Props) {
  const [src, setSrc] = useState<string | null>(null);
  const hasAvatar = !!pet.avatarExt;

  useEffect(() => {
    if (!hasAvatar) {
      setSrc(null);
      return;
    }
    let cancelled = false;
    let objectUrl: string | null = null;
    (async () => {
      try {
        const r = await fetch(`/api/identity/pets/${pet.id}/avatar`, {
          cache: "no-store",
          headers: authHeaders(),
        });
        if (!r.ok || cancelled) return;
        const blob = await r.blob();
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setSrc(objectUrl);
      } catch {
        /* 主 backend 未起 / 无头像时保留占位 */
      }
    })();
    return () => {
      cancelled = true;
      if (objectUrl) {
        const url = objectUrl;
        setSrc((cur) => (cur === url ? null : cur));
        URL.revokeObjectURL(url);
      }
    };
  }, [pet.id, hasAvatar, pet.avatarExt]);

  return (
    <span
      className="relative inline-flex shrink-0"
      style={{ width: size, height: size }}
      aria-hidden
    >
      <span
        className="rounded-full w-full h-full overflow-hidden flex items-center justify-center"
        style={{ background: "var(--color-bg-tertiary)" }}
      >
        {src ? (
          <img
            src={src}
            alt=""
            className="w-full h-full object-cover"
            onError={() => setSrc(null)}
          />
        ) : (
          <span style={{ fontSize: Math.round(size * 0.5), lineHeight: 1 }}>🐾</span>
        )}
      </span>
    </span>
  );
}
