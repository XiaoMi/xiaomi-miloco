/**
 * 单流 feed 左栏时间标签——事件行与动作行共用的**唯一**时间渲染
 * (双行:第 1 行日期「今天/昨天/YYYY/MM/DD」,第 2 行 HH:mm:ss,由 smartTimeParts 派生)。
 *
 * 从 ActivityFeed 抽出成共享组件:ActivityFeed 引用 ActionsFeed 的 ActionRow,
 * ActionRow 若反向 import ActivityFeed 会成环——时间渲染落在这个无依赖小模块,
 * 两种行用同一组件,时间格式天然一致(修「动作行与事件行时间格式不一致」)。
 */

import { smartTimeParts } from "@/lib/relativeTime";

export function TimeLabel({ timestamp }: { timestamp: number }) {
  // 双行布局:第 1 行日期(YYYY/MM/DD 或"今天/昨天"),第 2 行时分秒.
  // sm+(>=640px):父 grid 三列(70px / 1fr / auto),TimeLabel 在 70px 列内
  // sm:justify-self-stretch 占满,两行 sm:text-center 各自居中(等宽字体下日期
  // 10 字符撑满列宽,时间 8 字符居中显著).
  // mobile(<640px):父 flex-col 纵向堆叠,TimeLabel 自然左对齐,不加 text-center
  // 保持跟下方 text-body 文本同锚线(否则居中会让 feed 视觉节奏断裂).
  const { time, date } = smartTimeParts(timestamp);
  return (
    <div className="sm:justify-self-stretch leading-tight">
      <div className="text-caption-mono text-text-secondary whitespace-nowrap sm:text-center">
        {date}
      </div>
      <div className="text-caption-mono text-text-tertiary whitespace-nowrap sm:text-center">
        {time}
      </div>
    </div>
  );
}
