"""The fix agent loop (repo-agent/agents/fix_agent.py) and its entry point
(repo_agent_core.analyze), driven by a scripted model.

These prove the mechanics a real model relies on — multi-file edits, the
verification gate, budgets, recovery from bad replies — not model quality.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest
from agents import fix_agent
from agents.fix_agent import AgentError, FixAgent, ModelQuotaExhausted, parse_reply
from repo.sandbox import LocalSandbox, TestHarness, detect_test_plan
from repo.workspace import Workspace

import llm
import repo_agent_core
from llm.base import LLMProvider

PYTEST = f"{sys.executable} -m pytest -q -rfE --no-header -p no:cacheprovider"

ORDERS = """\
from shop import pricing


def order_total(items, coupon=None):
    amount = pricing.subtotal(items)
    amount = pricing.apply_coupon(amount, coupon)
    return round(amount + pricing.shipping_cost(amount), 2)


def refund_total(items, coupon=None):
    amount = pricing.subtotal(items)
    amount = pricing.apply_coupon(amount, coupon)
    return amount
"""

PRICING = """\
def subtotal(items):
    return round(sum(price * qty for price, qty in items), 2)


def shipping_cost(amount):
    return 0.0 if amount > 50 else 4.99


def apply_coupon(amount, coupon):
    return round(amount * (1 - coupon["percent_off"] / 100), 2)
"""

TESTS = """\
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shop import orders, pricing


def test_coupon_is_applied():
    assert orders.order_total([(100.0, 1)], {"percent_off": 10}) == 90.0


def test_small_order_pays_shipping():
    assert pricing.shipping_cost(20.0) == 4.99
"""

# Bugs seeded above: both order_total and refund_total crash without a coupon
# (two sites, one file), and free shipping uses > instead of >= (second file).
NEW_TESTS = """

def test_no_coupon_does_not_crash():
    assert orders.order_total([(10.0, 1)]) == 14.99
    assert orders.refund_total([(10.0, 1)]) == 10.0


def test_free_shipping_at_threshold():
    assert pricing.shipping_cost(50.0) == 0.0
"""

GUARD_OLD = "    amount = pricing.apply_coupon(amount, coupon)\n"
GUARD_NEW = "    if coupon is not None:\n        amount = pricing.apply_coupon(amount, coupon)\n"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "shop").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "shop" / "__init__.py").write_text("")
    (root / "shop" / "orders.py").write_text(ORDERS)
    (root / "shop" / "pricing.py").write_text(PRICING)
    (root / "tests" / "test_shop.py").write_text(TESTS)
    return root


def act(*actions, thought="next"):
    return json.dumps(
        {"thought": thought, "actions": [{"tool": tool, "args": args} for tool, args in actions]}
    )


def edit(path, old, new, **extra):
    return ("edit_file", {"path": path, "old": old, "new": new, **extra})


FINISH = act(("finish", {"summary": "Guarded the coupon.", "root_cause": "coupon may be None"}))
GUARD_FIX = edit("shop/orders.py", GUARD_OLD, GUARD_NEW, replace_all=True)
SHIPPING_FIX = edit("shop/pricing.py", "amount > 50", "amount >= 50")


class ScriptedModel(LLMProvider):
    """Replays a list of replies (an Exception entry is raised instead)."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen: list[list[dict]] = []
        self.system = None

    def generate(self, prompt, *, system=None, temperature=0.1):
        raise AssertionError("the agent must use converse()")

    def converse(self, system, messages, *, temperature=0.1, json_mode=False):
        assert json_mode is True
        self.system = system
        self.seen.append([dict(m) for m in messages])
        reply = self.replies.pop(0) if self.replies else act(("list_files", {}))
        if isinstance(reply, Exception):
            raise reply
        return reply

    def last_tool_output(self):
        return self.seen[-1][-1]["content"]


def make_agent(repo, replies, **kwargs):
    model = ScriptedModel(replies)
    kwargs.setdefault("sleep", lambda _seconds: None)
    return FixAgent(Workspace(repo), model, **kwargs), model


