"""记忆策略层：最近窗口、旧对话压缩、按相关性检索。"""

import re

_WORD_SPLIT = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")

# 记忆来源：用户亲口说的最可信，模型推断的最不可信。
SOURCES = ("USER_EXPLICIT", "SYSTEM_OBSERVED", "MODEL_INFERRED")
_SOURCE_TRUST = {"USER_EXPLICIT": 3, "SYSTEM_OBSERVED": 2, "MODEL_INFERRED": 1}
# 每条记忆都带一个可信度。程序自己观察到的事实不该被当成没根据的猜测。
_SOURCE_CONFIDENCE = {
    "USER_EXPLICIT": 1.0,
    "SYSTEM_OBSERVED": 0.8,
    "PROGRAM_OBSERVED": 0.8,
    "MODEL_INFERRED": 0.5,
}

# 可检索的记忆层：长期 / 项目 / 关系 / 对桌宠的反馈。
KINDS = ("long_term", "project", "relationship", "feedback")
_KIND_PREFIX = {"relationship": "和用户的关系：", "feedback": "用户对我说话方式的反馈："}

# 说法重合到这个比例就算同一条，新信息取代旧的。
_CONFLICT_RATIO = 0.5
# 普通记忆半衰期 30 天；重要记忆半衰期 10 年，约等于不衰减。
_NORMAL_HALF_LIFE_DAYS = 30.0
_IMPORTANT_HALF_LIFE_DAYS = 3650.0


def _terms(text):
    """抽关键词。英文按词，中文按二元组（无分词依赖）。"""
    text = str(text or "").lower()
    terms = {word for word in _WORD_SPLIT.split(text) if len(word) >= 2}
    for run in _CJK_RUN.findall(text):
        terms.update(run[i : i + 2] for i in range(len(run) - 1))
    return terms


# 提示待办或未解决的字眼，只用来分栏，不猜内容。
_TODO_MARKERS = ("帮我", "请", "记得", "待办", "提醒", "别忘", "安排", "要做的", "下次")
_QUESTION_MARKS = ("?", "？")

# 来源不同，进上下文时标不同的话：未确认的推断不能装成用户事实。
_SOURCE_MARKS = {
    "USER_EXPLICIT": "",
    "SYSTEM_OBSERVED": "（观察到）",
    "MODEL_INFERRED": "（推测）",
}

# 程序内部开关，不进模型：位置、隐私、静默、习惯推断、人格设置。
_INTERNAL_PREF_KEYS = (
    "pet_position",
    "proactive_enabled",
    "quiet_mode",
    "quiet_hours",
)
_INTERNAL_PREF_PREFIXES = (
    "privacy.",
    "habit.",
    "persona.",
    "devices.",
    "proactive.",
    "voice.",
    "shell.",
    "live2d.",
    "project.",
    "work.",
)


def _visible_preference(key):
    """用户偏好也要挑着发：内部开关不进上下文。"""
    name = str(key or "")
    if name in _INTERNAL_PREF_KEYS:
        return False
    return not name.startswith(_INTERNAL_PREF_PREFIXES)


def _clip(text, limit):
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: max(1, limit - 1)] + "…"


def _extract_summary(messages, max_chars, previous=""):
    """本地摘要：不调用模型，只从真实消息里提炼要点、待办和未解决的问题。

    不靠截断聊天文本充数：先分要点与待办两栏，提炼不到就不写，
    宁可空着也不编。模型可用时这条也能当兜底。
    """
    points, todos = [], []
    for message in messages or ():
        text = " ".join(str(message.get("content", "")).split())
        if not text:
            continue
        speaker = "用户" if message.get("role") == "user" else "桌宠"
        if message.get("role") == "user" and (
            any(word in text for word in _TODO_MARKERS)
            or any(ch in text for ch in _QUESTION_MARKS)
        ):
            todos.append(_clip(text, 50))
            continue
        points.append(f"{speaker}: {_clip(text, 60)}")

    sections = []
    if previous:
        sections.append("【早前】" + str(previous).strip())
    if points:
        sections.append("【要点】" + " ".join(points))
    if todos:
        sections.append("【待办与未解决】" + "；".join(todos))
    if not sections:
        return ""
    combined = "\n".join(sections)
    return combined if len(combined) <= max_chars else combined[-max_chars:]


