/**
 * 升级提示 + 一键升级（仅 release 部署）。
 *
 * 设计取向（对齐 knowledge/07-design + 需求"要体现但别太频繁"）：
 *  - 零主动打扰：打开页面查一次（后端缓存数小时），无轮询、无 toast 主动弹。
 *  - 被动 + 可按版本 dismiss：一条克制的窄 banner（StatusRibbon 视觉语汇：5px 点 + 文字 +
 *    CTA）。关闭 banner 时调 POST /upgrade/dismiss，把"已确认版本"记到**后端**（非浏览器
 *    localStorage），该版本永久不再出现、直到出现更新的版本。banner 显隐看 info.dismissed。
 *  - 侧栏红点独立：只要有可升级新版就常驻显示（与 dismiss 无关），常驻低调入口。
 *  - 只有用户点"升级"才有打扰型交互（确认弹窗 + 升级中全屏态）。
 *  - dev(git) 部署不出 banner（deploy_kind==="dev"）。
 *
 * 进度呈现（真实、跨语言）：竖排「状态点 + 文字」三步——下载 → 安装 → 重启（完成=刷新，
 * 不单列）。检测与显示解耦：显示文字走 i18n 跟随网页语言；进度/终态只认 /upgrade/status
 * 解析 upgrade.log 的结果——downloading/installing 分下载/安装步、连不上(throw)=重启步、
 * done 标记(AGENT_UPGRADE_DONE)=完成、failed 标记=失败。**不看 /version 版本变更判完成**：
 * install.py 会在中途多次重启到新版本、令其"提前"可达，只有末尾 done 标记才是可靠终态。
 *
 * 视觉规范：语义 token 类，dialog z-[60] + rounded-2xl + anim-in（同 ConfirmUnbindDialog），
 * 状态用点不用块（.status-dot*），Esc 关闭走 useEscClose（确认/检查/结果浮层，升级中除外）。
 */

import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { triggerUpgrade, upgradeStatus } from "@/api";
import { ApiError } from "@/api/client";
import {
  shouldShowUpgradeBanner,
  updateAvailable,
  phaseToStep,
  UPGRADE_STEPS,
  type UpgradePhase,
} from "@/lib/upgrade";
import {
  useUpgradeInfo,
  dismissUpgrade,
  refreshUpgradeInfo,
  OPEN_UPGRADE_EVENT,
} from "@/hooks/useUpgrade";
import { toast } from "@/components/Toast";
import { useEscClose } from "@/hooks/useEscClose";
import { IconX, IconAlert } from "@/lib/icons";

const POLL_INTERVAL_MS = 2_500;
// 冷升级要下载 ~68MB 安装包 + 解压模型 + 两趟 install.py，慢网络下轻松破 5min（实测
// 235kB/s ≈ 5min 只够下载）。超时给足 20min，避免真在进行时被误判"失败"并停轮询。
const POLL_TIMEOUT_MS = 20 * 60_000;
// 超过这个时长仍在升级 → 追加一句"首次升级下大包、需数分钟"的安抚，但不判失败、继续轮询。
const SLOW_HINT_MS = 3 * 60_000;
// 「重启」是最后一步（后端连不上时点亮）。
const RESTART_STEP = UPGRADE_STEPS.length - 1;

type Phase = UpgradePhase;

