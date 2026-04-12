"""
Data models for openterms-py.
All models are plain dataclasses with type hints — no external dependencies.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional


@dataclass
class CacheEntry:
    """A cached openterms.json payload with TTL metadata."""

    data: Dict[str, Any]
    """Parsed openterms.json content."""

    fetched_at: float = field(default_factory=time.time)
    """Unix timestamp when the entry was fetched."""

    ttl: int = 3600
    """Time-to-live in seconds. 0 means never expires."""

    content_hash: str = ""
    """SHA-256 hex digest of the raw JSON content at fetch time."""

    def is_expired(self) -> bool:
        """Return True if this cache entry has passed its TTL."""
        if self.ttl <= 0:
            return False
        return (time.time() - self.fetched_at) > self.ttl


@dataclass
class CheckResult:
    """Result of a permission check against an openterms.json file."""

    domain: str
    """The domain that was queried."""

    action: str
    """The permission key that was checked (e.g. 'scrape_data')."""

    decision: Literal["allow", "deny", "not_specified"]
    """
    - 'allow'         — permission is explicitly granted
    - 'deny'          — permission is explicitly denied
    - 'not_specified' — the key is absent or the file was unreachable
    """

    raw_value: Optional[Any] = None
    """
    The raw value from the permissions block, if present.
    Useful when the value is a conditional object rather than a plain bool.
    """

    source: Literal["cache", "network"] = "network"
    """Whether the result was served from local cache or a fresh fetch."""

    def __bool__(self) -> bool:
        """True when the decision is 'allow'."""
        return self.decision == "allow"


@dataclass
class McpServer:
    """An MCP server entry from the discovery block."""

    url: str
    transport: str
    description: Optional[str] = None


@dataclass
class ApiSpec:
    """An API spec entry from the discovery block."""

    url: str
    type: str
    description: Optional[str] = None


@dataclass
class DiscoveryResult:
    """Contents of the ``discovery`` block in an openterms.json file."""

    mcp_servers: List[McpServer] = field(default_factory=list)
    api_specs: List[ApiSpec] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DiscoveryResult":
        mcp_servers = [
            McpServer(
                url=s["url"],
                transport=s["transport"],
                description=s.get("description"),
            )
            for s in data.get("mcp_servers", [])
        ]
        api_specs = [
            ApiSpec(
                url=s["url"],
                type=s["type"],
                description=s.get("description"),
            )
            for s in data.get("api_specs", [])
        ]
        return cls(mcp_servers=mcp_servers, api_specs=api_specs)


@dataclass
class Receipt:
    """
    A minimal ORS compliance receipt.

    This is a local artifact only — no server storage, no signing keys.
    The consuming application can log or persist it however it chooses.
    """

    domain: str
    """The domain whose openterms.json was checked."""

    action: str
    """The permission key that was checked."""

    decision: Literal["allow", "deny", "not_specified"]
    """The allow/deny/not_specified decision recorded."""

    timestamp: str
    """ISO 8601 UTC timestamp of when the check occurred."""

    openterms_hash: str
    """
    SHA-256 hex digest of the openterms.json content at time of check.
    Empty string if the file was unreachable.
    """

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the receipt to a plain dict for logging or storage."""
        return {
            "domain": self.domain,
            "action": self.action,
            "decision": self.decision,
            "timestamp": self.timestamp,
            "openterms_hash": self.openterms_hash,
        }
