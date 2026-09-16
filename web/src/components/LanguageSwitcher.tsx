/** 语言切换器（中文 / English）—— 复用通用 Segmented，放 TopBar 操作区。 */
import { useTranslation } from "react-i18next";
import { Segmented } from "./Segmented";
import { inNativeApp, setLanguagePreference, type Lang } from "@/i18n";

export function LanguageSwitcher() {
  const { t, i18n } = useTranslation();
  const cur: Lang = i18n.language === "en" ? "en" : "zh";
  return (
    <Segmented<Lang>
      ariaLabel={t("lang.label")}
      value={cur}
      onChange={(v) => {
        if (v === cur) return;
        // 部分文案（设备状态/属性名）在取数映射时按当时语言烘焙进数据对象，纯组件
        // 重渲染翻不动，所以浏览器里整页 reload 重新拉取。
        void setLanguagePreference(v);
        // App 内置窗口里不自己 reload：原生端会跟着切语言、并用新的 ?lang= 重新加载；
        // 自己 reload 会拿着旧 URL（旧 ?lang=）把刚选的语言顶回去。
        if (!inNativeApp()) window.location.reload();
      }}
      options={[
        { key: "zh", label: t("lang.zh") },
        { key: "en", label: t("lang.en") },
      ]}
    />
  );
}
