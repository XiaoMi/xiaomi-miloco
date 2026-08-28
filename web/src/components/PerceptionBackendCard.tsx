/**
 * 「模型」页顶部的**感知后端**选择卡。
 *
 * 两条并列通路,二选一:
 * - **云端 API** —— 现状默认。用下方「模型列表」里配好的多模态模型,需要 API Key。
 * - **本地 GPU** —— 画面不出本地、零 token 成本、延迟低;但纯视觉:无音频结论、
 *   无身份识别,且不执行任何设备动作(命中只上报,配置好的动作会被拒绝)。
 *
 * 切到本地前后端会先探活,不通直接拒绝 —— 与「启用云端模型前先测连接」同一条
 * 不变量:不让用户切到一个不工作的后端上,否则感知会静默停摆。
 * 能力差异不在前端硬编码,由后端 local_capabilities 声明后在此渲染。
 */

import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { getPerceptionBackend, setPerceptionBackend } from "@/api";
import {
  buildSwitchPayload,
  healthLine,
  isReachable,
} from "@/lib/perceptionBackend";
import type { PerceptionBackendState } from "@/lib/types";
import { toast } from "./Toast";

/** 后端切换完成事件。同页的「模型配置」表要据此刷新它的"云端模型是否在被调用"
 *  提示 —— 两块卡挨着显示,不联动的话切完会一个说本地生效、另一个说云端生效。 */
export const PERCEPTION_BACKEND_CHANGED = "miloco:perception-backend-changed";

/** 云端就绪度提示 → 本地化文案。
 *
 *  与下面的 PB_CODE_KEY 同一条规矩:后端那句是硬编码中文,直出会污染英文界面。
 *  切换失败那条路径早就改成了 code + 前端查表(见 PB_CODE_KEY),cloud_hint 当时
 *  漏掉了 —— 这里补上。
 *
 *  认不出的 code 回落到后端的 message:契约对第三方/更新的后端开放,前端不认识
 *  的新 code 也不该让提示整个消失。 */
export function cloudHintText(
  hint: PerceptionBackendState["cloud_hint"],
  t: (k: string) => string,
): string {
  if (!hint) return "";
  const key = CLOUD_HINT_KEY[hint.code];
  if (!key) return hint.message ?? "";
  // models_missing 的具体缺失项只有后端知道,拼在本地化文案之后。
  return hint.detail ? `${t(key)}:${hint.detail}` : t(key);
}

const CLOUD_HINT_KEY: Record<string, string> = {
  cloud_no_api_key: "perceptionBackend.codes.cloud_no_api_key",
  cloud_models_missing: "perceptionBackend.codes.cloud_models_missing",
};

/** 后端错误码 → 本地化文案。与 UsageOmniConfig 的 OMNI_CODE_KEY 同构:backend
 *  message 是硬编码中文,直接注入会污染英文界面。 */
const PB_CODE_KEY: Record<string, string> = {
  unreachable: "perceptionBackend.codes.unreachable",
  auth_rejected: "perceptionBackend.codes.auth_rejected",
  loading: "perceptionBackend.codes.loading",
  load_failed: "perceptionBackend.codes.load_failed",
  blocking_rules: "perceptionBackend.codes.blocking_rules",
  bad_url: "perceptionBackend.codes.bad_url",
};

const INPUT_CLS =
  "w-full px-3 py-2 rounded-lg bg-bg-primary border border-border " +
  "focus:border-brand-primary focus:outline-none text-text-primary num";