def run_pytest(root):
    return subprocess.run(PYTEST.split() + ["tests"], cwd=root, capture_output=True, text=True)


# ── A real multi-site, multi-file fix ────────────────────────────────────


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_multi_file_fix_is_verified_and_its_diff_applies(repo, tmp_path):
    pristine = tmp_path / "pristine"
    shutil.copytree(repo, pristine)
    last_test_line = "    assert pricing.shipping_cost(20.0) == 4.99\n"
    added_tests = last_test_line + NEW_TESTS

    agent, model = make_agent(
        repo,
        [
            act(("search", {"pattern": "coupon"}), ("read_file", {"path": "shop/orders.py"})),
            act(GUARD_FIX, SHIPPING_FIX, edit("tests/test_shop.py", last_test_line, added_tests)),
            FINISH,
        ],
    )
    result = agent.run(
        "Checkout crashes when no coupon is applied", ["it just crashes at checkout"]
    )

    assert result.status == "ok"
    assert result.steps == 3
    assert result.files_changed == ["shop/orders.py", "shop/pricing.py", "tests/test_shop.py"]
    assert result.root_cause == "coupon may be None"
    assert result.verification["syntax"]["ok"] is True
    assert result.verification["tests"]["ran"] is False  # no sandbox configured
    assert result.verification["test_files_changed"] == ["tests/test_shop.py"]
    assert [t["tools"] for t in result.trace] == [
        ["search", "read_file"],
        ["edit_file", "edit_file", "edit_file"],
        ["finish"],
    ]

    # The bug is real on an untouched copy, and gone once the diff is applied there.
    crash = [sys.executable, "-c", "from shop import orders; orders.order_total([(10.0, 1)])"]
    assert subprocess.run(crash, cwd=pristine, capture_output=True).returncode != 0
    (tmp_path / "fix.patch").write_text(result.diff)
    subprocess.run(["git", "apply", str(tmp_path / "fix.patch")], cwd=pristine, check=True)
    assert subprocess.run(crash, cwd=pristine, capture_output=True).returncode == 0
    after = run_pytest(pristine)
    assert after.returncode == 0 and "4 passed" in after.stdout, after.stdout


def test_opening_message_carries_the_problem_reports_and_repo_map(repo):
    agent, model = make_agent(repo, [act(("give_up", {"reason": "n/a"}))])
    agent.run("Checkout crashes", ["  App died when I paid without a promo code  ", "", "x" * 5000])

    opening = model.seen[0][0]["content"]
    assert "# Problem to fix\nCheckout crashes" in opening
    assert '<report n="1">\nApp died when I paid without a promo code\n</report>' in opening
    assert "x" * 801 not in opening  # long reports are clipped
    assert "shop/orders.py (13 lines)\n    def order_total; def refund_total" in opening
    assert "You cannot execute code here" in model.system
    assert "run_tests" not in model.system  # tool not offered without a sandbox
    # an unrunnable test with a wrong expected value would break the customer's CI
    assert "Do not add or change test files" in model.system
    assert "add or update a test that covers the fix" not in model.system


# ── The verification gate ────────────────────────────────────────────────


def test_finish_is_rejected_until_the_change_parses(repo):
    broken = edit(
        "shop/orders.py",
        "coupon=None):\n    amount = pricing.subtotal",
        "coupon=None:\n    amount = pricing.subtotal",
        replace_all=True,
    )
    repaired = edit("shop/orders.py", "coupon=None:\n", "coupon=None):\n", replace_all=True)
    agent, model = make_agent(repo, [act(broken), FINISH, act(repaired, GUARD_FIX), FINISH])

    result = agent.run("Checkout crashes when no coupon is applied")

    rejection = model.seen[2][-1]["content"]
    assert "finish REJECTED" in rejection
    assert "shop/orders.py does not parse: line 4" in rejection
    assert result.status == "ok" and result.steps == 4
    compile((repo / "shop" / "orders.py").read_text(), "orders.py", "exec")


