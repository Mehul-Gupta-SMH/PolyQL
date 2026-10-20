"""
In-process TTL + LRU cache for retrieval results (the schema context built for
a question). Repeated questions and agents calling search_schema many times
skip the vector search, reranker and join-graph lookup.

Config (Utilities/retrieval_config.YAML, optional — defaults shown):
    cache: {enabled: true, ttl_seconds: 300, max_entries: 256}

Values are deep-copied on the way in and out, so callers can mutate what they
get back. Anything that changes metadata (ingestion, relationships, glossary,
OKF, Ossie) calls clear(); the TTL is only a backstop for out-of-process edits.
The cache is per process: a write made by another process (e.g. ingesting
through the web app while a separate MCP server process runs) is only seen
there after the TTL expires.
"""

import copy
import logging
import threading
import time
from collections import OrderedDict

logger = logging.getLogger(__name__)

_DEFAULTS = {"enabled": True, "ttl_seconds": 300, "max_entries": 256}


class TTLCache:
    """Thread-safe LRU cache whose entries expire *ttl_seconds* after being stored."""

    def __init__(self, max_entries: int = 256, ttl_seconds: float = 300, clock=time.monotonic):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._data: OrderedDict = OrderedDict()  # key -> (expires_at, value)
        self._lock = threading.Lock()
        self.generation = 0  # bumped by clear(); lets set() reject results computed before it

    def get(self, key):
        """Copy of the cached value, or None on a miss or expired entry."""
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if self._clock() >= expires_at:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return copy.deepcopy(value)

    def set(self, key, value, generation: int | None = None) -> None:
        """Store a copy of *value*; no-op if clear() ran since *generation* was read."""
        with self._lock:
            if generation is not None and generation != self.generation:
                return
            self._data[key] = (self._clock() + self.ttl_seconds, copy.deepcopy(value))
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self.generation += 1

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


_cache = TTLCache()
_cache_cfg: tuple | None = None  # (max_entries, ttl_seconds) the shared cache was built with


def _config() -> dict:
    try:
        from Utilities.base_utils import get_config_val
        cfg = get_config_val("retrieval_config", ["cache"], True) or {}
    except Exception:
        cfg = {}
    return {**_DEFAULTS, **cfg}


def _shared_cache(cfg: dict) -> TTLCache:
    """The shared cache, rebuilt if the configured size or TTL changed."""
    global _cache, _cache_cfg
    wanted = (int(cfg["max_entries"]), float(cfg["ttl_seconds"]))
    if _cache_cfg != wanted:
        _cache = TTLCache(max_entries=wanted[0], ttl_seconds=wanted[1])
        _cache_cfg = wanted
    return _cache


def make_key(query: str, instance_name: str = "default") -> tuple:
    """Cache key: whitespace-normalised query (case kept) + instance."""
    return (" ".join((query or "").split()), instance_name or "default")


def cached_context(query: str, instance_name: str, compute) -> dict:
    """
    Return compute()'s result for (query, instance_name), from cache when fresh.

    Exceptions from compute() propagate and are not cached; results with no
    tables are returned but not cached.
    """
    cfg = _config()
    if not cfg["enabled"]:
        return compute()

    cache = _shared_cache(cfg)
    key = make_key(query, instance_name)
    hit = cache.get(key)
    if hit is not None:
        logger.debug("retrieval cache hit: %r", key)
        return hit

    generation = cache.generation  # a write during compute() makes this result stale
    result = compute()
    table_list = result.get("table_list") or {}
    if any(table_list.get(kind) for kind in ("direct", "intermediate")):
        cache.set(key, result, generation=generation)
    return result


def clear() -> None:
    """Drop every cached entry. Call after any metadata write."""
    _cache.clear()
