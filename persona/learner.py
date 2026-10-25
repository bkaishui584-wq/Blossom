"""习惯学习：从用户明确说的话里挑候选偏好，带依据和置信度。

只认用户亲口说的偏好，不从行为里推断性格、健康、经济这类敏感属性。
一次提到只算候选，进不了提示词；同一件事说过三次以上、说法一致、
而且属于低影响字段，才会自动采纳，并且始终标注「观察到的」。
称呼和不喜欢的行为一律等用户点，不自动采纳。

拒绝过的建议不再出现，也不再累加。任何一步都改不了权限，
也只能写 persona. 开头的偏好键。删掉记录就彻底不生效。
"""

import json
import time

from persona import habits as habits_kit

KEY = "persona.habits.candidates"
MAX_CANDIDATES = 40
MAX_EVIDENCE = 5
MAX_EVIDENCE_CHARS = 60
MAX_VALUE_CHARS = 40
MAX_TEXT_CHARS = 400

# 点了「稍后」以后，这条建议先躲多久再回来问你。
SNOOZE_SECONDS = 12 * 3600

# 说到第三次、说法一致、置信度够了，低影响字段才自动采纳。
AUTO_MIN_SEEN = 3
AUTO_CONFIDENCE = 0.7

# 影响说话方式的字段：观察够了可以自动采纳。
AUTO_FIELDS = ("length", "depth", "tone", "remind", "proactive", "collab")
# 称呼和雷区一律等用户确认。
ASK_FIELDS = ("address", "avoid")

# 带这些字眼的句子更像是在立规矩，而不是随口一说。
DIRECTIVE = ("以后", "之后", "下次", "每次", "别再", "不要", "不用", "别", "麻烦", "记得", "请")

# 带这些字眼的是"就这一次"，不是长期习惯。哪怕说上十遍也不该自动采纳。
TEMP_WORDS = ("这次", "这一次", "这一回", "这回", "暂时", "先这样", "今天先", "就现在")


def _is_temporary(text):
    return any(word in str(text or "") for word in TEMP_WORDS)

# 规则：字段、建议值、触发词。只看这八个字段，不碰别的。
RULES = (
    ("length", "简短", ("简短", "短一点", "说重点", "别啰嗦", "简洁", "别写那么长", "别太长")),
    ("length", "详细", ("详细", "展开讲", "多讲点", "说细一点")),
    ("depth", "只给结论", ("只给结论", "直接给结果", "别解释")),
    ("depth", "说清理由", ("说理由", "说下原因", "讲下原因")),
    ("depth", "从头讲起", ("从头讲", "从基础讲")),
    ("tone", "随意", ("随意", "别太正式", "不用那么正式", "自然一点")),
    ("tone", "专业", ("专业一点", "正式一点")),
    ("tone", "轻松幽默", ("幽默一点", "逗一点", "轻松一点")),
    ("tone", "温和耐心", ("温柔一点", "耐心一点", "别凶")),
    ("collab", "先问清再做", ("先问我", "先问我一下", "先确认一下")),
    ("collab", "直接给方案", ("直接给方案", "别问了直接做")),
    ("collab", "边做边报进度", ("报进度", "同步进度", "随时说进展")),
    ("proactive", "不主动", ("别主动", "不用主动", "别打扰", "少搭话", "安静点")),
    ("proactive", "偶尔主动", ("偶尔主动", "偶尔说一句")),
    ("proactive", "经常主动", ("多主动", "主动一点", "多和我说话")),
    ("remind", "很少", ("少提醒", "别老提醒", "不用提醒")),
    ("remind", "多一点", ("多提醒", "提醒我几次", "多催催")),
)

# 不喜欢的行为：抓「不要 / 别 + 具体东西」后面那一小段。
_AVOID_HEADS = ("不要再", "不要", "别再", "别", "不用再", "不用")
_AVOID_STOP = "，。！？；、,.!?;:： \u3000\n\t"


def _clean(text, limit):
    body = " ".join(str(text or "").split())
    return body[:limit]


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _read(preferences):
    if preferences is None:
        return []
    try:
        raw = preferences.get_preference(KEY)
    except Exception:
        return []
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict) and row.get("field") in habits_kit.FIELDS]


