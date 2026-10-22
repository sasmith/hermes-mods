import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


core = _load("oa_core_t", "core.py")
plugin = _load("oa_plugin_t", "__init__.py")

# Real replies from the session that motivated the plugin.
ASKED = ("The passwordless sudo rule on the mini from my earlier message still needs your yes or no.")
ASKED2 = "Want me to set up the passwordless rule on the mini?"
NOT_ASKED = [
    "That card is the only thing that needs you. Card F is waiting on a timer, not on you.",  # points at card
    "No action needed from you; card F wakes at 2 AM PT.",
    "If you ever want the external drive back, say so.",
    "You approved the plan earlier, so I went ahead.",
    "```\nWant me to run this?\n```",
    "> Should I delete the backups?",
]


@pytest.mark.parametrize("text", [ASKED, ASKED2,
                                  "Should I archive the stale cards?",
                                  "Reply yes and I'll delete them.",
                                  "Deleting these needs your go-ahead.",
                                  "Let me know which option you want."])
def test_detects_real_asks(text):
    assert core.find_asks(text)


@pytest.mark.parametrize("text", NOT_ASKED[1:])
def test_ignores_non_asks(text):
    assert core.find_asks(text) == []


def test_status_report_is_not_an_ask():
    report = ("Card A is done. Every pod runs build 1582 and pruned its own updates. "
              "Card F starts now and reports tomorrow evening at the earliest.")
    assert core.find_asks(report) == []


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    db = tmp_path / "kanban.db"
    c = sqlite3.connect(db)
    c.execute("create table tasks (id text primary key, status text)")
    c.commit(); c.close()
    made = []

    def fake_create(session_id, asks, reply):
        tid = "t_%08x" % (len(made) + 0xabc0)
        made.append((tid, session_id, asks))
        c = sqlite3.connect(db); c.execute("insert into tasks values (?, 'blocked')", (tid,)); c.commit(); c.close()
        return tid
    monkeypatch.setattr(plugin, "_create_card", fake_create)
    return tmp_path, db, made


def test_ask_in_chat_files_one_blocked_card_and_says_so(env):
    _, _, made = env
    plugin.pre_llm_call(session_id="s1", platform="tui")
    out = plugin.transform_llm_output(response_text="Done.\n\n" + ASKED2, session_id="s1", platform="tui")
    assert len(made) == 1 and made[0][1] == "s1"
    assert made[0][0] in out and "waiting on you" in out


def test_no_card_when_turn_already_blocked_a_card(env):
    _, _, made = env
    plugin.pre_llm_call(session_id="s2", platform="tui")
    plugin.transform_tool_result(tool_name="kanban_create", args={"initial_status": "blocked"}, session_id="s2")
    assert plugin.transform_llm_output(response_text=ASKED2, session_id="s2", platform="tui") is None
    assert made == []


def test_no_card_when_ask_points_at_an_already_blocked_card(env):
    _, db, made = env
    c = sqlite3.connect(db); c.execute("insert into tasks values ('t_8fa00be7', 'blocked')"); c.commit(); c.close()
    plugin.pre_llm_call(session_id="s3", platform="tui")
    text = "Card t_8fa00be7 still needs your yes or no."
    assert plugin.transform_llm_output(response_text=text, session_id="s3", platform="tui") is None
    assert made == []


def test_no_card_when_ask_gate_refused_the_ask(env):
    tmp, _, made = env
    gate = tmp / "cache" / "ask-gate"
    gate.mkdir(parents=True)
    import time as _t
    (gate / "verdicts.json").write_text(json.dumps({"s7": {"t": _t.time(), "verdict": "unverified", "h": "x"}}))
    plugin.pre_llm_call(session_id="s7", platform="tui")
    assert plugin.transform_llm_output(response_text=ASKED2, session_id="s7", platform="tui") is None
    assert made == []
    # an ok verdict (or no record) files as before
    (gate / "verdicts.json").write_text(json.dumps({"s7": {"t": _t.time(), "verdict": "ok", "h": "x"}}))
    assert plugin.transform_llm_output(response_text=ASKED2, session_id="s7", platform="tui")
    assert len(made) == 1


def test_workers_and_cron_are_skipped(env, monkeypatch):
    _, _, made = env
    assert plugin.transform_llm_output(response_text=ASKED2, session_id="s4", platform="cron") is None
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    assert plugin.transform_llm_output(response_text=ASKED2, session_id="s4", platform="tui") is None
    assert made == []


def test_next_turn_reminds_until_card_is_closed(env):
    _, db, made = env
    plugin.pre_llm_call(session_id="s5", platform="tui")
    plugin.transform_llm_output(response_text=ASKED2, session_id="s5", platform="tui")
    tid = made[0][0]
    ctx = plugin.pre_llm_call(session_id="s5", platform="tui")
    assert ctx and tid in ctx["context"] and "archive" in ctx["context"]
    c = sqlite3.connect(db); c.execute("update tasks set status='archived' where id=?", (tid,)); c.commit(); c.close()
    assert plugin.pre_llm_call(session_id="s5", platform="tui") is None


def test_rate_limit(env):
    _, _, made = env
    for i in range(10):
        plugin.pre_llm_call(session_id="s6", platform="tui")
        plugin.transform_llm_output(response_text=f"Should I do thing number {i}?", session_id="s6", platform="tui")
    assert len(made) == plugin.MAX_CARDS_PER_HOUR


def test_card_linked_only_to_the_asking_chat(env, monkeypatch):
    _, db, _ = env
    c = sqlite3.connect(db)
    c.execute("create table kanban_notify_subs (task_id text, platform text, chat_id text)")
    c.execute("insert into kanban_notify_subs values ('t_x', 'tui', 'other-session')")
    c.commit(); c.close()
    calls = []
    monkeypatch.setattr(plugin, "_run", lambda argv: calls.append(argv))
    plugin._fix_subscription("t_x", "asking-session")
    assert ["kanban", "notify-subscribe", "t_x", "--platform", "tui", "--chat-id", "asking-session"] in calls
    assert ["kanban", "notify-unsubscribe", "t_x", "--platform", "tui", "--chat-id", "other-session"] in calls


def test_card_text():
    body = core.card_body("@session:default/s1", [ASKED2], "reply", "2026-10-21 11:00 PDT")
    assert ASKED2 in body and "unblocks" in body and "archives" in body
    assert core.card_title([ASKED2]).startswith("ASK: Want me to")
    assert core.ask_key("s1", [ASKED2]) == core.ask_key("s1", [ASKED2.lower()])
