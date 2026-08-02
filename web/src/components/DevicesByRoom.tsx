/**
 * 「家里的设备」按房间分组展示（v3 Mi Console 视觉）
 *
 * 视觉规格：
 * - 房间标题行用 chevron + 名字 + mono 计数 meta
 * - 设备行：紧凑（44px 高），左侧图标 + 名字 + 状态点+状态文字 + 主开关 + ⋯
 * - 状态点与状态文案同色：在线绿、离线灰；异常态预留 warning
 * - 场景行底部 hairline 分隔
 */

import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ComponentType, SVGProps } from "react";
import type { Device, DeviceCategory, Scene } from "@/lib/types";
import { triggerScene } from "@/api";
import {
  IconAircon,
  IconCamera,
  IconChevronDown,
  IconChevronRight,
  IconCurtain,
  IconLightbulb,
  IconLock,
  IconPlug,
  IconTV,
  IconWind,
} from "@/lib/icons";
import { toast } from "./Toast";

// 设备列控制能力暂未补齐（孤立的开关让人困惑，缺亮度/温度/模式等其它属性控制）。
// 等其余属性控件就位后把此处改回 true 一并放开;开关 JSX 在本 PR 中已删,
// 重新放开时从 git history(e06cfe2~1)取回 button[role=switch] 模板。
// **解锁条件**:当米家 spec 暴露 brightness / color-temp / mode 等读写接口后,
// 接通这些控件即可在卡片内 inline 展开;一并把这个 flag 改 true 删本注释。
const SHOW_DEVICE_MAIN_SWITCH = false;

const CATEGORY_ICON: Record<DeviceCategory, ComponentType<SVGProps<SVGSVGElement>>> = {
  light: IconLightbulb,
  aircond: IconAircon,
  purifier: IconWind,
  fan: IconWind,
  curtain: IconCurtain,
  lock: IconLock,
  tv: IconTV,
  camera: IconCamera,
  other: IconPlug,
};

interface Props {
  devices: Device[];
  scenes: Scene[];
  onChanged: () => void;
}

