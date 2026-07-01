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

type DeviceGroupCategory =
  | "cleaning_appliance"
  | "security"
  | "sensor"
  | "entertainment"
  | "environment_appliance"
  | "router_gateway"
  | "lighting"
  | "plug_switch"
  | "kitchen_appliance"
  | "bathroom"
  | "personal_living"
  | "pet_plant"
  | "fitness_health"
  | "office_study"
  | "other";

const GROUP_ORDER: DeviceGroupCategory[] = [
  "cleaning_appliance",
  "security",
  "sensor",
  "entertainment",
  "environment_appliance",
  "router_gateway",
  "lighting",
  "plug_switch",
  "kitchen_appliance",
  "bathroom",
  "personal_living",
  "pet_plant",
  "fitness_health",
  "office_study",
  "other",
];

const GROUP_LABEL_KEY = {
  cleaning_appliance: "devices.group.cleaningAppliance",
  security: "devices.group.security",
  sensor: "devices.group.sensor",
  entertainment: "devices.group.entertainment",
  environment_appliance: "devices.group.environmentAppliance",
  router_gateway: "devices.group.routerGateway",
  lighting: "devices.group.lighting",
  plug_switch: "devices.group.plugSwitch",
  kitchen_appliance: "devices.group.kitchenAppliance",
  bathroom: "devices.group.bathroom",
  personal_living: "devices.group.personalLiving",
  pet_plant: "devices.group.petPlant",
  fitness_health: "devices.group.fitnessHealth",
  office_study: "devices.group.officeStudy",
  other: "devices.group.other",
} satisfies { [K in DeviceGroupCategory]: string };

const DEVICE_TO_GROUP: { [K in DeviceCategory]: DeviceGroupCategory } = {
  camera: "security",
  lock: "security",
  purifier: "environment_appliance",
  fan: "environment_appliance",
  aircond: "environment_appliance",
  light: "lighting",
  curtain: "personal_living",
  tv: "entertainment",
  other: "other",
};

const RAW_CATEGORY_TO_GROUP: Record<string, DeviceGroupCategory> = {
  "扫地机器人": "cleaning_appliance",
  "擦地机": "cleaning_appliance",
  "洗衣机": "cleaning_appliance",
  "干衣机": "cleaning_appliance",
  "烘干机": "cleaning_appliance",
  "垃圾桶": "cleaning_appliance",
  "camera": "security",
  "lock": "security",
  "smart-lock": "security",
  "可视门铃": "security",
  "摄像头": "security",
  "摄像机灯": "security",
  "sensor": "sensor",
  "temperature-humidity-sensor": "sensor",
  "motion-sensor": "sensor",
  "contact-sensor": "sensor",
  "smoke-sensor": "sensor",
  "gas-sensor": "sensor",
  "water-leak-sensor": "sensor",
  "蓝牙温湿度传感器": "sensor",
  "温控器": "sensor",
  "tv": "entertainment",
  "television": "entertainment",
  "set-top-box": "entertainment",
  "projector": "entertainment",
  "speaker": "entertainment",
  "电视": "entertainment",
  "分体电视": "entertainment",
  "电视盒子": "entertainment",
  "机顶盒": "entertainment",
  "投影仪": "entertainment",
  "air-conditioner": "environment_appliance",
  "air-purifier": "environment_appliance",
  "fan": "environment_appliance",
  "heater": "environment_appliance",
  "空气净化器": "environment_appliance",
  "新风机": "environment_appliance",
  "加湿器": "environment_appliance",
  "除湿机": "environment_appliance",
  "电风扇": "environment_appliance",
  "电暖器/暖风机/电暖风": "environment_appliance",
  "凉霸": "environment_appliance",
  "空调": "environment_appliance",
  "空调伴侣": "environment_appliance",
  "香薰机": "environment_appliance",
  "gateway": "router_gateway",
  "router": "router_gateway",
  "网关": "router_gateway",
  "路由器": "router_gateway",
  "light": "lighting",
  "ceiling-light": "lighting",
  "灯": "lighting",
  "杀菌灯": "lighting",
  "outlet": "plug_switch",
  "wall-switch": "plug_switch",
  "plug": "plug_switch",
  "单控开关": "plug_switch",
  "带温湿度查询功能开关": "plug_switch",
  "电机控制器": "plug_switch",
  "养生壶": "kitchen_appliance",
  "压力锅": "kitchen_appliance",
  "多功能料理锅": "kitchen_appliance",
  "微波炉": "kitchen_appliance",
  "果汁机/破壁料理机": "kitchen_appliance",
  "油烟机": "kitchen_appliance",
  "电磁炉": "kitchen_appliance",
  "电饭煲": "kitchen_appliance",
  "空气炸锅": "kitchen_appliance",
  "蒸烤箱": "kitchen_appliance",
  "集成灶": "kitchen_appliance",
  "热水壶": "kitchen_appliance",
  "洗碗机": "kitchen_appliance",
  "冰箱": "kitchen_appliance",
  "净水器": "bathroom",
  "饮水机/净饮机": "bathroom",
  "热水器": "bathroom",
  "浴霸": "bathroom",
  "curtain": "personal_living",
  "smart-curtain": "personal_living",
  "窗帘": "personal_living",
  "开窗器": "personal_living",
  "晾衣架": "personal_living",
  "智能床": "personal_living",
  "智能枕": "personal_living",
  "电热毯": "personal_living",
  "按摩器": "personal_living",
  "按摩椅": "personal_living",
  "艾灸盒": "personal_living",
  "足浴盆": "personal_living",
  "灭蚊器": "personal_living",
  "宠物喂食器": "pet_plant",
  "宠物饮水机": "pet_plant",
  "猫砂盆": "pet_plant",
  "鱼缸": "pet_plant",
  "花盆": "pet_plant",
  "跑步机": "fitness_health",
  "走步机": "fitness_health",
  "控制面板": "office_study",
};

