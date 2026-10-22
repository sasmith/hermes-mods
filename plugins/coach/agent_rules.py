"""coach agent channel: watch the agent's own tool calls and hand it a short corrective hint.

Invisible to the user. `post_tool_call` feeds `observe()`; `transform_tool_result` calls `take()` and the hint is
attached to the tool result the model is about to read (a `coach_hint` key for JSON results, else an appended
`[coach]` line). `pre_llm_call` (once per user turn) calls `turn_start()`, which resets per-turn counters and
returns any hint that was still pending.

Each rule fires at most once per key per session, so a hint never repeats itself into noise.
The same observer also records the user-channel signals (subagent count, long jobs, Hermes bug signatures)
through the injected `signals` sink (core.SignalStore in production).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional

REPEAT_FAILS = 3          # same command failing N times -> stop and debug
POLL_WINDOW = 10          # last N tool calls ...
POLL_HITS = 3             # ... with this many sleep/poll calls -> background + notify
CHECKPOINT_TOOLS = 40     # tool calls in one turn without a reply -> summarize
LONG_JOB_S = 600          # a job this long belongs on the board

HINTS = {
    "repeat_failure": ("The same command has now failed {n} times: `{cmd}`. Stop retrying variations. Reproduce it "
                       "once in isolation, write down one hypothesis for the cause, test that hypothesis, and only "
                       "then change the code (skill: systematic-debugging)."),
    "poll_loop": ("You are polling with sleep/poll calls. Start the job with terminal(background=true, notify=true) "
                  "(or a Kanban card for anything over ~10 min) and end the turn; the completion notice wakes you."),
    "no_checkpoint": ("{n} tool calls this turn and the user has seen no update. Before more tool calls, write a "
                      "short state summary: what is done, what failed, what is next."),
    "live_checkout": ("You just changed the LIVE Hermes checkout ({path}) that running backends execute from. "
                      "Core work goes in a worktree: `~/.hermes/bin/hermes-core-wt new <slug>` -> edit/test/commit "
                      "there -> `hermes-core-wt ship <slug>`. Revert the change in the live tree if you can."),
}

SLEEP_RE = re.compile(r"\bsleep\s+(\d+(?:\.\d+)?)")
LOOP_SLEEP_RE = re.compile(r"\b(while|until|for)\b[^\n]*?(;|\n|\bdo\b)[\s\S]*?\bsleep\s+\d", re.I)
WATCH_RE = re.compile(r"(^|[;&|]\s*)watch\s+-n")
GIT_MUTATE_RE = re.compile(r"\bgit\b[^;&|]*?\b(commit|pull|reset|checkout|switch|merge|rebase|stash|am|apply|cherry-pick)\b(?!-)")
FILE_MUTATE_RE = re.compile(r"(\bsed\s+-i\b|\bperl\s+-pi\b|\bnpm\s+run\s+(pack|build)\b|\bhermes\s+desktop\b.*--build)")
# Output redirects: `>`, `>>`, `>|`, `N>`, `&>`. The target group is either an fd dup (`&1`, `&2`, `&-`) or a path.
REDIRECT_RE = re.compile(r"""(?<![-=<>])(?:&|\d*)>>?\|?\s*(&[\d-]*|"[^"]*"|'[^']*'|[^\s;&|()<>]+)""")
CD_RE = re.compile(r"""(?:^|[;&|(\n'"]\s*)cd\s+("[^"]*"|'[^']*'|[^\s;&|()'"]+)""")
GIT_C_RE = re.compile(r"""^git\s+-C\s+("[^"]*"|'[^']*'|\S+)""")
HERMES_INFRA_TOOLS = {
    "cross_session", "delegate_task", "session_search", "memory", "skill_manage", "skill_view", "cronjob",
    "process_manage", "send_message", "todo", "clarify", "tool_search", "tool_describe", "tool_call",
} | {f"kanban_{v}" for v in ("create", "show", "complete", "block", "comment", "heartbeat", "link",
                              "request_review", "request_changes", "attach", "attach_url", "attachments")}
TRACE_HERMES_RE = re.compile(r'File "[^"]*(hermes-agent|hermes_cli|tui_gateway|/agent/|/tools/|/gateway/)[^"]*\.py"')
EXC_NAME_RE = re.compile(r"^[A-Z][A-Za-z]+(Error|Exception)$")