export function DevicesByRoom({ devices, scenes, onChanged }: Props) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const unassigned = t("devices.unassigned");

  // 按 room 分组,保留原始顺序
  const groups = useMemo(() => {
    const m = new Map<string, Device[]>();
    for (const d of devices) {
      const key = d.room || unassigned;
      if (!m.has(key)) m.set(key, []);
      m.get(key)!.push(d);
    }
    return [...m.entries()];
  }, [devices, unassigned]);

  // 默认规则:≤3 个房间全展开;>3 个房间只展第一个
  const defaultOpen = (idx: number) => groups.length <= 3 || idx === 0;
  const isOpen = (room: string, idx: number) =>
    expanded[room] ?? defaultOpen(idx);

  return (
    <section
      className="rounded-xl bg-bg-secondary border border-border shadow-sm anim-in"
      aria-labelledby="devices-title"
    >
      <div className="flex items-baseline justify-between px-5 pt-4 pb-3">
        <h2
          id="devices-title"
          className="text-title text-text-primary inline-flex items-baseline gap-2"
        >
          {t("devices.title")}
          <span className="text-caption-mono text-text-tertiary font-normal">
            {devices.length} devices ·{" "}
            {groups.filter(([room]) => room !== unassigned).length} rooms
          </span>
        </h2>
      </div>

      {groups.length === 0 && (
        <div className="text-body text-text-secondary py-10 px-5 text-center">
          {t("devices.emptyState")}
        </div>
      )}

      <div className="px-2">
        {groups.map(([room, list], idx) => {
          const onlineCount = list.filter((d) => d.online).length;
          const onCount = list.filter(
            (d) =>
              d.online &&
              !d.dangerous &&
              d.category !== "lock" &&
              d.mainSwitch?.current,
          ).length;
          const open = isOpen(room, idx);
          return (
            <div
              key={room}
              className={idx > 0 ? "border-t border-border" : ""}
            >
              <button
                type="button"
                aria-expanded={open}
                onClick={() =>
                  setExpanded((s) => ({ ...s, [room]: !open }))
                }
                className="w-full flex items-center justify-between py-2.5 px-3 rounded-md hover:bg-[color-mix(in_srgb,var(--color-bg-tertiary),transparent_50%)] transition-colors"
              >
                <span className="flex items-center gap-2 min-w-0">
                  <span className="text-text-tertiary shrink-0">
                    {open ? <IconChevronDown /> : <IconChevronRight />}
                  </span>
                  <span className="text-title text-text-primary">{room}</span>
                  <span className="text-caption-mono text-text-tertiary">
                    {t("devices.countUnit", { n: list.length })}
                    {/* `onCount 开着` 跟 SHOW_DEVICE_MAIN_SWITCH 同步显示——
                        flag=false 时开关隐藏，"开着 N 个"也跟着隐藏（避免住户
                        看到只读计数却找不到地方点开关）。flag 改 true 时一并放开。 */}
                    {SHOW_DEVICE_MAIN_SWITCH && onCount > 0 ? t("devices.onCount", { n: onCount }) : ""}
                    {onlineCount < list.length
                      ? t("devices.offlineCount", { n: list.length - onlineCount })
                      : ""}
                  </span>
                </span>
              </button>
              {open && (
                <div className="pl-5 pb-1 pr-1">
                  {list.map((d) => (
                    <DeviceRow key={d.did} device={d} />
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {scenes.length > 0 && (
        <div className="border-t border-border px-5 pt-3 pb-4 mt-1">
          <div className="text-caption text-text-tertiary mb-2">
            {t("devices.scenesHeading")}
          </div>
          <div className="flex flex-wrap gap-2">
            {scenes.map((s) => (
              <button
                key={s.id}
                type="button"
                onClick={async () => {
                  try {
                    await triggerScene(s.id);
                    // backend 200 只代表指令已下发到米家云,场景内设备实际动作
                    // 是异步的(米家云 → 设备 LAN),离线设备会动不起来。给个
                    // toast 让住户至少知道按钮 work 了,不会以为"点了没反应"。
                    toast(t("devices.sceneTriggered", { name: s.name }), "ok");
                    onChanged();
                  } catch (e) {
                    toast(e instanceof Error ? e.message : t("devices.sceneTriggerFailed"), "warn");
                  }
                }}
                className="text-body px-3.5 py-1.5 rounded-md bg-brand-soft text-brand-primary border border-transparent hover:bg-brand-primary hover:text-white transition-colors"
              >
                {s.name}
              </button>
            ))}
          </div>
        </div>
      )}

    </section>
  );
}

interface RowProps {
  device: Device;
}

function DeviceRow({ device }: RowProps) {
  const Icon = CATEGORY_ICON[device.category] ?? IconPlug;
  const offline = !device.online;
  const ms = device.mainSwitch;
  const isOn = !offline && (ms?.current ?? false);
  const isUnlocked = device.statusKind === "unlocked";

  // 状态提示同色表达：在线=绿，离线=灰，需要注意=warning。
  let dotColor = "bg-text-tertiary";
  let dotRing = "var(--color-bg-tertiary)";
  let statusTextColor = "text-text-tertiary";
  if (isUnlocked) {
    dotColor = "bg-warning";
    dotRing = "var(--color-warning-bg)";
    statusTextColor = "text-warning";
  } else if (!offline) {
    dotColor = "bg-success";
    dotRing = "var(--color-success-bg)";
    statusTextColor = "text-success";
  }

  // v5：纯展示，不响应点击。原 DeviceQuickSheet 弹窗已删（控制能力暂未补齐
  // 时给孤立开关让住户困惑），等 brightness/color-temp/mode 三组控件接通真接口
  // 后再考虑加回 + 配合 SHOW_DEVICE_MAIN_SWITCH 一起放开。
  return (
    <div className="flex items-center gap-2.5 px-2 py-1.5 rounded-md transition-colors">
      <span
        className={`shrink-0 inline-flex items-center justify-center rounded-md ${
          offline
            ? "text-text-tertiary"
            : isOn
              ? "text-brand-primary"
              : "text-text-secondary"
        }`}
        style={{
          width: 36,
          height: 36,
          background: isOn && !offline
            ? "var(--color-brand-soft)"
            : "var(--color-bg-tertiary)",
        }}
      >
        <Icon width={24} height={24} />
      </span>
      <span className="text-body truncate text-text-primary flex-1">
        {device.name}
      </span>
      <span className="shrink-0 inline-flex items-center gap-1.5 pr-2">
        <span
          aria-hidden
          className={`shrink-0 rounded-full ${dotColor}`}
          style={{
            width: 5,
            height: 5,
            boxShadow: `0 0 0 3px ${dotRing}`,
          }}
        />
        <span className={`text-caption-mono ${statusTextColor}`}>
          {device.statusText}
        </span>
      </span>
    </div>
  );
}
