/**
 * 家庭档案（home_profile）共享零件——成员抽屉与非人面板共用。
 *
 * 交互模型：列表只平静展示一行内容（点整行 → 弹详情卡 EntryDetailSheet），
 * 来源用大白话解释、编辑/删除 或 确认/忽略 都收进详情卡里——不再用 hover 出按钮
 * （触屏用不了），也不在列表行甩「亲述 100%」这类小白看不懂的 jargon。
 */

import {
  addHomeEntry,
  commitHomeProfile,
  confirmCandidate,
  deleteHomeEntry,
  ignoreCandidate,
  updateHomeEntry,
} from "@/api";
import { PersonAvatar } from "@/components/PersonAvatar";
import { useEscClose } from "@/hooks/useEscClose";
import {
  IconCheck,
  IconChevronRight,
  IconEye,
  IconPencil,
  IconPlus,
  IconX,
} from "@/lib/icons";
import type { HomeEntry, HomeEntryType, Person } from "@/lib/types";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import i18n from "@/i18n";
import { toast } from "./Toast";

// ── 文案 / 分组顺序（对齐 backend render.py）──────────────────
// 类型 → i18n key 映射；展示文案由 typeLabel(type) 经 i18n 解析。
const TYPE_LABEL_KEY: Record<HomeEntryType, string> = {
  member_persona: "family.typeMemberPersona",
  member_health: "family.typeMemberHealth",
  member_routine: "family.typeMemberRoutine",
  member_entertain: "family.typeMemberEntertain",
  member_preference: "family.typeMemberPreference",
  family: "family.typeFamily",
  space: "family.typeSpace",
  device: "family.typeDevice",
};

export function typeLabel(type: HomeEntryType): string {
  return i18n.t(TYPE_LABEL_KEY[type]);
}

export const MEMBER_TYPE_ORDER: HomeEntryType[] = [
  "member_persona",
  "member_health",
  "member_routine",
  "member_entertain",
  "member_preference",
];

export const NON_MEMBER_TYPE_ORDER: HomeEntryType[] = [
  "family",
  "space",
  "device",
];

export const isMemberType = (t: HomeEntryType): boolean =>
  t.startsWith("member_");

// 归属判定：member 类型且 subjectId 命中某个已知家人 → 归该成员（进其抽屉）；
// 宠物条目（subjectId 是 pet_*）→ 归宠物档案卡，见 hostedByPetCard；
// 其余一律归家庭档案面板（含 family/space/device 与「没认出是谁」的 member_*）。
// 保证每条 profile/candidate 都恰好出现在一处，绝不从界面上蒸发、也不在两处重复。
export function ownerOf(entry: HomeEntry, persons: Person[]): Person | null {
  if (!isMemberType(entry.type) || !entry.subjectId) return null;
  return persons.find((p) => p.id === entry.subjectId) ?? null;
}

/** subjectId 形如 pet_* → 宠物主体条目。纯前缀判定（不查花名册），给动作守卫用：
 *  花名册未加载时也要拦住「改派给家人」，宁可少给一个动作也不要把宠物条目改成人的。 */
export function isPetSubject(entry: HomeEntry): boolean {
  return Boolean(entry.subjectId?.startsWith("pet_"));
}

/** 该条目是否由「宠物档案卡」承载（→ 家庭档案面板不再重复展示）。
 *  前缀 + 花名册命中（与后端 service 的 _is_pet_entry 同口径）；花名册还没到（undefined）时
 *  按前缀先当宠物，避免宠物外观在「共享信息」里闪现一帧。 */
export function hostedByPetCard(entry: HomeEntry, pets?: { id: string }[]): boolean {
  if (!isPetSubject(entry)) return false;
  return pets === undefined || pets.some((p) => p.id === entry.subjectId);
}

// ── 写执行器：写 → commit 重渲染 md → toast → reload ─────────
// 任一 write 失败抛出（real.ts 已把 backend message 透出），由调用方据此维持态；
// commit 失败仅吞掉（md 不同步不该挡住数据已落盘的事实）。
export function useHomeWrite(onChanged: () => void) {
  const { t } = useTranslation();
  // fn 的返回值一律忽略（只 await 完成与否）→ 用 unknown 收，让返回 id 的写操作
  // （如 addHomeEntry: Promise<string>）也能直接传进来。
  return async (fn: () => Promise<unknown>, okMsg?: string): Promise<void> => {
    try {
      await fn();
    } catch (e) {
      toast(e instanceof Error ? e.message : t("family.operationFail"), "warn");
      throw e;
    }
    try {
      await commitHomeProfile();
    } catch {
      /* md 重渲染失败不阻塞：数据已落盘，下次 commit 会补 */
    }
    if (okMsg) toast(okMsg, "ok");
    onChanged();
  };
}

