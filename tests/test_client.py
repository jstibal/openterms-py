"""
Tests for openterms-py.

Uses the `responses` library to mock HTTP calls — no real network requests.
Install dev deps first:  pip install "openterms-py[dev]"
"""

from __future__ import annotations

import hashlib
import json
import time

import pytest
import responses as resp_mock

import openterms
from openterms.cache import TermsCache
from openterms.client import OpenTermsClient, _normalise_domain, _sha256
from openterms.models import CacheEntry, CheckResult, DiscoveryResult, Receipt

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_TERMS = {
    "openterms_version": "0.2.0",
    "service": "example.com",
    "permissions": {
        "scrape_data": False,
        "api_access": True,
        "read_content": {
            "allowed": True,
            "max_frequency": "100/day",
        },
    },
}

TERMS_WITH_DISCOVERY = {
    **MINIMAL_TERMS,
    "discovery": {
        "mcp_servers": [
            {"url": "https://example.com/mcp/sse", "transport": "sse"},
        ],
        "api_specs": [
            {
                "url": "https://example.com/openapi.json",
                "type": "openapi_3",
                "description": "REST API",
            }
        ],
    },
}

WELL_KNOWN_URL = "https://example.com/.well-known/openterms.json"
ROOT_URL = "https://example.com/openterms.json"


def _json_body(data: dict) -> str:
    return json.dumps(data)


# ---------------------------------------------------------------------------
# Helper: _normalise_domain
# ---------------------------------------------------------------------------


def test_normalise_strips_scheme() -> None:
    assert _normalise_domain("https://example.com/path") == "example.com"


def test_normalise_strips_http() -> None:
    assert _normalise_domain("http://EXAMPLE.COM") == "example.com"


def test_normalise_plain_domain() -> None:
    assert _normalise_domain("  example.COM  ") == "example.com"


# ---------------------------------------------------------------------------
# Helper: _sha256
# ---------------------------------------------------------------------------


def test_sha256_correctness() -> None:
    text = '{"hello": "world"}'
    expected = hashlib.sha256(text.encode()).hexdigest()
    assert _sha256(text) == expected


# ---------------------------------------------------------------------------
# CacheEntry expiry
# ---------------------------------------------------------------------------


def test_cache_entry_not_expired() -> None:
    entry = CacheEntry(data={}, fetched_at=time.time(), ttl=3600)
    assert not entry.is_expired()


def test_cache_entry_expired() -> None:
    entry = CacheEntry(data={}, fetched_at=time.time() - 4000, ttl=3600)
    assert entry.is_expired()


def test_cache_entry_zero_ttl_never_expires() -> None:
    entry = CacheEntry(data={}, fetched_at=time.time() - 99999, ttl=0)
    assert not entry.is_expired()


# ---------------------------------------------------------------------------
# TermsCache
# ---------------------------------------------------------------------------


def test_cache_set_get() -> None:
    cache = TermsCache()
    entry = CacheEntry(data={"x": 1}, ttl=3600)
    cache.set("example.com", entry)
    result = cache.get("example.com")
    assert result is not None
    assert result.data == {"x": 1}


def test_cache_get_missing() -> None:
    cache = TermsCache()
    assert cache.get("missing.com") is None


def test_cache_get_expired() -> None:
    cache = TermsCache()
    entry = CacheEntry(data={"x": 1}, fetched_at=time.time() - 9999, ttl=1)
    cache.set("example.com", entry)
    assert cache.get("example.com") is None  # evicted


def test_cache_delete() -> None:
    cache = TermsCache()
    entry = CacheEntry(data={"x": 1}, ttl=3600)
    cache.set("example.com", entry)
    cache.delete("example.com")
    assert cache.get("example.com") is None


def test_cache_clear() -> None:
    cache = TermsCache()
    cache.set("a.com", CacheEntry(data={}, ttl=3600))
    cache.set("b.com", CacheEntry(data={}, ttl=3600))
    cache.clear()
    assert len(cache) == 0


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


