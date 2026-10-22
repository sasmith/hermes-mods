#!/usr/bin/env python3
"""Audit Hermes skills: size vs the skill_manage cap, lost (never-retried) skill writes, broken/orphan
references, overlaps, stale skills and weak descriptions.

  skill_audit.py --report [--days 7]   full markdown report (weekly audit job)
  skill_audit.py --signal [--days 7]   stable lines only; empty when nothing needs attention (daily monitor)

Stdlib only. Reads $HERMES_HOME (default ~/.hermes): skills/, skills/.usage.json, skills/.bundled_manifest,
state.db, and every dir in config.yaml skills.external_dirs. Writes the arguments of each lost write to
$HERMES_HOME/cache/skill-audit/lost-<tool_call_id>.json so an agent can re-apply the note. After handling one
(re-applied, or judged superseded), append "<tool_call_id> <note>" to $HERMES_HOME/cache/skill-audit/acked.txt.
"""
import argparse, glob, json, os, re, sqlite3, time
from collections import defaultdict

CAP = 100_000            # tools/skill_manager_tool.py MAX_SKILL_CONTENT_CHARS
WARN, CRIT = 0.70, 0.90
HOME = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
SKILLS = os.path.join(HOME, "skills")
OUT = os.path.join(HOME, "cache", "skill-audit")
STOP = set("use when the a an and or to of for in on with via from by is it this that your user's".split())


def external_dirs():
    try:
        txt = open(os.path.join(HOME, "config.yaml")).read()
    except OSError:
        return []
    m = re.search(r"^\s*external_dirs:\s*\n((?:\s+-\s*.+\n)+)", txt, re.M)
    if not m:
        return []
    return [os.path.expanduser(x.strip().strip("'\"")) for x in re.findall(r"-\s*(.+)", m.group(1))]


def load_skills():
    skills = {}
    for root in [SKILLS] + external_dirs():
        for p in glob.glob(os.path.join(root, "**", "SKILL.md"), recursive=True):
            rel = os.path.relpath(p, root)
            if any(part.startswith(".") for part in rel.split(os.sep)):
                continue
            t = open(p, encoding="utf-8", errors="replace").read()
            mn = re.search(r"^name:\s*(.+)$", t, re.M)
            md = re.search(r"^description:\s*(.+)$", t, re.M)
            name = mn.group(1).strip() if mn else "?"
            desc = md.group(1).strip().strip("\"'") if md else ""
            d = os.path.dirname(p)
            files = [f for f in glob.glob(os.path.join(d, "**", "*"), recursive=True)
                     if os.path.isfile(f) and f != p and "__pycache__" not in f]
            rec = dict(name=name, desc=desc, path=p, dir=d, text=t, size=len(t), files=files,
                       root="external" if root != SKILLS else "local")
            if name in skills:
                rec["shadows"] = skills[name]["path"]
            skills.setdefault(name, rec)
    return skills


def bundled():
    try:
        return {l.split(":")[0] for l in open(os.path.join(SKILLS, ".bundled_manifest")).read().split() if ":" in l}
    except OSError:
        return set()


def usage():
    try:
        return json.load(open(os.path.join(SKILLS, ".usage.json")))
    except (OSError, ValueError):
        return {}


def corpus_text():
    parts = []
    for root in [SKILLS] + external_dirs():
        for f in glob.glob(os.path.join(root, "**", "*.md"), recursive=True):
            if "/." not in f.replace(root, ""):
                try:
                    parts.append(open(f, encoding="utf-8", errors="replace").read())
                except OSError:
                    pass
    return "\n".join(parts)


def landed(args, corpus):
    """True when the new text of every op is already present in some skill file (re-applied later/elsewhere)."""
    try:
        a = json.loads(args)
    except ValueError:
        return False
    chunks = []
    for o in a.get("operations") or [a]:
        new = o.get("new_string") or o.get("content") or o.get("file_content") or ""
        old = o.get("old_string") or ""
        lines = [l.strip() for l in new.splitlines() if l.strip() and l.strip() not in old]
        if lines:
            key = max(lines, key=len)
            chunks.append(re.sub(r"\s+", " ", key)[20:120])
    norm = re.sub(r"\s+", " ", corpus)
    return bool(chunks) and all(c in norm for c in chunks)


