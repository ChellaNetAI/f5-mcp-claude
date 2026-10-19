"""
F5 BIG-IP MCP server for Claude.

Connects Claude (Claude Desktop / Claude Code) to an F5 BIG-IP over
iControl REST so you can ask questions in natural language:
  "Which pool members are down?"
  "Show me all virtual servers and their status"
  "What version is the BIG-IP running?"

Read-only by design: every tool only issues HTTP GET requests, and every
tool is annotated readOnlyHint=True / destructiveHint=False.
"""

import json
import logging
import os
import ssl
import sys
import time
from pathlib import Path
from typing import Annotated
from urllib.parse import unquote

import httpx
from dotenv import load_dotenv
from mcp.types import ToolAnnotations
from pydantic import Field

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # MCP Python SDK 1.x
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError

# Always read the .env that sits next to this file, whatever the working directory.
load_dotenv(Path(__file__).resolve().with_name(".env"))

# stdio transport uses stdout for MCP messages -> log to stderr only.
logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("f5_mcp")

F5_HOST = os.getenv("F5_HOST", "")
F5_USER = os.getenv("F5_USER", "")
F5_PASSWORD = os.getenv("F5_PASSWORD", "")
F5_PORT = int(os.getenv("F5_PORT", "443"))
F5_TIMEOUT = float(os.getenv("F5_TIMEOUT", "15"))
# TLS: F5_CA_BUNDLE (path to the mgmt cert/CA) wins; otherwise F5_VERIFY_SSL.
F5_CA_BUNDLE = os.getenv("F5_CA_BUNDLE", "")
F5_VERIFY: bool | ssl.SSLContext = (
    ssl.create_default_context(cafile=F5_CA_BUNDLE) if F5_CA_BUNDLE
    else os.getenv("F5_VERIFY_SSL", "false").lower() == "true")

BASE_URL = f"https://{F5_HOST}:{F5_PORT}"
MAX_CHARS = 20_000  # cap on raw JSON returned to Claude

mcp = FastMCP("f5_mcp")

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                            idempotentHint=True, openWorldHint=False)


