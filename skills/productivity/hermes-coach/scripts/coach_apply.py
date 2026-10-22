#!/usr/bin/env python3
"""coach-apply: review, apply, auto-apply and revert the coach Bot's proposals (stdlib; YAML via PyYAML/ruamel).

    coach_apply.py                      list pending proposals (pulls the data repo first)
    coach_apply.py show <id>            full proposal + the exact change it would make
    coach_apply.py apply <id> [--dry-run] [--force]
    coach_apply.py reject <id> "why"    goes on the Bot's rejected list; never proposed again
    coach_apply.py revert <id>          undo an applied/canary proposal (git revert / config + file restore)
    coach_apply.py auto [--dry-run]     the rollout pass (daily, launchd on the MacBook):
        auto tier    -> gate (applies cleanly, YAML/frontmatter valid, repo pre-commit + plugin tests) -> applied
        canary tier  -> applied on THIS host only + a 7-day before/after check -> promote on KEEP, revert on REVERT
        review tier  -> a card on the `hermes` Kanban board (status carded)
        kind fix     -> the fixer lane: one `coach-fix:<slug>` card a day (1 h runtime) while the lane's spend
                        today is under $7; fixer spend is logged to research/state/spend.jsonl (lane fixer)
        every pass   -> engine_budget watchdog: running engine cards over their card/lane cap or the $20/day
                        engine total are scheduled to the next UTC day (worker SIGTERMed); Inbox engine-budget:<day>
        kind trial   -> the trial lane: one `coach-trial:<slug>` card a day (goal mode, 2 h) while the lane's spend
                        (trial cards + coach_eval runs) today is under $10; logged as lane trial
        pending > 7 days whose gate still fails -> dropped with the reason
    coach_apply.py list --status all|pending|applied|canary|carded|rejected|dropped|reverted
    coach_apply.py inbox [--days 7]    re-post recent applied/canary improvements to the Hermes Inbox
Every applied / canary / kept / reverted improvement is posted to the Hermes Inbox (`hermes inbox post`, key
coach-improvement:<id>, one card per proposal; COACH_INBOX=0 turns it off).

Roots (env): COACH_DATA (~/code/hermes-coach-data), COACH_MODS (~/code/hermes-mods), COACH_PRIV (~/code/hermes,
holds shared-config.json), HERMES_HOME (~/.hermes), COACH_HERMES (hermes binary), COACH_KANBAN_BOARD (hermes).
Every decision is appended to reports/proposals.jsonl in the data repo and pushed.
"""
import argparse, calendar, difflib, json, os, re, shutil, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import coach_proposals as cp  # noqa: E402
import coach_fixer as fx  # noqa: E402
import coach_trial as ct  # noqa: E402
import engine_budget as eb  # noqa: E402

HOME = Path.home()
SYNCED = ("skills", "desktop-plugins", "bin", "scripts", "local-fixes", "compute-rentals")  # hermes-sync SHARED_DIRS
CANARY_MAX_DAYS = 21  # a canary with no final verdict after this long is kept as applied


class ApplyError(Exception):
    pass


class Env:
    def __init__(self, data=None, mods=None, priv=None, home=None, hermes=None, board=None, push=True):
        e = os.environ
        self.data = Path(data or e.get("COACH_DATA") or HOME / "code/hermes-coach-data")
        self.mods = Path(mods or e.get("COACH_MODS") or HOME / "code/hermes-mods")
        self.priv = Path(priv or e.get("COACH_PRIV") or HOME / "code/hermes")
        self.home = Path(home or e.get("HERMES_HOME") or HOME / ".hermes")
        self.hermes = hermes or e.get("COACH_HERMES") or shutil.which("hermes") or str(HOME / ".local/bin/hermes")
        self.board = board or e.get("COACH_KANBAN_BOARD") or "hermes"
        self.push = push
        self.backups = self.home / "coach" / "apply-backups"

    def repos(self):
        return [r for r in (self.mods, self.priv, self.data) if (r / ".git").exists()]


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sh(args, cwd=None, check=True, env=None):
    r = subprocess.run(list(map(str, args)), cwd=cwd, capture_output=True, text=True, env=env)
    if check and r.returncode:
        raise ApplyError(f"failed: {' '.join(map(str, args))}\n{(r.stderr.strip() or r.stdout.strip())[-1500:]}")
    return r


