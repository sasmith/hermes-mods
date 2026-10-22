"""Synthetic, offline Engine D fixtures; no private records or source configs."""
import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import coach_weekly
import coach_yield as y


class YieldFixtures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.sources = self.data / "scripts"
        self.sources.mkdir()
        self.put("scripts/coach_scout.py", 'X_HANDLE_GROUPS = [["oldaccount", "freshaccount", "productive"]]\nraise RuntimeError("never import")\n')
        self.put("scripts/coach_research.py", 'FEEDS = [("Old feed", "https://old.example/rss", None), ("Fresh feed", "https://fresh.example/rss", None)]\nraise RuntimeError("never import")\n')
        for name in ("ideas", "proposals"):
            self.jsonl("reports/" + name + ".jsonl", [])
        self.jsonl("research/state/spend.jsonl", [])
        self.put("research/state/seen.json", "{}")
        self.now = y.timestamp("2026-06-15T12:00:00Z")

    def put(self, path, content):
        p = self.data / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def jsonl(self, path, rows):
        self.put(path, "".join(json.dumps(r) + "\n" for r in rows))

    def aggregate(self):
        return y.aggregate(self.data, self.now, self.sources, "~/code/hermes/hosts/example/coach")

    def idea(self, **extra):
        return {"id": "idea-a", "date": "2026-06-12", "idea": "A synthetic technique", "source": "https://x.com/productive/status/101", "status": "no_gap", **extra}

    def test_research_seen_dedup_primary_urls_and_observation_dates(self):
        self.put("research/2026-06-10.md", "## X (Grok x_search)\n### Synthetic (2025-01-01) [x/@productive]\nhttps://twitter.com/Productive/status/101?s=20\n\nEvidence https://x.com/unrelated/status/202\n")
        self.put("research/2026-06-12-daily.md", "## X (Grok x_search)\n### Synthetic (2026-06-12) [x/@productive]\nhttps://x.com/productive/status/101\n")
        self.put("research/state/seen.json", json.dumps({"https://x.com/productive/status/101": "2026-06-10"}))
        r = self.aggregate()
        self.assertEqual(r["counts"][("handle", "productive")]["scouted"], 1)
        self.assertNotIn(("handle", "unrelated"), r["counts"])

    def test_seen_preserves_hn_section_attribution(self):
        self.put("research/2026-06-12.md", "## Hacker News\n### Synthetic (2026-06-11) [hn]\nhttps://article.example/a\n")
        self.put("research/state/seen.json", '{"https://article.example/a": "2026-06-12"}')
        self.assertEqual(self.aggregate()["counts"][("source_kind", "hn")]["scouted"], 1)

    def test_legacy_url_inference_and_multiple_source_credit(self):
        self.jsonl("reports/ideas.jsonl", [self.idea(source="https://old.example/post ; https://x.com/productive/status/101"), self.idea(id="idea-b", source="https://github.com/example/tool/releases/tag/v1")])
        counts = self.aggregate()["counts"]
        for key in (("feed", "Old feed"), ("handle", "productive"), ("source_kind", "release")):
            self.assertEqual(counts[key]["ideas"], 1)

    def test_explicit_id_trial_and_rollout_join_dedup(self):
        self.jsonl("reports/ideas.jsonl", [self.idea(), self.idea()])
        self.jsonl("reports/proposals.jsonl", [
            {"id": "rollout", "date": "2026-06-14", "status": "applied", "trial_id": "trial"},
            {"id": "trial", "date": "2026-06-12", "status": "pending", "kind": "trial", "idea_id": "idea-a"},
            {"id": "trial", "at": "2026-06-13T01:00:00Z", "status": "carded"}])
        c = self.aggregate()["counts"][("handle", "productive")]
        self.assertEqual([c[k] for k in y.STAGES], [0, 1, 1, 1, 1, 1])

    def test_legacy_numbered_reference_not_line_number(self):
        idea = self.idea()
        idea.pop("id")
        self.jsonl("reports/ideas.jsonl", [self.idea(id="other", idea="Other", source="https://old.example/other"), idea])
        self.put("reports/ideas.md", "## 2026-06-12\n### Proposed\n7. **A synthetic technique** — https://x.com/productive/status/101\n")
        self.jsonl("reports/proposals.jsonl", [{"id": "p", "date": "2026-06-14", "status": "applied", "change": {"idea": "reports/ideas.md 2026-06-12 #7"}}])
        r = self.aggregate()
        self.assertEqual(r["counts"][("handle", "productive")]["kept"], 1)
        self.assertEqual(r["counts"][("feed", "Old feed")]["kept"], 0)

    def test_ambiguous_shared_url_and_finding_ids_do_not_link(self):
        self.jsonl("reports/ideas.jsonl", [self.idea(), self.idea(id="idea-b", idea="Different")])
        self.jsonl("reports/proposals.jsonl", [{"id": "p", "date": "2026-06-14", "status": "applied", "evidence": "https://x.com/productive/status/101 finding 2026-06-12#1"}])
        r = self.aggregate()
        self.assertEqual(r["unlinked"], 1)
        self.assertEqual(r["counts"][("handle", "productive")]["kept"], 0)
        self.assertTrue(any("Ambiguous" in n for n in r["notes"]))

    def test_unique_url_joins_and_reverted_is_not_kept(self):
        self.jsonl("reports/ideas.jsonl", [self.idea()])
        self.jsonl("reports/proposals.jsonl", [
            {"id": "p", "date": "2026-06-13", "status": "applied", "change": {"idea": "https://twitter.com/productive/status/101"}},
            {"id": "p", "at": "2026-06-14T00:00:00Z", "status": "reverted"}])
        c = self.aggregate()["counts"][("handle", "productive")]
        self.assertEqual(c["applied"], 1)
        self.assertEqual(c["kept"], 0)

    def test_window_boundaries_future_records_excluded(self):
        self.jsonl("reports/ideas.jsonl", [self.idea(date="2026-04-01"), self.idea(id="future", date="2026-06-16"), self.idea(id="current")])
        self.jsonl("reports/proposals.jsonl", [{"id": "p", "at": "2026-06-16T00:00:00Z", "status": "applied", "idea_id": "current"}])
        c = self.aggregate()["counts"][("handle", "productive")]
        self.assertEqual(c["ideas"], 1)
        self.assertEqual(c["kept"], 0)

    def mature(self):
        for date in ("2026-04-01", "2026-06-14"):
            self.put(f"research/{date}.md", f"## Old feed\n_nothing new this window_\n## X (Grok x_search)\n### Synthetic ({date}) [x/@oldaccount]\nhttps://x.com/oldaccount/status/{date.replace('-', '')}\n")
        self.put("research/2026-06-13.md", "## Fresh feed\n_nothing new this window_\n## X (Grok x_search)\n### Synthetic (2020-01-01) [x/@freshaccount]\nhttps://x.com/freshaccount/status/1\n")

    def test_maturation_and_proposal_not_kept_pruning_threshold(self):
        self.mature()
        r = self.aggregate()
        self.assertEqual(r["drops"], [("feed", "Old feed"), ("handle", "oldaccount")])
        self.jsonl("reports/ideas.jsonl", [self.idea(source="https://x.com/oldaccount/status/7", status="proposed")])
        self.assertNotIn(("handle", "oldaccount"), self.aggregate()["drops"])

    def test_literal_blocks_target_correct_file_and_preserve_other_code(self):
        self.mature()
        blocks = y.source_proposals(self.aggregate())
        self.assertEqual(len(blocks), 2)
        for b in blocks:
            path = self.sources / Path(b["target"]).name
            before = path.read_text()
            self.assertEqual(before.count(b["change"]["old_string"]), 1)
            after = before.replace(b["change"]["old_string"], b["change"]["new_string"])
            ast.parse(after)
            self.assertIn('raise RuntimeError("never import")', after)
            self.assertEqual((b["kind"], b["tier"]), ("plugin_rule", "auto"))
            if "FEEDS" in b["change"]["old_string"]:
                self.assertTrue(b["target"].endswith("coach_research.py"))

    def test_unknown_links_or_missing_ledger_withhold_removals(self):
        self.mature()
        (self.data / "reports/ideas.jsonl").unlink()
        self.assertEqual(y.source_proposals(self.aggregate()), [])
        self.jsonl("reports/ideas.jsonl", [])
        self.jsonl("reports/proposals.jsonl", [{"id": "unknown", "date": "2026-06-14", "status": "applied"}])
        self.assertEqual(y.source_proposals(self.aggregate()), [])

    def test_quote_reply_candidates_require_kept_evidence(self):
        self.jsonl("reports/ideas.jsonl", [self.idea(status="kept", quoted_handles=["@candidate"], evidence="replied to @second; unrelated @ignore"), self.idea(id="other", status="proposed", quoted_handles=["@notkept"])])
        r = self.aggregate()
        self.assertEqual(set(r["additions"]), {"candidate", "second"})
        block = y.source_proposals(r)[0]
        self.assertIn("idea-a", block["evidence"])
        self.assertIn("candidate", block["change"]["new_string"])

    def test_nonliteral_config_not_executed(self):
        self.put("scripts/coach_scout.py", "X_HANDLE_GROUPS = dangerous()\n")
        r = self.aggregate()
        self.assertNotIn("X_HANDLE_GROUPS", r["config"])
        self.assertTrue(any("nonliteral" in n for n in r["notes"]))

    def test_spend_seven_buckets_lane_and_total_flags_backfill(self):
        records = [{"date": "2026-06-14", "at": "2026-06-15T00:00:00Z", "lane": lane, "usd": value} for lane, value in (("scout", 4), ("fixer", 8), ("trial", 11))]
        buckets, total, flags = y.spend_buckets(records, self.now)
        self.assertEqual(len(buckets), 7)
        self.assertEqual(total, 23)
        self.assertEqual(buckets["2026-06-14"]["scout"], 4)
        self.assertIsNone(buckets["2026-06-15"]["scout"])
        self.assertEqual(len(flags), 4)

    def test_missing_spend_not_zero_invalid_cost_and_weekly_cap(self):
        buckets, total, flags = y.spend_buckets([], self.now)
        self.assertTrue(all(v is None for lanes in buckets.values() for v in lanes.values()))
        records = [{"date": "2026-06-14", "lane": "trial", "usd": 141}, {"date": "2026-06-14", "lane": "scout", "usd": float("nan")}]
        _, total, flags = y.spend_buckets(records, self.now)
        self.assertEqual(total, 141)
        self.assertTrue(any("$140" in f for f in flags))
        self.assertTrue(any("invalid" in f for f in flags))

    def test_eval_settle_rows_net_against_reservations(self):
        records = [{"date": "2026-06-14", "lane": "trial", "usd": 0.1, "kind": "reserve"},
                   {"date": "2026-06-14", "lane": "trial", "usd": -0.07, "kind": "settle"},
                   {"date": "2026-06-14", "lane": "trial", "usd": -1.0}]
        buckets, total, flags = y.spend_buckets(records, self.now)
        self.assertAlmostEqual(buckets["2026-06-14"]["trial"], 0.03)
        self.assertEqual(sum("invalid" in f for f in flags), 1)  # a bare negative row is still invalid

    def test_pt_report_and_dst_budget_boundary(self):
        now = y.timestamp("2026-11-03T01:00:00Z")
        text = "\n".join(y.section_yield(self.data, now, self.sources))
        self.assertIn("2026-11-02 17:00 PST", text)
        self.assertIn("10-31 17:00 PDT → 11-01 16:00 PST", text)
        self.assertIn("unknown", text)
        self.assertIn("$140/week", text)

    def test_source_rule_tier_and_rejected_executable_edits(self):
        import coach_proposals as cp
        self.mature()
        for block in y.source_proposals(self.aggregate()):
            self.assertEqual(cp.effective_tier(block), "auto")
            hostile = json.loads(json.dumps(block))
            hostile["change"]["new_string"] += "\nimport os"
            self.assertEqual(cp.effective_tier(hostile), "canary")
            block["risk"] = "high"
            self.assertEqual(cp.effective_tier(block), "review")

    def test_quoted_account_in_research_body_and_explicit_kind(self):
        self.put("research/2026-06-12.md", "## X\n### Synthetic (2026-06-12) [x/@productive]\nhttps://x.com/productive/status/101\n\nEvidence: quoted @bodycandidate.\n")
        self.jsonl("reports/ideas.jsonl", [self.idea(status="kept"), self.idea(id="hn", source="https://github.com/example/repo", source_kind="hn")])
        result = self.aggregate()
        self.assertIn("bodycandidate", result["additions"])
        self.assertEqual(result["counts"][("source_kind", "hn")]["ideas"], 1)

    def test_registration_preserves_linkage_ids(self):
        import argparse
        import coach_proposals as cp
        block = {"kind": "curriculum", "target": "example.yaml", "change": {"append": "tip"},
                 "evidence": "fixture", "idea_id": "idea-a", "trial_id": "trial-a", "source_url": "https://example.org/a"}
        self.put("reports/weekly-2026-06-12.md", "```coach-proposal\n" + json.dumps(block) + "\n```\n")
        cp.cmd_add(argparse.Namespace(data=self.data, report="reports/weekly-2026-06-12.md"))
        rec = next(iter(cp.state(self.data).values()))
        self.assertEqual(rec["idea_id"], "idea-a")
        self.assertEqual(rec["trial_id"], "trial-a")

    def test_weekly_main_keeps_existing_sections_and_appends_engine_d(self):
        with patch.object(coach_weekly.sys, "argv", ["coach_weekly.py", "--data", str(self.data), "--sources-dir", str(self.sources)]), patch.object(coach_weekly.coach_mine, "load", return_value=[]), patch.object(coach_weekly, "section_kpis", return_value=["KPI unchanged"]), patch.object(coach_weekly, "section_findings", return_value=[]), patch.object(coach_weekly, "section_proposals", return_value=["B1 unchanged"]), patch("builtins.print") as output:
            coach_weekly.main()
        text = output.call_args.args[0]
        self.assertIn("KPI unchanged", text)
        self.assertIn("B1 unchanged", text)
        self.assertIn("Engine D", text)


