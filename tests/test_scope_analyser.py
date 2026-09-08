"""Characterization tests for repo-agent/agents/planner.py ScopeAnalyser.

ScopeAnalyser is pure regex over source text -- no LLM -- so it is a good
anchor for the repo-agent side of the pipeline.
"""

from __future__ import annotations

from agents.planner import ScopeAnalyser

DELETE_FUNC = [
    "def delete_user(user_id):",
    "    user = User.query.get(user_id)",
    "    db.session.delete(user)",
    "    db.session.commit()",
]


def test_analyse_sqlalchemy_delete():
    scope = ScopeAnalyser().analyse(
        DELETE_FUNC, target_line_0=2, import_lines=["from app import db"]
    )
    assert scope["target_obj"] == "user"
    assert scope["func_params"] == ["user_id"]
    assert scope["local_vars"] == ["user"]
    assert scope["commit_calls"] == ["db.session.commit()"]
    assert scope["db_var"] == "db"
    assert scope["orm_style"] == "sqlalchemy"


def test_analyse_self_delete_form():
    func = [
        "def remove(record):",
        "    record.delete()",
        "    record.save()",
    ]
    scope = ScopeAnalyser().analyse(func, target_line_0=1, import_lines=[])
    assert scope["target_obj"] == "record"


def test_build_prompt_facts_reports_missing_commit():
    func = [
        "def touch(item):",
        "    item.delete()",
    ]
    analyser = ScopeAnalyser()
    scope = analyser.analyse(func, target_line_0=1, import_lines=[])
    facts = analyser.build_prompt_facts(scope)
    assert "COMMIT/SAVE CALLS : none found" in facts
    assert "TARGET OBJECT   : item" in facts


def test_build_prompt_facts_includes_commit_when_found():
    analyser = ScopeAnalyser()
    scope = analyser.analyse(DELETE_FUNC, target_line_0=2, import_lines=[])
    facts = analyser.build_prompt_facts(scope)
    assert "COMMIT/SAVE CALLS : db.session.commit()" in facts
    assert "ORM STYLE         : sqlalchemy" in facts
