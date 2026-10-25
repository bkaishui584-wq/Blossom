"""把 Skill 系统暴露成一个只读工具，方便模型在对话里查询能力清单。

Skill 的执行入口在 Agent Core，不在工具层，避免模型绕过权限直接跑 Skill。
"""

from tools.registry import Tool


def skills_line(rows):
    if not rows:
        return "现在没有装任何 Skill。"
    names = [str(row.get("name")) for row in rows if row.get("enabled")]
    if not names:
        return "已装的 Skill 都被停用了。"
    return "可用的 Skill：" + "、".join(names[:8]) + "。"


def skills_tool(manager):
    def handler(kind=""):
        rows = manager.rows() if manager is not None else []
        wanted = str(kind or "").strip().lower()
        if wanted:
            rows = [row for row in rows if wanted in str(row.get("name"))]
        return {"skills": rows}

    return Tool(
        name="list_skills",
        description="查看当前装了哪些 Skill，以及它们需要什么权限",
        schema={
            "type": "object",
            "properties": {"kind": {"type": "string", "description": "按名字过滤，可留空"}},
            "required": [],
            "additionalProperties": False,
        },
        permission="READ",
        risk="只读能力清单",
        readonly=True,
        handler=handler,
        summarize=skills_line,
        keywords=("技能", "skill", "能力清单"),
    )