def default_live_checkout() -> Optional[Path]:
    env = os.environ.get("COACH_LIVE_CHECKOUT")
    if env:
        return Path(env).expanduser().resolve()
    try:
        import hermes_cli  # type: ignore
        root = Path(hermes_cli.__file__).resolve().parents[1]
        return root if (root / ".git").exists() else None
    except Exception:
        return None


def _norm_cmd(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _as_dict(result: Any) -> Optional[dict]:
    if isinstance(result, dict):
        return result
    if isinstance(result, str) and result.lstrip().startswith("{"):
        try:
            d = json.loads(result)
            return d if isinstance(d, dict) else None
        except Exception:
            return None
    return None


def failed(tool: str, result: Any, status: Optional[str]) -> bool:
    if status in ("error", "timeout"):
        return True
    d = _as_dict(result)
    if not d:
        return False
    if tool == "terminal":
        code = d.get("exit_code")
        return code not in (None, 0)
    if tool == "execute_code":
        return bool(d.get("error")) or d.get("status") == "error"
    return d.get("success") is False or bool(d.get("error"))


def command_key(tool: str, args: dict) -> str:
    if tool == "terminal":
        return "terminal:" + _norm_cmd(str(args.get("command") or ""))[:400]
    if tool == "execute_code":
        return "execute_code:" + hashlib.sha1(str(args.get("code") or "").encode()).hexdigest()[:12]
    try:
        blob = json.dumps(args, sort_keys=True, default=str)[:400]
    except Exception:
        blob = str(args)[:400]
    return f"{tool}:{blob}"


def is_poll(tool: str, args: dict) -> bool:
    if tool == "terminal":
        cmd = str(args.get("command") or "")
        m = SLEEP_RE.search(cmd)
        return bool((m and float(m.group(1)) >= 2) or WATCH_RE.search(cmd))
    if tool == "process_manage":
        return str(args.get("action") or "") in ("poll", "log", "status")
    return False


def is_poll_loop(tool: str, args: dict) -> bool:
    return tool == "terminal" and bool(LOOP_SLEEP_RE.search(str(args.get("command") or "")))


def _inside(p: str, root: str) -> bool:
    try:
        rp = str(Path(p).expanduser().resolve())
    except Exception:
        rp = p
    return rp == root or rp.startswith(root + os.sep)


HEREDOC_RE = re.compile(r"""<<-?\s*(['"]?)([A-Za-z_][\w-]*)\1""")
SHELL_RUNNER_RE = re.compile(r"""(?:^|[\s;&|(])(?:(?:ba|z|da)?sh|eval)(?:\s+-\w*c\w*)?\s*$""")
QUOTED_RE = re.compile(r""""(?:[^"\\]|\\.)*"|'[^']*'""")
HOME_VAR_RE = re.compile(r"\$\{HOME\}|\$HOME\b")


def _strip_heredocs(cmd: str) -> str:
    """Drop heredoc bodies (they are stdin data, not commands), except when fed to a shell (`bash <<EOF`)."""
    out, pos = [], 0
    while True:
        m = HEREDOC_RE.search(cmd, pos)
        if not m:
            out.append(cmd[pos:])
            return "".join(out)
        nl = cmd.find("\n", m.end())
        if nl < 0:
            out.append(cmd[pos:])
            return "".join(out)
        line_start = cmd.rfind("\n", 0, m.start()) + 1
        feeds_shell = re.search(r"(?:^|[\s;&|(])(?:ba|z|da)?sh(?=\s|$)", cmd[line_start:m.start()]) is not None
        end = re.compile(r"^\t*" + re.escape(m.group(2)) + r"[ \t]*$", re.M).search(cmd, nl + 1)
        body_end = end.end() if end else len(cmd)
        out.append(cmd[pos:nl + 1])
        if feeds_shell:
            out.append(cmd[nl + 1:body_end])
        pos = body_end


def _mask_quoted(cmd: str) -> str:
    """Blank quoted strings that hold data (python -c code, echo text, commit messages).

    A quoted single token (a path like "$HOME/.hermes/hermes-agent") is kept; so is the script of
    `bash -c '...'` / `eval "..."`, which the shell really runs.
    """
    def repl(m: re.Match) -> str:
        body = m.group(0)[1:-1]
        if not re.search(r"\s", body) or SHELL_RUNNER_RE.search(cmd[:m.start()]):
            return m.group(0)
        return m.group(0)[0] + "_" + m.group(0)[-1]
    return QUOTED_RE.sub(repl, cmd)


def shell_code(cmd: str) -> str:
    """The parts of a terminal command the shell executes: $HOME expanded, heredoc bodies and quoted data blanked."""
    cmd = HOME_VAR_RE.sub(lambda _: str(Path.home()), cmd or "")
    cmd = re.sub(r"(?m)^[ \t]*#.*$", "", cmd)  # whole-line comments are notes, not commands
    return _mask_quoted(_strip_heredocs(cmd))


def redirect_writes_into(cmd: str, workdir: str, root: str) -> bool:
    """True when a shell output redirect in `cmd` writes a file inside `root`.

    fd dups (`2>&1`, `>&2`) never count; path targets count only when they resolve into the tree.
    Relative targets resolve against the last `cd` before the redirect, else `workdir`; with neither
    the cwd is unknown, so they don't count.
    """
    for m in REDIRECT_RE.finditer(cmd):
        target = m.group(1).strip("\"'")
        if not target or target.startswith("&") or target.startswith("$"):
            continue  # fd dup, or an unexpanded variable like $TMPDIR/x (never the live tree; $HOME is pre-expanded)
        t = os.path.expanduser(target)
        if not os.path.isabs(t):
            cds = [c.group(1).strip("\"'") for c in CD_RE.finditer(cmd[:m.start()])]
            base = os.path.expanduser(cds[-1]) if cds else (os.path.expanduser(workdir) if workdir else "")
            if base and not os.path.isabs(base) and workdir:
                base = os.path.join(os.path.expanduser(workdir), base)
            if not base or not os.path.isabs(base):
                continue
            t = os.path.join(base, t)
        if _inside(t, root):
            return True
    return False


def touches_live_checkout(tool: str, args: dict, checkout: Optional[Path]) -> Optional[str]:
    if not checkout:
        return None
    root = str(checkout)
    home_form = root.replace(str(Path.home()), "~", 1)
    if tool in ("write_file", "patch"):
        p = str(args.get("path") or "")
        if not p:
            return None
        try:
            rp = str(Path(p).expanduser().resolve())
        except Exception:
            rp = p
        return rp if rp == root or rp.startswith(root + os.sep) else None
    if tool == "terminal":
        cmd = shell_code(str(args.get("command") or ""))
        wd = str(args.get("workdir") or "")
        if mutation_in_tree(cmd, wd, root, home_form) or redirect_writes_into(cmd, wd, root):
            return root
    return None


def _segment(cmd: str, start: int) -> str:
    """The simple command starting at `start` (up to the next ; & | or newline)."""
    m = re.compile(r"[;&|\n]").search(cmd, start)
    return cmd[start:m.start() if m else len(cmd)]


def mutation_in_tree(cmd: str, workdir: str, root: str, home_form: str) -> bool:
    """True when a git mutation / in-place edit / build in `cmd` runs on the live tree.

    Each mutation is attributed to its own directory (`git -C X`, else the last `cd` before it, else
    `workdir`) or to a live-tree path inside its own simple command. A live path elsewhere in the command
    (e.g. a later `grep` of a file in the tree) doesn't count: that made `cd other-repo && git pull; grep x
    ~/.hermes/hermes-agent/f.py` look like a live-tree change."""
    def mentions(seg: str) -> bool:
        return root in seg or home_form in seg

    for m in GIT_MUTATE_RE.finditer(cmd):
        seg = _segment(cmd, m.start())
        c = GIT_C_RE.match(seg)
        d = _target_dir(cmd, m.start(), c.group(1) if c else "", workdir)
        if c and d is None and mentions(c.group(1)):
            return True
        if d and _inside(d, root):
            return True
        if d is None and not c and mentions(seg):
            return True
    for m in FILE_MUTATE_RE.finditer(cmd):
        seg = _segment(cmd, m.start())
        if mentions(seg):
            return True
        d = _target_dir(cmd, m.start(), "", workdir)
        if d and _inside(d, root):
            return True
    return False


GIT_HEAD_RE = re.compile(r"\bgit\b((?:\s+-C\s+(?:\"[^\"]*\"|'[^']*'|\S+))?)[^;&|\n]*?\s"
                         r"(commit|pull|reset|checkout|switch|merge|rebase|am|cherry-pick|revert)(?![-\w])")
PACK_RE = re.compile(r"(\bnpm\s+run\s+pack\b|\bhermes\s+desktop\b[^;&|\n]*--build)")
UPDATE_RE = re.compile(r"(?:^|[;&|(]\s*|\s)hermes\s+update\b")
ALLOW_LIVE_RE = re.compile(r"\bCOACH_ALLOW_LIVE=1\b")


def _target_dir(cmd: str, pos: int, explicit: str, workdir: str) -> Optional[str]:
    """Directory a command at `pos` runs in: `git -C X`, else the last `cd` before it, else workdir.
    None when it can't be known (a `cd $VAR` we can't expand)."""
    def norm(d: str, base: str) -> Optional[str]:
        d = d.strip("\"'")
        if "$" in d or "`" in d:
            return None
        d = os.path.expanduser(d)
        if not os.path.isabs(d):
            if not base:
                return None
            d = os.path.join(base, d)
        return os.path.normpath(d)
    base = os.path.expanduser(workdir) if workdir else ""
    for c in CD_RE.finditer(cmd[:pos]):
        nb = norm(c.group(1), base)
        base = nb if nb else ""
    if explicit:
        return norm(explicit, base)
    return base or None


def blocks_live_checkout(tool: str, args: dict, checkout: Optional[Path]) -> Optional[str]:
    """The live-checkout change that must be refused BEFORE it runs: anything that moves HEAD (every running
    backend then reports code skew and the Desktop restarts, killing in-flight turns in every chat) or
    repacks the running Hermes.app. Plain file edits stay a post-hoc hint. `COACH_ALLOW_LIVE=1` in the
    command is the escape hatch for a change the user explicitly asked to make in the live tree."""
    if not checkout or tool != "terminal":
        return None
    raw = str(args.get("command") or "")
    if ALLOW_LIVE_RE.search(raw):
        return None
    root = str(checkout)
    cmd = shell_code(raw)
    wd = str(args.get("workdir") or "")
    if any(not re.match(r"[^;&|\n]*--help", cmd[m.end():]) for m in UPDATE_RE.finditer(cmd)):
        return "`hermes update`"
    for m in GIT_HEAD_RE.finditer(cmd):
        if m.group(2) == "checkout" and re.match(r"[^;&|\n]*\s--(\s|$)", cmd[m.end():]):
            continue  # `git checkout -- <file>` discards live edits (what the post-hoc hint asks for); HEAD stays
        explicit = re.sub(r"^\s*-C\s+", "", m.group(1) or "")
        d = _target_dir(cmd, m.start(), explicit, wd)
        if d and _inside(d, root):
            return f"`git {m.group(2)}`"
    for m in PACK_RE.finditer(cmd):
        d = _target_dir(cmd, m.start(), "", wd)
        if d and _inside(d, root):
            return f"`{m.group(1).strip()}`"
    return None


def block_message(what: str, checkout: Path) -> str:
    path = str(checkout).replace(str(Path.home()), "~", 1)
    return (f"COACH LIVE-CHECKOUT GUARD: blocked {what} in {path}, the checkout every running Hermes backend executes. "
            "Moving its HEAD or repacking the app makes Desktop restart and kills in-flight turns in every chat "
            "(measured: ~10 Desktop quits on 2026-10-13, mostly from live-tree commits). Do core work in a worktree: "
            "`~/.hermes/bin/hermes-core-wt new <slug>` -> edit/test/commit there -> `hermes-core-wt ship <slug>`; "
            "hermes-sync deploys it when Desktop is idle. Only if the user explicitly asked for this exact change in "
            "the live tree, rerun with `COACH_ALLOW_LIVE=1` at the start of the command.")


def bug_signature(tool: str, result: Any, status: Optional[str], error_type: Optional[str],
                  error_message: Optional[str]) -> Optional[str]:
    """A normalized signature when this failure looks like a Hermes bug (not the agent's own mistake)."""
    text = str(error_message or "")
    raw = result if isinstance(result, str) else json.dumps(result, default=str) if result is not None else ""
    frame = TRACE_HERMES_RE.search(raw) or TRACE_HERMES_RE.search(text)
    if frame:
        last = re.findall(r"\n\s*([A-Z][A-Za-z]+(?:Error|Exception)[^\n]{0,120})", raw + "\n" + text)
        head = last[-1] if last else "traceback"
        return _normalize_sig(f"{tool}: {head}")
    if status == "error" and error_type and (EXC_NAME_RE.match(error_type) or error_type == "thread_missing_result"):
        return _normalize_sig(f"{tool}: {error_type}: {text}")
    if status == "error" and tool in HERMES_INFRA_TOOLS:
        d = _as_dict(result) or _leading_dict(result)
        if _guided_refusal(tool, d):
            return None
        return _normalize_sig(f"{tool}: {text or d.get('error') or 'failed'}")
    return None


def _leading_dict(result: Any) -> dict:
    """The JSON object a result starts with, when Hermes appended guidance text after it (repeat-failure hints)."""
    if isinstance(result, str) and result.lstrip().startswith("{"):
        try:
            d, _ = json.JSONDecoder().raw_decode(result.lstrip())
            return d if isinstance(d, dict) else {}
        except ValueError:
            pass
    return {}


def _guided_refusal(tool: str, d: dict) -> bool:
    """A designed validation refusal that tells the agent how to retry (the agent's mistake, not a Hermes bug).

    The memory tool refuses over-budget adds/patches/batches, zero-match or ambiguous old_text, empty content and
    scan hits on purpose, with the fix in the message, and the agent's next call recovers. Filing those as
    "Hermes bug" cards spent a fix-worker card on working-as-designed guards. Hermes-side memory failures still count:
    an unreadable file (read_failed), external drift (drift_backup) and the terminal per-turn retry cap
    (done=True: the guidance failed to get the agent through).
    """
    if tool != "memory" or d.get("success") is not False or d.get("done") or "drift_backup" in d:
        return False
    return "could not be read right now" not in str(d.get("error") or "")


def _normalize_sig(s: str) -> str:
    s = re.sub(r"(/[\w.@-]+)+", "<path>", s)
    s = re.sub(r"\b[0-9a-f]{6,}\b|\b\d+\b", "<n>", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:200]


def attach_hint(result: Any, hint: str) -> Any:
    """Put the hint where the model reads it without breaking JSON results for other consumers."""
    if isinstance(result, str):
        d = _as_dict(result)
        if d is not None:
            d["coach_hint"] = hint
            return json.dumps(d, ensure_ascii=False)
        return f"{result}\n\n[coach] {hint}"
    return result


class AgentCoach:
    def __init__(self, checkout: Optional[Path] = None, signals=None, clock: Callable[[], float] = time.monotonic):
        self.checkout = checkout
        self.signals = signals  # SignalStore-like: subagents(sid, n), long_job(sid, reason), bug(sid, sig, sample)
        self.clock = clock
        self.sessions: dict[str, dict] = {}

    def _s(self, sid: str) -> dict:
        s = self.sessions.get(sid)
        if s is None:
            s = self.sessions[sid] = {"turn": 0, "turn_tools": 0, "fails": {}, "recent": deque(maxlen=POLL_WINDOW),
                                      "fired": set(), "pending": [], "bg": {}, "ask": "",
                                      "turn_log": deque(maxlen=60), "loop": {"fails": []}}
        return s

    def _fire(self, s: dict, sid: str, rule: str, key: str, **fmt) -> None:
        k = f"{rule}:{key}"
        if k in s["fired"]:
            return
        s["fired"].add(k)
        s["pending"].append(HINTS[rule].format(**fmt))
        if self.signals:
            self.signals.log({"event": "agent_hint", "session_id": sid, "rule": rule})

    def turn_start(self, sid: str, user_message: Any = None) -> Optional[str]:
        s = self._s(sid)
        s["turn"] += 1
        s["turn_tools"] = 0
        s["turn_log"].clear()
        s["turn_t0"] = time.time()  # wall clock: the diff review asks git for commits made since the turn began
        if isinstance(user_message, str) and user_message.strip():
            s["ask"] = user_message[:2000]
        return self.take(sid)

    def queue_hint(self, sid: str, text: str) -> None:
        """A hint from a background judge: delivered with the next tool result or the next turn's context."""
        self._s(sid)["pending"].append(text[len("[coach] "):] if text.startswith("[coach] ") else text)

    def turn_end(self, sid: str, reply: str) -> None:
        """Remember the turn's final answer and tool log: the fix-loop judge reads them on the next user turn."""
        s = self._s(sid)
        s["loop"]["last_reply"] = reply[-4000:]
        s["loop"]["last_log"] = list(s["turn_log"])[-25:]

    def loop_state(self, sid: str) -> dict:
        return self._s(sid)["loop"]

    def turn_number(self, sid: str) -> int:
        return self._s(sid)["turn"]

    def turn_context(self, sid: str) -> tuple[str, list[str]]:
        """(latest user ask, this turn's tool log) for the reply check."""
        s = self._s(sid)
        return s["ask"], list(s["turn_log"])

    def turn_started(self, sid: str) -> float | None:
        """Wall-clock start of the session's current turn (None before the first turn)."""
        return self._s(sid).get("turn_t0")

    def take(self, sid: str) -> Optional[str]:
        s = self.sessions.get(sid)
        if not s or not s["pending"]:
            return None
        out = " ".join(s["pending"])
        s["pending"] = []
        return out

    def check_live(self, sid: str, tool: str, args: Any) -> None:
        """live_checkout rule for one call. Also run from transform_tool_result: core's sequential path fires
        transform BEFORE post_tool_call, so a hint queued only from observe() lands on the NEXT result."""
        if not sid:
            return
        hit = touches_live_checkout(tool, args if isinstance(args, dict) else {}, self.checkout)
        if hit:
            self._fire(self._s(sid), sid, "live_checkout", "session", path=hit.replace(str(Path.home()), "~", 1))

    def observe(self, sid: str, tool: str, args: Any, result: Any = None, status: Optional[str] = None,
                duration_ms: int = 0, error_type: Optional[str] = None, error_message: Optional[str] = None) -> None:
        if not sid:
            return
        args = args if isinstance(args, dict) else {}
        s = self._s(sid)
        s["turn_tools"] += 1
        now = self.clock()
        try:
            arg = next((str(args[k]) for k in ("command", "code", "path", "url", "query", "goal", "name")
                        if args.get(k)), json.dumps(args, default=str))
        except Exception:
            arg = ""
        s["turn_log"].append(f"-> {tool}({arg[:140]})")
        if failed(tool, result, status):
            s["turn_log"].append(f"   <- ERROR {str(result)[:120]}")

        # repeat_failure
        key = command_key(tool, args)
        if failed(tool, result, status):
            n = s["fails"].get(key, 0) + 1
            s["fails"][key] = n
            if n >= REPEAT_FAILS:
                shown = key.split(":", 1)[1][:120] if tool == "terminal" else tool
                self._fire(s, sid, "repeat_failure", key, n=n, cmd=shown)
        else:
            s["fails"].pop(key, None)

        # poll_loop
        poll = is_poll(tool, args)
        s["recent"].append(poll)
        if is_poll_loop(tool, args) or (poll and sum(s["recent"]) >= POLL_HITS):
            self._fire(s, sid, "poll_loop", f"t{s['turn']}" if not is_poll_loop(tool, args) else "loop")

        # no_checkpoint (once per turn)
        if s["turn_tools"] >= CHECKPOINT_TOOLS:
            self._fire(s, sid, "no_checkpoint", f"t{s['turn']}", n=s["turn_tools"])

        # live_checkout (once per session)
        self.check_live(sid, tool, args)

        if not self.signals:
            return
        # user-channel signals
        if tool == "delegate_task":
            tasks = args.get("tasks")
            n = len(tasks) if isinstance(tasks, list) and tasks else 1
            self.signals.add_subagents(sid, n)
        # Long job = it really ran (or was waited on) for 10+ min. A generous `timeout=` alone is not evidence:
        # agents set 600 s caps on 5-second commands, which made every such call look like board work.
        if tool == "terminal":
            if args.get("background"):
                d = _as_dict(result) or {}
                pid = str(d.get("session_id") or d.get("process_id") or d.get("pid") or "")
                if pid:
                    s["bg"][pid] = now
        if tool == "process_manage":
            pid = str(args.get("session_id") or args.get("process_id") or "")
            started = s["bg"].get(pid)
            if started is not None and now - started >= LONG_JOB_S:
                self.signals.long_job(sid, f"waiting on a background job for {int((now - started) // 60)} min")
        if duration_ms and duration_ms >= LONG_JOB_S * 1000:
            self.signals.long_job(sid, f"{tool} ran {int(duration_ms // 60000)} min")
        sig = bug_signature(tool, result, status, error_type, error_message)
        if sig:
            self.signals.bug(sid, sig, str(error_message or "")[:300])
