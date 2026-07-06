__all__ = [
    "MILOCO_IM_PUSH",
    "MILOCO_HABIT_SUGGEST",
]


MILOCO_IM_PUSH = {
    "type": "function",
    "name": "miloco_im_push",
    "description": (
        "向主人推送一条 IM 通知消息。配合 miloco-notify skill 使用，"
        "用于主动告知主人重要事项或等待确认的内容。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "要推送给主人的通知正文内容。",
            },
        },
        "required": ["message"],
    },
}


MILOCO_HABIT_SUGGEST = {
    "type": "function",
    "name": "miloco_habit_suggest",
    "description": (
        "对习惯建议候选库进行读写，用于防骚扰状态机管理。"
        "action 取值：list 查询候选；record 记录新建议；"
        "mark_asked 标记已询问；resolve 结束某条建议。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "record", "mark_asked", "resolve"],
                "description": "对习惯建议候选库执行的操作。",
            },
            "key": {
                "type": "string",
                "description": "可选。习惯建议的唯一标识键。",
            },
            "subject": {
                "type": "string",
                "description": "可选。建议所属主题。",
            },
            "habit": {
                "type": "string",
                "description": "可选。目标习惯名称。",
            },
            "suggestion": {
                "type": "string",
                "description": "可选。建议的具体内容文案。",
            },
            "title": {
                "type": "string",
                "description": "可选。建议标题。",
            },
            "evidence": {
                "type": "string",
                "description": "可选。支撑该建议的依据。",
            },
            "item_id": {
                "type": "string",
                "description": "可选。候选条目 ID。",
            },
            "outcome": {
                "type": "string",
                "enum": ["accepted", "rejected", "created"],
                "description": "可选。建议处理结果。",
            },
            "task_id": {
                "type": "string",
                "description": "可选。关联的任务 ID。",
            },
            "reason": {
                "type": "string",
                "description": "可选。处理原因说明。",
            },
        },
        "required": ["action"],
    },
}