from memory.diary import DiaryService
from memory.habit import HabitModel
from memory.project import ProjectMemory
from memory.fts import MemoryFts
from memory.relationship import RelationshipState
from memory.timeline import Timeline


class MemoryManager:
    """程序端的记忆规则。是否需要打扰、是否记住，都由这里判断。"""

    def __init__(
        self,
        repository,
        recent_window=24,
        summary_trigger=24,
        memory_limit=12,
        summary_max_chars=600,
        memory_capacity=2000,
        pet_id=None,
        logger=None,
    ):
        # 当前是哪只宠物在看这些记忆。None 表示不区分（老调用方照旧）。
        self.pet_id = str(pet_id or "").strip() or None
        self.repository = repository
        # 近期窗口：进上下文的候选消息条数，按真实 token 预算再裁。
        self.recent_window = recent_window
        self.summary_trigger = summary_trigger
        # 每轮检索条数，和总存储量分开：检索条数小，存储容量大。
        self.memory_limit = memory_limit
        self.summary_max_chars = summary_max_chars
        # 存储容量：超出的旧记忆只归档不删除，仍可查看和恢复。
        self.memory_capacity = memory_capacity
        self.logger = logger
        self.last_recall = {}
        # 关系状态：只记天数与互动次数，不记聊天内容。
        self.relationship = RelationshipState(getattr(repository, "db", None))
        # 日记：每天一条，程序自己总结，不调用模型。
        self.diary = DiaryService(
            getattr(repository, "db", None), relationship=self.relationship
        )
        # 真实时间线：只记确实发生过的第一次，读不到就什么都不记。
        self.timeline = Timeline(getattr(repository, "db", None))
        # 习惯模型：从真实聊天时间推断活跃时段，只当推测。
        self.habit = HabitModel(getattr(repository, "db", None), preferences=self)
        # 项目记忆：把「正在做的事」记成结构化状态，和长期记忆分开存。
        self.projects = ProjectMemory(repository, logger=logger)
        # 全文索引：FTS5 能用就建；用不了照旧按关键词打分，功能不受影响。
        self.fts = MemoryFts(getattr(repository, "db", None), logger=logger)

    def _warn(self, message, *args):
        if self.logger:
            try:
                self.logger.warning(message, *args)
            except Exception:
                pass

    # ---------- 归属 ----------
    def use_pet(self, pet_id):
        """换宠物就换记忆归属。别只宠物的私有记忆读不到，公共记忆照样读。"""
        self.pet_id = str(pet_id or "").strip() or None
        return self.pet_id

    def owner(self):
        return self.pet_id

    # ---------- 写入 ----------
    def record(self, session_id, role, content):
        message_id = self.repository.add_message(session_id, role, content)
        self._compress_if_needed(session_id)
        return message_id

    def _compress_if_needed(self, session_id):
        if self.repository.count_unsummarized(session_id) <= self.summary_trigger:
            return None
        window = self.recent_window + self.summary_trigger
        keep = {row["id"] for row in self.repository.recent_messages(session_id, self.recent_window)}
        old = [
            row
            for row in self.repository.recent_messages(session_id, window, unsummarized_only=True)
            if row["id"] not in keep
        ]
        if not old:
            return None
        previous = self.repository.latest_summary(session_id)
        summary = _extract_summary(
            old, self.summary_max_chars, (previous or {}).get("content", "")
        )
        self.repository.add_summary(session_id, summary, old[-1]["id"])
        self.repository.mark_summarized([row["id"] for row in old])
        return summary

    def remember(
        self, content, kind="long_term", importance=1, source="USER_EXPLICIT", confirmed=None
    ):
        """写入长期记忆。一模一样的去重，新明确信息取代同类的旧说法。"""
        text = str(content or "").strip()
        if not text:
            return None
        kind = str(kind or "long_term").strip() or "long_term"
        if kind not in KINDS:
            # 搜不到的记忆等于没记。收到没登记的层就归到长期记忆，并留一条日志。
            self._warn("unknown memory kind %r, stored as long_term", kind)
            kind = "long_term"
        existing = self.repository.find_active(kind, text, pet_id=self.pet_id)
        if existing is not None:
            # 已经有一条一样的：只更新时间戳，不新增，也不覆盖。
            self.repository.touch_memory([existing["id"]])
            return existing["id"]
        terms = _terms(text)
        keywords = ",".join(sorted(terms)[:40])
        confidence = _SOURCE_CONFIDENCE.get(source, 0.5)
        superseded = self._find_conflicts(kind, terms, source)
        memory_id = self.repository.add_memory(
            text,
            kind,
            keywords,
            importance,
            source,
            confidence,
            confirmed=confirmed,
            pet_id=self.pet_id or "",
        )
        if superseded:
            self.repository.supersede(superseded, memory_id)
        self._enforce_capacity()
        return memory_id

    def _find_conflicts(self, kind, terms, source):
        """同类里说法重合的旧记忆，交给更新、更可信的一条取代。"""
        rank = _SOURCE_TRUST.get(source, 1)
        conflicts = []
        for row in self.repository.candidate_memories(
            kinds=(kind,), limit=50, pet_id=self.pet_id
        ):
            if _SOURCE_TRUST.get(row["source"], 1) > rank:
                continue
            old = set(filter(None, row["keywords"].split(",")))
            overlap = terms & old
            span = min(len(terms), len(old))
            if len(overlap) >= 2 and span and len(overlap) / span >= _CONFLICT_RATIO:
                conflicts.append(row["id"])
        return conflicts

    def _enforce_capacity(self):
        """超出容量就归档最旧最不重要的，不删除；锁定和重要的留下。"""
        try:
            return self.repository.archive_overflow(self.memory_capacity)
        except Exception as exc:
            self._warn("memory capacity check failed: %s", exc)
            return 0

    def set_preference(self, key, value):
        return self.repository.set_preference(key, value)

    def get_preference(self, key):
        return self.repository.get_preference(key)

    # ---------- 读取 ----------
    def search(self, query, limit=None, kinds=None):
        limit = self.memory_limit if limit is None else limit
        if limit <= 0:
            return []
        terms = _terms(query)
        if not terms:
            return []
        rows, from_index = self._search_rows(terms, kinds, limit)
        scored = []
        # ponytail: 最多扫描 200 条，按相关度 + 重要度 + 可信度 + 新鲜度打分。
        for row in rows:
            hits = len(terms & set(filter(None, row["keywords"].split(","))))
            if not hits and not from_index:
                # 全表扫描时只有关键字命中才算；索引已经按正文筛过一轮了。
                continue
            scored.append((self._score(row, hits or 1), row))
        scored.sort(key=lambda item: item[0], reverse=True)
        picked = [row for _, row in scored[:limit]]
        if picked:
            self.repository.touch_memory([row["id"] for row in picked])
        return picked

    def _search_rows(self, terms, kinds, limit):
        """先用全文索引缩小范围，索引用不了就退回扫候选集。返回 (行, 是否来自索引)。"""
        pool = max(50, int(limit or 12) * 8)
        ids = None
        try:
            ids = self.fts.search_ids(
                terms, kinds=kinds or KINDS, pet_id=self.pet_id, limit=pool
            )
        except Exception as exc:
            self._warn("memory fts search failed: %s", exc)
            ids = None
        if ids:
            rows = self.repository.memories_by_ids(ids, pet_id=self.pet_id)
            if rows:
                return rows, True
        return self.repository.candidate_memories(kinds or KINDS, pet_id=self.pet_id), False
    @staticmethod
    def _score(row, hits):
        """重要记忆长期保存，普通记忆随时间衰减；不确定的排后面。"""
        importance = int(row["importance"] or 1)
        half_life = _IMPORTANT_HALF_LIFE_DAYS if importance >= 2 else _NORMAL_HALF_LIFE_DAYS
        freshness = 0.5 ** (float(row["age_days"] or 0.0) / half_life)
        confidence = float(row["confidence"] if row["confidence"] is not None else 1.0)
        # 未确认的推断降一档，别挤掉用户亲口说的事实。
        trust = 1.0 if row.get("confirmed", 1) else 0.6
        return trust * (
            hits
            + 0.5 * confidence
            + 0.3 * importance * (0.5 + 0.5 * freshness)
            + 0.3 * freshness
        )

    def recent_memories(self, limit=5):
        """最近记住的内容，给「你记住了什么」这类本地能力用。"""
        return self.repository.candidate_memories(
            kinds=KINDS, limit=max(1, int(limit)), pet_id=self.pet_id
        )

    def feedback_memories(self, limit=10):
        """用户对桌宠表达方式的反馈，给人格层整理用。"""
        return self.repository.memories_by_kind("feedback", limit, pet_id=self.pet_id)

    # ---------- 记忆管理（界面用；读为主，改删都要用户点头） ----------
    def sessions(self, limit=20):
        """会话列表：只给界面用，读不到就返回空。"""
        try:
            return self.repository.sessions(limit=limit)
        except Exception as exc:
            self._warn("session list failed: %s", exc)
            return []

    # ---------- 会话管理（界面用；改名、置顶、归档、删除都要用户点头） ----------
    def session_overview(self, limit=200, include_archived=False):
        try:
            return self.repository.session_overview(
                limit=limit, include_archived=include_archived
            )
        except Exception as exc:
            self._warn("session overview failed: %s", exc)
            return []

    def session_meta(self, session_id):
        try:
            return self.repository.session_meta(session_id) or {}
        except Exception as exc:
            self._warn("session meta failed: %s", exc)
            return {}

    def rename_session(self, session_id, title):
        return self.repository.update_session(session_id, title=str(title or "").strip())

    def pin_session(self, session_id, pinned=True):
        return self.repository.update_session(session_id, pinned=bool(pinned))

    def archive_session(self, session_id, archived=True):
        return self.repository.update_session(session_id, archived=bool(archived))

    def tag_session(self, session_id, tags):
        return self.repository.update_session(session_id, tags=str(tags or ""))

    def delete_session(self, session_id):
        return self.repository.delete_session(session_id)

    def find_sessions(self, word, limit=50):
        word = str(word or "").strip().lower()
        rows = self.session_overview(limit=limit, include_archived=True)
        if not word:
            return rows
        return [
            row
            for row in rows
            if word in str(row.get("title") or "").lower()
            or word in str(row.get("session_id") or "").lower()
            or word in str(row.get("tags") or "").lower()
        ]

    def last_exchange(self, session_id):
        """最近一轮问答，给「编辑重发」和「重新生成」用。"""
        rows = self.repository.recent_messages(session_id, limit=8)
        user_text = pet_text = ""
        for row in reversed(rows):
            role = str(row.get("role") or "")
            if role == "user" and not user_text:
                user_text = str(row.get("content") or "")
            elif role == "assistant" and not pet_text and user_text:
                pet_text = str(row.get("content") or "")
                break
        return user_text, pet_text

    def stats(self):
        return {
            "total": self.repository.count_memories(),
            "archived": self.repository.archived_count(),
            "kinds": self.repository.kind_counts(),
            "capacity": self.memory_capacity,
        }

    def browse(self, kind=None, query=None, include_archived=False, limit=200, offset=0):
        """记忆管理界面用。只列出当前宠物的和公共的，别只宠物的看不到。"""
        return self.repository.browse(
            kind=kind,
            query=query,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
            pet_id=self.pet_id,
        )

    def get(self, memory_id):
        """按 id 取一条。拿别的宠物的 id 也取不到，返回 None。"""
        return self.repository.get_memory(memory_id, pet_id=self.pet_id)

    def edit(self, memory_id, content=None, importance=None):
        """改一条记忆。改了内容就重算关键词。"""
        text = None
        keywords = None
        if content is not None:
            text = str(content).strip()
            if not text:
                return False
            keywords = ",".join(sorted(_terms(text))[:40])
        try:
            return bool(
                self.repository.update_memory(
                    memory_id, text, importance, keywords, pet_id=self.pet_id
                )
            )
        except Exception as exc:
            self._warn("memory edit failed: %s", exc)
            return False

    def set_locked(self, memory_id, locked=True):
        try:
            return bool(self.repository.set_locked(memory_id, locked, pet_id=self.pet_id))
        except Exception as exc:
            self._warn("memory lock failed: %s", exc)
            return False

    def confirm_memory(self, memory_id, confirmed=True):
        """用户认下这条：从「未确认」变成用户事实。"""
        try:
            return bool(
                self.repository.confirm_memory(memory_id, confirmed, pet_id=self.pet_id)
            )
        except Exception as exc:
            self._warn("memory confirm failed: %s", exc)
            return False

    def archive_memory(self, memory_id, archived=True):
        """撤下一条记忆：不再参与检索和表达，但内容留在库里，随时能从记忆管理恢复。"""
        try:
            self.repository.set_archived(memory_id, archived, pet_id=self.pet_id)
            return True
        except Exception as exc:
            self._warn("memory archive failed: %s", exc)
            return False

    def forget(self, memory_id, confirm=False):
        """删一条记忆。没有确认就拒绝，绝不自动删。"""
        if not confirm:
            return False
        try:
            return bool(self.repository.delete_memory(memory_id, pet_id=self.pet_id))
        except Exception as exc:
            self._warn("memory delete failed: %s", exc)
            return False

    @staticmethod
    def _describe_memory(row):
        """一条记忆写给模型看的样子：带层次，也带可信度标记。"""
        prefix = _KIND_PREFIX.get(row.get("kind"), "")
        source = row.get("source")
        if source == "USER_EXPLICIT":
            mark = "" if row.get("confirmed", 1) else "（待确认）"
        else:
            mark = _SOURCE_MARKS.get(source, "（不确定）")
        return f"{prefix}{mark}{row.get('content', '')}"

    def recall(self, session_id, query):
        """返回 (最近消息, 记忆块, 摘要)，供 Context 装配。"""
        recent = self.repository.recent_messages(session_id, self.recent_window)
        summary_row = self.repository.latest_summary(session_id) or {}
        memories = self.search(query)

        lines = []
        for row in memories:
            lines.append(f"- {self._describe_memory(row)}")
        # 项目状态排在长期记忆之后：它说的是「现在做到哪一步」。
        project_lines = self.projects.context_lines()
        lines.extend(project_lines)
        for row in self.repository.all_preferences():
            if not _visible_preference(row["key"]):
                continue
            lines.append(f"- 用户偏好 {row['key']}: {row['value']}")
        relationship = self.relationship.note()
        if relationship:
            lines.append(f"- {relationship}")
        block = "\n".join(lines)

        self.last_recall = {
            "recent_messages": len(recent),
            "summary": bool(summary_row.get("content")),
            "memory_ids": [row["id"] for row in memories],
            "memory_count": len(memories),
            "project_count": len(project_lines),
        }
        return recent, block, summary_row.get("content", "")

    def record_usage(self, session_id, model, usage):
        return self.repository.record_usage(
            session_id, model, usage.prompt_tokens, usage.completion_tokens
        )

    def usage_totals(self, session_id=None):
        return self.repository.usage_totals(session_id)
