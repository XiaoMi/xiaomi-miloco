/**
 * 家庭档案面板——家庭 tab 内、家人区下方。
 *
 * 仿「家里的设备」的折叠分组：每个类别（家庭规则 / 空间 / 设备 / 共享信息）
 * 是一个可折叠组，组头 = chevron + 名字 + mono 计数；空间/设备组内再按 subject_name 起
 * 小标题二级分组。靠 ownerOf 统一路由——任何一条都不会从界面上蒸发。每行点开 → 详情卡。
 * 「共享信息」= member_* 类型但没归到具体家人的信息（可能本就是全家共享）；在详情卡里仍可关联成员。
 */

import { IconChevronDown, IconChevronRight } from "@/lib/icons";
import type { HomeEntries, HomeEntry, Person } from "@/lib/types";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  AddEntryForm,
  addHomeEntry,
  bindEntryActions,
  EntryDetailSheet,
  EntryRow,
  hostedByPetCard,
  isGenericSubject,
  NON_MEMBER_TYPE_ORDER,
  ownerOf,
  typeLabel,
  useHomeWrite,
} from "./HomeProfileParts";
import i18n from "@/i18n";

interface Props {
  data: HomeEntries | undefined;
  persons: Person[];
  /** 花名册：用于把宠物条目排除出本面板（它们由「宠物档案卡」独占展示）。 */
  pets?: { id: string }[];
  loading: boolean;
  onChanged: () => void;
}

// space/device 的归属展示名：占位词（general/shared）/ 空 → 通用，否则用 subject_name。
function subjectLabel(name: string | null | undefined): string {
  return isGenericSubject(name) ? i18n.t("family.subjectGeneric") : name!;
}

type SubGroup = { label: string; items: HomeEntry[] };

type Section =
  | {
      key: string;
      title: string;
      count: number;
      kind: "entries";
      sub: SubGroup[];
    }
  | {
      key: string;
      title: string;
      count: number;
      kind: "orphan";
      items: HomeEntry[];
    };

