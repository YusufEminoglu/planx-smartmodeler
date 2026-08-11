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
    MAX_NO_PROGRESS_INTERVENTIONS,
    RunAlreadyActiveError,
    RunEventKind,
)

EMPTY_SCHEMA = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
ECHO_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": [],
    "additionalProperties": False,
}
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
    echo_active: bool = False,
):
    registry = AgentToolRegistry()
    echo_handler = echo_handler or RecordingHandler()
    registry.register(
        AgentToolSpec(
            name="test.echo",
            title="Echo",
            description="Echoes its arguments.",
            risk=AgentRisk.READ_ONLY,
            input_schema=ECHO_SCHEMA,
            allowed_scopes=(AgentScope.PROJECT, AgentScope.ACTIVE_LAYER)
            if echo_active
            else (AgentScope.PROJECT,),
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
    def test_malformed_provider_response_gets_one_bounded_repair_turn(self) -> None:
        loop, _, _, _ = build_loop()
        first = loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        repaired = loop.submit_provider_response(first.request.request_token, "not json")
        self.assertEqual(repaired.kind, RunEventKind.REQUEST_PROVIDER)
        self.assertEqual(
            [event["kind"] for event in repaired.tool_events],
            ["provider_recovery"],
        )
        self.assertIn("agent_turn", repaired.request.user_prompt)
        final = loop.submit_provider_response(
            repaired.request.request_token, final_turn_json("Recovered.")
        )
        self.assertEqual(final.kind, RunEventKind.FINAL)
        self.assertEqual(final.text, "Recovered.")

    def test_malformed_provider_response_cannot_trigger_unbounded_retries(self) -> None:
        loop, _, _, _ = build_loop()
        first = loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        repaired = loop.submit_provider_response(first.request.request_token, "not json")
        failed = loop.submit_provider_response(repaired.request.request_token, "still not json")
        self.assertEqual(failed.kind, RunEventKind.FAILED)
        self.assertEqual(failed.reason_code, "malformed_provider_turn")

    def test_a_second_distinct_fault_still_gets_its_own_repair(self) -> None:
        # An owner session died here: the run spent its single repair on a
        # malformed envelope, then hit an unrelated missing context_token and
        # had nothing left, so a twelve-step request ended on a mechanical
        # error the application could have asked one question about. Two
        # different mistakes are ordinary; the same mistake twice is not.
        loop, _, _, _ = build_loop()
        first = loop.start("hi", AgentMode.ACT, AgentScope.PROJECT)
        envelope_repair = loop.submit_provider_response(
            first.request.request_token, "not json"
        )
        self.assertEqual(envelope_repair.kind, RunEventKind.REQUEST_PROVIDER)
        self.assertEqual(
            envelope_repair.tool_events[-1]["strategy"], "repair_response"
        )

        tokenless = proposal_turn_json(
            "processing_run",
            json.dumps(
                {
                    "schema_version": 1,
                    "context_token": "",
                    "algorithm_id": "native:buffer",
                    "title": "Buffer",
                    "summary": "Buffer the layer.",
                    "inputs": {"INPUT": {"layer": "L1"}},
                    "warnings": [],
                }
            ),
        )
        token_repair = loop.submit_provider_response(
            envelope_repair.request.request_token, tokenless
        )
        self.assertEqual(token_repair.kind, RunEventKind.REQUEST_PROVIDER)
        self.assertEqual(
            token_repair.tool_events[-1]["strategy"], "repair_typed_proposal"
        )

        # The same fault a second time is refused, so this cannot loop.
        failed = loop.submit_provider_response(
            token_repair.request.request_token, tokenless
        )
        self.assertEqual(failed.kind, RunEventKind.FAILED)
        self.assertEqual(failed.reason_code, "malformed_provider_turn")

    def test_repeating_one_fault_never_earns_a_second_repair(self) -> None:
        loop, _, _, _ = build_loop()
        first = loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        repaired = loop.submit_provider_response(first.request.request_token, "not json")
        self.assertEqual(repaired.kind, RunEventKind.REQUEST_PROVIDER)
        failed = loop.submit_provider_response(
            repaired.request.request_token, "still not json"
        )
        self.assertEqual(failed.kind, RunEventKind.FAILED)

    def test_transient_provider_failure_gets_one_bounded_retry(self) -> None:
        loop, _, _, _ = build_loop()
        first = loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        retried = loop.submit_provider_failure(
            first.request.request_token, "AI provider request failed (503): service unavailable"
        )
        self.assertEqual(retried.kind, RunEventKind.REQUEST_PROVIDER)
        self.assertEqual(retried.tool_events[0]["strategy"], "retry_transient_failure")
        failed = loop.submit_provider_failure(
            retried.request.request_token, "AI provider request failed (503): service unavailable"
        )
        self.assertEqual(failed.kind, RunEventKind.FAILED)
        self.assertEqual(failed.reason_code, "provider_request_failed")

    def test_empty_structured_provider_response_gets_one_bounded_retry(self) -> None:
        loop, _, _, _ = build_loop()
        first = loop.start("hi", AgentMode.ACT, AgentScope.ACTIVE_LAYER)
        retried = loop.submit_provider_failure(
            first.request.request_token,
            "AI provider returned an unreadable response: Provider response content was empty.",
        )
        self.assertEqual(retried.kind, RunEventKind.REQUEST_PROVIDER)
        self.assertEqual(retried.tool_events[0]["strategy"], "retry_transient_failure")
        final = loop.submit_provider_response(
            retried.request.request_token, final_turn_json("Recovered from empty output.")
        )
        self.assertEqual(final.kind, RunEventKind.FINAL)

    def test_active_layer_id_question_gets_one_proposal_continuation(self) -> None:
        loop, controller, _, _ = build_loop(echo_active=True)
        loop = AgentRunLoop(
            controller,
            STATIC_INSTRUCTIONS,
            active_layer_id_provider=lambda: "active-layer-42",
        )
        first = loop.start(
            "Use the active layer and reproject it.",
            AgentMode.ACT,
            AgentScope.ACTIVE_LAYER,
        )
        continued = loop.submit_provider_response(
            first.request.request_token,
            final_turn_json(
                "I need to identify the polygon layer produced by the download. "
                "Please provide the layer ID so I can bind it to the INPUT parameter."
            ),
        )
        self.assertEqual(continued.kind, RunEventKind.REQUEST_PROVIDER)
        self.assertEqual(
            continued.tool_events[0]["strategy"],
            "continue_active_layer_proposal",
        )
        finished = loop.submit_provider_response(
            continued.request.request_token,
            final_turn_json("The active layer is ready."),
        )
        self.assertEqual(finished.kind, RunEventKind.FINAL)

    def test_promised_but_unattached_proposal_gets_one_continuation(self) -> None:
        # Observed in a real session: the model finished its inspections, wrote
        # "approve the run below" and attached nothing, so no card appeared and
        # the user asked twice for an approval that could never arrive.
        loop, _, _, _ = build_loop()
        first = loop.start(
            "Filter buildings under 400 m2 into a new layer.",
            AgentMode.ACT,
            AgentScope.PROJECT,
        )
        continued = loop.submit_provider_response(
            first.request.request_token,
            final_turn_json(
                "Alan sutunu dogrulandi ve filtre algoritmasi cozuldu. 400 m2 ve "
                "daha kucuk binalari yeni katman olarak almak icin asagidaki "
                "islemi onaylayin."
            ),
        )
        self.assertEqual(continued.kind, RunEventKind.REQUEST_PROVIDER)
        self.assertEqual(
            continued.tool_events[0]["strategy"],
            "attach_the_promised_proposal",
        )

    def test_an_explained_approval_card_is_not_a_promised_proposal(self) -> None:
        # Answering "who shows the card?" is a legitimate final message and must
        # not spend a continuation turn.
        loop, _, _, _ = build_loop()
        first = loop.start("Who approves runs?", AgentMode.ACT, AgentScope.PROJECT)
        finished = loop.submit_provider_response(
            first.request.request_token,
            final_turn_json(
                "Onay karti uygulama tarafindan gosterilir; ben islemi "
                "calistiramam."
            ),
        )
        self.assertEqual(finished.kind, RunEventKind.FINAL)

    def test_promised_proposal_continuation_is_bounded(self) -> None:
        loop, _, _, _ = build_loop()
        event = loop.start("Filter them.", AgentMode.ACT, AgentScope.PROJECT)
        promise = final_turn_json("Lutfen asagidaki islemi onaylayin.")
        seen_continuations = 0
        for _ in range(6):
            event = loop.submit_provider_response(event.request.request_token, promise)
            if event.kind != RunEventKind.REQUEST_PROVIDER:
                break
            seen_continuations += 1
        self.assertEqual(event.kind, RunEventKind.FINAL)
        self.assertLessEqual(seen_continuations, 2)

    def test_structured_agent_envelope_matrix_covers_thirty_small_turns(self) -> None:
        """Exercise repeated DeepSeek-shaped envelopes without any network call."""
        for index in range(30):
            with self.subTest(index=index):
                loop, _, echo_handler, _ = build_loop(echo_active=True)
                first = loop.start(
                    f"small acceptance task {index}",
                    AgentMode.ACT,
                    AgentScope.ACTIVE_LAYER,
                )
                if index % 2:
                    next_event = loop.submit_provider_response(
                        first.request.request_token,
                        tool_calls_turn_json(
                            [(f"call-{index}", "test.echo", "{}")],
                            assistant_text="Inspecting the active layer.",
                        ),
                    )
                    self.assertEqual(next_event.kind, RunEventKind.REQUEST_PROVIDER)
                    self.assertEqual(len(echo_handler.calls), 1)
                    first = next_event
                final = loop.submit_provider_response(
                    first.request.request_token,
                    final_turn_json(f"Completed acceptance task {index}."),
                )
                self.assertEqual(final.kind, RunEventKind.FINAL)

    def test_auth_failure_is_not_retried(self) -> None:
        loop, _, _, _ = build_loop()
        first = loop.start("hi", AgentMode.ASK, AgentScope.PROJECT)
        failed = loop.submit_provider_failure(
            first.request.request_token, "AI provider request failed (401): unauthorized"
        )
        self.assertEqual(failed.kind, RunEventKind.FAILED)
        self.assertEqual(failed.reason_code, "provider_request_failed")

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
    def test_single_large_turn_requires_confirmation_before_provider_request(self) -> None:
        loop, _, _, _ = build_loop()
        with patch(
            "planx_smartmodeler.core.agent.run_loop.SINGLE_TURN_WARNING_TOKENS", 1
        ), patch(
            "planx_smartmodeler.core.agent.run_loop.TOTAL_TOKEN_WARNING_START",
            1_000_000,
        ):
            event = loop.start("hello", AgentMode.ASK, AgentScope.PROJECT)
            self.assertEqual(event.kind, RunEventKind.BUDGET_CONFIRMATION)
            self.assertIsNone(event.request)
            self.assertEqual(loop.estimated_input_tokens, 0)
            released = loop.confirm_budget()
            self.assertEqual(released.kind, RunEventKind.REQUEST_PROVIDER)
            self.assertGreater(loop.estimated_input_tokens, 0)

    def test_cumulative_warning_occurs_once_per_100k_milestone(self) -> None:
        loop, _, _, _ = build_loop(AgentRunLimits(max_turns=5))
        with patch(
            "planx_smartmodeler.core.agent.run_loop.TOTAL_TOKEN_WARNING_START", 1
        ), patch(
            "planx_smartmodeler.core.agent.run_loop.TOTAL_TOKEN_WARNING_STEP",
            100_000,
        ), patch(
            "planx_smartmodeler.core.agent.run_loop.SINGLE_TURN_WARNING_TOKENS",
            1_000_000,
        ):
            first = loop.start("hello", AgentMode.ASK, AgentScope.PROJECT)
            self.assertEqual(first.kind, RunEventKind.BUDGET_CONFIRMATION)
            released = loop.confirm_budget()
            second = loop.submit_provider_response(
                released.request.request_token,
                tool_calls_turn_json([("c1", "test.echo", "{}")]),
            )
            self.assertEqual(second.kind, RunEventKind.REQUEST_PROVIDER)
            loop._estimated_input_tokens = 100_000
            third = loop.submit_provider_response(
                second.request.request_token,
                tool_calls_turn_json([("c2", "test.echo", "{}")]),
            )
            self.assertEqual(third.kind, RunEventKind.BUDGET_CONFIRMATION)
            self.assertIn("100,001-token", third.text)

    def test_large_cumulative_total_is_never_an_absolute_block(self) -> None:
        loop, _, _, _ = build_loop(AgentRunLimits(max_turns=3))
        with patch(
            "planx_smartmodeler.core.agent.run_loop.TOTAL_TOKEN_WARNING_START",
            1_000_000_000,
        ), patch(
            "planx_smartmodeler.core.agent.run_loop.SINGLE_TURN_WARNING_TOKENS",
            1_000_000_000,
        ):
            first = loop.start("hello", AgentMode.ASK, AgentScope.PROJECT)
            loop._estimated_input_tokens = 500_000
            second = loop.submit_provider_response(
                first.request.request_token,
                tool_calls_turn_json([("c1", "test.echo", "{}")]),
            )
        self.assertEqual(second.kind, RunEventKind.REQUEST_PROVIDER)
        self.assertNotEqual(second.reason_code, "absolute_token_budget_exceeded")

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
            token2, tool_calls_turn_json([("c2", "test.mutate", "{}")])
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
        self.assertEqual(len(echo_handler.calls), 1)
        self.assertEqual(loop.tool_calls_used, 1)
        reused = [
            item
            for item in event3.tool_events
            if item["kind"] == "tool_result"
        ]
        self.assertEqual(len(reused), 1)
        self.assertTrue(reused[0]["reused"])

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


class NoProgressCircuitBreakerTests(unittest.TestCase):
    def test_reused_inspections_get_three_strategy_changes_before_stop(self) -> None:
        loop, _, echo_handler, _ = build_loop(AgentRunLimits(max_turns=8))
        first = loop.start("inspect something", AgentMode.ASK, AgentScope.PROJECT)
        current = loop.submit_provider_response(
            first.request.request_token,
            tool_calls_turn_json([("c1", "test.echo", "{}")]),
        )
        for level in range(1, MAX_NO_PROGRESS_INTERVENTIONS + 1):
            current = loop.submit_provider_response(
                current.request.request_token,
                tool_calls_turn_json(
                    [(f"repeat-{level}", "test.echo", "{}")]
                ),
            )
            self.assertEqual(current.kind, RunEventKind.REQUEST_PROVIDER)
            intervention = current.tool_events[-1]
            self.assertEqual(intervention["kind"], "strategy_intervention")
            self.assertEqual(intervention["level"], level)
            payload = json.loads(current.request.user_prompt)
            self.assertEqual(
                payload["current_turn_events"][-1]["strategy"],
                intervention["strategy"],
            )

        stopped = loop.submit_provider_response(
            current.request.request_token,
            tool_calls_turn_json([("repeat-stop", "test.echo", "{}")]),
        )
        self.assertEqual(stopped.kind, RunEventKind.FAILED)
        self.assertEqual(
            stopped.reason_code, "repeated_inspections_no_progress"
        )
        self.assertEqual(len(echo_handler.calls), 1)
        self.assertLess(loop.turns_used, loop._run_state.limits.max_turns)

    def test_provider_can_finish_after_a_strategy_intervention(self) -> None:
        loop, _, echo_handler, _ = build_loop(AgentRunLimits(max_turns=6))
        first = loop.start("inspect something", AgentMode.ASK, AgentScope.PROJECT)
        second = loop.submit_provider_response(
            first.request.request_token,
            tool_calls_turn_json([("c1", "test.echo", "{}")]),
        )
        recovery = loop.submit_provider_response(
            second.request.request_token,
            tool_calls_turn_json([("c2", "test.echo", "{}")]),
        )
        finished = loop.submit_provider_response(
            recovery.request.request_token,
            final_turn_json("Completed from the cached inspection."),
        )
        self.assertEqual(finished.kind, RunEventKind.FINAL)
        self.assertEqual(len(echo_handler.calls), 1)

    def test_materially_new_arguments_reset_no_progress_recovery(self) -> None:
        loop, _, echo_handler, _ = build_loop(AgentRunLimits(max_turns=8))
        first = loop.start("inspect two things", AgentMode.ASK, AgentScope.PROJECT)
        second = loop.submit_provider_response(
            first.request.request_token,
            tool_calls_turn_json(
                [("a1", "test.echo", '{"query":"first"}')]
            ),
        )
        first_recovery = loop.submit_provider_response(
            second.request.request_token,
            tool_calls_turn_json(
                [("a2", "test.echo", '{"query":"first"}')]
            ),
        )
        self.assertEqual(
            first_recovery.tool_events[-1]["level"], 1
        )
        new_evidence = loop.submit_provider_response(
            first_recovery.request.request_token,
            tool_calls_turn_json(
                [("b1", "test.echo", '{"query":"second"}')]
            ),
        )
        self.assertEqual(len(echo_handler.calls), 2)
        self.assertNotIn(
            "strategy_intervention",
            [item["kind"] for item in new_evidence.tool_events],
        )
        reset_recovery = loop.submit_provider_response(
            new_evidence.request.request_token,
            tool_calls_turn_json(
                [("b2", "test.echo", '{"query":"second"}')]
            ),
        )
        self.assertEqual(
            reset_recovery.tool_events[-1]["level"], 1
        )


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
        self.assertEqual(event2.kind, RunEventKind.REQUEST_PROVIDER)
        event3 = loop.submit_provider_response(event2.request.request_token, "still malformed")
        self.assertEqual(event3.kind, RunEventKind.FAILED)
        self.assertEqual(event3.reason_code, "malformed_provider_turn")
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
            token2,
            tool_calls_turn_json(
                [
                    ("c3", "test.mutate", "{}"),
                    ("c4", "does.not_exist", "{}"),
                ]
            ),
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


class AttributeFilterValidator:
    def __init__(self) -> None:
        from planx_smartmodeler.core.agent.proposals import ProposalValidation

        self.proposals = []
        self._result = ProposalValidation.success(
            {
                "kind": "processing_run",
                "title": "Filter active layer by attribute",
                "target": "Built intensity",
                "summary": "Create a temporary filtered layer.",
            }
        )

    def __call__(self, kind, proposal, mode, scope):
        self.proposals.append((kind, proposal, mode, scope))
        return self._result


def _tool_schema(properties=None, required=()):
    return {
        "type": "object",
        "properties": properties or {},
        "required": list(required),
        "additionalProperties": False,
    }


def build_attribute_filter_loop(
    *,
    include_field=True,
    ambiguous_field=False,
    power_enabled=False,
    fields_truncated=False,
):
    registry = AgentToolRegistry()
    calls = []

    def _register(name, schema, result):
        def _handler(call):
            calls.append((call.tool_name, dict(call.arguments)))
            return result(call) if callable(result) else result

        registry.register(
            AgentToolSpec(
                name=name,
                title=name,
                description=name,
                risk=AgentRisk.READ_ONLY,
                input_schema=schema,
                allowed_scopes=(AgentScope.PROJECT, AgentScope.ACTIVE_LAYER),
            ),
            _handler,
        )

    _register(
        "layer.list",
        _tool_schema(
            {"limit": {"type": "integer", "minimum": 1, "maximum": 100}}
        ),
        {
            "layers": [
                {
                    "layer_id": "active-layer",
                    "name": "Audit - DOLDURULACAK",
                    "kind": "vector",
                    "active": True,
                }
            ],
            "count": 1,
            "truncated": False,
        },
    )
    _register(
        "processing.resolve",
        _tool_schema(
            {"algorithm_id": {"type": "string", "minLength": 1}},
            ("algorithm_id",),
        ),
        {
            "resolved": {
                "available": True,
                "algorithm_id": "native:extractbyattribute",
                "agent_runnable": True,
                "context_token": "trusted-filter-token",
            },
            "algorithms": [],
            "truncated": False,
        },
    )
    fields = (
        [
            {"name": "built_intensity_bin", "field_type": "string"},
            {"name": "lcz_weak_confidence", "field_type": "double"},
        ]
        if include_field
        else [{"name": "other", "field_type": "string"}]
    )
    if ambiguous_field:
        fields.append(
            {"name": "built_intensitiy_bim", "field_type": "string"}
        )
    _register(
        "layer.describe",
        _tool_schema(
            {
                "layer_id": {"type": "string", "minLength": 1},
                "field_name": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            ("layer_id",),
        ),
        lambda call: {
            "available": True,
            "layer_id": "active-layer",
            "fields": (
                [
                    item
                    for item in fields
                    if item["name"] == call.arguments["field_name"]
                ]
                if call.arguments.get("field_name")
                else (
                    [{"name": "other", "field_type": "string"}]
                    if fields_truncated
                    else fields
                )
            ),
            "fields_truncated": (
                False
                if call.arguments.get("field_name")
                else fields_truncated
            ),
        },
    )
    validator = AttributeFilterValidator()
    loop = AgentRunLoop(
        AgentController(registry),
        STATIC_INSTRUCTIONS,
        proposal_validator=validator,
        power_enabled_provider=lambda: power_enabled,
    )
    return loop, validator, calls


class DeterministicAttributeFilterTests(unittest.TestCase):
    REQUEST = (
        "Audit - DOLDURULACAK bu katmanda built_intensitiy_bin isimli "
        'sütun değeri "low" lan geometrileri filtreleyip yeni bir katman '
        "olarak kaydet"
    )
    ACTIVE_LAYER_WORD_ORDER = (
        'aktif katmanda built_intensity_bin sütununda değeri "low" '
        "olanları yeni katman olarak ver bana"
    )
    NUMERIC_LESS_THAN_REQUEST = (
        "lcz_weak_confidence değeri 0.6 değerinin altına olanları seçip "
        "farklı bir katman olarak kaydet"
    )

    def test_exact_filter_is_prepared_without_a_provider_turn(self) -> None:
        loop, validator, calls = build_attribute_filter_loop()
        event = loop.start(self.REQUEST, AgentMode.ACT, AgentScope.PROJECT)
        self.assertEqual(event.kind, RunEventKind.PROPOSAL)
        self.assertIsNone(event.request)
        self.assertEqual(
            [name for name, _arguments in calls],
            [
                "layer.list",
                "processing.resolve",
                "layer.describe",
                "layer.describe",
            ],
        )
        self.assertEqual(loop.turns_used, 1)
        self.assertEqual(loop.tool_calls_used, 4)
        self.assertEqual(len(validator.proposals), 1)
        kind, proposal, mode, scope = validator.proposals[0]
        self.assertEqual(kind, "processing_run")
        self.assertEqual(mode, AgentMode.ACT)
        self.assertEqual(scope, AgentScope.PROJECT)
        bindings = dict(proposal.inputs)
        self.assertEqual(bindings["INPUT"].value, "active-layer")
        self.assertEqual(bindings["FIELD"].value, "built_intensity_bin")
        self.assertEqual(bindings["VALUE"].value, "low")
        self.assertEqual(bindings["OPERATOR"].value, 0)
        self.assertEqual(len(proposal.warnings), 1)
        self.assertIn("built_intensitiy_bin", proposal.warnings[0])
        self.assertIn("built_intensity_bin", proposal.warnings[0])

    def test_previous_field_before_column_word_order_remains_supported(self) -> None:
        loop, validator, _calls = build_attribute_filter_loop()
        event = loop.start(
            self.ACTIVE_LAYER_WORD_ORDER, AgentMode.ACT, AgentScope.PROJECT
        )
        self.assertEqual(event.kind, RunEventKind.PROPOSAL)
        self.assertEqual(len(validator.proposals), 1)

    def test_numeric_less_than_filter_uses_active_layer_without_provider(self) -> None:
        loop, validator, calls = build_attribute_filter_loop()
        event = loop.start(
            self.NUMERIC_LESS_THAN_REQUEST,
            AgentMode.ACT,
            AgentScope.PROJECT,
        )
        self.assertEqual(event.kind, RunEventKind.PROPOSAL)
        self.assertIsNone(event.request)
        self.assertEqual(len(validator.proposals), 1)
        bindings = dict(validator.proposals[0][1].inputs)
        self.assertEqual(bindings["INPUT"].value, "active-layer")
        self.assertEqual(bindings["FIELD"].value, "lcz_weak_confidence")
        self.assertEqual(bindings["OPERATOR"].value, 4)
        self.assertEqual(bindings["VALUE"].value, "0.6")
        self.assertEqual(
            [name for name, _arguments in calls],
            ["layer.list", "processing.resolve", "layer.describe"],
        )

    def test_symbolic_numeric_comparison_is_supported(self) -> None:
        for request in (
            "lcz_weak_confidence değeri < 0.6 olanları farklı bir katman "
            "olarak kaydet",
            "filter lcz_weak_confidence value below 0.6 as a new layer",
        ):
            loop, validator, _calls = build_attribute_filter_loop()
            event = loop.start(
                request,
                AgentMode.ACT,
                AgentScope.PROJECT,
            )
            self.assertEqual(event.kind, RunEventKind.PROPOSAL)
            bindings = dict(validator.proposals[0][1].inputs)
            self.assertEqual(bindings["OPERATOR"].value, 4)
            self.assertEqual(bindings["VALUE"].value, "0.6")

    def test_truncated_field_preview_defers_exact_name_to_live_validator(self) -> None:
        loop, validator, _calls = build_attribute_filter_loop(
            fields_truncated=True,
        )
        event = loop.start(
            self.NUMERIC_LESS_THAN_REQUEST,
            AgentMode.ACT,
            AgentScope.PROJECT,
        )
        self.assertEqual(event.kind, RunEventKind.PROPOSAL)
        proposal = validator.proposals[0][1]
        bindings = dict(proposal.inputs)
        self.assertEqual(bindings["FIELD"].value, "lcz_weak_confidence")
        self.assertEqual(proposal.warnings, ())

    def test_retry_skips_an_intervening_diagnostic_exchange(self) -> None:
        for retry_text in ("tekrar yap", "tekrar dene"):
            loop, validator, _calls = build_attribute_filter_loop()
            loop.session_memory.append(
                self.REQUEST,
                "[Attempt did not complete: invalid call id]",
            )
            loop.session_memory.append(
                "neden yapamıyorsun sorgula",
                "The previous attempt reached its turn limit.",
            )
            for index in range(4):
                loop.session_memory.append(
                    f"diagnostic follow-up {index}",
                    "The operation still did not complete.",
                )
            event = loop.start(
                retry_text, AgentMode.ACT, AgentScope.ACTIVE_LAYER
            )
            self.assertEqual(event.kind, RunEventKind.PROPOSAL)
            self.assertEqual(len(validator.proposals), 1)
            self.assertEqual(
                validator.proposals[0][3], AgentScope.ACTIVE_LAYER
            )

    def test_missing_named_field_fails_before_validation(self) -> None:
        loop, validator, _calls = build_attribute_filter_loop(include_field=False)
        event = loop.start(self.REQUEST, AgentMode.ACT, AgentScope.PROJECT)
        self.assertEqual(event.kind, RunEventKind.FAILED)
        self.assertEqual(event.reason_code, "attribute_filter_field_missing")
        self.assertIn("built_intensitiy_bin", event.text)
        self.assertEqual(validator.proposals, [])

    def test_ambiguous_one_edit_field_correction_is_rejected(self) -> None:
        loop, validator, _calls = build_attribute_filter_loop(
            ambiguous_field=True
        )
        event = loop.start(self.REQUEST, AgentMode.ACT, AgentScope.PROJECT)
        self.assertEqual(event.kind, RunEventKind.FAILED)
        self.assertEqual(event.reason_code, "attribute_filter_field_missing")
        self.assertIn("no unique one-edit correction", event.text)
        self.assertEqual(validator.proposals, [])

    def test_ask_mode_does_not_run_the_local_proposal_path(self) -> None:
        loop, validator, calls = build_attribute_filter_loop()
        event = loop.start(self.REQUEST, AgentMode.ASK, AgentScope.PROJECT)
        self.assertEqual(event.kind, RunEventKind.FINAL)
        self.assertIn("Act (approve to apply)", event.text)
        self.assertIn("Power Mode", event.text)
        self.assertEqual(calls, [])
        self.assertEqual(validator.proposals, [])

    def test_processing_filter_does_not_depend_on_power_mode(self) -> None:
        for enabled in (False, True):
            loop, validator, _calls = build_attribute_filter_loop(
                power_enabled=enabled
            )
            event = loop.start(
                self.REQUEST, AgentMode.ACT, AgentScope.PROJECT
            )
            self.assertEqual(event.kind, RunEventKind.PROPOSAL)
            self.assertEqual(len(validator.proposals), 1)


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

    def test_validator_rejection_is_terminal_and_leaves_only_a_bounded_note(self) -> None:
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
        # A failed attempt is now remembered so a follow-up "why?" has context,
        # but only as a bounded note -- never the raw proposal payload.
        exchanges = loop.session_memory.exchanges()
        self.assertEqual(len(exchanges), 1)
        stored = exchanges[0].assistant_text
        self.assertIn("did not complete", stored)
        self.assertNotIn("set_model_metadata", stored)
        self.assertNotIn("context_token", stored)

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


class SequencedValidator:
    """Fail the first proposal with ``message``, accept every later one."""

    def __init__(self, reason_code: str, message: str) -> None:
        from planx_smartmodeler.core.agent.proposals import ProposalValidation

        self.calls = []
        self._reason_code = reason_code
        self._message = message
        self._success = ProposalValidation.success(
            {"kind": "model_patch", "title": "Add report", "target": "M", "summary": "s"}
        )

    def __call__(self, kind, proposal, mode, scope):
        from planx_smartmodeler.core.agent.proposals import ProposalValidation

        self.calls.append((kind, mode, scope))
        if len(self.calls) == 1:
            return ProposalValidation.failure(self._reason_code, self._message)
        return self._success


def build_model_patch_loop(validator):
    """A Workflow Studio loop that also has the ``model.describe`` inspection."""
    from planx_smartmodeler.core.agent.run_loop import AgentRunLoop

    registry = AgentToolRegistry()
    describe_handler = RecordingHandler({"available": True, "context_token": "fresh"})
    registry.register(
        AgentToolSpec(
            name="model.describe",
            title="Describe model",
            description="Reports the live graph and its freshness receipt.",
            risk=AgentRisk.READ_ONLY,
            input_schema=EMPTY_SCHEMA,
            allowed_scopes=(AgentScope.CURRENT_MODEL,),
        ),
        describe_handler,
    )
    controller = AgentController(registry)
    loop = AgentRunLoop(controller, STATIC_INSTRUCTIONS, proposal_validator=validator)
    return loop, describe_handler


class WorkflowPatchRecoveryTests(unittest.TestCase):
    """A complex workflow makes one mechanical mistake and must survive it.

    Every case here ended a real session with the user retyping the whole
    request, because the run failed instead of spending one bounded repair
    turn on evidence it could get for free.
    """

    def _drive(self, loop, raw):
        event = loop.start("build the workflow", AgentMode.PLAN, AgentScope.CURRENT_MODEL)
        return loop.submit_provider_response(event.request.request_token, raw)

    def test_guessed_algorithm_ids_get_one_repair_turn_naming_them(self) -> None:
        validator = SequencedValidator(
            "proposal_validation_failed",
            "Unavailable algorithm: native:rastercalculator, native:distance.",
        )
        loop, _describe = build_model_patch_loop(validator)
        repaired = self._drive(
            loop, proposal_turn_json("model_patch", VALID_MODEL_PATCH_JSON)
        )
        self.assertEqual(repaired.kind, RunEventKind.REQUEST_PROVIDER)
        recovery = [
            event for event in repaired.tool_events if event["kind"] == "provider_recovery"
        ]
        self.assertEqual(len(recovery), 1)
        instruction = recovery[0]["instruction"]
        self.assertIn("native:rastercalculator", instruction)
        self.assertIn("processing.resolve", instruction)
        # The Processing wording must not leak into a patch repair: a workflow
        # patch has no bindings, no destinations and no project layers.
        self.assertNotIn("proposal_binding", instruction)
        final = loop.submit_provider_response(
            repaired.request.request_token,
            proposal_turn_json("model_patch", VALID_MODEL_PATCH_JSON),
        )
        self.assertEqual(final.kind, RunEventKind.PROPOSAL)
        self.assertEqual(len(validator.calls), 2)

    def test_a_restricted_algorithm_still_fails_closed(self) -> None:
        from planx_smartmodeler.core.agent.proposals import ProposalValidation

        validator = RecordingValidator(
            ProposalValidation.failure(
                "proposal_validation_failed", "Restricted algorithm: native:shellcommand."
            )
        )
        loop, _describe = build_model_patch_loop(validator)
        event = self._drive(
            loop, proposal_turn_json("model_patch", VALID_MODEL_PATCH_JSON)
        )
        self.assertEqual(event.kind, RunEventKind.FAILED)
        self.assertEqual(len(validator.calls), 1)

    def test_two_unrelated_validation_mistakes_each_get_their_own_repair(self) -> None:
        # Keying the fault on the reason code alone spent the whole allowance
        # on the first refusal, so a workflow that fixed a stale receipt and
        # then mistyped a parameter died on the second, unrelated mistake.
        from planx_smartmodeler.core.agent.proposals import ProposalValidation

        class TwoFaults:
            def __init__(self) -> None:
                self.calls = []

            def __call__(self, kind, proposal, mode, scope):
                self.calls.append(kind)
                if len(self.calls) == 1:
                    return ProposalValidation.failure(
                        "proposal_validation_failed",
                        "Unavailable algorithm: native:rastercalculator.",
                    )
                if len(self.calls) == 2:
                    return ProposalValidation.failure(
                        "proposal_validation_failed",
                        "A text parameter value is required.",
                    )
                return ProposalValidation.success(
                    {"kind": "model_patch", "title": "T", "target": "M", "summary": "s"}
                )

        validator = TwoFaults()
        loop, _describe = build_model_patch_loop(validator)
        event = self._drive(loop, proposal_turn_json("model_patch", VALID_MODEL_PATCH_JSON))
        for _ in range(2):
            self.assertEqual(event.kind, RunEventKind.REQUEST_PROVIDER)
            event = loop.submit_provider_response(
                event.request.request_token,
                proposal_turn_json("model_patch", VALID_MODEL_PATCH_JSON),
            )
        self.assertEqual(event.kind, RunEventKind.PROPOSAL)
        self.assertEqual(len(validator.calls), 3)

    def test_a_mistyped_parameter_value_gets_one_repair_turn(self) -> None:
        validator = SequencedValidator(
            "proposal_validation_failed", "A text parameter value is required."
        )
        loop, _describe = build_model_patch_loop(validator)
        repaired = self._drive(
            loop, proposal_turn_json("model_patch", VALID_MODEL_PATCH_JSON)
        )
        self.assertEqual(repaired.kind, RunEventKind.REQUEST_PROVIDER)

    def test_a_path_shaped_parameter_value_still_fails_closed(self) -> None:
        from planx_smartmodeler.core.agent.proposals import ProposalValidation

        validator = RecordingValidator(
            ProposalValidation.failure(
                "proposal_validation_failed",
                "Path, URI, connection, or credential values are not permitted.",
            )
        )
        loop, _describe = build_model_patch_loop(validator)
        event = self._drive(
            loop, proposal_turn_json("model_patch", VALID_MODEL_PATCH_JSON)
        )
        self.assertEqual(event.kind, RunEventKind.FAILED)

    def test_a_stale_graph_receipt_is_reinspected_instead_of_ending_the_run(self) -> None:
        validator = SequencedValidator(
            "stale_proposal_context",
            "The model changed since this proposal was prepared. Inspect it again.",
        )
        loop, describe_handler = build_model_patch_loop(validator)
        repaired = self._drive(
            loop, proposal_turn_json("model_patch", VALID_MODEL_PATCH_JSON)
        )
        self.assertEqual(repaired.kind, RunEventKind.REQUEST_PROVIDER)
        # The graph itself is re-read, so the repair turn writes the patch
        # against the node ids and receipt that exist now.
        self.assertEqual(len(describe_handler.calls), 1)
        self.assertIn(
            "model.describe",
            [event.get("tool_name") for event in repaired.tool_events],
        )
        final = loop.submit_provider_response(
            repaired.request.request_token,
            proposal_turn_json("model_patch", VALID_MODEL_PATCH_JSON),
        )
        self.assertEqual(final.kind, RunEventKind.PROPOSAL)

    def test_more_tool_calls_than_the_turn_allows_still_run_the_first_ones(
        self,
    ) -> None:
        # An oversized batch used to execute nothing and cost a repair turn; a
        # second oversized batch then ended the run outright. Now the turn does
        # its allowed work and the provider is told what was dropped.
        echo = RecordingHandler()
        loop, _controller, echo_handler, _mutate = build_loop(
            AgentRunLimits(max_tool_calls_per_run=8, max_tool_calls_per_turn=2),
            echo_handler=echo,
        )
        first = loop.start("plan a big workflow", AgentMode.ASK, AgentScope.PROJECT)
        event = loop.submit_provider_response(
            first.request.request_token,
            tool_calls_turn_json(
                [
                    ("c1", "test.echo", "{}"),
                    ("c2", "test.echo", '{"query":"a"}'),
                    ("c3", "test.echo", '{"query":"b"}'),
                ]
            ),
        )
        self.assertEqual(event.kind, RunEventKind.REQUEST_PROVIDER)
        self.assertEqual(len(echo_handler.calls), 2)
        notice = [
            item for item in event.tool_events if item["kind"] == "provider_notice"
        ]
        self.assertEqual(len(notice), 1)
        self.assertEqual(notice[0]["strategy"], "tool_calls_truncated")
        self.assertIn("2-call", notice[0]["instruction"])
        # Truncation is not a repair, so the run's bounded repair budget is
        # untouched and a later oversized batch is handled the same way.
        again = loop.submit_provider_response(
            event.request.request_token,
            tool_calls_turn_json(
                [
                    ("c4", "test.echo", '{"query":"c"}'),
                    ("c5", "test.echo", '{"query":"d"}'),
                    ("c6", "test.echo", '{"query":"e"}'),
                ]
            ),
        )
        self.assertEqual(again.kind, RunEventKind.REQUEST_PROVIDER)
        self.assertEqual(len(echo_handler.calls), 4)
        final = loop.submit_provider_response(
            again.request.request_token, final_turn_json("Recovered.")
        )
        self.assertEqual(final.kind, RunEventKind.FINAL)

    def test_a_tagged_parameter_value_is_repaired_with_the_patch_shape_named(
        self,
    ) -> None:
        # Live DeepSeek wrote {"expression":"$area"} -- the processing_run
        # binding envelope -- into a complete four-node workflow patch, and the
        # whole run died on the strict parser.
        patch = json.dumps(
            {
                "schema_version": 1,
                "context_token": "tok",
                "title": "Compute area",
                "summary": "Add a field calculator node.",
                "operations": [
                    {
                        "op": "add_node",
                        "node_id": "calc1",
                        "algorithm_id": "native:fieldcalculator",
                        "title": "Compute area",
                        "parameters": [{"name": "FORMULA", "value": {"expression": "$area"}}],
                    }
                ],
                "warnings": [],
            }
        )
        validator = RecordingValidator()
        loop, _describe = build_model_patch_loop(validator)
        repaired = self._drive(loop, proposal_turn_json("model_patch", patch))
        self.assertEqual(repaired.kind, RunEventKind.REQUEST_PROVIDER)
        recovery = [
            event for event in repaired.tool_events if event["kind"] == "provider_recovery"
        ]
        self.assertEqual(len(recovery), 1)
        self.assertIn("expression", recovery[0]["instruction"])
        self.assertEqual(validator.calls, [])
        final = loop.submit_provider_response(
            repaired.request.request_token,
            proposal_turn_json("model_patch", VALID_MODEL_PATCH_JSON),
        )
        self.assertEqual(final.kind, RunEventKind.PROPOSAL)

    def test_resolved_algorithms_survive_the_trimmed_working_trace(self) -> None:
        # A live run at turn eleven reported algorithms it had resolved at turn
        # two as "not resolved in this session" -- the trace holding them had
        # been trimmed to fit the prompt budget. The digest is tiny and last.
        loop, _describe = build_model_patch_loop(RecordingValidator())
        event = loop.start("build it", AgentMode.PLAN, AgentScope.CURRENT_MODEL)
        self.assertNotIn("run_facts", event.request.user_prompt)
        loop._proposal_receipts[("processing_run", "native:slope")] = "tok1"
        loop._proposal_receipts[("processing_run", "native:aspect")] = "tok2"
        nudged = loop.submit_provider_response(
            event.request.request_token,
            tool_calls_turn_json([("c1", "model.describe", "{}")]),
        )
        self.assertEqual(nudged.kind, RunEventKind.REQUEST_PROVIDER)
        self.assertIn("run_facts", nudged.request.user_prompt)
        self.assertIn("native:slope", nudged.request.user_prompt)
        self.assertIn("native:aspect", nudged.request.user_prompt)
        # Receipts themselves are never handed to the provider by the digest.
        self.assertNotIn("tok1", nudged.request.user_prompt)

    def test_live_node_ids_reach_the_provider_so_it_cannot_invent_one(self) -> None:
        # With the topology trimmed out of the trace, a provider writes the
        # shape of an id -- "<existing_node_id>" -- and the whole patch is
        # rejected. These come from the trusted model.describe result.
        registry = AgentToolRegistry()
        registry.register(
            AgentToolSpec(
                name="model.describe",
                title="Describe model",
                description="Reports the live graph and its freshness receipt.",
                risk=AgentRisk.READ_ONLY,
                input_schema=EMPTY_SCHEMA,
                allowed_scopes=(AgentScope.CURRENT_MODEL,),
            ),
            RecordingHandler(
                {
                    "available": True,
                    "context_token": "fresh",
                    "nodes": [{"node_id": "src"}, {"node_id": "buf"}],
                }
            ),
        )
        from planx_smartmodeler.core.agent.run_loop import AgentRunLoop

        loop = AgentRunLoop(
            AgentController(registry),
            STATIC_INSTRUCTIONS,
            proposal_validator=RecordingValidator(),
        )
        event = loop.start("replace it", AgentMode.PLAN, AgentScope.CURRENT_MODEL)
        nudged = loop.submit_provider_response(
            event.request.request_token,
            tool_calls_turn_json([("c1", "model.describe", "{}")]),
        )
        self.assertEqual(nudged.kind, RunEventKind.REQUEST_PROVIDER)
        self.assertIn("open_workflow_node_ids", nudged.request.user_prompt)
        self.assertIn("buf", nudged.request.user_prompt)
        # The graph's own receipt travels with them: providers kept echoing a
        # different token and a complete patch was refused for a copy error.
        self.assertIn("workflow_context_token", nudged.request.user_prompt)
        self.assertIn("fresh", nudged.request.user_prompt)

    def test_no_workflow_receipt_is_offered_before_the_graph_is_inspected(self) -> None:
        loop, _describe = build_model_patch_loop(RecordingValidator())
        event = loop.start("build it", AgentMode.PLAN, AgentScope.CURRENT_MODEL)
        self.assertNotIn("workflow_context_token", event.request.user_prompt)

    def test_the_same_stall_earns_a_second_push_only_after_real_progress(self) -> None:
        # A live workflow stalled, was pushed into acting, resolved four more
        # algorithms, then stalled again -- and had nothing left, ending with
        # no result and two thirds of its budget unused.
        stall = final_turn_json(
            "I need to resolve the remaining algorithms before proposing the patch."
        )
        loop, describe_handler = build_model_patch_loop(RecordingValidator())
        event = self._drive(loop, stall)
        self.assertEqual(event.kind, RunEventKind.REQUEST_PROVIDER)
        # No provider work in between: the same stall must not buy another push.
        repeated = loop.submit_provider_response(event.request.request_token, stall)
        self.assertEqual(repeated.kind, RunEventKind.FINAL)

        loop2, _describe2 = build_model_patch_loop(RecordingValidator())
        event = self._drive(loop2, stall)
        self.assertEqual(event.kind, RunEventKind.REQUEST_PROVIDER)
        worked = loop2.submit_provider_response(
            event.request.request_token,
            tool_calls_turn_json([("c1", "model.describe", "{}")]),
        )
        self.assertEqual(worked.kind, RunEventKind.REQUEST_PROVIDER)
        pushed_again = loop2.submit_provider_response(
            worked.request.request_token, stall
        )
        self.assertEqual(pushed_again.kind, RunEventKind.REQUEST_PROVIDER)

    def test_a_patch_labelled_none_needs_no_repair_turn_at_all(self) -> None:
        # A complete workflow patch under "proposal_kind":"none" cost a repair
        # turn and then the run, twice in live sessions. `operations` belongs
        # to no other kind, so the label is read off the payload -- and the
        # patch still crosses the same parser, receipt and approval boundaries.
        validator = RecordingValidator()
        loop, _describe = build_model_patch_loop(validator)
        raw = json.dumps(
            {
                "action": "proposal",
                "assistant_text": "Here is the workflow.",
                "tool_calls": [],
                "proposal_kind": "none",
                "proposal_json": VALID_MODEL_PATCH_JSON,
            }
        )
        event = self._drive(loop, raw)
        self.assertEqual(event.kind, RunEventKind.PROPOSAL)
        self.assertEqual([call[0] for call in validator.calls], ["model_patch"])

    def test_the_names_a_provider_reaches_for_still_mean_model_patch(self) -> None:
        for label in ("workflow", "model", "patch", "graph_patch"):
            validator = RecordingValidator()
            loop, _describe = build_model_patch_loop(validator)
            event = self._drive(
                loop, proposal_turn_json(label, VALID_MODEL_PATCH_JSON)
            )
            self.assertEqual(event.kind, RunEventKind.PROPOSAL, label)
            self.assertEqual([call[0] for call in validator.calls], ["model_patch"])

    def test_a_missing_proposal_kind_is_repaired_with_this_scope_s_kinds(self) -> None:
        # The fixed advice was "for this Processing request use processing_run",
        # which Current model scope rejects outright -- a live workflow run
        # spent its repair on impossible instructions and then died.
        loop, _describe = build_model_patch_loop(RecordingValidator())
        raw = json.dumps(
            {
                "action": "proposal",
                "assistant_text": "Here is the workflow.",
                "tool_calls": [],
                "proposal_kind": "none",
                "proposal_json": json.dumps(
                    {"schema_version": 1, "context_token": "tok", "title": "T"}
                ),
            }
        )
        repaired = self._drive(loop, raw)
        self.assertEqual(repaired.kind, RunEventKind.REQUEST_PROVIDER)
        instruction = [
            event["instruction"]
            for event in repaired.tool_events
            if event["kind"] == "provider_recovery"
        ][0]
        self.assertIn("model_patch", instruction)
        self.assertNotIn("processing_run", instruction)

    def test_a_final_turn_that_only_announces_the_next_step_is_pushed_to_act(
        self,
    ) -> None:
        loop, _describe = build_model_patch_loop(RecordingValidator())
        repaired = self._drive(
            loop,
            final_turn_json(
                "I need to resolve the algorithms for dissolve, multipart to "
                "singleparts and field calculator before proposing the workflow patch."
            ),
        )
        self.assertEqual(repaired.kind, RunEventKind.REQUEST_PROVIDER)
        self.assertEqual(
            [
                event["strategy"]
                for event in repaired.tool_events
                if event["kind"] == "provider_recovery"
            ],
            ["do_the_work_you_announced"],
        )

    def test_a_final_turn_claiming_completion_without_a_proposal_is_pushed(self) -> None:
        # "The request is complete." after five turns of inspection, with no
        # proposal ever attached: nothing was built and nothing is waiting.
        loop, _describe = build_model_patch_loop(RecordingValidator())
        repaired = self._drive(loop, final_turn_json("The request is complete."))
        self.assertEqual(repaired.kind, RunEventKind.REQUEST_PROVIDER)
        self.assertIn(
            "attach_the_promised_proposal",
            [
                event.get("strategy")
                for event in repaired.tool_events
                if event["kind"] == "provider_recovery"
            ],
        )

    def test_a_final_turn_that_asks_the_user_something_still_ends_the_run(self) -> None:
        loop, _describe = build_model_patch_loop(RecordingValidator())
        event = self._drive(
            loop,
            final_turn_json(
                "Which DEM should the workflow use, and do you want the slope in "
                "degrees or percent?"
            ),
        )
        self.assertEqual(event.kind, RunEventKind.FINAL)

    def test_a_proposal_turn_with_an_empty_payload_is_repaired(self) -> None:
        validator = RecordingValidator()
        loop, _describe = build_model_patch_loop(validator)
        repaired = self._drive(loop, proposal_turn_json("model_patch", ""))
        self.assertEqual(repaired.kind, RunEventKind.REQUEST_PROVIDER)
        recovery = [
            event for event in repaired.tool_events if event["kind"] == "provider_recovery"
        ]
        self.assertEqual(len(recovery), 1)
        self.assertIn("proposal_json", recovery[0]["instruction"])
        final = loop.submit_provider_response(
            repaired.request.request_token,
            proposal_turn_json("model_patch", VALID_MODEL_PATCH_JSON),
        )
        self.assertEqual(final.kind, RunEventKind.PROPOSAL)


if __name__ == "__main__":
    unittest.main()