# ---------------------------------------------------------------------------
# iControl REST client (token auth)
# ---------------------------------------------------------------------------
class F5Client:
    """Minimal async iControl REST client using X-F5-Auth-Token."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._token_expiry = 0.0

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=BASE_URL, verify=F5_VERIFY, timeout=F5_TIMEOUT,
                headers={"Content-Type": "application/json"})
        return self._client

    async def _login(self) -> None:
        if not (F5_HOST and F5_USER and F5_PASSWORD):
            raise ToolError("F5_HOST, F5_USER and F5_PASSWORD are not set. "
                            "Add them to .env or the Claude config 'env' block.")
        r = await self._http().post("/mgmt/shared/authn/login", json={
            "username": F5_USER, "password": F5_PASSWORD,
            "loginProviderName": "tmos"})
        if r.status_code in (401, 403):
            raise ToolError(f"BIG-IP rejected login for '{F5_USER}' (HTTP {r.status_code}). "
                            "Check the password, and that the user's role is allowed to use "
                            "iControl REST (see README > Troubleshooting).")
        r.raise_for_status()
        token = r.json()["token"]
        self._http().headers["X-F5-Auth-Token"] = token["token"]
        # Default token lifetime is 1200 s; refresh a minute early.
        self._token_expiry = time.time() + int(token.get("timeout", 1200)) - 60
        log.info("Authenticated to %s as %s", F5_HOST, F5_USER)

    async def get(self, path: str) -> dict:
        try:
            if time.time() >= self._token_expiry:
                await self._login()
            r = await self._http().get(path)
            if r.status_code == 401:  # token revoked or expired early
                await self._login()
                r = await self._http().get(path)
        except ToolError:
            raise
        except httpx.ConnectError as exc:
            raise ToolError(f"Cannot reach BIG-IP at {BASE_URL}: {exc}. Check the mgmt IP, "
                            "routing, and 'tmsh list sys httpd allow'.") from exc
        except httpx.TimeoutException as exc:
            raise ToolError(f"BIG-IP at {BASE_URL} did not answer within {F5_TIMEOUT}s. "
                            "Raise F5_TIMEOUT or check restjavad load.") from exc
        except httpx.HTTPError as exc:
            raise ToolError(f"HTTP error talking to BIG-IP: {exc}") from exc

        if r.status_code == 404:
            raise ToolError(f"Not found: {path}. Check the object name and partition, "
                            "e.g. /Common/web_pool.")
        if r.status_code == 403:
            raise ToolError(f"User '{F5_USER}' is not allowed to read {path} (HTTP 403). "
                            "Give the user a role that can read this object, e.g. Auditor.")
        if r.status_code != 200:
            raise ToolError(f"GET {path} returned HTTP {r.status_code}: {r.text[:300]}")
        return r.json()


f5 = F5Client()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _stat(entries: dict, key: str):
    """Read one value out of an iControl REST nestedStats block."""
    v = entries.get(key, {})
    return v.get("description", v.get("value"))


def _first_entries(stats_json: dict) -> dict:
    """nestedStats entries of the first (often only) object in a /stats reply."""
    item = next(iter(stats_json.get("entries", {}).values()), {})
    return item.get("nestedStats", {}).get("entries", {})


def _stats_by_name(stats_json: dict) -> dict:
    """Map object full path -> nestedStats entries from a /stats response."""
    out = {}
    for item in stats_json.get("entries", {}).values():
        e = item.get("nestedStats", {}).get("entries", {})
        name = _stat(e, "tmName")
        if name:
            out[name] = e
    return out


def _page(items: list, name_filter: str, limit: int, offset: int) -> tuple[list, str]:
    """Filter by substring of fullPath, then apply limit/offset. Returns (page, footer)."""
    if name_filter:
        items = [i for i in items if name_filter.lower() in i.get("fullPath", "").lower()]
    total = len(items)
    page = items[offset:offset + limit]
    footer = f"\n-- showing {len(page)} of {total} (offset {offset})"
    if offset + limit < total:
        footer += f"; more available, call again with offset={offset + limit}"
    return page, footer


# Shared parameter types
NameFilter = Annotated[str, Field(
    description="Only include objects whose full path contains this text "
                "(case-insensitive), e.g. 'web' or '/Common/app1'. Empty = all.",
    max_length=200)]
Limit = Annotated[int, Field(description="Maximum objects to return.", ge=1, le=200)]
Offset = Annotated[int, Field(description="Number of objects to skip, for paging.", ge=0)]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@mcp.tool(title="BIG-IP system info", annotations=READ_ONLY)
async def f5_get_system_info() -> str:
    """Get the BIG-IP hostname, TMOS version/build/edition and HA failover state
    (ACTIVE / STANDBY / OFFLINE). Use this first to confirm which device you are on."""
    ver = _first_entries(await f5.get("/mgmt/tm/sys/version"))
    host = (await f5.get("/mgmt/tm/sys/global-settings")).get("hostname", "?")
    fo = _first_entries(await f5.get("/mgmt/tm/cm/failover-status"))
    return (f"Hostname : {host}\n"
            f"Product  : {_stat(ver, 'Product')}\n"
            f"Version  : {_stat(ver, 'Version')}  Build {_stat(ver, 'Build')}  "
            f"Edition {_stat(ver, 'Edition')}\n"
            f"Failover : {_stat(fo, 'status')}")


@mcp.tool(title="List virtual servers", annotations=READ_ONLY)
async def f5_list_virtual_servers(name_filter: NameFilter = "",
                                  limit: Limit = 50, offset: Offset = 0) -> str:
    """List LTM virtual servers with destination, pool, availability
    (available/offline/unknown), enabled state, status reason and current
    client-side connections. Supports name_filter and limit/offset paging."""
    vs = (await f5.get("/mgmt/tm/ltm/virtual")).get("items", [])
    if not vs:
        return "No virtual servers configured."
    stats = _stats_by_name(await f5.get("/mgmt/tm/ltm/virtual/stats"))
    page, footer = _page(vs, name_filter, limit, offset)
    lines = []
    for v in page:
        s = stats.get(v["fullPath"], {})
        lines.append(
            f"{v['fullPath']}  dest={v.get('destination', '-')}  "
            f"pool={v.get('pool', 'none')}  "
            f"state={_stat(s, 'status.availabilityState') or '?'}/"
            f"{_stat(s, 'status.enabledState') or '?'}  "
            f"reason={_stat(s, 'status.statusReason') or '-'}  "
            f"cur_conns={_stat(s, 'clientside.curConns') or 0}")
    return "\n".join(lines) + footer


@mcp.tool(title="Pool and member status", annotations=READ_ONLY)
async def f5_get_pool_status(
    pool: Annotated[str, Field(
        description="Exact pool full path, e.g. /Common/web_pool. Empty = all pools.",
        pattern=r"^$|^/[\w.\-]+(/[\w.\-]+)*$", max_length=255)] = "",
    name_filter: NameFilter = "",
    limit: Limit = 50, offset: Offset = 0,
) -> str:
    """Show pools with LB method, monitor, 'members up x/y', and each member's
    address, monitor state (up/down/unchecked/user-down) and session
    (monitor-enabled/user-disabled). Use to answer 'which members are down'."""
    if pool:
        data = await f5.get(f"/mgmt/tm/ltm/pool/{pool.replace('/', '~')}"
                            "?expandSubcollections=true")
        pools, footer = [data], ""
    else:
        data = await f5.get("/mgmt/tm/ltm/pool?expandSubcollections=true")
        pools, footer = _page(data.get("items", []), name_filter, limit, offset)
    if not pools:
        return "No pools found."
    out = []
    for p in pools:
        members = p.get("membersReference", {}).get("items", [])
        up = sum(1 for m in members if m.get("state") == "up")
        out.append(f"{p['fullPath']}  lb={p.get('loadBalancingMode')}  "
                   f"monitor={(p.get('monitor') or 'none').strip()}  "
                   f"members up {up}/{len(members)}")
        for m in members:
            out.append(f"    {m['name']:<28} addr={m.get('address')}  "
                       f"state={m.get('state')}  session={m.get('session')}")
    return "\n".join(out) + footer


@mcp.tool(title="Read-only iControl REST GET", annotations=READ_ONLY)
async def f5_rest_get(
    path: Annotated[str, Field(
        description="iControl REST path under /mgmt/tm/, e.g. /mgmt/tm/ltm/monitor/http, "
                    "/mgmt/tm/sys/provision, /mgmt/tm/net/self, "
                    "/mgmt/tm/ltm/profile/client-ssl/~Common~clientssl. "
                    "Query strings such as ?$select=name,destination are allowed.",
        min_length=9, max_length=500)],
) -> str:
    """Run any read-only iControl REST GET under /mgmt/tm/ and return the raw JSON
    (truncated to 20,000 characters). Use only when no specific tool fits.
    /mgmt/tm/util/* is blocked because those endpoints can run commands."""
    decoded = unquote(path)
    if not decoded.startswith("/mgmt/tm/"):
        raise ToolError("Only paths under /mgmt/tm/ are allowed.")
    if ".." in decoded or "/util" in decoded.lower():
        raise ToolError("Path blocked: '..' and /mgmt/tm/util/* are not allowed "
                        "(read-only server).")
    text = json.dumps(await f5.get(path), indent=1)
    if len(text) > MAX_CHARS:
        text = (text[:MAX_CHARS] + "\n...[truncated - narrow the query, e.g. add "
                "?$select=name,fullPath or request a single object]")
    return text


def main() -> None:
    if not (F5_HOST and F5_USER and F5_PASSWORD):
        log.warning("F5_HOST / F5_USER / F5_PASSWORD not set - tools will return an error "
                    "until they are configured.")
    if not F5_VERIFY:
        log.warning("TLS certificate verification is OFF (lab mode). Set F5_CA_BUNDLE "
                    "or F5_VERIFY_SSL=true for anything beyond a lab.")
    log.info("Starting f5_mcp for %s", F5_HOST or "<F5_HOST not set>")
    mcp.run()  # stdio transport (what Claude Desktop / Claude Code use)


if __name__ == "__main__":
    main()
