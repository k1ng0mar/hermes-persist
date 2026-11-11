"""Unit tests for persist. stdlib unittest, no Hermes install needed.

Run: python3 -m unittest discover -s test -v
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from __init__ import Persist, register, DEFAULT_MIN_ATTEMPTS, MAX_FOOTER_LINES  # noqa: E402


def _fail(tool, args, error="boom", sid="s1"):
    return {
        "tool_name": tool,
        "args": args,
        "result": json.dumps({"error": error}),
        "session_id": sid,
        "status": "error",
        "error_message": error,
    }


def _ok(tool, args, sid="s1"):
    return {
        "tool_name": tool,
        "args": args,
        "result": json.dumps({"result": "done"}),
        "session_id": sid,
        "status": "ok",
        "error_message": None,
    }


def _pre_verify_kwargs(coding=True, attempt=0, final_response="", sid="s1"):
    return {
        "coding": coding,
        "attempt": attempt,
        "final_response": final_response,
        "session_id": sid,
        "platform": "cli",
        "model": "test",
        "changed_paths": ["foo.py"],
    }


class FakeCtx:
    def __init__(self, settings=None):
        self.settings = settings or {}
        self.hooks = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, fn, **kwargs):
        self.hooks.setdefault(name, []).append(fn)


class TestDistinctFailureCounting(unittest.TestCase):
    def setUp(self):
        self.p = Persist(min_attempts=3)

    def test_same_command_twice_counts_as_one_approach(self):
        args = {"command": "pytest"}
        self.p.on_post_tool_call(**_fail("terminal", args))
        self.p.on_post_tool_call(**_fail("terminal", args))
        self.p.on_post_tool_call(**_fail("terminal", args))
        # 1 distinct approach, so the gate should still fire
        r = self.p.on_pre_verify(**_pre_verify_kwargs(final_response="I can't get this green"))
        self.assertIsNotNone(r)
        self.assertEqual(r["action"], "continue")
        self.assertIn("1 distinct attempt", r["message"])

    def test_different_commands_count_as_different_approaches(self):
        self.p.on_post_tool_call(**_fail("terminal", {"command": "pytest"}))
        self.p.on_post_tool_call(**_fail("terminal", {"command": "pytest -x"}))
        self.p.on_post_tool_call(**_fail("patch", {"path": "foo.py"}))
        # 3 distinct approaches, floor is 3, so the gate should NOT fire
        r = self.p.on_pre_verify(**_pre_verify_kwargs(final_response="I can't get this green"))
        self.assertIsNone(r)

    def test_ok_calls_do_not_count(self):
        self.p.on_post_tool_call(**_ok("terminal", {"command": "ls"}))
        self.p.on_post_tool_call(**_ok("terminal", {"command": "pwd"}))
        r = self.p.on_pre_verify(**_pre_verify_kwargs(final_response="I can't get this green"))
        self.assertIsNone(r)

    def test_zero_failures_never_nudges(self):
        r = self.p.on_pre_verify(**_pre_verify_kwargs(final_response="I can't get this green"))
        self.assertIsNone(r)


class TestGiveUpDetection(unittest.TestCase):
    def setUp(self):
        self.p = Persist(min_attempts=3)
        self.p.on_post_tool_call(**_fail("terminal", {"command": "pytest"}))

    def test_cant_finish_triggers(self):
        r = self.p.on_pre_verify(**_pre_verify_kwargs(final_response="I can't finish this task"))
        self.assertIsNotNone(r)

    def test_im_unable_triggers(self):
        r = self.p.on_pre_verify(**_pre_verify_kwargs(final_response="I'm unable to resolve this"))
        self.assertIsNotNone(r)

    def test_stuck_triggers(self):
        r = self.p.on_pre_verify(**_pre_verify_kwargs(final_response="I'm stuck on this bug"))
        self.assertIsNotNone(r)

    def test_success_language_does_not_trigger(self):
        r = self.p.on_pre_verify(**_pre_verify_kwargs(final_response="All tests pass, done!"))
        self.assertIsNone(r)

    def test_cure_phrase_containing_green_still_triggers(self):
        # Regression: "tests green" inside a negation is a give-up, not success.
        r = self.p.on_pre_verify(**_pre_verify_kwargs(final_response="I cannot get these tests green."))
        self.assertIsNotNone(r)

    def test_tests_pass_after_failure_does_not_trigger(self):
        r = self.p.on_pre_verify(**_pre_verify_kwargs(final_response="Tests pass now."))
        self.assertIsNone(r)

    def test_cannot_get_green_after_failure_triggers(self):
        r = self.p.on_pre_verify(**_pre_verify_kwargs(final_response="I can't get this green."))
        self.assertIsNotNone(r)

    def test_not_fixable_triggers(self):
        r = self.p.on_pre_verify(**_pre_verify_kwargs(final_response="this is not fixable."))
        self.assertIsNotNone(r)

    def test_no_correct_x_can_triggers(self):
        r = self.p.on_pre_verify(**_pre_verify_kwargs(final_response="no correct add can satisfy both."))
        self.assertIsNotNone(r)

    def test_impossible_to_satisfy_triggers(self):
        r = self.p.on_pre_verify(**_pre_verify_kwargs(final_response="it is impossible to satisfy both specs."))
        self.assertIsNotNone(r)

    def test_tests_are_green_is_success(self):
        r = self.p.on_pre_verify(**_pre_verify_kwargs(final_response="All tests are green now."))
        self.assertIsNone(r)

    def test_cant_go_green_triggers(self):
        r = self.p.on_pre_verify(**_pre_verify_kwargs(final_response="The suite can't go green."))
        self.assertIsNotNone(r)

    def test_no_real_impl_can_satisfy_triggers(self):
        r = self.p.on_pre_verify(**_pre_verify_kwargs(final_response="No real implementation can satisfy both."))
        self.assertIsNotNone(r)

    def test_mutually_exclusive_triggers(self):
        r = self.p.on_pre_verify(**_pre_verify_kwargs(final_response="The two tests are mutually exclusive."))
        self.assertIsNotNone(r)

    def test_not_going_to_fake_triggers(self):
        r = self.p.on_pre_verify(**_pre_verify_kwargs(final_response="I'm not going to fake it."))
        self.assertIsNotNone(r)

    def test_neutral_stop_after_red_turn_still_nudges(self):
        # The turn ended on a failed tool call: a neutral wrap-up is still a
        # stop-while-broken. This is the deterministic path that does not
        # depend on give-up phrasing.
        r = self.p.on_pre_verify(**_pre_verify_kwargs(final_response="Here's the file you asked for"))
        self.assertIsNotNone(r)

    def test_neutral_stop_after_clean_turn_does_not_nudge(self):
        p2 = Persist(min_attempts=3)
        p2.on_post_tool_call(**_fail("terminal", {"command": "pytest"}))
        p2.on_post_tool_call(**_ok("read_file", {"path": "calc.py"}))
        r = p2.on_pre_verify(**_pre_verify_kwargs(final_response="Here's the file you asked for"))
        self.assertIsNone(r)

    def test_policy_refusal_without_failures_does_not_trigger(self):
        # No failures recorded, so even give-up language is ignored
        p2 = Persist(min_attempts=3)
        r = p2.on_pre_verify(**_pre_verify_kwargs(final_response="I can't do that, it violates policy"))
        self.assertIsNone(r)


class TestGateConditions(unittest.TestCase):
    def setUp(self):
        self.p = Persist(min_attempts=3)
        self.p.on_post_tool_call(**_fail("terminal", {"command": "pytest"}))

    def test_non_coding_turn_does_not_nudge(self):
        r = self.p.on_pre_verify(**_pre_verify_kwargs(coding=False, final_response="I can't get this green"))
        self.assertIsNone(r)

    def test_attempt_gt_zero_does_not_nudge(self):
        r = self.p.on_pre_verify(**_pre_verify_kwargs(attempt=1, final_response="I can't get this green"))
        self.assertIsNone(r)

    def test_nudges_once_per_turn(self):
        # First call nudges
        r1 = self.p.on_pre_verify(**_pre_verify_kwargs(attempt=0, final_response="I can't get this green"))
        self.assertIsNotNone(r1)
        # Second call (attempt=1) does not
        r2 = self.p.on_pre_verify(**_pre_verify_kwargs(attempt=1, final_response="I can't get this green"))
        self.assertIsNone(r2)

    def test_nudges_once_even_if_caller_keeps_attempt_zero(self):
        # Don't rely on the core's attempt counter: self-throttle on nudges.
        r1 = self.p.on_pre_verify(**_pre_verify_kwargs(attempt=0, final_response="I can't get this green"))
        self.assertIsNotNone(r1)
        r2 = self.p.on_pre_verify(**_pre_verify_kwargs(attempt=0, final_response="I can't get this green"))
        self.assertIsNone(r2)
        r3 = self.p.on_pre_verify(**_pre_verify_kwargs(attempt=0, final_response="I can't get this green"))
        self.assertIsNone(r3)

    def test_throttle_resets_next_turn(self):
        self.assertIsNotNone(self.p.on_pre_verify(**_pre_verify_kwargs(attempt=0, final_response="I can't get this green")))
        # new user turn
        self.p.on_pre_llm_call(session_id="s1")
        self.p.on_post_tool_call(**_fail("terminal", {"command": "pytest"}))
        self.assertIsNotNone(self.p.on_pre_verify(**_pre_verify_kwargs(attempt=0, final_response="I can't get this green")))


class TestEndedErrorSignal(unittest.TestCase):
    """The turn ending on a failed tool call is a deterministic give-up signal
    that does not depend on phrasing."""

    def setUp(self):
        self.p = Persist(min_attempts=3)

    def test_ended_on_error_nudges_without_any_giveup_phrase(self):
        self.p.on_post_tool_call(**_fail("terminal", {"command": "unittest"}))
        # neutral wording, but the turn ended red
        r = self.p.on_pre_verify(**_pre_verify_kwargs(
            final_response="The remaining failure is not a code bug."))
        self.assertIsNotNone(r)

    def test_ended_clean_does_not_nudge(self):
        self.p.on_post_tool_call(**_fail("terminal", {"command": "unittest"}))
        # a later successful call means the turn did not end red
        self.p.on_post_tool_call(**_ok("read_file", {"path": "calc.py"}))
        r = self.p.on_pre_verify(**_pre_verify_kwargs(
            final_response="Here is a summary of the file."))
        self.assertIsNone(r)

    def test_ended_error_still_respects_floor(self):
        for i in range(3):
            self.p.on_post_tool_call(**_fail("terminal", {"command": "unittest %d" % i}))
        r = self.p.on_pre_verify(**_pre_verify_kwargs(
            attempt=1, final_response="Stopping here."))
        self.assertIsNone(r)

    def test_ended_error_still_one_nudge_per_turn(self):
        self.p.on_post_tool_call(**_fail("terminal", {"command": "unittest"}))
        self.assertIsNotNone(self.p.on_pre_verify(**_pre_verify_kwargs(final_response="Stopping here.")))
        self.assertIsNone(self.p.on_pre_verify(**_pre_verify_kwargs(final_response="Stopping here.")))


class TestMinAttemptsSetting(unittest.TestCase):
    def test_min_attempts_1_disables_gate(self):
        p = Persist(min_attempts=1)
        p.on_post_tool_call(**_fail("terminal", {"command": "pytest"}))
        # 1 failure >= floor of 1, so no nudge
        r = p.on_pre_verify(**_pre_verify_kwargs(final_response="I can't get this green"))
        self.assertIsNone(r)

    def test_min_attempts_5_raises_floor(self):
        p = Persist(min_attempts=5)
        for i in range(4):
            p.on_post_tool_call(**_fail("terminal", {"command": "pytest %d" % i}))
        # 4 < 5, so gate fires
        r = p.on_pre_verify(**_pre_verify_kwargs(final_response="I can't get this green"))
        self.assertIsNotNone(r)
        self.assertIn("4 distinct attempt", r["message"])


class TestFooter(unittest.TestCase):
    def setUp(self):
        self.p = Persist(min_attempts=3)

    def test_footer_shows_when_failures_exist(self):
        self.p.on_post_tool_call(**_fail("terminal", {"command": "pytest"}))
        self.p.on_post_tool_call(**_fail("patch", {"path": "foo.py"}))
        out = self.p.on_transform_llm_output(response_text="I can't get this green", session_id="s1")
        self.assertIsNotNone(out)
        self.assertIn("persist: 2 distinct approach(es) failed", out)
        self.assertIn("- ran: pytest", out)
        self.assertIn("- patch: foo.py", out)

    def test_footer_absent_when_no_failures(self):
        out = self.p.on_transform_llm_output(response_text="Done!", session_id="s1")
        self.assertIsNone(out)

    def test_footer_shows_nudge_count(self):
        self.p.on_post_tool_call(**_fail("terminal", {"command": "pytest"}))
        # Trigger a nudge
        self.p.on_pre_verify(**_pre_verify_kwargs(final_response="I can't get this green"))
        out = self.p.on_transform_llm_output(response_text="Still stuck", session_id="s1")
        self.assertIn("forced 1 extra attempt", out)

    def test_footer_caps_at_max_lines(self):
        for i in range(MAX_FOOTER_LINES + 3):
            self.p.on_post_tool_call(**_fail("terminal", {"command": "pytest %d" % i}))
        out = self.p.on_transform_llm_output(response_text="I can't get this green", session_id="s1")
        self.assertIn("+%d more" % 3, out)

    def test_footer_absent_on_empty_response(self):
        self.p.on_post_tool_call(**_fail("terminal", {"command": "pytest"}))
        out = self.p.on_transform_llm_output(response_text="", session_id="s1")
        self.assertIsNone(out)


class TestTurnReset(unittest.TestCase):
    def setUp(self):
        self.p = Persist(min_attempts=3)

    def test_pre_llm_call_clears_ledger(self):
        self.p.on_post_tool_call(**_fail("terminal", {"command": "pytest"}))
        self.p.on_pre_llm_call(session_id="s1")
        out = self.p.on_transform_llm_output(response_text="I can't get this green", session_id="s1")
        self.assertIsNone(out)

    def test_pre_llm_call_does_not_clear_other_sessions(self):
        self.p.on_post_tool_call(**_fail("terminal", {"command": "pytest"}, sid="s1"))
        self.p.on_pre_llm_call(session_id="s2")
        out = self.p.on_transform_llm_output(response_text="I can't get this green", session_id="s1")
        self.assertIsNotNone(out)


class TestCleanup(unittest.TestCase):
    def setUp(self):
        self.p = Persist(min_attempts=3)

    def test_session_finalize_drops_entry(self):
        self.p.on_post_tool_call(**_fail("terminal", {"command": "pytest"}))
        self.p._drop(session_id="s1")
        out = self.p.on_transform_llm_output(response_text="I can't get this green", session_id="s1")
        self.assertIsNone(out)

    def test_post_compaction_drops_old_session(self):
        self.p.on_post_tool_call(**_fail("terminal", {"command": "pytest"}, sid="old-sess"))
        self.p._drop(session_id="new-sess", old_session_id="old-sess")
        out = self.p.on_transform_llm_output(response_text="I can't get this green", session_id="old-sess")
        self.assertIsNone(out)


class TestRegister(unittest.TestCase):
    def test_registers_all_hooks(self):
        ctx = FakeCtx()
        register(ctx)
        for hook in ("post_tool_call", "pre_llm_call", "pre_verify",
                     "transform_llm_output", "on_session_finalize",
                     "on_session_reset", "post_compaction"):
            self.assertIn(hook, ctx.hooks)

    def test_min_attempts_from_settings(self):
        ctx = FakeCtx(settings={"min_attempts": 5})
        p = register(ctx)
        self.assertEqual(p.min_attempts, 5)

    def test_invalid_min_attempts_falls_back(self):
        ctx = FakeCtx(settings={"min_attempts": "garbage"})
        p = register(ctx)
        self.assertEqual(p.min_attempts, DEFAULT_MIN_ATTEMPTS)


class TestFailSoft(unittest.TestCase):
    def test_post_tool_call_exception_is_swallowed(self):
        p = Persist()
        # args is not a dict, should not raise
        p.on_post_tool_call(tool_name="terminal", args=None, result="x", status="error", session_id="s1")
        # still counted (signature handles None args)
        out = p.on_transform_llm_output(response_text="I can't get this", session_id="s1")
        self.assertIsNotNone(out)

    def test_pre_verify_exception_is_swallowed(self):
        p = Persist()
        # final_response is not a string
        r = p.on_pre_verify(coding=True, attempt=0, final_response=None, session_id="s1")
        self.assertIsNone(r)


if __name__ == "__main__":
    unittest.main()
