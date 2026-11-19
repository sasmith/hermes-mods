"""find_owning_chat in scripts/notify-pr-chat.py: strict PR matching, notifications ignored."""
import importlib.util
import sqlite3
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "notify-pr-chat.py"


def load(tmp_path, rows):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, last_activity_at REAL)")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, content TEXT, timestamp REAL)")
    sessions = {}
    for sid, title, active, ts, text in rows:
        sessions[sid] = (sid, "desktop", title, active)
        conn.execute("INSERT INTO messages (session_id, content, timestamp) VALUES (?, ?, ?)", (sid, text, ts))
    conn.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?)", sessions.values())
    conn.commit()
    conn.close()
    spec = importlib.util.spec_from_file_location("notify_pr_chat", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    setattr(mod, "STATE_DB", db)
    return mod


PR, BRANCH = "acme/widget#119", "me/basic-checkpoints"


def test_bare_number_in_other_chat_does_not_match(tmp_path):
    mod = load(tmp_path, [
        ("owner", "Saving", 1, 1, f"Opened {PR} on branch {BRANCH}"),
        ("other", "Other", 9, 2, "applied local patch #119570 and issue #119"),
    ])
    assert mod.find_owning_chat(PR, BRANCH) == "owner"


def test_received_notifications_do_not_make_an_owner(tmp_path):
    mod = load(tmp_path, [
        ("owner", "Saving", 1, 1, f"Working on {PR}"),
        ("other", "Other", 9, 2, f"[kanban] Task t_x blocked. PR attention needed on {PR} ({BRANCH})"),
        ("other", "Other", 9, 3, f"PR attention needed on {PR} ({BRANCH})"),
    ])
    assert mod.find_owning_chat(PR, BRANCH) == "owner"


def test_most_discussion_wins_over_recency(tmp_path):
    mod = load(tmp_path, [
        ("owner", "Saving", 1, 1, f"{PR} first"),
        ("owner", "Saving", 1, 2, f"pushed to {BRANCH}"),
        ("recent", "Recent", 9, 5, f"see https://github.com/acme/widget/pull/119"),
    ])
    assert mod.find_owning_chat(PR, BRANCH) == "owner"


def test_longer_pr_number_does_not_match(tmp_path):
    mod = load(tmp_path, [
        ("a", "A", 1, 1, "acme/widget#1190 is unrelated"),
        ("b", "B", 2, 2, f"{PR} is ours"),
    ])
    assert mod.find_owning_chat(PR, "") == "b"
