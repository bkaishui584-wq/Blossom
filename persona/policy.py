"""Deterministic persona policy for local behavior.

Personality text shapes what the model says. This module shapes what the
program does before a model is called: whether a proactive message may appear,
how long to wait, and which short context line belongs in the prompt.
"""

from persona import habits as habits_kit

MODE_KEY = "persona.context_mode"
DEFAULT_MODE = "companion"
MODES = ("companion", "work", "rest", "dnd")

LABELS = {
    "companion": "陪伴",
    "work": "工作",
    "rest": "休息",
    "dnd": "勿扰",
}

DESCRIPTIONS = {
    "companion": "正常聊天和主动陪伴。",
    "work": "普通主动搭话先等一等，高优先级提醒仍可出现。",
    "rest": "只保留高优先级提醒，普通主动搭话先等一等。",
    "dnd": "停止普通主动搭话和低优先级提醒。",
}

PROMPT_LINES = {
    "companion": "当前是陪伴情境。自然交流，别频繁打扰。",
    "work": "当前是工作情境。用户在忙，回答直接，非必要不主动搭话。",
    "rest": "当前是休息情境。语气放松，非必要不主动搭话。",
    "dnd": "当前是勿扰情境。用户主动说话时照常回答，但不要主动开口。",
}

_PRIORITY = {"low": 0, "normal": 1, "high": 2}


def normalize_mode(value):
    """Return a known mode, or the companion default for garbage."""
    text = str(value or "").strip().lower()
    return text if text in MODES else DEFAULT_MODE


def get_mode(preferences):
    if preferences is None:
        return DEFAULT_MODE
    try:
        return normalize_mode(preferences.get_preference(MODE_KEY))
    except Exception:
        return DEFAULT_MODE


def set_mode(preferences, value):
    if preferences is None:
        return False
    mode = normalize_mode(value)
    try:
        preferences.set_preference(MODE_KEY, mode)
    except Exception:
        return False
    return True


class PersonaPolicy:
    """Small local policy shared by chat, proactive rules and reminders."""

    def __init__(self, preferences=None):
        self.preferences = preferences

    def mode(self):
        return get_mode(self.preferences)

    def set_mode(self, value):
        return set_mode(self.preferences, value)

    def habits(self):
        return habits_kit.load(self.preferences)

    def decision(self, priority="normal", urgent=False):
        """Return interact / wait / silent with a human-readable reason."""
        rank = _PRIORITY.get(str(priority or "normal").lower(), 1)
        high = bool(urgent) or rank >= _PRIORITY["high"]
        mode = self.mode()
        habits = self.habits()

        if mode == "dnd":
            return {
                "verdict": "silent",
                "reason": "当前是勿扰情境",
                "mode": mode,
            }
        if mode == "rest":
            if high:
                return {
                    "verdict": "interact",
                    "reason": "高优先级提醒可穿过休息情境",
                    "mode": mode,
                }
            return {
                "verdict": "wait" if rank >= _PRIORITY["normal"] else "silent",
                "reason": "当前是休息情境",
                "mode": mode,
            }
        if mode == "work" and not high:
            return {
                "verdict": "wait" if rank >= _PRIORITY["normal"] else "silent",
                "reason": "当前是工作情境",
                "mode": mode,
            }
        if habits.get("proactive") == "不主动" and not high:
            return {
                "verdict": "silent",
                "reason": "习惯设置为不主动",
                "mode": mode,
            }
        if habits.get("remind") == "很少" and not high:
            return {
                "verdict": "silent",
                "reason": "习惯设置为少提醒",
                "mode": mode,
            }
        return {"verdict": "interact", "reason": "", "mode": mode}

    def cooldown_factor(self):
        """Scale proactive cooldowns from explicit habits."""
        habits = self.habits()
        proactive = habits.get("proactive")
        if proactive == "经常主动":
            return 0.5
        if proactive == "偶尔主动":
            return 2.0
        if proactive == "不主动":
            return 4.0
        if habits.get("remind") == "多一点":
            return 0.75
        if habits.get("remind") == "很少":
            return 2.0
        return 1.0

    def prompt_lines(self):
        line = PROMPT_LINES.get(self.mode())
        return [line] if line else []

    def reminder_text(self, text):
        """Add the user's explicit address to local reminder text, if set."""
        body = str(text or "").strip()
        address = str(self.habits().get("address") or "").strip()
        if not body or not address or body.startswith(address):
            return body
        return "%s，%s" % (address, body)

    def summary(self):
        mode = self.mode()
        return {
            "mode": mode,
            "label": LABELS.get(mode, mode),
            "description": DESCRIPTIONS.get(mode, ""),
            "cooldown_factor": self.cooldown_factor(),
            "habits": self.habits(),
        }
