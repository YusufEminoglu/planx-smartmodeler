"""Pure-Python tests for the QGIS-free multi-turn Agent Chat run loop."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from planx_smartmodeler.core.agent.contracts import (
    AgentMode,
    AgentResultStatus,
    AgentRisk,
    AgentRunLimits,
    AgentScope,
    AgentToolResult,
    AgentToolSpec,
)
from planx_smartmodeler.core.agent.controller import AgentController
from planx_smartmodeler.core.agent.registry import AgentToolRegistry
from planx_smartmodeler.core.agent.run_loop import (
    AgentRunLoop,
    RunAlreadyActiveError,
    RunEventKind,
)

EMPTY_SCHEMA = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
STATIC_INSTRUCTIONS = "Static Agent Chat instructions."


def final_turn_json(text: str = "Here is your answer.") -> str:
    return json.dumps(
        {
            "action": "final",
            "assistant_text": text,
            "tool_calls": [],
            "proposal_kind": "none",
            "proposal_json": "",
        }
    )


def tool_calls_turn_json(calls, assistant_text: str = "") -> str:
    return json.dumps(
        {
            "action": "tool_calls",
            "assistant_text": assistant_text,
            "tool_calls": [
                {"call_id": call_id, "tool_name": tool_name, "arguments_json": arguments_json}
                for call_id, tool_name, arguments_json in calls
            ],
            "proposal_kind": "none",
            "proposal_json": "",
        }
    )


def proposal_turn_json(kind: str, proposal_json: str, assistant_text: str = "Here is a proposal.") -> str:
    return json.dumps(
        {
            "action": "proposal",
            "assistant_text": assistant_text,
            "tool_calls": [],
            "proposal_kind": kind,
            "proposal_json": proposal_json,
        }
    )


class RecordingHandler:
    def __init__(self, result=None, raises: bool = False) -> None:
        self.calls = []
        self._result = result if result is not None else {"ok": True}
        self._raises = raises

    def __call__(self, call):
        self.calls.append(call)
        if self._raises:
            raise RuntimeError("boom")
        return self._result


def build_loop(
    limits: AgentRunLimits = None,
    echo_handler: RecordingHandler = None,
    mutate_handler: RecordingHandler = None,
):
    registry = AgentToolRegistry()
    echo_handler = echo_handler or RecordingHandler()
    registry.register(
        AgentToolSpec(
            name="test.echo",
            title="Echo",
            description="Echoes its arguments.",
            risk=AgentRisk.READ_ONLY,
            input_schema=EMPTY_SCHEMA,
            allowed_scopes=(AgentScope.PROJECT,),
        ),
        echo_handler,
    )
    mutate_handler = mutate_handler or RecordingHandler()
    registry.register(
        AgentToolSpec(
            name="test.mutate",
            title="Mutate",
            description="A non-read-only tool for approval-required testing.",
            risk=AgentRisk.MUTATING,
            input_schema=EMPTY_SCHEMA,
            allowed_scopes=(AgentScope.PROJECT,),
        ),
        mutate_handler,
    )
    controller = AgentController(registry, limits=limits)
    loop = AgentRunLoop(controller, STATIC_INSTRUCTIONS)
    return loop, controller, echo_handler, mutate_handler


class BasicLifecycleTests(unittest.TestCase):
    def test_one_tool_turn_followed_by_final(self) -> None:
        loop, _, echo_handler, _ = build_loop()
        event = loop.start("What layers do I have?", AgentMode.ASK, AgentScope.PROJECT)
        self.assertEqual(event.kind, RunEventKind.REQUEST_PROVIDER)
        token = event.request.request_token

        raw = tool_calls_turn_json([("c1", "test.echo", "{}")])
        event2 = loop.submit_provider_response(token, raw)
        self.assertEqual(event2.kind, RunEventKind.REQUEST_PROVIDER)
        self.assertEqual(len(echo_handler.calls), 1)

        token2 = event2.request.request_token
        event3 = loop.submit_provider_response(token2, final_turn_json("All done."))
        self.assertEqual(event3.kind, RunEventKind.FINAL)
        self.assertEqual(event3.text, "All done.")
        self.assertFalse(loop.is_active())

    def test_multiple_tool_calls_preserve_order(self) -> None:
        loop, _, echo_handler, _ = build_loop(AgentRunLimits(max_tool_calls_per_turn=3))
        event = loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        token = event.request.request_token
        raw = tool_calls_turn_json(
            [("c1", "test.echo", "{}"), ("c2", "test.echo", "{}"), ("c3", "test.echo", "{}")]
        )
        event2 = loop.submit_provider_response(token, raw)
        self.assertEqual(event2.kind, RunEventKind.REQUEST_PROVIDER)
        self.assertEqual(len(echo_handler.calls), 3)
        payload = json.loads(event2.request.user_prompt)
        tool_events = [e for e in payload["current_turn_events"] if e["kind"] == "tool_result"]
        self.assertEqual([e["call_id"] for e in tool_events], ["c1", "c2", "c3"])

    def test_controller_new_run_state_and_start_turn_lifecycle_is_used(self) -> None:
        loop, controller, _, _ = build_loop()
        loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        self.assertEqual(loop._run_state.limits, controller.limits)
        self.assertEqual(loop._run_state.turns, 1)

    def test_scope_and_mode_are_captured_and_fixed_for_the_run(self) -> None:
        loop, _, _, _ = build_loop()
        loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        self.assertEqual(loop.mode, AgentMode.ASK)
        self.assertEqual(loop.scope, AgentScope.PROJECT)

    def test_request_provider_event_carries_this_turns_tool_events_for_transcript_rendering(
        self,
    ) -> None:
        loop, _, _, _ = build_loop()
        event = loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        token = event.request.request_token
        event2 = loop.submit_provider_response(
            token, tool_calls_turn_json([("c1", "test.echo", "{}")], assistant_text="checking")
        )
        self.assertEqual(event2.kind, RunEventKind.REQUEST_PROVIDER)
        self.assertEqual(event2.text, "checking")
        kinds = [item["kind"] for item in event2.tool_events]
        self.assertEqual(kinds, ["assistant_note", "tool_result"])
        self.assertEqual(event2.tool_events[1]["tool_name"], "test.echo")
        self.assertEqual(event2.tool_events[1]["result"]["status"], AgentResultStatus.SUCCESS)

    def test_every_controller_call_receives_application_owned_approved_false(self) -> None:
        loop, controller, _, _ = build_loop()
        event = loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        token = event.request.request_token
        with patch.object(controller, "execute", wraps=controller.execute) as spy:
            raw = tool_calls_turn_json([("c1", "test.echo", "{}")])
            loop.submit_provider_response(token, raw)
            self.assertEqual(spy.call_args.kwargs.get("approved"), False)


class LimitTests(unittest.TestCase):
    def test_max_turns_limit_stops_the_run(self) -> None:
        loop, _, _, _ = build_loop(AgentRunLimits(max_turns=1))
        event = loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        token = event.request.request_token
        raw = tool_calls_turn_json([("c1", "test.echo", "{}")])
        event2 = loop.submit_provider_response(token, raw)
        self.assertEqual(event2.kind, RunEventKind.FAILED)
        self.assertEqual(event2.reason_code, "max_turns_exceeded")
        self.assertFalse(loop.is_active())

    def test_max_tool_calls_per_run_limit_stops_the_run_across_turns(self) -> None:
        loop, _, _, _ = build_loop(
            AgentRunLimits(max_tool_calls_per_run=1, max_tool_calls_per_turn=1, max_turns=5)
        )
        event = loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        token = event.request.request_token
        event2 = loop.submit_provider_response(
            token, tool_calls_turn_json([("c1", "test.echo", "{}")])
        )
        self.assertEqual(event2.kind, RunEventKind.REQUEST_PROVIDER)
        token2 = event2.request.request_token
        event3 = loop.submit_provider_response(
            token2, tool_calls_turn_json([("c2", "test.echo", "{}")])
        )
        self.assertEqual(event3.kind, RunEventKind.FAILED)
        self.assertEqual(event3.reason_code, "run_call_limit_exceeded")

    def test_generic_limit_reason_code_from_the_controller_stops_the_run(self) -> None:
        # Exercises the loop's own limit-handling branch directly (a
        # single provider turn can never itself request more calls than
        # max_tool_calls_per_turn -- the parser's own schema already caps
        # that -- so this proves the loop still reacts correctly to the
        # reason code if the controller ever reports it).
        loop, controller, _, _ = build_loop()
        event = loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        token = event.request.request_token
        denied = AgentToolResult(
            "c1", "test.echo", AgentResultStatus.DENIED, None, "denied", "turn_call_limit_exceeded"
        )
        with patch.object(controller, "execute", return_value=denied):
            event2 = loop.submit_provider_response(
                token, tool_calls_turn_json([("c1", "test.echo", "{}")])
            )
        self.assertEqual(event2.kind, RunEventKind.FAILED)
        self.assertEqual(event2.reason_code, "turn_call_limit_exceeded")


class DuplicateCallIdTests(unittest.TestCase):
    def test_a_call_id_reused_across_turns_continues_the_run(self) -> None:
        # Providers that restart their call numbering every turn are common;
        # a call id labels results within one turn only, so reuse across turns
        # must not end the run.
        loop, _, echo_handler, _ = build_loop(AgentRunLimits(max_turns=5))
        event = loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        token = event.request.request_token
        event2 = loop.submit_provider_response(
            token, tool_calls_turn_json([("c1", "test.echo", "{}")])
        )
        self.assertEqual(len(echo_handler.calls), 1)
        token2 = event2.request.request_token
        event3 = loop.submit_provider_response(
            token2, tool_calls_turn_json([("c1", "test.echo", "{}")])
        )
        self.assertNotEqual(event3.kind, RunEventKind.FAILED)
        self.assertEqual(len(echo_handler.calls), 2)

    def test_a_reused_call_id_is_disambiguated_in_the_run_trace(self) -> None:
        loop, _, _, _ = build_loop(AgentRunLimits(max_turns=5))
        event = loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        event2 = loop.submit_provider_response(
            event.request.request_token, tool_calls_turn_json([("c1", "test.echo", "{}")])
        )
        event3 = loop.submit_provider_response(
            event2.request.request_token, tool_calls_turn_json([("c1", "test.echo", "{}")])
        )
        first = [item["call_id"] for item in event2.tool_events if item["kind"] == "tool_result"]
        second = [item["call_id"] for item in event3.tool_events if item["kind"] == "tool_result"]
        self.assertEqual(first, ["c1"])
        self.assertEqual(len(second), 1)
        self.assertNotEqual(second[0], "c1")


class UnknownToolTests(unittest.TestCase):
    def test_unknown_tool_produces_a_controlled_denial_and_the_run_continues(self) -> None:
        loop, _, _, _ = build_loop()
        event = loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        token = event.request.request_token
        raw = tool_calls_turn_json([("c1", "does.not_exist", "{}")])
        event2 = loop.submit_provider_response(token, raw)
        self.assertEqual(event2.kind, RunEventKind.REQUEST_PROVIDER)
        payload = json.loads(event2.request.user_prompt)
        tool_event = payload["current_turn_events"][-1]
        self.assertEqual(tool_event["result"]["status"], AgentResultStatus.DENIED)
        self.assertEqual(tool_event["result"]["reason_code"], "unknown_tool")


class MalformedProviderOutputTests(unittest.TestCase):
    def test_malformed_provider_output_calls_no_handler(self) -> None:
        loop, _, echo_handler, _ = build_loop()
        event = loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        token = event.request.request_token
        event2 = loop.submit_provider_response(token, "```json\n{}\n```")
        self.assertEqual(event2.kind, RunEventKind.FAILED)
        self.assertEqual(event2.reason_code, "malformed_provider_turn")
        self.assertEqual(echo_handler.calls, [])


class HandlerFailureTests(unittest.TestCase):
    def test_handler_failure_is_sanitized_and_the_run_continues(self) -> None:
        raising_handler = RecordingHandler(raises=True)
        loop, _, _, _ = build_loop(echo_handler=raising_handler)
        event = loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        token = event.request.request_token
        event2 = loop.submit_provider_response(
            token, tool_calls_turn_json([("c1", "test.echo", "{}")])
        )
        self.assertEqual(event2.kind, RunEventKind.REQUEST_PROVIDER)
        payload = json.loads(event2.request.user_prompt)
        tool_event = payload["current_turn_events"][-1]
        self.assertEqual(tool_event["result"]["status"], AgentResultStatus.FAILED)
        self.assertNotIn("boom", json.dumps(tool_event["result"]))


class ApprovalRequiredTests(unittest.TestCase):
    def test_approval_required_result_stops_the_run_without_approval(self) -> None:
        loop, _, _, mutate_handler = build_loop()
        event = loop.start("please change something", AgentMode.ACT, AgentScope.PROJECT)
        token = event.request.request_token
        raw = tool_calls_turn_json([("c1", "test.mutate", "{}")])
        event2 = loop.submit_provider_response(token, raw)
        self.assertEqual(event2.kind, RunEventKind.FAILED)
        self.assertEqual(event2.reason_code, "approval_required")
        self.assertEqual(mutate_handler.calls, [])


class BusyAndCancelTests(unittest.TestCase):
    def test_new_run_while_busy_is_rejected(self) -> None:
        loop, _, _, _ = build_loop()
        loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        with self.assertRaises(RunAlreadyActiveError):
            loop.start("again", AgentMode.ASK, AgentScope.PROJECT)

    def test_cancel_is_terminal_and_late_callback_is_ignored(self) -> None:
        loop, _, echo_handler, _ = build_loop()
        event = loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        token = event.request.request_token
        cancel_event = loop.cancel()
        self.assertEqual(cancel_event.kind, RunEventKind.CANCELLED)
        self.assertFalse(loop.is_active())

        late = loop.submit_provider_response(
            token, tool_calls_turn_json([("c1", "test.echo", "{}")])
        )
        self.assertIsNone(late)
        self.assertEqual(echo_handler.calls, [])

    def test_late_provider_failure_after_cancel_is_ignored(self) -> None:
        loop, _, _, _ = build_loop()
        event = loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        token = event.request.request_token
        loop.cancel()
        self.assertIsNone(loop.submit_provider_failure(token, "network died"))

    def test_stale_token_from_an_earlier_turn_is_ignored(self) -> None:
        loop, _, _, _ = build_loop(AgentRunLimits(max_turns=5))
        event = loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        first_token = event.request.request_token
        event2 = loop.submit_provider_response(
            first_token, tool_calls_turn_json([("c1", "test.echo", "{}")])
        )
        self.assertEqual(event2.kind, RunEventKind.REQUEST_PROVIDER)
        # Replaying the first (now stale) token must be ignored, not
        # re-executed against the run's current (second) turn.
        stale_result = loop.submit_provider_response(first_token, final_turn_json("late"))
        self.assertIsNone(stale_result)
        self.assertTrue(loop.is_active())


class PromptBudgetAuthorityTests(unittest.TestCase):
    """Finding 1: a supplied PromptBudget cannot widen the controller limit."""

    def test_supplied_prompt_budget_max_prompt_chars_is_normalized_to_controller(self) -> None:
        from planx_smartmodeler.core.agent.prompt_builder import PromptBudget

        registry = AgentToolRegistry()
        controller = AgentController(registry, limits=AgentRunLimits(max_prompt_chars=100))
        wide_budget = PromptBudget(max_prompt_chars=1000, max_user_message_chars=123)
        loop = AgentRunLoop(controller, STATIC_INSTRUCTIONS, prompt_budget=wide_budget)
        # The authoritative controller value wins...
        self.assertEqual(loop.prompt_budget.max_prompt_chars, 100)
        # ...while the caller's other customized fields are preserved.
        self.assertEqual(loop.prompt_budget.max_user_message_chars, 123)

    def test_combined_prompt_never_exceeds_controller_limit(self) -> None:
        from planx_smartmodeler.core.agent.prompt_builder import PromptBudget

        loop, controller, _, _ = build_loop()
        # The controller's default max_prompt_chars (12000) is authoritative;
        # a larger (but individually valid) budget must be normalized down to
        # it rather than widening the effective prompt bound.
        self.assertGreater(controller.limits.max_prompt_chars, 0)
        wide_budget = PromptBudget(max_prompt_chars=100_000)
        self.assertGreater(wide_budget.max_prompt_chars, controller.limits.max_prompt_chars)
        loop = AgentRunLoop(controller, STATIC_INSTRUCTIONS, prompt_budget=wide_budget)
        self.assertEqual(loop.prompt_budget.max_prompt_chars, controller.limits.max_prompt_chars)
        event = loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        self.assertEqual(event.kind, RunEventKind.REQUEST_PROVIDER)
        combined = len(event.request.system_prompt) + len(event.request.user_prompt)
        self.assertLessEqual(combined, controller.limits.max_prompt_chars)


class AtomicCallBatchTests(unittest.TestCase):
    """Finding 2: a quota-invalid multi-call turn executes zero handlers."""

    def test_second_turn_over_remaining_run_quota_runs_no_handlers(self) -> None:
        loop, _, echo_handler, _ = build_loop(
            AgentRunLimits(max_tool_calls_per_run=3, max_tool_calls_per_turn=2, max_turns=5)
        )
        event = loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        token = event.request.request_token
        # Turn 1 uses two of the three allowed run calls.
        event2 = loop.submit_provider_response(
            token, tool_calls_turn_json([("c1", "test.echo", "{}"), ("c2", "test.echo", "{}")])
        )
        self.assertEqual(event2.kind, RunEventKind.REQUEST_PROVIDER)
        self.assertEqual(len(echo_handler.calls), 2)
        # Turn 2 asks for two calls, but only one run call remains -> the whole
        # batch is rejected atomically and NEITHER handler runs.
        token2 = event2.request.request_token
        event3 = loop.submit_provider_response(
            token2, tool_calls_turn_json([("c3", "test.echo", "{}"), ("c4", "test.echo", "{}")])
        )
        self.assertEqual(event3.kind, RunEventKind.FAILED)
        self.assertEqual(event3.reason_code, "run_call_limit_exceeded")
        # Still exactly the two handlers from turn 1 -- zero from the rejected turn.
        self.assertEqual(len(echo_handler.calls), 2)
        self.assertEqual([c.call_id for c in echo_handler.calls], ["c1", "c2"])


class FailureTextBoundTests(unittest.TestCase):
    """Finding 7: public failure text is bounded and sanitized."""

    def test_oversized_provider_failure_message_is_truncated(self) -> None:
        from planx_smartmodeler.core.agent.run_loop import MAX_FAILURE_TEXT_CHARS

        loop, _, _, _ = build_loop()
        event = loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        token = event.request.request_token
        huge = "x" * 250_000
        failed = loop.submit_provider_failure(token, huge)
        self.assertEqual(failed.kind, RunEventKind.FAILED)
        self.assertLessEqual(len(failed.text), MAX_FAILURE_TEXT_CHARS)

    def test_failure_text_exactly_at_bound_is_kept(self) -> None:
        from planx_smartmodeler.core.agent.run_loop import MAX_FAILURE_TEXT_CHARS

        loop, _, _, _ = build_loop()
        event = loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        token = event.request.request_token
        exact = "y" * MAX_FAILURE_TEXT_CHARS
        failed = loop.submit_provider_failure(token, exact)
        self.assertEqual(len(failed.text), MAX_FAILURE_TEXT_CHARS)


class SessionMemoryAndNewChatTests(unittest.TestCase):
    def test_session_history_is_available_to_a_follow_up_run_and_cleared_by_new_chat(self) -> None:
        loop, _, _, _ = build_loop()
        event = loop.start("first question", AgentMode.ASK, AgentScope.PROJECT)
        token = event.request.request_token
        loop.submit_provider_response(token, final_turn_json("first answer"))
        self.assertFalse(loop.is_active())

        event2 = loop.start("second question", AgentMode.ASK, AgentScope.PROJECT)
        payload = json.loads(event2.request.user_prompt)
        self.assertEqual(len(payload["session_history"]), 1)
        self.assertEqual(payload["session_history"][0]["user_text"], "first question")
        loop.submit_provider_response(
            event2.request.request_token, final_turn_json("second answer")
        )

        loop.new_chat()
        event3 = loop.start("third question", AgentMode.ASK, AgentScope.PROJECT)
        payload3 = json.loads(event3.request.user_prompt)
        self.assertEqual(payload3["session_history"], [])

    def test_new_chat_while_active_is_rejected(self) -> None:
        loop, _, _, _ = build_loop()
        loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        with self.assertRaises(RunAlreadyActiveError):
            loop.new_chat()


VALID_MODEL_PATCH_JSON = json.dumps(
    {
        "schema_version": 1,
        "context_token": "tok",
        "title": "Add report",
        "summary": "Adds a summary node",
        "operations": [{"op": "set_model_metadata", "name": "New name", "description": "d"}],
        "warnings": [],
    }
)


class RecordingValidator:
    def __init__(self, result=None) -> None:
        from planx_smartmodeler.core.agent.proposals import ProposalValidation

        self.calls = []
        self._result = result or ProposalValidation.success(
            {"kind": "model_patch", "title": "Add report", "target": "M", "summary": "s"}
        )

    def __call__(self, kind, proposal, mode, scope):
        self.calls.append((kind, mode, scope))
        return self._result


def build_proposal_loop(validator=None):
    from planx_smartmodeler.core.agent.run_loop import AgentRunLoop

    registry = AgentToolRegistry()
    registry.register(
        AgentToolSpec(
            name="test.echo",
            title="Echo",
            description="Echoes its arguments.",
            risk=AgentRisk.READ_ONLY,
            input_schema=EMPTY_SCHEMA,
            allowed_scopes=(AgentScope.CURRENT_MODEL,),
        ),
        RecordingHandler(),
    )
    controller = AgentController(registry)
    validator = validator or RecordingValidator()
    loop = AgentRunLoop(controller, STATIC_INSTRUCTIONS, proposal_validator=validator)
    return loop, validator


class ProposalRunLoopTests(unittest.TestCase):
    def _drive(self, loop, mode, scope, raw):
        event = loop.start("please propose", mode, scope)
        return loop.submit_provider_response(event.request.request_token, raw)

    def test_ask_rejects_a_proposal_before_validation(self) -> None:
        loop, validator = build_proposal_loop()
        event = self._drive(
            loop,
            AgentMode.ASK,
            AgentScope.CURRENT_MODEL,
            proposal_turn_json("model_patch", VALID_MODEL_PATCH_JSON),
        )
        self.assertEqual(event.kind, RunEventKind.FAILED)
        self.assertEqual(event.reason_code, "proposal_not_allowed_in_ask")
        self.assertEqual(validator.calls, [])

    def test_plan_accepts_a_valid_proposal_and_is_terminal(self) -> None:
        loop, validator = build_proposal_loop()
        event = self._drive(
            loop,
            AgentMode.PLAN,
            AgentScope.CURRENT_MODEL,
            proposal_turn_json("model_patch", VALID_MODEL_PATCH_JSON),
        )
        self.assertEqual(event.kind, RunEventKind.PROPOSAL)
        self.assertEqual(event.proposal["title"], "Add report")
        self.assertFalse(loop.is_active())
        self.assertEqual(len(validator.calls), 1)

    def test_act_accepts_a_valid_proposal(self) -> None:
        loop, _ = build_proposal_loop()
        event = self._drive(
            loop,
            AgentMode.ACT,
            AgentScope.CURRENT_MODEL,
            proposal_turn_json("model_patch", VALID_MODEL_PATCH_JSON),
        )
        self.assertEqual(event.kind, RunEventKind.PROPOSAL)

    def test_kind_scope_mismatch_rejects_without_validation(self) -> None:
        loop, validator = build_proposal_loop()
        event = self._drive(
            loop,
            AgentMode.PLAN,
            AgentScope.PROJECT,
            proposal_turn_json("model_patch", VALID_MODEL_PATCH_JSON),
        )
        self.assertEqual(event.kind, RunEventKind.FAILED)
        self.assertEqual(event.reason_code, "proposal_scope_mismatch")
        self.assertEqual(validator.calls, [])

    def test_validator_rejection_is_terminal_and_not_stored(self) -> None:
        from planx_smartmodeler.core.agent.proposals import ProposalValidation

        validator = RecordingValidator(
            ProposalValidation.failure("stale_proposal_context", "stale")
        )
        loop, _ = build_proposal_loop(validator)
        event = self._drive(
            loop,
            AgentMode.PLAN,
            AgentScope.CURRENT_MODEL,
            proposal_turn_json("model_patch", VALID_MODEL_PATCH_JSON),
        )
        self.assertEqual(event.kind, RunEventKind.FAILED)
        self.assertEqual(event.reason_code, "stale_proposal_context")
        self.assertTrue(loop.session_memory.is_empty())

    def test_valid_proposal_stores_only_bounded_summary(self) -> None:
        loop, _ = build_proposal_loop()
        self._drive(
            loop,
            AgentMode.PLAN,
            AgentScope.CURRENT_MODEL,
            proposal_turn_json("model_patch", VALID_MODEL_PATCH_JSON, "See my patch."),
        )
        exchanges = loop.session_memory.exchanges()
        self.assertEqual(len(exchanges), 1)
        self.assertIn("Not applied", exchanges[0].assistant_text)
        self.assertNotIn("set_model_metadata", exchanges[0].assistant_text)

    def test_no_provider_request_and_stale_callback_ignored_after_proposal(self) -> None:
        loop, _ = build_proposal_loop()
        event = loop.start("propose", AgentMode.PLAN, AgentScope.CURRENT_MODEL)
        token = event.request.request_token
        loop.submit_provider_response(
            token, proposal_turn_json("model_patch", VALID_MODEL_PATCH_JSON)
        )
        self.assertFalse(loop.is_active())
        # A late duplicate callback for the same token must be ignored.
        self.assertIsNone(
            loop.submit_provider_response(token, final_turn_json("late"))
        )

    def test_proposal_does_not_consume_tool_quota(self) -> None:
        loop, _ = build_proposal_loop()
        self._drive(
            loop,
            AgentMode.PLAN,
            AgentScope.CURRENT_MODEL,
            proposal_turn_json("model_patch", VALID_MODEL_PATCH_JSON),
        )
        self.assertEqual(loop.tool_calls_used, 0)

    def test_missing_validator_fails_closed(self) -> None:
        from planx_smartmodeler.core.agent.run_loop import AgentRunLoop

        registry = AgentToolRegistry()
        controller = AgentController(registry)
        loop = AgentRunLoop(controller, STATIC_INSTRUCTIONS)  # no validator
        event = self._drive(
            loop,
            AgentMode.PLAN,
            AgentScope.CURRENT_MODEL,
            proposal_turn_json("model_patch", VALID_MODEL_PATCH_JSON),
        )
        self.assertEqual(event.kind, RunEventKind.FAILED)

    def test_invalid_mode_at_start_fails_before_any_request(self) -> None:
        loop, validator = build_proposal_loop()
        event = loop.start("hello", "bogus_mode", AgentScope.CURRENT_MODEL)
        self.assertEqual(event.kind, RunEventKind.FAILED)
        self.assertEqual(event.reason_code, "invalid_mode")
        self.assertIsNone(event.request)
        self.assertFalse(loop.is_active())
        self.assertEqual(validator.calls, [])

    def test_invalid_scope_at_start_fails_before_any_request(self) -> None:
        loop, validator = build_proposal_loop()
        event = loop.start("hello", AgentMode.PLAN, "bogus_scope")
        self.assertEqual(event.kind, RunEventKind.FAILED)
        self.assertEqual(event.reason_code, "invalid_scope")
        self.assertIsNone(event.request)
        self.assertEqual(validator.calls, [])

    def test_invalid_mode_reaching_handle_proposal_never_calls_validator(self) -> None:
        # Defense-in-depth: even if a run's captured mode were somehow invalid,
        # _handle_proposal must fail before the validator.
        loop, validator = build_proposal_loop()
        event = loop.start("propose", AgentMode.PLAN, AgentScope.CURRENT_MODEL)
        loop._mode = "invalid_mode"  # simulate a corrupted captured mode
        result = loop.submit_provider_response(
            event.request.request_token, proposal_turn_json("model_patch", VALID_MODEL_PATCH_JSON)
        )
        self.assertEqual(result.kind, RunEventKind.FAILED)
        self.assertEqual(result.reason_code, "invalid_mode")
        self.assertEqual(validator.calls, [])

    def test_validator_exception_is_sanitized(self) -> None:
        def _raising(kind, proposal, mode, scope):
            raise RuntimeError("SENSITIVE_VALIDATOR_TRACE secret=hunter2")

        loop, _ = build_proposal_loop(_raising)
        event = self._drive(
            loop,
            AgentMode.PLAN,
            AgentScope.CURRENT_MODEL,
            proposal_turn_json("model_patch", VALID_MODEL_PATCH_JSON),
        )
        self.assertEqual(event.kind, RunEventKind.FAILED)
        self.assertEqual(event.reason_code, "proposal_validation_failed")
        self.assertNotIn("SENSITIVE_VALIDATOR_TRACE", event.text)
        self.assertNotIn("hunter2", event.text)
        self.assertNotIn("RuntimeError", event.text)


if __name__ == "__main__":
    unittest.main()