if __name__ == "__main__":
    unittest.main()


class SpendSection(unittest.TestCase):
    NOW = 1_800_000_000.0

    def s(self, sid, cost, reply, ago_days=1, host="mac", profile="default", source="cron", title="job-a · Oct 1"):
        return {"id": sid, "host": host, "profile": profile, "source": source, "title": title, "cost_usd": cost,
                "last_reply": reply, "last_activity_at": self.NOW - ago_days * 86400, "signals": {}}

    def test_flags_mostly_noop_cron_and_duplicates_and_spikes(self):
        ds = [self.s(f"cron_aaa111_{i}", 1.0, "[SILENT]") for i in range(6)]
        ds += [self.s("cron_aaa111_x", 1.0, "Fixed the disk alert")]
        ds += [self.s("cron_bbb222_1", 0.5, "team self-managing, nothing to fix", host="mini")]
        ds += [self.s("cron_ccc333_1", 0.2, "nothing to report", title="job-c · Oct 1"),
               self.s("k1", 20.0, "Shipped PR #4", source="kanban"),
               self.s("k0", 6.0, "Shipped PR #3", source="kanban", ago_days=9),
               self.s("d1", 3.0, "Here is the plan", source="desktop")]
        out = "\n".join(coach_weekly.section_spend(ds, self.NOW, 7))
        row = next(l for l in out.splitlines() if "| mac/default | cron:job-a (aaa111)" in l)
        self.assertIn("| 7 | 7.00 |", row)
        self.assertIn("86%", row)
        self.assertIn("mostly no-op: ~$6 spent on nothing", row)
        self.assertIn("same cron on mac/default, mini/default", row)
        krow = next(l for l in out.splitlines() if "| kanban |" in l)
        self.assertIn("spend x3.3 vs prev", krow)
        self.assertIn("| 20.00 | 6.00 |", krow)
        self.assertIn("| mac/default | chats | 1 | 3.00 |", out)
        ccc = next(l for l in out.splitlines() if "job-c" in l)
        self.assertTrue(ccc.rstrip().endswith("|  |"))  # cheap no-op job: under min_cost, not flagged

    def test_empty_window(self):
        self.assertIn("No sessions in this window.", coach_weekly.section_spend([], self.NOW, 7))

    def test_top_cut_keeps_flagged_rows(self):
        ds = [self.s(f"k{i}", 100.0 - i, "done", source=f"src{i}") for i in range(5)]
        ds += [self.s(f"cron_zzz999_{i}", 2.0, "[SILENT]", title="idle") for i in range(3)]
        out = "\n".join(coach_weekly.section_spend(ds, self.NOW, 7, top=2))
        self.assertIn("cron:idle (zzz999)", out)
        self.assertIn("3 more agents", out)