def head(repo):
    return sh(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()


def git_commit(env, repo, paths, msg):
    """Commit `paths` (pre-commit hook runs: a hook failure raises and unstages). Returns sha or None."""
    sh(["git", "add", "--", *paths], cwd=repo)
    if sh(["git", "diff", "--cached", "--quiet", "--", *paths], cwd=repo, check=False).returncode == 0:
        return None
    r = sh(["git", "commit", "-q", "-m", msg, "--", *paths], cwd=repo, check=False)
    if r.returncode:
        sh(["git", "reset", "-q", "--", *paths], cwd=repo, check=False)
        raise ApplyError(f"commit refused in {repo} (pre-commit hook?):\n{(r.stderr or r.stdout).strip()[-1500:]}")
    return head(repo)


def git_push(env, repo):
    if not env.push:
        return "not pushed (push disabled)"
    for _ in range(3):
        if sh(["git", "push", "-q"], cwd=repo, check=False).returncode == 0:
            return "pushed"
        sh(["git", "pull", "-q", "--rebase", "--autostash"], cwd=repo, check=False)
    return "PUSH FAILED: the commit is local, push it by hand"


def repo_of(env, path):
    p = Path(path).resolve()
    for r in env.repos():
        if r.resolve() in p.parents:
            return r
    return None


def pull_data(env):
    if env.push:
        sh(["git", "pull", "-q", "--rebase", "--autostash"], cwd=env.data, check=False)


def push_data(env, msg):
    f = env.data / "reports/proposals.jsonl"
    if not (env.data / ".git").exists():
        return "data repo is not git"
    paths = [f.relative_to(env.data)] + [s.relative_to(env.data) for s in
                                         [fx.spend_log_path(env.data), eb.state_dir(env.data) / "budget-stops.jsonl"]
                                         if s.exists()]
    sha = git_commit(env, env.data, paths, msg)
    return (git_push(env, env.data) if sha else "nothing to commit")


def cfg_value(v):
    return v if isinstance(v, str) else json.dumps(v)


def cfg_get(env, key):
    r = sh([env.hermes, "config", "get", key], check=False)
    lines = [ln for ln in r.stdout.splitlines() if ln.strip() and not ln.startswith(("⚠", "  "))]
    return lines[-1].strip() if r.returncode == 0 and lines else None


def cfg_set(env, key, val):
    if val is None:
        sh([env.hermes, "config", "unset", key])
    else:
        sh([env.hermes, "config", "set", key, cfg_value(val)])


def find_skill(env, name, file_path):
    hits = list(env.mods.glob(f"skills/**/{name}/{file_path}")) + list(env.mods.glob(f"skills/{name}/{file_path}"))
    hits += [p for p in (env.home / "skills").glob(f"**/{name}/{file_path}") if ".hermes-sync-trash" not in str(p)]
    hits = list(dict.fromkeys(h.resolve() for h in hits))
    if not hits:
        raise ApplyError(f"skill {name}: {file_path} not found in hermes-mods or ~/.hermes/skills")
    return hits[0]  # hermes-mods wins (a same-path local copy is a shadow that hermes-sync trashes)


def target_file(env, target):
    p = Path(os.path.expanduser(target))
    if str(target).startswith("~/.hermes/"):
        p = env.home / str(target)[len("~/.hermes/"):]
    if not p.is_absolute():
        for base in (env.mods, env.priv, env.home):
            if (base / p).exists():
                p = base / p
                break
        else:
            p = env.home / p
    rp = p.resolve()
    if not any(b.resolve() == rp or b.resolve() in rp.parents for b in (env.mods, env.priv, env.home)):
        raise ApplyError(f"refusing to edit {p}: outside hermes-mods, ~/code/hermes and ~/.hermes")
    return p


def text_edit(path, ch):
    old = path.read_text() if path.exists() else ""
    if "append" in ch:
        new = old + ("" if not old or old.endswith("\n") else "\n") + ch["append"].rstrip("\n") + "\n"
    else:
        n = old.count(ch["old_string"])
        if n != 1:
            raise ApplyError(f"{path}: old_string found {n} times (need exactly 1); the file changed since the "
                             "proposal")
        new = old.replace(ch["old_string"], ch["new_string"])
    return old, new


# ── gate (until coach_eval.py exists): applies cleanly + validators + repo hooks/tests ──

def yaml_load(text):
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except ImportError:
        pass
    try:
        from ruamel.yaml import YAML  # type: ignore
        return YAML(typ="safe", pure=True).load(text)
    except ImportError:
        raise ApplyError("gate needs PyYAML or ruamel.yaml to check a YAML target (run under the Hermes venv python)")


def check_text(path, old, new, p):
    """Validators for the edited text. Raises ApplyError."""
    if path.suffix in (".yaml", ".yml"):
        try:
            doc = yaml_load(new)
        except ApplyError:
            raise
        except Exception as e:
            raise ApplyError(f"{path.name} no longer parses as YAML: {str(e)[:300]}")
        if p.get("kind") == "curriculum":
            entries = (doc.get("entries") if isinstance(doc, dict) else doc) or []
            before = yaml_load(old) if old.strip() else []
            before = (before.get("entries") if isinstance(before, dict) else before) or []
            known = {str(e.get("id") or e.get("feature")) for e in before if isinstance(e, dict)}
            added = [e for e in entries if isinstance(e, dict) and str(e.get("id") or e.get("feature")) not in known]
            for e in added if "append" in p["change"] else []:
                if not e.get("id"):
                    raise ApplyError("curriculum entry has no `id`")
                if not (isinstance(e.get("trigger"), (dict, list)) and isinstance(e.get("adopted"), dict)
                        and e.get("tip")):
                    raise ApplyError(f"curriculum entry {e.get('id')} is prose: it needs a machine-readable "
                                     "`trigger` (dict/list), an `adopted` dict and a `tip`, or the coach never fires it")
    if path.name == "SKILL.md":
        m = re.match(r"---\n(.*?)\n---\n", new, re.S)
        if not m or not re.search(r"(?m)^name:", m.group(1)) or not re.search(r"(?m)^description:", m.group(1)):
            raise ApplyError("SKILL.md frontmatter (name/description) broken by the edit")


def plugin_tests(env, path):
    """Run the plugin's tests when the edited file sits in plugins/<n>/ of hermes-mods and it has tests/."""
    try:
        rel = path.resolve().relative_to(env.mods.resolve()).parts
    except ValueError:
        return None
    if len(rel) < 3 or rel[0] != "plugins" or not (env.mods / rel[0] / rel[1] / "tests").is_dir():
        return None
    uv = shutil.which("uv")
    if not uv:
        raise ApplyError("plugin tests need uv on PATH")
    d = env.mods / rel[0] / rel[1]
    r = sh([uv, "run", "-q", "--with", "pytest", "--with", "pyyaml", "--with", "fastapi", "--with", "httpx",
            "python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"], cwd=d, check=False)
    if r.returncode:
        raise ApplyError(f"{rel[1]} tests failed:\n{(r.stdout + r.stderr).strip()[-1500:]}")
    return f"{rel[1]} tests pass"


# ── plan / apply / revert ──

def plan(env, p, host_only=False):
    """Returns (description, run). run() -> dict event fields (result + revert info). Raises ApplyError."""
    kind, tgt, ch = p["kind"], p["target"], p["change"]
    if kind == "core":
        raise ApplyError("core proposals are never applied by a script; they go to a Kanban card (review tier)")
    if kind in ("config", "shared_config"):
        v = ch["value"]
        cur = cfg_get(env, tgt)
        shared = kind == "shared_config" and not host_only
        desc = f"{tgt}: {cur or '(unset)'} -> {cfg_value(v)}" + ("" if shared else " (this host only)")

        def run():
            ev = {"prev": cur if cur is not None else "__unset__", "host_only": not shared}
            out = []
            if shared:
                ev.update(share_config(env, tgt, v, p))
                out.append(ev.pop("_msg"))
            cfg_set(env, tgt, v)
            out.append(f"config set {tgt}={cfg_value(v)}")
            ev["result"] = "; ".join(out)
            return ev
        return desc, run
    path = find_skill(env, tgt, ch.get("file_path") or "SKILL.md") if kind == "skill_patch" else target_file(env, tgt)
    old, new = text_edit(path, ch)
    check_text(path, old, new, p)

    def run():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new)
        repo = repo_of(env, path)
        try:
            tests = plugin_tests(env, path)
            if repo and repo != env.data:
                rel = path.resolve().relative_to(repo.resolve())
                sha = git_commit(env, repo, [rel], f"{tgt if kind == 'skill_patch' else path.name}: "
                                 f"{p.get('title') or kind} (coach {p['id']})")
                res = f"edited {path}; commit {sha[:10] if sha else 'none'} {git_push(env, repo) if sha else ''}"
                return {"result": res + (f"; {tests}" if tests else ""), "repo": str(repo), "commit": sha,
                        "path": str(path)}
        except ApplyError:
            path.write_text(old)
            raise
        env.backups.mkdir(parents=True, exist_ok=True)
        bk = env.backups / f"{p['id']}.orig"
        bk.write_text(old)
        rel = path.resolve().relative_to(env.home.resolve()).parts if env.home.resolve() in path.resolve().parents \
            else ()
        where = ("hermes-sync carries it to the other hosts within 10 min" if rel and rel[0] in SYNCED
                 else f"private, this host only: ~/.hermes/{rel[0] if rel else ''} is not synced")
        return {"result": f"edited {path} ({where})", "path": str(path), "backup": str(bk)}
    return (path, old, new), run