// ── 来源大白话 + 友好日期（仅详情卡用，列表不展示）──────────
// 当前年省略年份，跨年才带上——详情里看「起止时间」不至于歧义。
function friendlyDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const sameYear = d.getFullYear() === new Date().getFullYear();
  // 英文走 Intl 本地化日期(Jun 15 / Jun 15, 2025);中文沿用「数字+单位」拼法。
  // 不能用「数字+单位」拼英文——会拼出 "2025-6/15" 这种畸形日期。
  if (i18n.language === "en") {
    return new Intl.DateTimeFormat("en", {
      month: "short",
      day: "numeric",
      ...(sameYear ? {} : { year: "numeric" }),
    }).format(d);
  }
  const md = `${d.getMonth() + 1}${i18n.t("family.dateUnitMonth")}${d.getDate()}${i18n.t("family.dateUnitDay")}`;
  return sameYear ? md : `${d.getFullYear()}${i18n.t("family.dateUnitYear")}${md}`;
}

// 候选观察小结：first/last/count 拼一句自然语言，替代表格式 dl。
function observeSummary(entry: HomeEntry): string {
  const first = friendlyDate(entry.firstSeen);
  const last = friendlyDate(entry.lastSeen);
  const when =
    first && last && first !== last
      ? i18n.t("family.observeRangeTo", { first, last })
      : first || last;
  const n = entry.evidenceCount;
  if (when && n > 0) return i18n.t("family.observeRangeAndCount", { when, count: n });
  if (when) return when;
  return n > 0 ? i18n.t("family.observeCount", { count: n }) : "";
}

// 证据条目拆「时间 + 描述」（格式 "YYYY-MM-DD HH:MM-HH:MM: 描述"）；
// 拆不出就整条当描述。
function splitEvidence(ev: string): { time: string; text: string } {
  const idx = ev.indexOf(": ");
  if (idx === -1) return { time: "", text: ev };
  return { time: ev.slice(0, idx), text: ev.slice(idx + 2) };
}

// ── 平静列表行：只展示内容，点整行 → onOpen 弹详情卡 ─────────
// showSubject：在内容前缀里露出 subject_name（它一般是主语，如「奶奶 · 喜欢喝茶」）；
// 「共享信息」组用，成员档案内已按人归属故不传。
export function EntryRow({
  entry,
  onOpen,
  showSubject = false,
}: {
  entry: HomeEntry;
  onOpen: () => void;
  showSubject?: boolean;
}) {
  const { t } = useTranslation();
  const who = showSubject ? subjectTag(entry.subjectName) : "";
  return (
    <button
      type="button"
      onClick={onOpen}
      className="group w-full flex items-center gap-3 py-3 text-left transition-colors hover:bg-bg-tertiary/50 -mx-2 px-2 rounded-md"
    >
      <span className="min-w-0 flex-1 text-body text-text-primary break-words">
        {who && <span className="text-text-secondary">{who} · </span>}
        {entry.content}
      </span>
      <RowEditHint label={t("family.rowEdit")} />
      <IconChevronRight
        width={16}
        height={16}
        className="shrink-0 text-text-tertiary"
      />
    </button>
  );
}

// 行尾的「可点击」提示：hover（桌面）才浮现，触屏无 hover 不占视觉、靠整行点击。
// 正式条目可改用铅笔（编辑），候选只能确认/忽略不能直编，用眼睛（查看）。
function RowEditHint({ label, view = false }: { label: string; view?: boolean }) {
  const Icon = view ? IconEye : IconPencil;
  return (
    <span className="shrink-0 hidden sm:inline-flex items-center gap-1 text-caption text-text-tertiary opacity-0 transition-opacity group-hover:opacity-100">
      <Icon width={13} height={13} />
      {label}
    </span>
  );
}

