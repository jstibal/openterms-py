"""
Core API surface for openterms-py.

All public functions (fetch, check, discover, receipt) are module-level
convenience wrappers around the OpenTermsClient class. The module ships
a single shared client instance; call configure() to adjust its settings.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import time
from typing import Any, Dict, Literal, Optional

try:
    import requests  # type: ignore[import]
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "openterms-py requires the 'requests' package. "
        "Install it with: pip install openterms-py"
    ) from exc

from .cache import TermsCache, get_default_cache
from .models import CacheEntry, CheckResult, DiscoveryResult, Receipt

# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------

_DEFAULT_TTL: int = 3600          # seconds
_DEFAULT_TIMEOUT: int = 10        # seconds for HTTP requests
_DEFAULT_USER_AGENT: str = "openterms-py/0.1.0"
_DEFAULT_REGISTRY_URL: str = "https://openterms.com/registry"

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalise_domain(domain: str) -> str:
    """Strip scheme/path and lowercase the domain string."""
    domain = domain.strip().lower()
    # Remove any scheme
    domain = re.sub(r"^https?://", "", domain)
    # Strip trailing slashes and paths
    domain = domain.split("/")[0]
    return domain


def _parse_max_age(cache_control: str) -> Optional[int]:
    """
    Extract max-age seconds from a Cache-Control header value.
    Returns None if the header is absent or has no max-age directive.
    """
    match = re.search(r"max-age=(\d+)", cache_control, re.IGNORECASE)
    if match:
        return int(match.group(1))
    # Treat no-store / no-cache as TTL=0 (do not cache)
    if re.search(r"no-store|no-cache", cache_control, re.IGNORECASE):
        return 0
    return None


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _utc_now_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# OpenTermsClient
# ---------------------------------------------------------------------------


class OpenTermsClient:
    """
    Low-level client for fetching and querying openterms.json files.

    You normally don't instantiate this directly — use the module-level
    ``fetch``, ``check``, ``discover``, and ``receipt`` functions instead.
    """

    def __init__(
        self,
        default_ttl: int = _DEFAULT_TTL,
        timeout: int = _DEFAULT_TIMEOUT,
        user_agent: str = _DEFAULT_USER_AGENT,
        cache: Optional[TermsCache] = None,
        registry_url: Optional[str] = _DEFAULT_REGISTRY_URL,
    ) -> None:
        self.default_ttl = default_ttl
        self.timeout = timeout
        self.user_agent = user_agent
        self.registry_url = registry_url.rstrip("/") if registry_url else None
        self._cache: TermsCache = cache or get_default_cache()
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": self.user_agent})

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def fetch(self, domain: str) -> Optional[Dict[str, Any]]:
        """
        Fetch and return the parsed openterms.json for *domain*.

        Tries ``/.well-known/openterms.json`` first, falls back to
        ``/openterms.json``. Returns ``None`` if neither URL responds
        with a valid JSON document.

        Results are cached according to the server's Cache-Control header
        or the client's ``default_ttl``.
        """
        domain = _normalise_domain(domain)

        # 1. Check cache
        cached = self._cache.get(domain)
        if cached is not None:
            return cached.data

        # 2. Try to fetch from network
        raw_json, content_hash, ttl = self._fetch_raw(domain)
        if raw_json is None:
            return None

        # 3. Store in cache
        entry = CacheEntry(
            data=raw_json,
            fetched_at=time.time(),
            ttl=ttl,
            content_hash=content_hash,
        )
        self._cache.set(domain, entry)
        return raw_json

    def check(
        self, domain: str, action: str
    ) -> CheckResult:
        """
        Check whether *action* is permitted by the openterms.json for *domain*.

        Permission keys are looked up case-insensitively.

        Decision logic:
        - The value is ``True`` (or a conditional dict with ``allowed: true``) → **allow**
        - The value is ``False`` (or a conditional dict with ``allowed: false``) → **deny**
        - The key is absent, the file is unreachable, or the value is anything
          else → **not_specified**

        Returns a :class:`CheckResult` whose ``__bool__`` is ``True`` when
        the decision is ``"allow"``.
        """
        domain = _normalise_domain(domain)

        # Check cache to determine source tag
        cached_before = self._cache.get(domain)
        source: Literal["cache", "network"] = "cache" if cached_before else "network"

        data = self.fetch(domain)

        # If we just fetched (cache was empty), update source
        if cached_before is None:
            source = "network"

        if data is None:
            return CheckResult(
                domain=domain,
                action=action,
                decision="not_specified",
                raw_value=None,
                source=source,
            )

        permissions: Dict[str, Any] = data.get("permissions", {})
        # Case-insensitive key lookup
        action_lower = action.lower()
        raw_value: Optional[Any] = None
        for key, val in permissions.items():
            if key.lower() == action_lower:
                raw_value = val
                break

        if raw_value is None:
            decision: Literal["allow", "deny", "not_specified"] = "not_specified"
        elif isinstance(raw_value, bool):
            decision = "allow" if raw_value else "deny"
        elif isinstance(raw_value, dict):
            allowed = raw_value.get("allowed")
            if allowed is True:
                decision = "allow"
            elif allowed is False:
                decision = "deny"
            else:
                decision = "not_specified"
        else:
            decision = "not_specified"

        return CheckResult(
            domain=domain,
            action=action,
            decision=decision,
            raw_value=raw_value,
            source=source,
        )

    def discover(self, domain: str) -> Optional[DiscoveryResult]:
        """
        Return the ``discovery`` block from *domain*'s openterms.json.

        Returns ``None`` if the file is unreachable or contains no
        ``discovery`` key.
        """
        domain = _normalise_domain(domain)
        data = self.fetch(domain)
        if data is None:
            return None
        discovery_raw = data.get("discovery")
        if discovery_raw is None:
            return None
        return DiscoveryResult.from_dict(discovery_raw)

    def receipt(
        self,
        domain: str,
        action: str,
        decision: Literal["allow", "deny", "not_specified"],
    ) -> Receipt:
        """
        Generate a minimal ORS compliance receipt for a permission check.

        The receipt captures the domain, action, decision, a UTC timestamp,
        and the SHA-256 hash of the openterms.json content at the time the
        check was performed. It is a **local artifact only** — nothing is
        sent to any server.

        The consuming application is responsible for persisting or logging
        the receipt however it sees fit.

        If the openterms.json content is not in the local cache (e.g. the
        domain was unreachable), the hash will be an empty string.
        """
        domain = _normalise_domain(domain)
        cached = self._cache.get(domain)
        content_hash = cached.content_hash if cached else ""

        return Receipt(
            domain=domain,
            action=action,
            decision=decision,
            timestamp=_utc_now_iso(),
            openterms_hash=content_hash,
        )

    def clear_cache(self, domain: Optional[str] = None) -> None:
        """
        Clear cached entries.

        If *domain* is provided, only that domain is evicted.
        Otherwise the entire cache is flushed.
        """
        if domain is not None:
            self._cache.delete(_normalise_domain(domain))
        else:
            self._cache.clear()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch_raw(
        self, domain: str
    ) -> tuple[Optional[Dict[str, Any]], str, int]:
        """
        Attempt to fetch openterms.json for *domain*.

        Lookup order:
        1. ``https://{domain}/.well-known/openterms.json``
        2. ``https://{domain}/openterms.json``
        3. ``{registry_url}/{domain}`` (if ``registry_url`` is set)

        Returns ``(parsed_json, sha256_hash, effective_ttl_seconds)`` or
        ``(None, '', 0)`` on failure.
        """
        urls = [
            f"https://{domain}/.well-known/openterms.json",
            f"https://{domain}/openterms.json",
        ]

        for url in urls:
            try:
                resp = self._session.get(url, timeout=self.timeout)
            except requests.RequestException:
                continue

            if resp.status_code != 200:
                continue

            raw_text = resp.text
            try:
                parsed = json.loads(raw_text)
            except json.JSONDecodeError:
                continue

            if not isinstance(parsed, dict):
                continue

            content_hash = _sha256(raw_text)

            # Determine TTL from Cache-Control header
            ttl = self.default_ttl
            cc_header = resp.headers.get("Cache-Control", "")
            if cc_header:
                parsed_ttl = _parse_max_age(cc_header)
                if parsed_ttl is not None:
                    ttl = parsed_ttl

            return parsed, content_hash, ttl

        # 3. Fall back to registry if configured
        if self.registry_url:
            registry_url = f"{self.registry_url}/{domain}"
            try:
                resp = self._session.get(registry_url, timeout=self.timeout)
            except requests.RequestException:
                return None, "", 0

            if resp.status_code == 200:
                raw_text = resp.text
                try:
                    parsed = json.loads(raw_text)
                except json.JSONDecodeError:
                    return None, "", 0

                if isinstance(parsed, dict) and "error" not in parsed:
                    content_hash = _sha256(raw_text)
                    ttl = self.default_ttl
                    cc_header = resp.headers.get("Cache-Control", "")
                    if cc_header:
                        parsed_ttl = _parse_max_age(cc_header)
                        if parsed_ttl is not None:
                            ttl = parsed_ttl
                    return parsed, content_hash, ttl

        return None, "", 0


# ---------------------------------------------------------------------------
# Module-level shared client + convenience API
# ---------------------------------------------------------------------------

_default_client: OpenTermsClient = OpenTermsClient()


def configure(
    default_ttl: int = _DEFAULT_TTL,
    timeout: int = _DEFAULT_TIMEOUT,
    user_agent: str = _DEFAULT_USER_AGENT,
    registry_url: Optional[str] = _DEFAULT_REGISTRY_URL,
) -> None:
    """
    Reconfigure the module-level shared client.

    This replaces the existing client and clears the cache.

    Args:
        default_ttl:    Cache TTL in seconds (default 3600). Pass ``0`` to
                        disable caching.
        timeout:        HTTP request timeout in seconds (default 10).
        user_agent:     User-Agent header sent with every request.
        registry_url:   Base URL of the OpenTerms registry to use as a
                        fallback when the domain doesn't host its own file.
                        Set to ``None`` to disable registry fallback.
                        Default: ``"https://openterms.com/registry"``
    """
    global _default_client
    _default_client = OpenTermsClient(
        default_ttl=default_ttl,
        timeout=timeout,
        user_agent=user_agent,
        registry_url=registry_url,
    )


def fetch(domain: str) -> Optional[Dict[str, Any]]:
    """
    Fetch the parsed openterms.json for *domain*.

    Tries ``/.well-known/openterms.json`` first, falls back to
    ``/openterms.json``. Returns ``None`` if neither URL responds with
    a valid JSON document.

    Results are cached in memory (default TTL: 3600s). Cache-Control
    response headers override the default TTL when present.

    Args:
        domain: Hostname to query (e.g. ``"github.com"``).
                Schemes and paths are stripped automatically.

    Returns:
        Parsed openterms.json as a dict, or ``None`` if unavailable.

    Example::

        terms = openterms.fetch("github.com")
        if terms:
            print(terms["service"])
    """
    return _default_client.fetch(domain)


def check(domain: str, action: str) -> CheckResult:
    """
    Check whether *action* is permitted by *domain*'s openterms.json.

    Args:
        domain: Hostname to query.
        action: Permission key to look up (e.g. ``"scrape_data"``).

    Returns:
        A :class:`CheckResult`. Evaluates to ``True`` in boolean context
        when the decision is ``"allow"``.

    Example::

        result = openterms.check("github.com", "api_access")
        if result:
            # proceed
            ...
        else:
            print(f"Blocked: {result.decision}")
    """
    return _default_client.check(domain, action)


def discover(domain: str) -> Optional[DiscoveryResult]:
    """
    Return the ``discovery`` block from *domain*'s openterms.json.

    Args:
        domain: Hostname to query.

    Returns:
        A :class:`DiscoveryResult` with ``mcp_servers`` and ``api_specs``
        lists, or ``None`` if the file is unreachable or has no discovery
        block.

    Example::

        disc = openterms.discover("acme-corp.com")
        if disc:
            for server in disc.mcp_servers:
                print(server.url, server.transport)
    """
    return _default_client.discover(domain)


def receipt(
    domain: str,
    action: str,
    decision: Literal["allow", "deny", "not_specified"],
) -> Receipt:
    """
    Generate a minimal ORS compliance receipt.

    The receipt is a local artifact — nothing is sent to any server.
    Persist or log it however your application requires.

    Args:
        domain:   Hostname that was queried.
        action:   Permission key that was checked.
        decision: The allow/deny/not_specified result to record.

    Returns:
        A :class:`Receipt` dict-serialisable via ``.to_dict()``.

    Example::

        result = openterms.check("github.com", "api_access")
        rec = openterms.receipt("github.com", "api_access", result.decision)
        print(rec.to_dict())
    """
    return _default_client.receipt(domain, action, decision)


def clear_cache(domain: Optional[str] = None) -> None:
    """
    Clear cached openterms.json entries.

    Args:
        domain: If provided, evict only that domain. Otherwise flush all.
    """
    _default_client.clear_cache(domain)
