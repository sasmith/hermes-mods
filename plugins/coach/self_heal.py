"""self_heal: notice an agent/Hermes defect as early as possible: at the end of the turn that caused it, or on the
user's first message that reveals it.

Two entry paths, one schema:
- detect_complaint(prev_reply, user_text, tool_log=...) — pre_llm_call. Every real user message except pure acks
  goes to the aux judge `coach_defect` with the agent's previous reply and that turn's tool log: corrections, failed
  fixes, needless handoffs, frustration, Hermes/product misbehaviour, and nudges after a stalled turn.
- detect_admission(final_reply, tool_log=..., ask=...) — post_llm_call. Machine signals first, no judge (provider
  error reply, interrupted turn, one tool error repeated 3x); then, when the reply admits a mistake, hands the user a
  step, or the tool log shows errors, a turn-end audit by the same judge.
Both judges append the private user overlay (COACH_REPLY_PROFILE or <HERMES_HOME>/coach/reply_profile.md, shared
with reply_check) so they know the user's standing rules.

Both return {p_defect, kind, component, symptom, fingerprint, source} when p_defect >= threshold, else None.
The fingerprint is a stable hash of component + the symptom's content words, so one defect reported several ways
in one day is filed once (`Fingerprints.record`). Eval: eval/selfheal_incident_replay.py.

Mode: COACH_SELFHEAL=off|shadow|live (default shadow). shadow logs `selfheal_detect` and `selfheal_would_file` (the
full card it would file) to coach log.jsonl; live files the investigation card on the hub (see "Lane" below; the
README's "Self-heal lane" section). Fail-open everywhere.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional

COMPONENTS = ("agent", "desktop", "plugin", "cron", "kanban", "bot", "gateway", "provider", "browser", "bb", "other")

OOB_RE = re.compile(r"^\s*\[OUT-OF-BAND USER MESSAGE[^\]]*\]\s*(.*?)\s*\[/OUT-OF-BAND USER MESSAGE\]", re.S)
# Hermes-written user-role text: "[IMPORTANT: ...", "[System note: ...", kanban completion notices.
SYNTHETIC_RE = re.compile(r"^\s*(?:\[[A-Z][A-Za-z -]{2,40}(?::|\]| —)|You've reached the maximum|[✔✓✗✖⚠] )")
# Plugin-appended blocks that are not the user's words.
TRAILER_RE = re.compile(r"\n?\s*(?:\[user-steps\]|--- Attached Context ---|Session link\(s\) @session).*", re.S)

ADMIT_RE = re.compile(
    r"\b(my mistake|my (?:bad|fault|error)|that was (?:my|a) mistake|i (?:was wrong|missed|forgot|skipped|"
    r"overlooked|misread|didn'?t (?:check|load|verify|notice|read))|you'?re right[,:.]? (?:and|that|i|it)|i (?:should|"
    r"shouldn'?t) have|(?:a|the) bug in|is a bug|was a bug|bug (?:in|where)|work ?around|regress\w*|i told you "
    r"(?:otherwise|wrong)|i (?:said|reported|claimed) .{0,60} (?:but|when) (?:it|that) (?:wasn'?t|was not|isn'?t)|"
    r"(?:that|this) (?:was|is) (?:wrong|incorrect)|sorry)\b", re.I)

THRESHOLD = 0.6

# Pure acknowledgements: never a defect report, never judged.
ACK_RE = re.compile(
    r"^\s*(?:/\S+.*|go|yes|y|yep|yeah|ok(?:ay)?|k|do it|do both|done|thanks?|thank you|ty|sure|proceed|lgtm|"
    r"ship it|sounds good|perfect|great|nice|cool|approved?|go ahead|yes,? do it|👍)\W*$", re.I)

# Turn-end machine signals, computed from the reply and the coach's tool log (no judge). Same definitions as the
# replay's telemetry (api_err / interrupted / repeat): a provider error reply, an interrupted turn, or the same tool
# error three times in one turn.
API_ERR_RE = re.compile(r"not processed|API call failed|provider error|rate limit(?:ed)? .{0,40}(?:retry|exhaust)", re.I)
INTERRUPT_RE = re.compile(r"Operation interrupted|turn lease lost|Turn stopped|Tool execution cancelled", re.I)
ERR_LINE_RE = re.compile(r"(?:<-|->)\s*ERROR\b\s*(.*)")

COMPLAINT_SYS = """You watch a power user's chats with his AI agent system "Hermes" (the coding/ops agent, its Desktop
app, plugins, skills, cron jobs, Kanban task cards and workers, chat bots, gateways, model providers) and the products
Hermes builds and runs for him (e.g. a home assistant). You see the agent's previous final message, the tool log of
its previous turn, and the user's new message.