def _write(preferences, rows):
    if preferences is None:
        return False
    try:
        preferences.set_preference(KEY, json.dumps(rows, ensure_ascii=False))
        return True
    except Exception:
        return False


def _identifier(field, value):
    return "%s|%s" % (field, value)


def confidence(seen, directive=False, conflict=False):
    """一次听到只算弱证据；说过的次数越多越可信。"""
    score = 0.45 if directive else 0.3
    score += min(0.4, 0.15 * (max(1, int(seen)) - 1))
    if conflict:
        score -= 0.15
    return round(max(0.05, min(0.95, score)), 2)


def _avoid_value(text):
    """从否定句里取出用户不喜欢的那件事。取不到就不猜。"""
    for head in _AVOID_HEADS:
        if head not in text:
            continue
        tail = text.split(head, 1)[1]
        body = ""
        for char in tail:
            if char in _AVOID_STOP:
                break
            body += char
        body = body.strip()
        if 1 <= len(body) <= 12:
            return body
    return ""


def _address_value(text):
    """「叫我阿墨」这类称呼。取不到就不猜。"""
    for head in ("叫我", "称呼我", "管我叫"):
        if head not in text:
            continue
        tail = text.split(head, 1)[1]
        body = ""
        for char in tail:
            if char in _AVOID_STOP:
                break
            body += char
        body = body.strip()
        if 1 <= len(body) <= 8:
            return body
    return ""


def propose(text):
    """从一句话里挑建议。返回 [(字段, 建议值, 依据片段)]，挑不到就是空。"""
    body = _clean(text, MAX_TEXT_CHARS)
    if not body:
        return []
    found = []
    seen_fields = set()
    for field, value, words in RULES:
        if field in seen_fields:
            continue
        if any(word in body for word in words):
            found.append((field, value, body))
            seen_fields.add(field)
    if "address" not in seen_fields:
        name = _address_value(body)
        if name:
            found.append(("address", name, body))
            seen_fields.add("address")
    if "avoid" not in seen_fields:
        avoid = _avoid_value(body)
        if avoid:
            found.append(("avoid", avoid, body))
    return found