def share_config(env, key, v, p):
    f = env.priv / "shared-config.json"
    d = json.loads(f.read_text()) if f.exists() else {}
    had, prev = key in d, d.get(key)
    d[key] = v
    f.write_text(json.dumps(d, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    sha = git_commit(env, env.priv, ["shared-config.json"], f"shared-config: {key} -> {cfg_value(v)} (coach {p['id']})")
    return {"_msg": f"shared-config {sha[:10] if sha else 'unchanged'} {git_push(env, env.priv) if sha else ''}",
            "shared_commit": sha, "shared_prev": prev if had else "__unset__"}


def revert_event(env, p):
    """Undo what an applied/canary event recorded. Returns a result string."""
    out = []
    for key, repo in (("commit", p.get("repo")), ("shared_commit", str(env.priv))):
        sha = p.get(key)
        if sha and repo:
            r = sh(["git", "revert", "--no-edit", sha], cwd=repo, check=False)
            if r.returncode:
                sh(["git", "revert", "--abort"], cwd=repo, check=False)
                raise ApplyError(f"git revert {sha[:10]} failed in {repo}: {(r.stderr or r.stdout).strip()[-600:]}")
            out.append(f"reverted {sha[:10]} in {Path(repo).name} ({git_push(env, Path(repo))})")
    if p.get("backup"):  # undo only this proposal's edit, so later edits to the same file survive
        f, ch = Path(p["path"]), p["change"]
        cur = f.read_text()
        if "append" in ch:
            blk = ch["append"].rstrip("\n") + "\n"
            i = cur.rfind(blk)
            if i < 0:
                raise ApplyError(f"{f}: the appended text is no longer there; undo by hand (backup {p['backup']})")
            new = cur[:i] + cur[i + len(blk):]
        elif cur.count(ch["new_string"]) == 1:
            new = cur.replace(ch["new_string"], ch["old_string"])
        else:
            raise ApplyError(f"{f}: new_string found {cur.count(ch['new_string'])} times; undo by hand "
                             f"(backup {p['backup']})")
        f.write_text(new)
        out.append(f"undid the edit in {f} (pre-apply copy: {p['backup']})")
    if p["kind"] in ("config", "shared_config") and "prev" in p:
        prev = None if p["prev"] == "__unset__" else p["prev"]
        cfg_set(env, p["target"], prev)
        out.append(f"config {p['target']} -> {prev if prev is not None else '(unset)'}")
    if not out:
        raise ApplyError(f"{p['id']} has no revert info (applied before revert support): undo it by hand")
    return "; ".join(out)


def mark(env, pid, status, by="coach-apply", note="", result="", **extra):
    ns = argparse.Namespace(data=str(env.data), id=pid, status=status, note=note, result=result[:400], by=by,
                            extra={k: v for k, v in extra.items() if v is not None})
    with open(os.devnull, "w") as dn:
        so, sys.stdout = sys.stdout, dn
        try:
            cp.cmd_mark(ns)
        finally:
            sys.stdout = so


# Inbox card copy (design: "Now / Before", ~/design/coach-inbox-cards). Row title = "<Kind>: <short>", with a
# status prefix only when it is not simply live. Body = Now / Before lines, the literal change, one muted meta line.
INBOX_HEAD = {"applied": "", "canary": "Trying", "kept": "Kept", "promoted": "Kept", "reverted": "Undone"}
KIND_LABEL = {"curriculum": "Tip", "skill_patch": "Rule", "plugin_rule": "Rule", "config": "Setting",
              "shared_config": "Setting", "core": "Code", "fix": "Fix", "trial": "Trial"}
INBOX_TYPE = "coach-improvement"
NOT_NOW_DAYS = 14
PROMPT_LABEL = "check whether this is helping, then keep or undo it"


def _yaml_str(block, key):
    """First `key: "..."` (or unquoted) scalar in a curriculum YAML snippet, unescaped; '' if absent."""
    m = re.search(rf"^\s*-?\s*{key}:\s*(\"(?:[^\"\\]|\\.)*\"|'[^']*'|[^\n]+)\s*$", block or "", re.M)
    if not m:
        return ""
    v = m.group(1).strip()
    if v[:1] == '"':
        try:
            return json.loads(v)
        except ValueError:
            return v.strip('"')
    return v.strip("'")


def _added_text(change):
    """The text a skill/curriculum edit adds (append, or new_string minus old_string)."""
    if "append" in change:
        return change["append"]
    old, new = change.get("old_string") or "", change.get("new_string") or ""
    if new.startswith(old):
        return new[len(old):]
    added = [l[2:] for l in difflib.ndiff(old.splitlines(), new.splitlines()) if l.startswith("+ ")]
    return "\n".join(added)


def _quote(text, limit=700):
    text = " ".join(str(text).split())
    text = text if len(text) <= limit else text[:limit - 1].rstrip() + "…"
    return "> " + text


def _when_pt(at):
    try:
        from zoneinfo import ZoneInfo
        t = calendar.timegm(time.strptime(str(at)[:19], "%Y-%m-%dT%H:%M:%S"))
        import datetime as _dt
        d = _dt.datetime.fromtimestamp(t, ZoneInfo("America/Los_Angeles"))
        return f"{d:%b} {d.day}, {d:%I:%M %p}".replace(" 0", " ") + f" {d.tzname()}"
    except Exception:  # noqa: BLE001
        return ""


def _ro_connect(path, timeout=10):
    """Read-only open. mode=ro fails on a WAL db while its -wal/-shm is absent (writers delete them on last close),
    so fall back to mode=rw + query_only, which reads the same data and never writes rows."""
    import sqlite3
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout)
        c.execute("select 1 from sqlite_master limit 1")
        return c
    except sqlite3.OperationalError:
        c = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=timeout)
        c.execute("PRAGMA query_only=ON")
        return c


