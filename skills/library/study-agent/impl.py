"""Study Agent：分层提示与讲解结构，错题记进记忆。

没有材料也没有模型时，只给方法和结构，不编造知识内容。
"""

MODES = {
    "hint": "只给一层提示，先让用户自己走一步",
    "step": "给分步推导，每一步说清依据",
    "full": "给完整解答，并标出关键转折点",
}

HINT_LADDER = (
    "第一层：这道题问的是什么？把已知和未知各写一行。",
    "第二层：有没有类似的题目？把上次的解法拿过来对照。",
    "第三层：卡住的那一步，需要哪条定义或公式？",
)


def run(ctx):
    topic = str(ctx.inputs.get("topic") or ctx.request or "").strip()
    mode = str(ctx.inputs.get("mode") or "hint").strip().lower()
    mode = mode if mode in MODES else "hint"
    if not topic:
        return {"ok": False, "error": "要讲什么？给我一个题目、一段代码或者一个概念。"}

    blocks = ["主题：%s" % topic, "模式：%s（%s）" % (mode, MODES[mode])]
    blocks.append("\n提示阶梯：")
    blocks.extend(" " + line for line in HINT_LADDER)
    if mode in ("step", "full"):
        blocks.append(
            "\n推导结构：1) 复述条件 2) 写出目标 3) 找可用定义或公式 "
            "4) 逐步骤推导 5) 回代检查"
        )
    if mode == "full":
        blocks.append(
            "\n说明：完整内容需要模型或你提供的材料。现在没有材料，"
            "我只能给出结构，不会凭空写出推导过程。"
        )

    recorded = False
    mistake = str(ctx.inputs.get("mistake") or "").strip()
    if mistake and ctx.memory is not None:
        try:
            ctx.memory.remember("错题：%s" % mistake, kind="project", importance=3)
            recorded = True
        except Exception:
            recorded = False
    if recorded:
        blocks.append("\n已把这道错题记下来，可以在记忆界面查看或删除。")

    summary = "按「%s」模式给了学习路径。" % mode
    if recorded:
        summary += "错题也记下了。"
    return {"ok": True, "summary": summary, "detail": "\n".join(blocks), "verified": True}