class HabitLearner:
    """候选习惯的存取与推送。出任何问题都不打断对话，也不改权限。"""

    def __init__(self, preferences=None, persona=None, logger=None, clock=_now):
        self.preferences = preferences
        self.persona = persona
        self.logger = logger
        self.clock = clock

    # ---------- 开关 ----------
    def enabled(self):
        return habits_kit.learning_enabled(self.preferences)

    def set_enabled(self, flag):
        ok = habits_kit.set_learning_enabled(self.preferences, bool(flag))
        # 开关都要马上同步：关掉把观察到的内容收回来，重开再推回去。
        self.apply()
        return ok

    # ---------- 观察 ----------
    def observe(self, text):
        """记一次用户说的话，返回这次动过的记录。学习关着就什么都不做。"""
        if not self.enabled():
            return []
        proposals = propose(text)
        if not proposals:
            return []
        rows = _read(self.preferences)
        touched = []
        for field, value, snippet in proposals:
            row = self._find(rows, field, value)
            if row is not None and row.get("status") == "rejected":
                # 拒绝过的建议不再累加，也不再出现。
                continue
            if row is None:
                row = self._new_row(field, value, snippet, text)
                rows.append(row)
            else:
                self._touch(row, snippet, text)
            # 这条是"就这一次"还是"以后都这样"，按这一次说的话重新定。
            # 之后哪天不带"这次"再说一遍，它才转成长期候选。
            row["temporary"] = _is_temporary(text)
            touched.append(row)
        if not touched:
            return []
        if not _write(self.preferences, rows[-MAX_CANDIDATES:]):
            # 存不下去就别装作记下了：宁可什么都不报，也不要给假成功。
            return []
        self.apply()
        return touched

    def _new_row(self, field, value, snippet, text):
        directive = any(word in text for word in DIRECTIVE)
        return {
            "id": _identifier(field, value),
            "field": field,
            "value": value,
            "seen": 1,
            "confidence": confidence(1, directive),
            "directive": bool(directive),
            "status": "candidate",
            "auto": False,
            "first": self.clock(),
            "updated": self.clock(),
            "evidence": [{"t": self.clock(), "text": _clean(snippet, MAX_EVIDENCE_CHARS)}],
        }

    def _touch(self, row, snippet, text):
        row["seen"] = int(row.get("seen") or 0) + 1
        # 用户又提了一次，就别再按「稍后」压着了。
        row.pop("snoozed", None)
        row["updated"] = self.clock()
        if any(word in text for word in DIRECTIVE):
            row["directive"] = True
        evidence = list(row.get("evidence") or [])
        piece = {"t": self.clock(), "text": _clean(snippet, MAX_EVIDENCE_CHARS)}
        if piece["text"] and all(item.get("text") != piece["text"] for item in evidence):
            evidence.append(piece)
        row["evidence"] = evidence[-MAX_EVIDENCE:]
        row["confidence"] = confidence(
            row["seen"], row.get("directive"), self._has_conflict(row)
        )
        if (
            row.get("status") == "candidate"
            and row.get("field") in AUTO_FIELDS
            # "就这一次"永远不自动采纳：它不是习惯。
            and not row.get("temporary")
            and row["seen"] >= AUTO_MIN_SEEN
            and row["confidence"] >= AUTO_CONFIDENCE
        ):
            row["status"] = "accepted"
            row["auto"] = True

    def _find(self, rows, field, value):
        wanted = _identifier(field, value)
        for row in rows:
            if row.get("id") == wanted:
                return row
        return None

    def _has_conflict(self, row):
        for other in _read(self.preferences):
            if other.get("field") != row.get("field"):
                continue
            if other.get("id") == row.get("id"):
                continue
            if other.get("status") == "rejected":
                continue
            return True
        return False

    # ---------- 读取 ----------
    def rows(self):
        """按字段顺序排好的全部记录。"""
        rows = _read(self.preferences)
        order = {name: index for index, name in enumerate(habits_kit.FIELDS)}
        rows.sort(key=lambda row: (order.get(row.get("field"), 99), -float(row.get("confidence") or 0)))
        return rows

    def observed(self):
        """已采纳的记录。同一字段有多个值时只取置信度最高的那个。"""
        best = {}
        for row in self.rows():
            if row.get("status") != "accepted":
                continue
            field = row.get("field")
            current = best.get(field)
            if current is None or float(row.get("confidence") or 0) > float(
                current.get("confidence") or 0
            ):
                best[field] = row
        return [best[name] for name in habits_kit.FIELDS if name in best]

    def records(self):
        """推给人格对象的形状：[{field, value}]。"""
        return [{"field": row.get("field"), "value": row.get("value")} for row in self.observed()]

    def conflicts(self):
        """同一字段出现多个不同建议：先摆出来，等用户自己选。"""
        groups = {}
        for row in self.rows():
            if row.get("status") == "rejected":
                continue
            groups.setdefault(row.get("field"), []).append(row)
        return {field: rows for field, rows in groups.items() if len(rows) > 1}

    def evidence(self, identifier):
        for row in _read(self.preferences):
            if row.get("id") == str(identifier or ""):
                return list(row.get("evidence") or [])
        return []

    def summary(self):
        counts = {"candidate": 0, "accepted": 0, "rejected": 0}
        for row in self.rows():
            key = row.get("status") or "candidate"
            counts[key] = counts.get(key, 0) + 1
        return {
            "learning": self.enabled(),
            "total": sum(counts.values()),
            "candidate": counts.get("candidate", 0),
            "accepted": counts.get("accepted", 0),
            "rejected": counts.get("rejected", 0),
            "applied": len(self.records()),
        }

    def key(self, identifier, name, default=""):
        """界面读某条记录的一个字段。读不到就返回默认值。"""
        for row in _read(self.preferences):
            if row.get("id") == str(identifier or ""):
                return row.get(name, default)
        return default

    # ---------- 改动 ----------
    def accept(self, identifier):
        return self._set_status(identifier, "accepted")

    def reject(self, identifier):
        return self._set_status(identifier, "rejected")

    def undo(self, identifier):
        """撤销：已采纳的退回候选，等用户重新拿主意。权威记录不变。"""
        return self._set_status(identifier, "candidate")

    def snooze(self, identifier, seconds=SNOOZE_SECONDS):
        """稍后再说：这段时间里不再排在待办前面，记录本身留着。"""
        try:
            span = float(seconds)
        except (TypeError, ValueError):
            span = float(SNOOZE_SECONDS)
        until = self._stamp_seconds() + span
        rows = _read(self.preferences)
        for row in rows:
            if row.get("id") != str(identifier or ""):
                continue
            row["snoozed"] = until
            row["updated"] = self.clock()
            _write(self.preferences, rows)
            return True
        return False

    def snoozed_until(self, identifier):
        """这条被推到什么时候。没推过就是 0。"""
        for row in _read(self.preferences):
            if row.get("id") == str(identifier or ""):
                return float(row.get("snoozed") or 0)
        return 0.0

    def visible_rows(self):
        """待你拿主意的记录：推后过的先躲开，到点自己回来。"""
        now = self._stamp_seconds()
        return [row for row in self.rows() if float(row.get("snoozed") or 0) <= now]

    def _stamp_seconds(self):
        """时钟可能是时间戳，也可能是格式化过的字符串；要算秒数就走这儿。"""
        try:
            return float(self.clock())
        except (TypeError, ValueError):
            return time.time()

    def scope(self, identifier):
        """这条建议管多大范围，按真实规则说，不猜。"""
        if self.key(identifier, "temporary", False):
            return "仅限当前这一次"
        field = self.key(identifier, "field", "")
        if field in ASK_FIELDS:
            return "称呼与雷区：只在你点头后生效"
        if field in AUTO_FIELDS:
            return "说话方式：观察够了会自己采纳"
        return "只记下来，不会自动生效"

    def _set_status(self, identifier, status):
        rows = _read(self.preferences)
        for row in rows:
            if row.get("id") != str(identifier or ""):
                continue
            row["status"] = status
            row["auto"] = False
            row["updated"] = self.clock()
            _write(self.preferences, rows)
            self.apply()
            return True
        return False

    def remove(self, identifier):
        rows = _read(self.preferences)
        left = [row for row in rows if row.get("id") != str(identifier or "")]
        if len(left) == len(rows):
            return False
        _write(self.preferences, left)
        self.apply()
        return True

    def edit(self, identifier, value):
        """改建议内容。改完退回候选，等用户自己再点接受。"""
        cleaned = _clean(value, MAX_VALUE_CHARS)
        if not cleaned:
            return False
        rows = _read(self.preferences)
        for row in rows:
            if row.get("id") != str(identifier or ""):
                continue
            row["value"] = cleaned
            row["id"] = _identifier(row.get("field"), cleaned)
            row["status"] = "candidate"
            row["auto"] = False
            row["updated"] = self.clock()
            _write(self.preferences, rows)
            self.apply()
            return True
        return False

    def clear(self):
        """清掉全部候选记录。显式设置的习惯不动。"""
        _write(self.preferences, [])
        self.apply()
        return True

    # ---------- 推送 ----------
    def apply(self):
        """把已采纳的建议推给当前人格对象。学习关着就一条都不推。"""
        target = self._holder()
        if target is None:
            return {"ok": False, "error": "当前人格不支持观察记录"}
        records = self.records() if self.enabled() else []
        try:
            target(records)
        except Exception as exc:
            if self.logger:
                self.logger.warning("habit learner apply failed: %s", exc)
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "count": len(records)}

    def _holder(self):
        node = self.persona
        try:
            if callable(node):
                node = node()
        except Exception:
            return None
        seen = set()
        while node is not None and id(node) not in seen:
            setter = getattr(node, "set_observed", None)
            if setter is not None:
                return setter
            seen.add(id(node))
            node = getattr(node, "default", None)
        return None


def rules():
    """界面用的规则说明。改不了它，只是摆出来给人看。"""
    return {
        "auto_fields": AUTO_FIELDS,
        "ask_fields": ASK_FIELDS,
        "min_seen": AUTO_MIN_SEEN,
        "confidence": AUTO_CONFIDENCE,
        "labels": {name: habits_kit.LABELS[name] for name in habits_kit.FIELDS},
    }


STATUS_LABELS = {
    "candidate": "候选",
    "accepted": "已采纳",
    "rejected": "已拒绝",
}


def status_label(row):
    status = str((row or {}).get("status") or "candidate")
    label = STATUS_LABELS.get(status, status)
    if status == "accepted" and (row or {}).get("auto"):
        return label + "（自动）"
    return label
