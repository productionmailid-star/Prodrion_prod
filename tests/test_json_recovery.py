"""Characterization tests for feedback_platform.extract_json_from_llm and
normalize_taxonomy_nodes."""

from __future__ import annotations

import feedback_platform as fp

# --- extract_json_from_llm -------------------------------------------------


def test_empty_input_returns_none():
    assert fp.extract_json_from_llm("") is None
    assert fp.extract_json_from_llm(None) is None


def test_plain_json_object():
    assert fp.extract_json_from_llm('{"a": 1}') == {"a": 1}


def test_markdown_fenced_json():
    assert fp.extract_json_from_llm('```json\n{"a": 1}\n```') == {"a": 1}
    assert fp.extract_json_from_llm('```\n{"a": 1}\n```') == {"a": 1}


def test_json_embedded_in_prose():
    assert fp.extract_json_from_llm('Sure! {"a": 1} hope that helps') == {"a": 1}


def test_nested_braces_span_first_to_last():
    assert fp.extract_json_from_llm('{"a": {"b": 2}}') == {"a": {"b": 2}}


def test_no_braces_returns_none():
    assert fp.extract_json_from_llm("no json here") is None


def test_malformed_json_between_braces_returns_none():
    assert fp.extract_json_from_llm("{ not valid }") is None


# --- normalize_taxonomy_nodes -------------------------------------------------


def test_none_or_empty_returns_empty_list():
    assert fp.normalize_taxonomy_nodes(None) == []
    assert fp.normalize_taxonomy_nodes({}) == []


def test_taxonomy_key_full_node():
    data = {"taxonomy": [{"level1": "A", "level2": "B", "level3": "C", "description": "d"}]}
    assert fp.normalize_taxonomy_nodes(data) == [
        {"level1": "A", "level2": "B", "level3": "C", "description": "d"}
    ]


def test_alternate_keys_and_category_alias():
    data = {"categories": [{"category": "X"}]}
    assert fp.normalize_taxonomy_nodes(data) == [
        {"level1": "X", "level2": None, "level3": None, "description": ""}
    ]


def test_non_dict_entries_are_skipped():
    data = {"features": ["junk", {"level1": "Y"}]}
    out = fp.normalize_taxonomy_nodes(data)
    assert len(out) == 1 and out[0]["level1"] == "Y"


def test_level1_wins_over_category_when_both_present():
    data = {"taxonomy": [{"level1": "Real", "category": "Ignored"}]}
    assert fp.normalize_taxonomy_nodes(data)[0]["level1"] == "Real"
