/**
 * 自动刷新周期输入框（秒）。Token 用量与性能监测两处共用同一个值。
 *
 * 输入校验有意做成「宽进严出」：键入过程允许空串和中间态（删光了、只打了个 "1"），
 * 否则每敲一个字符就被夹回下限，删不动也改不了。真正的夹取发生在**失焦或回车**时，
 * 那才是「用户说完了」的时刻。非数字字符直接不进 state。
 */

import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  clampRefreshSec,
  REFRESH_MAX_SEC,
  REFRESH_MIN_SEC,
} from "@/hooks/useRefreshInterval";

export function RefreshIntervalInput({
  sec,
  onChange,
}: {
  sec: number;
  onChange: (n: number) => void;
}) {
  const { t } = useTranslation();
  const [text, setText] = useState(String(sec));
  // 另一处改了值要跟着显示
  useEffect(() => setText(String(sec)), [sec]);

  const commit = () => {
    const n = Number.parseInt(text, 10);
    if (!Number.isFinite(n)) {
      setText(String(sec)); // 空串 / 非法 → 回到当前值，不静默改成别的数
      return;
    }
    // 先夹一次再回显：夹取规则仍只有一处定义（clampRefreshSec），但显示不能依赖
    // 「上层的值一定会变」——已在下限时再输更小的数，夹完等于原值，state 不变、
    // 回显的副作用不触发，框里就会留着那个不生效的数。
    const v = clampRefreshSec(n);
    setText(String(v));
    onChange(v);
  };

  return (
    <label className="flex items-center gap-1.5 text-caption text-text-secondary">
      <span>{t("usage.refreshEvery")}</span>
      <input
        type="text"
        inputMode="numeric"
        value={text}
        aria-label={t("usage.refreshSecAria", {
          min: REFRESH_MIN_SEC,
          max: REFRESH_MAX_SEC,
        })}
        onChange={(e) => {
          // 纯数字检查：非数字字符不进 state（含中文全角、负号、小数点）
          const v = e.target.value;
          if (v === "" || /^\d+$/.test(v)) setText(v);
        }}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            commit();
            (e.target as HTMLInputElement).blur();
          }
        }}
        className="w-12 px-1.5 py-0.5 text-caption num text-right rounded-md bg-bg-primary
                   border border-border focus:border-brand-primary focus:outline-none"
      />
      <span>{t("usage.refreshSecUnit")}</span>
      {sec <= REFRESH_MIN_SEC && (
        // 到了任一端就说一句，别让人以为还能再往那个方向调。两端撞的是同一个夹取规则，
        // 只给下限解释会让人以为上限不存在——而想「关掉自动刷新」的人填个大数正好撞上。
        // 边界值走插值，常量改了文案跟着变，也不会在中文界面里冒出英文小尾巴。
        <span className="text-text-tertiary">
          ({t("usage.refreshAtMin", { min: REFRESH_MIN_SEC })})
        </span>
      )}
      {sec >= REFRESH_MAX_SEC && (
        <span className="text-text-tertiary">
          ({t("usage.refreshAtMax", { max: REFRESH_MAX_SEC })})
        </span>
      )}
    </label>
  );
}
