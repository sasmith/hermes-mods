"""user-steps tests: fake LLM, fake board + state.db under a temp HERMES_HOME. No network, no real home."""
import importlib.util
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


core = _load("user_steps_core", ROOT / "core.py")
sys.modules["user_steps_core"] = core


# ---- fixtures ------------------------------------------------------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    db = sqlite3.connect(tmp_path / "state.db")
    db.execute("create table sessions (id text primary key, parent_session_id text, end_reason text, source text)")
    db.commit()
    db.close()
    return tmp_path


def add_session(home, sid, parent=None, end_reason=None, source="desktop"):
    db = sqlite3.connect(home / "state.db")
    db.execute("insert or replace into sessions values (?,?,?,?)", (sid, parent, end_reason, source))
    db.commit()
    db.close()


class FakeBoard(core.Cards):
    """A throwaway kanban.db under the temp HERMES_HOME (tasks: id, title, body, status)."""

    def __init__(self, home):
        self.db = home / "kanban.db"
        c = sqlite3.connect(self.db)
        c.execute("create table if not exists tasks (id text primary key, title text, body text, status text,"
                  " key text, chat text)")
        c.commit()
        c.close()
        self.n = 0

    def _q(self, sql, args=()):
        c = sqlite3.connect(self.db)
        try:
            r = c.execute(sql, args).fetchall()
            c.commit()
            return r
        finally:
            c.close()

    def create(self, session_id, title, body, key):
        hit = self._q("select id from tasks where key=?", (key,))
        if hit:
            return {"id": hit[0][0], "board": "default"}
        self.n += 1
        tid = f"t_{self.n:08x}"
        self._q("insert into tasks values (?,?,?,?,?,?)", (tid, title, body, "blocked", key, session_id))
        return {"id": tid, "board": "default"}

    def update(self, card, title, body):
        self._q("update tasks set title=?, body=? where id=?", (title, body, card["id"]))

    def archive(self, card):
        self._q("update tasks set status='archived' where id=?", (card["id"],))
        return True

    def rows(self):
        return self._q("select id, title, body, status, chat from tasks order by id")


def fake_llm(payload):
    calls = []

    def llm(system, user):
        calls.append(user)
        return json.dumps(payload) if not callable(payload) else json.dumps(payload(user))

    llm.calls = calls
    return llm


def boom(system, user):
    raise TimeoutError("aux model down")


def ledger(home):
    return core.Ledger(home / "user-steps", home / "state.db")


def run(home, reply, sid="S1", llm=None, cards=None, user_msg="", framed=True):
    return core.process_reply(reply, sid, user_msg=user_msg, llm=llm, ledger=ledger(home), cards=cards,
                              framed=framed, session_ref=f"@session:default/{sid}")


def directives(text):
    return re.findall(r'::user-step\{id="([^"]+)" s="([^"]*)"(?: t="([^"]*)")?\}', text)


# ---- extractor: one case per kind ----------------------------------------------------------------------

APPROVE = "Build 412 passed every check.\n\nWant me to deploy build 412 to prod now?"
DO = "The firmware is staged.\n\nPlug the watch into the charger and tell me when it shows the green ring."
CHOOSE = "Two ways to fix it.\n\nShould I use Postgres or SQLite for the cache? I recommend SQLite."
INPUT = "I need one more thing.\n\nWhat Wi-Fi network name should the hub join?"


def test_approve_step_is_recorded_framed_and_carded(home):
    add_session(home, "S1")
    board = FakeBoard(home)
    llm = fake_llm({"steps": [{"text": "Approve deploying build 412 to prod", "kind": "approve",
                               "verbatim_span": "Want me to deploy build 412 to prod now?"}], "resolved": []})
    out = run(home, APPROVE, llm=llm, cards=board)
    assert out["source"] == "model" and len(out["steps"]) == 1
    st = out["steps"][0]
    assert st["kind"] == "approve" and st["id"].startswith("us_") and st["status"] == "open"
    # the paragraph became a directive that carries its text; the other paragraph is untouched
    assert out["text"].startswith("Build 412 passed every check.\n\n::user-step{")
    (ids, s, t), = directives(out["text"])
    assert ids == st["id"] and s == "S1" and core.dec(t) == "Want me to deploy build 412 to prod now?"
    # ledger + rolling card
    assert [x["id"] for x in ledger(home).open_steps("S1")] == [st["id"]]
    (tid, title, body, status, chat), = board.rows()
    assert status == "blocked" and chat == "S1" and "Approve deploying build 412" in title and st["id"] in body