Score p_defect (0-1): the user's message reveals that the agent or Hermes did something it should not have, or failed
to do something it should have known to do, so a self-correcting system should root-cause it. Count:
- corrections of a wrong action, assumption, claim, interpretation, scope or choice (including "use X not Y", "you can
  do X via Y", "I said Z", "don't do that", "that's not what I asked"),
- reports that a claimed fix/build/deploy/change doesn't work, isn't visible, or regressed ("still broken", "nope",
  "I don't see it", "it's back"),
- objections to asks or handoffs the agent could have done itself ("you have ssh", "you buy it", "run it for me", "stop
  asking"), to re-asked questions, or to manual steps it gave him,
- frustration or "why did you / what went wrong / again / I told you",
- reports that Hermes itself (Desktop app, sidebar, panels, plugin, tool, cron, Kanban card state, session status,
  gateway, bot) or a product Hermes built and runs for him (his home assistant, its apps, pods, watch/phone clients,
  hub, feeds, Inbox) misbehaved, lost data, shows wrong state or did the wrong thing,
- requests to fix or root-cause the agent's or Hermes's own behavior,
- a bare nudge ("resume", "what's going on", "are you stuck", "status?") when the previous turn did not end in a real
  answer: it was interrupted, errored, stopped mid-work, went silent after many tool calls, or promised work that never
  came. A nudge after a complete answer is low. A previous message that merely looks cut off may have been clipped
  for length before you saw it; that alone is not a defect.
Do NOT count (low, <0.3): brand-new requests or feature asks with no failure; design taste or wording preferences the
agent could not have predicted; answers to the agent's legitimate questions; approvals; status questions with no sign
of a problem; problems in third-party services or the outside world that Hermes neither caused nor should have
prevented (kind "external"); bugs in research or demo code that ARE the task he just gave the agent.

kind: agent_fault (how the agent worked: claims, asks, verification, instruction-following, scope), hermes_bug
(Hermes or a product it built/runs misbehaved), external, none.
component: one of agent, desktop, plugin, cron, kanban, bot, gateway, provider, browser, bb (his home-assistant
product), other.
symptom: <=10 words naming what is wrong, neutral words, no names (e.g. "asked user to run terminal command").

Return ONLY JSON: {"p_defect": p, "kind": "...", "component": "...", "symptom": "..."}"""

# Second first-stage opinion: the narrower wording (only Hermes itself, product bugs reported as new issues score low).
# Averaging it with COMPLAINT_SYS cuts false alarms on product bug reports without losing the agent-fault catches.
COMPLAINT_STRICT_SYS = """You watch a power user's chats with his AI agent system "Hermes" (the coding/ops agent, its Desktop
app, plugins, skills, cron jobs, Kanban task cards and workers, chat bots, gateways, model providers) and the products
Hermes builds and runs for him (e.g. a home assistant). You see the agent's previous final message, the tool log of
its previous turn, and the user's new message.

Score p_defect (0-1): the user's message reveals that the agent or Hermes did something it should not have, or failed
to do something it should have known to do, so a self-correcting system should root-cause it. Count:
- corrections of a wrong action, assumption, claim, interpretation, scope or choice (including "use X not Y", "you can
  do X via Y", "I said Z", "don't do that", "that's not what I asked"),
- reports that a claimed fix/build/deploy/change doesn't work, isn't visible, or regressed ("still broken", "nope",
  "I don't see it", "it's back"),
- objections to asks or handoffs the agent could have done itself ("you have ssh", "you buy it", "run it for me", "stop
  asking"), to re-asked questions, or to manual steps it gave him,
- frustration or "why did you / what went wrong / again / I told you",
- reports that Hermes itself (Desktop app, sidebar, panels, plugin, tool, cron, Kanban card state, session status,
  gateway, bot) misbehaved, lost data or shows wrong state,
- requests to fix or root-cause the agent's or Hermes's own behavior,
- a bare nudge ("resume", "what's going on", "are you stuck", "status?") when the previous turn did not end in a real
  answer: it was interrupted, errored, stopped mid-work, went silent after many tool calls, or promised work that never
  came. A nudge after a complete answer is low.
Do NOT count (low, <0.3): brand-new requests or feature asks; design taste or wording preferences the agent could not
have predicted; answers to the agent's legitimate questions; approvals; status questions with no sign of a problem;
bugs in his products or the outside world that he reports as a NEW issue with no sign the agent's or Hermes's own work
caused them or should have prevented them (score those p_defect low and set kind "product_bug").

kind: agent_fault (how the agent worked: claims, asks, verification, instruction-following, scope), hermes_bug
(Hermes software/infra misbehaved), product_bug (see above), none.
component: one of agent, desktop, plugin, cron, kanban, bot, gateway, provider, browser, bb (his home-assistant
product), other.
symptom: <=10 words naming what is wrong, neutral words, no names (e.g. "asked user to run terminal command").

Return ONLY JSON: {"p_defect": p, "kind": "...", "component": "...", "symptom": "..."}"""

AUDIT_SYS = """You audit an AI agent's turn right as it ends, for a demanding power user who wants the agent to
self-serve, verify on his real path, and fix its own mistakes. You see his request, the turn's tool log (errors marked
ERROR) and the agent's final message. Judge only from that evidence.

Score p_defect (0-1): this turn shows a defect in the agent or its system that should be root-caused. High (0.8+):
- self-admission: the agent says it was wrong, made a mistake, missed or skipped something, told him something untrue,
  or found a bug in Hermes or its tools while doing something else (even if it fixed the symptom);
- unhandled_failure: the log shows errors, aborts, repeated failures or an empty/partial result that the final message
  hides, glosses over or leaves unresolved, or the message itself is an error / empty after many tool calls;
- needless_handoff: it asks him to do or decide something it could do itself (shell, ssh, sudo via a user-terminal
  tool, browser, credential stores), re-asks something answered, or hands him an infeasible step;
- contradiction: the message contradicts the tool log or itself (claims a check that never ran).
Low (<0.3): honest status reports that state plainly what is unverified or failed and what happens next; a bug that IS
the task he gave (root cause + fix report); apologies with no defect; external outages it surfaces clearly; legitimate
questions about his preferences.

component: agent, desktop, plugin, cron, kanban, bot, gateway, provider, browser, bb, other.
symptom: <=10 words naming what is wrong, neutral words, no names.

Return ONLY JSON: {"p_defect": p, "kind": "admission|unhandled_failure|needless_handoff|contradiction|none",
"component": "...", "symptom": "..."}"""


def clean_user(text) -> str:
    if isinstance(text, list):
        parts = []
        for p in text:
            if isinstance(p, dict):
                if p.get("type") in ("image_url", "image", "input_image"):
                    parts.append("[screenshot]")
                elif p.get("text"):
                    parts.append(str(p["text"]))
        text = " ".join(parts)
    t = str(text or "")
    m = OOB_RE.match(t)
    if m:
        t = m.group(1)
    t = TRAILER_RE.sub("", t)
    return t.strip()


HANDOFF_RE = re.compile(
    r"\b(?:you(?:'ll| will)? (?:need|have) to|run (?:this|these|the following)|paste|in your terminal|can you|could you|"
    r"please (?:run|open|tap|click|log ?in|sign ?in|approve|check|confirm|enable|install)|let me know|once you|"
    r"reply with|do you want|should i|want me to|on your (?:phone|mac|iphone|end)|your call|needs? you|"
    r"for you to|waiting on you|manual step)\b", re.I)


def prefilter(user_text) -> bool:
    """Worth judging: any real user message except pure acknowledgements, slash commands and Hermes-written text.
    (The v1 complaint regex missed corrections like "you buy the credits" and nudges like "whats going on"; one cheap
    judge call per real user turn is the price of recall.)"""
    t = clean_user(user_text)[:2000]
    if not t or SYNTHETIC_RE.match(t):
        return False
    return not ACK_RE.match(t[:200])


def _err_lines(tool_log) -> list:
    return [ln for ln in (tool_log or []) if isinstance(ln, str) and ERR_LINE_RE.search(ln)]


def admission_prefilter(reply: str, tool_log=None) -> bool:
    r = (reply or "")[:6000]
    return bool(ADMIT_RE.search(r) or HANDOFF_RE.search(r) or _err_lines(tool_log)
                or (not r.strip() and tool_log))


def machine_signal(final_reply: str, tool_log=None) -> Optional[dict]:
    """Turn-end telemetry, no judge: a provider-error reply, an interrupted turn, or one tool error repeated 3x."""
    r = (final_reply or "")[:400]
    log = [str(x) for x in (tool_log or [])]
    if API_ERR_RE.search(r):
        v = {"p_defect": 0.9, "kind": "api_error", "component": "provider", "symptom": "turn failed with provider error"}
    elif INTERRUPT_RE.search(r) or any(INTERRUPT_RE.search(x) for x in log[-10:]):
        v = {"p_defect": 0.9, "kind": "interrupted", "component": "gateway",
             "symptom": "turn interrupted before finishing"}
    else:
        errs = [ERR_LINE_RE.search(x).group(1)[:80] for x in _err_lines(log)]
        rep = next((e for e in set(errs) if e and errs.count(e) >= 3), None)
        if not rep:
            return None
        v = {"p_defect": 0.85, "kind": "repeat_failure", "component": "agent", "symptom": "same tool error repeated"}
    return {**v, "fingerprint": fingerprint(v["component"], v["symptom"]), "source": "signal"}


_STOP = set("a an the to of in on for and or is are was were be been it its this that with not no by at as from "
            "user users his her he she they them i you we me my your our does do did doesnt didnt isnt wasnt "
            "after before when while again still".split())


def _stem(w: str) -> str:
    s = re.sub(r"(?:ing|ed|s)$", "", w) if len(w) > 4 else w
    return s.rstrip("e") or w


def fingerprint(component: str, symptom: str) -> str:
    """Stable id for one defect: component + sorted content words of the symptom (stemmed crudely), so rewordings
    like "sidebar sessions reshuffle" / "session sidebar reshuffles" collide."""
    words = re.findall(r"[a-z0-9]+", (symptom or "").lower())
    stems = sorted({_stem(w) for w in words if w not in _STOP and len(w) > 1})
    comp = component if component in COMPONENTS else "other"
    key = comp + ":" + " ".join(stems)
    return comp + "-" + hashlib.sha1(key.encode()).hexdigest()[:10]


def parse(raw: str) -> Optional[dict]:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        comp = str(d.get("component") or "other").strip().lower()
        return {"p_defect": max(0.0, min(1.0, float(d.get("p_defect") or 0))),
                "component": comp if comp in COMPONENTS else "other",
                "symptom": str(d.get("symptom") or "").strip()[:120],
                "kind": str(d.get("kind") or "").strip().lower()[:30]}
    except Exception:
        return None


def profile_text() -> str:
    """The private user-environment overlay shared with reply_check (COACH_REPLY_PROFILE or
    <HERMES_HOME>/coach/reply_profile.md): his standing rules and what his agents can reach, so the judge can tell a
    lazy handoff or a rule break from a legitimate one. '' when absent."""
    p = os.environ.get("COACH_REPLY_PROFILE")
    if not p:
        try:
            from hermes_constants import get_hermes_home  # type: ignore
            home = Path(get_hermes_home())
        except Exception:
            home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
        p = str(home / "coach" / "reply_profile.md")
    try:
        return Path(p).read_text()[:6000]
    except Exception:
        return ""


def _with_profile(system: str) -> str:
    prof = profile_text()
    return system + (f"\n\nUSER'S STANDING RULES AND ENVIRONMENT (breaking these is a defect):\n{prof}"
                     if prof.strip() else "")


def _log_text(tool_log, n=25, chars=2500) -> str:
    return "\n".join(str(x)[:200] for x in (tool_log or [])[-n:])[:chars] or "(no tool calls)"


def complaint_prompt(prev_reply: str, user_text, tool_log=None, context: str = "") -> str:
    """`context` (digest backstop only): the session title and earlier user messages, for turns whose previous agent
    reply is unknown. Without it a context-free report (\"why didnt this work [screenshot]\") reads as a new issue."""
    ctx = f"SESSION CONTEXT (from a digest; agent replies are not available):\n{context[:1500]}\n\n" if context else ""
    prev = prev_reply or ("(not available)" if context else "(none: first turn)")
    return (f"{ctx}AGENT'S PREVIOUS FINAL MESSAGE:\n{prev[-3000:]}\n\n"
            f"TOOL LOG OF THE AGENT'S PREVIOUS TURN:\n{_log_text(tool_log)}\n\n"
            f"USER'S NEW MESSAGE:\n{clean_user(user_text)[:2000]}")


def admission_prompt(final_reply: str, tool_log=None, ask="") -> str:
    return (f"USER'S REQUEST:\n{clean_user(ask)[:1500] or '(unknown)'}\n\n"
            f"TOOL LOG THIS TURN:\n{_log_text(tool_log, 30, 3500)}\n\n"
            f"AGENT'S FINAL MESSAGE:\n{(final_reply or '(empty)')[-4000:]}")


def _verdict(v: Optional[dict], source: str, threshold: float) -> Optional[dict]:
    """A hit is an agent/Hermes defect at or above threshold; outside-world problems (kind external) are not."""
    if not v or v["p_defect"] < threshold or v.get("kind") in ("external", "product_bug", "none"):
        return None
    return {**v, "fingerprint": fingerprint(v["component"], v["symptom"]), "source": source}


GATE = 0.6            # first-stage score that escalates to the second opinion and the confirm model
AUDIT_THRESHOLD = 0.9  # turn-end audit admissions file only at this (confirmed) score


def _score(v: Optional[dict]) -> float:
    return 0.0 if not v or v.get("kind") in ("external", "product_bug", "none") else v["p_defect"]


def _ask(llm: Callable[[str, str], str], system: str, prompt: str, tries: int = 2) -> Optional[dict]:
    """One judge stage, retried once when the call fails or the answer does not parse (an empty flash answer is
    common). None = no opinion, which the cascade leaves out; it is never a 'no' vote."""
    for _ in range(tries):
        try:
            v = parse(llm(system, prompt))
        except Exception:
            v = None
        if v:
            return v
    return None


def complaint_verdict(prev_reply: str, user_text, llm: Callable[[str, str], str], tool_log=None,
                      confirm: Optional[Callable[[str, str], str]] = None, context: str = "") -> Optional[dict]:
    """The complaint cascade, before the threshold: judge (COMPLAINT_SYS); below GATE stop there. Otherwise a second
    flash opinion (COMPLAINT_STRICT_SYS) and, when configured, the confirm model on COMPLAINT_SYS; p_defect = the mean
    of the stage scores (a non-defect kind scores 0; a stage with no answer after a retry is left out, since counting
    it as 0 halved real catches). Component/symptom come from the strongest stage."""
    prompt = complaint_prompt(prev_reply, user_text, tool_log, context)
    v1 = _ask(llm, _with_profile(COMPLAINT_SYS), prompt)
    if not v1:
        return None
    s1 = _score(v1)
    if s1 < GATE:
        return {**v1, "p_defect": s1, "stages": [s1]}
    v2 = _ask(llm, _with_profile(COMPLAINT_STRICT_SYS), prompt)
    stages, best = [s1] + ([_score(v2)] if v2 else []), v1
    if confirm:
        v3 = _ask(confirm, _with_profile(COMPLAINT_SYS), prompt)
        if v3:
            stages.append(_score(v3))
        best = v3 if v3 and _score(v3) > 0 else best
    p = round(sum(stages) / len(stages), 3)
    return {**best, "kind": best.get("kind") if _score(best) > 0 else v1.get("kind"), "p_defect": p,
            "stages": stages}


def detect_complaint(prev_reply: str, user_text, llm: Optional[Callable[[str, str], str]] = None,
                     threshold: float = THRESHOLD, tool_log=None,
                     confirm: Optional[Callable[[str, str], str]] = None, context: str = "") -> Optional[dict]:
    """pre_llm_call path: prefilter, then the cascade (complaint_verdict) on (previous reply + its turn's tool log,
    user message). Returns {p_defect, kind, component, symptom, fingerprint, source, stages} or None. Fail-open."""
    try:
        if not prefilter(user_text):
            return None
        return _verdict(complaint_verdict(prev_reply, user_text, llm or aux_judge, tool_log, confirm, context),
                        "complaint", threshold)
    except Exception:
        return None


def audit_verdict(final_reply: str, llm: Callable[[str, str], str], tool_log=None, ask="",
                  confirm: Optional[Callable[[str, str], str]] = None) -> Optional[dict]:
    """The turn-end audit, before the threshold: judge; an admission at >= GATE goes to the confirm model (when
    configured), whose verdict replaces it. Only AUDIT_KINDS count; other kinds score 0."""
    system, prompt = _with_profile(AUDIT_SYS), admission_prompt(final_reply, tool_log, ask)
    v = parse(llm(system, prompt))
    if v and confirm and v.get("kind") in AUDIT_KINDS and v["p_defect"] >= GATE:
        v = parse(confirm(system, prompt))
    if not v:
        return None
    return v if v.get("kind") in AUDIT_KINDS else {**v, "p_defect": 0.0}


def detect_admission(final_reply: str, llm: Optional[Callable[[str, str], str]] = None,
                     threshold: float = AUDIT_THRESHOLD, tool_log=None, ask="",
                     confirm: Optional[Callable[[str, str], str]] = None) -> Optional[dict]:
    """post_llm_call path: machine signals first (no judge), then a turn-end audit when the reply admits a mistake or
    hands the user a step, or the turn's tool log shows errors. Only self-admissions count from the audit: the other
    audit kinds (unhandled failure, handoff, contradiction) cost more false alarms than they catch on the replay set.
    Fail-open."""
    try:
        if not isinstance(final_reply, str):
            return None
        sig = machine_signal(final_reply, tool_log)
        if sig:
            return sig
        if not admission_prefilter(final_reply, tool_log):
            return None
        return _verdict(audit_verdict(final_reply, llm or aux_judge, tool_log, ask, confirm), "admission", threshold)
    except Exception:
        return None


AUDIT_KINDS = ("admission",)


def confirm_judge() -> Optional[Callable[[str, str], str]]:
    """The optional second-stage judge: aux task `coach_defect_confirm` when configured
    (auxiliary.coach_defect_confirm.{provider,model}) or COACH_DEFECT_CONFIRM_MODEL is set; else None."""
    model = os.environ.get("COACH_DEFECT_CONFIRM_MODEL", "")
    provider = os.environ.get("COACH_DEFECT_CONFIRM_PROVIDER", "")
    if not model:
        try:
            from hermes_cli.config import load_config  # type: ignore
            c = (load_config().get("auxiliary") or {}).get("coach_defect_confirm") or {}
            model, provider = c.get("model") or "", c.get("provider") or ""
        except Exception:
            return None
    if not model:
        return None

    def judge(system: str, prompt: str) -> str:
        from agent.auxiliary_client import call_llm  # type: ignore
        resp = call_llm(task="coach_defect_confirm", provider=provider or None, model=model,
                        messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                        max_tokens=400, temperature=0, timeout=60)
        return (getattr(resp.choices[0].message, "content", None) or "").strip()
    return judge


def user_facing(platform: str = "", parent_session_id: str = "") -> bool:
    """Only the user's own chats: not cron runs, subagents (their 'user' is the parent agent) or Kanban workers
    (their first message is a card body written by an agent)."""
    if (platform or "").lower() in ("cron", "kanban", "webhook", "batch"):
        return False
    if parent_session_id or os.environ.get("HERMES_KANBAN_TASK"):
        return False
    return True


def mode() -> str:
    """COACH_SELFHEAL env, else `mode` in <coach>/selfheal-config.json (so a cron scheduler or gateway needs no env
    change), else shadow."""
    m = os.environ.get("COACH_SELFHEAL", "").strip().lower()
    if not m:
        try:
            m = str(json.loads((coach_root() / "selfheal-config.json").read_text()).get("mode") or "").lower()
        except Exception:
            m = ""
    return m if m in ("off", "shadow", "live") else "shadow"


def threshold() -> float:
    try:
        return float(os.environ.get("COACH_SELFHEAL_THRESHOLD", THRESHOLD))
    except ValueError:
        return THRESHOLD


def audit_threshold() -> float:
    try:
        return float(os.environ.get("COACH_SELFHEAL_AUDIT_THRESHOLD", AUDIT_THRESHOLD))
    except ValueError:
        return AUDIT_THRESHOLD


def aux_judge(system: str, prompt: str) -> str:
    """Aux task `coach_defect` (auxiliary.coach_defect.{provider,model})."""
    from agent.auxiliary_client import call_llm  # type: ignore
    kw = {}
    if os.environ.get("COACH_DEFECT_MODEL"):
        kw = {"provider": os.environ.get("COACH_DEFECT_PROVIDER") or None, "model": os.environ["COACH_DEFECT_MODEL"]}
    resp = call_llm(task="coach_defect", messages=[{"role": "system", "content": system},
                                                  {"role": "user", "content": prompt}],
                    max_tokens=1500, temperature=0, timeout=30, **kw)
    return (getattr(resp.choices[0].message, "content", None) or "").strip()


class Fingerprints:
    """<coach>/selfheal.json: fingerprint -> {first, last, count, sessions, day_count}. `seen` = already recorded
    today (UTC), so one defect becomes one filing per day however many times it is reported."""

    def __init__(self, root: Path, clock: Callable[[], float] = time.time):
        self.path = Path(root) / "selfheal.json"
        self.clock = clock

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return {}

    def record(self, hit: dict, session_id: str = "") -> bool:
        """Record a hit; True when it is the first time this fingerprint is seen today."""
        d = self._read()
        now = self.clock()
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        fp = hit["fingerprint"]
        e = d.get(fp) or {"first": now, "count": 0, "sessions": [], "component": hit.get("component"),
                          "symptom": hit.get("symptom")}
        new = e.get("day") != day
        e.update(last=now, day=day, count=e["count"] + 1)
        if session_id and session_id not in e["sessions"]:
            e["sessions"] = (e["sessions"] + [session_id])[-20:]
        d[fp] = e
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1))
        os.replace(tmp, self.path)
        return new


def handle(hit: Optional[dict], session_id: str, store, evidence: str = "", m: Optional[str] = None,
           filer: Optional[Callable] = None, platform: str = "") -> Optional[dict]:
    """Log a detection and hand a new fingerprint (first time today) to the lane: `file_defect` logs
    `selfheal_would_file` in shadow mode and files the investigation card on the hub in live mode."""
    if not hit:
        return None
    m = m or mode()
    new = Fingerprints(store.root).record(hit, session_id)
    rec = {"event": "selfheal_detect", "mode": m, "session_id": session_id, "source": hit["source"],
           "p": hit["p_defect"], "component": hit["component"], "symptom": hit["symptom"],
           "fingerprint": hit["fingerprint"], "new": new, "evidence": (evidence or "")[:300]}
    store.log(rec)
    if m == "live" and new:
        q = Path(store.root) / "selfheal_queue.jsonl"
        with open(q, "a") as fh:
            fh.write(json.dumps({**rec, "ts": time.time()}) + "\n")
    if new and m in ("shadow", "live"):
        d = {**hit, "session_id": session_id, "quote": (evidence or "")[:500], "host": host_name(),
             "platform": platform, "at": time.time(), "kind": hit.get("kind") or hit["source"]}
        try:
            (filer or file_defect)(d, store=store, m=m)
        except Exception as exc:  # fail-open: a filing problem never breaks a turn
            store.log({"event": "selfheal_file_error", "fingerprint": hit["fingerprint"], "error": str(exc)[:300]})
    return rec


# ════════════════════════════════════════════════════════════════════════════════════════════════════════════
# Lane: defect -> investigation card (hub) -> ONE Inbox proposal (What happened / Why / Plan) -> Do it -> fix card
#
#   file_defect(d)    any entry path (complaint, admission, machine signal, digest backstop, monitors). On the hub it
#                     decides locally; elsewhere (live) it pipes `d` to `self_heal.py file` on the hub over ssh, so
#                     dedupe, recurrence, suppression and budget are decided in one place. Shadow logs
#                     `selfheal_would_file` with the full card args and creates nothing.
#   investigation     card `selfheal:<fp>` for generation 1, `selfheal:<fp>:r<gen>` after (one monotonic counter per
#                     fingerprint over refiles AND recurrences; kanban idempotency returns done cards too), model
#                     claude-opus-5-5, 1 h. Investigates LIVE (kanban.db, logs, processes, git log), writes the
#                     five-whys chain, posts Inbox type `selfheal` (Do it / Not now / Never) under key selfheal:<fp>.
#   Do it             -> file_fix (hub cron sync, or `self_heal.py fix <fp>`): fix card `selfheal:<fp>[:r<gen>]:fix` (red-then-green regression test, ship,
#                     verify live, re-post the same key as `selfheal-result` "Fixed: …").
#   sync (cron)       reads Inbox outcomes every 5 min: Do it (a resolve) files the fix card, Not now snoozes the
#                     fingerprint 14 days, Never suppresses it. It also reconciles against the board: a `filed`/`approved`
#                     fingerprint whose card is done/archived/gone with no proposed/fixed event becomes `stale`
#                     (selfheal_stale), so the next detection refiles it under a new generation.
#   backstop (cron)   runs the complaint detector over new user turns in the coach digests (chats where the
#                     in-process hook did not fire).
#   budget            engine_budget lane `selfheal`: $25/UTC day (its own cap, outside the $20 engine total), <= 3
#                     running cards, <= 1 filing per fingerprint per UTC day. Over the $ cap the card is created and
#                     scheduled to the next UTC midnight; at 3 running it is scheduled 1 h out.
# Events (coach log.jsonl, read by coach_weekly's self-heal section): selfheal_would_file, selfheal_filed,
# selfheal_recurrence, selfheal_skip, selfheal_proposed, selfheal_approved, selfheal_fixed, selfheal_reverted,
# selfheal_not_defect, selfheal_declined.
# ════════════════════════════════════════════════════════════════════════════════════════════════════════════

MODEL = "claude-opus-5-5"
INVESTIGATION_RUNTIME = 3600
FIX_RUNTIME = 7200
INVESTIGATION_BUDGET = 8
FIX_BUDGET = 15
USD_DAY = 25.0
MAX_RUNNING = 3
SNOOZE_DAYS = 14
NOT_DEFECT_DAYS = 7
SKILLS = ("five-whys", "hermes-mods-repo")
BB_SKILLS = ("five-whys", "bb-self-fix")
FIX_SKILLS = ("five-whys", "hermes-mods-repo", "systematic-debugging")
CREATED_BY = "self-heal"
INBOX_TYPE = "selfheal"
RESULT_TYPE = "selfheal-result"
SELF = "~/code/hermes-mods/plugins/coach/self_heal.py"  # how cards and Inbox actions call this file on the hub
FP_RE = re.compile(r"^[a-z]+-[0-9a-f]{8,}$")


def host_name() -> str:
    return os.uname().nodename.split(".")[0]


def coach_root() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        return Path(get_hermes_home()) / "coach"
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / "coach"


def lane_config() -> dict:
    """<coach>/selfheal-config.json (private, per host): {"hub_host": "<hostname of the hub>", "hub_ssh":
    "<user@addr>", "board": "<hub board>", "bb_board": "bb"}. Env overrides: COACH_SELFHEAL_HUB (ssh target),
    COACH_SELFHEAL_IS_HUB=1. With no hub configured every host files locally."""
    try:
        cfg = json.loads((coach_root() / "selfheal-config.json").read_text())
    except Exception:
        cfg = {}
    if os.environ.get("COACH_SELFHEAL_HUB"):
        cfg["hub_ssh"] = os.environ["COACH_SELFHEAL_HUB"]
    return cfg


def hub_ssh_argv(cfg: dict) -> list:
    """ssh argv prefix to the hub. Pins the key (`hub_ssh_key` in the config, default ~/.ssh/id_ed25519 when it
    exists) with IdentitiesOnly: a launchd job's default identities differ from the shell's (no agent, other keys
    offered first), and the hub then refuses it preauth while the same host's interactive ssh works."""
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    kp = Path(os.path.expanduser(cfg.get("hub_ssh_key") or "~/.ssh/id_ed25519"))
    if cfg.get("hub_ssh_key") or kp.exists():
        argv += ["-i", str(kp), "-o", "IdentitiesOnly=yes"]
    return argv


def is_hub(cfg: Optional[dict] = None) -> bool:
    cfg = lane_config() if cfg is None else cfg
    if os.environ.get("COACH_SELFHEAL_IS_HUB") in ("1", "true"):
        return True
    if not cfg.get("hub_ssh"):
        return True  # no hub configured: file locally
    return bool(cfg.get("hub_host")) and host_name() == cfg["hub_host"]


def route(d: dict, cfg: Optional[dict] = None) -> dict:
    """Board + skills. Baby Bear product defects go to the bb board with bb-self-fix; everything else to the hub's
    default board (Hermes / harness)."""
    cfg = lane_config() if cfg is None else cfg
    if d.get("component") == "bb":
        return {"board": cfg.get("bb_board") or "bb", "skills": list(BB_SKILLS), "bb": True}
    return {"board": cfg.get("board") or None, "skills": list(SKILLS), "bb": False}


def _pt(ts: float) -> str:
    import datetime
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.fromtimestamp(ts or 0, ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts or 0))


