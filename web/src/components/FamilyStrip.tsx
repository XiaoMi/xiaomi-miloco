/**
 * 「家庭成员」选择条——横向 chip 选择器（头像 + 名字，选中态品牌色高亮）。
 * 选中某人后，其档案在下方独立的「成员档案」卡里展开（不再嵌在本卡内）。
 * 身份是否采集靠头像右下角 success 对勾角标体现，不再用「已录入 / 未录入」文字。
 * 末尾一个虚线「+ 添加家人」chip 承载新增入口。
 */

import { useTranslation } from "react-i18next";
import type { Person, Pet } from "@/lib/types";
import { PersonAvatar } from "@/components/PersonAvatar";
import { PetAvatar } from "@/components/PetAvatar";
import { Switch } from "@/components/Switch";
import { IconPlus } from "@/lib/icons";

interface Props {
  persons: Person[];
  selectedId: string | null;
  onSelect: (p: Person) => void;
  onAddPerson: () => void;
  // 宠物成员（实验性）——onTogglePets 存在时渲染开关行；petsEnabled 为真时再渲染宠物 chip。
  pets?: Pet[];
  petsEnabled?: boolean;
  selectedPetId?: string | null; // 选中的宠物（下方展开档案卡，与人类互斥）
  onSelectPet?: (p: Pet) => void; // 点宠物 chip 选中（toggle 由上层处理）
  onAddPet?: () => void;
  onTogglePets?: (enabled: boolean) => void;
}

export function FamilyStrip({
  persons,
  selectedId,
  onSelect,
  onAddPerson,
  pets,
  petsEnabled = false,
  selectedPetId,
  onSelectPet,
  onAddPet,
  onTogglePets,
}: Props) {
  const { t } = useTranslation();
  return (
    <section
      aria-labelledby="family-title"
      className="rounded-xl bg-bg-secondary border border-border shadow-sm anim-in"
    >
      <div className="p-5">
        <div className="mb-4">
          <h2
            id="family-title"
            className="text-title text-text-primary inline-flex items-baseline gap-2"
          >
            {t("family.stripTitle")}
            {persons.length > 0 && (
              <span className="text-caption-mono text-text-tertiary font-normal num">
                {t("family.stripCount", { count: persons.length })}
              </span>
            )}
          </h2>
          {persons.length > 0 && (
            <p className="text-caption text-text-tertiary mt-1">
              {t("family.stripHint")}
            </p>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {persons.map((p) => (
            <PersonChip
              key={p.id}
              person={p}
              selected={p.id === selectedId}
              onClick={() => onSelect(p)}
            />
          ))}
          <button
            type="button"
            onClick={onAddPerson}
            className="inline-flex items-center gap-1 h-11 px-3.5 rounded-full border border-dashed border-border text-caption text-text-tertiary hover:text-text-primary hover:border-border-strong transition-colors"
          >
            <IconPlus width={14} height={14} />
            {t("family.addPerson")}
          </button>
        </div>

        {persons.length === 0 && (
          <div className="text-caption text-text-tertiary mt-3">
            {t("family.stripEmpty")}
          </div>
        )}

        {/* 宠物成员（实验性）—— 开关常驻（关闭时也能再开启）；开启后展示宠物 chip */}
        {onTogglePets && (
          <div className="mt-5 pt-4 border-t border-border">
            <div className="flex items-center justify-between gap-3 mb-3">
              <h3 className="text-body text-text-primary inline-flex items-center gap-2">
                {t("pet.memberTitle")}
                <span className="text-caption text-warning font-medium px-1.5 py-0.5 rounded bg-warning-bg">
                  {t("pet.experimentalBadge")}
                </span>
              </h3>
              <Switch
                checked={petsEnabled}
                onChange={() => onTogglePets(!petsEnabled)}
                label={t("pet.experimentalHint")}
              />
            </div>
            {petsEnabled && (
              <div className="flex flex-wrap items-center gap-2">
                {(pets ?? []).map((p) => (
                  <PetChip
                    key={p.id}
                    pet={p}
                    selected={p.id === selectedPetId}
                    onClick={() => onSelectPet?.(p)}
                  />
                ))}
                <button
                  type="button"
                  onClick={onAddPet}
                  className="inline-flex items-center gap-1 h-11 px-3.5 rounded-full border border-dashed border-border text-caption text-text-tertiary hover:text-text-primary hover:border-border-strong transition-colors"
                >
                  <IconPlus width={14} height={14} />
                  {t("pet.addPet")}
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </section>
  );
}

function PetChip({
  pet,
  selected,
  onClick,
}: {
  pet: Pet;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={selected}
      title={pet.species ? `${pet.name} · ${pet.species}` : pet.name}
      className={`inline-flex items-center gap-2 h-11 pl-1 pr-3.5 rounded-full border transition-colors ${
        selected
          ? "bg-brand-soft border-brand-primary text-brand-primary"
          : "bg-bg-primary border-border text-text-secondary hover:text-text-primary hover:border-border-strong"
      }`}
    >
      <PetAvatar pet={pet} size={34} />
      <span className="text-body truncate max-w-[7rem]">{pet.name}</span>
    </button>
  );
}

function PersonChip({
  person,
  selected,
  onClick,
}: {
  person: Person;
  selected: boolean;
  onClick: () => void;
}) {
  const { t } = useTranslation();
  const faceText = person.faceEnrolled
    ? t("family.faceEnrolled")
    : t("family.faceNotEnrolled");
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={selected}
      title={`${person.name} · ${faceText}`}
      className={`inline-flex items-center gap-2 h-11 pl-1 pr-3.5 rounded-full border transition-colors ${
        selected
          ? "bg-brand-soft border-brand-primary text-brand-primary"
          : "bg-bg-primary border-border text-text-secondary hover:text-text-primary hover:border-border-strong"
      }`}
    >
      <PersonAvatar person={person} size={34} badge />
      <span className="text-body truncate max-w-[7rem]">{person.name}</span>
    </button>
  );
}
