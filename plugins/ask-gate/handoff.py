"""Detect a reply that hands a terminal/password step back to the user.

"Run `sudo ...` on the mini", "add your key to authorized_keys", "run this in your terminal": a shell on this Mac
(or one it can SSH to) could do all of these, with the user typing any password into a ``run_in_user_terminal``
window. Physical steps (taps on a phone, a GUI login screen, plugging in hardware, web sign-ins, pasting a code into
the chat) are not hand-offs and must not match.

Pure stdlib, no I/O: ask-gate's ``pre_final_gate`` and user-steps (loaded by file path) share it.
"""
from __future__ import annotations

import re

# Things only a shell does. A sentence that asks the user to act on one of these is a terminal hand-off.
_SHELL = re.compile(
    r"\bsudo\b|\bvisudo\b|\bssh(?:-copy-id|-keygen|-add)?\b|\bscp\b|\brsync\b|authorized_keys|\bid_\w+\.pub\b|"
    r"\bpublic key\b|\bssh key\b|\bterminal\b|\biterm2?\b|\bshell\b|\bcommand line\b|\blaunchctl\b|\bkillall\b|"
    r"\bpmset\b|\bmdutil\b|\bdefaults write\b|\bchmod\b|\bchown\b|\bbrew\b|\bnpm\b|\bpip3?\b|\bgit (?:pull|push|clone)\b|"
    r"\bhermes (?:update|auth|config|gateway|setup|login)\b|\bdocker\b|\bkeychain\b|\bsecurity unlock", re.I)

# Imperative verbs a hand-off starts with.
_VERB = r"(?:run|paste|type|enter|execute|add|append|copy|install|start|restart|sign in|log in|unlock|turn off|turn on)"
_LEAD = (r"(?:^|(?<=[.!?:;])\s+|,\s*(?:and\s+|then\s+|or\s+)?|\b(?:and|then|or)\s+|\bplease\s+|\byou\s+(?:can|could|"
         r"should|must|need\s+to|'ll\s+need\s+to|will\s+need\s+to|may\s+need\s+to|have\s+to)\s+|\bi'?d rather you\s+)")
_IMPERATIVE = re.compile(_LEAD + r"(?:\*\*)?" + _VERB + r"\b", re.I)
_ASK_PHRASE = re.compile(
    r"\b(?:run|paste|type|execute)\b[^.]{0,80}\bin your (?:own )?(?:terminal|shell)\b|"
    r"\bleft for you is\b|\bwaiting on you for\b|\bneeds you to\b|\b(?:run|do) (?:it|this|that|them) yourself\b|\byourself\b.{0,40}\brun\b|"
    r"\bneeds? (?:one|a|an|two|\d+)?\s*`?(?:sudo|ssh|terminal)`?[^.\n]{0,30}\bfrom you\b|"
    r"\b(?:enter|type) (?:your|the|her|his|its|the \w+'s|\w+'s) password\b(?! (?:in|into|on) (?:the|that) (?:web|page|form|field))",
    re.I)

# Shell code fences / inline code that looks like a command.
_FENCE = re.compile(r"```[a-z]*\n(.*?)```", re.S)
_CODE_SHELL = re.compile(r"(?m)^\s*(?:\$\s*)?(?:sudo|ssh|scp|rsync|cd|launchctl|killall|pmset|brew|git|hermes|docker|"
                         r"ssh-copy-id|visudo|chmod|security|defaults|npm|uv|python3?)\b")

# Context that makes the step physical / GUI / chat, not a terminal hand-off.
_PHYSICAL = re.compile(
    r"\bsystem settings\b|\bbrowser\b|\baddress bar\b|\bdevtools\b|\bweb ?page\b|\bsign-in page\b|\bpassword field\b|"
    r"\bform\b|\bchrome\b|\bsafari\b|\biphone\b|\bipad\b|\bwatch app\b|\bon (?:its|the|her|his) (?:own )?screen\b|"
    r"\bat (?:its|the|her|his) screen\b|\block screen\b|\bcaptcha\b|\bplug\b|\bcable\b|\bdrive into\b|"
    r"\b(?:paste|send) (?:it|them|the \w+|this|that|your \w+) (?:back )?(?:here|in the chat|to me)\b|\bpaste [^.\n]{0,40} here\b|"
    r"\bdon'?t paste\b|\bplease don'?t\b|\bfinder\b|\bclick\b|\btap\b|\bsettings\b|\bthe terminal where\b", re.I)
# Reversal / later-option instructions ("To take it down, run ...") and past-tense reports ("Gave t_x the fix: ...").
_NOT_NOW = re.compile(r"^\s*(?:[-*•]\s*)?(?:to (?:take|turn|shut) (?:it|this|them|that|\w+) (?:down|off)\b|to (?:undo|revert|roll back|"
                      r"stop|remove|uninstall|disable)\b|(?:gave|told|asked|sent|filed|fixed|made|wrote|moved|built|reported)\b)", re.I)

# The agent describing its own action, not asking.
_SELF = re.compile(r"^\s*(?:i|i'll|i'd|i've|i'm|we|we'll|let me|i will|i can|i can't|i couldn't)\b", re.I)
_ALREADY = re.compile(r"\btells? me to\b|\basks? you to\b|\btold (?:me|you) to\b|\b(?:i ran|i've run|i added|i opened|already ran|already added|works now|you typed)\b", re.I)


