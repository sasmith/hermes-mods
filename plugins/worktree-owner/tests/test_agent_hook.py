"""agent_hook.py: the same owner marker for Claude Code / Codex sessions, and session-end removal of merged trees.

Run: python3 -B -m unittest discover -s plugins/worktree-owner/tests   (stdlib only; temp git repos)
"""
import importlib.util, json, os, subprocess, tempfile, time, unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "agent_hook.py"
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True, env=ENV).stdout


def load():
    spec = importlib.util.spec_from_file_location("worktree_owner_agent_hook_t", SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class AgentHook(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.h = load()
        self.b = self.h.base
        self.code = self.tmp / "code"; self.code.mkdir()
        self.b.REPO_GLOBS = [str(self.code / "*" / ".git"), str(self.code / "*" / "*" / ".git")]
        self.b.LOG = self.tmp / "owner.log"
        self.b.live_cwds = lambda: []
        self.h.STATE = self.tmp / "state"
        self.prs = {}  # branch -> merged PR head oid
        self.h.merged_pr_heads = lambda path, branch: [self.prs[branch]] if branch in self.prs else []
        self.bare = self.tmp / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.bare)], check=True)
        self.repo = self.code / "proj"
        git(self.tmp, "clone", "-q", str(self.bare), str(self.repo))
        (self.repo / "a.txt").write_text("a\n")
        git(self.repo, "add", "."); git(self.repo, "commit", "-qm", "init"); git(self.repo, "push", "-q", "origin", "HEAD:main")
        git(self.repo, "fetch", "-q")

    def post(self, cmd, sid="sess-1", tool_use_id="tu1", agent="claude", event="PostToolUse"):
        return self.h.handle({"hook_event_name": event, "session_id": sid, "cwd": str(self.repo), "tool_name": "Bash",
                              "tool_input": {"command": cmd}, "tool_use_id": tool_use_id}, agent, env={})

    def make(self, name, branch, sid="sess-1", agent="claude"):
        p = self.code / name
        cmd = f"cd {self.repo} && git worktree add -b {branch} {p} origin/main"
        self.post(cmd, sid, f"tu-{name}", agent, event="PreToolUse")
        git(self.repo, "worktree", "add", "-q", "-b", branch, str(p), "origin/main")
        self.post(cmd, sid, f"tu-{name}", agent)
        return p

    def marker(self, path):
        gd = git(path, "rev-parse", "--absolute-git-dir").strip()
        f = Path(gd) / self.b.MARKER
        return json.loads(f.read_text()) if f.exists() else None

    def commit_push(self, p, fname, branch):
        (p / fname).write_text("x\n"); git(p, "add", fname); git(p, "commit", "-qm", fname)
        git(p, "push", "-q", "origin", f"HEAD:{branch}")

    def merge_to_main(self, branch):
        git(self.repo, "fetch", "-q"); git(self.repo, "merge", "-q", "--ff-only", f"origin/{branch}")
        git(self.repo, "push", "-q", "origin", "HEAD:main")

    def end(self, sid="sess-1", agent="claude", event="SessionEnd"):
        return self.h.handle({"hook_event_name": event, "session_id": sid, "cwd": str(self.repo)}, agent, env={},
                             detach=False)

    # --- registration ---------------------------------------------------------------------------------------------
    def test_post_tool_use_writes_the_hermes_owner_marker_with_the_claude_session(self):
        p = self.make("proj-a", "ba")
        mk = self.marker(p)
        self.assertEqual(mk["agent"], "claude"); self.assertEqual(mk["agent_session_id"], "sess-1")
        self.assertEqual(os.path.realpath(mk["path"]), os.path.realpath(p))

    def test_codex_payload_is_tagged_codex(self):
        self.assertEqual(self.marker(self.make("proj-cx", "bcx", sid="cx-1", agent="codex"))["agent"], "codex")

    def test_kanban_card_env_is_kept_so_the_hermes_teardown_also_applies(self):
        p = self.code / "proj-k"
        git(self.repo, "worktree", "add", "-q", "-b", "bk", str(p), "origin/main")
        self.h.handle({"hook_event_name": "PostToolUse", "session_id": "s", "cwd": str(self.repo), "tool_name": "Bash",
                       "tool_input": {"command": f"git worktree add -b bk {p}"}}, "claude", env={"HERMES_KANBAN_TASK": "t_k"})
        self.assertEqual(self.marker(p)["task_id"], "t_k")

    def test_ignores_other_commands_and_unnamed_concurrent_trees(self):
        p = self.code / "proj-n"
        git(self.repo, "worktree", "add", "-q", "-b", "bn", str(p), "origin/main")
        self.post("ls -la")
        self.post(f"git worktree add -b other {self.code}/proj-zzz origin/main")
        self.assertIsNone(self.marker(p))

    def test_pre_tool_use_stamp_bounds_the_birth_window(self):
        p = self.code / "proj-old"
        git(self.repo, "worktree", "add", "-q", "-b", "bold", str(p), "origin/main")
        time.sleep(1.2)
        self.h.SLACK_S = 0.5
        loop = "for b in x y; do git worktree add -b $b $W/$b; done"
        self.post(loop, tool_use_id="tl", event="PreToolUse")
        self.post(loop, tool_use_id="tl")
        self.assertIsNone(self.marker(p))  # born before this call started: not claimed by a variable loop

    # --- teardown ---------------------------------------------------------------------------------------------------
    def test_session_end_removes_only_merged_clean_trees_of_this_session(self):
        merged = self.make("proj-merged", "bm"); self.commit_push(merged, "m.txt", "bm"); self.merge_to_main("bm")
        pushed = self.make("proj-pushed", "bp"); self.commit_push(pushed, "p.txt", "bp")  # on remote, not merged
        dirty = self.make("proj-dirty", "bd"); self.commit_push(dirty, "d.txt", "bd"); self.merge_to_main("bd")
        (dirty / "wip.txt").write_text("wip\n")
        fresh = self.make("proj-fresh", "bf")  # at origin/main, nothing done: merged, clean -> removable
        other = self.make("proj-other", "bo", sid="sess-2")
        res = self.end()
        acts = {Path(r["path"]).name: r["action"] for r in res}
        self.assertFalse(merged.exists()); self.assertEqual(acts["proj-merged"], "removed")
        self.assertFalse(fresh.exists())
        self.assertTrue(pushed.exists()); self.assertIn("not merged", acts["proj-pushed"])
        self.assertTrue(dirty.exists()); self.assertIn("uncommitted", acts["proj-dirty"])
        self.assertTrue(other.exists()); self.assertNotIn("proj-other", acts)
        self.assertIn("bm", git(self.repo, "branch"))  # branch kept

    def test_squash_merged_pr_whose_head_matches_is_removed_even_after_branch_delete(self):
        p = self.make("proj-sq", "bsq"); self.commit_push(p, "s.txt", "bsq")
        git(self.repo, "push", "-q", "origin", "--delete", "bsq")  # GitHub auto-delete after squash merge
        self.prs["bsq"] = git(p, "rev-parse", "HEAD").strip()
        self.assertEqual(self.end()[0]["action"], "removed"); self.assertFalse(p.exists())

    def test_merged_pr_with_newer_local_commits_is_kept(self):
        p = self.make("proj-sq2", "bsq2"); self.commit_push(p, "s.txt", "bsq2")
        self.prs["bsq2"] = git(p, "rev-parse", "HEAD").strip()
        (p / "more.txt").write_text("more\n"); git(p, "add", "more.txt"); git(p, "commit", "-qm", "more")
        self.assertTrue(self.end()[0]["action"].startswith("kept")); self.assertTrue(p.exists())

    def test_live_process_cwd_keeps_the_tree(self):
        p = self.make("proj-busy", "bb")
        self.b.live_cwds = lambda: [str(p)]
        self.assertIn("live process", self.end()[0]["action"]); self.assertTrue(p.exists())

    def test_stop_is_throttled_but_session_end_is_not(self):
        p = self.make("proj-s", "bs")
        self.b.live_cwds = lambda: [str(p)]
        self.assertEqual(len(self.end(event="Stop")), 1)
        self.assertEqual(self.end(event="Stop"), [])  # within the throttle window
        self.assertEqual(len(self.end(event="SessionEnd")), 1)

    def test_overlapping_stop_and_session_end_do_not_double_handle(self):
        import threading
        p = self.make("proj-race", "brace")
        out = []
        ts = [threading.Thread(target=lambda e=e: out.append(self.end(event=e))) for e in ("Stop", "SessionEnd")]
        [t.start() for t in ts]; [t.join() for t in ts]
        acts = [r["action"] for res in out for r in res]
        self.assertEqual(acts, ["removed"]); self.assertFalse(p.exists())

    def test_main_never_fails_on_garbage(self):
        self.assertEqual(self.h.handle({}, "claude", env={}), [])
        self.assertEqual(self.h.handle({"hook_event_name": "PostToolUse", "tool_input": None}, "claude", env={}), [])


if __name__ == "__main__":
    unittest.main()