def _evidence_source(env, evidence):
    """First evidence session as a link chip when it lives on this host, else its title; '' if none."""
    m = re.match(r"\s*([\w.-]+)/([\w.-]+)/(\d{8}_\d{6}_[0-9a-f]+)\s+—\s+([^:]+)", str(evidence or ""))
    if not m:
        return ""
    _host, prof, sid, title = m.groups()
    db = env.home / ("state.db" if prof == "default" else f"profiles/{prof}/state.db")
    try:
        import sqlite3
        con = _ro_connect(db, timeout=5)
        try:
            if con.execute("select 1 from sessions where id=?", (sid,)).fetchone():
                return f"@session:{prof}/{sid}"
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        pass
    return f"“{title.strip()[:60]}”"


def _before_fallback(evidence):
    """Without a `before` field: the first evidence item's quote/facts, without the session id."""
    first = re.split(r";\s*(?=[\w.-]+/[\w.-]+/\d{8}_)", str(evidence or ""))[0]
    first = re.sub(r"^\s*[\w.-]+/[\w.-]+/\d{8}_\d{6}_[0-9a-f]+\s+—\s+[^:]+:\s*", "", first)
    first = " ".join(first.split())
    return first if len(first) <= 220 else first[:219].rstrip() + "…"


def _scope(p, action):
    k = p["kind"]
    if k == "curriculum":
        return "this Mac only"
    if k == "skill_patch":
        return "all Macs (synced)"
    if k == "core":
        return "all Macs on next update"
    if k in ("config", "shared_config", "plugin_rule"):
        return "all Macs" if action == "promoted" or k == "shared_config" and action != "canary" else \
            "this Mac only (7-day trial)" if action == "canary" else "this Mac only"
    return ""


def _change_block(p, action, detail):
    """Markdown for the literal change: the tip as it will appear, the rule text added, the setting, or the commit."""
    k, ch = p["kind"], p.get("change") or {}
    if action == "reverted":
        return f"**Undone:** {detail or p.get('result') or 'reverted'}"
    if k == "curriculum":
        added = _added_text(ch)
        tip, try_ = _yaml_str(added, "tip"), _yaml_str(added, "try")
        if tip:
            return "> [!TIP]\n" + _quote(tip) + (f"\n>\n> Try: “{try_}”" if try_ else "")
        return "Added to the coach tips:\n\n" + _quote(added)
    if k in ("skill_patch", "plugin_rule"):
        added = _added_text(ch).strip().lstrip("- ")
        where = f"the `{p['target']}` skill" if k == "skill_patch" else f"`{Path(str(p['target'])).name}`"
        return f"Added to {where}:\n\n" + _quote(added)
    if k in ("config", "shared_config"):
        prev = p.get("prev")
        prev = "(unset)" if prev in (None, "__unset__") else json.dumps(prev)
        return f"`{p['target']}`: {prev} → {json.dumps(ch.get('value'))}"
    res = str(detail or p.get("result") or "").strip()
    out = f"**Shipped:** {res}" if res else f"**Planned:** {ch.get('summary', '')}"
    note = str(p.get("note") or "").strip()
    if note and action == "applied":
        out += f"\n\n**Left out:** {note}" if re.search(r"declin|not (done|implemented)|skipp|left out", note, re.I) \
            else f"\n\n**Note:** {note}"
    return out


def inbox_card(env, p, action, detail=""):
    """(title, body) for an improvement's Inbox card. Optional proposal fields `short` (row title), `effect`
    (what is different for the user now) and `before` (the problem, with a number) make it readable at a glance."""
    kind = KIND_LABEL.get(p["kind"], p["kind"])
    short = (p.get("short") or p.get("title") or f"{p['kind']} {p['target']}").strip().rstrip(".")
    title = (f"{INBOX_HEAD[action]} · " if INBOX_HEAD[action] else "") + f"{kind}: {short}"
    effect = str(p.get("effect") or "").strip()
    before = str(p.get("before") or "").strip() or _before_fallback(p.get("evidence"))
    lines = []
    if action == "reverted":
        lines.append(f"**Undone:** this no longer applies{f' ({effect[0].lower() + effect[1:]})' if effect else ''}.")
    elif effect:
        lines.append(f"**Now:** {effect}")
    elif p.get("title") and p.get("short"):
        lines.append(f"**Now:** {p['title']}")
    if before:
        lines.append(f"**Before:** {before}")
    parts = ["  \n".join(lines)] if lines else []
    if action != "reverted" or p["kind"] != "curriculum":
        parts.append(_change_block(p, action, detail))
    elif detail:
        parts.append(f"_{detail}_")
    meta = [kind, _scope(p, action)]
    when = _when_pt(p.get("at"))
    if when:
        meta.append(("undone " if action == "reverted" else "trying since " if action == "canary" else "live ")
                    + when)
    meta.append(f"id {p['id']}")
    tail = [f"Undo: `coach-apply revert {p['id']}`"] if action != "reverted" else []
    src = _evidence_source(env, p.get("evidence"))
    if src:
        tail.append(f"Source: {src}")
    parts.append("_" + " · ".join(m for m in meta if m) + "_" + ("  \n" + " · ".join(tail) if tail else ""))
    return title[:80], "\n\n".join(x for x in parts if x)