export function HomeKnowledgePanel({
  data,
  persons,
  pets,
  loading,
  onChanged,
}: Props) {
  const { t } = useTranslation();
  const run = useHomeWrite(onChanged);
  // 候选已集中到总览「观察中」聚合卡，本面板只展示正式条目。
  const [selected, setSelected] = useState<HomeEntry | null>(null);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  const { sections, total } = useMemo(() => {
    // 归本面板 = ownerOf 解析不到已知家人的条目（含未匹配到人的 member_*）；
    // 宠物条目由「宠物档案卡」独占，排除掉——否则同一条外观在两处重复出现，
    // 且从这里能「改派给家人」把宠物档案卡清空。
    const profile = (data?.profile ?? []).filter(
      (e) => ownerOf(e, persons) === null && !hostedByPetCard(e, pets),
    );

    const sections: Section[] = [];

    for (const type of NON_MEMBER_TYPE_ORDER) {
      const items = profile.filter((e) => e.type === type);
      if (items.length === 0) continue;
      let sub: SubGroup[];
      if (type === "family") {
        sub = [{ label: "", items }];
      } else {
        const byName = new Map<string, HomeEntry[]>();
        for (const e of items) {
          const label = subjectLabel(e.subjectName);
          const arr = byName.get(label) ?? [];
          arr.push(e);
          byName.set(label, arr);
        }
        sub = [...byName.entries()]
          .sort((a, b) => a[0].localeCompare(b[0], "zh"))
          .map(([label, items]) => ({ label, items }));
      }
      sections.push({
        key: type,
        title: typeLabel(type),
        count: items.length,
        kind: "entries",
        sub,
      });
    }

    // 未匹配到人的 member_* 正式条目 → 「共享信息」组（可能本就是全家共享）。
    const orphan = profile.filter((e) => e.type.startsWith("member_"));
    if (orphan.length > 0) {
      sections.push({
        key: "orphan",
        title: t("family.sharedInfo"),
        count: orphan.length,
        kind: "orphan",
        items: orphan,
      });
    }

    return {
      sections,
      total: profile.length,
    };
    // i18n.language 入 deps：切语言时重算 section 标题（typeLabel/subjectLabel 经 i18n 解析）。
  }, [data, persons, pets, t, i18n.language]);

  const empty = !loading && total === 0;
  const isOpen = (key: string) => !(collapsed[key] ?? false);

  return (
    <section
      className="rounded-xl bg-bg-secondary border border-border shadow-sm anim-in"
      aria-labelledby="home-knowledge-title"
    >
      <div className="px-5 pt-4 pb-1">
        <h2
          id="home-knowledge-title"
          className="text-title text-text-primary inline-flex items-baseline gap-2"
        >
          {t("family.knowledgeTitle")}
          <span className="text-caption-mono text-text-tertiary font-normal num">
            {t("family.knowledgeCount", { count: total })}
          </span>
        </h2>
      </div>
      <p className="px-5 pb-2 text-caption text-text-tertiary">
        {t("family.knowledgeHint")}
      </p>

      {loading && !data ? (
        <div className="text-body text-text-secondary py-10 px-5 text-center">
          <span className="inline-flex items-center gap-2">
            <span className="inline-block w-2 h-2 rounded-full bg-text-tertiary animate-pulse" />
            {t("family.loading")}
          </span>
        </div>
      ) : empty ? (
        <div className="text-body text-text-secondary py-10 px-5 text-center">
          {t("family.knowledgeEmpty")}
          <div className="text-caption text-text-tertiary mt-1">
            {t("family.knowledgeEmptyHint")}
          </div>
        </div>
      ) : (
        <div className="px-2">
          {sections.map((sec, idx) => {
            const open = isOpen(sec.key);
            return (
              <div
                key={sec.key}
                className={idx > 0 ? "border-t border-border" : ""}
              >
                <button
                  type="button"
                  aria-expanded={open}
                  onClick={() =>
                    setCollapsed((s) => ({ ...s, [sec.key]: open }))
                  }
                  className="w-full flex items-center justify-between py-2.5 px-3 rounded-md hover:bg-[color-mix(in_srgb,var(--color-bg-tertiary),transparent_50%)] transition-colors"
                >
                  <span className="flex items-center gap-2 min-w-0">
                    <span className="text-text-tertiary shrink-0">
                      {open ? <IconChevronDown /> : <IconChevronRight />}
                    </span>
                    <span className="text-title text-text-primary">
                      {sec.title}
                    </span>
                    <span className="text-caption-mono text-text-tertiary num">
                      {sec.count}
                    </span>
                  </span>
                </button>

                {open && (
                  <div className="pl-5 pr-1 pb-2">
                    {sec.kind === "entries" &&
                      sec.sub.map((s) => (
                        <div key={s.label || sec.key} className="mb-1">
                          {s.label && (
                            <div className="text-caption-mono text-text-tertiary mt-1 mb-0.5">
                              {s.label}
                            </div>
                          )}
                          <div className="divide-y divide-border">
                            {s.items.map((e) => (
                              <EntryRow
                                key={e.id}
                                entry={e}
                                onOpen={() => setSelected(e)}
                              />
                            ))}
                          </div>
                        </div>
                      ))}

                    {sec.kind === "orphan" && (
                      <div className="divide-y divide-border">
                        {sec.items.map((e) => (
                          <EntryRow
                            key={e.id}
                            entry={e}
                            showSubject
                            onOpen={() => setSelected(e)}
                          />
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      <div className="border-t border-border px-5 pt-3 pb-4 mt-1">
        <AddEntryForm
          types={NON_MEMBER_TYPE_ORDER}
          addLabel={t("family.addEntry")}
          withSubjectName
          subjectNamePlaceholder={t("family.subjectPlaceholder")}
          onAdd={(input) =>
            run(
              () =>
                addHomeEntry({
                  type: input.type,
                  content: input.content,
                  subjectName: input.subjectName ?? null,
                }),
              t("family.added"),
            )
          }
        />
      </div>

      {selected && (
        <EntryDetailSheet
          entry={selected}
          persons={persons}
          onClose={() => setSelected(null)}
          {...bindEntryActions(run, selected)}
        />
      )}
    </section>
  );
}
