import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import coach_askgate as g

HITS = ["why did you ask me to do cloudlfare work? how do i get you to do it yourself and not ask me?",
        "superhuman has been revoked for a while, i keep completing it",
        "YOU JUST SAID IT WAS CANCELED, WHY DO YOU KEEP ASKING ME",
        "why didnt you do this yourself, without me telling you",
        "are you waiting for me? i dont see any blocked cards",
        "[OUT-OF-BAND USER MESSAGE — x]\nWHY DO YOU KEEP ASKING ME\n[/OUT-OF-BAND USER MESSAGE]"]
MISSES = ["i keep asking what time is it and getting no reply",
          "When contacting someone for the first time, intro yourself as Babybear",
          "i want to give you the rights to do it yourself, give me those instructions",
          "[Slack engagement eng_1 · #x] why are you asking me",
          "Without any other tools: call kanban_comment ... do NOT send it yourself",
          "do it", "yes, figure it out",
          # third-party agent (Baby Bear), not Hermes asking David
          "why didnt the agent figure it out even with failing search? it could have just listed the devices",
          # David handing off unprompted, not correcting an ask
          "done. not sure if i hit the phases, listen yourself and possible split things up"]


class T(unittest.TestCase):
    def test_hits(self):
        for t in HITS:
            self.assertTrue(g.classify(g.clean(t)), t)

    def test_misses(self):
        for t in MISSES:
            self.assertIsNone(g.classify(g.clean(t)), t)

    def test_opening_turn_skipped(self):
        import json, tempfile, os
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(f"{d}/mini/default")
            turns = [{"t": 2000, "text": "whats this, i want it to stop asking me [screenshot]"},
                     {"t": 2100, "text": "why are you asking me to run things"}]
            json.dump({"id": "s1", "host": "mini", "user_turns": turns}, open(f"{d}/mini/default/s1.json", "w"))
            # text-only opener answering a carried-over ask still counts
            json.dump({"id": "s2", "host": "nous-1", "user_turns": [{"t": 3000, "text": "i dont plan to review. you should merge it yourself"}]},
                      open(f"{d}/mini/default/s2.json", "w"))
            hits, scanned = g.collect(d, 0, 10**10)
            self.assertEqual([h["quote"] for h in hits],
                             ["why are you asking me to run things", "i dont plan to review. you should merge it yourself"])



# had_to_ask_fix: paraphrased from real asks (no private names); each must land in the fix class, not ask-me.
FIX_HITS = {"make a card to fix": "card-to-fix",
            "and make a card to rootcause and fix the log shipper": "card-to-fix",
            "create a task to investigate why you did not do that, and fix it": "card-to-fix",
            "root cause it, and fix the plugin": "root-cause",
            "ROOT CAUSE WHY YOU KEPT GETTING THIS WRONG": "root-cause",
            "figure out why the plugin stopped working after the update": "figure-out-why",
            "dont just start working, do an analysis and fix for the problem": "analyze-and-fix",
            "i fixed this once, but now it's broken again": "broken-again",
            "i thought we mostly fixed it but it still shows up": "broken-again",
            "i hit do it, why did you not?": "why-did-you-not",
            "why are all the dots red? [screenshot]": "why-did-you-not",
            "the sessions keep getting re-shuffled, i think the sort is broken": "is-broken",
            "fix [screenshot]": "screenshot-fix",
            "fix this [Image attached: a.png]": "screenshot-fix"}
FIX_MISSES = ["fix the typo in the README",                      # an ordinary task, not a defect report
              # week of 2026-10-12 review: a status question, a conditional plan, two specs for card-filing mechanisms
              "hows the mini perf doing now, do we expect that we fixed the root causes",
              "dont build it from scratch, make babybear do it, and if it fails figure out why and fix it",
              "can we modify bb-tuner to measure the relevant timing and quality of watch coms, and then file issues to "
              "investigate and optimize?",
              "i want a process that looks at all sessions every 10 minutes, and makes sure their status (green|red) and "
              "linked tasks are correct. if not, it should create a kanban task to fix",
              "what is the root of this repo?",
              "[screenshot] here is the design i want",
              "work kanban task t_abc123",
              "Investigate this hermes-hub cron output and fix whatever it reports: x",
              "**Background:** find out why the service returns 403",   # a Hermes-written brief
              "some cards say \"still broken\" as a button",
              "why didnt you do this yourself, without me telling you"]  # ask-me class wins


