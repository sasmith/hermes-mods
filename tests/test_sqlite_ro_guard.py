"""Guard: no bare SQLite ``mode=ro`` open in plugin/skill code.

Root cause: SQLite cannot open a WAL database with ``mode=ro`` while its ``-wal`` / ``-shm`` file is absent.
Hermes writers (state.db, kanban.db) delete those files on each last-close checkpoint, so a ``mode=ro`` reader
fails at random with ``sqlite3.OperationalError: unable to open database file``. ``mode=rw`` + ``PRAGMA
query_only=ON`` opens fine, reads the same data and never creates a missing db.

Run: ``python -m pytest tests/test_sqlite_ro_guard.py`` or ``python tests/test_sqlite_ro_guard.py``.
"""
import importlib.util
import re
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Spelled in two halves so this file does not trip its own scan.
NEEDLE = "mode=" + "ro"
URI_FLAG = re.compile(r"[?&]" + NEEDLE + r"\b")  # a URI flag, not prose that mentions the mode
FIX = "query_" + "only"
EXCLUDE_PREFIX = ("core/patches/",)


def _tracked(*suffixes):
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [f for f in out.splitlines() if f.endswith(suffixes) and not f.startswith(EXCLUDE_PREFIX)]


def offenders():
    """Files with a non-comment line holding the ro URI flag but no fallback (``query_only``) anywhere in the file."""
    bad = []
    for rel in _tracked(".py", ".sh"):
        p = ROOT / rel
        if not p.is_file():
            continue
        text = p.read_text(errors="replace")
        hits = [(n, ln.strip()) for n, ln in enumerate(text.splitlines(), 1)
                if URI_FLAG.search(ln) and not ln.lstrip().startswith("#")]
        if hits and FIX not in text:
            bad.append(f"{rel}:{hits[0][0]}: {hits[0][1][:100]}" + (f"  (+{len(hits) - 1} more)" if len(hits) > 1 else ""))
    return bad


def _wal_db_without_sidecars(tmp):
    """A WAL db whose -wal/-shm files are gone, as left by a writer's last-close checkpoint."""
    db = Path(tmp) / "t.db"
    c = sqlite3.connect(db)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("create table t(x)")
    c.execute("insert into t values (42)")
    c.commit()
    c.close()  # last close checkpoints and normally deletes the sidecars
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    return db


def _load(rel, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SqliteRoGuard(unittest.TestCase):
    def test_no_bare_ro_open(self):
        bad = offenders()
        self.assertEqual(bad, [], "bare ?" + NEEDLE + " opens without a query_only fallback "
                         "(use a _ro_connect helper: ro first, rw + PRAGMA query_only on OperationalError):\n  "
                         + "\n  ".join(bad))

    def test_plain_ro_fails_without_wal_sidecars_but_helper_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _wal_db_without_sidecars(tmp)
            try:
                c = sqlite3.connect(f"file:{db}?{NEEDLE}", uri=True)
                c.execute("select 1 from sqlite_master limit 1")
                c.close()
            except sqlite3.OperationalError:
                pass
            else:
                self.skipTest("this SQLite build opens a WAL db read-only without -wal/-shm")
            for rel in ("plugins/open-asks/core.py", "plugins/user-steps/cards.py", "plugins/follow-through/core.py"):
                helper = _load(rel, "_ro_guard_" + rel.split("/")[1].replace("-", "_")).__dict__.get("_ro_connect")
                self.assertIsNotNone(helper, f"{rel} has no _ro_connect")
                c = helper(db, timeout=1)
                try:
                    self.assertEqual(c.execute("select x from t").fetchall(), [(42,)])
                    with self.assertRaises(sqlite3.OperationalError):  # still read-only
                        c.execute("insert into t values (1)")
                finally:
                    c.close()

    def test_helper_never_creates_a_missing_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.db"
            helper = _load("plugins/open-asks/core.py", "_ro_guard_missing")._ro_connect
            with self.assertRaises(sqlite3.OperationalError):
                helper(missing, timeout=1)
            self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
