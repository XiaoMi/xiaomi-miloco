/**
 * 单价设置弹窗。形制沿用工程既有 dialog（遮罩 + 居中卡 + 右上关闭 + 底部取消/保存）。
 *
 * 单价**按模型名分别填**——不同模型价目本就不同，一份全局单价在有第二个模型时必然
 * 算错。注意只到模型名这一层：同一个模型名挂在两个 endpoint 上共用一份价（这是有意
 * 的取舍，见 usagePricing 的说明），弹窗底部对住户明说了这一条。可配的模型取自本周期
 * 实际出现过的模型（stats.rows），列表里只会有真正需要定价的那几个。
 *
 * 弹窗里有意**不解释口径**（残差、缓存摊分那些）：那些属于「看一眼试一下也知道不了」
 * 的背景知识，塞进表单会挤掉真正要填的东西，故收进费用旁边的「?」提示里。留在这里的
 * 只有几条「不说就无从得知」的：计价基数是每 MTokens、这是本机估算而非服务商账单、
 * 单价按模型名保存，以及命中价看着没打折时的那句提醒。
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { UsageStats } from "@/lib/types";
import { humanTokens } from "@/lib/formatTokens";
import {
  cacheLooksUndiscounted,
  costInputsByModel,
  costInputsByTarget,
  costPerModelFromTargets,
  mergeEditedPricing,
  moneyDigits,
  knownPricingFor,
  pricingSourceFor,
  seedPricingFor,
  type ModelPricing,
  type UsagePricing,
} from "@/lib/usagePricing";
import { IconX } from "@/lib/icons";

const CURRENCIES = ["¥", "$", "€", "£"];


export function UsagePricingDialog({
  stats,
  pricing,
  onSave,
  onClose,
}: {
  stats: UsageStats;
  pricing: UsagePricing;
  onSave: (next: UsagePricing) => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const byModel = useMemo(() => costInputsByModel(stats), [stats]);
  // 算钱一律走「模型名 + endpoint」的目标；byModel 只用来回答「本周期有哪些模型」
  // 与算命中率提示，不参与计价（折叠键必须与顶部合计、明细各行是同一把）。
  const byTarget = useMemo(() => costInputsByTarget(stats), [stats]);
  const models = useMemo(() => [...byModel.keys()].sort(), [byModel]);

  // 草稿：改动只在保存时才落到外面，取消即丢弃
  const [draft, setDraft] = useState<UsagePricing>(() => ({
    ...pricing,
    byModel: Object.fromEntries(
      models.map((m) => [m, seedPricingFor(pricing, m)]),
    ),
  }));
  const [sel, setSel] = useState(models[0] ?? "");
  // 只有住户真的动过的模型才写回本机表：弹窗只列本周期出现过的模型，
  // 整表覆写会把上周录过、这周没用到的单价静默删掉（见 mergeEditedPricing）。
  const [touched, setTouched] = useState<ReadonlySet<string>>(() => new Set());
  const cancelRef = useRef<HTMLButtonElement | null>(null);

  // 进场聚焦只在挂载时做一次。**不能和 Esc 监听合成一个 effect**：那样它会跟着
  // onClose 的身份走，而父组件每轮自动刷新都会新建这个闭包，住户正在输入的光标
  // 会被反复拽回「取消」。
  useEffect(() => {
    cancelRef.current?.focus();
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const pr: ModelPricing = draft.byModel[sel] ?? seedPricingFor(draft, sel);
  const setPr = (patch: Partial<ModelPricing>) => {
    setTouched((s) => (s.has(sel) ? s : new Set(s).add(sel)));
    setDraft((d) => ({
      ...d,
      byModel: { ...d.byModel, [sel]: { ...pr, ...patch } },
    }));
  };

  const perModelCost = costPerModelFromTargets(byTarget, draft);
  const perModel = models.map((m) => ({ model: m, total: perModelCost.get(m) ?? 0 }));
  const grandTotal = perModel.reduce((a, x) => a + x.total, 0);
  const money = (v: number) =>
    draft.currency + moneyDigits(v);

  const ci = sel ? byModel.get(sel) : undefined;
  const cacheShare =
    ci && ci.text + ci.video + ci.audio > 0
      ? (ci.cache / (ci.text + ci.video + ci.audio)) * 100
      : 0;
  // 命中价没真打折、且本周期确实有命中量时告警。
  // 不给「会高估百分之多少」——那个数只能拿一个**假定的**服务商折扣才算得出来；
  // 拿住户自己填的这份没打折的价当基准，算出来恒等于 0，等于告警永不出现。
  const cacheUndiscounted = ci != null && cacheShare > 0 && cacheLooksUndiscounted(pr);

  return (
    <div
      className="fixed inset-0 z-[70] flex items-end md:items-center justify-center
                 bg-black/40 backdrop-blur-sm p-0 md:p-5"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="usage-pricing-title"
        className="modal-surface w-full md:max-w-[540px] max-h-[88vh] overflow-y-auto
                   rounded-t-2xl md:rounded-xl border border-border shadow-lg p-5 md:p-6"
      >
        <div className="flex items-start justify-between gap-3 mb-1">
          <h2 id="usage-pricing-title" className="text-title font-semibold">
            {t("usage.pricingTitle")}
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label={t("common.close")}
            className="shrink-0 rounded-full p-1 text-text-secondary hover:text-text-primary
                       hover:bg-bg-tertiary transition-colors"
          >
            <IconX />
          </button>
        </div>
        <p className="text-caption text-text-secondary mb-5">{t("usage.pricingSub")}</p>

        {models.length === 0 ? (
          <p className="text-body text-text-secondary py-6 text-center">
            {t("usage.pricingNoModels")}
          </p>
        ) : (
          <>
            {/* 货币（全局，不按模型分） */}
            <Row label={t("usage.pricingCurrency")}>
              {CURRENCIES.map((c) => (
                <Chip
                  key={c}
                  on={draft.currency === c}
                  onClick={() => setDraft((d) => ({ ...d, currency: c }))}
                >
                  {c}
                </Chip>
              ))}
              <input
                type="text"
                value={CURRENCIES.includes(draft.currency) ? "" : draft.currency}
                placeholder={t("usage.pricingCustom")}
                aria-label={t("usage.pricingCustom")}
                onChange={(e) => {
                  // 空串也要落下去：只在非空时才写的话，删到最后一个字符时状态没变、
                  // 而 value 是从状态推出来的，那个字符会立刻弹回来，框根本删不空。
                  // 空串等于「没选自定义」，此时下面的档位按钮回到未选中态。
                  setDraft((d) => ({ ...d, currency: e.target.value.trim() }));
                }}
                className="w-20 px-2 py-1 text-caption num rounded-md bg-bg-primary
                           border border-border focus:border-brand-primary focus:outline-none"
              />
            </Row>

            {/* 模型：只列本周期真出现过的 */}
            <Row label={t("usage.pricingModel")}>
              {models.map((m) => (
                <Chip key={m} on={sel === m} onClick={() => setSel(m)}>
                  <span className="num">{m}</span>
                </Chip>
              ))}
            </Row>

            <Row label={t("usage.pricingMode")}>
              <Chip on={pr.mode === "flat"} onClick={() => setPr({ mode: "flat" })}>
                {t("usage.pricingModeFlat")}
              </Chip>
              <Chip on={pr.mode === "modality"} onClick={() => setPr({ mode: "modality" })}>
                {t("usage.pricingModeModality")}
              </Chip>
            </Row>

            {knownPricingFor(sel) && (
              <p className="text-caption text-text-secondary mb-2">
                {t("usage.pricingPresetHint")}
              </p>
            )}

            {/* 这个模型此前从没有过单价依据 → 下面输入框里的数是占位草稿。
                必须说明，否则住户会以为那是我们查到的价目而直接保存。 */}
            {pricingSourceFor(pricing, sel) === "unset" && (
              <p className="text-caption text-warning mb-2">{t("usage.pricingUnsetHint")}</p>
            )}

            <div
              className="grid gap-3 sm:grid-cols-3 bg-bg-primary rounded-lg p-3.5 mb-3"
              key={pr.mode}
            >
              {/* 色点的含义是「这个色在图表里出现」。输入 / 命中在图表里没有对应色，
                  给它们配点等于凭空造一个语义，所以不区分模态这一档三项都不带点，
                  内部保持一致；区分模态那一档四个模态带点，命中是唯一例外——它本来
                  就不是图表里的序列，且排在最后自成一行。 */}
              {pr.mode === "flat" ? (
                <>
                  <Price label={t("usage.priceInputMiss")} value={pr.input} onChange={(v) => setPr({ input: v })} />
                  <Price label={t("usage.priceInputHit")} value={pr.cache} onChange={(v) => setPr({ cache: v })} />
                  <Price label={t("usage.modalityOutput")} value={pr.output} onChange={(v) => setPr({ output: v })} />
                </>
              ) : (
                <>
                  <Price label={t("usage.priceText")} value={pr.text} onChange={(v) => setPr({ text: v })} dot="bg-usage-text" />
                  <Price label={t("usage.modalityVideo")} value={pr.video} onChange={(v) => setPr({ video: v })} dot="bg-usage-video" />
                  <Price label={t("usage.modalityAudio")} value={pr.audio} onChange={(v) => setPr({ audio: v })} dot="bg-usage-audio" />
                  <Price label={t("usage.modalityOutput")} value={pr.output} onChange={(v) => setPr({ output: v })} dot="bg-usage-output" />
                  <Price label={t("usage.priceInputHit")} value={pr.cache} onChange={(v) => setPr({ cache: v })} />
                </>
              )}
            </div>

            {/* 命中价没打折时点出命中占比——占比高时这份估算会明显偏高，不提示会被当成「差不多」 */}
            {cacheUndiscounted && (
              <p className="text-caption text-warning mb-3 leading-relaxed">
                ⚠️ {t("usage.pricingCacheWarn", { share: cacheShare.toFixed(1) })}
              </p>
            )}

            {/* 实时预览：按模型拆开，改一个价立刻看到影响 */}
            <div className="rounded-lg bg-brand-soft px-3.5 py-3 mb-3 flex items-baseline justify-between gap-3">
              <span className="text-caption text-text-secondary">
                {t("usage.pricingPreview", { tokens: humanTokens(stats.total_tokens) })}
              </span>
              <b className="text-display num font-semibold">≈ {money(grandTotal)}</b>
            </div>
            {perModel.length > 1 && (
              <ul className="mb-4 flex flex-col gap-1">
                {perModel.map((x) => (
                  <li
                    key={x.model}
                    className="flex items-baseline justify-between gap-3 text-caption text-text-secondary"
                  >
                    <span className="num">{x.model}</span>
                    <span className="num font-semibold text-text-primary">{money(x.total)}</span>
                  </li>
                ))}
              </ul>
            )}

            <ul className="text-caption text-text-secondary mb-5 leading-relaxed flex flex-col gap-1">
              {[t("usage.pricingNote1"), t("usage.pricingNote2")].map((line) => (
                <li key={line} className="grid grid-cols-[0.6rem_1fr] gap-1">
                  <span aria-hidden>·</span>
                  <span>{line}</span>
                </li>
              ))}
            </ul>
          </>
        )}

        <div className="flex justify-end gap-2">
          <button
            ref={cancelRef}
            type="button"
            onClick={onClose}
            className="text-body px-4 py-2 rounded-lg bg-bg-primary border border-border
                       text-text-primary hover:border-border-strong transition-colors"
          >
            {t("usage.cancel")}
          </button>
          <button
            type="button"
            onClick={() => onSave(mergeEditedPricing(pricing, draft, touched))}
            disabled={models.length === 0}
            className="text-body px-4 py-2 rounded-lg bg-brand-primary text-text-inverse
                       hover:bg-brand-accent disabled:opacity-60 transition-colors"
          >
            {t("usage.save")}
          </button>
        </div>
      </div>
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-2.5 flex-wrap mb-3">
      <span className="text-caption text-text-secondary min-w-[72px]">{label}</span>
      {children}
    </div>
  );
}