@resp_mock.activate
def test_fetch_well_known_success() -> None:
    body = _json_body(MINIMAL_TERMS)
    resp_mock.add(resp_mock.GET, WELL_KNOWN_URL, body=body, status=200)

    client = OpenTermsClient(cache=TermsCache())
    result = client.fetch("example.com")
    assert result is not None
    assert result["service"] == "example.com"


@resp_mock.activate
def test_fetch_falls_back_to_root() -> None:
    body = _json_body(MINIMAL_TERMS)
    resp_mock.add(resp_mock.GET, WELL_KNOWN_URL, status=404)
    resp_mock.add(resp_mock.GET, ROOT_URL, body=body, status=200)

    client = OpenTermsClient(cache=TermsCache())
    result = client.fetch("example.com")
    assert result is not None


@resp_mock.activate
def test_fetch_returns_none_when_both_fail() -> None:
    resp_mock.add(resp_mock.GET, WELL_KNOWN_URL, status=404)
    resp_mock.add(resp_mock.GET, ROOT_URL, status=404)

    client = OpenTermsClient(cache=TermsCache())
    result = client.fetch("example.com")
    assert result is None


@resp_mock.activate
def test_fetch_returns_none_on_invalid_json() -> None:
    resp_mock.add(resp_mock.GET, WELL_KNOWN_URL, body="not json", status=200)
    resp_mock.add(resp_mock.GET, ROOT_URL, status=404)

    client = OpenTermsClient(cache=TermsCache())
    result = client.fetch("example.com")
    assert result is None


@resp_mock.activate
def test_fetch_uses_cache_on_second_call() -> None:
    body = _json_body(MINIMAL_TERMS)
    resp_mock.add(resp_mock.GET, WELL_KNOWN_URL, body=body, status=200)

    client = OpenTermsClient(cache=TermsCache())
    client.fetch("example.com")
    client.fetch("example.com")  # should hit cache

    assert len(resp_mock.calls) == 1  # only one HTTP call made


@resp_mock.activate
def test_fetch_respects_cache_control_max_age() -> None:
    body = _json_body(MINIMAL_TERMS)
    resp_mock.add(
        resp_mock.GET,
        WELL_KNOWN_URL,
        body=body,
        status=200,
        headers={"Cache-Control": "max-age=60"},
    )

    cache = TermsCache()
    client = OpenTermsClient(cache=cache, default_ttl=3600)
    client.fetch("example.com")
    entry = cache.get("example.com")
    assert entry is not None
    assert entry.ttl == 60


@resp_mock.activate
def test_fetch_cache_control_no_cache_sets_ttl_zero() -> None:
    body = _json_body(MINIMAL_TERMS)
    resp_mock.add(
        resp_mock.GET,
        WELL_KNOWN_URL,
        body=body,
        status=200,
        headers={"Cache-Control": "no-cache"},
    )

    cache = TermsCache()
    client = OpenTermsClient(cache=cache, default_ttl=3600)
    client.fetch("example.com")
    entry = cache.get("example.com")
    # TTL=0 means never expires in our model
    assert entry is not None
    assert entry.ttl == 0


# ---------------------------------------------------------------------------
# check()
# ---------------------------------------------------------------------------


@resp_mock.activate
def test_check_boolean_true_returns_allow() -> None:
    body = _json_body(MINIMAL_TERMS)
    resp_mock.add(resp_mock.GET, WELL_KNOWN_URL, body=body, status=200)

    client = OpenTermsClient(cache=TermsCache())
    result = client.check("example.com", "api_access")
    assert result.decision == "allow"
    assert bool(result) is True


