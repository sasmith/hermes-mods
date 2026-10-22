import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
FIX = json.loads((Path(__file__).parent / "fixtures" / "session_20261021_103941.json").read_text())

SHEET = ("Things the agent can do itself (generated from local config just now):\n"
         "- token-broker scopes: cf-token-minter (auto, can mint narrower Cloudflare tokens), cf-infra (auto)\n"
         "- ssh hosts: mini-host\n- Chrome profiles signed in: user@example.com\n- browser vault entries: none")


def _load():
    spec = importlib.util.spec_from_file_location("ask_gate_plugin_t", HERE / "__init__.py",
                                                  submodule_search_locations=[str(HERE)])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def gate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("ASK_GATE_LEDGER", str(tmp_path / "state" / "ask-ledger.sqlite"))
    for k in ("HERMES_KANBAN_TASK", "ASK_GATE_OFF"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ASK_GATE_NO_PREWARM", "1")
    mod = _load()
    monkeypatch.setattr(mod.core, "capability_sheet", lambda *a, **k: SHEET)

    def boom(system, user):
        raise AssertionError("the model must not be called here")
    mod.LLM = boom
    mod._JUDGED.clear()
    mod.LOG.reset("s1")
    return mod


@pytest.fixture
def board(tmp_path):
    db = tmp_path / "kanban.db"
    c = sqlite3.connect(db)
    c.execute("create table tasks (id text, title text, status text, assignee text, created_at real, "
              "completed_at real, result text, session_id text)")
    c.commit()
    c.close()

    def add(id, status, title="card", session="s1", created=1000.0, completed=None, result=""):
        c = sqlite3.connect(db)
        c.execute("insert into tasks values (?,?,?,?,?,?,?,?)", (id, title, status, "default", created, completed, result, session))
        c.commit()
        c.close()
    return add


def llm_returning(verdicts):
    """A stub model returning the given [(verdict, reason, how)] in order; records the prompts it saw."""
    seen = []

    def fn(system, user):
        seen.append((system, user))
        return json.dumps({"asks": [{"i": i, "verdict": v[0], "reason": v[1], "how": v[2] if len(v) > 2 else ""}
                                    for i, v in enumerate(verdicts)]})
    fn.seen = seen
    return fn