# Where to look, per component. The replay of 202 incidents showed a transcript-only investigator almost never fixes
# surface provenance (C10), Kanban state/routing (C11) or process lifecycle (C12): those need the live systems.
CHECKLIST = {
    "kanban": ("C11 Kanban state machine / routing",
               "- `sqlite3 -readonly ~/.hermes/kanban.db` (and `~/.hermes/kanban/boards/*/kanban.db`): the card's "
               "`tasks` row (status, assignee, board, idempotency_key, created_by), `task_events` in order "
               "(kind + payload: claimed/blocked/scheduled/unblocked/reclaimed/timed_out and their reasons), "
               "`task_runs` (outcome, error, worker pid, session_id).\n"
               "- Compare what the user was told (quote) with the event log at that minute: which writer moved the "
               "state, and which session/board it routed to.\n"
               "- Cross-session leaks: is the card/notice the chat talked about OWNED by this chat (tasks.session_id, "
               "created_by) or did it arrive via a subscription (kanban_notify_subs) or a board-wide query? "
               "Duplicate cards (same title, no idempotency_key)? A worker that parked David-facing steps in a "
               "comment or a timer instead of a needs_input block?\n"
               "- `git log -S'<event kind or state>' -- hermes_cli/kanban*` in ~/.hermes/hermes-agent for the code "
               "that writes it; the dispatcher log `~/.hermes/logs/kanban*.log` / `agent.log` at that time."),
    "desktop": ("C10 surface provenance / duplicated state",
                "- Which surface showed it (sidebar row, Inbox card, toast, panel, status chip) and which writer "
                "produced it: grep the exact string in ~/.hermes/hermes-agent/apps/desktop and "
                "~/code/hermes-mods/desktop-plugins; `git log --since` on that file.\n"
                "- The backing state: the REST endpoint or db row the surface renders (state.db sessions row, inbox "
                "items, kanban task) at the reported time; two writers or a stale cache = provenance gap.\n"
                "- Do not stop at the renderer: the wrong row/badge/card is usually right for the data it got. Follow "
                "the field it keys on (source, owner, status, origin/test flag) back to the code path that WROTE it "
                "(e.g. a create path that hardcodes `source`, test traffic with no origin tag) and cite that file:line.\n"
                "- On a Mac, the Desktop log (`~/Library/Logs/Hermes/` / `~/.hermes/logs/desktop.log`) via the "
                "mac diag command if configured; else the coach digest of the session."),
    "plugin": ("C10/C12 plugin behaviour",
               "- `git -C ~/code/hermes-mods log --since=7.days -- plugins/<name> desktop-plugins/<name>`: what "
               "changed just before the report.\n"
               "- `grep -n '<plugin>' ~/.hermes/logs/errors.log` around the time; hook exceptions; the plugin's "
               "own log/state under ~/.hermes/<plugin>/ or ~/.hermes/cache/<plugin>/.\n"
               "- Reproduce by calling the plugin function directly under the Hermes interpreter."),
    "gateway": ("C12 process lifecycle / environment isolation",
                "- `ps -eo pid,lstart,command | grep -i hermes`, `systemctl --user status` / `launchctl print "
                "gui/$UID/<label>`: what was running, who started it, restarts (`journalctl --user -u ...`).\n"
                "- `~/.hermes/logs/{gateway,agent,errors}.log` and `hermes-sync.log` (a `hermes update` at that "
                "minute stops servers) in a +-10 min window.\n"
                "- Environment: HERMES_HOME, profile, interpreter/venv of each process (`/proc/<pid>/environ` or "
                "`ps -E`). Guards keyed on inherited env vars (HERMES_DELEGATED_CHILD_CONTEXT, HERMES_SESSION_*, "
                "HERMES_KANBAN_*) misfire when a launcher (update/relaunch/sync, execute_code's env scrub) passes or "
                "drops the parent env: grep the guard, then every spawn path that sets or strips the var.\n"
                "- Who ended or orphaned it: session reapers / LRU eviction and agent close tearing down owned "
                "background processes; checkpoints (processes.json) written before the notify/watch flags, so a "
                "restart re-adopts the job without them. A wrong status chip is the symptom; the lifecycle writer is "
                "the root."),
    "cron": ("C12/C15 scheduled jobs",
             "- `hermes cron list` and `~/.hermes/cron/output/<job>/` for the failing run; its script under "
             "~/.hermes/scripts; the delivery target; `errors.log` at run time."),
    "agent": ("agent behaviour (C03-C09)",
              "- The session transcript: `sqlite3 -readonly ~/.hermes/state.db \"select role, substr(content,1,800) "
              "from messages where session_id='<sid>' order by id\"` on the host that ran it, else the coach digest "
              "`~/code/hermes-coach-data/<host>/<profile>/<sid>.json`.\n"
              "- What instruction pointed the agent there: the skill(s) loaded, ~/AGENTS.md rules, tool descriptions; "
              "`git log` of the skill. The fix is usually a skill/tool/AGENTS.md change plus a check that fires at "
              "the failure point (a coach rule or a tool-result hint), not more prose."),
}
CHECKLIST["bot"] = CHECKLIST["gateway"]
CHECKLIST["provider"] = ("provider / model calls",
                         "- `~/.hermes/logs/agent.log` + `errors.log` at the time: provider, status code, retries, "
                         "fallback chain; `hermes config get model` and the fallback plugin's state.")
