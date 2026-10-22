"""Tests for coach_feed.py conflict handling. Run: python3 -B -m unittest test_coach_feed (from scripts/)."""
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import coach_feed


def g(repo, *a):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *a],
                          capture_output=True, text=True, check=True).stdout


class ConflictTest(unittest.TestCase):
    def make_conflict(self, name="research/state/spend.jsonl"):
        d = Path(tempfile.mkdtemp())
        repo = d / "repo"
        repo.mkdir()
        g(repo, "init", "-q", "-b", "main")
        f = repo / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text('{"a":1}\n')
        g(repo, "add", "-A"); g(repo, "commit", "-qm", "base")
        g(repo, "checkout", "-qb", "other")
        f.write_text('{"a":1}\n{"b":2}\n{"c":3}\n')
        g(repo, "commit", "-qam", "other")
        g(repo, "checkout", "-q", "main")
        f.write_text('{"a":1}\n{"c":3}\n{"d":4}\n')
        g(repo, "commit", "-qam", "main")
        subprocess.run(["git", "-C", str(repo), "merge", "other"], capture_output=True)
        return repo, f

    def test_jsonl_union_dedupe(self):
        repo, f = self.make_conflict()
        self.assertEqual(coach_feed.unmerged_paths(repo), ["research/state/spend.jsonl"])
        resolved, unresolved = coach_feed.resolve_unmerged(repo)
        self.assertEqual((resolved, unresolved), (["research/state/spend.jsonl"], []))
        self.assertEqual(coach_feed.unmerged_paths(repo), [])
        self.assertEqual(sorted(f.read_text().splitlines()), sorted(['{"a":1}', '{"b":2}', '{"c":3}', '{"d":4}']))

    def test_non_jsonl_is_unresolved(self):
        repo, _ = self.make_conflict("notes.txt")
        resolved, unresolved = coach_feed.resolve_unmerged(repo)
        self.assertEqual((resolved, unresolved), ([], ["notes.txt"]))

    def run_feed(self, repo):
        home = repo.parent / "home"
        home.mkdir(exist_ok=True)
        return subprocess.run([sys.executable, str(HERE / "coach_feed.py"), "--host", "h", "--repo-dir", str(repo),
                               "--home", str(home), "--no-push"], capture_output=True, text=True)

    def test_main_survives_conflict(self):
        repo, f = self.make_conflict()
        r = self.run_feed(repo)
        self.assertEqual(r.returncode, 0, r.stderr)
        st = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(st["resolved_conflicts"], ["research/state/spend.jsonl"])
        self.assertIn("auto-resolved", r.stderr)

    def test_main_skips_unresolvable(self):
        repo, _ = self.make_conflict("notes.txt")
        r = self.run_feed(repo)
        self.assertEqual(r.returncode, 0, r.stderr)
        st = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertIn("notes.txt", st["skipped"])
        self.assertIn("ERROR", r.stderr)


class ForwardSignalsTest(unittest.TestCase):
    def test_runs_the_forwarder_with_host_and_home(self):
        seen = []

        def runner(cmd, **kw):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "signals: forwarded 3 to you@hub.example.com", "")
        out = coach_feed.forward_signals("macbook", "/tmp/h", runner=runner)
        self.assertIn("forwarded 3", out)
        cmd = seen[0]
        self.assertTrue(cmd[1].endswith("plugins/coach/signals.py"))
        self.assertEqual(cmd[2:], ["--host", "macbook", "--home", "/tmp/h", "forward"])

    def test_failure_is_reported_not_raised(self):
        def boom(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 150)
        self.assertEqual(coach_feed.forward_signals("mini", None, runner=boom), "error: TimeoutExpired")
        bad = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "ssh failed")
        self.assertEqual(coach_feed.forward_signals("mini", None, runner=bad), "error: ssh failed")

    def test_no_hub_configured_says_so_and_ships_nothing(self):
        h = Path(tempfile.mkdtemp())
        out = coach_feed.forward_signals("macbook", str(h))
        self.assertIn("no hub configured", out)
        self.assertEqual(coach_feed.signals_state(out), "no-hub")
        self.assertFalse((h / "coach" / "signals-cursor.json").exists())


class FeedStatusTest(unittest.TestCase):
    """<host>/feed-status.json: the hub's only view of a Mac forwarder that never reached it."""

    def test_states(self):
        s = coach_feed.signals_state
        self.assertEqual(s("signals: heartbeat ok to you@hub.example.com"), "ok")
        self.assertEqual(s("signals: forwarded 4 to you@hub.example.com"), "ok")
        self.assertEqual(s("signals: this host is the hub (its own cron sweeps locally)"), "hub")
        self.assertEqual(s("error: signals forward to you@hub.example.com failed (2 pending): Permission denied"),
                         "error")
        self.assertEqual(s("ok"), "unknown")

    def test_written_only_when_health_changes(self):
        repo = Path(tempfile.mkdtemp())
        w = coach_feed.write_feed_status
        self.assertTrue(w(repo, "mini", "signals: no hub configured (x)", now="t1"))
        self.assertFalse(w(repo, "mini", "signals: no hub configured (x)", now="t2"))
        self.assertTrue(w(repo, "mini", "signals: forwarded 3 to you@hub.example.com", now="t3"))
        self.assertFalse(w(repo, "mini", "signals: heartbeat ok to you@hub.example.com", now="t4"))
        self.assertFalse(w(repo, "mini", "signals: forwarded 9 to you@hub.example.com", now="t5"))
        err = "error: signals forward to you@hub.example.com failed ({} pending): Permission denied"
        self.assertTrue(w(repo, "mini", err.format(2), now="t6"))
        self.assertFalse(w(repo, "mini", err.format(5), now="t7"))  # the pending count alone is not a change
        st = json.loads((repo / "mini" / "feed-status.json").read_text())
        self.assertEqual(st["signals"], "error")
        self.assertEqual(st["since"], "t6")
        self.assertIn("(pending)", st["detail"])

    def test_main_commits_the_status(self):
        d = Path(tempfile.mkdtemp())
        repo, home = d / "repo", d / "home"
        repo.mkdir(); home.mkdir()
        g(repo, "init", "-q", "-b", "main")
        (repo / "README.md").write_text("x\n")
        g(repo, "add", "-A"); g(repo, "commit", "-qm", "base")
        (repo / "h").mkdir()
        r = subprocess.run([sys.executable, str(HERE / "coach_feed.py"), "--host", "h", "--repo-dir", str(repo),
                            "--home", str(home), "--no-push"], capture_output=True, text=True,
                           env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        self.assertEqual(r.returncode, 0, r.stderr)
        st = json.loads(g(repo, "show", "HEAD:h/feed-status.json"))
        self.assertEqual(st["signals"], "no-hub")


if __name__ == "__main__":
    unittest.main()
