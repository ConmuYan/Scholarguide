"""Minimal Lune MCP (streamable HTTP) client for FrontierLitBench data prep.

Usage:
  from lune_client import LuneClient
  c = LuneClient()
  print(c.list_conferences())
"""
import json, os, urllib.request

MCP_URL = "https://mcp.luneresearch.com/mcp"


def _load_key():
    here = os.path.dirname(os.path.abspath(__file__))
    kv = {}
    with open(os.path.join(here, "keys.local.env"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip()
    return kv["LUNE_API_KEY"]


class LuneClient:
    def __init__(self):
        self.key = _load_key()
        self._id = 0

    def _rpc(self, method, params=None):
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}).encode()
        req = urllib.request.Request(
            MCP_URL, data=body, method="POST",
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "User-Agent": "frontierlitbench-prep/1.0 (MCP client)",
            })
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", "replace")
        # parse SSE: take the last "data: {json}" payload
        payload = None
        for line in raw.splitlines():
            if line.startswith("data: "):
                payload = line[6:]
        if payload is None:
            raise RuntimeError(f"No SSE payload in response: {raw[:500]}")
        msg = json.loads(payload)
        if "error" in msg:
            raise RuntimeError(f"MCP error: {msg['error']}")
        return msg.get("result")

    def initialize(self):
        return self._rpc("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "frontierlitbench-prep", "version": "1.0"}})

    def tools(self):
        return self._rpc("tools/list")

    def call(self, tool, **kwargs):
        res = self._rpc("tools/call", {"name": tool, "arguments": kwargs})
        texts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
        out = "\n".join(texts)
        try:
            return json.loads(out)
        except Exception:
            return out

    # ---- convenience wrappers (names per server instructions) ----
    def list_conferences(self):
        return self.call("list_conferences")

    def search_papers(self, query, n_results=25):
        return self.call("search_papers", query=query, n_results=n_results)

    def search_papers_many(self, queries):
        return self.call("search_papers_many", queries=queries)

    def get_conference_papers(self, conference, year, n_results=100):
        return self.call("get_conference_papers", conference=conference, year=year, n_results=n_results)


if __name__ == "__main__":
    import sys
    c = LuneClient()
    c.initialize()
    tools = c.tools()
    print("tools:", [t["name"] for t in tools["tools"]])
    if len(sys.argv) > 1 and sys.argv[1] == "conferences":
        print(json.dumps(c.list_conferences(), ensure_ascii=False, indent=2))