// 候选行：左侧一个淡 info 圆点（非文字，避免每行重复「观察中」），
// 右侧露出归属（subjectName，成员名 / 空间设备名）。点整行 → 弹详情卡确认/忽略。
// showSubject：成员档案面板内已按人聚合，传 false 省掉重复的名字标签。
export function CandidateRow({
  entry,
  onOpen,
  showSubject = true,
}: {
  entry: HomeEntry;
  onOpen: () => void;
  showSubject?: boolean;
}) {
  const { t } = useTranslation();
  const who = showSubject ? subjectTag(entry.subjectName) : "";
  return (
    <button
      type="button"
      onClick={onOpen}
      className="group w-full flex items-center gap-3 py-3 text-left transition-colors hover:bg-bg-tertiary/50 -mx-2 px-2 rounded-md"
    >
      <span className="status-dot status-dot-info shrink-0" />
      <span className="min-w-0 flex-1 text-body text-text-primary break-words">
        {who && <span className="text-text-secondary">{who} · </span>}
        {entry.content}
      </span>
      <RowEditHint label={t("family.rowView")} view />
      <IconChevronRight
        width={16}
        height={16}
        className="shrink-0 text-text-tertiary"
      />
    </button>
  );
}

// 后端用 general / shared 这类占位词表示「无具体归属 / 全家共享」，展示时一律视作空。
export function isGenericSubject(name: string | null | undefined): boolean {
  if (!name) return true;
  const n = name.trim().toLowerCase();
  return n === "" || n === "general" || n === "shared";
}

// 归属标签：占位词 / 空 → 不展示，否则用 subjectName（成员名 / 空间设备名）。
function subjectTag(name: string | null | undefined): string {
  return isGenericSubject(name) ? "" : name!;
}

// 观察记录最多展示最近几条（evidence_log 为新→旧序，取前 N 即最近）。
const EVIDENCE_LIMIT = 3;