def test_finish_without_any_change_is_rejected(repo):
    agent, model = make_agent(repo, [FINISH, act(("give_up", {"reason": "Pricing complaint."}))])
    result = agent.run("Your product is too expensive")

    assert "no file was changed" in model.last_tool_output()
    assert result.status == "not_actionable"
    assert result.summary == "Pricing complaint."
    assert result.diff == ""


def test_edits_and_finish_in_one_reply_cost_a_single_request(repo):
    finish = ("finish", {"summary": "s", "root_cause": "r"})
    agent, model = make_agent(repo, [act(SHIPPING_FIX, finish)])
    result = agent.run("Free shipping threshold is off by one")

    assert result.status == "ok" and result.steps == 1 and len(model.seen) == 1
    assert result.files_changed == ["shop/pricing.py"]


def test_finish_is_held_back_when_an_edit_in_the_same_reply_failed(repo):
    finish = ("finish", {"summary": "s", "root_cause": "r"})
    typo = edit("shop/pricing.py", "amount  > 50", "amount >= 50")  # does not match the file
    agent, model = make_agent(repo, [act(GUARD_FIX, typo, finish), act(SHIPPING_FIX, finish)])
    result = agent.run("Checkout crashes and shipping is wrong")

    held = model.last_tool_output()
    assert 'ERROR: "old" was not found in shop/pricing.py' in held
    assert "finish was NOT processed because an earlier action in this reply failed" in held
    assert result.status == "ok" and result.steps == 2
    assert result.files_changed == ["shop/orders.py", "shop/pricing.py"]


def test_give_up_combined_with_other_actions_is_not_processed(repo):
    give_up = ("give_up", {"reason": "cannot fix"})
    agent, model = make_agent(repo, [act(("list_files", {}), give_up), act(give_up)])
    result = agent.run("anything")

    assert "give_up was NOT processed" in model.last_tool_output()
    assert result.status == "not_actionable" and result.steps == 2


def test_small_repos_are_shown_in_full_up_front(repo, monkeypatch):
    agent, model = make_agent(repo, [act(("give_up", {"reason": "n/a"}))])
    agent.run("anything")
    opening = model.seen[0][0]["content"]
    assert "# File contents" in opening
    assert "shop/pricing.py (lines 1-10 of 10)\n    1| def subtotal(items):" in opening

    monkeypatch.setattr(fix_agent, "INLINE_REPO_CHARS", 100)  # now "too big" to inline
    agent, model = make_agent(repo, [act(("give_up", {"reason": "n/a"}))])
    agent.run("anything")
    opening = model.seen[0][0]["content"]
    assert "# File contents" not in opening and "Start by exploring" in opening
    assert "shop/pricing.py (10 lines)" in opening  # the map is still there


def test_fix_that_breaks_an_existing_test_is_rejected(repo):
    """With a sandbox, the repo's own tests gate the fix."""
    sandbox = LocalSandbox(repo)
    harness = TestHarness(sandbox, detect_test_plan(repo, test_override=PYTEST), timeout=120)
    harness.prepare()
    assert harness.available, harness.reason

    breaks_shipping = edit("shop/pricing.py", "else 4.99", "else 0.0")
    undo = edit("shop/pricing.py", "else 0.0", "else 4.99")
    agent, model = make_agent(
        repo,
        [act(breaks_shipping), FINISH, act(undo, GUARD_FIX), act(("run_tests", {})), FINISH],
        sandbox=sandbox,
        harness=harness,
    )
    result = agent.run("Checkout crashes when no coupon is applied")

    rejection = model.seen[2][-1]["content"]
    assert "finish REJECTED" in rejection
    assert "newly failing: tests/test_shop.py::test_small_order_pays_shipping" in rejection
    assert "No regressions against the baseline." in model.seen[4][-1]["content"]

    assert result.status == "ok"
    assert result.files_changed == ["shop/orders.py"]
    assert result.verification["tests"]["ran"] is True
    (line,) = result.verification["tests"]["lines"]
    assert line.startswith(f"`{PYTEST}`: 2 passed in ") and " before -> 2 passed in " in line
    assert "run_tests" in model.system and "2 passed" in model.system
    assert "add or update a test that covers the fix" in model.system
    assert "Do not add or change test files" not in model.system