def test_do_choose_input_kinds(home):
    add_session(home, "S1")
    cases = [
        (DO, {"text": "Plug the watch into the charger", "kind": "do",
              "verbatim_span": "Plug the watch into the charger and tell me when it shows the green ring."}, "do", []),
        (CHOOSE, {"text": "Pick the cache backend", "kind": "choose", "options": ["SQLite", "Postgres"],
                  "verbatim_span": "Should I use Postgres or SQLite for the cache?"}, "choose", ["SQLite", "Postgres"]),
        (INPUT, {"text": "Tell me the Wi-Fi network name", "kind": "input",
                 "verbatim_span": "What Wi-Fi network name should the hub join?"}, "input", []),
    ]
    for i, (reply, step, kind, opts) in enumerate(cases):
        sid = f"K{i}"
        add_session(home, sid)
        out = run(home, reply, sid=sid, llm=fake_llm({"steps": [step], "resolved": []}))
        assert [s["kind"] for s in out["steps"]] == [kind], reply
        assert out["steps"][0]["options"] == opts
        assert len(directives(out["text"])) == 1


def test_choose_without_options_degrades_to_input_and_bad_kind_to_input(home):
    add_session(home, "S1")
    out = run(home, CHOOSE, llm=fake_llm({"steps": [
        {"text": "Pick one", "kind": "choose", "options": ["SQLite"], "verbatim_span": "Should I use Postgres or SQLite"},
        {"text": "Say which you recommend", "kind": "weird", "verbatim_span": "I recommend SQLite."}]}))
    assert [s["kind"] for s in out["steps"]] == ["input", "input"]
    # both spans live in the same paragraph -> ONE directive naming both ids
    (ids, _, _), = directives(out["text"])
    assert ids.split(",") == [s["id"] for s in out["steps"]]


# ---- no steps: mention-only, hypothetical, code blocks -------------------------------------------------

MENTION = "Deployed build 412; it is live on prod and the smoke test passed. Nothing is waiting on you."
HYPO = ("All done. If you ever want me to roll it back, run `hermes rollback 412`.\n\n"
        "Unless you say otherwise, I'll leave the canary at 5%.")
CODE = ("Here is the script:\n\n```bash\n# should I keep this? want me to delete it?\nrm -rf build/\n```\n\n"
        "> Want me to deploy it? (quoting your old message)\n\nThat's the whole change.")


@pytest.mark.parametrize("reply", [MENTION, HYPO, CODE])
def test_regex_fallback_finds_no_steps_in_mentions_hypotheticals_code(home, reply):
    add_session(home, "S1")
    board = FakeBoard(home)
    out = run(home, reply, llm=boom, cards=board)
    assert out["steps"] == [] and out["text"] is None and board.rows() == []


@pytest.mark.parametrize("reply,span", [
    (HYPO, "If you ever want me to roll it back, run `hermes rollback 412`."),
    (HYPO, "Unless you say otherwise, I'll leave the canary at 5%."),
    (CODE, "should I keep this? want me to delete it?"),
    (CODE, "Want me to deploy it?"),
    (MENTION, "Want me to restart the gateway?"),  # hallucinated: not in the reply at all
])
def test_guards_drop_model_steps_that_are_hypothetical_quoted_coded_or_invented(home, reply, span):
    add_session(home, "S1")
    out = run(home, reply, llm=fake_llm({"steps": [{"text": "x", "kind": "approve", "verbatim_span": span}]}))
    assert out["steps"] == [] and out["text"] is None


def test_regex_fallback_still_catches_a_real_ask(home):
    add_session(home, "S1")
    out = run(home, APPROVE, llm=boom)
    assert out["source"] == "regex" and "aux model down" in out["error"]
    assert [s["kind"] for s in out["steps"]] == ["approve"]
    assert len(directives(out["text"])) == 1


