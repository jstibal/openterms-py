# openterms-py

Python SDK for the [OpenTerms Protocol](https://openterms.com).

Query machine-readable AI agent permissions from `openterms.json` files before
your agent acts on a domain.

```
pip install openterms-py
```

---

## Core API

```python
import openterms

# Fetch the full openterms.json (cached in memory, TTL 1h by default)
terms = openterms.fetch("github.com")

# Check a single permission
result = openterms.check("github.com", "api_access")
# result.decision → "allow" | "deny" | "not_specified"
# bool(result) → True when decision is "allow"

# Get the discovery block (MCP servers, OpenAPI specs)
disc = openterms.discover("github.com")

# Generate a local compliance receipt
rec = openterms.receipt("github.com", "api_access", result.decision)
print(rec.to_dict())
```

---

## Installation

Requires Python 3.9+ and [`requests`](https://pypi.org/project/requests/)
(installed automatically).

```bash
pip install openterms-py
```

Optional async support via [`httpx`](https://pypi.org/project/httpx/):

```bash
pip install "openterms-py[async]"
```

---

## Functions

### `fetch(domain) → dict | None`

Fetches `/.well-known/openterms.json` from the domain, falling back to
`/openterms.json`. Returns the parsed JSON dict or `None` if unreachable.

Results are cached in memory. The TTL is taken from the server's
`Cache-Control: max-age=N` header, or the configured default (3600s).

```python
terms = openterms.fetch("stripe.com")
if terms:
    print(terms.get("service"))
    print(terms.get("permissions"))
```

---

### `check(domain, action) → CheckResult`

Returns allow/deny for a single permission key. Evaluates to `True` in
boolean context when the decision is `"allow"`.

```python
result = openterms.check("stripe.com", "api_access")

if result:
    print("Access allowed")
else:
    print(f"Blocked: {result.decision}")  # "deny" or "not_specified"

# Access all fields
print(result.domain)     # "stripe.com"
print(result.action)     # "api_access"
print(result.decision)   # "allow" | "deny" | "not_specified"
print(result.raw_value)  # the raw value from permissions block
print(result.source)     # "cache" | "network"
```

Common permission keys: `scrape_data`, `api_access`, `read_content`,
`index_content`, `train_on_content`, `execute_code`, `access_user_data`.

---

### `discover(domain) → DiscoveryResult | None`

Returns the `discovery` block from the domain's `openterms.json`, or
`None` if absent.

```python
disc = openterms.discover("acme-corp.com")
if disc:
    for server in disc.mcp_servers:
        print(server.url, server.transport, server.description)
    for spec in disc.api_specs:
        print(spec.url, spec.type)
```

`DiscoveryResult` fields:
- `mcp_servers` — list of `McpServer(url, transport, description)`
- `api_specs` — list of `ApiSpec(url, type, description)`

---

### `receipt(domain, action, decision) → Receipt`

Generates a minimal ORS compliance receipt. Local artifact only — nothing
is sent to any server.

```python
result = openterms.check("github.com", "scrape_data")
rec = openterms.receipt("github.com", "scrape_data", result.decision)

print(rec.to_dict())
# {
#   "domain": "github.com",
#   "action": "scrape_data",
#   "decision": "deny",
#   "timestamp": "2026-04-11T10:40:00Z",
#   "openterms_hash": "a3f2...c91d"
# }

# Log it, write to a file, store in your DB — your choice
import json
with open("compliance_log.jsonl", "a") as f:
    f.write(json.dumps(rec.to_dict()) + "\n")
```

---

### `configure(default_ttl, timeout, user_agent)`

Adjust the shared client settings. Clears the existing cache.

```python
openterms.configure(
    default_ttl=600,   # 10-minute cache
    timeout=5,         # 5-second HTTP timeout
)
```

### `clear_cache(domain=None)`

Flush cached entries. Pass a domain to evict a single entry, or call with
no args to flush everything.

```python
openterms.clear_cache("github.com")  # evict one domain
openterms.clear_cache()              # flush all
```

---

## Plain Python example

No framework, just a permission gate before an HTTP call.

```python
import requests
import openterms

TARGET_DOMAIN = "data-provider.com"

def fetch_data_if_permitted(url: str) -> dict | None:
    result = openterms.check(TARGET_DOMAIN, "api_access")

    # Record the decision
    rec = openterms.receipt(TARGET_DOMAIN, "api_access", result.decision)
    print("Receipt:", rec.to_dict())

    if not result:
        print(f"api_access is {result.decision} for {TARGET_DOMAIN}. Aborting.")
        return None

    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()


data = fetch_data_if_permitted("https://data-provider.com/api/items")
```

---

## LangChain integration

Gate a web-interaction tool behind an OpenTerms permission check.

### Option 1 — Custom Tool with permission guard

```python
from langchain_core.tools import tool
import openterms

@tool
def fetch_page_content(url: str) -> str:
    """Fetch the text content of a web page.

    Only proceeds if the domain's openterms.json permits scraping.
    """
    from urllib.parse import urlparse
    import requests

    domain = urlparse(url).hostname or url

    result = openterms.check(domain, "scrape_data")

    # Log the compliance receipt
    rec = openterms.receipt(domain, "scrape_data", result.decision)
    print(f"[OpenTerms] receipt: {rec.to_dict()}")

    if not result:
        return (
            f"Cannot fetch {url}: scrape_data is '{result.decision}' "
            f"for {domain} per their openterms.json."
        )

    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.text[:4000]


# Use in an agent
from langchain_anthropic import ChatAnthropic
from langgraph.prebuilt import create_react_agent

llm = ChatAnthropic(model="claude-3-5-sonnet-20241022")
agent = create_react_agent(llm, tools=[fetch_page_content])

result = agent.invoke({
    "messages": [("user", "Summarise the content at https://example.com")]
})
```

### Option 2 — Pre-action callback on any browser tool

Wrap an existing tool class to inject the permission check transparently:

```python
from langchain_core.tools import BaseTool
from langchain_core.callbacks import CallbackManagerForToolRun
from typing import Optional, Type, Any
from pydantic import BaseModel
import openterms


class OpenTermsGuard(BaseTool):
    """Wraps any web tool and gates execution on OpenTerms permission."""

    name: str = "openTerms_guarded_browser"
    description: str = "Fetch a URL, checking OpenTerms permissions first."
    permission: str = "scrape_data"
    wrapped_tool: Any  # the underlying LangChain browser/fetch tool

    class ArgsSchema(BaseModel):
        url: str

    args_schema: Type[BaseModel] = ArgsSchema

    def _run(
        self,
        url: str,
        run_manager: Optional[CallbackManagerForToolRun] = None,
    ) -> str:
        from urllib.parse import urlparse
        domain = urlparse(url).hostname or url
        result = openterms.check(domain, self.permission)

        rec = openterms.receipt(domain, self.permission, result.decision)
        print(f"[OpenTerms] {rec.to_dict()}")

        if not result:
            return (
                f"Blocked by OpenTerms: '{self.permission}' is "
                f"'{result.decision}' for {domain}."
            )
        return self.wrapped_tool.run(url)


# Usage
from langchain_community.tools import BrowserTool  # or any fetch tool
browser = BrowserTool()
guarded = OpenTermsGuard(wrapped_tool=browser)
```

### Option 3 — Discover MCP servers for a domain before connecting

```python
import openterms

def get_mcp_servers_for_domain(domain: str) -> list[dict]:
    disc = openterms.discover(domain)
    if not disc:
        return []
    return [
        {"url": s.url, "transport": s.transport}
        for s in disc.mcp_servers
    ]

servers = get_mcp_servers_for_domain("acme-corp.com")
# [{"url": "https://acme-corp.com/mcp/sse", "transport": "sse"}]
```

---

## CrewAI integration

Gate a CrewAI agent's web tasks behind OpenTerms permission checks.

### Option 1 — Custom tool for CrewAI

```python
from crewai_tools import BaseTool
import openterms
import requests


class OpenTermsWebTool(BaseTool):
    name: str = "web_fetch_with_permissions"
    description: str = (
        "Fetch content from a URL. "
        "Checks the domain's OpenTerms permissions before proceeding. "
        "Returns an error string if the domain denies the requested action."
    )
    permission: str = "scrape_data"

    def _run(self, url: str) -> str:
        from urllib.parse import urlparse
        domain = urlparse(url).hostname or url

        result = openterms.check(domain, self.permission)

        # Store receipt for audit trail
        rec = openterms.receipt(domain, self.permission, result.decision)
        # In production: write rec.to_dict() to your audit log

        if not result:
            return (
                f"OpenTerms check failed for {domain}: "
                f"{self.permission} = {result.decision}. "
                "Do not proceed with this URL."
            )

        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return resp.text[:4000]


# Use in a CrewAI agent
from crewai import Agent, Task, Crew

web_tool = OpenTermsWebTool(permission="scrape_data")

researcher = Agent(
    role="Web Researcher",
    goal="Research topics from web sources that permit scraping.",
    backstory="You respect site permissions and only access allowed content.",
    tools=[web_tool],
    verbose=True,
)

task = Task(
    description="Find and summarise pricing information from competitor websites.",
    expected_output="A bullet-point comparison of competitor pricing.",
    agent=researcher,
)

crew = Crew(agents=[researcher], tasks=[task], verbose=True)
result = crew.kickoff()
```

### Option 2 — Callback hook for CrewAI task lifecycle

Use a `before_kickoff` step to pre-validate all domains a task will touch:

```python
from crewai import Crew, Agent, Task, Process
from typing import Union
import openterms


def check_domain_permissions(
    domains: list[str],
    action: str,
) -> dict[str, str]:
    """
    Returns {domain: decision} for all domains.
    Raises ValueError if any domain explicitly denies the action.
    """
    results = {}
    denied = []
    for domain in domains:
        r = openterms.check(domain, action)
        results[domain] = r.decision
        if r.decision == "deny":
            denied.append(domain)
    if denied:
        raise ValueError(
            f"OpenTerms: {action!r} denied for: {', '.join(denied)}"
        )
    return results


# Before running your Crew, validate the target domains
target_domains = ["competitor-a.com", "competitor-b.com"]

try:
    permissions = check_domain_permissions(target_domains, "scrape_data")
    print(f"All domains permitted: {permissions}")
    # safe to proceed
    # crew.kickoff(...)
except ValueError as e:
    print(f"Aborting: {e}")
```

### Option 3 — API discovery for CrewAI MCP tool selection

```python
import openterms
from crewai import Agent

def build_agent_for_domain(domain: str) -> Agent:
    disc = openterms.discover(domain)

    tools = []
    if disc and disc.api_specs:
        # Dynamically load tools from discovered OpenAPI specs
        for spec in disc.api_specs:
            print(f"Found API spec: {spec.url} ({spec.type})")
            # Load spec and generate tools here (e.g. via openapi-core)

    return Agent(
        role="Domain Specialist",
        goal=f"Interact with {domain} using its declared API.",
        backstory=f"You have been given the API specs for {domain}.",
        tools=tools,
    )
```

---

## Models reference

```python
# CheckResult
result.domain      # str
result.action      # str
result.decision    # "allow" | "deny" | "not_specified"
result.raw_value   # Any — the raw permissions value (bool, dict, None)
result.source      # "cache" | "network"
bool(result)       # True iff decision == "allow"

# DiscoveryResult
disc.mcp_servers   # list[McpServer]
disc.api_specs     # list[ApiSpec]

# McpServer
server.url          # str
server.transport    # str  ("sse" | "stdio" | "streamable-http")
server.description  # str | None

# ApiSpec
spec.url            # str
spec.type           # str  ("openapi_3" | "swagger_2" | "graphql_schema")
spec.description    # str | None

# Receipt
rec.domain          # str
rec.action          # str
rec.decision        # "allow" | "deny" | "not_specified"
rec.timestamp       # str  (ISO 8601 UTC)
rec.openterms_hash  # str  (SHA-256 hex, empty if domain was unreachable)
rec.to_dict()       # → dict
```

---

## Advanced configuration

```python
import openterms

# Shorter cache, stricter timeout
openterms.configure(default_ttl=300, timeout=5)

# Per-request: bypass cache by clearing first
openterms.clear_cache("github.com")
result = openterms.check("github.com", "api_access")

# Use your own client instance (e.g. for testing with a mock cache)
from openterms.client import OpenTermsClient
from openterms.cache import TermsCache

custom_cache = TermsCache()
client = OpenTermsClient(default_ttl=0, cache=custom_cache)
result = client.check("github.com", "api_access")
```

---

## License

MIT
