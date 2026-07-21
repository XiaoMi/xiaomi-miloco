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
    // 依赖整个 pet 对象而非仅 avatarExt：同扩展名替换头像（jpg→jpg）时 avatarExt 值不变，
    // 只盯它会漏刷新（显示旧图）。pets 列表用 useAsync，data 仅在 reload 时换新对象，
    // 换/删头像保存后触发 pets.reload() → 新对象 → 重新拉图；平时 re-render 引用不变不多拉。
  }, [pet, hasAvatar]);

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