def test_plain_report_with_nothing_open_skips_the_model(home):
    add_session(home, "S1")
    llm = fake_llm({"steps": []})
    out = run(home, "Merged PR 12 and deployed it. Tests: 41 passed.", llm=llm)
    assert out["source"] == "skip" and llm.calls == []


# ---- resolve by the user's message ---------------------------------------------------------------------

def test_user_message_resolves_open_step_and_archives_card(home):
    add_session(home, "S1")
    board = FakeBoard(home)
    first = run(home, DO, llm=fake_llm({"steps": [{"text": "Plug the watch in", "kind": "do",
                                                   "verbatim_span": "Plug the watch into the charger"}]}), cards=board)
    sid = first["steps"][0]["id"]
    assert board.rows()[0][3] == "blocked"
    llm = fake_llm(lambda user: {"steps": [], "resolved": [sid] if "green ring now" in user else []})
    out = run(home, "Great, I see the watch online. Flashing now.", user_msg="done, green ring now", llm=llm,
              cards=board)
    assert out["resolved"] == [sid] and out["text"] is None
    assert sid in llm.calls[0] and "done, green ring now" in llm.calls[0]  # open steps + user msg reach the model
    assert ledger(home).open_steps("S1") == []
    step = ledger(home).read("S1")["steps"][0]
    assert step["status"] == "resolved" and step["resolved_by"] == "model"
    assert board.rows()[0][3] == "archived" and ledger(home).read("S1")["card"] is None


def test_model_cannot_resolve_unknown_ids(home):
    add_session(home, "S1")
    run(home, DO, llm=fake_llm({"steps": [{"text": "Plug in", "kind": "do", "verbatim_span": "Plug the watch"}]}))
    out = run(home, "ok?", llm=fake_llm({"steps": [], "resolved": ["us_nope"]}))
    assert out["resolved"] == [] and len(ledger(home).open_steps("S1")) == 1


# ---- ledger lineage ------------------------------------------------------------------------------------

def test_ledger_follows_compression_lineage_not_subagents(home):
    add_session(home, "A", end_reason="compression")
    add_session(home, "B", parent="A", end_reason="compressed")
    add_session(home, "C", parent="B")
    add_session(home, "SUB", parent="C", source="subagent")  # parent C is not compressed -> own root
    assert core.lineage("C", home / "state.db") == ["C", "B", "A"]
    lg = ledger(home)
    assert lg.root("C") == "A" and lg.root("SUB") == "SUB"
    out = run(home, APPROVE, sid="A", llm=fake_llm({"steps": [{"text": "Approve deploy", "kind": "approve",
                                                               "verbatim_span": "Want me to deploy build 412"}]}))
    sid = out["steps"][0]["id"]
    assert [s["id"] for s in lg.open_steps("C")] == [sid]  # visible from the compressed tip
    assert lg.open_steps("SUB") == []
    assert (home / "user-steps" / "A.json").is_file()
    assert lg.find(sid, "C")[0] == "A" and lg.find(sid)[0] == "A"  # by hint and by scan


# ---- rolling card lifecycle + REST resolve -------------------------------------------------------------