def acked():
    try:
        return {l.split()[0] for l in open(os.path.join(OUT, "acked.txt")) if l.strip()}
    except OSError:
        return set()


def _ro_connect(path, timeout=10):
    """Read-only open. mode=ro fails on a WAL db while its -wal/-shm is absent (writers delete them on last close),
    so fall back to mode=rw + query_only, which reads the same data and never writes rows."""
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout)
        c.execute("select 1 from sqlite_master limit 1")
        return c
    except sqlite3.OperationalError:
        c = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=timeout)
        c.execute("PRAGMA query_only=ON")
        return c


def lost_writes(days):
    """Failed skill_manage calls with no later successful write to the same skill in that session."""
    db = os.path.join(HOME, "state.db")
    if not os.path.exists(db):
        return []
    con = _ro_connect(db, timeout=5)
    since = time.time() - days * 86400
    rows = con.execute(
        "select session_id, timestamp, tool_call_id, content from messages "
        "where tool_name='skill_manage' and timestamp>? order by timestamp", (since,)).fetchall()
    calls = {}
    for sid, tc in con.execute(
            "select session_id, tool_calls from messages where timestamp>? and tool_calls like '%skill_manage%'",
            (since - 3600,)):
        try:
            for c in json.loads(tc):
                calls[c.get("id")] = c.get("function", {}).get("arguments", "")
        except (ValueError, TypeError):
            pass
    events = []  # (sid, ts, tcid, ok, skills, error)
    for sid, ts, tcid, content in rows:
        args = calls.get(tcid, "")
        try:
            a = json.loads(args)
            ops = a.get("operations") or [a]
            names = sorted({o.get("name") for o in ops if o.get("name")})
            noop = all(o.get("old_string") is not None and o.get("old_string") == o.get("new_string") for o in ops)
        except (ValueError, AttributeError):
            names, noop = [], False
        ok = '"success": true' in (content or "")
        err = ""
        if not ok:
            m = re.search(r'"error":\s*"(.{0,160})', content or "")
            err = m.group(1) if m else (content or "")[:160]
        if noop:
            continue  # probes like identical old/new strings are not lost notes
        events.append((sid, ts, tcid, ok, names, err, args))
    lost = []
    corpus = corpus_text()
    done = acked()
    for i, (sid, ts, tcid, ok, names, err, args) in enumerate(events):
        if ok or not names:
            continue
        recovered = any(s == sid and t > ts and o and set(names) & set(n) for s, t, _, o, n, _, _ in events[i + 1:])
        if tcid in done:
            continue
        if not recovered and not landed(args, corpus):
            os.makedirs(OUT, exist_ok=True)
            with open(os.path.join(OUT, f"lost-{tcid}.json"), "w") as f:
                json.dump(dict(session=sid, time=ts, skills=names, error=err, arguments=args), f, indent=1)
            lost.append(dict(session=sid, time=ts, tcid=tcid, skills=names, error=err))
    return lost


def ref_issues(skills, custom):
    """references/x.md mentioned but missing (in own dir or the named umbrella dir); files never mentioned."""
    by_dir = defaultdict(str)
    for s in skills.values():
        by_dir[s["dir"]] += s["text"]
    broken, orphans = [], []
    for s in custom.values():
        for ref in sorted(set(re.findall(r"\b(references/[\w./-]+\.\w+)", s["text"]))):
            ref = ref.rstrip(".")
            cands = [os.path.join(s["dir"], ref)] + [os.path.join(o["dir"], ref) for o in skills.values()
                                                      if o["name"] in s["text"] and o is not s]
            if not any(os.path.exists(c) for c in cands):
                broken.append((s["name"], ref))
    corpus = "\n".join(by_dir.values())
    # Reachable = named in a SKILL.md, or in a text file that is itself reachable (a reference may index scripts).
    texts = {f: None for s in skills.values() for f in s["files"] if f.endswith((".md", ".txt"))}
    seen = set()
    while True:
        new = [f for f in texts if f not in seen and os.path.basename(f) in corpus]
        if not new:
            break
        for f in new:
            seen.add(f)
            corpus += "\n" + open(f, encoding="utf-8", errors="replace").read()

    def mentioned(f, d):
        rel = os.path.relpath(f, d)
        parts = rel.split(os.sep)
        if os.path.basename(f) in corpus:
            return True
        if any(p + "/" in corpus for p in parts[1:-1]):  # a mentioned sub-dir covers its files
            return True
        b = os.path.basename(f)  # test_x.py next to a mentioned x.py
        return b.startswith("test_") and b[5:] in corpus and os.path.exists(os.path.join(os.path.dirname(f), b[5:]))

    for s in custom.values():
        for f in s["files"]:
            rel = os.path.relpath(f, s["dir"])
            if rel.split(os.sep)[0] in ("references", "scripts", "templates") and not mentioned(f, s["dir"]):
                orphans.append((s["name"], rel))
    return broken, orphans


