import type { UsagePeriod } from "@/lib/types";

/**
 * 周期 → i18n 键。三个消费方共用：卡顶的周期选择器、左栏总览的读屏标题、时间分布图的
 * 图表描述。只有一份定义——各写一份的话，加一个周期档就得记得改三处，而漏掉哪一处
 * 界面上只表现为某处显示原始 key。
 */
export const PERIOD_KEYS: Record<UsagePeriod, string> = {
  today: "usage.periodToday",
  week: "usage.periodWeek",
  month: "usage.periodMonth",
};
