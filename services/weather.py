"""天气服务。只负责取数据，不生成回复；失败时如实返回原因，不编造数据。

使用 Open-Meteo，本地无需 API Key 即可使用。
"""

import json
import urllib.parse
import urllib.request

from services.cache import TTLCache

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO 天气码 → 简短中文
WEATHER_CODES = {
    0: "晴",
    1: "少云",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "毛毛雨",
    53: "小雨",
    55: "中雨",
    56: "冻雨",
    57: "冻雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "霰",
    80: "阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "阵雪",
    86: "强阵雪",
    95: "雷阵雨",
    96: "雷阵雨伴冰雹",
    99: "强雷阵雨伴冰雹",
}


def describe_code(code):
    try:
        return WEATHER_CODES.get(int(code), f"未知天气({code})")
    except (TypeError, ValueError):
        return "未知天气"


def _stamp(result, from_cache, stale, age_seconds, note=""):
    """把「数据从哪来、放了多久」标进结果，界面和模型都能如实说明。"""
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


def http_get_json(url, timeout=10.0):
    request = urllib.request.Request(url, headers={"User-Agent": "deskpet/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class WeatherService:
    """查询指定城市当前天气。"""

    def __init__(self, fetch=None, cache=None, default_city=""):
        self.fetch = fetch or http_get_json
        self.cache = cache or TTLCache(ttl_seconds=600)
        self.default_city = default_city or ""

    def current(self, city=""):
        city = (city or self.default_city or "").strip()
        if not city:
            return {"ok": False, "error": "没有指定城市，请告诉我城市名"}

        cache_key = f"weather:{city}"
        cached = self.cache.entry(cache_key)
        fresh = self._fresh(cached)
        if fresh is not None:
            return fresh

        try:
            place = self._geocode(city)
        except Exception as exc:
            return self._degrade(cached, f"天气服务暂时不可用：{exc}")
        if place is None:
            return {"ok": False, "error": f"找不到城市：{city}"}

        try:
            payload = self.fetch(self._forecast_url(place))
        except Exception as exc:
            return self._degrade(cached, f"天气服务暂时不可用：{exc}")

        current = (payload or {}).get("current") or {}
        if not current:
            return self._degrade(cached, "天气服务返回了无法识别的数据")

        result = {
            "ok": True,
            "data": {
                "city": place.get("name") or city,
                "temperature": current.get("temperature_2m"),
                "wind": current.get("wind_speed_10m"),
                "weather": describe_code(current.get("weather_code")),
            },
        }
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
        """取新数据失败：有旧记录就如实给旧记录并说清放了多久，没有就报错。"""
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

    def _geocode(self, city):
        query = urllib.parse.urlencode({"name": city, "count": 1, "language": "zh"})
        payload = self.fetch(f"{GEOCODE_URL}?{query}")
        results = (payload or {}).get("results") or []
        return results[0] if results else None

    @staticmethod
    def _forecast_url(place):
        query = urllib.parse.urlencode(
            {
                "latitude": place.get("latitude"),
                "longitude": place.get("longitude"),
                "current": "temperature_2m,weather_code,wind_speed_10m",
            }
        )
        return f"{FORECAST_URL}?{query}"
