"""Smoke test: the module imports and the FastAPI app is wired with the
expected routes. Anchors the surface area before it is restructured."""

from __future__ import annotations

import feedback_platform as fp


def test_app_metadata():
    assert fp.app.title == "Feedback Intelligence Platform"


def test_expected_routes_present():
    paths = {route.path for route in fp.app.routes}
    for expected in {
        "/webhook/feedback",
        "/webhook/slack",
        "/query",
        "/revenue-impact",
        "/bootstrap-taxonomy",
        "/taxonomy",
        "/import-users",
        "/jobs",
        "/jobs/{job_id}",
        "/credits",
        "/admin/credits/grant",
        "/webhook/stripe",
        "/repo-analysis",
        "/health",
    }:
        assert expected in paths
