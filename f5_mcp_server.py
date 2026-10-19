"""
F5 BIG-IP MCP server for Claude.

Connects Claude (Claude Desktop / Claude Code) to an F5 BIG-IP over
iControl REST so you can ask questions in natural language:
  "Which pool members are down?"
  "Show me all virtual servers and their status"
  "What version is the BIG-IP running?"

Read-only by design: every tool only issues HTTP GET requests.
"""

import functools
import json
import os
import sys
import time
import logging

import requests
import urllib3
from dotenv import load_dotenv
try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # MCP Python SDK 1.x
    from mcp.server.fastmcp import FastMCP

load_dotenv()

# MCP over stdio uses stdout for protocol messages -> log to stderr only.
logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("f5-mcp")

F5_HOST = os.getenv("F5_HOST", "")
F5_USER = os.getenv("F5_USER", "")
F5_PASSWORD = os.getenv("F5_PASSWORD", "")
F5_PORT = os.getenv("F5_PORT", "443")
F5_VERIFY_SSL = os.getenv("F5_VERIFY_SSL", "false").lower() == "true"
F5_TIMEOUT = int(os.getenv("F5_TIMEOUT", "15"))

if not F5_VERIFY_SSL:
    # Lab BIG-IPs use the self-signed mgmt cert.
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = f"https://{F5_HOST}:{F5_PORT}"

mcp = FastMCP("f5-bigip")