def notify_inbox(env, p, action, detail="", out=print):
    """Post an accepted (or rolled-back) improvement to the Hermes Inbox. One card per proposal (key
    coach-improvement:<id>), so canary -> kept/reverted updates the same card. Best effort: never fails the apply.
    COACH_INBOX=0 turns it off."""
    if os.environ.get("COACH_INBOX", "1") == "0" or action not in INBOX_HEAD:
        return None
    pid = p["id"]
    title, body = inbox_card(env, p, action, detail)
    prompt = (f"Review coach improvement {pid} (`coach-apply show {pid}`; the change is recorded in "
              "~/code/hermes-coach-data/reports/proposals.jsonl). Show me exactly what changed, then check whether "
              "it is helping using the coach's before/after KPI (coach_weekly check for this proposal, or the latest "
              "reports/weekly-*.md) and recent sessions. Give a one-line verdict: keep or revert. If it is clearly "
              f"hurting, run `coach-apply revert {pid} \"<why>\"` and tell me. If I answer keep / not now / never, record it "
              f"with `coach-apply decide {pid} keep|not_now|never`.")
    try:
        base = [env.hermes, "inbox", "post", title, "--body", body, "--prompt", prompt,
                "--key", f"coach-improvement:{pid}", "--poster", "coach · improvements", "--cwd", str(env.data)]
        typed = ["--type", INBOX_TYPE, "--field", f"id={pid}"]
        r = None
        # richest first; an older inbox plugin lacks --type / --prompt-label, and a host without the type installed
        # rejects it ("unknown inbox card type"): any failure falls back to the plainer card
        for extra in (typed + ["--prompt-label", PROMPT_LABEL], ["--prompt-label", PROMPT_LABEL], []):
            if action == "reverted":  # an Undone notice has nothing to decide: the plain Do it / Archive card
                extra = [x for x in extra if x not in ("--type", "--field", INBOX_TYPE, f"id={pid}")]
            r = sh(base + extra, check=False)
            if not r.returncode:
                break
        if r.returncode:
            out(f"inbox post failed for {pid}: {(r.stderr or r.stdout).strip()[-200:]}")
            return None
        return True
    except Exception as e:  # noqa: BLE001 — the inbox is a notification, not part of the apply
        out(f"inbox post failed for {pid}: {e}")
        return None


def decisions_path(data):
    return Path(data) / "reports" / "inbox-decisions.jsonl"


def inbox_decisions(data):
    f = decisions_path(data)
    rows = []
    for line in (f.read_text().splitlines() if f.exists() else []):
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def apply_decision(env, p, act, now=None, dry=False, out=print):
    """Do what Keep / Not now / Never means for proposal *p*. Returns a one-line detail. Idempotent: a repeat adds
    no second suppression row."""
    now = now or time.time()
    pid = p["id"]
    if act == "keep":
        if p.get("status") != "canary":
            return "kept"
        if not dry:
            ev = {}
            if p["kind"] == "shared_config":
                ev = share_config(env, p["target"], p["change"]["value"], p)
                ev.pop("_msg", None)
            mark(env, pid, "applied", by="inbox", note="kept from the Inbox",
                 result="promoted to shared-config" if ev else "kept", **ev)
        return "canary kept from the Inbox"
    if act == "not_now":
        if not dry and not _has_suppression(env, p, "not_now", now):
            cp.add_suppression(env.data, p, "not_now", NOT_NOW_DAYS, now)
        return f"no {p['kind']}:{p['target']} proposals for {NOT_NOW_DAYS} days"
    detail = f"{p['kind']}:{p['target']} is never proposed again"
    if not dry:
        if not _has_suppression(env, p, "never", now):
            cp.add_suppression(env.data, p, "never", None, now)
        if p.get("status") in ("applied", "canary"):
            res = revert_event(env, p)
            mark(env, pid, "reverted", by="inbox", note="Never, from the Inbox", result=res)
            detail += f"; reverted ({res})"
    return detail


def _has_suppression(env, p, mode, now):
    return any(r.get("kind") == p.get("kind") and r.get("target") == p.get("target") and r.get("mode") == mode
               and (not r.get("until") or cp.suppressed(env.data, p, now)) for r in cp.suppressions(env.data))


def sync_inbox(env, dry=False, now=None, out=print, days=14, limit=40):
    """Read the outcome of each recent improvement card (`hermes inbox outcome coach-improvement:<id>`) and honour it:
    Keep = a canary is promoted now (no auto-revert), Not now = nothing of that kind + target is proposed for
    NOT_NOW_DAYS, Never = never proposed again and undone if live. Each outcome is acted on once (it is recorded in
    reports/inbox-decisions.jsonl). Undo is a Kanban task that already ran `revert`; it needs nothing here.
    Returns [(id, outcome, detail)]."""
    if os.environ.get("COACH_INBOX", "1") == "0":
        return []
    now = now or time.time()
    seen = {(r.get("id"), r.get("outcome_at")) for r in inbox_decisions(env.data)}
    live = [p for p in cp.state(env.data).values() if p.get("status") in ("applied", "canary", "reverted")
            and p.get("kind") not in ("fix", "trial") and cp.age_days(p, now) <= days]
    done = []
    for p in sorted(live, key=lambda q: q.get("at", ""), reverse=True)[:limit]:
        pid = p["id"]
        try:
            r = sh([env.hermes, "inbox", "outcome", f"coach-improvement:{pid}"], check=False)
            o = json.loads(r.stdout or "{}")
        except Exception:  # noqa: BLE001 — an unreadable card never blocks the rollout pass
            continue
        if not o.get("found") or o.get("state") != "resolved" or (pid, o.get("at")) in seen:
            continue
        act = o.get("outcome")
        if act not in ("keep", "not_now", "never"):  # undo (a task already reverted it), archived, unknown
            continue
        try:
            detail = apply_decision(env, p, act, now, dry, out)
        except Exception as e:  # noqa: BLE001 — a failed revert is recorded, never repeated or allowed to stop the pass
            detail = f"FAILED: {type(e).__name__}: {str(e)[:200]}"
            out(f"inbox decision {act} on {pid} failed: {detail}")
        if not dry:
            f = decisions_path(env.data)
            f.parent.mkdir(parents=True, exist_ok=True)
            with open(f, "a") as fh:
                fh.write(json.dumps({"id": pid, "outcome": act, "outcome_at": o.get("at"), "value": o.get("value"),
                                     "at": now_iso(), "detail": detail}) + "\n")
        done.append((pid, "inbox " + act, detail))
    return done