@resp_mock.activate
def test_check_boolean_false_returns_deny() -> None:
    body = _json_body(MINIMAL_TERMS)
    resp_mock.add(resp_mock.GET, WELL_KNOWN_URL, body=body, status=200)

    client = OpenTermsClient(cache=TermsCache())
    result = client.check("example.com", "scrape_data")
    assert result.decision == "deny"
    assert bool(result) is False


@resp_mock.activate
def test_check_conditional_allowed_true_returns_allow() -> None:
    body = _json_body(MINIMAL_TERMS)
    resp_mock.add(resp_mock.GET, WELL_KNOWN_URL, body=body, status=200)

    client = OpenTermsClient(cache=TermsCache())
    result = client.check("example.com", "read_content")
    assert result.decision == "allow"


@resp_mock.activate
def test_check_missing_key_returns_not_specified() -> None:
    body = _json_body(MINIMAL_TERMS)
    resp_mock.add(resp_mock.GET, WELL_KNOWN_URL, body=body, status=200)

    client = OpenTermsClient(cache=TermsCache())
    result = client.check("example.com", "train_on_content")
    assert result.decision == "not_specified"
    assert bool(result) is False


@resp_mock.activate
def test_check_unreachable_domain_returns_not_specified() -> None:
    resp_mock.add(resp_mock.GET, WELL_KNOWN_URL, status=404)
    resp_mock.add(resp_mock.GET, ROOT_URL, status=404)

    client = OpenTermsClient(cache=TermsCache())
    result = client.check("example.com", "api_access")
    assert result.decision == "not_specified"


@resp_mock.activate
def test_check_case_insensitive_key_lookup() -> None:
    body = _json_body(MINIMAL_TERMS)
    resp_mock.add(resp_mock.GET, WELL_KNOWN_URL, body=body, status=200)

    client = OpenTermsClient(cache=TermsCache())
    result = client.check("example.com", "API_ACCESS")
    assert result.decision == "allow"


# ---------------------------------------------------------------------------
# discover()
# ---------------------------------------------------------------------------


@resp_mock.activate
def test_discover_returns_discovery_result() -> None:
    body = _json_body(TERMS_WITH_DISCOVERY)
    resp_mock.add(resp_mock.GET, WELL_KNOWN_URL, body=body, status=200)

    client = OpenTermsClient(cache=TermsCache())
    disc = client.discover("example.com")
    assert disc is not None
    assert len(disc.mcp_servers) == 1
    assert disc.mcp_servers[0].url == "https://example.com/mcp/sse"
    assert disc.mcp_servers[0].transport == "sse"
    assert len(disc.api_specs) == 1
    assert disc.api_specs[0].type == "openapi_3"


@resp_mock.activate
def test_discover_returns_none_when_no_discovery_block() -> None:
    body = _json_body(MINIMAL_TERMS)
    resp_mock.add(resp_mock.GET, WELL_KNOWN_URL, body=body, status=200)

    client = OpenTermsClient(cache=TermsCache())
    disc = client.discover("example.com")
    assert disc is None


@resp_mock.activate
def test_discover_returns_none_when_unreachable() -> None:
    resp_mock.add(resp_mock.GET, WELL_KNOWN_URL, status=404)
    resp_mock.add(resp_mock.GET, ROOT_URL, status=404)

    client = OpenTermsClient(cache=TermsCache())
    disc = client.discover("example.com")
    assert disc is None


# ---------------------------------------------------------------------------
# receipt()
# ---------------------------------------------------------------------------


@resp_mock.activate
def test_receipt_contains_correct_fields() -> None:
    body = _json_body(MINIMAL_TERMS)
    resp_mock.add(resp_mock.GET, WELL_KNOWN_URL, body=body, status=200)

    client = OpenTermsClient(cache=TermsCache())
    client.fetch("example.com")  # populate cache with hash

    rec = client.receipt("example.com", "api_access", "allow")
    assert isinstance(rec, Receipt)
    assert rec.domain == "example.com"
    assert rec.action == "api_access"
    assert rec.decision == "allow"
    assert "T" in rec.timestamp  # ISO format
    assert len(rec.openterms_hash) == 64  # SHA-256 hex