function groupRank(group: DeviceGroupCategory): number {
  const idx = GROUP_ORDER.indexOf(group);
  return idx === -1 ? GROUP_ORDER.length : idx;
}

function deviceGroup(device: Device): DeviceGroupCategory {
  if (device.rawCategory && RAW_CATEGORY_TO_GROUP[device.rawCategory]) {
    return RAW_CATEGORY_TO_GROUP[device.rawCategory];
  }
  return DEVICE_TO_GROUP[device.category] ?? "other";
}

export function sortDevicesForDisplay(devices: Device[]): Device[] {
  return [...devices].sort((a, b) => {
    if (a.online !== b.online) return a.online ? -1 : 1;
    const groupDiff = groupRank(deviceGroup(a)) - groupRank(deviceGroup(b));
    if (groupDiff !== 0) return groupDiff;
    return a.name.localeCompare(b.name, "zh-Hans-CN") || a.did.localeCompare(b.did);
  });
}

export function groupDevicesByCategory(devices: Device[]): [DeviceGroupCategory, Device[]][] {
  const groups = new Map<DeviceGroupCategory, Device[]>();
  for (const d of sortDevicesForDisplay(devices)) {
    const group = deviceGroup(d);
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group)!.push(d);
  }
  return [...groups.entries()].sort(([aGroup, aList], [bGroup, bList]) => {
    const aOnline = aList.some((d) => d.online);
    const bOnline = bList.some((d) => d.online);
    if (aOnline !== bOnline) return aOnline ? -1 : 1;
    return groupRank(aGroup) - groupRank(bGroup);
  });
}

interface Props {
  devices: Device[];
  scenes: Scene[];
  onChanged: () => void;
}

export function DevicesByRoom({ devices, scenes, onChanged }: Props) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const unassigned = t("devices.unassigned");

  // 按 room 分组；每个房间内排序为在线优先，并按设备类型聚类。
  const groups = useMemo(() => {
    const m = new Map<string, Device[]>();
    for (const d of devices) {
      const key = d.room || unassigned;
      if (!m.has(key)) m.set(key, []);
      m.get(key)!.push(d);
    }
    return [...m.entries()].map(([room, list]) => {
      const categoryGroups = groupDevicesByCategory(list);
      const sorted = categoryGroups.flatMap(([, categoryDevices]) => categoryDevices);
      return [room, sorted, categoryGroups] as const;
    });
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
        {groups.map(([room, list, categoryGroups], idx) => {
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
                  {categoryGroups.map(([category, categoryDevices]) => (
                    <div key={category} className="py-1">
                      <div className="text-caption-mono text-text-tertiary px-2 pb-1 flex items-center gap-1.5">
                        <span>{t(GROUP_LABEL_KEY[category])}</span>
                        <span>·</span>
                        <span>{t("devices.countUnit", { n: categoryDevices.length })}</span>
                      </div>
                      {categoryDevices.map((d) => (
                        <DeviceRow key={d.did} device={d} />
                      ))}
                    </div>
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
