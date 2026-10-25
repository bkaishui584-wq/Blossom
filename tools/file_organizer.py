"""Registered tools for previewing, applying and undoing local organization."""

from tools.registry import Tool, ToolError


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        raise ToolError(str(exc))


def register_file_organizer_tools(registry, service=None):
    def preview(root, mode="type"):
        if service is None:
            raise ToolError("文件整理服务没有接上")
        result = _call(service.preview, root, mode=mode)
        data = result.get("data") or {}
        data["items"] = list(data.get("items") or [])[:8]
        data["duplicates"] = [
            {"size": row.get("size"), "paths": list(row.get("paths") or [])[:3]}
            for row in list(data.get("duplicates") or [])[:5]
        ]
        data["empty"] = list(data.get("empty") or [])[:8]
        data["old"] = list(data.get("old") or [])[:8]
        result["data"] = data
        return result

    def apply(operation_id):
        if service is None:
            raise ToolError("文件整理服务没有接上")
        result = _call(service.apply, operation_id)
        if not result.get("ok"):
            raise ToolError(result.get("error") or "整理没有完成")
        return result

    def undo(operation_id):
        if service is None:
            raise ToolError("文件整理服务没有接上")
        result = _call(service.undo, operation_id)
        if not result.get("ok"):
            raise ToolError(result.get("error") or "撤销没有完成")
        return result

    def history(limit=30):
        if service is None:
            raise ToolError("文件整理服务没有接上")
        return _call(service.history, limit)

    registry.register(
        Tool(
            name="organize_preview",
            description="预览按文件类型整理一个授权目录，不移动文件；同时列出重复、空文件和旧文件。",
            schema={
                "type": "object",
                "properties": {
                    "root": {"type": "string", "description": "已经授权的目录"},
                    "mode": {"type": "string", "enum": ["type"], "description": "整理方式"},
                },
                "required": ["root"],
                "additionalProperties": False,
            },
            permission="LOW",
            risk="只生成整理计划和数据库记录，不移动文件",
            idempotent=True,
            handler=preview,
            keywords=("整理文件", "文件分类", "重复文件", "空文件", "旧文件"),
        )
    )
    registry.register(
        Tool(
            name="organize_apply",
            description="执行一份已预览的整理计划。目标同名时安全停止，不覆盖任何文件。",
            schema={
                "type": "object",
                "properties": {
                    "operation_id": {"type": "string", "description": "预览返回的计划编号"}
                },
                "required": ["operation_id"],
                "additionalProperties": False,
            },
            permission="HIGH",
            risk="会移动授权目录里的文件",
            idempotent=False,
            handler=apply,
            keywords=("执行整理", "移动文件", "文件分类"),
        )
    )
    registry.register(
        Tool(
            name="organize_undo",
            description="把一次整理操作中的文件移回原位置。原位置有同名文件时停止，不覆盖。",
            schema={
                "type": "object",
                "properties": {
                    "operation_id": {"type": "string", "description": "整理操作编号"}
                },
                "required": ["operation_id"],
                "additionalProperties": False,
            },
            permission="HIGH",
            risk="会移动文件并恢复原路径",
            idempotent=False,
            handler=undo,
            keywords=("撤销整理", "恢复文件", "文件移回"),
        )
    )
    registry.register(
        Tool(
            name="organize_history",
            description="查看最近的本地文件整理记录，只读。",
            schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "最多几条"}},
                "required": [],
                "additionalProperties": False,
            },
            permission="READ",
            risk="只读整理历史",
            readonly=True,
            handler=history,
            keywords=("整理记录", "文件操作历史"),
        )
    )
    return registry
