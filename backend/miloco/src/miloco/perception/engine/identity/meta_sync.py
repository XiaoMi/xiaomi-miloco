# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""把 SQL ``person(name, role)`` 同步进文件层 ``persons/<id>/meta.json``。

SQL 是单一事实源（真名唯一、角色可空），meta.json 是感知层每窗读取的缓存。新装 /
重启时跑一次，让"有样本目录但 meta.json 缺失 / 漂移"的 person 把真名 / 角色补齐——否则
``get_name`` 返 None，omni prompt 渲染退化成 UUID。

幂等：只在缺失 / 与 SQL 漂移时才写。best-effort：DB 不可达或单条失败都吞掉，
绝不阻塞启动。
"""

import logging

logger = logging.getLogger(__name__)


def sync_person_meta_from_sql(library) -> dict:
    """遍历 SQL person，把 name/role 同步进 meta.json。

    Returns:
        dict: ``{"synced": 修了几个 person, "orphans": 孤儿目录数, "total": SQL person 总数}``
    """
    try:
        from miloco.database.person_repo import PersonRepo

        persons = PersonRepo().get_all()
    except Exception as e:  # noqa: BLE001
        logger.warning("meta sync: 读 SQL person 失败，跳过 backfill：%s", e)
        return {"synced": 0, "orphans": 0, "total": 0}

    sql_ids: set[str] = set()
    synced = 0
    for p in persons:
        sql_ids.add(p.id)
        # 只对"已有样本目录"的 person 同步——无样本 person(仅在 SQL、尚未注册样本)的 meta
        # 文件层无人消费, 且 set_meta/_write_person_meta 会凭空 mkdir, 让 list_persons 多出
        # (pid,False,0,0) 扰动 IdentityEngine snapshot(与 router.update_person 守卫一致)。
        # backfill 的真正目标是"有样本但缺 meta.json 的老 person"。
        if not library.has_person_dir(p.id):
            continue
        try:
            # SQL 权威：name/role 与文件层不一致就以 SQL 为准重写。一次读(get_name_role) + 一次
            # 合并写(library.set_meta, omit 不动的字段), 避免两次 IO; synced 按 person 计数。
            cur_name, cur_role = library.get_name_role(p.id)
            meta_fields: dict = {}
            if cur_name != p.name:
                meta_fields["name"] = p.name
            if cur_role != p.role:
                meta_fields["role"] = p.role
            if meta_fields:
                library.set_meta(p.id, **meta_fields)
                synced += 1
        except Exception:  # noqa: BLE001
            logger.warning("meta sync: 同步 person_id=%s 失败", p.id, exc_info=True)

    # 孤儿检测：文件层有目录但 SQL 无对应行（只报告，不动文件——删除留给人工裁决）
    orphans = 0
    try:
        pd = library.persons_dir
        if pd.is_dir():
            for d in pd.iterdir():
                if d.is_dir() and not d.name.startswith(".") and d.name not in sql_ids:
                    orphans += 1
                    logger.warning(
                        "meta sync: 孤儿 person 目录（SQL 无对应行）person_id=%s", d.name
                    )
    except Exception:  # noqa: BLE001
        pass

    if orphans:
        # orphan = 盘上有 person 目录但 SQL 无对应行。这些人不进 SQL 驱动的家庭档案 roster,
        # omni prompt 会渲染成"(暂无已注册成员)"——正是该类上报的症状。升 WARNING 并点明含义,
        # 让静默矛盾变可操作信号（成因多为 home/db 漂移，或删人时 rmtree 残留；修复留人工裁决）。
        logger.warning(
            "meta sync 完成：synced=%d orphans=%d total=%d —— 有 %d 个 person 目录在盘但 SQL 无对应行,"
            "这些成员不会进家庭档案 roster、omni 可能渲染“暂无已注册成员”;"
            "常见成因：MILOCO_HOME/db 路径漂移，或删除成员时目录未清干净。请人工核实后决定重注册 / 恢复 db / 删目录。",
            synced, orphans, len(persons), orphans,
        )
    elif synced:
        logger.info(
            "meta sync 完成：synced=%d orphans=%d total=%d", synced, orphans, len(persons)
        )
    return {"synced": synced, "orphans": orphans, "total": len(persons)}