# ── Budgets and failure handling ─────────────────────────────────────────


def test_step_budget_ends_the_run_without_a_fix(repo):
    agent, model = make_agent(repo, [], max_steps=5)  # the model only ever lists files
    result = agent.run("anything")

    assert result.status == "no_fix"
    assert result.steps == 5 and len(model.seen) == 5
    assert "step budget exhausted" in result.error
    assert "almost out of steps" in model.last_tool_output()


def test_time_budget_ends_the_run(repo):
    ticks = iter([0.0, 0.0, 10.0, 99999.0])
    agent, _ = make_agent(repo, [], timeout_seconds=60, clock=lambda: next(ticks))
    result = agent.run("anything")
    assert result.status == "no_fix" and "time budget" in result.error
    assert result.steps == 2


def test_unusable_replies_get_one_correction_then_stop(repo):
    replies = [
        "I think the bug is in orders.py",
        act(("list_files", {})),
        "",
        "nope",
        "still prose",
    ]
    agent, model = make_agent(repo, replies)
    result = agent.run("anything")

    assert "Reply with exactly one JSON object" in model.seen[1][-1]["content"]
    assert result.status == "no_fix"
    assert "unusable format" in result.error


def test_model_errors_are_retried_with_backoff(repo):
    sleeps: list[float] = []
    agent, _ = make_agent(
        repo,
        [RuntimeError("429 quota"), RuntimeError("503"), act(("give_up", {"reason": "done"}))],
        sleep=sleeps.append,
    )
    assert agent.run("anything").status == "not_actionable"
    assert sleeps == [5.0, 15.0]


class QuotaModel(ScriptedModel):
    """A provider that says how long to wait, as Gemini does on 429s."""

    def retry_after_seconds(self, exc):
        return getattr(exc, "wait", None)


def _limit(wait):
    exc = RuntimeError("429 RESOURCE_EXHAUSTED")
    exc.wait = wait
    return exc


def test_exhausted_daily_quota_stops_at_once_instead_of_sleeping(repo):
    sleeps: list[float] = []
    model = QuotaModel([_limit(39041.0)])
    agent = FixAgent(Workspace(repo), model, sleep=sleeps.append)

    with pytest.raises(ModelQuotaExhausted, match="retry in 10.8h") as caught:
        agent.run("anything")
    assert caught.value.retry_after == 39041.0
    assert sleeps == [] and len(model.seen) == 1  # no retries burned on a dead quota


def test_brief_rate_limit_waits_as_long_as_the_provider_asks(repo):
    sleeps: list[float] = []
    model = QuotaModel([_limit(23.0), act(("give_up", {"reason": "done"}))])
    agent = FixAgent(Workspace(repo), model, sleep=sleeps.append)

    assert agent.run("anything").status == "not_actionable"
    assert sleeps == [24.0]  # the provider's 23s (+1), not our default 5s


def test_persistent_model_failure_raises_for_the_job_queue_to_retry(repo):
    agent, _ = make_agent(repo, [RuntimeError("down")] * 10)
    with pytest.raises(AgentError, match="model call failed after retries"):
        agent.run("anything")


def test_old_tool_output_is_dropped_when_the_transcript_grows(monkeypatch):
    monkeypatch.setattr(fix_agent, "MAX_CONTEXT_CHARS", 1000)
    messages = [{"role": "user", "content": "opening " + "o" * 500}]
    for i in range(6):
        messages.append({"role": "assistant", "content": f"reply {i}"})
        messages.append({"role": "user", "content": f"output {i} " + "x" * 400})
    FixAgent._trim(messages)

    assert messages[0]["content"].startswith("opening")  # problem + repo map kept
    assert messages[2]["content"].startswith("[earlier tool output dropped")
    assert messages[-1]["content"].startswith("output 5")  # recent results kept
    assert all(m["content"].startswith("reply") for m in messages[1::2])


