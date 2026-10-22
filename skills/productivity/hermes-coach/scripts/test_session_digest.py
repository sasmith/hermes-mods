#!/usr/bin/env python3
"""Stdlib tests for session_digest.py:  python3 test_session_digest.py"""
import json, sqlite3, sys, tempfile, time, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import session_digest as sd  # noqa: E402

SCHEMA = """
create table sessions (id text primary key, source text, model text, parent_session_id text, started_at real,
  ended_at real, end_reason text, message_count int, tool_call_count int, api_call_count int, input_tokens int,
  output_tokens int, cache_read_tokens int, actual_cost_usd real, estimated_cost_usd real, title text,
  last_activity_at real);
create table messages (id integer primary key autoincrement, session_id text, role text, content text,
  tool_name text, tool_calls text, timestamp real, active int not null default 1, compacted int not null default 0,
  display_kind text, display_metadata text, message_uid text);
"""


def make_db(path, rows):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    now = time.time()
    con.execute("insert into sessions (id, source, started_at, last_activity_at) values ('s1','desktop',?,?)", (now, now))
    for r in rows:
        con.execute("insert into messages (session_id, role, content, timestamp, active, compacted, display_kind,"
                    " display_metadata, message_uid) values ('s1',?,?,?,?,?,?,?,?)", r)
    con.commit(); con.close()


