/**
 * 发行版本（full / slim）运行时能力 —— slim = 独立 App 版。
 *
 * slim 版后端（MILOCO_EDITION=slim）只注册「概览 / 场景联动 / 日志 / 模型 / 设置」这条
 * 主链路：身份、宠物、家庭档案、任务、设备列表页、agent 动态动作、在线一键升级都不存在。
 * 前端必须跟着收敛，否则住户点进去只会看到一排 404 报错（连首屏那几条「加载家人/宠物/
 * 家庭档案失败」的 toast 都来自这些不存在的路由）。
 *
 * 唯一权威来源是后端 `GET /api/admin/edition`。main.tsx 在 render **之前** 取一次并
 * 写进这里，所以组件里同步读、没有首帧闪动，也不需要 context。
 *
 * 取不到（老后端没有这个端点 / 请求失败 / 超时）一律按 **full** 处理：宁可多显示几个
 * 入口让后端报 404，也不能因为一次网络抖动就把功能砍掉。
 */

import type { EditionInfo } from "./types";

export const FULL_EDITION: EditionInfo = {
  edition: "full",
  slim: false,
  capabilities: {
    identity: true,
    pet: true,
    home_profile: true,
    tasks: true,
    schedule: true,
    observability: true,
    one_click_upgrade: true,
    rule_only: false,
  },
};

/** slim 下保留的导航 tab（对应 Sidebar 的 TabKey）。 */
// 概览（只看画面 + 每路投喂/拾音开关）、场景联动、日志、模型（只看/配模型）。
export const SLIM_TAB_KEYS: readonly string[] = ["now", "scenes", "activity", "usage"];

let current: EditionInfo = FULL_EDITION;

export function getEdition(): EditionInfo {
  return current;
}

export function setEditionInfo(next: EditionInfo | null | undefined): void {
  current = next ?? FULL_EDITION;
}

/** 是否 slim（独立 App）版。 */
export function isSlimEdition(): boolean {
  return current.slim;
}

/**
 * 按发行版本过滤导航 tab。full 原样返回；slim 只留 {@link SLIM_TAB_KEYS}。
 * 结构化传入（而不是写死 Sidebar 的 TABS）便于单测，也避免 lib ↔ components 循环依赖。
 */
export function visibleTabs<T extends { key: string }>(all: readonly T[]): readonly T[] {
  return current.slim ? all.filter((tab) => SLIM_TAB_KEYS.includes(tab.key)) : all;
}