CHECKLIST["browser"] = CHECKLIST["plugin"]


def investigation_brief(d: dict, previous: Optional[dict] = None, cfg: Optional[dict] = None) -> str:
    fp = d["fingerprint"]
    comp = d.get("component") or "other"
    when = _pt(d.get("at") or time.time())
    sid = d.get("session_id") or ""
    title, checks = CHECKLIST.get(comp, CHECKLIST["agent"])
    extra = "" if comp == "agent" else "\n" + CHECKLIST["agent"][1]
    rec = ""
    if previous:
        rec = (f"\n## Recurrence\nThis fingerprint was fixed before by card {previous.get('fix_card') or '?'} "
               f"({_pt(previous.get('fixed_at') or 0)}). The previous fix {previous.get('fix_card') or ''} didn't "
               "hold. Say so in the Inbox card's first line, find why the earlier fix and its test didn't catch the "
               "return (five-whys step 5), and the Plan MUST add a detection-level guard (test, assertion, alert or "
               "gate) that fails if it comes back.\n")
    proposal = f"~/.hermes/coach/selfheal/{fp}/proposal.md"
    return f"""Self-heal investigation for defect `{fp}`. Nothing here needs a human; David's approval of this lane
covers the investigation. You do NOT ship the fix: you post ONE Inbox proposal, and his Do it starts the fix card.
Budget for this card <= ${INVESTIGATION_BUDGET}.

## Evidence (from the detector)
- Symptom: {d.get('symptom') or '?'}
- Component: {comp} · detector: {d.get('source') or '?'} ({d.get('kind') or '?'}, p={d.get('p_defect', '?')})
- When: {when} · host: {d.get('host') or '?'} · session: @session:{d.get('profile') or 'default'}/{sid}
- Quote: "{(d.get('quote') or '').strip()[:500]}"
{('- Extra: ' + str(d.get('details'))[:1500]) if d.get('details') else ''}
{rec}
## Do this
1. Reproduce or confirm it LIVE, not from the transcript alone. Scope it from the user's own words and the session's
   open work (its cards, background processes, earlier turns), not only the detector's symptom: the trigger can point
   at a different event than the one that hurt him. Checklist for {title}:
{checks}{extra}
   Other hosts: the coach digests in ~/code/hermes-coach-data/<host>/ (git pull first) carry every session's user
   turns, tool counts and last reply. A host's state.db / kanban.db / logs are only readable ON that host: if the
   incident ran elsewhere, use what reaches it (ssh diag, digests, git) and list in the proposal what you could not
   read.
2. Before writing the chain, name 2-3 candidate roots and run the one check that tells them apart. The root is the
   writer of the wrong state (code path, env, config, skill rule), not the surface that displayed it or the turn the
   detector fired on. Then write the five-whys chain from evidence (five-whys skill): one causal step per why, each
   with a file:line, log line, db row or quote; label guesses `(hypothesis)`; branch "why it happened" and "why
   nothing caught it"; stop at a process/guard/design gap Hermes controls. Post the chain as a comment on this card.
3. Not a real defect (expected behaviour, user's own setting, outside world with nothing to guard)? Only after you
   checked the user's complaint itself, not just the detector's trigger. Run
   `python3 {SELF} event not_defect {fp} --card $HERMES_KANBAN_TASK --note "<why>"` and complete this card with a
   result that starts with "not a defect:". Post nothing to the Inbox.
4. Otherwise write the proposal to {proposal} (markdown, <= 3500 chars, plain words, no jargon) with exactly these
   sections:
   **What happened**: the symptom, when (PT), the `@session:<profile>/<id>` link, his quote.
   **Why**: the five-whys chain, one line per why, each with its evidence.
   **Plan**: the exact changes (files/skills/config), the regression test (fails before, passes after), the
   detection guard, and what gets verified live after shipping.
   **Cost / risk**, then **Undo**: how to revert.
   Then post it (one card; a re-post of the same key updates it):
   `hermes inbox post --type {INBOX_TYPE} --field fp={fp} --key selfheal:{fp} --poster "self-heal" --body - "<one-line title: what is broken>" < {proposal}`
   and record it: `python3 {SELF} event proposed {fp} --card $HERMES_KANBAN_TASK`.
5. Complete this card with the proposal's first line as the summary. Do not start the fix.
"""