class DigestDedupe(unittest.TestCase):
    def test_compaction_copies_and_queued_placeholders_count_once(self):
        t = time.time() - 100
        shot = "redesign it\n@image:`/Users/x/Library/Application Support/Hermes/composer-images/s.png`\n[screenshot]"
        rows = [
            ("user", "first ask", t, 0, 1, None, None, "u1"),
            ("assistant", "ok", t + 1, 0, 1, None, None, "a1"),
            # queued placeholder, re-written on every compaction pass (rewound: active=0, compacted=0)
            ("user", "redesign it", t + 2, 0, 0, None, '{"_queued_prompt": true}', "q1"),
            ("user", "redesign it", t + 2, 0, 0, None, '{"_queued_prompt": true}', "q1"),
            # compaction re-materialized copies of the first turn
            ("user", "first ask", t, 0, 1, None, None, "u1"),
            ("user", "first ask", t, 1, 0, None, None, "u1"),
            # the real drained send, with its screenshot
            ("user", shot, t + 3, 1, 0, None, None, "u2"),
            ("user", "secret plumbing", t + 4, 1, 0, "hidden", None, "h1"),
        ]
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "state.db"
            make_db(db, rows)
            (dg,) = list(sd.digest_db("default", db, "test", 0))
        texts = [u["text"] for u in dg["user_turns"]]
        self.assertEqual(texts, ["first ask", "redesign it\n[screenshot]"])
        self.assertEqual(dg["user_turns_total"], 2)
        self.assertEqual(dg["signals"].get("screenshot"), 1)
        self.assertEqual(dg["msgs"], 3)

    def test_legacy_schema_without_active_columns(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "state.db"
            con = sqlite3.connect(db)
            con.executescript(SCHEMA.split("create table messages")[0] +
                              "create table messages (id integer primary key, session_id text, role text,"
                              " content text, tool_name text, tool_calls text, timestamp real);")
            now = time.time()
            con.execute("insert into sessions (id, started_at) values ('s1', ?)", (now,))
            con.execute("insert into messages (session_id, role, content, timestamp) values ('s1','user','hi',?)", (now,))
            con.commit(); con.close()
            (dg,) = list(sd.digest_db("default", db, "test", 0))
        self.assertEqual([u["text"] for u in dg["user_turns"]], ["hi"])


class DigestFeatures(unittest.TestCase):
    def test_feature_counts_from_tool_args_and_tool_call_wrapper(self):
        t = time.time() - 100
        call = lambda name, args: {"type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        calls = [
            call("kanban_create", {"title": "x", "assignee": "reviewer", "parents": ["t_1"], "max_runtime_seconds": 3600,
                                   "workspace_kind": "worktree", "goal_mode": True}),
            call("kanban_create", {"title": "y", "assignee": "default", "triage": True}),
            call("terminal", {"command": "hermes kanban boards create babybear && git worktree add ../wt"}),
            call("terminal", {"command": "grep -w foo ~/.hermes/config.yaml"}),
            call("tool_call", {"calls": [{"name": "todo_list", "arguments": {"todos": []}},
                                         {"name": "cronjob_manage", "arguments": {"action": "create"}}]}),
        ]
        rows = [("user", "go", t, 1, 0, None, None, "u1"),
                ("assistant", "", t + 1, 1, 0, None, None, "a1")]
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "state.db"
            make_db(db, rows)
            con = sqlite3.connect(db)
            con.execute("update messages set tool_calls=? where message_uid='a1'", (json.dumps(calls),))
            con.commit(); con.close()
            (dg,) = list(sd.digest_db("default", db, "test", 0))
        f = dg["features"]
        self.assertEqual(f["kanban_create"], 2)
        for k in ("max_runtime", "parents", "worktree", "goal_mode", "specialist", "triage"):
            self.assertEqual(f[f"kanban_create:{k}"], 1, k)
        self.assertEqual((f["kanban_board_create"], f["git_worktree"], f["todo"], f["cron_create"]), (1, 1, 1, 1))
        self.assertNotIn("hermes_worktree", f)  # `grep -w` on a ~/.hermes path is not `hermes -w`
        self.assertEqual(dg["tools"].get("tool_call"), 1)  # inner calls feed features, not tool counts
        self.assertNotIn("todo_list", dg["tools"])
        self.assertEqual(dg["digest_v"], sd.DIGEST_V)


class DigestSynthetic(unittest.TestCase):
    def test_kanban_notices_cron_prompts_and_continuation_notes_are_not_user_turns(self):
        t = time.time() - 100
        texts = [
            "✔ [default] @default Kanban t_x done — still failing",
            "⏸ [default] @default Kanban t_x blocked: stuck waiting on the hub, same error again",
            "✖ [default] @default Kanban t_x worker crashed (pid gone); dispatcher will retry",
            "⏱ Kanban t_x timed out (max_runtime=60s); will retry",
            '[Cronjob "[bot:coach] Daily mine" output — scheduled job, not the user.] still broken, stuck again',
            "[Continuing toward this kanban task — judge says it is not done yet]\nReason: still broken",
            "[Your active task list was preserved across context compression]\n- [>] fix the stuck thing again",
            "[Current user objective preserved from compacted history]\nit still doesn't work",
            "[Turn stopped by Desktop before it finished.]",
        ]
        rows = [("user", s, t + i, 1, 0, None, None, f"u{i}") for i, s in enumerate(texts)]
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "state.db"
            make_db(db, rows)
            (dg,) = list(sd.digest_db("default", db, "test", 0))
        self.assertEqual(dg["user_turns"], [])
        self.assertEqual(dg["user_turns_total"], 0)
        s = dg["signals"]
        self.assertNotIn("repeat_fix", s)
        self.assertNotIn("stuck", s)
        self.assertEqual(s.get("synthetic_kanban"), 4)
        self.assertEqual(s.get("synthetic_cron"), 1)
        self.assertEqual((s.get("synthetic_kanban_continue"), s.get("synthetic_todo_preserved"),
                          s.get("synthetic_objective_preserved"), s.get("synthetic_turn_stopped")), (1, 1, 1, 1))

    def test_user_turn_mentioning_kanban_is_kept(self):
        t = time.time() - 100
        rows = [("user", "Kanban t_x is still broken", t, 1, 0, None, None, "u1"),
                ("user", "✔ looks good, ship it", t + 1, 1, 0, None, None, "u2")]
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "state.db"
            make_db(db, rows)
            (dg,) = list(sd.digest_db("default", db, "test", 0))
        self.assertEqual(dg["user_turns_total"], 2)
        self.assertEqual(dg["signals"].get("repeat_fix"), 1)

    def test_each_user_turn_carries_the_agent_reply_before_it(self):
        t = time.time() - 100
        long = "Shipped the fix to /Users/x/code/app. " + "detail " * 200 + "Want me to also restart the pod?"
        rows = [("user", "fix the sidebar", t, 1, 0, None, None, "u1"),
                ("assistant", long, t + 1, 1, 0, None, None, "a1"),
                ("assistant", "", t + 2, 1, 0, None, None, "a2"),  # an empty tool-call turn is not the reply
                ("user", "nope, still broken", t + 3, 1, 0, None, None, "u2")]
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "state.db"
            make_db(db, rows)
            (dg,) = list(sd.digest_db("default", db, "test", 0))
        first, second = dg["user_turns"]
        self.assertNotIn("prev", first)
        self.assertTrue(second["prev"].startswith("Shipped the fix to ~/code/app."))
        self.assertTrue(second["prev"].endswith("Want me to also restart the pod?"))
        self.assertIn("…[clipped]…", second["prev"])
        self.assertLessEqual(len(second["prev"]), sd.PREV_HEAD + sd.PREV_TAIL + 20)
        self.assertEqual(dg["digest_v"], 6)


class ExcludeProfileGlob(unittest.TestCase):
    def test_glob_drops_follower_profile(self):
        self.assertTrue(sd.excluded_profile("alpha-follow", ["alpha*"]))
        self.assertTrue(sd.excluded_profile("alpha", ["alpha*"]))
        self.assertFalse(sd.excluded_profile("default", ["alpha*"]))
        self.assertTrue(sd.excluded_profile("alpha", ["alpha"]))  # exact names still work
        self.assertFalse(sd.excluded_profile("alpha-follow", ["alpha"]))


if __name__ == "__main__":
    unittest.main()
