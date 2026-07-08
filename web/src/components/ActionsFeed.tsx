/**
 * 「miloco 做了什么」动作审计——现已并入 ActivityFeed 单流,本文件降为
 * 纯数据 helper + 行组件(ActionRow),不再是独立 tab 组件。
 *
 * 数据源:GET /api/actions(observability/router::list_actions)。
 * 返回 BARE JSON 数组(无 {code,data} 信封),新到旧排序。一次 agent 控制/播报/触发一行。
 * ActionRow 展示:时间 · 设备名(米家别名)+ 房间 · 动作类型 humanize + iid + value 截断 · 成功/失败徽标。
 */

import type { TFunction } from "i18next";
import { apiFetch } from "@/api/client";
import { TimeLabel } from "./TimeLabel";

/** backend action_ledger 行——就地类型,不进 lib/types.ts(仅本组件用)。 */
export interface BackendActionRow {
  id: string;
  timestamp: number;
  action_type: string;
  did: string;
  device_name: string | null;
  room: string | null;
  iid: string | null;
  value_json: string | null;
  result_code: number | null;
  result_msg: string | null;
  success: 0 | 1;
  error: string | null;
  trace_id: string | null;
}

const VALUE_MAX = 60;

/** 单流合并窗口用 limit=500 一次拉全,不再分页(见 ActivityFeed mergeFeedRows)。 */
const ACTIONS_LIMIT = 500;

/** 统一拉取——failedOnly 时带 failed_only=1。导出供 tests 守 query 参数 + 解析。 */
export async function fetchActions(failedOnly: boolean): Promise<BackendActionRow[]> {
  const q = failedOnly
    ? `?limit=${ACTIONS_LIMIT}&failed_only=1`
    : `?limit=${ACTIONS_LIMIT}`;
  return apiFetch<BackendActionRow[]>(`/api/actions${q}`);
}

/** action_type → i18n key。set_property/set_properties 归"设置属性";其余各自映射。 */
export function actionTypeKey(t: string): string {
  switch (t) {
    case "set_property":
    case "set_properties":
      return "actions.typeSetProperty";
    case "call_action":
      return "actions.typeCallAction";
    case "scene_trigger":
      return "actions.typeSceneTrigger";
    default:
      return "actions.typeUnknown";
  }
}

/** value_json 截断到 ~60 字符,超长加省略号(完整值走 title attr)。 */
function truncateValue(v: string | null): string {
  if (!v) return "";
  return v.length <= VALUE_MAX ? v : `${v.slice(0, VALUE_MAX)}…`;
}

/** 动作行——并入 ActivityFeed 单流时,动作行套一层 brand-soft 底色 + brand 左边条
 *  跟事件行(无底色)区分;失败原因仍走 error 语义色(spec 允许行内保留失败徽标)。 */
export function ActionRow({ row, t }: { row: BackendActionRow; t: TFunction }) {
  const ok = row.success === 1;
  const value = truncateValue(row.value_json);
  // 失败原因:优先 result_msg,退回 error;成功时不显。
  const reason = !ok ? row.result_msg || row.error || "" : "";
  const deviceLabel = row.device_name || row.did;

  return (
    <li className="px-5 py-2.5 bg-brand-soft border-l-2 border-brand-primary hover:bg-brand-soft-strong transition-colors">
      {/* 时间列与事件行(ActivityRow)完全一致:同一 TimeLabel 组件 + 同 70px 列宽,
          合并单流里两种行的时间格式/对齐不再有差异(修「时间格式不一致」)。 */}
      <div className="flex flex-col gap-1 sm:grid sm:grid-cols-[70px_1fr_auto] sm:gap-x-3 sm:gap-y-1 sm:items-baseline">
        <TimeLabel timestamp={row.timestamp} />

        <div className="min-w-0 sm:order-2">
          <div className="text-body text-text-primary break-words">
            <span className="font-medium">{deviceLabel}</span>
            {row.room && (
              <span className="text-caption text-text-tertiary ml-2">{row.room}</span>
            )}
          </div>
          <div className="text-caption text-text-secondary break-words">
            {t(actionTypeKey(row.action_type))}
            {row.iid && (
              <span className="text-caption-mono text-text-tertiary ml-1.5">{row.iid}</span>
            )}
            {value && (
              <span
                className="text-caption-mono text-text-tertiary ml-1.5 break-all"
                title={row.value_json ?? undefined}
              >
                {value}
              </span>
            )}
          </div>
          {reason && (
            <div className="text-caption text-error break-words mt-0.5" title={reason}>
              {reason}
            </div>
          )}
        </div>

        <span
          className={`text-caption px-2 py-0.5 rounded-full whitespace-nowrap sm:order-last sm:justify-self-end ${
            ok
              ? "text-success bg-success-bg"
              : "text-error bg-error-bg"
          }`}
        >
          {ok ? t("actions.resultSuccess") : t("actions.resultFailed")}
        </span>
      </div>
    </li>
  );
}
