import time
from typing import Any


class TTLCache:
    def __init__(self, ttl: float, maxsize: int = 100_000) -> None:
        if ttl <= 0 or maxsize <= 0:
            raise ValueError("ttl and maxsize must be positive")
        self.ttl = ttl
        self.maxsize = maxsize
        self._data: dict[str, tuple[float, Any]] = {}

    def _prune(self) -> None:
        now = time.monotonic()
        expired = [k for k, (exp, _) in self._data.items() if exp <= now]
        for k in expired:
            del self._data[k]

    def get(self, key: str, default: Any = None) -> Any:
        entry = self._data.get(key)
        if entry is None:
            return default
        expires, value = entry
        if expires <= time.monotonic():
            del self._data[key]
            return default
        return value

    def set(self, key: str, value: Any) -> None:
        if key not in self._data and len(self._data) >= self.maxsize:
            self._prune()
            if len(self._data) >= self.maxsize:
                del self._data[next(iter(self._data))]
        self._data[key] = (time.monotonic() + self.ttl, value)

    def pop(self, key: str) -> None:
        self._data.pop(key, None)