export function PerceptionBackendCard() {
  const { t } = useTranslation();
  const [state, setState] = useState<PerceptionBackendState | null>(null);
  const [baseUrl, setBaseUrl] = useState("");
  const [token, setToken] = useState("");
  const [switching, setSwitching] = useState<"cloud" | "local" | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);

  useEffect(() => {
    void load();
  }, []);

  async function load() {
    try {
      const s = await getPerceptionBackend({ probe: true });
      setState(s);
      setBaseUrl(s.local_vision.base_url);
      setLoadErr(null);
    } catch (e) {
      setLoadErr(e instanceof Error ? e.message : String(e));
    }
  }

  async function onSwitch(
    backend: "cloud" | "local",
    extra?: Record<string, number>,
  ) {
    if (!state || switching) return;
    setSwitching(backend);
    try {
      const s = await setPerceptionBackend({
        ...buildSwitchPayload(backend, baseUrl, token),
        ...(extra ?? {}),
      });
      setState(s);
      setToken("");
      window.dispatchEvent(new Event(PERCEPTION_BACKEND_CHANGED));
      toast(
        backend === "local"
          ? t("perceptionBackend.switchedLocal")
          : t("perceptionBackend.switchedCloud"),
        "ok",
      );
    } catch (e) {
      // 优先按 code 查本地化文案;后端那句中文只在没有 code 时兜底。
      const code = (e as { code?: string } | null)?.code;
      const key = code ? PB_CODE_KEY[code] : undefined;
      const detail = (e as { message?: string } | null)?.message;
      // blocking_rules 的 message 是「一整句中文 + 规则名」,拼进 toast 会让英文
      // 界面冒出中文 —— 与本文件 PB_CODE_KEY 上方那条注释自相矛盾。payload 里
      // 本来就带 `rules` 数组(纯用户数据、不含任何文案),用它。
      // load_failed 的 detail 是模型加载的异常原文:那是技术细节,本来就无从
      // 本地化,照旧透传。
      const rules = (e as { data?: { rules?: string[] } } | null)?.data?.rules;
      const extra =
        code === "blocking_rules"
          ? rules?.join(t("perceptionBackend.listSeparator"))
          : code === "load_failed"
            ? detail
            : "";
      toast(
        key
          ? [t(key), extra].filter(Boolean).join(":")
          : (detail ?? t("perceptionBackend.switchFailed")),
        "danger",
      );
    } finally {
      setSwitching(null);
    }
  }

  if (loadErr) {
    return (
      <section className="rounded-xl bg-bg-secondary border border-border shadow-sm p-5 md:p-6">
        <div className="text-error text-center py-4">{loadErr}</div>
      </section>
    );
  }
  if (!state) {
    return (
      <section className="rounded-xl bg-bg-secondary border border-border shadow-sm p-5 md:p-6">
        <div className="text-text-secondary text-center py-4">
          {t("usage.loading")}
        </div>
      </section>
    );
  }

  const isLocal = state.backend === "local";
  const line = healthLine(state);
  const reachable = isReachable(state);

  return (
    <section
      className="rounded-xl bg-bg-secondary border border-border shadow-sm p-5 md:p-6"
      aria-labelledby="perception-backend-title"
    >
      <h2 id="perception-backend-title" className="text-section-title">
        {t("perceptionBackend.title")}
      </h2>
      <p className="text-caption text-text-secondary mt-1">
        {t("perceptionBackend.subtitle")}
      </p>

      <div className="mt-4 grid grid-cols-1 md:grid-cols-2 gap-3">
        {/* 云端 API */}
        <button
          type="button"
          onClick={() => onSwitch("cloud")}
          disabled={!!switching}
          aria-pressed={!isLocal}
          className={`text-left rounded-lg border p-4 transition disabled:opacity-60 ${
            !isLocal
              ? "border-brand-primary bg-brand-soft"
              : "border-border hover:border-brand-primary"
          }`}
        >
          <div className="flex items-center justify-between">
            <span className="text-text-primary font-medium">
              {t("perceptionBackend.cloudTitle")}
            </span>
            {!isLocal && (
              <span className="rounded px-1.5 py-0.5 bg-brand-primary text-white text-caption">
                {t("usage.activeTag")}
              </span>
            )}
          </div>
          <p className="text-caption text-text-secondary mt-1">
            {t("perceptionBackend.cloudDesc")}
          </p>
          {/* 缺 Key / 模型没下完时切回云端同样起不来。不拒绝(云端是退路,挡住
              就把用户关在坏掉的后端里),但必须让它成为知情的选择。 */}
          {isLocal && state.cloud_hint && (
            <p className="text-caption text-warning mt-2">
              {cloudHintText(state.cloud_hint, t)}
            </p>
          )}
        </button>

        {/* 本地 GPU */}
        <button
          type="button"
          onClick={() => onSwitch("local")}
          disabled={!!switching}
          aria-pressed={isLocal}
          className={`text-left rounded-lg border p-4 transition disabled:opacity-60 ${
            isLocal
              ? "border-brand-primary bg-brand-soft"
              : "border-border hover:border-brand-primary"
          }`}
        >
          <div className="flex items-center justify-between">
            <span className="text-text-primary font-medium">
              {t("perceptionBackend.localTitle")}
            </span>
            {isLocal ? (
              <span className="rounded px-1.5 py-0.5 bg-brand-primary text-white text-caption">
                {t("usage.activeTag")}
              </span>
            ) : (
              <span
                className={`text-caption ${reachable ? "text-success" : "text-text-tertiary"}`}
              >
                {reachable
                  ? t("perceptionBackend.reachable")
                  : t("perceptionBackend.unreachable")}
              </span>
            )}
          </div>
          <p className="text-caption text-text-secondary mt-1">
            {t("perceptionBackend.localDesc")}
          </p>
          {/* 边车要用户自己部署。不写这句的话,点下去只会得到「服务不可达」——
              准确,但是个死胡同:从没有人告诉过他那是什么服务、去哪儿拿。 */}
          {!isLocal && (
            <p className="text-caption text-text-tertiary mt-1">
              {t("perceptionBackend.setupHint")}
            </p>
          )}
        </button>
      </div>

      {/* 边车地址 + 连通性 */}
      <div className="mt-4 grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-3 items-start">
        <label className="block">
          <span className="text-caption text-text-secondary mb-1 block">
            {t("perceptionBackend.baseUrlLabel")}
          </span>
          <input
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="http://127.0.0.1:18800"
            className={INPUT_CLS}
          />
        </label>
        <label className="block">
          <span className="text-caption text-text-secondary mb-1 block">
            {t("perceptionBackend.tokenLabel")}
          </span>
          <input
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder={
              state.local_vision.has_token
                ? t("perceptionBackend.tokenSaved")
                : t("perceptionBackend.tokenPlaceholder")
            }
            className={INPUT_CLS}
          />
        </label>
        <div className="text-caption md:col-span-2">
          {line.kind === "unreachable" ? (
            <span className="text-error">
              ✗ {t("perceptionBackend.unreachable")}
            </span>
          ) : line.kind === "auth-rejected" ? (
            <span className="text-error">
              ✗ {t("perceptionBackend.authBad")}
            </span>
          ) : line.kind === "load-failed" ? (
            <span className="text-error">
              ✗ {t("perceptionBackend.codes.load_failed")}
              <span className="num"> {line.detail}</span>
            </span>
          ) : line.kind === "loading" ? (
            <span className="text-text-secondary">
              {t("perceptionBackend.loadingModel")}
            </span>
          ) : line.kind === "ok" ? (
            <span className="text-success num">
              ✓ {line.device} · {line.backend}
              <span className="text-text-tertiary">
                {" "}
                {t("perceptionBackend.backendConfigured")}
              </span>
              {line.gateOff ? ` · ${t("perceptionBackend.gateOff")}` : ""}
            </span>
          ) : null}
        </div>
      </div>

      {/* 能力差异:切到本地会失去什么,必须在切之前就看得到 */}
      <ul className="mt-4 text-caption text-text-secondary space-y-1">
        {!state.local_capabilities.needs_api_key && (
          <li>• {t("perceptionBackend.capNoKey")}</li>
        )}
        {!state.local_capabilities.audio && (
          <li>• {t("perceptionBackend.capNoAudio")}</li>
        )}
        {!state.local_capabilities.identity && (
          <li>• {t("perceptionBackend.capNoIdentity")}</li>
        )}
        {!state.local_capabilities.suggestions && (
          <li>• {t("perceptionBackend.capNoSuggestions")}</li>
        )}
        {!state.local_capabilities.static_rule_execution && (
          <li className="text-warning">
            • {t("perceptionBackend.capNoStatic")}
          </li>
        )}
      </ul>

      {/* 输入参数:先选通路,再显示那条通路自己的那组。
          两条通路的成本结构相反(云端每帧每像素都要付 token 钱;本地由 token 预算
          封顶、帧数几乎免费),摆在一起会让人以为调一个就够了。 */}
      <div className="mt-4 rounded-lg border border-border p-4">
        <div className="text-body font-medium text-text-primary">
          {t("perceptionBackend.inputTitle")}
        </div>
        <p className="text-caption text-text-secondary mt-1">
          {t(
            isLocal
              ? "perceptionBackend.inputWhyLocal"
              : "perceptionBackend.inputWhyCloud",
          )}
        </p>
        <div className="mt-3 grid grid-cols-1 md:grid-cols-3 gap-4">
          {(isLocal
            ? ([
                [
                  "window_size",
                  "windowLabel",
                  "windowHintLocal",
                  "unitSec",
                  1,
                  60,
                ],
                ["container_fps", "fpsLabel", "fpsHint", "unitFps", 1, 120],
                [
                  "codec_target_canvas",
                  "canvasLabel",
                  "canvasHint",
                  "unitCanvas",
                  4,
                  64,
                ],
                [
                  "video_short_edge",
                  "shortEdgeLabel",
                  "shortEdgeHintLocal",
                  "unitPx",
                  64,
                  2160,
                ],
              ] as const)
            : ([] as const)
          ).map(([field, label, hint, unit, min, max]) => (
            <label key={field} className="block">
              <span className="text-caption text-text-secondary mb-1 block">
                {t(`perceptionBackend.${label}`)}
                <span className="text-text-tertiary">
                  {" "}
                  ({t(`perceptionBackend.${unit}`)})
                </span>
              </span>
              <input
                type="number"
                min={min}
                max={max}
                defaultValue={state.local_vision[field] ?? ""}
                placeholder={field === "video_short_edge" ? "—" : undefined}
                onBlur={(e) => {
                  const raw = e.target.value.trim();
                  if (raw === "") return;
                  const v = Number(raw);
                  if (!Number.isFinite(v) || v < min || v > max) return;
                  if (v === state.local_vision[field]) return;
                  void onSwitch("local", { [field]: v });
                }}
                className={INPUT_CLS}
              />
              <span className="text-caption text-text-tertiary mt-1 block">
                {t(`perceptionBackend.${hint}`)}
              </span>
            </label>
          ))}
          {/* 云端那组只读展示 —— 它归设置页管,这里只是让人看清两边不是一套值。 */}
          {!isLocal && (
            <>
              {(
                [
                  ["window_size", "windowLabel", "unitSec"],
                  ["omni_fps", "omniFpsLabel", "unitFps"],
                  ["video_short_edge", "shortEdgeLabel", "unitPx"],
                ] as const
              ).map(([field, label, unit]) => (
                <div key={field}>
                  <span className="text-caption text-text-secondary mb-1 block">
                    {t(`perceptionBackend.${label}`)}
                    <span className="text-text-tertiary">
                      {" "}
                      ({t(`perceptionBackend.${unit}`)})
                    </span>
                  </span>
                  <div className="text-body text-text-primary num">
                    {state.local_vision.cloud[field]}
                  </div>
                </div>
              ))}
            </>
          )}
        </div>
      </div>

      {/* 只在已切到本地时显示:此刻这些规则的设备动作正在被拒绝执行,是要紧且
          可行动的信息。还在云端时不显示 —— 直连设备规则是本产品最常见的自动化,
          在那儿常驻一条橙色警告等于给不会切过去的用户天天报警;切换被拒时后端的
          400 本来就会把它们逐条列出来。 */}
      {isLocal && state.blocking_static_rules.length > 0 && (
        <div className="mt-3 text-caption text-warning bg-warning-bg rounded-lg px-3 py-2">
          {t("perceptionBackend.blockingRulesLocal")}
          <span className="num">
            {" "}
            {state.blocking_static_rules.join(
              t("perceptionBackend.listSeparator"),
            )}
          </span>
        </div>
      )}
    </section>
  );
}