def fix_brief(fp: str, st: dict) -> str:
    proposal = coach_root() / "selfheal" / fp / "proposal.md"
    try:
        text = proposal.read_text()[:6000]
    except Exception:
        text = f"(proposal file {proposal} missing: read the investigation card {st.get('card') or '?'} comments)"
    bb = st.get("component") == "bb"
    ship = ("Ship it through the bb-self-fix procedure (PR, merge, deploy) under David's standing approval."
            if bb else
            "Ship it under David's standing merge/deploy approval: hermes-mods commit + push (pre-commit hook, never "
            "--no-verify), core changes through a hermes-core-wt worktree + ship, settings through shared-config.json "
            "(hermes-mods-repo skill).")
    return f"""Self-heal FIX for defect `{fp}`. David pressed Do it on the Inbox proposal below: that is the approval.
Investigation card: {st.get('card') or '?'}. Budget for this card <= ${FIX_BUDGET}.

## Approved proposal
{text}

## Acceptance (all of it, in order)
1. Re-check the defect still reproduces on the current code (it may have been fixed meanwhile: then say so, run
   `python3 {SELF} event not_defect {fp} --card $HERMES_KANBAN_TASK --note "already fixed by <sha>"` and stop).
2. Write the regression test FIRST and show it FAILING on the current code (paste the failing output in a card
   comment).
3. Make the fix from the plan (root level, not just the symptom) and the detection guard. Show the same test
   PASSING, plus the module's existing tests.
4. {ship}
5. Verify live on the real path (the surface/command/session type where it happened), and capture the proof.
6. Post the result on the SAME Inbox key:
   `hermes inbox post --type {RESULT_TYPE} --field fp={fp} --key selfheal:{fp} --poster "self-heal" --body - "Fixed: <what>" < <result.md>`
   with what changed (commits), the red/green test output, the live proof, and the undo command.
   Then `python3 {SELF} event fixed {fp} --card $HERMES_KANBAN_TASK --note "<commit sha>"`.
7. Complete the card. If something irreversible or external blocks the fix, kanban_block with the exact ask instead.
"""


