"""coach: one nudge bus. Agent channel = hooks here (hints the model reads, invisible to the user).
User channel = dashboard/plugin_api.py + desktop-plugins/coach (one-button panels above the composer)."""
from __future__ import annotations

import importlib.util
import logging
import threading
import os
import time
import sys
from pathlib import Path

log = logging.getLogger("coach")
_HERE = Path(__file__).resolve().parent


def _last_assistant(history) -> str:
    """The latest assistant message with text in the hook's conversation_history (fallback when the coach's own
    per-session state has no last reply yet, e.g. the first turn after a backend restart)."""
    for m in reversed(history or []):
        if isinstance(m, dict) and m.get("role") == "assistant":
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(str(p.get("text") or "") for p in c if isinstance(p, dict))
            if isinstance(c, str) and c.strip():
                return c
    return ""


def _load(name: str):
    """Load a sibling module once; re-exec it when the file changed, so a plugin reload
    (activate / discover_and_load(force=True)) picks up edits instead of the cached copy."""
    key = f"_coach_{name}"
    path = _HERE / f"{name}.py"
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = None
    mod = sys.modules.get(key)
    if mod is None or getattr(mod, "_coach_mtime", None) != mtime:
        spec = importlib.util.spec_from_file_location(key, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
        setattr(mod, "_coach_mtime", mtime)
    return mod


def build(store=None, checkout=None, selfheal_sync=False, selfheal_llm=None, selfheal_confirm="auto"):
    """The three hook callbacks around one AgentCoach (exposed for tests). `selfheal_sync` runs the self-heal
    detectors inline (tests); in production they run in a daemon thread so a judge call never delays a turn."""
    core, rules = _load("core"), _load("agent_rules")
    store = store or core.Store()
    coach = rules.AgentCoach(checkout if checkout is not None else rules.default_live_checkout(), signals=store)

    def pre_llm_call(session_id="", user_message=None, platform="", parent_session_id="", conversation_history=None,
                     **_):
        try:
            if not session_id:
                return None
            rec = store.user_turn(session_id, user_message) or {}
            hint = coach.turn_start(session_id, user_message)
            # The fix-loop window survives process boundaries (`hermes chat -q --resume`, backend restarts): the
            # in-memory loop state is re-seeded from the session file, and windows count persistent user turns.
            st = coach.loop_state(session_id)
            if not st.get("last_reply") and isinstance(rec.get("loop"), dict):
                st.update(rec["loop"])
            turn = rec.get("turns") or coach.turn_number(session_id)
            # Lessons judge nearly every message, so it runs in the background; its hint rides the next tool result
            # (or the next turn). The fix-loop judge is prefilter-gated and its hint matters before the first patch,
            # so it stays inline.
            prev = coach.loop_state(session_id).get("last_reply") or ""
            threading.Thread(target=_lesson_bg, args=(session_id, user_message, prev), daemon=True).start()
            _selfheal_run(_selfheal_complaint, session_id, user_message,
                          prev or _last_assistant(conversation_history), platform, parent_session_id,
                          list(coach.loop_state(session_id).get("last_log") or []))
            loop_hint = _fix_loop(session_id, user_message, turn)
            hint = " ".join(h for h in (f"[coach] {hint}" if hint else "", loop_hint or "") if h)
            return {"context": hint} if hint else None
        except Exception as exc:
            log.debug("coach pre_llm_call: %s", exc)
            return None

    def post_tool_call(tool_name="", args=None, result=None, session_id="", status=None, duration_ms=0,
                       error_type=None, error_message=None, **_):
        try:
            coach.observe(session_id, tool_name, args, result, status, duration_ms or 0, error_type, error_message)
        except Exception as exc:
            log.debug("coach post_tool_call: %s", exc)

    def transform_tool_result(tool_name="", result=None, session_id="", args=None, **_):
        try:
            coach.check_live(session_id, tool_name, args)
            hint = coach.take(session_id)
            return rules.attach_hint(result, hint) if hint and isinstance(result, str) else None
        except Exception as exc:
            log.debug("coach transform_tool_result: %s", exc)
            return None

    def pre_tool_call(tool_name="", args=None, **_):
        try:
            what = rules.blocks_live_checkout(tool_name, args if isinstance(args, dict) else {}, coach.checkout)
        except Exception as exc:
            log.debug("coach pre_tool_call: %s", exc)
            return None
        if not what:
            return None
        log.info("coach blocked live-checkout change: %s", what)
        try:
            store.log({"event": "agent_block", "rule": "live_checkout", "what": what})
        except Exception:
            pass
        return {"action": "block", "message": rules.block_message(what, coach.checkout)}

    def _fix_loop(session_id, user_message, turn=None):
        """Judge whether this user message reports that the agent's last fix failed (once per turn, sync,
        prefilter-gated, short timeout). Fail-open."""
        try:
            fl = _load("fix_loop")
            cfg = fl.settings()
            if not cfg["on"] or not isinstance(user_message, str):
                return None
            st = coach.loop_state(session_id)
            s1 = _load("system_one")
            m = s1.mode()
            turn = turn or coach.turn_number(session_id)
            hit = fl.assess(st, turn, user_message, fl.aux_judge, threshold=cfg["threshold"],
                            screen=s1.fixloop_screen if m != "off" else None, screen_below=s1.below(),
                            shadow=m == "shadow", log=s1.log)
            if not hit:
                return None
            store.log({"event": "agent_hint", "session_id": session_id, "rule": "fix_loop", "n": hit["n"],
                       "p": hit["p"], "issue": hit["issue"]})
            store.confirmed_fail(session_id)
            return " ".join(h for h in (hit["hint"], _fixloop_card(session_id, st, hit, user_message, turn)) if h)
        except Exception as exc:
            log.debug("coach fix_loop: %s", exc)
            return None

    def _fixloop_card(session_id, st, hit, user_message, turn):
        """2nd confirmed failure in the window -> one read-only review card pinned to the strong model."""
        try:
            so = _load("second_opinion")
            created = []

            def create(args):
                res = so.create_card(args)
                created.append(res)
                return res
            extra = so.fixloop_review(st, session_id, turn, hit, user_message, create=create)
            if extra and created:
                store.log({"event": "fixloop_card", "session_id": session_id, "task_id": created[0].get("task_id"),
                           "n": hit["n"], "issue": hit["issue"]})
            elif created:  # the tool refused (e.g. a fenced subprocess of a Kanban worker): audit it, hint unchanged
                store.log({"event": "fixloop_card_error", "session_id": session_id,
                           "error": str(created[0].get("error") or created[0])[:300]})
            return extra
        except Exception as exc:
            log.debug("coach fix_loop: %s", exc)
            return None

    def _diff_review(session_id, so, hit, ask, log_lines, changed_paths, final_response, repos):
        """One bounded strong-model review of this turn's diff; its findings become the pre_verify nudge."""
        coach.loop_state(session_id)["diff_reviewed"] = coach.turn_number(session_id)
        t0 = time.time()
        since = getattr(coach, "turn_started", lambda _s: None)(session_id)
        try:
            v = so.diff_review(ask, final_response, log_lines, changed_paths, since, repos=repos)
        except Exception as exc:
            log.debug("coach diff_review: %s", exc)
            v = None
        store.log({"event": "diff_review", "session_id": session_id, "p": hit["p"], "ok": bool(v),
                   "repos": (v or {}).get("repos") or list(repos), "findings": len((v or {}).get("findings") or []),
                   "supported": (v or {}).get("supported"), "diff_chars": (v or {}).get("diff_chars"),
                   "secs": round(time.time() - t0, 1)})
        return so.review_nudge(v, hit.get("evidence") or "") if v else None

    def _lesson_bg(session_id, user_message, prev):
        h = _lesson(session_id, user_message, prev)
        if h:
            coach.queue_hint(session_id, h)

    def _lesson(session_id, user_message, prev=None):
        """A correction that implies a standing rule -> candidate lesson (promoted on recurrence) + agent hint."""
        try:
            ls = _load("lessons")
            if os.environ.get("COACH_LESSONS", "1").strip().lower() in ("0", "off", "false", "no"):
                return None
            if not isinstance(user_message, str):
                return None
            if prev is None:
                prev = coach.loop_state(session_id).get("last_reply") or ""
            lstore = ls.LessonStore(store.root)
            v = ls.assess(prev, user_message, ls.aux_judge, wide=True, known=lstore.known())
            if not v:
                return None
            c = lstore.add(v, session_id, example=user_message)
            if c["status"] == "saved":  # already in memory: just remind, don't ask to save again
                store.log({"event": "lesson", "session_id": session_id, "slug": v["slug"], "status": "saved"})
                return ls.hint(v, promoted=False)
            promoted = c["status"] == "promoted"
            store.log({"event": "lesson", "session_id": session_id, "slug": v["slug"], "status": c["status"],
                       "sessions": len(c["sessions"])})
            return ls.hint(v, promoted)
        except Exception as exc:
            log.debug("coach lessons: %s", exc)
            return None

    def _selfheal_run(fn, *args):
        """Self-heal detectors: fail-open, off the critical path unless selfheal_sync."""
        try:
            if _load("self_heal").mode() == "off":
                return
            if selfheal_sync:
                fn(*args)
            else:
                threading.Thread(target=fn, args=args, daemon=True).start()
        except Exception as exc:
            log.debug("coach self_heal: %s", exc)

    def _confirm(sh):
        """Second-stage judge: explicit `selfheal_confirm`, else (production, no injected judge) the configured
        `coach_defect_confirm` aux model, else none."""
        if selfheal_confirm != "auto":
            return selfheal_confirm
        return None if selfheal_llm else sh.confirm_judge()

    def _selfheal_complaint(session_id, user_message, prev, platform="", parent_session_id="", tool_log=None):
        try:
            sh = _load("self_heal")
            if not sh.user_facing(platform, parent_session_id):
                return
            hit = sh.detect_complaint(prev or "", user_message, llm=selfheal_llm, threshold=sh.threshold(),
                                      confirm=_confirm(sh),
                                      tool_log=tool_log)
            sh.handle(hit, session_id, store, evidence=sh.clean_user(user_message), platform=platform)
        except Exception as exc:
            log.debug("coach self_heal complaint: %s", exc)

    def _selfheal_admission(session_id, reply, platform="", parent_session_id="", tool_log=None, ask=""):
        try:
            sh = _load("self_heal")
            if not sh.user_facing(platform, parent_session_id):
                return
            hit = sh.detect_admission(reply, llm=selfheal_llm, threshold=sh.audit_threshold(), tool_log=tool_log,
                                      ask=ask,
                                      confirm=_confirm(sh))
            sh.handle(hit, session_id, store, evidence=reply[-300:], platform=platform)
        except Exception as exc:
            log.debug("coach self_heal admission: %s", exc)

    def post_llm_call(session_id="", assistant_response=None, platform="", parent_session_id="", **_):
        try:
            if session_id and isinstance(assistant_response, str) and assistant_response.strip():
                ask, log_lines = coach.turn_context(session_id)
                log_lines = list(log_lines or [])[-30:]
                _selfheal_run(_selfheal_admission, session_id, assistant_response, platform, parent_session_id,
                              log_lines, ask)
                coach.turn_end(session_id, assistant_response)
                coach.loop_state(session_id)["last_ask"] = coach.turn_context(session_id)[0][:1500]
                loop = {k: v for k, v in coach.loop_state(session_id).items() if k != "diff_reviewed"}
                store._update(session_id, lambda s: s.__setitem__("loop", loop))
        except Exception as exc:
            log.debug("coach post_llm_call: %s", exc)

    def pre_verify(session_id="", platform="", attempt=0, final_response="", changed_paths=None, **_):
        """Check the agent's final answer before the turn ends; a hit keeps the turn going with a nudge.
        At most one nudge per turn (attempt 0 only); fail-open on every error."""
        try:
            if attempt or not session_id or not final_response or platform in ("cron",):
                return None
            rc = _load("reply_check")
            cfg = rc.settings()
            ask, log_lines = coach.turn_context(session_id)
            so = _load("second_opinion")
            # unverified + this turn changed a git repo -> one bounded diff review on the strong model. The rule
            # joins the judged set only for such turns, so diff_review off (default) = the old behaviour exactly.
            repos = (so.changed_repos(log_lines, changed_paths)
                     if so.setting("diff_review") and "unverified" in rc.prefilter(final_response)
                     and coach.loop_state(session_id).get("diff_reviewed") != coach.turn_number(session_id) else {})
            only = list(cfg["rules"]) + (["unverified"] if repos and "unverified" not in cfg["rules"] else [])
            if not only:
                return None
            th_unv = so.setting("diff_review_threshold")
            thresholds = {"unverified": th_unv} if repos and th_unv is not None else None
            s1 = _load("system_one")
            m = s1.mode()
            screen = ((lambda cands: s1.reply_screen(cands, ask, log_lines, final_response, rc.profile_text()))
                      if m != "off" else None)
            hit = rc.check(final_response, ask, log_lines, rc.aux_judge, threshold=cfg["threshold"],
                           only=only, changed_paths=changed_paths,
                           wake=lambda: rc.wake_sources(session_id, log_lines),
                           screen=screen, screen_below=s1.below(), shadow=m == "shadow", log=s1.log,
                           **({"thresholds": thresholds} if thresholds else {}))
            if hit and hit["rule"] == "unverified" and repos:
                msg = _diff_review(session_id, so, hit, ask, log_lines, changed_paths, final_response, repos)
                if msg:
                    return {"action": "continue", "message": msg}
                if "unverified" not in cfg["rules"]:  # added only for the review: fall back to the next real rule
                    v = hit["verdict"]
                    rest = [r for r in cfg["rules"] if r in rc.prefilter(final_response) and v.get(r, 0) >= cfg["threshold"]
                            and r != "unwatched"]
                    hit = None
                    if rest:
                        r = max(rest, key=lambda x: v[x])
                        hit = {"rule": r, "p": v[r], "evidence": v["evidence"],
                               "nudge": rc.NUDGES[r].format(evidence=v["evidence"] or "see your last message")}
            if not hit:
                store.log({"event": "reply_checked", "session_id": session_id, "platform": platform,
                           "candidates": rc.prefilter(final_response)})
                return None
            store.log({"event": "reply_nudge", "session_id": session_id, "rule": hit["rule"],
                       "p": hit["p"], "evidence": hit["evidence"]})
            return {"action": "continue", "message": hit["nudge"]}
        except Exception as exc:
            log.debug("coach pre_verify: %s", exc)
            return None

    return coach, {"pre_tool_call": pre_tool_call, "pre_verify": pre_verify, "post_llm_call": post_llm_call, "pre_llm_call": pre_llm_call, "post_tool_call": post_tool_call,
                   "transform_tool_result": transform_tool_result}


def register(ctx) -> None:
    _, hooks = build()
    for name, fn in hooks.items():
        ctx.register_hook(name, fn)