def _strip(text: str) -> str:
    text = re.sub(r"::user-step\{[^}]*t=\"((?:[^\"\\]|\\.)*)\"[^}]*\}", lambda m: m.group(1).replace("\\n", "\n"), text)
    return text


def _sentences(text: str) -> list[tuple[str, int]]:
    """(sentence, end offset in text) over prose only; code fences are replaced by a placeholder line."""
    prose = _FENCE.sub(lambda m: "\n⟦code⟧\n", text)
    out: list[tuple[str, int]] = []
    pos = 0
    for para in re.split(r"(\n\s*\n|\n(?=\s*(?:[-*•]|\d+[.)])\s))", prose):
        for s in re.split(r"(?<=[.!?])\s+(?=[A-Z*`(])", para):
            s2 = s.strip()
            if s2 and s2 not in ("⟦code⟧",) and not re.fullmatch(r"\n\s*\n|\n", s):
                out.append((s2, pos))
        pos += len(para)
    return out


def _fence_after(text: str, sentence: str) -> str:
    """The code fence (if any) that directly follows a sentence ending in ':' (\"run this:\")."""
    i = text.find(sentence[-60:])
    if i < 0:
        return ""
    m = _FENCE.search(text, i + len(sentence[-60:]))
    if not m or m.start() - (i + len(sentence[-60:])) > 12:
        return ""
    return m.group(1)


def find_handoffs(text: str) -> list[str]:
    """Sentences in ``text`` that ask the user to do a terminal-doable step. Empty list = none."""
    if not isinstance(text, str) or not text.strip():
        return []
    text = _strip(text)
    hits: list[str] = []
    for s, _ in _sentences(text):
        flat = re.sub(r"\s+", " ", s)
        flat = re.sub(r"\"[^\"]{0,200}\"|“[^”]{0,200}”", "\"…\"", flat)  # quoted speech is not an ask
        if _NOT_NOW.match(flat):
            continue
        if _ALREADY.search(flat) or _PHYSICAL.search(flat):
            continue
        if _SELF.match(flat) and not re.search(r"\byou (?:can|could|should|must|need|'ll need|have to)\b|\bplease\b|"
                                               r"\brather you\b", flat, re.I):
            continue
        asks = bool(_IMPERATIVE.search(flat) or _ASK_PHRASE.search(flat))
        if not asks:
            continue
        shellish = bool(_SHELL.search(flat))
        if not shellish and flat.rstrip("*").endswith(":"):
            shellish = bool(_CODE_SHELL.search(_fence_after(text, s)))
        if not shellish:
            continue
        # "you can let my account run ..." / "I'd need root": a verb whose subject is the agent's account
        if re.search(r"\b(?:my account|i can|i'll|i will) (?:run|add|start)\b", flat, re.I) and not _ASK_PHRASE.search(flat) \
                and not re.search(r"(?:^|[.;:]\s+)(?:\*\*)?" + _VERB + r"\b", flat, re.I):
            continue
        hits.append(flat[:300])
    # A "for you / needs you" heading followed by a shell fence with no imperative sentence of its own.
    for m in re.finditer(r"(?i)(?:for you|needs you|waiting on you|still open)[^\n]*\n(?:[^\n]*\n){0,3}?\s*```[a-z]*\n(.*?)```",
                         text, re.S):
        if _CODE_SHELL.search(m.group(1)) and re.search(r"\bsudo\b|\bssh\b|\bauth add\b|-it\b", m.group(1)):
            hits.append(re.sub(r"\s+", " ", m.group(0))[:300])
    seen: set[str] = set()
    return [h for h in hits if not (h in seen or seen.add(h))]


NUDGE = ("Do it yourself: call run_in_user_terminal (tool_call name=\"run_in_user_terminal\", arguments {command, "
         "reason}); the user types any password there and you get the output back. For another host wrap it: "
         "ssh -t <host> '<cmd>', or for a refused SSH key run `ssh-copy-id -i ~/.ssh/id_ed25519.pub <user>@<host>` "
         "there and continue with key auth. Hand back only truly physical steps (on-device taps, GUI login screens, "
         "hardware).")


def gate_nudge(hits: list[str]) -> str:
    quoted = "\n".join(f"- \"{h[:200]}\"" for h in hits[:4])
    return ("[ask-gate] Not delivered: this reply hands a terminal/password step back to the user:\n" + quoted
            + "\n" + NUDGE + " Then rewrite the reply with the result.")


_OPENED = re.compile(r"run_in_user_terminal|tell application \\?\"(?:Terminal|iTerm2?)\\?\"|open -a (?:Terminal|iTerm)|\bcmux\b",
                     re.I)


def opened_terminal(tools: list[dict]) -> bool:
    """True when this turn already put a terminal in front of the user (run_in_user_terminal, or a hand-rolled
    osascript/open -a Terminal window), so \"type your password in that window\" is a pointer, not a hand-off."""
    for t in tools or []:
        blob = f"{t.get('tool', '')} {t.get('args', '')}"
        if _OPENED.search(blob):
            return True
    return False
