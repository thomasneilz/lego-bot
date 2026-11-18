#!/usr/bin/env python3
"""Test ponctuel : le serveur MCP public de Brickfact répond-il depuis GitHub, et quels outils propose-t-il ?"""
import json
import sys
import urllib.request

URL = "https://mcp.brickfact.com/mcp"
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
           "User-Agent": "lego-bot/1.0 (+https://github.com/thomasneilz/lego-bot)"}
session = {}


def rpc(method, params=None, id_=1):
    body = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        body["id"] = id_
    if params is not None:
        body["params"] = params
    h = dict(HEADERS)
    if "sid" in session:
        h["Mcp-Session-Id"] = session["sid"]
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers=h)
    with urllib.request.urlopen(req, timeout=30) as r:
        sid = r.headers.get("Mcp-Session-Id")
        if sid:
            session["sid"] = sid
        raw = r.read().decode("utf-8", "replace")
    if id_ is None:
        return None
    # Réponse JSON directe ou flux SSE ("data: {...}")
    for line in raw.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
    return json.loads(raw) if raw.strip() else None


try:
    init = rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                              "clientInfo": {"name": "lego-bot", "version": "1.0"}})
    print("INIT:", json.dumps(init)[:500])
    rpc("notifications/initialized", id_=None)
    tools = rpc("tools/list", {}, id_=2)
    for t in tools["result"]["tools"]:
        print("\nTOOL:", t["name"])
        print("  desc:", (t.get("description") or "")[:300].replace("\n", " "))
        print("  args:", json.dumps(t.get("inputSchema", {}).get("properties", {}))[:600])
    # Essai concret sur le casque Kylo Ren, si un outil d'offres existe
    names = [t["name"] for t in tools["result"]["tools"]]
    print("\nNOMS:", names)
    st = [t for t in tools["result"]["tools"] if t["name"] == "search_sets"][0]
    print("\nSEARCH_ARGS:", json.dumps(st.get("inputSchema", {}).get("properties", {}))[600:2500])
    calls = [
        ("get_set_offers", {"market": "GB", "set_id": 75415, "price_basis": "item"}),
        ("get_set_offers", {"market": "FR", "set_id": 75415, "price_basis": "item"}),
        ("get_price_history", {"market": "FR", "set_id": 75415, "range": "1Y"}),
        ("assess_deal", {"market": "GB", "set_id": 76452}),
    ]
    for i, (name, args) in enumerate(calls, start=10):
        res = rpc("tools/call", {"name": name, "arguments": args}, id_=i)
        print(f"\n===== {name} {args}")
        r = res.get("result", res)
        if isinstance(r, dict) and "structuredContent" in r:
            print("STRUCT:", json.dumps(r["structuredContent"])[:2500])
        for c in (r.get("content") or [])[:2] if isinstance(r, dict) else []:
            print("TEXT:", str(c.get("text", ""))[:1500])
except Exception as e:
    print("ERREUR:", repr(e))
    sys.exit(1)