export default function UpgradeNotice() {
  const { t } = useTranslation();
  const info = useUpgradeInfo();
  const [phase, setPhase] = useState<Phase>("idle");
  // 步骤指示器：stepIndex 单调不回退，指向 UPGRADE_STEPS 里当前到达的步骤。
  const [stepIndex, setStepIndex] = useState(0);
  // 升级耗时超过 SLOW_HINT_MS 时置真：追加安抚文案，不判失败。
  const [slow, setSlow] = useState(false);
  // 升级中弹窗被用户主动隐藏到后台（升级继续跑、完成仍自动刷新）——避免 20min 硬锁死。
  const [hidden, setHidden] = useState(false);
  // 确认按钮 in-flight 禁用，防双击起两个升级（第二个会 409）。
  const [submitting, setSubmitting] = useState(false);
  const pollTimer = useRef<number | null>(null);
  const reloadTimer = useRef<number | null>(null);
  // t 不放进轮询 effect 依赖（否则切语言会重置进度 + 起第二个轮询循环）——用 ref 取最新。
  const tRef = useRef(t);
  tRef.current = t;
  // open 事件处理器只绑一次（[] deps），用 ref 取最新 info/phase，避免闭包读到旧值。
  const infoRef = useRef(info);
  infoRef.current = info;
  const phaseRef = useRef(phase);
  phaseRef.current = phase;

  // 侧栏底部升级入口点击 → 跨组件意图（走 window 事件解耦）：
  //  - idle + 已知有新版：打开升级确认弹窗（不 dismiss——dismiss 只由关 banner 触发）；
  //  - idle + 未知/无新版：进入 checking → 现查一次（弹窗显 loading），查完分流；
  //  - 非 idle（升级中/结果态）：仅把"转入后台"隐藏的窗重新唤出，不改 phase、不打断升级。
  useEffect(() => {
    const open = () => {
      setHidden(false);
      if (phaseRef.current !== "idle") return;
      setPhase(updateAvailable(infoRef.current) ? "confirm" : "checking");
    };
    window.addEventListener(OPEN_UPGRADE_EVENT, open);
    return () => window.removeEventListener(OPEN_UPGRADE_EVENT, open);
  }, []);

  // 手动检查更新：进入 checking 后强制现查一次（force，跳后端缓存）。有可升级新版 → 直接进
  // 确认弹窗，但**不**在此顺手 dismiss——这是用户主动"查"出来的新版，若立刻确认掉，取消后
  // 用来提醒它的提示点/banner 就再也不冒了；不 dismiss 才能让它照常浮现。无新版 → checked
  // 结果态（已是最新 / 无法检查）。全程复用同一弹窗、无 toast。
  useEffect(() => {
    if (phase !== "checking") return;
    let cancelled = false;
    (async () => {
      const r = await refreshUpgradeInfo();
      if (cancelled) return;
      setPhase(updateAvailable(r) ? "confirm" : "checked");
    })();
    return () => {
      cancelled = true;
    };
  }, [phase]);

  // 三个非升级中的浮层（确认 / loading / 结果）都可 Esc 关闭；升级中/终态浮层刻意不给
  // Esc（不在升级途中误关，超时/失败态只经"刷新页面"退出）。
  useEscClose(
    phase === "confirm" || phase === "checked" || phase === "checking",
    () => setPhase("idle"),
  );

  // 升级中轮询 /upgrade/status（唯一数据源）：
  //  - phase="done"  → 升级真正跑完（脚本末尾 echo 的终态标记）→ 刷新页面；
  //  - phase="failed"→ 升级失败 → 转失败提示；
  //  - 连不上（throw）→ 后端正在重启 → 点亮"重启"步；
  //  - 其余（downloading/installing/starting）→ 映射到 下载/安装 步。
  // 不看 /version 版本变更判完成——install.py 会在 prepare/finish 中途多次重启到新版本，
  // 新版本"提前"可达，单看版本会误判完成并把页面刷到还没装完的后端上（终态只认 done 标记）。
  // 步骤单调不回退（Math.max）。
  useEffect(() => {
    if (phase !== "upgrading") return;
    const startedAt = Date.now();
    const deadline = startedAt + POLL_TIMEOUT_MS;
    let cancelled = false;
    setStepIndex(0);
    setSlow(false);
    setHidden(false);

    const bump = (idx: number) => setStepIndex((s) => Math.max(s, idx));

    const tick = async () => {
      if (cancelled) return;
      if (Date.now() > deadline) {
        setPhase("timeout");
        return;
      }
      if (Date.now() - startedAt > SLOW_HINT_MS) setSlow(true);

      try {
        const s = await upgradeStatus();
        if (cancelled) return;
        if (s.phase === "done") {
          setStepIndex(UPGRADE_STEPS.length); // 越过末步 → 全部点亮为已完成
          toast(tRef.current("update.success"), "ok");
          reloadTimer.current = window.setTimeout(
            () => window.location.reload(),
            800,
          );
          return;
        }
        if (s.phase === "failed") {
          // 后端写下 AGENT_UPGRADE_FAILED（如 curl 连不上 GitHub，数秒内即失败）→ 独立
          // 失败态。不复用 timeout 文案：那句"未在预期时间内完成"会在失败后数秒就弹出、
          // 与后端刻意做的"快失败"自相矛盾。roll-forward 下后端已回到原版本、可正常刷新。
          setPhase("failed");
          return;
        }
        bump(phaseToStep(s.phase));
      } catch (e) {
        if (cancelled) return;
        // 后端连不上（fetch 直接 throw、非 ApiError）= 正在重启 → 点亮"重启"步。但若后端有
        // 响应却回鉴权错（401/403，如升级中 token 轮换），一直显示"重启中"到 20min 超时会
        // 掩盖真因 → 转失败并提示，让用户看到真正原因而非空等（roll-forward：后端仍在原版本）。
        if (e instanceof ApiError && (e.status === 401 || e.status === 403)) {
          toast(tRef.current("update.authError"), "danger");
          setPhase("failed");
          return;
        }
        bump(RESTART_STEP); // 连不上 / 网关暂不可用 = 正在重启
      }
      if (cancelled) return;
      pollTimer.current = window.setTimeout(tick, POLL_INTERVAL_MS);
    };
    pollTimer.current = window.setTimeout(tick, POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      if (pollTimer.current) window.clearTimeout(pollTimer.current);
      if (reloadTimer.current) window.clearTimeout(reloadTimer.current);
    };
  }, [phase]);

  function onDismiss() {
    if (!info?.latest) return;
    // 关 banner = 后端记录"已确认到该版本"（持久化、非浏览器）；成功后 banner 立即隐藏，
    // 该版本永久不再提示、直到出现更新版本。失败则不记（banner 保留），不阻塞——
    // 显式吞掉 rejection：dismissUpgrade 仅在成功后才更新 _info，失败时 banner 天然保留，
    // 此处 catch 只为避免 fire-and-forget 的未处理 promise rejection（非改语义）。
    void dismissUpgrade(info.latest).catch(() => {});
  }

  async function onConfirmUpgrade() {
    if (submitting) return;
    setSubmitting(true);
    try {
      await triggerUpgrade();
      setPhase("upgrading");
    } catch (e) {
      // 409 = 后端已有升级在跑（双击 / 多标签）→ 接管进度，不当失败。
      if (e instanceof ApiError && e.status === 409) {
        setPhase("upgrading");
        return;
      }
      const msg = e instanceof Error ? e.message : String(e);
      toast(t("update.failed", { msg }), "danger");
      setPhase("idle");
    } finally {
      setSubmitting(false);
    }
  }

  const showBanner = shouldShowUpgradeBanner(info, phase);
  const showProgressModal =
    (phase === "upgrading" && !hidden) ||
    phase === "timeout" ||
    phase === "failed";

  return (
    <>
      {showBanner && info?.latest && (
        <div className="shrink-0 mx-4 md:mx-8 mt-2 flex items-center gap-2 rounded-xl border border-border bg-bg-secondary px-4 py-2 shadow-sm">
          <span
            aria-hidden
            className="shrink-0 rounded-full bg-brand-primary"
            style={{
              width: 5,
              height: 5,
              boxShadow: "0 0 0 3px var(--color-brand-soft)",
            }}
          />
          <span className="text-body text-text-primary">
            {t("update.newVersion", { version: info.latest })}
          </span>
          {info.release_url && (
            <a
              href={info.release_url}
              target="_blank"
              rel="noreferrer"
              className="text-caption text-text-secondary hover:text-text-primary underline underline-offset-2"
            >
              {t("update.releaseNotes")}
            </a>
          )}
          <div className="ml-auto flex items-center gap-1">
            <button
              type="button"
              onClick={() => setPhase("confirm")}
              className="rounded-md bg-brand-primary px-3 py-1 text-caption text-white hover:bg-brand-accent transition-colors"
            >
              {t("update.upgrade")}
            </button>
            <button
              type="button"
              aria-label={t("update.dismiss")}
              onClick={onDismiss}
              className="p-1 rounded-md text-text-tertiary hover:text-text-primary hover:bg-bg-tertiary transition-colors"
            >
              <IconX aria-hidden />
            </button>
          </div>
        </div>
      )}

      {phase === "confirm" && info?.latest && (
        <div
          className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 p-4"
          onClick={() => setPhase("idle")}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="upgrade-confirm-title"
            className="w-[90%] max-w-md bg-bg-secondary border border-border rounded-2xl shadow-lg p-6 anim-in"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center gap-2 mb-3">
              <IconAlert aria-hidden />
              <h2
                id="upgrade-confirm-title"
                className="text-title font-semibold text-text-primary"
              >
                {t("update.confirmTitle", { version: info.latest })}
              </h2>
            </div>
            <p className="text-body text-text-secondary mb-6">
              {t("update.confirmBody")}
            </p>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setPhase("idle")}
                className="rounded-lg px-4 py-2 text-body bg-bg-primary border border-border text-text-primary hover:border-border-strong transition-colors"
              >
                {t("update.cancel")}
              </button>
              <button
                type="button"
                onClick={onConfirmUpgrade}
                disabled={submitting}
                className="rounded-lg bg-brand-primary px-4 py-2 text-body text-white hover:bg-brand-accent transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
              >
                {t("update.confirm")}
              </button>
            </div>
          </div>
        </div>
      )}

      {phase === "checking" && (
        <div
          className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 p-4"
          onClick={() => setPhase("idle")}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-live="polite"
            aria-label={t("update.checking")}
            className="w-[90%] max-w-md bg-bg-secondary border border-border rounded-2xl shadow-lg p-6 anim-in text-center"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="inline-flex items-center gap-2 text-body text-text-secondary">
              {/* loading 走全站 animate-pulse 点语汇（07-design），不新造 spinner */}
              <span
                aria-hidden
                className="inline-block w-2 h-2 rounded-full bg-text-tertiary animate-pulse"
              />
              {t("update.checking")}
            </div>
          </div>
        </div>
      )}

      {phase === "checked" && (
        <div
          className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 p-4"
          onClick={() => setPhase("idle")}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="upgrade-checked-title"
            className="w-[90%] max-w-md bg-bg-secondary border border-border rounded-2xl shadow-lg p-6 anim-in"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center gap-2 mb-3">
              <IconAlert aria-hidden />
              <h2
                id="upgrade-checked-title"
                className="text-title font-semibold text-text-primary"
              >
                {t("update.checkTitle")}
              </h2>
            </div>
            <p className="text-body text-text-secondary mb-6">
              {/* has_update 为真时不会走到 checked（去了 confirm），故此处结果有三种：
                  dev(git) 部署——引导 git pull（一键升级对 dev 不适用，dev 优先判定，避免
                  谎称"已是最新"）/ 连不上——诚实告知"无法检查" / 已是最新。 */}
              {info?.deploy_kind === "dev"
                ? t("update.devDeploy")
                : !info || !info.reachable
                  ? t("update.checkFailed")
                  : t("update.upToDate", { version: info.current })}
            </p>
            <div className="flex justify-end">
              <button
                type="button"
                onClick={() => setPhase("idle")}
                className="rounded-lg px-4 py-2 text-body bg-bg-primary border border-border text-text-primary hover:border-border-strong transition-colors"
              >
                {t("update.gotIt")}
              </button>
            </div>
          </div>
        </div>
      )}

      {showProgressModal && info?.latest && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 p-4">
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="upgrade-progress-title"
            aria-live="polite"
            className="w-[90%] max-w-md bg-bg-secondary border border-border rounded-2xl shadow-lg p-6 anim-in text-center"
          >
            {phase === "upgrading" ? (
              <>
                <h2
                  id="upgrade-progress-title"
                  className="text-title font-semibold text-text-primary mb-5"
                >
                  {t("update.upgrading", { version: info.latest })}
                </h2>
                {/* 步骤清单：竖排「状态点 + 文字」，走设计铁律②「状态用点不用块」——
                    已完成=绿点(status-dot-ok)、进行中=橙点脉冲(status-dot-brand)、
                    未开始=灰点(status-dot-muted)。不显示虚假百分比。左对齐块整体居中。 */}
                <ol className="mx-auto flex w-fit flex-col items-start gap-3">
                  {UPGRADE_STEPS.map((key, i) => {
                    const done = i < stepIndex;
                    const current = i === stepIndex;
                    return (
                      <li
                        key={key}
                        className="inline-flex items-center gap-2.5"
                        aria-current={current ? "step" : undefined}
                      >
                        <span
                          aria-hidden
                          className={
                            done
                              ? "status-dot status-dot-ok"
                              : current
                                ? "status-dot status-dot-brand animate-pulse"
                                : "status-dot status-dot-muted"
                          }
                        />
                        <span
                          className={`text-body ${
                            current
                              ? "font-semibold text-text-primary"
                              : done
                                ? "text-text-secondary"
                                : "text-text-tertiary"
                          }`}
                        >
                          {t(`update.step.${key}`)}
                        </span>
                      </li>
                    );
                  })}
                </ol>
                <p className="mt-5 text-body text-text-secondary">
                  {t("update.upgradingHint")}
                </p>
                {slow && (
                  <p className="mt-2 text-caption text-text-tertiary">
                    {t("update.slowHint")}
                  </p>
                )}
                {/* 逃生口：不硬锁——可隐藏到后台，升级继续跑、完成仍自动刷新。 */}
                <button
                  type="button"
                  onClick={() => setHidden(true)}
                  className="mt-4 text-caption text-text-tertiary hover:text-text-secondary underline underline-offset-2 transition-colors"
                >
                  {t("update.runInBackground")}
                </button>
              </>
            ) : (
              <>
                <div className="flex items-center justify-center gap-2 mb-2">
                  <IconAlert aria-hidden />
                  <h2
                    id="upgrade-progress-title"
                    className="text-title font-semibold text-text-primary"
                  >
                    {t(
                      phase === "failed"
                        ? "update.failedTitle"
                        : "update.timeoutTitle",
                    )}
                  </h2>
                </div>
                <p className="text-body text-text-secondary mb-6">
                  {t(
                    phase === "failed"
                      ? "update.failedHint"
                      : "update.timeoutHint",
                  )}
                </p>
                <button
                  type="button"
                  onClick={() => window.location.reload()}
                  className="rounded-lg bg-brand-primary px-4 py-2 text-body text-white hover:bg-brand-accent transition-colors"
                >
                  {t("update.refresh")}
                </button>
              </>
            )}
          </div>
        </div>
      )}
    </>
  );
}
