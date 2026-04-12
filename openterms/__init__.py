"""
openterms-py — Python SDK for the OpenTerms Protocol.

Provides fetch, check, discover, and receipt functions for querying
machine-readable AI agent permissions from openterms.json files.

Usage:
    import openterms

    terms = openterms.fetch("example.com")
    result = openterms.check("example.com", "scrape_data")
    disc = openterms.discover("example.com")
    rec = openterms.receipt("example.com", "scrape_data", "allow")
"""

from .client import fetch, check, discover, receipt, configure, clear_cache
from .models import CheckResult, DiscoveryResult, Receipt, CacheEntry
from .cache import TermsCache

__version__ = "0.1.0"
__all__ = [
    "fetch",
    "check",
    "discover",
    "receipt",
    "configure",
    "clear_cache",
    "CheckResult",
    "DiscoveryResult",
    "Receipt",
    "CacheEntry",
    "TermsCache",
]
