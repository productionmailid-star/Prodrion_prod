"""Characterization tests for feedback_platform.Normalizer.

These pin the *current* behaviour of each source adapter so later milestones
(idempotency keys, corrected field mappings) change it deliberately, not by
accident.
"""

from __future__ import annotations

import uuid

import feedback_platform as fp


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, TypeError):
        return False


def test_standard_object_defaults():
    obj = fp.Normalizer.from_webhook({"text": "hello"})
    assert _is_uuid(obj.id)
    assert obj.labels == []
    assert obj.arr == 0.0
    assert obj.plan_type == ""
    assert obj.jira_ticket == "" and obj.jira_status == ""


# --- from_webhook --------------------------------------------------------------


def test_from_webhook_maps_common_fields():
    obj = fp.Normalizer.from_webhook(
        {"text": "cannot log in", "email": "a@b.com", "user_id": "7", "source": "typeform"}
    )
    assert obj.text == "cannot log in"
    assert obj.user_email == "a@b.com"
    assert obj.user_id == "7"
    assert obj.source == "typeform"
    assert obj.raw_meta == {
        "text": "cannot log in",
        "email": "a@b.com",
        "user_id": "7",
        "source": "typeform",
    }


def test_from_webhook_text_precedence_and_defaults():
    assert fp.Normalizer.from_webhook({"message": "m"}).text == "m"
    assert fp.Normalizer.from_webhook({"body": "b"}).text == "b"
    assert fp.Normalizer.from_webhook({"feedback": "f"}).text == "f"
    # no recognised text field -> str(payload); source defaults to "webhook"
    fallback = fp.Normalizer.from_webhook({"foo": "bar"})
    assert fallback.text == "{'foo': 'bar'}"
    assert fallback.source == "webhook"


def test_from_webhook_alt_key_casing():
    obj = fp.Normalizer.from_webhook({"text": "x", "userId": 9, "user_email": "c@d.com"})
    assert obj.user_id == "9"
    assert obj.user_email == "c@d.com"


# --- from_zendesk ------------------------------------------------------------


def test_from_zendesk_reads_requester_and_description():
    obj = fp.Normalizer.from_zendesk(
        {
            "description": "SSO broken",
            "requester": {"id": 42, "email": "x@y.com"},
            "created_at": "2024-01-01T00:00:00Z",
        }
    )
    assert obj.text == "SSO broken"
    assert obj.user_id == "42"
    assert obj.user_email == "x@y.com"
    assert obj.source == "zendesk"
    assert obj.timestamp == "2024-01-01T00:00:00Z"


def test_from_zendesk_missing_requester():
    obj = fp.Normalizer.from_zendesk({"description": "d"})
    assert obj.user_id == ""
    assert obj.user_email == ""


# --- from_app_store / from_play_store --------------------------------------


def test_from_app_store_concatenates_title_and_content():
    assert fp.Normalizer.from_app_store({"title": "T", "content": "C"}).text == "T C"
    assert fp.Normalizer.from_app_store({"content": "C"}).text == "C"
    assert fp.Normalizer.from_app_store({"title": "T"}).text == "T"
    obj = fp.Normalizer.from_app_store({"title": "T", "content": "C", "userName": "bob"})
    assert obj.user_id == "bob"
    assert obj.user_email == ""
    assert obj.source == "app_store"


def test_from_play_store_uses_content_only():
    obj = fp.Normalizer.from_play_store({"content": "crashes", "userName": "kim"})
    assert obj.text == "crashes"
    assert obj.user_id == "kim"
    assert obj.source == "play_store"


# --- from_slack ------------------------------------------------------------


def test_from_slack_maps_text_user_and_ts():
    obj = fp.Normalizer.from_slack({"text": "hello", "user": "U1", "ts": "1700000000.000100"})
    assert obj.text == "hello"
    assert obj.user_id == "U1"
    assert obj.source == "slack"
    assert obj.timestamp.startswith("2023-11-14T")