@resp_mock.activate
def test_receipt_hash_matches_content() -> None:
    raw_body = json.dumps(MINIMAL_TERMS)
    resp_mock.add(resp_mock.GET, WELL_KNOWN_URL, body=raw_body, status=200)

    client = OpenTermsClient(cache=TermsCache())
    client.fetch("example.com")

    expected_hash = hashlib.sha256(raw_body.encode()).hexdigest()
    rec = client.receipt("example.com", "api_access", "allow")
    assert rec.openterms_hash == expected_hash


def test_receipt_empty_hash_when_not_cached() -> None:
    client = OpenTermsClient(cache=TermsCache())
    rec = client.receipt("unreachable.com", "api_access", "not_specified")
    assert rec.openterms_hash == ""


def test_receipt_to_dict() -> None:
    client = OpenTermsClient(cache=TermsCache())
    rec = client.receipt("example.com", "scrape_data", "deny")
    d = rec.to_dict()
    assert set(d.keys()) == {
        "domain", "action", "decision", "timestamp", "openterms_hash"
    }
    assert d["decision"] == "deny"


# ---------------------------------------------------------------------------
# clear_cache()
# ---------------------------------------------------------------------------


@resp_mock.activate
def test_clear_cache_single_domain() -> None:
    body = _json_body(MINIMAL_TERMS)
    resp_mock.add(resp_mock.GET, WELL_KNOWN_URL, body=body, status=200)
    resp_mock.add(resp_mock.GET, WELL_KNOWN_URL, body=body, status=200)

    client = OpenTermsClient(cache=TermsCache())
    client.fetch("example.com")
    client.clear_cache("example.com")
    client.fetch("example.com")  # should re-fetch
    assert len(resp_mock.calls) == 2


@resp_mock.activate
def test_clear_cache_all() -> None:
    body = _json_body(MINIMAL_TERMS)
    resp_mock.add(resp_mock.GET, WELL_KNOWN_URL, body=body, status=200)
    resp_mock.add(resp_mock.GET, WELL_KNOWN_URL, body=body, status=200)

    client = OpenTermsClient(cache=TermsCache())
    client.fetch("example.com")
    client.clear_cache()
    client.fetch("example.com")
    assert len(resp_mock.calls) == 2


# ---------------------------------------------------------------------------
# Module-level convenience API
# ---------------------------------------------------------------------------


@resp_mock.activate
def test_module_level_fetch() -> None:
    body = _json_body(MINIMAL_TERMS)
    resp_mock.add(resp_mock.GET, WELL_KNOWN_URL, body=body, status=200)

    openterms.clear_cache()  # reset shared state
    result = openterms.fetch("example.com")
    assert result is not None
    assert result["service"] == "example.com"
    openterms.clear_cache()


@resp_mock.activate
def test_module_level_check() -> None:
    body = _json_body(MINIMAL_TERMS)
    resp_mock.add(resp_mock.GET, WELL_KNOWN_URL, body=body, status=200)

    openterms.clear_cache()
    result = openterms.check("example.com", "api_access")
    assert result.decision == "allow"
    openterms.clear_cache()


@resp_mock.activate
def test_module_level_discover() -> None:
    body = _json_body(TERMS_WITH_DISCOVERY)
    resp_mock.add(resp_mock.GET, WELL_KNOWN_URL, body=body, status=200)

    openterms.clear_cache()
    disc = openterms.discover("example.com")
    assert disc is not None
    assert len(disc.mcp_servers) == 1
    openterms.clear_cache()


def test_module_level_receipt() -> None:
    openterms.clear_cache()
    rec = openterms.receipt("example.com", "api_access", "allow")
    assert rec.domain == "example.com"
    assert rec.decision == "allow"
    openterms.clear_cache()