def check_spec(p, at=None):
    v = p["change"].get("value")
    return f"{p['target']}={cfg_value(v)}@{at or now_iso()[:16]}Z#{p.get('signal') or 'synthetic_turn_cap'}" \
        if p["kind"] in ("config", "shared_config") else \
        f"{p['kind']}:{Path(str(p['target'])).name}@{at or now_iso()[:16]}Z#{p.get('signal') or 'repeat_fix'}"


def file_card(env, p, dry=False):
    title = f"Coach proposal {p['id']} (review tier): {p.get('title') or p['kind'] + ' ' + p['target']}"[:120]
    body = (f"The coach Bot proposed this in {p.get('report')}. Tier `review` ({cp.default_tier(p)} by default): "
            "a human-or-reviewer decision, not an auto apply.\n\n"
            f"kind: {p['kind']}\ntarget: {p['target']}\nrisk: {p.get('risk', '?')}\n\nchange:\n```json\n"
            f"{json.dumps(p['change'], indent=1, ensure_ascii=False)}\n```\n\nevidence: {p.get('evidence')}\n\n"
            "Done when: the change is built and verified (tests next to existing ones), merged on green per standing "
            f"approval, and `coach-apply` marks {p['id']} applied (or reject it with `coach-apply reject {p['id']} "
            "\"why\"`).")
    if dry:
        return "dry-run"
    r = sh([env.hermes, "kanban", "--board", env.board, "create", "--assignee", "default", "--json",
            "--idempotency-key", f"coach-proposal-{p['id']}", "--body", body, title])
    m = re.search(r'"id"\s*:\s*"([^"]+)"', r.stdout) or re.search(r"\bt_[0-9a-f]{6,}\b", r.stdout)
    if not m:
        raise ApplyError(f"kanban create gave no id: {r.stdout[-300:]}")
    return m.group(1) if m.groups() else m.group(0)


def file_fix(env, p, dry=False, now=None, by_day=None, force=False, initial_status=None):
    """Fixer lane: (status, card, how). carded = a new or existing coach-fix:<slug> card; a closed lane raises
    ApplyError (stays pending; dropped once stale). force = file past the daily caps (a human backlog pass)."""
    errs = fx.validate_fix(p)
    if errs:
        raise ApplyError("fix proposal invalid: " + "; ".join(errs))
    have = fx.existing_card(env.home, env.board, p["target"])
    if have:
        return "carded", have["id"], f"already a card ({have['status']})"
    ok, why, _usd = fx.gate(env.home, env.board, now, by_day, data=env.data)
    if not ok and not force:
        raise ApplyError(f"fixer lane closed: {why}")
    if dry:
        return "carded", "dry-run", "would file"
    r = sh([env.hermes, "kanban", "--board", env.board, "create", "--assignee", "default", "--json",
            "--created-by", "coach", "--idempotency-key", f"coach-fix:{p['target']}",
            "--max-runtime", str(fx.FIX_RUNTIME_S), "--body", fx.card_body(p)]
           + (["--initial-status", initial_status] if initial_status else []) + [fx.card_title(p)])
    m = re.search(r'"id"\s*:\s*"([^"]+)"', r.stdout) or re.search(r"\bt_[0-9a-f]{6,}\b", r.stdout)
    if not m:
        raise ApplyError(f"kanban create gave no id: {r.stdout[-300:]}")
    return "carded", (m.group(1) if m.groups() else m.group(0)), "filed"


def file_trial(env, p, dry=False, now=None, by_day=None, force=False):
    """Trial lane: (status, card, how), like file_fix. The card builds the idea behind a flag and gates it with
    coach_eval.py; a pass becomes a normal proposal with its own tier."""
    errs = ct.validate_trial(p)
    if errs:
        raise ApplyError("trial proposal invalid: " + "; ".join(errs))
    have = ct.existing_card(env.home, env.board, p["target"])
    if have:
        return "carded", have["id"], f"already a card ({have['status']})"
    ok, why, _usd = ct.gate(env.home, env.board, env.data, now, by_day)
    if not ok and not force:
        raise ApplyError(f"trial lane closed: {why}")
    if dry:
        return "carded", "dry-run", "would file"
    r = sh([env.hermes, "kanban", "--board", env.board, "create", "--assignee", "default", "--json",
            "--created-by", "coach", "--idempotency-key", f"coach-trial:{p['target']}", "--goal",
            "--max-runtime", str(ct.TRIAL_RUNTIME_S), "--skill", "hermes-coach", "--body", ct.card_body(p),
            ct.card_title(p)])
    m = re.search(r'"id"\s*:\s*"([^"]+)"', r.stdout) or re.search(r"\bt_[0-9a-f]{6,}\b", r.stdout)
    if not m:
        raise ApplyError(f"kanban create gave no id: {r.stdout[-300:]}")
    return "carded", (m.group(1) if m.groups() else m.group(0)), "filed"


def canary_verdict(env, p, now):
    """KEEP | KEEP (provisional) | RAISE | REVERT from coach_weekly's before/after check."""
    import coach_mine, coach_weekly  # noqa: E401
    rec = coach_weekly.check_change(coach_mine.load(env.data), p["check"], now)[-1]
    return rec.replace("Recommendation: ", "")


