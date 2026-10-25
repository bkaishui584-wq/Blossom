"""上下文装配。只发送必要内容，并按 token 预算裁剪。"""

from dataclasses import dataclass, field

# ponytail: 粗略估算，约 2 字符/token。要精确统计时只改这一个常量。
CHARS_PER_TOKEN = 2.0


def estimate_tokens(text):
    return int(len(str(text or "")) / CHARS_PER_TOKEN) + 1


@dataclass
class ContextBundle:
    messages: list
    stats: dict = field(default_factory=dict)


class ContextManager:
    """不保存状态，只负责把已有材料装进预算内。"""

    # 记忆块最多占预算的这个比例，剩下的留给最近对话和本轮输入。
    MEMORY_BUDGET_RATIO = 0.4

    def __init__(self, token_budget=2000, recent_window=24):
        self.token_budget = token_budget
        self.recent_window = recent_window

    def build(
        self,
        system_prompt,
        recent_messages,
        memory_block="",
        summary="",
        persona_note="",
        current_input="",
        extra_block="",
    ):
        messages = [{"role": "system", "content": system_prompt}]
        used = estimate_tokens(system_prompt)

        if persona_note:
            messages.append({"role": "system", "content": persona_note})
            used += estimate_tokens(persona_note)

        # 本轮用户输入必须发出，先为它预留额度。
        reserved = estimate_tokens(current_input)

        recalled = self._recall_block(memory_block, summary, extra_block)
        recalled = self._clip_block(recalled, self.token_budget - reserved)
        if recalled:
            messages.append({"role": "system", "content": recalled})
            used += estimate_tokens(recalled)

        limit = self.token_budget - reserved
        kept = []
        for message in reversed(list(recent_messages)[-self.recent_window :]):
            cost = estimate_tokens(message.get("content", ""))
            if kept and used + cost > limit:
                break
            kept.append({"role": message["role"], "content": message["content"]})
            used += cost
        kept.reverse()
        messages.extend(kept)

        if current_input:
            messages.append({"role": "user", "content": current_input})
            used += reserved

        stats = {
            "estimated_tokens": used,
            "budget": self.token_budget,
            "recent_sent": len(kept),
            "recent_available": len(recent_messages),
            "memory_chars": len(recalled),
            "has_summary": bool(summary),
        }
        return ContextBundle(messages=messages, stats=stats)

    @staticmethod
    def _clip_block(block, budget_tokens, ratio=None):
        """记忆再相关也不能吃掉整个预算，超了就截断并说明。"""
        if not block:
            return ""
        share = ContextManager.MEMORY_BUDGET_RATIO if ratio is None else ratio
        cap = max(64, int(max(0, budget_tokens) * share * CHARS_PER_TOKEN))
        if len(block) <= cap:
            return block
        return block[:cap] + "\n…（记忆过长，已截断）"

    @staticmethod
    def _recall_block(memory_block, summary, extra_block=""):
        parts = []
        if memory_block:
            parts.append("已知记忆：\n" + memory_block)
        if summary:
            parts.append("更早对话摘要：\n" + summary)
        if extra_block:
            parts.append("本轮已查到的信息：\n" + extra_block)
        return "\n\n".join(parts)
