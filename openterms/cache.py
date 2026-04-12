"""
In-memory LRU cache for openterms.json payloads.

Thread-safe via a simple lock. No external dependencies.
"""

from __future__ import annotations

import threading
from typing import Dict, Optional

from .models import CacheEntry


class TermsCache:
    """
    A thread-safe in-memory cache keyed by normalised domain string.

    TTL is determined per-entry at insertion time (from Cache-Control
    headers or the global default). Expired entries are lazily evicted
    on the next ``get`` call.
    """

    def __init__(self) -> None:
        self._store: Dict[str, CacheEntry] = {}
        self._lock = threading.Lock()

    def get(self, domain: str) -> Optional[CacheEntry]:
        """Return a live cache entry for *domain*, or None if absent/expired."""
        with self._lock:
            entry = self._store.get(domain)
            if entry is None:
                return None
            if entry.is_expired():
                del self._store[domain]
                return None
            return entry

    def set(self, domain: str, entry: CacheEntry) -> None:
        """Insert or overwrite the cache entry for *domain*."""
        with self._lock:
            self._store[domain] = entry

    def delete(self, domain: str) -> None:
        """Remove a single domain from the cache."""
        with self._lock:
            self._store.pop(domain, None)

    def clear(self) -> None:
        """Flush the entire cache."""
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


# Module-level default cache instance shared by the top-level API functions.
_default_cache: TermsCache = TermsCache()


def get_default_cache() -> TermsCache:
    """Return the module-level shared cache."""
    return _default_cache
