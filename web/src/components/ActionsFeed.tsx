/**
 * 「miloco 做了什么」动作审计——现已并入 ActivityFeed 单流,本文件降为
 * 纯数据 helper + 行组件(ActionRow),不再是独立 tab 组件。
 *
 * 数据源:GET /api/actions(observability/router::list_actions)。
 * 返回 BARE JSON 数组(无 {code,data} 信封),新到旧排序。一次 agent 控制/播报/触发一行。
 * ActionRow 展示:时间 · 设备名(米家别名)+ 房间 · 动作人话(读不出时退回动作类型
 * + 原始 iid + 截断的 value)· 成功/失败徽标。人话的取数与回落见 lib/actionText。
 */

import type { ReactNode } from "react";
import type { TFunction } from "i18next";
import { apiFetch } from "@/api/client";
import { describeAction, type SpecTable } from "@/lib/actionText";
import type { FoldActionLike } from "@/lib/feedFold";
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
  /** v4:设备所属家庭;老行 / 解析失败为 null(后端按 home 过滤时对 null 放行) */
  home_id?: string | null;
  /** v5:相位。'enter' | 'exit' | 'legacy'(迁移前的历史行);无链路的行恒 null。
   *  可选——老后端不吐这两列时前端不该炸,只是折叠时它们全落进"无触发事件"那一档。 */
  phase?: string | null;
  /** v5:触发事件主键(宿主)。null = 这条动作没有触发源,或早于链路记录。 */
  trigger_event_id?: string | null;
}

const VALUE_MAX = 60;

/** 动作一次拉全的条数上限(见 lib/feedFold 的窗口规则)。导出供 ActivityFeed 判「是否已达
 *  上限」以渲染截断提示。 */
export const ACTIONS_LIMIT = 500;

/** 统一拉取——failedOnly 时带 failed_only=1;传时间窗时带 since_ms/until_ms(与事件流同口径,
 *  让动作也受当前筛选段约束,不混入范围外历史动作);传 homeId 时带 home_id(多 home 下
 *  切家后动作流只显当前家,无 home 标的老行后端放行)。导出供 tests 守 query 参数 + 解析。 */
export async function fetchActions(
  failedOnly: boolean,
  sinceMs?: number,
  untilMs?: number,
  homeId?: string,
): Promise<BackendActionRow[]> {
  const params = new URLSearchParams({ limit: String(ACTIONS_LIMIT) });
  if (failedOnly) params.set("failed_only", "1");
  if (sinceMs !== undefined) params.set("since_ms", String(sinceMs));
  if (untilMs !== undefined) params.set("until_ms", String(untilMs));
  if (homeId !== undefined) params.set("home_id", homeId);
  return apiFetch<BackendActionRow[]>(`/api/actions?${params.toString()}`);
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

/** 动作行——并入 ActivityFeed 单流时,动作行按**结果**着底色跟事件行(无底色)区分:
 *  成功=低饱和的柔和绿(success-bg,~8-12% alpha 的主题 token)、失败=柔和红(error-bg),
 *  「尽量和原色接近一点,别太扎眼」——不再用统一的 brand 橙;左边条用语义色全值
 *  (2px 细条,比底色略强的强调)。失败徽标保持不变。
 *
 *  入参取 lib/feedFold 的窄类型(FoldActionLike)而不是 BackendActionRow:本组件只读
 *  那几个字段,台账内部列(result_code / trace_id / home_id)不该长进它的契约里——
 *  折叠后的成员行本来就是从支里拿的行,不是重新从后端取的那一份。 */
export function ActionRow({
  row,
  t,
  spec,
  nested = false,
  domId,
  chip,
}: {
  row: FoldActionLike;
  t: TFunction;
  /** 该设备(did)的 spec 表,iid → spec。缺省 / 取不到时整行退回原始键——
   *  日志页顶层没拿到规格不该让动作行消失,退化今天的样子即可。 */
  spec?: SpecTable;
  /** 展开面里的成员行:缩进、压扁、去掉自己的左边条(那条属于它所属的支)。 */
  nested?: boolean;
  /** 供程序化聚焦用的 id(展开后焦点落到第一条动作)。 */
  domId?: string;
  /** 挂在行上的说明 chip(「无触发事件」/「早于链路记录」)。 */
  chip?: ReactNode;
}) {
  const ok = row.success === 1;
  const phrase = describeAction(row, spec, t);
  // 有了人话就不再重复原始 JSON:值已经读进那句话里,原始键留 chip 可搜。
  const value = phrase ? "" : truncateValue(row.value_json);
  // 失败原因:优先 result_msg,退回 error;成功时不显。
  const reason = !ok ? row.result_msg || row.error || "" : "";
  const deviceLabel = row.device_name || row.did;

  return (
    <li
      id={domId}
      tabIndex={domId ? -1 : undefined}
      className={`transition-colors ${
        nested ? "pl-9 pr-5 py-1.5" : "px-5 py-2.5 border-l-2"
      } ${ok ? "bg-success-bg" : "bg-error-bg"} ${nested ? "" : ok ? "border-success" : "border-error"}`}
    >
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
            {chip && <span className="ml-2 align-middle">{chip}</span>}
          </div>
          <div className="text-caption text-text-secondary break-words">
            {phrase ? (
              <span className="text-text-primary">{phrase}</span>
            ) : (
              t(actionTypeKey(row.action_type))
            )}
            {row.iid && (
              <span
                className="text-caption-mono text-text-tertiary bg-bg-secondary rounded-sm px-1.5 py-px ml-1.5 break-all"
                title={`${t("actions.rawKeyTitle")}${row.iid}`}
              >
                {row.iid}
              </span>
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