def auto(env, dry=False, now=None, out=print):
    """One rollout pass over every proposal. Returns a list of (id, action, detail)."""
    pull_data(env)
    now = now or time.time()
    done = []
    by_day = fx.spend_by_day(env.home, env.board, 3, now)
    trial_by_day = fx.spend_by_day(env.home, env.board, 3, now, lane="trial")
    if not dry and (env.data / ".git").exists():
        fx.log_spend(env.data, by_day)
        fx.log_spend(env.data, trial_by_day, lane="trial")
    try:  # in-flight enforcement: stop running engine cards over their card/lane/total budget (engine_budget.py)
        wd = eb.watchdog(env.home, env.board, env.data, env.hermes, now, dry, out=lambda m: out("budget " + m))
        done += [(c["id"], "budget stop", f"{why} -> {res}") for c, _u, _b, why, res in wd["stops"]]
    except Exception as e:  # noqa: BLE001 — never block the rollout pass on the watchdog
        out(f"budget watchdog failed: {type(e).__name__}: {e}")
    try:  # honour Keep / Not now / Never pressed on improvement cards since the last pass
        done += sync_inbox(env, dry, now, out)
    except Exception as e:  # noqa: BLE001
        out(f"inbox sync failed: {type(e).__name__}: {e}")
    for p in sorted(cp.state(env.data).values(),
                    key=lambda q: (q.get("kind") not in ("fix", "trial"), q.get("at", ""))):
        st, pid = p.get("status"), p["id"]
        try:
            if st == "pending":
                sup = cp.suppressed(env.data, p, now)
                if sup:
                    dry or mark(env, pid, "dropped", by="coach-apply auto",
                                note=f"{sup['mode'].replace('_', ' ')} from the Inbox ({sup.get('id')})")
                    done.append((pid, "dropped", f"{sup['mode']} from the Inbox"))
                    continue
                tier = cp.effective_tier(p)
                stale = cp.age_days(p, now) > cp.STALE_DAYS
                if p.get("kind") == "fix":
                    st2, card, how = file_fix(env, p, dry, now, by_day)
                    dry or mark(env, pid, st2, by="coach-apply auto", card=card, tier=tier,
                                result=f"fix card {card} on board {env.board} ({how})")
                    done.append((pid, "carded", f"{card} ({how})"))
                    continue
                if p.get("kind") == "trial":
                    st2, card, how = file_trial(env, p, dry, now, trial_by_day)
                    dry or mark(env, pid, st2, by="coach-apply auto", card=card, tier=tier,
                                result=f"trial card {card} on board {env.board} ({how})")
                    done.append((pid, "carded", f"{card} ({how})"))
                    continue
                if tier == "review":
                    card = file_card(env, p, dry)
                    dry or mark(env, pid, "carded", by="coach-apply auto", card=card, tier=tier,
                                result=f"kanban card {card} on board {env.board}")
                    done.append((pid, "carded", card))
                    continue
                desc, run = plan(env, p, host_only=(tier == "canary"))
                if dry:
                    done.append((pid, f"would apply ({tier})", desc if isinstance(desc, str) else str(desc[0])))
                    continue
                ev = run()
                if tier == "canary":
                    ev["check"] = check_spec(p)
                    mark(env, pid, "canary", by="coach-apply auto", tier=tier, **ev)
                    done.append((pid, "canary", ev["result"]))
                    notify_inbox(env, {**p, "tier": tier}, "canary", ev["result"], out)
                else:
                    mark(env, pid, "applied", by="coach-apply auto", tier=tier, **ev)
                    done.append((pid, "applied", ev["result"]))
                    notify_inbox(env, {**p, "tier": tier}, "applied", ev["result"], out)
            elif st == "canary" and p.get("check"):
                verdict = canary_verdict(env, p, now)
                age = (now - calendar.timegm(time.strptime(p["check"].split("@")[1][:16], "%Y-%m-%dT%H:%M"))) / 86400
                if verdict.startswith("REVERT"):
                    res = "dry-run" if dry else revert_event(env, p)
                    dry or mark(env, pid, "reverted", by="coach-apply auto", note=verdict, result=res)
                    done.append((pid, "reverted", verdict))
                    dry or notify_inbox(env, p, "reverted", f"canary verdict {verdict}: {res}", out)
                elif verdict.startswith(("KEEP:", "RAISE")) or age > CANARY_MAX_DAYS:
                    ev = {}
                    if p["kind"] == "shared_config" and not dry:
                        ev = share_config(env, p["target"], p["change"]["value"], p)
                        ev.pop("_msg", None)
                    dry or mark(env, pid, "applied", by="coach-apply auto", note=f"canary verdict: {verdict}",
                                result="promoted to shared-config" if ev else "kept", **ev)
                    done.append((pid, "promoted" if ev else "kept", verdict))
                    dry or notify_inbox(env, p, "promoted" if ev else "kept",
                                        f"{p.get('result') or ''} — canary verdict: {verdict}", out)
        except ApplyError as e:
            if st == "pending" and cp.age_days(p, now) > cp.STALE_DAYS:
                dry or mark(env, pid, "dropped", by="coach-apply auto",
                            note=f"stale >{cp.STALE_DAYS}d and the gate fails: {str(e)[:300]}")
                done.append((pid, "dropped", str(e)))
            else:
                done.append((pid, "gate failed", str(e)))
    for d in done:
        out(f"{d[0]:<13} {d[1]:<14} {str(d[2])[:300]}")
    if not done:
        out("coach-apply auto: nothing to do")
    if not dry:
        msg = ", ".join(d[0] + " " + d[1] for d in done if d[1] != "gate failed") or "fixer spend"
        out("data " + push_data(env, f"coach-apply auto: {msg}"[:200]))
    return done


# ── CLI ──

def get(env, pid):
    st = cp.state(env.data)
    if pid not in st:
        raise SystemExit(f"unknown proposal {pid}; `coach-apply list --status all`")
    return st[pid]