# ---------------------------------------------------------------------------
# iControl REST client (token auth)
# ---------------------------------------------------------------------------
class F5Client:
    """Minimal iControl REST client using X-F5-Auth-Token."""

    def __init__(self):
        self.session = requests.Session()
        self.session.verify = F5_VERIFY_SSL
        self.session.headers.update({"Content-Type": "application/json"})
        self.token_expiry = 0.0

    def _login(self):
        if not (F5_HOST and F5_USER and F5_PASSWORD):
            raise RuntimeError("F5_HOST, F5_USER and F5_PASSWORD must be set "
                               "(see .env.example)")
        r = self.session.post(
            f"{BASE_URL}/mgmt/shared/authn/login",
            json={"username": F5_USER, "password": F5_PASSWORD,
                  "loginProviderName": "tmos"},
            timeout=F5_TIMEOUT,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Login failed: HTTP {r.status_code} {r.text[:200]}")
        token = r.json()["token"]
        self.session.headers["X-F5-Auth-Token"] = token["token"]
        # Default token lifetime is 1200 s; refresh a minute early.
        self.token_expiry = time.time() + int(token.get("timeout", 1200)) - 60
        log.info("Authenticated to %s as %s", F5_HOST, F5_USER)

    def get(self, path: str) -> dict:
        if time.time() >= self.token_expiry:
            self._login()
        r = self.session.get(f"{BASE_URL}{path}", timeout=F5_TIMEOUT)
        if r.status_code == 401:  # token revoked / expired early
            self._login()
            r = self.session.get(f"{BASE_URL}{path}", timeout=F5_TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"GET {path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r.json()


f5 = F5Client()


def _stat(entries: dict, key: str):
    """Read a value out of an iControl REST nestedStats block."""
    v = entries.get(key, {})
    return v.get("description", v.get("value"))


def _stats_by_name(stats_json: dict) -> dict:
    """Map object full path -> nestedStats entries from a /stats response."""
    out = {}
    for item in stats_json.get("entries", {}).values():
        e = item.get("nestedStats", {}).get("entries", {})
        name = _stat(e, "tmName")
        if name:
            out[name] = e
    return out


def _safe(fn):
    """Return errors to Claude as text instead of crashing the server."""
    @functools.wraps(fn)  # keeps signature so MCP builds the right schema
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            log.exception("Tool %s failed", fn.__name__)
            return f"ERROR: {exc}"
    return wrapper


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@mcp.tool()
@_safe
def f5_system_info() -> str:
    """Get BIG-IP hostname, TMOS version/build and HA failover state."""
    ver = f5.get("/mgmt/tm/sys/version")
    e = next(iter(ver["entries"].values()))["nestedStats"]["entries"]
    host = f5.get("/mgmt/tm/sys/global-settings").get("hostname", "?")
    fo = f5.get("/mgmt/tm/cm/failover-status")
    fo_e = next(iter(fo["entries"].values()))["nestedStats"]["entries"]
    return (f"Hostname : {host}\n"
            f"Product  : {_stat(e, 'Product')}\n"
            f"Version  : {_stat(e, 'Version')}  Build {_stat(e, 'Build')}  "
            f"Edition {_stat(e, 'Edition')}\n"
            f"Failover : {_stat(fo_e, 'status')}")


@mcp.tool()
@_safe
def f5_list_virtual_servers() -> str:
    """List all LTM virtual servers with destination, pool and availability."""
    vs = f5.get("/mgmt/tm/ltm/virtual").get("items", [])
    stats = _stats_by_name(f5.get("/mgmt/tm/ltm/virtual/stats"))
    if not vs:
        return "No virtual servers configured."
    lines = []
    for v in vs:
        s = stats.get(v["fullPath"], {})
        lines.append(
            f"{v['fullPath']}  dest={v.get('destination', '-')}  "
            f"pool={v.get('pool', 'none')}  "
            f"state={_stat(s, 'status.availabilityState') or '?'}/"
            f"{_stat(s, 'status.enabledState') or '?'}  "
            f"reason={_stat(s, 'status.statusReason') or '-'}  "
            f"cur_conns={_stat(s, 'clientside.curConns') or 0}"
        )
    return "\n".join(lines)


@mcp.tool()
@_safe
def f5_pool_status(pool: str = "") -> str:
    """Show pools with monitor and member status (up/down/disabled).

    Args:
        pool: optional pool full path, e.g. /Common/web_pool. Empty = all pools.
    """
    path = "/mgmt/tm/ltm/pool?expandSubcollections=true"
    if pool:
        path = f"/mgmt/tm/ltm/pool/{pool.replace('/', '~')}?expandSubcollections=true"
    data = f5.get(path)
    pools = data.get("items", [data] if pool else [])
    if not pools:
        return "No pools found."
    out = []
    for p in pools:
        members = p.get("membersReference", {}).get("items", [])
        up = sum(1 for m in members if m.get("state") == "up")
        out.append(f"{p['fullPath']}  lb={p.get('loadBalancingMode')}  "
                   f"monitor={p.get('monitor', 'none').strip()}  "
                   f"members up {up}/{len(members)}")
        for m in members:
            out.append(f"    {m['name']:<28} addr={m.get('address')}  "
                       f"state={m.get('state')}  session={m.get('session')}")
    return "\n".join(out)


@mcp.tool()
@_safe
def f5_rest_get(path: str) -> str:
    """Run any read-only iControl REST GET under /mgmt/tm/ and return raw JSON.

    Use when no specific tool fits. Examples:
      /mgmt/tm/ltm/monitor/http
      /mgmt/tm/sys/provision
      /mgmt/tm/net/self
      /mgmt/tm/ltm/profile/client-ssl/~Common~clientssl
    """
    if not path.startswith("/mgmt/tm/"):
        return "ERROR: only paths under /mgmt/tm/ are allowed."
    if "/util/" in path:
        return "ERROR: /mgmt/tm/util/* endpoints are not allowed (read-only server)."
    text = json.dumps(f5.get(path), indent=1)
    return text[:20000] + ("\n...[truncated]" if len(text) > 20000 else "")


if __name__ == "__main__":
    log.info("Starting F5 MCP server for %s", F5_HOST or "<F5_HOST not set>")
    mcp.run()  # stdio transport (what Claude Desktop / Claude Code use)
