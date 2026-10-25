"""人格与提示词的拼装顺序：谁压过谁，写成一张表。

从高到低：

    安全与权限规则 > 当前指令 > 用户自定义人格 > 已确认习惯 > 推断的偏好 > 默认行为

这张表只描述顺序，不碰内容。真正拼提示词的是 persona.interface.build_persona_note，
界面和测试拿这张表来核对顺序有没有被改乱。
"""

ORDER = (
    ("safety", "安全与权限规则", "程序固定，人格和指令都压不过它"),
    ("instruction", "当前指令", "用户这一轮明确说的话"),
    ("persona", "用户自定义人格", "用户自己写的人格文本，原文照发"),
    ("habits", "已确认的习惯", "用户在人格面板里明确设置的八项"),
    ("observed", "推断的偏好", "桌宠从对话里观察到的，可能不准，会标出来"),
    ("default", "默认行为", "名字、性格、行为准则、语气、说话示例"),
)

LABELS = {key: label for key, label, _note in ORDER}
NOTES = {key: note for key, _label, note in ORDER}


def ladder():
    """界面用的一行行说明，按优先级从高到低。"""
    return [{"key": key, "label": label, "note": note} for key, label, note in ORDER]


def describe():
    """一句话说清顺序。给界面和文档用。"""
    return "优先级：" + " > ".join(label for _key, label, _note in ORDER)


def rank(key):
    """数值越小优先级越高。不认识的层排最后。"""
    for index, (name, _label, _note) in enumerate(ORDER):
        if name == key:
            return index
    return len(ORDER)
