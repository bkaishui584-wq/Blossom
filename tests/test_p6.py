"""P6 验收：服务层取数、失败不编造、结果缓存、工具权限与端到端调用。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.context import ContextManager
from agent.core import AgentCore
from database.db import Database
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, ToolCall, Usage
from security.audit import AuditLog
from services.cache import TTLCache
from services.calendar import CalendarService, parse_when
from services.news import NewsService
from services.weather import WeatherService, describe_code
from tools.builtin import register_default_tools
from tools.permission import PermissionManager
from tools.registry import ToolExecutor, ToolRegistry


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


def weather_fetch(calls, fail=False):
    def fetch(url):
        calls.append(url)
        if fail:
            raise RuntimeError("连接超时")
        if "geocoding" in url:
            return {"results": [{"name": "北京", "latitude": 39.9, "longitude": 116.4}]}
        return {
            "current": {"temperature_2m": 21.5, "weather_code": 61, "wind_speed_10m": 3.2}
        }

    return fetch


def news_fetch(calls, payload=None):
    def fetch(url):
        calls.append(url)
        if payload is not None:
            return payload
        return {
            "status": "ok",
            "articles": [
                {"title": "标题一", "source": {"name": "来源甲"}},
                {"title": "标题二", "source": {"name": "来源乙"}},
                {"title": "标题三", "source": {"name": "来源丙"}},
            ],
        }

    return fetch


class CacheTests(unittest.TestCase):
    def test_hit_and_expiry(self):
        clock = FakeClock(100.0)
        cache = TTLCache(ttl_seconds=60, clock=clock)
        cache.set("k", "v")
        self.assertEqual(cache.get("k"), "v")
        clock.advance(59)
        self.assertEqual(cache.get("k"), "v")
        clock.advance(2)
        self.assertIsNone(cache.get("k"))

    def test_missing_key(self):
        self.assertIsNone(TTLCache().get("nope"))


class WeatherTests(unittest.TestCase):
    def test_needs_a_city(self):
        calls = []
        service = WeatherService(fetch=weather_fetch(calls))
        result = service.current("")
        self.assertFalse(result["ok"])
        self.assertIn("城市", result["error"])
        self.assertEqual(calls, [])

    def test_reads_current_weather(self):
        service = WeatherService(fetch=weather_fetch([]))
        result = service.current("北京")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["city"], "北京")
        self.assertEqual(result["data"]["temperature"], 21.5)
        self.assertEqual(result["data"]["weather"], "小雨")

    def test_unknown_city_is_reported(self):
        service = WeatherService(fetch=lambda url: {"results": []})
        result = service.current("不存在的地方")
        self.assertFalse(result["ok"])
        self.assertIn("找不到城市", result["error"])

    def test_network_failure_is_reported_not_faked(self):
        service = WeatherService(fetch=weather_fetch([], fail=True))
        result = service.current("北京")
        self.assertFalse(result["ok"])
        self.assertIn("暂时不可用", result["error"])
        self.assertNotIn("temperature", result)

    def test_result_is_cached(self):
        calls = []
        service = WeatherService(fetch=weather_fetch(calls))
        service.current("北京")
        service.current("北京")
        self.assertEqual(len(calls), 2)

    def test_default_city_used_when_omitted(self):
        service = WeatherService(fetch=weather_fetch([]), default_city="上海")
        self.assertTrue(service.current("")["ok"])

    def test_weather_codes_have_chinese_labels(self):
        self.assertEqual(describe_code(0), "晴")
        self.assertIn("未知", describe_code(1234))
        self.assertIn("未知", describe_code(None))


class NewsTests(unittest.TestCase):
    def test_missing_key_is_reported_without_network_call(self):
        calls = []
        service = NewsService(fetch=news_fetch(calls))
        result = service.latest()
        self.assertFalse(result["ok"])
        self.assertIn("未配置新闻 API Key", result["error"])
        self.assertEqual(calls, [])

    def test_headlines_are_parsed(self):
        service = NewsService(api_key="news-key-1234", fetch=news_fetch([]))
        result = service.latest(limit=2)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["data"]["items"]), 2)
        self.assertEqual(result["data"]["items"][0]["title"], "标题一")
        self.assertEqual(result["data"]["items"][0]["source"], "来源甲")

    def test_api_key_not_placed_in_url(self):
        calls = []
        service = NewsService(api_key="news-key-1234", fetch=news_fetch(calls))
        service.latest()
        self.assertNotIn("news-key-1234", calls[0])

    def test_api_error_payload_is_reported(self):
        service = NewsService(
            api_key="k-123456", fetch=news_fetch([], payload={"status": "error", "message": "额度用尽"})
        )
        result = service.latest()
        self.assertFalse(result["ok"])
        self.assertIn("额度用尽", result["error"])

    def test_network_failure_is_reported(self):
        def boom(url):
            raise RuntimeError("连接被拒绝")

        service = NewsService(api_key="k-123456", fetch=boom)
        result = service.latest()
        self.assertFalse(result["ok"])
        self.assertIn("暂时不可用", result["error"])

    def test_result_is_cached(self):
        calls = []
        service = NewsService(api_key="k-123456", fetch=news_fetch(calls))
        service.latest("科技")
        service.latest("科技")
        self.assertEqual(len(calls), 1)


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Database(Path(self.tmp.name) / "t.sqlite3")
        self.database.migrate()
        self.service = CalendarService(self.database)

    def test_add_and_list(self):
        added = self.service.add_event("交周报", "2026-10-07 09:00", "记得附截图")
        self.assertTrue(added["ok"])
        listed = self.service.list_events()
        self.assertEqual(listed["data"]["count"], 1)
        event = listed["data"]["events"][0]
        self.assertEqual(event["title"], "交周报")
        self.assertEqual(event["when_at"], "2026-10-07 09:00")
        self.assertEqual(event["note"], "记得附截图")

    def test_events_are_ordered_by_time(self):
        self.service.add_event("晚的", "2026-10-09 10:00")
        self.service.add_event("早的", "2026-10-07 08:00")
        titles = [e["title"] for e in self.service.list_events()["data"]["events"]]
        self.assertEqual(titles, ["早的", "晚的"])

    def test_missing_title_is_rejected(self):
        result = self.service.add_event("   ", "2026-10-07 09:00")
        self.assertFalse(result["ok"])
        self.assertIn("标题", result["error"])

    def test_bad_time_is_rejected(self):
        result = self.service.add_event("开会", "明天下午")
        self.assertFalse(result["ok"])
        self.assertIn("时间格式", result["error"])
        self.assertEqual(self.service.list_events()["data"]["count"], 0)

    def test_date_only_defaults_to_morning(self):
        self.assertEqual(parse_when("2026-10-07"), "2026-10-07 09:00")

    def test_month_day_infers_current_year(self):
        from datetime import datetime

        parsed = parse_when("10-07 09:30", datetime(2026, 1, 1, 8, 0))
        self.assertEqual(parsed, "2026-10-07 09:30")


class ToolRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Database(Path(self.tmp.name) / "t.sqlite3")
        self.database.migrate()
        from security.config import Config

        self.config = Config(load_env=False)
        self.registry = ToolRegistry()
        register_default_tools(self.registry, self.config, self.database)

    def test_tools_registered_with_expected_permissions(self):
        levels = {tool.name: tool.permission for tool in self.registry.all()}
        self.assertEqual(levels["get_weather"], "READ")
        self.assertEqual(levels["get_news"], "READ")
        self.assertEqual(levels["list_calendar_events"], "READ")
        self.assertEqual(levels["add_calendar_event"], "LOW")

    def test_relevance_only_picks_matching_tools(self):
        self.assertEqual(self.registry.relevant("今天天气怎么样"), ["get_weather"])
        self.assertEqual(self.registry.relevant("给我讲讲新闻"), ["get_news"])
        self.assertEqual(self.registry.relevant("讲个冷笑话"), [])

    def test_read_tool_needs_no_confirmation(self):
        executor = ToolExecutor(self.registry, PermissionManager())
        outcome = executor.execute("get_weather", {"city": ""})
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.allowed)
        self.assertIn("城市", outcome.error)

    def test_service_failure_surfaces_as_real_error(self):
        registry = ToolRegistry()
        register_default_tools(registry, self.config, self.database)
        executor = ToolExecutor(registry, PermissionManager())
        outcome = executor.execute("get_news", {"topic": ""})
        self.assertFalse(outcome.ok)
        self.assertIn("未配置新闻 API Key", outcome.error)

    def test_unknown_argument_is_rejected(self):
        executor = ToolExecutor(self.registry, PermissionManager())
        outcome = executor.execute("get_weather", {"city": "北京", "units": "c"})
        self.assertFalse(outcome.ok)
        self.assertIn("未声明参数", outcome.error)

    def test_calendar_tool_writes_real_data(self):
        executor = ToolExecutor(self.registry, PermissionManager())
        outcome = executor.execute(
            "add_calendar_event", {"title": "喝水", "when": "2026-10-07 15:00"}
        )
        self.assertTrue(outcome.ok)
        self.assertIn("喝水", outcome.result)
        rows = self.database.query("SELECT title, when_at FROM calendar_events")
        self.assertEqual(rows[0]["when_at"], "2026-10-07 15:00")


class WeatherThroughAgentTests(unittest.TestCase):
    class WeatherProvider(ModelProvider):
        name = "weather-stub"

        def __init__(self):
            self.calls = 0
            self.seen = []

        def is_available(self):
            return True

        def chat(self, messages, tools=None, temperature=0.7):
            self.calls += 1
            self.seen = messages
            if self.calls == 1:
                return ChatResult(
                    ok=True,
                    tool_calls=[ToolCall(id="c1", name="get_weather", arguments={"city": "北京"})],
                    usage=Usage(prompt_tokens=10, completion_tokens=4),
                    model="stub",
                )
            return ChatResult(ok=True, content="北京 21.5 度，小雨。", model="stub")

        def stream(self, messages, tools=None, temperature=0.7):
            yield ChatDelta(done=True)

    def test_agent_uses_weather_service_result(self):
        from security.config import Config
        from tools.registry import Tool
        from tools.builtin import unwrap

        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "t.sqlite3")
            database.migrate()

            registry = ToolRegistry()
            service = WeatherService(fetch=weather_fetch([]))
            registry.register(
                Tool(
                    name="get_weather",
                    description="查天气",
                    permission="READ",
                    handler=lambda city="": unwrap(service.current(city)),
                    keywords=("天气",),
                )
            )
            audit = AuditLog(database)
            executor = ToolExecutor(registry, PermissionManager(audit=audit), audit=audit)
            provider = self.WeatherProvider()
            agent = AgentCore(
                provider,
                MemoryManager(MemoryRepository(database)),
                ContextManager(token_budget=800),
                executor=executor,
            )
            reply = agent.respond("s", "北京今天天气怎么样")
            self.assertTrue(reply.ok)
            self.assertEqual(reply.debug["tool_results"][0]["ok"], True)
            self.assertEqual(reply.content, "北京 21.5 度，小雨。")
            tool_message = provider.seen[-1]["content"]
            self.assertIn("21.5", tool_message)
            self.assertIn("北京", tool_message)


if __name__ == "__main__":
    unittest.main()