class SelfHealLane(unittest.TestCase):
    """Self-heal lane section: synthetic events + a synthetic Kanban board (no real cards or quotes)."""
    NOW = 1_800_000_000.0
    DAY = 86400

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.data = Path(self.tmp.name) / "data"
        (self.home / "coach").mkdir(parents=True)
        self.data.mkdir()

    def events(self, rows):
        with (self.home / "coach" / "log.jsonl").open("a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def board(self, rows):
        import sqlite3
        c = sqlite3.connect(self.home / "kanban.db")
        c.execute("create table tasks (id text primary key, title text, status text, created_at int, completed_at int, "
                  "result text, idempotency_key text)")
        c.executemany("insert into tasks values (?,?,?,?,?,?,?)", rows)
        c.commit()
        c.close()

    def section(self):
        with patch("engine_budget.card_cost", lambda home, board, tid: {"t_inv1": 2.0, "t_fix1": 3.0}.get(tid, 1.0)):
            return "\n".join(coach_weekly.section_selfheal(self.data, self.NOW, 7, homes=[self.home]))

    def test_no_activity(self):
        out = self.section()
        self.assertIn("No self-heal activity yet", out)
        self.assertNotIn("Traceback", out)

    def test_no_home_at_all(self):
        out = "\n".join(coach_weekly.section_selfheal(self.data, self.NOW, 7, homes=[self.home / "missing"]))
        self.assertIn("No self-heal activity yet", out)

    def test_stages_recurrence_cost(self):
        t = self.NOW - 2 * self.DAY
        self.events([
            {"ts": t, "event": "selfheal_would_file", "fingerprint": "shadow-a"},
            {"ts": t, "event": "selfheal_filed", "fingerprint": "ui-aaaaaaaaaa", "card": "t_inv1"},
            {"ts": t + 60, "event": "selfheal_proposed", "fingerprint": "ui-aaaaaaaaaa"},
            {"ts": t + 120, "event": "selfheal_approved", "fingerprint": "ui-aaaaaaaaaa"},
            {"ts": t + 600, "event": "selfheal_fixed", "fingerprint": "ui-aaaaaaaaaa"},
            {"ts": t, "event": "selfheal_filed", "fingerprint": "fp-b"},
            {"ts": t + 60, "event": "selfheal_not_defect", "fingerprint": "fp-b"},
            {"ts": t, "event": "selfheal_filed", "fingerprint": "fp-old", "recurrence": True},
            {"ts": t, "event": "unrelated", "fingerprint": "x"},
            {"ts": self.NOW - 10 * self.DAY, "event": "selfheal_filed", "fingerprint": "fp-prev"},
        ])
        self.board([("t_inv1", "inv", "done", int(t), int(t + 60), "proposal posted", "selfheal:ui-aaaaaaaaaa"),
                    ("t_fix1", "fix", "done", int(t + 120), int(t + 600), "fixed", "selfheal:ui-aaaaaaaaaa:fix"),
                    ("t_other", "x", "done", int(t), int(t), "", "coach-fix:zzz"),
                    ("t_build", "build", "done", int(t), int(t), "", "selfheal:1-detector")])
        out = self.section()
        self.assertIn("| Filed (investigation card) | 3 | 1 |", out)
        self.assertIn("| Proposed (Inbox, What happened / Why / Plan) | 1 | 0 |", out)
        self.assertIn("| Approved (Do it) | 1 | 0 |", out)
        self.assertIn("| Fixed (verified live) | 1 | 0 |", out)
        self.assertIn("| Closed as not-a-defect (false positive) | 1 | 0 |", out)
        self.assertIn("| Shadow would-file (not created) | 1 | 0 |", out)
        self.assertIn("Recurrence: 1 of 3", out)
        self.assertIn("Spend: $5.00", out)   # the two defect cards, not the coach-fix or build card
        self.assertIn("$ per fix: $5.00", out)

    def test_recurrence_from_cards(self):
        t = self.NOW - 3 * self.DAY
        self.board([("t_f0", "fix", "done", int(t - 20 * self.DAY), int(t - 19 * self.DAY), "", "selfheal-fix:cron-0123456789"),
                    ("t_i1", "inv", "running", int(t), None, "", "selfheal:cron-0123456789")])
        out = self.section()
        self.assertIn("Recurrence: 1 of 1", out)
        self.assertIn("n/a (no fixes this window)", out)

    def test_snapshot_roundtrip(self):
        t = self.NOW - self.DAY
        self.events([{"ts": t, "event": "selfheal_filed", "fingerprint": "fp-s", "quote": "private text"}])
        snap = self.data / "reports" / "selfheal-lane.jsonl"
        with patch("engine_budget.card_cost", lambda *a: 0.0):
            self.assertEqual(coach_weekly.selfheal_export(snap, [self.home]), 1)
        self.assertNotIn("private text", snap.read_text())  # only content-free fields are exported
        out = "\n".join(coach_weekly.section_selfheal(self.data, self.NOW, 7, homes=[self.home / "missing"]))
        self.assertIn("| Filed (investigation card) | 1 | 0 |", out)

    def test_fingerprint_of_keys(self):
        self.assertEqual(coach_weekly.selfheal_fp("selfheal:abc"), "abc")
        self.assertEqual(coach_weekly.selfheal_fp("selfheal:abc:fix"), "abc")
        self.assertEqual(coach_weekly.selfheal_fp("selfheal-fix:abc"), "abc")