// ── 详情卡：查看 + 编辑/删除（正式）或 确认收下/忽略（候选）─────
// 移动端底部铺满、桌面居中；z-[70] 高于抽屉，ESC 入栈只关本卡。
// 任一写成功后关卡（列表已 reload，避免展示陈旧内容）。
export function EntryDetailSheet({
  entry,
  persons,
  onClose,
  onSave,
  onDelete,
  onReassign,
  onConfirm,
  onIgnore,
}: {
  entry: HomeEntry;
  // 传入家人列表即开启「关联成员」：把未关联的 member_* 记忆手动归到某人。
  persons?: Person[];
  onClose: () => void;
  // 正式条目
  onSave?: (content: string) => Promise<void>;
  onDelete?: () => Promise<void>;
  onReassign?: (personId: string, personName: string) => Promise<void>;
  // 候选条目
  onConfirm?: () => Promise<void>;
  onIgnore?: () => Promise<void>;
}) {
  const { t } = useTranslation();
  useEscClose(true, onClose);
  const isCandidate = !!onConfirm;
  // 可关联：开启了 onReassign + 有家人可选 + 本身是 member_* 记忆（正式态）。
  const canReassign =
    !isCandidate &&
    !!onReassign &&
    !!persons &&
    persons.length > 0 &&
    isMemberType(entry.type) &&
    // 宠物条目不给「改派给家人」：改派会把 subjectId 从 pet_* 换成 person_id，
    // 宠物档案卡按 pet_id 过滤就再也找不到它 → 卡片被清空、外观看似消失。
    // 删除按钮保留（孤儿 pet_* 条目的唯一清理入口）。
    !isPetSubject(entry);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(entry.content);
  const [confirmDel, setConfirmDel] = useState(false);
  const [assigning, setAssigning] = useState(false);
  const [busy, setBusy] = useState(false);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    try {
      await fn();
      onClose();
    } catch {
      /* toast 已弹，保留卡片供重试 */
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    const next = draft.trim();
    if (!next || next === entry.content) {
      setEditing(false);
      setDraft(entry.content);
      return;
    }
    if (!onSave) return;
    await run(() => onSave(next));
  };

  // 头部身份：能解析到家人就以「头像 + 姓名」为主、类型为副；否则类型唱主角。
  const owner = persons?.find((p) => p.id === entry.subjectId) ?? null;
  const ownerName = owner?.name ?? ownerHint(entry);
  const headerTitle = ownerName || typeLabel(entry.type);
  const headerSub = ownerName ? typeLabel(entry.type) : "";

  return (
    <div
      className="fixed inset-0 z-[70] flex items-end md:items-center justify-center bg-black/40 backdrop-blur-sm"
      onClick={(e) => {
        e.stopPropagation();
        onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="entry-detail-title"
        className="flex w-full max-h-[85vh] flex-col bg-bg-secondary border border-border rounded-t-2xl md:max-w-md md:rounded-2xl shadow-lg anim-in"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 移动端抓手 */}
        <div className="md:hidden flex justify-center pt-2.5 pb-1 shrink-0">
          <span className="h-1 w-9 rounded-full bg-border-strong" />
        </div>

        {/* 头部：身份（头像 + 姓名 / 类型）+ 关闭，底部分隔线锚定 */}
        <div className="flex items-center gap-3 px-5 pt-3 pb-3 border-b border-border shrink-0">
          {owner && <PersonAvatar person={owner} size={36} />}
          <div className="min-w-0 flex-1">
            <h2
              id="entry-detail-title"
              className="text-body font-semibold text-text-primary truncate"
            >
              {headerTitle}
            </h2>
            {headerSub && (
              <p className="text-caption text-text-tertiary truncate mt-0.5">
                {headerSub}
              </p>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            className="-mr-1.5 rounded-full p-1.5 text-text-tertiary hover:text-text-primary hover:bg-bg-tertiary transition-colors shrink-0"
            aria-label={t("family.close")}
          >
            <IconX width={18} height={18} />
          </button>
        </div>

        {/* 主体：内容 +（候选）观察记录，长则滚动 */}
        <div className="flex-1 overflow-y-auto px-5 pt-4 pb-5">
          {editing ? (
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              rows={3}
              autoFocus
              className="w-full px-3.5 py-2.5 rounded-xl bg-bg-primary border border-border focus:border-brand-primary focus:ring-2 focus:ring-brand-ring focus:outline-none text-body text-text-primary leading-relaxed resize-none"
            />
          ) : (
            <p className="text-title text-text-primary break-words leading-relaxed">
              {entry.content}
            </p>
          )}

          {/* 明细仅候选条目展示：观察小结 + 时间线（正式条目只看内容）。 */}
          {isCandidate && (
            <div className="mt-4 space-y-4">
              {observeSummary(entry) && (
                <p className="text-caption text-text-tertiary">
                  {observeSummary(entry)}
                </p>
              )}

              {entry.evidenceLog && entry.evidenceLog.length > 0 && (
                <div>
                  <p className="text-caption text-text-tertiary mb-2.5">
                    {t("family.observeRecord")}
                    {entry.evidenceLog.length > EVIDENCE_LIMIT && (
                      <span className="text-text-tertiary">
                        {t("family.observeRecentPrefix")}
                        <span className="num">{EVIDENCE_LIMIT}</span>
                        {t("family.observeRecentSuffix")}
                      </span>
                    )}
                  </p>
                  <ul className="space-y-0">
                    {entry.evidenceLog.slice(0, EVIDENCE_LIMIT).map((ev, i, shown) => {
                      const { time, text } = splitEvidence(ev);
                      const last = i === shown.length - 1;
                      return (
                        <li key={i} className="flex gap-3">
                          <div className="flex flex-col items-center pt-1.5">
                            <span className="h-1.5 w-1.5 rounded-full bg-border-strong shrink-0" />
                            {!last && (
                              <span className="w-px flex-1 bg-border my-1" />
                            )}
                          </div>
                          <div className="min-w-0 flex-1 pb-4">
                            {time && (
                              <p className="text-caption-mono text-text-tertiary num">
                                {time}
                              </p>
                            )}
                            <p className="text-caption text-text-secondary leading-relaxed break-words mt-0.5">
                              {text}
                            </p>
                          </div>
                        </li>
                      );
                    })}
                  </ul>
                </div>
              )}
            </div>
          )}

          {/* 关联成员选择器 */}
          {assigning && persons && (
            <div className="mt-4">
              <p className="text-caption text-text-tertiary mb-2">
                {t("family.linkToWhichMember")}
              </p>
              <div className="flex flex-wrap gap-2">
                {persons.map((p) => (
                  <button
                    key={p.id}
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      onReassign && run(() => onReassign(p.id, p.name))
                    }
                    className={`inline-flex items-center gap-2 text-caption pl-1 pr-3 py-1 rounded-full border transition-colors disabled:opacity-60 ${
                      p.id === entry.subjectId
                        ? "bg-brand-soft border-brand-primary text-brand-primary"
                        : "bg-bg-primary border-border text-text-secondary hover:text-text-primary hover:border-border-strong"
                    }`}
                  >
                    <PersonAvatar person={p} size={24} />
                    {p.name}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* 操作区 */}
        <div className="flex items-center justify-between gap-2 px-4 py-3 border-t border-border shrink-0">
          {editing ? (
            <>
              <span />
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => {
                    setEditing(false);
                    setDraft(entry.content);
                  }}
                  className="h-9 px-4 rounded-lg text-caption text-text-secondary hover:text-text-primary hover:bg-bg-tertiary transition-colors"
                >
                  {t("family.cancel")}
                </button>
                <button
                  type="button"
                  onClick={save}
                  disabled={busy}
                  className="h-9 px-4 rounded-lg text-caption font-semibold bg-brand-primary text-white hover:bg-brand-accent transition-colors disabled:opacity-60"
                >
                  {busy ? t("family.saving") : t("family.save")}
                </button>
              </div>
            </>
          ) : confirmDel ? (
            <>
              <span />
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setConfirmDel(false)}
                  className="h-9 px-4 rounded-lg text-caption text-text-secondary hover:text-text-primary hover:bg-bg-tertiary transition-colors"
                >
                  {t("family.cancel")}
                </button>
                <button
                  type="button"
                  onClick={() => onDelete && run(onDelete)}
                  disabled={busy}
                  className="h-9 px-4 rounded-lg text-caption font-semibold bg-error-bg text-error hover:bg-error hover:text-white transition-colors disabled:opacity-60"
                >
                  {busy ? t("family.deleting") : t("family.confirmDelete")}
                </button>
              </div>
            </>
          ) : assigning ? (
            <>
              <span />
              <button
                type="button"
                onClick={() => setAssigning(false)}
                disabled={busy}
                className="h-9 px-4 rounded-lg text-caption text-text-secondary hover:text-text-primary hover:bg-bg-tertiary transition-colors disabled:opacity-60"
              >
                {t("family.cancel")}
              </button>
            </>
          ) : isCandidate ? (
            <>
              <button
                type="button"
                onClick={() => onIgnore && run(onIgnore)}
                disabled={busy}
                className="inline-flex items-center gap-1 h-9 px-4 rounded-lg text-caption text-text-tertiary hover:text-text-primary hover:bg-bg-tertiary transition-colors disabled:opacity-60"
              >
                <IconX width={14} height={14} />
                {t("family.ignore")}
              </button>
              <button
                type="button"
                onClick={() => onConfirm && run(onConfirm)}
                disabled={busy}
                className="inline-flex items-center gap-1.5 h-9 px-4 rounded-lg text-caption font-semibold bg-brand-primary text-white hover:bg-brand-accent transition-colors disabled:opacity-60"
              >
                <IconCheck width={14} height={14} />
                {t("family.saveToHomeProfile")}
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                onClick={() => setConfirmDel(true)}
                className="h-9 px-4 rounded-lg text-caption text-text-tertiary hover:text-error hover:bg-error-bg transition-colors"
              >
                {t("family.delete")}
              </button>
              <div className="flex items-center gap-2">
                {canReassign && (
                  <button
                    type="button"
                    onClick={() => setAssigning(true)}
                    className="h-9 px-4 rounded-lg text-caption bg-bg-secondary border border-border text-text-secondary hover:text-text-primary hover:border-border-strong transition-colors"
                  >
                    {t("family.linkMember")}
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => {
                    setDraft(entry.content);
                    setEditing(true);
                  }}
                  className="h-9 px-4 rounded-lg text-caption font-semibold bg-brand-primary text-white hover:bg-brand-accent transition-colors"
                >
                  {t("family.edit")}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// 详情卡顶部的归属提示：space/device 用 subjectName，「没认出是谁」的 member 也带上。
function ownerHint(entry: HomeEntry): string {
  return isGenericSubject(entry.subjectName) ? "" : entry.subjectName!;
}

// ── 新增表单（折叠式）；折叠态触发器统一为「家庭成员」同款虚线圆角 chip ──
export function AddEntryForm({
  types,
  addLabel,
  withSubjectName = false,
  subjectNamePlaceholder,
  onAdd,
}: {
  types: HomeEntryType[];
  addLabel?: string;
  withSubjectName?: boolean;
  subjectNamePlaceholder?: string;
  onAdd: (input: {
    type: HomeEntryType;
    content: string;
    subjectName?: string;
  }) => Promise<void>;
}) {
  const { t } = useTranslation();
  const addLabelText = addLabel ?? t("family.addEntry");
  const subjectPlaceholderText = subjectNamePlaceholder ?? t("family.subjectOptional");
  const [open, setOpen] = useState(false);
  const [type, setType] = useState<HomeEntryType>(types[0]);
  const [content, setContent] = useState("");
  const [subjectName, setSubjectName] = useState("");
  const [busy, setBusy] = useState(false);

  const reset = () => {
    setType(types[0]);
    setContent("");
    setSubjectName("");
  };

  const submit = async () => {
    const text = content.trim();
    if (!text) return;
    setBusy(true);
    try {
      await onAdd({
        type,
        content: text,
        subjectName: withSubjectName
          ? subjectName.trim() || undefined
          : undefined,
      });
      reset();
      setOpen(false);
    } catch {
      /* toast 已弹，保留表单内容供重试 */
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-1 h-11 px-3.5 rounded-full border border-dashed border-border text-caption text-text-tertiary hover:text-text-primary hover:border-border-strong transition-colors"
      >
        <IconPlus width={14} height={14} />
        {addLabelText}
      </button>
    );
  }

  return (
    <div className="rounded-lg bg-bg-primary border border-border p-3 space-y-2">
      <div className="flex gap-2">
        <select
          value={type}
          onChange={(e) => setType(e.target.value as HomeEntryType)}
          className="text-caption px-2 py-1.5 rounded-md bg-bg-secondary border border-border text-text-primary focus:border-brand-primary focus:outline-none"
        >
          {types.map((ty) => (
            <option key={ty} value={ty}>
              {typeLabel(ty)}
            </option>
          ))}
        </select>
        {withSubjectName && (
          <input
            value={subjectName}
            onChange={(e) => setSubjectName(e.target.value)}
            placeholder={subjectPlaceholderText}
            className="text-caption flex-1 min-w-0 px-2 py-1.5 rounded-md bg-bg-secondary border border-border text-text-primary focus:border-brand-primary focus:outline-none"
          />
        )}
      </div>
      <textarea
        value={content}
        onChange={(e) => setContent(e.target.value)}
        rows={2}
        autoFocus
        placeholder={t("family.entryContentPlaceholder")}
        className="w-full px-3 py-2 rounded-lg bg-bg-secondary border border-border focus:border-brand-primary focus:outline-none text-body text-text-primary resize-none"
      />
      <div className="flex justify-end gap-2">
        <button
          type="button"
          onClick={() => {
            reset();
            setOpen(false);
          }}
          className="text-caption px-3 py-1.5 rounded-md bg-bg-secondary border border-border text-text-secondary hover:text-text-primary"
        >
          {t("family.cancel")}
        </button>
        <button
          type="button"
          onClick={submit}
          disabled={!content.trim() || busy}
          className="text-caption px-3 py-1.5 rounded-md bg-brand-primary text-white hover:bg-brand-accent disabled:opacity-60"
        >
          {busy ? t("family.adding") : t("family.add")}
        </button>
      </div>
    </div>
  );
}

// ── 行内写操作绑定（把 useHomeWrite 包成详情卡的回调）──────────
export function bindEntryActions(
  run: (fn: () => Promise<void>, okMsg?: string) => Promise<void>,
  entry: HomeEntry,
) {
  return {
    onSave: (content: string) =>
      run(() => updateHomeEntry(entry.id, { content })),
    onDelete: () => run(() => deleteHomeEntry(entry.id), i18n.t("family.deleted")),
    onReassign: (personId: string, personName: string) =>
      run(
        () =>
          updateHomeEntry(entry.id, {
            subjectId: personId,
            subjectName: personName,
          }),
        i18n.t("family.linked"),
      ),
  };
}

export function bindCandidateActions(
  run: (fn: () => Promise<void>, okMsg?: string) => Promise<void>,
  entry: HomeEntry,
) {
  return {
    onConfirm: () => run(() => confirmCandidate(entry.id), i18n.t("family.confirmed")),
    onIgnore: () => run(() => ignoreCandidate(entry.id)),
  };
}

export { addHomeEntry };