class LaneState:
    """<coach>/selfheal_lane.json: fingerprint -> {status, card, fix_card, component, symptom, filed_day, snooze_until,
    suppressed, fixed_at, recurrences, refiles, gen, cards, history}. Statuses: filed, proposed, approved, fixed,
    not_defect, declined, suppressed, reverted, stale."""

    def __init__(self, root: Path):
        self.path = Path(root) / "selfheal_lane.json"

    def read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return {}

    def get(self, fp: str) -> dict:
        return dict(self.read().get(fp) or {})

    def update(self, fp: str, **kw) -> dict:
        d = self.read()
        e = d.get(fp) or {}
        hist = e.get("history") or []
        if "status" in kw and kw["status"] != e.get("status"):
            hist = (hist + [[round(time.time()), kw["status"]]])[-30:]
        e.update(kw, history=hist)
        d[fp] = e
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1))
        os.replace(tmp, self.path)
        return e


def _utc_day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _next_utc_midnight(ts: float) -> str:
    return time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime((int(ts) // 86400 + 1) * 86400))


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _budget_mod():
    """engine_budget from the hermes-coach skill (same repo), or None."""
    import importlib.util
    import sys
    p = Path(__file__).resolve().parents[2] / "skills" / "productivity" / "hermes-coach" / "scripts" / "engine_budget.py"
    if not p.exists():
        return None
    name = "_selfheal_engine_budget"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(name, p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod = sys.modules.setdefault(name, mod)
    return mod


def budget(board: Optional[str] = None, now: Optional[float] = None) -> dict:
    """{"usd", "cap", "running", "ok_usd", "ok_running", "why"} for the selfheal lane today (UTC)."""
    now = now or time.time()
    out = {"usd": 0.0, "cap": USD_DAY, "running": 0, "ok_usd": True, "ok_running": True, "why": ""}
    eb = _budget_mod()
    if eb is None:
        return out
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    b = board or os.environ.get("COACH_KANBAN_BOARD") or "default"
    try:
        snap = eb.snapshot(home=home, board=b, now=now)
        out["usd"] = snap["lanes"].get("selfheal", 0.0)
        out["cap"] = snap["caps"].get("selfheal", USD_DAY)
        ok, why = eb.check(snap, "selfheal")
        out["ok_usd"], out["why"] = ok, why
        out["running"] = sum(1 for c in eb.engine_cards(home, b, ("running",)) if c["lane"] == "selfheal")
    except Exception as exc:
        out["why"] = f"budget read failed: {exc}"
    out["ok_running"] = out["running"] < MAX_RUNNING
    return out


def generation(st: dict) -> int:
    """Generations used so far for a fingerprint (0 = never filed). Lane files written before `gen` existed derive it
    from the old counters; a collision with an earlier card is caught in file_defect anyway."""
    if st.get("gen") is not None:
        return int(st["gen"])
    if not st.get("card"):
        return 0
    return 1 + int(st.get("recurrences") or 0) + int(st.get("refiles") or 0)


def inv_key(fp: str, gen: int) -> str:
    return f"selfheal:{fp}" if gen <= 1 else f"selfheal:{fp}:r{gen}"


def fix_key(fp: str, gen: int) -> str:
    return f"selfheal:{fp}:fix" if gen <= 1 else f"selfheal:{fp}:r{gen}:fix"


def known_cards(st: dict) -> set:
    return {c for c in (st.get("cards") or []) + [st.get("card"), st.get("fix_card")] if c}


def decide(d: dict, st: dict, now: float) -> dict:
    """Pure dedupe / suppression / recurrence decision for one defect against its lane state.
    -> {"action": "file"|"skip", "why", "key", "previous"}"""
    fp = d["fingerprint"]
    status = st.get("status")
    if st.get("suppressed"):
        return {"action": "skip", "why": "suppressed (Never)"}
    if (st.get("snooze_until") or 0) > now:
        return {"action": "skip", "why": f"snoozed until {_iso(st['snooze_until'])} (Not now)"}
    if st.get("filed_day") == _utc_day(now):
        return {"action": "skip", "why": "already filed today (<= 1 per fingerprint per UTC day)"}
    if status in ("filed", "proposed", "approved"):
        return {"action": "skip", "why": f"open: {status} {st.get('fix_card') or st.get('card') or ''}".strip()}
    if status == "not_defect" and now - (st.get("closed_at") or 0) < NOT_DEFECT_DAYS * 86400:
        return {"action": "skip", "why": f"closed as not a defect within {NOT_DEFECT_DAYS} days"}
    g = generation(st) + 1  # every filing gets a fresh generation, so its keys never name an earlier card
    if status == "fixed":
        return {"action": "file", "why": "recurrence", "key": inv_key(fp, g), "gen": g, "previous": st}
    if st.get("card"):  # an earlier card closed without a fix (declined/reverted/not_defect/stale): new key
        return {"action": "file", "why": "refile", "key": inv_key(fp, g), "gen": g, "previous": None}
    return {"action": "file", "why": "new", "key": inv_key(fp, g), "gen": g, "previous": None}


def card_args(d: dict, key: str, previous: Optional[dict] = None, cfg: Optional[dict] = None) -> dict:
    r = route(d, cfg)
    sym = re.sub(r"\s+", " ", d.get("symptom") or "defect").strip()[:90]
    return {"title": f"self-heal: {sym}" + (" (came back)" if previous else ""),
            "body": investigation_brief(d, previous, cfg), "idempotency_key": key, "model": MODEL,
            "max_runtime": INVESTIGATION_RUNTIME, "skills": r["skills"], "board": r["board"],
            "assignee": "default", "created_by": CREATED_BY}


def kanban_argv(args: dict, hermes: str = "hermes") -> list:
    argv = [hermes, "kanban"] + (["--board", args["board"]] if args.get("board") else []) + [
        "create", args["title"], "--body", args["body"], "--assignee", args["assignee"],
        "--idempotency-key", args["idempotency_key"], "--model", args["model"],
        "--max-runtime", str(args["max_runtime"]), "--created-by", args["created_by"], "--json"]
    for s in args.get("skills") or []:
        argv += ["--skill", s]
    return argv


def _run(argv, runner=None, stdin=None):
    import subprocess
    if runner is None and os.environ.get("COACH_SELFHEAL_NO_EXEC") == "1":  # tests: never touch the real board/hub
        raise RuntimeError(f"COACH_SELFHEAL_NO_EXEC: refused {argv[:3]}")
    r = (runner or subprocess.run)(argv, capture_output=True, text=True, input=stdin, timeout=120)
    return r.returncode, (r.stdout or ""), (r.stderr or "")


def _task_id(out: str) -> Optional[str]:
    m = re.search(r'"id"\s*:\s*"(t_[0-9a-f]+)"', out) or re.search(r"\b(t_[0-9a-f]{6,})\b", out)
    return m.group(1) if m else None


def _store(store=None):
    if store is not None:
        return store

    class _S:
        root = coach_root()

        def log(self, rec):
            self.root.mkdir(parents=True, exist_ok=True)
            with (self.root / "log.jsonl").open("a") as f:
                f.write(json.dumps({"ts": time.time(), **rec}) + "\n")
    return _S()


def file_defect(d: dict, store=None, m: Optional[str] = None, runner=None, now: Optional[float] = None,
                cfg: Optional[dict] = None, budget_fn: Optional[Callable] = None) -> dict:
    """File one defect: {fingerprint, component, symptom, source, kind, p_defect, session_id, quote, host, at,
    details}. Returns {"action": would_file|filed|skip|forwarded|error, ...}."""
    store = _store(store)
    m = m or mode()
    now = now or time.time()
    cfg = lane_config() if cfg is None else cfg
    if not d.get("fingerprint"):
        d = {**d, "fingerprint": fingerprint(d.get("component") or "other", d.get("symptom") or "")}
    fp = d["fingerprint"]
    if m == "off":
        return {"action": "skip", "why": "COACH_SELFHEAL=off"}
    hub = is_hub(cfg)
    if m == "live" and not hub:
        rc, out, err = _run(hub_ssh_argv(cfg) + [cfg["hub_ssh"],
                             f"python3 {SELF} file --mode live"], runner, stdin=json.dumps(d))
        res = {"action": "forwarded" if rc == 0 else "error", "hub": cfg["hub_ssh"],
               "out": (out or err).strip()[-300:]}
        try:  # the hub prints its own file_defect result as the last line: callers (monitors) need its action
            res["hub_result"] = json.loads((out or "").strip().splitlines()[-1])
        except Exception:
            pass
        store.log({"event": "selfheal_forward", "fingerprint": fp, "ok": rc == 0, "detail": res["out"]})
        return res
    lane = LaneState(store.root)
    st = lane.get(fp)
    dec = decide(d, st, now)
    if dec["action"] == "skip":
        store.log({"event": "selfheal_skip", "mode": m, "fingerprint": fp, "why": dec["why"]})
        return {"action": "skip", "why": dec["why"]}
    args = card_args(d, dec["key"], dec.get("previous"), cfg)
    b = (budget_fn or budget)(args.get("board"), now)
    sched = None
    if not b["ok_usd"]:
        sched = (_next_utc_midnight(now), f"self-heal lane budget: {b['why'] or 'cap reached'}")
    elif not b["ok_running"]:
        sched = (_iso(now + 3600), f"self-heal lane: {b['running']} cards running (max {MAX_RUNNING})")
    if m != "live" or not hub:
        rec = {"event": "selfheal_would_file", "mode": m, "fingerprint": fp, "component": d.get("component"),
               "source": d.get("source"), "why": dec["why"], "recurrence": bool(dec.get("previous")),
               "schedule": sched, "args": args, "host": d.get("host")}
        store.log(rec)
        return {"action": "would_file", **rec}
    known, gen = known_cards(st), dec["gen"]
    tid, out, err = None, "", ""
    for _ in range(5):  # an idempotency key that hands back an earlier card (any non-archived status) -> next gen
        rc, out, err = _run(kanban_argv(args), runner)
        tid = _task_id(out) if rc == 0 else None
        if not tid or tid not in known:
            break
        store.log({"event": "selfheal_key_collision", "fingerprint": fp, "key": args["idempotency_key"], "card": tid})
        gen += 1
        args = card_args(d, inv_key(fp, gen), dec.get("previous"), cfg)
    if not tid or tid in known:
        why = (err or out).strip()[-300:] if not tid else f"idempotency keys keep returning earlier card {tid}"
        store.log({"event": "selfheal_file_error", "fingerprint": fp, "error": why})
        return {"action": "error", "why": why}
    if sched:
        bargs = ["hermes", "kanban"] + (["--board", args["board"]] if args.get("board") else [])
        _run(bargs + ["schedule", tid, f"SCHEDULED_UNTIL={sched[0]} — {sched[1]}"], runner)
    prev = dec.get("previous")
    kw: dict = dict(status="filed", card=tid, component=d.get("component"), symptom=d.get("symptom"),
              filed_day=_utc_day(now), filed_at=now, board=args.get("board"), session_id=d.get("session_id"),
              fix_card=None if not prev else st.get("fix_card"), gen=gen,
              cards=sorted(known | {tid}), proposed_at=None)
    if prev:
        kw["recurrences"] = int(st.get("recurrences") or 0) + 1
    elif dec["why"] == "refile":
        kw["refiles"] = int(st.get("refiles") or 0) + 1
    lane.update(fp, **kw)
    store.log({"event": "selfheal_filed", "fingerprint": fp, "card": tid, "component": d.get("component"),
               "board": args.get("board"), "recurrence": bool(prev), "scheduled": sched[0] if sched else None,
               "source": d.get("source")})
    if prev:
        store.log({"event": "selfheal_recurrence", "fingerprint": fp, "card": tid,
                   "previous": prev.get("fix_card"), "component": d.get("component")})
    return {"action": "filed", "card": tid, "key": args["idempotency_key"], "scheduled": sched}


def file_fix(fp: str, store=None, runner=None, now: Optional[float] = None, budget_fn=None,
             trigger: str = "do_it") -> dict:
    """Do it: the fix card `selfheal:<fp>[:r<gen>]:fix` on the hub, keyed to the current generation (idempotent
    within it; a key that returns an earlier card of this fingerprint is an error, never an approval)."""
    store = _store(store)
    now = now or time.time()
    lane = LaneState(store.root)
    st = lane.get(fp)
    if not FP_RE.match(fp or ""):
        return {"action": "error", "why": f"not a fingerprint: {fp!r}"}
    if st.get("fix_card") and st.get("status") in ("approved", "fixed"):
        return {"action": "skip", "why": f"fix card exists: {st['fix_card']}", "card": st["fix_card"]}
    r = route({"component": st.get("component")})
    key = fix_key(fp, generation(st) or 1)
    args = {"title": f"self-heal fix: {(st.get('symptom') or fp)[:90]}", "body": fix_brief(fp, st),
            "idempotency_key": key, "model": MODEL, "max_runtime": FIX_RUNTIME,
            "skills": (list(BB_SKILLS) if r["bb"] else list(FIX_SKILLS)), "board": r["board"],
            "assignee": "default", "created_by": CREATED_BY}
    b = (budget_fn or budget)(args.get("board"), now)
    rc, out, err = _run(kanban_argv(args), runner)
    tid = _task_id(out) if rc == 0 else None
    if not tid:
        return {"action": "error", "why": (err or out).strip()[-300:]}
    if tid in known_cards(st):
        why = f"fix key {key} returned earlier card {tid}"
        store.log({"event": "selfheal_file_error", "fingerprint": fp, "error": why})
        return {"action": "error", "why": why}
    sched = None
    if not b["ok_usd"]:
        sched = _next_utc_midnight(now)
    elif not b["ok_running"]:
        sched = _iso(now + 3600)
    if sched:
        bargs = ["hermes", "kanban"] + (["--board", args["board"]] if args.get("board") else [])
        _run(bargs + ["schedule", tid, f"SCHEDULED_UNTIL={sched} — self-heal lane budget/concurrency"], runner)
    lane.update(fp, status="approved", fix_card=tid, approved_at=now, cards=sorted(known_cards(st) | {tid}))
    store.log({"event": "selfheal_approved", "fingerprint": fp, "card": tid, "component": st.get("component"),
               "trigger": trigger, "scheduled": sched})
    return {"action": "filed", "card": tid, "key": key, "scheduled": sched}


EVENT_STATUS = {"proposed": "proposed", "fixed": "fixed", "not_defect": "not_defect", "reverted": "reverted",
                "declined": "declined"}


def record_event(kind: str, fp: str, card: str = "", note: str = "", store=None, now: Optional[float] = None) -> dict:
    """`self_heal.py event <kind> <fp>`: cards report their lifecycle; logged as selfheal_<kind>."""
    store = _store(store)
    now = now or time.time()
    if kind not in EVENT_STATUS:
        raise ValueError(f"unknown event {kind}; one of {sorted(EVENT_STATUS)}")
    lane = LaneState(store.root)
    st = lane.get(fp)
    kw = {"status": EVENT_STATUS[kind]}
    if kind == "proposed":
        kw["proposed_at"] = now
    if kind == "fixed":
        kw.update(fixed_at=now, fix_card=card or st.get("fix_card"))
    if kind in ("not_defect", "declined"):
        kw["closed_at"] = now
    e = lane.update(fp, **kw)
    store.log({"event": f"selfheal_{kind}", "fingerprint": fp, "card": card or e.get("card"),
               "component": e.get("component"), "note": note[:300], "recurrence": bool(e.get("recurrences"))})
    return e


def inbox_outcome(fp: str, runner=None) -> Optional[dict]:
    rc, out, _ = _run(["hermes", "inbox", "outcome", f"selfheal:{fp}"], runner)
    if rc != 0:
        return None
    try:
        return json.loads(out.strip().splitlines()[-1])
    except Exception:
        return None


def card_status(card: str, board: Optional[str] = None, runner=None) -> Optional[str]:
    """Kanban status of a card, "missing" when the board has no such card, None when unknown (read failed)."""
    argv = ["hermes", "kanban"] + (["--board", board] if board else []) + ["show", card, "--json"]
    try:
        rc, out, err = _run(argv, runner)
    except Exception:
        return None
    if rc != 0:
        return "missing" if "no such task" in (err + out).lower() else None
    try:
        return json.loads(out)["task"]["status"]
    except Exception:
        return None


ENDED = ("done", "archived", "missing")


def reconcile(store=None, runner=None, now: Optional[float] = None) -> list:
    """A `filed` (investigation) or `approved` (fix) fingerprint whose card ended without reporting proposed / fixed
    (crashed, timed out, archived) would stay open forever and block every later filing: mark it `stale` so the next
    detection refiles it under a new generation. Returns [(fp, what)]."""
    store = _store(store)
    now = now or time.time()
    lane = LaneState(store.root)
    done = []
    for fp, st in lane.read().items():
        status = st.get("status")
        card = st.get("card") if status == "filed" else st.get("fix_card") if status == "approved" else None
        if not card:
            continue
        ks = card_status(card, st.get("board"), runner)
        if ks not in ENDED:
            continue
        lane.update(fp, status="stale", closed_at=now)
        store.log({"event": "selfheal_stale", "fingerprint": fp, "card": card, "was": status, "kanban": ks,
                   "component": st.get("component")})
        done.append((fp, f"stale ({status}, card {card} {ks})"))
    return done


def sync(store=None, runner=None, now: Optional[float] = None, fixer: Optional[Callable] = None) -> list:
    """Act once on each proposal's Inbox outcome: do_it (backstop for the Do-it task), not_now (snooze), never
    (suppress). Returns [(fp, what)]."""
    store = _store(store)
    now = now or time.time()
    lane = LaneState(store.root)
    done = reconcile(store, runner, now)
    for fp, st in lane.read().items():
        if st.get("status") != "proposed":
            continue
        o = inbox_outcome(fp, runner)
        if not o or o.get("state") == "open":
            continue
        if (o.get("at") or now) < (st.get("filed_at") or 0):  # an earlier generation's decision on the same key
            continue
        what = o.get("outcome")
        if what == "do_it":
            r = (fixer or file_fix)(fp, store=store, runner=runner, now=now, trigger="sync")
            done.append((fp, f"do_it -> {r.get('card') or r.get('why')}"))
        elif what in ("not_now", "never"):
            kw = {"status": "suppressed" if what == "never" else "declined", "closed_at": now}
            if what == "never":
                kw["suppressed"] = True
            else:
                kw["snooze_until"] = now + SNOOZE_DAYS * 86400
            lane.update(fp, **kw)
            store.log({"event": "selfheal_declined", "fingerprint": fp, "card": st.get("card"), "how": what,
                       "component": st.get("component")})
            done.append((fp, what))
        elif o.get("state") == "archived":
            lane.update(fp, status="declined", closed_at=now)
            store.log({"event": "selfheal_declined", "fingerprint": fp, "card": st.get("card"), "how": "archived",
                       "component": st.get("component")})
            done.append((fp, "archived"))
    return done


# ── digest backstop ──
INTERACTIVE = ("desktop", "tui", "cli", "slack", "telegram", "discord", "whatsapp", "signal")


def backstop_context(dg: dict, turns: list, t: dict, n: int = 3, chars: int = 300) -> str:
    """Session title plus the `n` user turns before `t` (each clipped), for the backstop judge."""
    i = next((k for k, x in enumerate(turns) if x is t), len(turns))
    earlier = [str(x.get("text") or "")[:chars].replace("\n", " ") for x in turns[max(0, i - n):i]]
    lines = [f"Session title: {dg.get('title') or '(untitled)'}"]
    lines += ([f"Earlier user messages, oldest first:"] + [f"- {e}" for e in earlier] if earlier
              else ["This is the first user message of the session."])
    return "\n".join(lines)


def backstop(data: Path, store=None, judge: Optional[Callable] = None, now: Optional[float] = None,
             lookback: float = 3 * 3600, filer: Optional[Callable] = None, m: Optional[str] = None,
             limit: int = 200) -> list:
    """Run the complaint detector over user turns newer than the per-session cursor in the coach digests
    (<data>/<host>/<profile>/<sid>.json). A session seen for the first time is scanned only `lookback` back. The judge
    gets the agent reply before each turn (digest v6 `prev`) and always the session context (title + the earlier user
    turns, `backstop_context`): with neither, the strict stage read \"why didnt this work\" or \"the camera stream is
    broken\" as a new issue and scored 0. Returns the filings."""
    store = _store(store)
    now = now or time.time()
    m = m or mode()
    cur_p = Path(store.root) / "selfheal_backstop.json"
    try:
        cursor = json.loads(cur_p.read_text())
    except Exception:
        cursor = {}
    judge = judge or aux_judge
    out, n = [], 0
    for f in sorted(Path(data).glob("*/*/*.json")):
        if f.parts[-3] in ("reports", "findings", "research", "eval", "curriculum"):
            continue
        rel = "/".join(f.parts[-3:])
        try:
            if f.stat().st_mtime < now - 14 * 86400:
                continue
            dg = json.loads(f.read_text())
        except Exception:
            continue
        if dg.get("source") not in INTERACTIVE or dg.get("parent_session_id"):
            continue
        turns = [t for t in dg.get("user_turns") or [] if isinstance(t, dict) and t.get("t")]
        since = cursor.get(rel, now - lookback)
        new = [t for t in turns if t["t"] > since]
        if not new:
            continue
        cursor[rel] = max(t["t"] for t in turns)
        for t in new:
            if n >= limit:
                break
            text = t.get("text") or ""
            if not prefilter(text):
                continue
            n += 1
            hit = detect_complaint(t.get("prev") or "", text, llm=judge, threshold=threshold(),
                                   context=backstop_context(dg, turns, t))
            if not hit:
                continue
            sid = dg.get("id") or f.stem
            new_fp = Fingerprints(store.root).record(hit, sid)
            d = {**hit, "session_id": sid, "quote": clean_user(text)[:500], "host": dg.get("host") or f.parts[-3],
                 "profile": dg.get("profile") or f.parts[-2], "at": t["t"], "source": "backstop",
                 "kind": hit.get("kind") or "complaint"}
            store.log({"event": "selfheal_detect", "mode": m, "session_id": sid, "source": "backstop",
                       "p": hit["p_defect"], "component": hit["component"], "symptom": hit["symptom"],
                       "fingerprint": hit["fingerprint"], "new": new_fp, "evidence": d["quote"][:300]})
            if new_fp:
                out.append((filer or file_defect)(d, store=store, m=m))
    cur_p.parent.mkdir(parents=True, exist_ok=True)
    cur_p.write_text(json.dumps(cursor))
    return out


def main(argv=None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog="self_heal.py", description="self-heal lane (see the Lane comment)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("file", help="file one defect JSON from stdin (hub side of an ssh forward)")
    f.add_argument("--mode", choices=("shadow", "live"))
    x = sub.add_parser("fix", help="Do it: create the fix card for a fingerprint")
    x.add_argument("fp")
    e = sub.add_parser("event", help="record a lifecycle event from a card")
    e.add_argument("kind", choices=sorted(EVENT_STATUS))
    e.add_argument("fp")
    e.add_argument("--card", default="")
    e.add_argument("--note", default="")
    sub.add_parser("sync", help="act on Inbox outcomes (Do it / Not now / Never)")
    b = sub.add_parser("backstop", help="detector over new user turns in the coach digests")
    b.add_argument("--data", default=os.environ.get("COACH_DATA") or str(Path.home() / "code/hermes-coach-data"))
    b.add_argument("--mode", choices=("shadow", "live"))
    c = sub.add_parser("cron", help="hub pass every 30 min: drain queue, backstop, sync")
    c.add_argument("--data", default=os.environ.get("COACH_DATA") or str(Path.home() / "code/hermes-coach-data"))
    sub.add_parser("status", help="lane state + budget")
    a = ap.parse_args(argv)
    if a.cmd == "file":
        d = json.loads(sys.stdin.read())
        print(json.dumps(file_defect(d, m=a.mode)))
    elif a.cmd == "fix":
        print(json.dumps(file_fix(a.fp)))
    elif a.cmd == "event":
        e = record_event(a.kind, a.fp, a.card, a.note)
        print(json.dumps({"fingerprint": a.fp, "status": e.get("status")}))
    elif a.cmd == "sync":
        for fp, what in sync():
            print(f"{fp}: {what}")
    elif a.cmd == "backstop":
        for r in backstop(Path(a.data), m=a.mode):
            print(json.dumps(r)[:400])
    elif a.cmd == "cron":
        for line in cron(Path(a.data)):
            print(line)
    elif a.cmd == "status":
        st = LaneState(coach_root()).read()
        print(json.dumps({"mode": mode(), "hub": is_hub(), "budget": budget(),
                          "fingerprints": {k: {kk: v.get(kk) for kk in ("status", "card", "fix_card", "filed_day")}
                                           for k, v in st.items()}}, indent=1))
    return 0


def drain(store=None, m: Optional[str] = None, filer: Optional[Callable] = None) -> list:
    """File queued live detections that were not filed in-process (e.g. the hub was unreachable)."""
    store = _store(store)
    q = Path(store.root) / "selfheal_queue.jsonl"
    done_p = Path(store.root) / "selfheal_queue.offset"
    if not q.exists():
        return []
    lines = q.read_text().splitlines()
    try:
        off = int(done_p.read_text().strip() or 0)
    except Exception:
        off = 0
    out = []
    for line in lines[off:]:
        try:
            r = json.loads(line)
        except Exception:
            continue
        d = {k: r.get(k) for k in ("fingerprint", "component", "symptom", "source", "session_id")}
        d.update(quote=r.get("evidence") or "", at=r.get("ts"), p_defect=r.get("p"), host=host_name())
        out.append((filer or file_defect)(d, store=store, m=m))
    done_p.write_text(str(len(lines)))
    return out


def cron(data: Path, store=None, now: Optional[float] = None, backstop_every: float = 25 * 60,
         backstop_fn: Optional[Callable] = None) -> list:
    """The hub's pass (no agent, hermes cron every 5 min): Inbox outcomes (Do it -> fix card within 5 min), queue
    drain, and the digest backstop at most every `backstop_every` seconds. Prints only what it did.

    Do it is a plain resolve on the Inbox card and THIS pass files the fix card: a Kanban worker (the Inbox's task
    action would run one) is fenced from creating cards through the CLI, the cron scheduler is not."""
    lines = []
    if mode() == "off":
        return lines
    store = _store(store)
    now = now or time.time()
    for fp, what in sync(store, now=now):
        lines.append(f"inbox: {fp} {what}")
    for r in drain(store):
        if r.get("action") not in ("skip",):
            lines.append(f"queue: {json.dumps(r)[:300]}")
    stamp = Path(store.root) / "selfheal_backstop.last"
    try:
        last = float(stamp.read_text().strip() or 0)
    except Exception:
        last = 0.0
    if now - last >= backstop_every:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(now))
        try:
            for r in (backstop_fn or backstop)(data, store):
                if r.get("action") not in ("skip",):
                    lines.append(f"backstop: {r.get('action')} {r.get('fingerprint') or r.get('card') or ''}")
        except Exception as exc:
            lines.append(f"backstop failed: {exc}")
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