# ── Reply parsing ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "reply",
    [
        '{"thought": "t", "actions": [{"tool": "read_file", "args": {"path": "a.py"}}]}',
        '```json\n{"thought": "t", "actions": [{"tool": "read_file", "args": {"path": "a.py"}}]}\n```',
        'Sure, here you go: {"thought": "t", "action": {"tool": "read_file", "args": {"path": "a.py"}}}',
        '{"thought": "t", "tool": "read_file", "args": {"path": "a.py"}}',
        '{"thought": "t", "actions": [{"name": "read_file", "arguments": {"path": "a.py"}}]}',
        # arguments beside "tool" rather than under "args" (seen from GLM-5.3)
        '{"thought": "t", "actions": [{"tool": "read_file", "path": "a.py"}]}',
        '{"thought": "t", "tool": "read_file", "path": "a.py"}',
    ],
)
def test_parse_reply_accepts_common_shapes(reply):
    assert parse_reply(reply) == ("t", [{"tool": "read_file", "args": {"path": "a.py"}}])


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "no json here",
        "[1, 2]",
        '{"thought": "only thinking"}',
        '{"actions": []}',
        '{"actions": [{"tool": 5}]}',
        '{"actions": ["read_file"]}',
    ],
)
def test_parse_reply_rejects_unusable_replies(reply):
    assert parse_reply(reply) is None


# ── repo_agent_core.analyze ──────────────────────────────────────────────


@pytest.fixture
def scripted_llm():
    def install(replies):
        model = ScriptedModel(replies)
        llm.reset_cache()
        llm._llm = model
        return model

    yield install
    llm.reset_cache()


FIX_SCRIPT = [act(GUARD_FIX), FINISH]


def test_analyze_never_modifies_the_source_repo_by_default(repo, scripted_llm):
    scripted_llm(FIX_SCRIPT)
    events: list[dict] = []
    result = repo_agent_core.analyze(str(repo), "Checkout crashes", on_event=events.append)

    assert result["status"] == "ok"
    assert result["goal"] == "Checkout crashes"
    assert result["files_changed"] == ["shop/orders.py"]
    assert result["diff"].startswith("diff --git a/shop/orders.py b/shop/orders.py\n")
    assert (repo / "shop" / "orders.py").read_text() == ORDERS  # untouched
    assert [e["tools"] for e in events] == [["edit_file"], ["finish"]]


def test_analyze_in_place_edits_the_given_checkout(repo, scripted_llm):
    scripted_llm(FIX_SCRIPT)
    result = repo_agent_core.analyze(str(repo), "Checkout crashes", in_place=True)
    assert result["status"] == "ok"
    assert "if coupon is not None:" in (repo / "shop" / "orders.py").read_text()


def test_analyze_reports_unusable_repositories(tmp_path, scripted_llm):
    scripted_llm([])
    assert repo_agent_core.analyze(str(tmp_path / "missing"), "x")["status"] == "error"
    (tmp_path / "empty").mkdir()
    result = repo_agent_core.analyze(str(tmp_path / "empty"), "x")
    assert result == {
        "status": "error",
        "error": "No readable source files found in the repository.",
    }


def test_docker_sandbox_without_docker_degrades_to_static_checks(repo, scripted_llm, monkeypatch):
    """Never fall back to running customer code on the host; say why tests did not run."""
    from config import get_settings

    monkeypatch.setattr(get_settings(), "repo_agent_sandbox", "docker")
    monkeypatch.setattr(repo_agent_core.shutil, "which", lambda name: None)
    model = scripted_llm(FIX_SCRIPT)

    result = repo_agent_core.analyze(str(repo), "Checkout crashes")

    assert result["status"] == "ok"
    assert result["verification"]["tests"]["ran"] is False
    assert "docker is not installed" in result["verification"]["tests"]["reason"]
    assert "run_tests" not in model.system