function Chip({
  on,
  onClick,
  children,
}: {
  on: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={on}
      onClick={onClick}
      className={`text-caption px-2.5 py-1.5 rounded-lg border transition-colors whitespace-nowrap ${
        on
          ? "border-brand-primary text-brand-primary bg-brand-soft"
          : "border-border text-text-primary bg-bg-primary hover:border-border-strong"
      }`}
    >
      {children}
    </button>
  );
}

/** 单价输入。允许空串与中间态（"1." 这类），只在解析成合法非负数时才回写。 */
function Price({
  label,
  value,
  onChange,
  dot,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  dot?: string;
}) {
  const [text, setText] = useState(String(value));
  useEffect(() => setText(String(value)), [value]);
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-caption text-text-secondary flex items-center gap-1.5">
        {dot && <span className={`w-2 h-2 rounded-sm shrink-0 ${dot}`} aria-hidden />}
        {label}
      </span>
      <input
        type="text"
        inputMode="decimal"
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          const n = Number.parseFloat(e.target.value);
          if (Number.isFinite(n) && n >= 0) onChange(n);
        }}
        className="w-full px-2.5 py-1.5 text-body num rounded-md bg-bg-secondary
                   border border-border focus:border-brand-primary focus:outline-none"
      />
    </label>
  );
}