def test_rolling_card_create_update_archive_via_rest(home):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    api = _load("user_steps_api", ROOT / "dashboard" / "plugin_api.py")
    add_session(home, "S1")
    board = FakeBoard(home)
    a = run(home, APPROVE, llm=fake_llm({"steps": [{"text": "Approve deploy", "kind": "approve",
                                                    "verbatim_span": "Want me to deploy build 412"}]}), cards=board)
    b = run(home, INPUT, llm=fake_llm({"steps": [{"text": "Name the Wi-Fi", "kind": "input",
                                                  "verbatim_span": "What Wi-Fi network name"}]}), cards=board)
    ids = [a["steps"][0]["id"], b["steps"][0]["id"]]
    rows = board.rows()
    assert len(rows) == 1 and "(+1 more)" in rows[0][1] and all(i in rows[0][2] for i in ids)  # ONE card, updated

    app = FastAPI()
    app.include_router(api.create_router(home / "user-steps", home / "state.db", cards=board),
                       prefix="/api/plugins/user-steps")
    c = TestClient(app)
    assert c.get("/api/plugins/user-steps/ping").json()["api"] >= 1
    got = c.get("/api/plugins/user-steps/steps", params={"session": "S1"}).json()
    assert [s["id"] for s in got["steps"]] == ids and got["card"]["id"] == rows[0][0]  # oldest first
    one = c.get("/api/plugins/user-steps/step", params={"id": ids[0]}).json()["step"]
    assert one["kind"] == "approve" and one["paragraph"].startswith("Want me to deploy")

    r = c.post("/api/plugins/user-steps/resolve", json={"id": ids[0], "session": "S1", "resolution": "Go"}).json()
    assert r["resolved"] == [ids[0]] and r["card"]["id"] == rows[0][0]
    assert board.rows()[0][3] == "blocked" and ids[0] not in board.rows()[0][2]  # body rewritten, still open
    r = c.post("/api/plugins/user-steps/resolve", json={"id": ids[1], "session": "S1"}).json()
    assert r["card"] is None and board.rows()[0][3] == "archived"
    after = c.get("/api/plugins/user-steps/steps", params={"session": "S1"}).json()
    assert after["steps"] == [] and after["card"] is None and after["own_cards"] == [rows[0][0]]
    # a resolved step stays resolved; resolving again is a no-op
    assert c.post("/api/plugins/user-steps/resolve", json={"id": ids[1]}).json()["resolved"] == []
    # a NEW step after archival opens a NEW card (fresh idempotency generation)
    run(home, DO, llm=fake_llm({"steps": [{"text": "Plug in", "kind": "do", "verbatim_span": "Plug the watch"}]}),
        cards=board)
    assert [r[3] for r in board.rows()] == ["archived", "blocked"]


def test_archive_failure_keeps_card_pointer(home):
    add_session(home, "S1")

    class Stuck(FakeBoard):
        def archive(self, card):
            return False

    board = Stuck(home)
    out = run(home, APPROVE, llm=fake_llm({"steps": [{"text": "Approve", "kind": "approve",
                                                      "verbatim_span": "Want me to deploy"}]}), cards=board)
    lg = ledger(home)
    core.resolve(lg, "S1", [out["steps"][0]["id"]], "Go", "desktop")
    assert core.sync_card(lg, "S1", "S1", "", board)["id"] == board.rows()[0][0]
    assert lg.read("S1")["card"] is not None


# ---- frame: text is never lost -------------------------------------------------------------------------

def test_attr_encoding_roundtrip_and_grammar():
    s = 'Reply "yes" or {no} \\ then\nnew line'
    e = core.enc(s)
    assert '"' not in e and "{" not in e and "}" not in e and "\n" not in e
    assert core.dec(e) == s


def test_long_paragraph_falls_back_to_line_then_to_appending(home):
    long_para = ("Context " * 140).strip() + "\nWant me to ship it?"
    steps = [{"id": "us_a", "verbatim_span": "Want me to ship it?"}]
    out = core.frame(long_para, steps, "S1")
    assert out.startswith("Context Context") and out.endswith('::user-step{id="us_a" s="S1" t="Want me to ship it?"}')
    huge_line = "Want me to ship it? " + "because " * 200
    out = core.frame(huge_line, steps, "S1")
    assert out.startswith(huge_line) and out.endswith('::user-step{id="us_a" s="S1"}')  # text kept, no t


def test_framing_off_for_non_desktop_keeps_text(home):
    add_session(home, "S1", source="cli")
    out = run(home, APPROVE, framed=False, llm=fake_llm({"steps": [{"text": "Approve", "kind": "approve",
                                                                    "verbatim_span": "Want me to deploy"}]}))
    assert out["text"] is None and len(out["steps"]) == 1


# ---- hooks ---------------------------------------------------------------------------------------------

def _plugin(monkeypatch, home):
    monkeypatch.setitem(sys.modules, "user_steps_core", core)
    mod = _load("user_steps_plugin", ROOT / "__init__.py")
    return mod


def test_pre_llm_call_note_only_when_steps_are_open(home, monkeypatch):
    plugin = _plugin(monkeypatch, home)
    monkeypatch.setattr(plugin, "_ledger", lambda: ledger(home))
    add_session(home, "S1")
    assert plugin.pre_llm_call(session_id="S1", user_message="hi", platform="tui") is None
    run(home, APPROVE, llm=fake_llm({"steps": [{"text": "Approve deploy", "kind": "approve",
                                                "verbatim_span": "Want me to deploy"}]}))
    note = plugin.pre_llm_call(session_id="S1", user_message="what's left?", platform="tui")["context"]
    assert "[user-steps]" in note and "Approve deploy" in note
    assert plugin._USER_MSG["S1"] == "what's left?"
    assert plugin.pre_llm_call(session_id="S1", platform="cron") is None