class Fix(unittest.TestCase):
    def test_fix_hits(self):
        for t, want in FIX_HITS.items():
            self.assertIsNone(g.classify(g.clean(t)), t)
            self.assertEqual(g.classify_fix(g.clean(t)), want, t)

    def test_fix_misses(self):
        for t in FIX_MISSES:
            self.assertIsNone(g.classify_fix(g.clean(t)), t)

    def _data(self, d):
        import json, os
        os.makedirs(f"{d}/macbook/default")
        turns = [{"t": 2000, "text": "fix [screenshot]"},                       # opener: counts for the fix class
                 {"t": 2100, "text": "why are you asking me to run things"},     # ask-me
                 {"t": 2200, "text": "root cause and fix"}]
        json.dump({"id": "s1", "host": "macbook", "source": "desktop", "user_turns": turns},
                  open(f"{d}/macbook/default/s1.json", "w"))
        # a cron prompt that says "root cause" is not David asking
        json.dump({"id": "c1", "host": "macbook", "source": "cron", "user_turns": [{"t": 2300, "text": "root cause it"}]},
                  open(f"{d}/macbook/default/c1.json", "w"))

    def test_collect_classes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self._data(d)
            hits, _ = g.collect(d, 0, 10**10)
            self.assertEqual([(h["class"], h["pattern"]) for h in hits],
                             [(g.FIX_CLASS, "screenshot-fix"), (g.ASK_CLASS, "why-asking-me"), (g.FIX_CLASS, "root-cause")])
            body = g.post_body(hits, g.week_start(2000), ["## Self-heal lane", "none"])
            self.assertIn("**2** times David had to ask for a Hermes fix", body)
            self.assertIn("in 1 session,", body)
            self.assertIn("**1** correction where", body)
            self.assertIn("## Self-heal lane", body)
            md = g.render(hits, [g.week_start(2000)], {g.week_start(2000): 4}, "t")
            self.assertIn("| 1 | 2 (1) | 4 |", md)

    def test_marker_one_post_per_week(self):
        import json, tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            w = g.week_start(2000)
            self.assertIsNone(g.posted_elsewhere(d, w, "hub"))
            p = g.marker_path(d, w)
            p.parent.mkdir(parents=True)
            p.write_text(json.dumps({"posted_by": "hub"}))
            self.assertIsNone(g.posted_elsewhere(d, w, "hub"))       # our own marker: re-post updates the same card
            self.assertEqual(g.posted_elsewhere(d, w, "laptop"), "hub")
            p.write_text(json.dumps({"host": "hub"}))                # marker written before the rename
            self.assertEqual(g.posted_elsewhere(d, w, "laptop"), "hub")


class RouteToLane(unittest.TestCase):
    """The weekly review's Do-it work goes to the self-heal lane: each hit is filed as a monitor finding unless the
    lane already has that session; the card keeps its review prompt only for hits the lane did not take."""

    def hits(self):
        return [{"t": 1792600000, "session": "s_fix", "host": "macbook", "source": "cli", "class": g.FIX_CLASS,
                 "pattern": "root_cause", "quote": "root cause and fix this please"},
                {"t": 1792600100, "session": "s_ask", "host": "mini", "source": "cli", "class": g.ASK_CLASS,
                 "pattern": "do_it_yourself", "quote": "why are you asking me, do it yourself"},
                {"t": 1792600200, "session": "s_known", "host": "macbook", "source": "cli", "class": g.FIX_CLASS,
                 "pattern": "make_card", "quote": "make a card to fix"}]

    def home(self, lane):
        import json, tempfile
        h = Path(tempfile.mkdtemp())
        (h / "coach").mkdir()
        (h / "coach" / "selfheal_lane.json").write_text(json.dumps(lane))
        return h

    def test_live_lane_owns_all(self):
        filed = []
        home = self.home({"agent-0123456789": {"status": "filed", "session_id": "s_known"}})
        owned, rest = g.route_hits(self.hits(), home, filer=lambda d: filed.append(d) or {"action": "filed"})
        self.assertEqual(rest, [])
        self.assertEqual(len(owned), 3)
        self.assertEqual([d["session_id"] for d in filed], ["s_fix", "s_ask"])  # the known session is not re-filed
        self.assertEqual(filed[0]["component"], "plugin")
        self.assertIn("detector missed a fix request", filed[0]["symptom"])
        self.assertEqual(filed[1]["component"], "agent")
        self.assertTrue(all(d["source"] == "signal" and d["kind"] == "monitor" for d in filed))

    def test_shadow_keeps_the_review(self):
        owned, rest = g.route_hits(self.hits(), self.home({}), filer=lambda d: {"action": "would_file"})
        self.assertEqual(owned, [])
        self.assertEqual(len(rest), 3)


if __name__ == "__main__":
    unittest.main()