def show_diff(path, old, new):
    sys.stdout.writelines(difflib.unified_diff(old.splitlines(True), new.splitlines(True), str(path), str(path)))


def cmd_list(env, a):
    pull_data(env)
    rows = [p for p in cp.state(env.data).values() if a.status == "all" or p.get("status") == a.status]
    if not rows:
        print(f"no {a.status} proposals in {env.data}/reports/proposals.jsonl")
    for p in rows:
        print(f"{p['id']:<13} {p.get('status', ''):<8} {p.get('tier', '?'):<6} [{p.get('risk', '?')}] "
              f"{p['kind']:<13} {p['target']}\n{'':14}{p.get('title') or ''}\n"
              f"{'':14}evidence: {str(p.get('evidence', ''))[:160]}")
    if a.status == "pending" and rows:
        print("\ncoach-apply show <id> | apply <id> [--dry-run] | reject <id> \"why\" | auto [--dry-run]")


def cmd_show(env, a):
    pull_data(env)
    p = get(env, a.id)
    print(json.dumps(p, indent=1, ensure_ascii=False))
    desc, _ = plan(env, p)
    print("\nWould change:")
    show_diff(*desc) if isinstance(desc, tuple) else print(" ", desc)


def cmd_apply(env, a):
    pull_data(env)
    p = get(env, a.id)
    if p.get("status") != "pending" and not a.force:
        raise SystemExit(f"{a.id} is {p.get('status')}; --force to apply anyway")
    if p.get("kind") in ("fix", "trial"):  # file the lane card now; --force also passes the lane's daily caps
        st, card, how = (file_fix if p["kind"] == "fix" else file_trial)(env, p, a.dry_run, force=a.force)
        print(f"{p['id']} {p['kind']}:{p['target']} -> {card} ({how})")
        if not a.dry_run:
            mark(env, a.id, st, card=card, tier="review",
                 result=f"{p['kind']} card {card} on board {env.board} ({how})")
            print("data " + push_data(env, f"coach-apply: {a.id} carded"))
        return
    desc, run = plan(env, p)
    print(f"{p['id']} [{p.get('tier')}/{p.get('risk', '?')}] {p['kind']} {p['target']}: {p.get('title') or ''}")
    show_diff(*desc) if isinstance(desc, tuple) else print(" ", desc)
    if a.dry_run:
        print("dry run: nothing changed, proposal stays pending")
        return
    ev = run()
    print(ev["result"])
    mark(env, a.id, "applied", **ev)
    notify_inbox(env, p, "applied", ev["result"])
    print("data " + push_data(env, f"coach-apply: {a.id} applied"))


def cmd_inbox(env, a):
    """Re-post applied/canary improvements from the last N days to the Inbox (same keys: no duplicates)."""
    pull_data(env)
    cut = time.time() - a.days * 86400
    decided = {r.get("id") for r in inbox_decisions(env.data)}
    n = 0
    for p in cp.state(env.data).values():
        if p.get("status") not in ("applied", "canary") or p["id"] in decided:
            continue
        try:
            at = calendar.timegm(time.strptime(str(p.get("at", ""))[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            continue
        if at >= cut and notify_inbox(env, p, p["status"]):
            n += 1
            print(f"{p['id']:<13} {p['status']:<8} {p.get('title') or p['target']}")
    print(f"{n} improvement(s) posted to the Inbox")


def cmd_decide(env, a):
    """Same as pressing Keep / Not now / Never on the improvement's Inbox card (for after Review, which closes the
    card's buttons)."""
    pull_data(env)
    p = get(env, a.id)
    detail = apply_decision(env, p, a.choice)
    f = decisions_path(env.data)
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "a") as fh:
        fh.write(json.dumps({"id": a.id, "outcome": a.choice, "outcome_at": None, "source": "cli", "at": now_iso(),
                             "detail": detail}) + "\n")
    print(detail)
    print("data " + push_data(env, f"coach-apply: {a.id} {a.choice}"))


def cmd_reject(env, a):
    pull_data(env)
    p = get(env, a.id)
    mark(env, a.id, "rejected", note=a.note)
    print(f"{p['kind']} {p['target']} is on the coach's rejected list")
    print("data " + push_data(env, f"coach-apply: {a.id} rejected"))


def cmd_revert(env, a):
    pull_data(env)
    p = get(env, a.id)
    if p.get("status") not in ("applied", "canary"):
        raise SystemExit(f"{a.id} is {p.get('status')}; only applied/canary proposals can be reverted")
    res = revert_event(env, p)
    print(res)
    mark(env, a.id, "reverted", note=a.note or "reverted by hand", result=res)
    notify_inbox(env, p, "reverted", res)
    print("data " + push_data(env, f"coach-apply: {a.id} reverted"))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="coach-apply", description=__doc__.split("\n")[0])
    ap.add_argument("--no-push", action="store_true", help="commit locally, never push (tests)")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("list"); s.add_argument("--status", default="pending")
    s = sub.add_parser("show"); s.add_argument("id")
    s = sub.add_parser("apply"); s.add_argument("id"); s.add_argument("--dry-run", action="store_true")
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("reject"); s.add_argument("id"); s.add_argument("note", nargs="?", default="")
    s = sub.add_parser("revert"); s.add_argument("id"); s.add_argument("note", nargs="?", default="")
    s = sub.add_parser("decide"); s.add_argument("id"); s.add_argument("choice", choices=("keep", "not_now", "never"))
    s = sub.add_parser("auto"); s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("inbox"); s.add_argument("--days", type=int, default=7)
    a = ap.parse_args(argv)
    env = Env(push=not a.no_push)
    if not a.cmd:
        a.cmd, a.status = "list", "pending"
    try:
        if a.cmd == "auto":
            print(f"[coach-apply auto {now_iso()}]")
            auto(env, dry=a.dry_run)
            return 0
        {"list": cmd_list, "show": cmd_show, "apply": cmd_apply, "reject": cmd_reject, "revert": cmd_revert,
         "inbox": cmd_inbox, "decide": cmd_decide}[a.cmd](env, a)
    except ApplyError as e:
        raise SystemExit(str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
