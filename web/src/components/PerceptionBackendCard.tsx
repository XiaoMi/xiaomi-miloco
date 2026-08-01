/**
 * 「模型」页顶部的**感知后端**选择卡。
 *
 * 两条并列通路,二选一:
 * - **云端 API** —— 现状默认。用下方「模型列表」里配好的多模态模型,需要 API Key。
 * - **本地 GPU** —— 画面不出本地、零 token 成本、延迟低;但纯视觉:无音频结论、
 *   无身份识别,且不直接执行设备动作(规则命中一律交 agent 决策)。
 *
 * 切到本地前后端会先探活,不通直接拒绝 —— 与「启用云端模型前先测连接」同一条
 * 不变量:不让用户切到一个不工作的后端上,否则感知会静默停摆。
 * 能力差异不在前端硬编码,由后端 local_capabilities 声明后在此渲染。
 */

import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { getPerceptionBackend, setPerceptionBackend } from "@/api";
import { buildSwitchPayload, healthLine } from "@/lib/perceptionBackend";
import type { PerceptionBackendState } from "@/lib/types";
import { toast } from "./Toast";

/** 后端切换完成事件。同页的「模型配置」表要据此刷新它的"云端模型是否在被调用"
 *  提示 —— 两块卡挨着显示,不联动的话切完会一个说本地生效、另一个说云端生效。 */
export const PERCEPTION_BACKEND_CHANGED = "miloco:perception-backend-changed";

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
      const s = await getPerceptionBackend();
      setState(s);
      setBaseUrl(s.local_vision.base_url);
      setLoadErr(null);
    } catch (e) {
      setLoadErr(e instanceof Error ? e.message : String(e));
    }
  }

  async function onSwitch(backend: "cloud" | "local") {
    if (!state || switching) return;
    setSwitching(backend);
    try {
      const s = await setPerceptionBackend(
        buildSwitchPayload(backend, baseUrl, token),
      );
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
      toast(e instanceof Error ? e.message : t("perceptionBackend.switchFailed"), "danger");
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
        <div className="text-text-secondary text-center py-4">{t("usage.loading")}</div>
      </section>
    );
  }

  const isLocal = state.backend === "local";
  const line = healthLine(state);
  // 「可达」必须和下面那行探活结论用同一个判定。分开算的话,凭证被拒时会出现
  // 按钮显示绿色「可达」、紧邻的一行显示「✗ 边车拒绝当前凭证」的自相矛盾。
  const reachable = line.kind === "ok";

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
            <p className="text-caption text-warning mt-2">{state.cloud_hint}</p>
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
              <span className={`text-caption ${reachable ? "text-success" : "text-text-tertiary"}`}>
                {reachable ? t("perceptionBackend.reachable") : t("perceptionBackend.unreachable")}
              </span>
            )}
          </div>
          <p className="text-caption text-text-secondary mt-1">
            {t("perceptionBackend.localDesc")}
          </p>
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
            <span className="text-error">✗ {t("perceptionBackend.unreachable")}</span>
          ) : line.kind === "auth-rejected" ? (
            <span className="text-error">✗ {t("perceptionBackend.authBad")}</span>
          ) : line.kind === "ok" ? (
            <span className="text-success num">
              ✓ {line.device} · {line.backend}
              {line.gateOff ? ` · ${t("perceptionBackend.gateOff")}` : ""}
            </span>
          ) : null}
        </div>
      </div>

      {/* 能力差异:切到本地会失去什么,必须在切之前就看得到 */}
      <ul className="mt-4 text-caption text-text-secondary space-y-1">
        <li>• {t("perceptionBackend.capNoKey")}</li>
        {!state.local_capabilities.audio && <li>• {t("perceptionBackend.capNoAudio")}</li>}
        {!state.local_capabilities.identity && <li>• {t("perceptionBackend.capNoIdentity")}</li>}
        {!state.local_capabilities.static_rule_execution && (
          <li className="text-warning">• {t("perceptionBackend.capNoStatic")}</li>
        )}
      </ul>

      {/* 会挡住切换的规则先列出来,免得用户点了才知道 —— 后端会以 400 拒绝。 */}
      {!isLocal && state.blocking_static_rules.length > 0 && (
        <div className="mt-3 text-caption text-warning bg-warning-bg rounded-lg px-3 py-2">
          {t("perceptionBackend.blockingRules")}
          <span className="num"> {state.blocking_static_rules.join("、")}</span>
        </div>
      )}
    </section>
  );
}
