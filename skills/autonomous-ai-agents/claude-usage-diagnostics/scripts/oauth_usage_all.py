#!/usr/bin/env python3
"""Print 5-hour / weekly utilization for every Claude OAuth login on this Mac.

Covers the Claude Code CLI login (macOS keychain item 'Claude Code-credentials',
which `claude -p` jobs such as OpenEvolve spend) and every Anthropic entry in the
Hermes pool (~/.hermes/auth.json). Never prints tokens.
"""
import json, os, subprocess, time, urllib.error, urllib.request


def usage(tok):
    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={"Authorization": f"Bearer {tok}", "anthropic-beta": "oauth-2025-04-20",
                 "User-Agent": "claude-code/2.1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"http": e.code, "body": e.read()[:200].decode(errors="replace")}


rows = []
try:
    raw = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                         capture_output=True, text=True).stdout
    o = json.loads(raw)["claudeAiOauth"]
    rows.append(("claude-code CLI (keychain)", o.get("expiresAt"), o.get("subscriptionType"), o.get("accessToken")))
except Exception as e:  # no keychain item / not macOS
    print("keychain:", e)
try:
    auth = json.load(open(os.path.expanduser("~/.hermes/auth.json")))
    for i, c in enumerate((auth.get("credential_pool") or {}).get("anthropic", [])):
        rows.append((f"hermes pool #{i + 1} {c.get('label') or c.get('id')}", c.get("expires_at_ms"),
                     c.get("source"), c.get("access_token")))
except FileNotFoundError:
    pass

now = time.time() * 1000
for name, exp, kind, tok in rows:
    mins = None if not exp else round((exp - now) / 60000)
    if not tok:
        print(f"{name} | plan {kind} | no access token stored")
        continue
    if mins is not None and mins <= 0:
        print(f"{name} | plan {kind} | token EXPIRED (refresh before reading errors)")
        continue
    u = usage(tok)
    keep = {k: ({kk: v.get(kk) for kk in ("utilization", "resets_at")} if isinstance(v, dict) else v)
            for k, v in u.items() if v is not None and (k.startswith("five_hour") or k.startswith("seven_day") or k in ("http", "body"))}
    print(f"{name} | plan {kind} | token mins left {mins} | {json.dumps(keep)}")
