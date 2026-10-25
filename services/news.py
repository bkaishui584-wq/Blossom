"""新闻服务。密钥只从环境变量读取；没有密钥时明确返回不可用，不编造内容。"""

import json
import urllib.parse
import urllib.request

from services.cache import TTLCache

DEFAULT_BASE_URL = "https://newsapi.org/v2"


def http_get_json(url, api_key=None, timeout=10.0):
    headers = {"User-Agent": "deskpet/0.1"}
    if api_key:
        headers["X-Api-Key"] = api_key
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _stamp(result, from_cache, stale, age_seconds, note=""):
    """和天气一样，标清数据从哪来、放了多久，取不到就是取不到。"""
    meta = {
        "from_cache": bool(from_cache),
        "stale": bool(stale),
        "age_seconds": int(age_seconds),
    }
    if note:
        meta["note"] = note
    stamped = {**result, **meta}
    data = dict(result.get("data") or {})
    data.update(meta)
    stamped["data"] = data
    return stamped


class NewsService:
    """取几条头条或按关键词搜索。"""

    def __init__(self, api_key="", base_url="", fetch=None, cache=None, max_items=5):
        self.api_key = api_key or ""
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.fetch = fetch or self._http_get
        self.cache = cache or TTLCache(ttl_seconds=600)
        self.max_items = max(1, int(max_items))

    def available(self):
        return bool(self.api_key)

    def latest(self, topic="", limit=3):
        if not self.available():
            return {"ok": False, "error": "新闻服务暂时不可用：未配置新闻 API Key"}

        try:
            limit = max(1, min(self.max_items, int(limit or 3)))
        except (TypeError, ValueError):
            limit = 3

        topic = (topic or "").strip()
        cache_key = f"news:{topic}:{limit}"
        cached = self.cache.entry(cache_key)
        fresh = self._fresh(cached)
        if fresh is not None:
            return fresh

        endpoint = "everything" if topic else "top-headlines"
        params = {
            "language": "zh",
            "pageSize": limit,
        }
        if topic:
            params["q"] = topic
        else:
            params["country"] = "cn"

        try:
            payload = self.fetch(f"{self.base_url}/{endpoint}?{urllib.parse.urlencode(params)}")
        except Exception as exc:
            return self._degrade(cached, f"新闻服务暂时不可用：{exc}")

        if not isinstance(payload, dict) or payload.get("status") == "error":
            message = (payload or {}).get("message") if isinstance(payload, dict) else None
            return self._degrade(cached, f"新闻服务返回错误：{message or '无法识别的响应'}")

        articles = payload.get("articles") or []
        items = [
            {
                "title": (article.get("title") or "").strip(),
                "source": ((article.get("source") or {}).get("name") or "").strip(),
            }
            for article in articles[:limit]
        ]
        result = {"ok": True, "data": {"topic": topic or "头条", "items": items}}
        self.cache.set(cache_key, result)
        return _stamp(result, from_cache=False, stale=False, age_seconds=0)

    def _fresh(self, cached):
        """没过期的缓存直接给，并标明是缓存；过期或没有就返回 None。"""
        if not cached:
            return None
        age, value = cached
        ttl = int(getattr(self.cache, "ttl_seconds", 0) or 0)
        if ttl and age >= ttl:
            return None
        return _stamp(value, from_cache=True, stale=False, age_seconds=age)

    def _degrade(self, cached, reason):
        """取新新闻失败：有旧记录就给旧记录并说清时间，没有就如实报错。"""
        if not cached:
            return {"ok": False, "error": reason}
        age, value = cached
        minutes = max(1, int(age // 60))
        return _stamp(
            value,
            from_cache=True,
            stale=True,
            age_seconds=age,
            note=f"{reason}。这是 {minutes} 分钟前的记录。",
        )

    def _http_get(self, url):
        # 密钥走请求头，不出现在 URL 里，避免被日志或异常信息带出去。
        return http_get_json(url, api_key=self.api_key)