def test_transform_skips_workers_and_subagents(home, monkeypatch):
    plugin = _plugin(monkeypatch, home)
    called = []
    monkeypatch.setattr(plugin.core, "process_reply", lambda *a, **k: called.append(1) or {"text": "X"})
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    assert plugin.transform_llm_output(response_text=APPROVE, session_id="S1", platform="tui") is None
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    assert plugin.transform_llm_output(response_text=APPROVE, session_id="S1", platform="subagent") is None
    assert called == []
    assert plugin.transform_llm_output(response_text=APPROVE, session_id="S1", platform="tui") == "X"


def test_transform_fails_open(home, monkeypatch):
    plugin = _plugin(monkeypatch, home)

    def explode(*a, **k):
        raise RuntimeError("bad")

    monkeypatch.setattr(plugin.core, "process_reply", explode)
    assert plugin.transform_llm_output(response_text=APPROVE, session_id="S1", platform="tui") is None


def test_imperative_after_a_comma_or_or_reaches_the_model(home):
    # A real reply that skipped the model: no "you", no question, verbs after ", " and "or ".
    add_session(home, "S1")
    llm = fake_llm({"steps": []})
    reply = ('The new connection bar is in iPhone build 530. To get it, tap the green "Update available" bar '
             'at the top of the app, or go to Settings → This phone → Install update.')
    out = run(home, reply, llm=llm)
    assert out["source"] != "skip" and len(llm.calls) == 1
    assert core._worth_asking("Shipped it. Logs are clean, and the release is green.") is False


# ---- terminal hand-offs never become For-you steps --------------------------------------------------------

HANDOFF_REPLY = ("The app on her desktop is quit and I couldn't reopen it.\n\n"
                 "If you'd rather I SSH in, add `~/.ssh/id_ed25519.pub` to her account's `~/.ssh/authorized_keys`.\n\n"
                 "Plug the USB drive into the box and tell me when it's in.")


def test_terminal_doable_step_is_dropped_physical_kept(home):
    add_session(home, "S1")
    board = FakeBoard(home)
    llm = fake_llm({"steps": [
        {"text": "Add the ssh key to her authorized_keys", "kind": "do",
         "verbatim_span": "add `~/.ssh/id_ed25519.pub` to her account's `~/.ssh/authorized_keys`"},
        {"text": "Plug the USB drive into the box", "kind": "do",
         "verbatim_span": "Plug the USB drive into the box and tell me when it's in."}]})
    out = run(home, HANDOFF_REPLY, llm=llm, cards=board)
    assert [s["text"] for s in out["steps"]] == ["Plug the USB drive into the box"]
    assert out["dropped"] == ["Add the ssh key to her authorized_keys"]
    assert "authorized_keys" not in (board.rows()[0][1] + board.rows()[0][2])


def test_run_this_fence_step_is_dropped(home):
    add_session(home, "S1")
    reply = "Spotlight is the biggest cost.\n\nTo clear it, run this on the box:\n```\nsudo killall fseventsd\n```"
    llm = fake_llm({"steps": [{"text": "Run sudo killall fseventsd on the box", "kind": "do",
                               "verbatim_span": "To clear it, run this on the box:"}]})
    out = run(home, reply, llm=llm)
    assert out["steps"] == [] and out["dropped"]


def test_approval_to_run_a_command_is_kept(home):
    add_session(home, "S1")
    reply = "Sleep is on.\n\nWant me to run `sudo pmset -a sleep 0` on the box?"
    llm = fake_llm({"steps": [{"text": "Approve turning off sleep on the box", "kind": "approve",
                               "verbatim_span": "Want me to run `sudo pmset -a sleep 0` on the box?"}]})
    out = run(home, reply, llm=llm)
    assert [s["kind"] for s in out["steps"]] == ["approve"] and out["dropped"] == []