def words(s):
    return {w for w in re.findall(r"[a-z0-9]+", (s["name"].replace("-", " ") + " " + s["desc"]).lower())
            if w not in STOP and len(w) > 2}


def overlaps(custom):
    out = []
    items = sorted(custom.values(), key=lambda s: s["name"])
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            wa, wb = words(a), words(b)
            if wa and wb:
                j = len(wa & wb) / len(wa | wb)
                if j >= 0.34:
                    out.append((round(j, 2), a["name"], b["name"]))
    return sorted(out, reverse=True)


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--report", action="store_true")
    g.add_argument("--signal", action="store_true")
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args()

    skills, man, use = load_skills(), bundled(), usage()
    custom = {n: s for n, s in skills.items() if n not in man}
    lost = lost_writes(a.days)
    oversize = sorted((s["size"] / CAP, n) for n, s in custom.items() if s["size"] >= WARN * CAP)

    if a.signal:
        for frac, n in oversize:
            if frac >= CRIT:
                print(f"oversize {n}")
        for l in lost:
            print(f"lost-write {l['tcid']} {','.join(l['skills'])}")
        return

    broken, orphans = ref_issues(skills, custom)
    now = time.time()

    def age(n):
        t = use.get(n, {}).get("last_used_at")
        if not t:
            return None
        return int((now - time.mktime(time.strptime(t[:19], "%Y-%m-%dT%H:%M:%S"))) / 86400)

    print(f"# Skill audit ({len(skills)} skills: {len(custom)} custom, {len(skills) - len(custom)} bundled; "
          f"cap {CAP:,} chars)\n")
    print("## Near the size cap (SKILL.md)\n")
    for frac, n in reversed(oversize):
        if n in man:
            continue  # bundled: upstream owns it
        s = skills[n]
        print(f"- {n}: {s['size']:,} ({frac:.0%}) {'BUNDLED' if n in man else s['root']} {s['path']}")
    print(f"\n## Lost skill writes, last {a.days} days (failed, never retried in that session)\n")
    for l in lost:
        print(f"- {time.strftime('%m-%d %H:%M', time.localtime(l['time']))} {','.join(l['skills'])} "
              f"session {l['session']}: {l['error'][:120]}  -> {OUT}/lost-{l['tcid']}.json")
    print("\n## Broken reference links\n")
    for n, r in broken:
        print(f"- {n}: {r}")
    print("\n## Unreferenced supporting files\n")
    for n, r in orphans:
        print(f"- {n}: {r}")
    print("\n## Possible overlaps (custom skills, name+description word overlap)\n")
    for j, x, y in overlaps(custom):
        print(f"- {j}: {x} <> {y}")
    print("\n## Custom skills: usage\n")
    print("| skill | size | uses | patches | idle days | where |\n|---|---|---|---|---|---|")
    for n, s in sorted(custom.items()):
        u = use.get(n, {})
        print(f"| {n} | {s['size']:,} | {u.get('use_count', 0)} | {u.get('patch_count', 0)} | "
              f"{age(n) if age(n) is not None else 'never'} | {s['root']} |")
    print("\n## Description problems (custom)\n")
    for n, s in sorted(custom.items()):
        d = s["desc"]
        if not d.lower().startswith("use when") or len(d) > 120:
            print(f"- {n}: {d!r}")
    print("\n## Shadowed names\n")
    for n, s in skills.items():
        if s.get("shadows"):
            print(f"- {n}: {s['path']} shadows {s['shadows']}")


if __name__ == "__main__":
    main()
