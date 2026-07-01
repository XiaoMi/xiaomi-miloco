/**
 * 宠物档案面板——家庭 tab 单页布局：点宠物 chip 后在「家庭成员」条下方就地展开（与人类成员卡互斥）。
 * 头部：头像 + 名字 + 物种药丸 + 特征数；右侧**唯一**「编辑」入口唤起 PetDrawer 做改名/物种/换头像/删除。
 * 主体：该宠物名下的家庭档案事实（外观、习惯等 member_* 条目）按类型分组**只读**展示，
 * 与人类成员同构（宠物在 home_profile 里就是一个 subject_id=pet_id 的主体，可挂多条事实）。
 */
import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import type { HomeEntries, Pet } from "@/lib/types";
import { PetAvatar } from "@/components/PetAvatar";
import { MEMBER_TYPE_ORDER, isMemberType, typeLabel } from "./HomeProfileParts";

interface Props {
  pet: Pet;
  entries: HomeEntries | undefined;
  loading: boolean;
  onEdit: () => void;
}

export function PetProfilePanel({ pet, entries, loading, onEdit }: Props) {
  const { t } = useTranslation();

  const profile = useMemo(
    () =>
      (entries?.profile ?? []).filter(
        (e) => e.subjectId === pet.id && isMemberType(e.type),
      ),
    [entries, pet.id],
  );
  const groups = MEMBER_TYPE_ORDER.map((type) => ({
    type,
    items: profile.filter((e) => e.type === type),
  })).filter((g) => g.items.length > 0);
  const empty = !loading && profile.length === 0;

  return (
    <section
      aria-labelledby="pet-profile-title"
      className="rounded-xl bg-bg-secondary border border-border shadow-sm anim-in"
    >
      {/* 头部：头像 + 名字/物种 + 特征数；右侧唯一「编辑」 */}
      <div className="flex items-center gap-3.5 px-5 py-4 border-b border-border">
        <PetAvatar pet={pet} size={48} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 min-w-0">
            <h3
              id="pet-profile-title"
              className="text-title text-text-primary truncate"
            >
              {pet.name}
            </h3>
            {pet.species && (
              <span className="shrink-0 text-caption text-text-secondary font-normal px-2 py-0.5 rounded-md bg-bg-tertiary">
                {pet.species}
              </span>
            )}
          </div>
          {profile.length > 0 && (
            <div className="text-caption text-text-tertiary mt-1.5">
              <span className="num">{profile.length}</span>{" "}
              {t("family.featureCount")}
            </div>
          )}
        </div>
        <button
          type="button"
          onClick={onEdit}
          className="shrink-0 text-caption px-3 py-1.5 rounded-md bg-bg-primary border border-border text-text-secondary hover:text-text-primary hover:border-border-strong transition-colors"
        >
          {t("pet.edit")}
        </button>
      </div>

      {/* 主体：宠物名下的档案事实（只读，按类型分组） */}
      <div className="px-5 pt-4 pb-4">
        {loading && !entries ? (
          <div className="text-body text-text-secondary py-10 text-center">
            <span className="inline-flex items-center gap-2">
              <span className="inline-block w-2 h-2 rounded-full bg-text-tertiary animate-pulse" />
              {t("family.loading")}
            </span>
          </div>
        ) : empty ? (
          <div className="text-body text-text-secondary py-10 text-center">
            {t("pet.profilePetEmpty", { name: pet.name })}
            <div className="text-caption text-text-tertiary mt-1">
              {t("pet.profilePetEmptyHint")}
            </div>
          </div>
        ) : (
          <div className="space-y-4">
            {groups.map((g) => (
              <section key={g.type}>
                <h4 className="text-caption text-text-tertiary mb-1">
                  {typeLabel(g.type)}
                </h4>
                <div className="divide-y divide-border">
                  {g.items.map((e) => (
                    <div key={e.id} className="py-2 text-body text-text-primary">
                      {e.content}
                    </div>
                  ))}
                </div>
              </section>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
