/**
 * 家人圆形头像:统一组件——所有家人头像位都走它。
 *
 * - 已录入(faceEnrolled=true)且主 backend 起着:拉首张 face 样本作 <img>
 * - 否则:淡彩色底(paletteFor(person.avatarHue))+ 白色 IconPerson 填充
 *
 * 淡彩色由 personPalette 的 6 套预设决定,通过 person.avatarHue 索引:
 *   listPersons:i % 6(按返回列表序,跨 reload 稳定)
 *   createPerson:Math.random() % 6(本地态,下次 listPersons reload 时被 i % 6 覆盖)
 *
 * 路由迁移:已从独立 register_server(``/identity/*``)迁到主 backend(``/api/identity/*``)。
 */
import { useEffect, useState } from "react";
import type { Person } from "@/lib/types";
import { IconCheck, IconPerson } from "@/lib/icons";
import { paletteFor } from "@/lib/personPalette";
import { authHeaders } from "@/api/register";

interface Props {
  person: Person;
  /** 直径 px */
  size?: number;
  /** 已采集身份时，右下角挂一个 success 对勾角标（未采集则不挂，靠"无角标"区分）。 */
  badge?: boolean;
}

export function PersonAvatar({ person, size = 28, badge = false }: Props) {
  const [src, setSrc] = useState<string | null>(null);
  const enrolled = person.faceEnrolled;
  // 有显式头像 或 已录入(可回落 tier_a face[0]) 才拉图；都无则占位色块。
  // 后端 GET /avatar 内部按「显式头像 > face[0] > 404」解析，前端只需一条 URL。
  const hasAvatar = enrolled || !!person.avatarExt;
  const iconSize = Math.round(size * 0.58);
  const palette = paletteFor(person.avatarHue);
  const badgeSize = Math.max(14, Math.round(size * 0.34));

  useEffect(() => {
    if (!hasAvatar) {
      setSrc(null);
      return;
    }
    let cancelled = false;
    let objectUrl: string | null = null;
    (async () => {
      try {
        // <img> 挂不了 Bearer header，故 fetch + blobURL（与旧实现同因）；单一 URL
        // 交后端解析显式头像 or 回落 face[0]。avatarExt 入 dep：换/清头像后自动刷新。
        const imgRes = await fetch(`/api/identity/persons/${person.id}/avatar`, {
          cache: "no-store",
          headers: authHeaders(),
        });
        if (!imgRes.ok || cancelled) return;
        const blob = await imgRes.blob();
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setSrc(objectUrl);
      } catch {
        /* 主 backend 未起时保留图标占位 */
      }
    })();
    return () => {
      cancelled = true;
      // blob URL 必须显式 revoke,否则 GC 不回收,长跑页面会泄。同时把 src state
      // 切回 null,避免 <img> 仍引用已 revoke 的 URL — Chrome 偶发 zoom/repaint
      // 触发 blob 重 fetch 时会拿到 404 让头像变占位。
      if (objectUrl) {
        const url = objectUrl;
        setSrc((cur) => (cur === url ? null : cur));
        URL.revokeObjectURL(url);
      }
    };
  }, [person.id, hasAvatar, person.avatarExt]);

  return (
    <span
      className="relative inline-flex shrink-0"
      style={{ width: size, height: size }}
      aria-hidden
    >
      <span
        className="rounded-full w-full h-full overflow-hidden flex items-center justify-center"
        style={{ background: src ? "var(--color-bg-tertiary)" : palette.bg }}
      >
        {src ? (
          <img
            src={src}
            alt=""
            className="w-full h-full object-cover"
            onError={() => setSrc(null)}
          />
        ) : (
          <IconPerson
            width={iconSize}
            height={iconSize}
            className="text-white"
          />
        )}
      </span>
      {badge && enrolled && (
        <span
          className="absolute bottom-0 right-0 rounded-full bg-success flex items-center justify-center border-2 border-bg-secondary"
          style={{ width: badgeSize, height: badgeSize }}
        >
          <IconCheck
            width={Math.round(badgeSize * 0.66)}
            height={Math.round(badgeSize * 0.66)}
            className="text-white"
            strokeWidth={2.4}
          />
        </span>
      )}
    </span>
  );
}
