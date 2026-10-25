"""服务结果缓存。避免重复请求，减少网络与 Token 开销。"""

import time


class TTLCache:
    def __init__(self, ttl_seconds=600, clock=time.time):
        self.ttl_seconds = max(0, int(ttl_seconds))
        self.clock = clock
        self._items = {}

    def get(self, key):
        item = self._items.get(key)
        if item is None:
            return None
        stored_at, value = item
        if self.ttl_seconds and (self.clock() - stored_at) >= self.ttl_seconds:
            self._items.pop(key, None)
            return None
        return value

    def set(self, key, value):
        self._items[key] = (self.clock(), value)
        return value

    def entry(self, key):
        """不管过没过期，取出 (存了多久, 值)。取不到返回 None。

        过期降级要用旧值，所以这个方法不删条目，也不看 TTL。
        """
        item = self._items.get(key)
        if item is None:
            return None
        stored_at, value = item
        return max(0.0, self.clock() - stored_at), value

    def clear(self):
        self._items.clear()
