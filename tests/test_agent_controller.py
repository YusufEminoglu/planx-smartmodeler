from __future__ import annotations

import unittest

from planx_smartmodeler.core.agent.contracts import (
    AgentMode,
    AgentResultStatus,
    AgentRisk,
    AgentRunLimits,
    AgentScope,
    AgentToolCall,
    AgentToolSpec,
)
from planx_smartmodeler.core.agent.controller import AgentController, AgentRunState
from planx_smartmodeler.core.agent.registry import AgentToolRegistry

EMPTY_OBJECT_SCHEMA = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
LAYER_ID_SCHEMA = {
    "type": "object",
    "properties": {"layer_id": {"type": "string", "minLength": 1, "maxLength": 128}},
    "required": ["layer_id"],
    "additionalProperties": False,
}


def build_registry(
    risk: str = AgentRisk.READ_ONLY, handler=None, input_schema=None
) -> AgentToolRegistry:
    registry = AgentToolRegistry()
    registry.register(
        AgentToolSpec(
            name="test.tool",
            title="Test tool",
            description="A tool used only in tests.",
            risk=risk,
            input_schema=input_schema if input_schema is not None else dict(EMPTY_OBJECT_SCHEMA),
            allowed_scopes=(AgentScope.PROJECT,),
        ),
        handler or (lambda call: {"ok": True}),
    )
    return registry


class ControllerBasicTests(unittest.TestCase):
    def test_unknown_tool_is_denied_without_calling_a_handler(self) -> None:
        calls = []

        def handler(call):
            calls.append(call)
            return {}

        registry = build_registry(handler=handler)
        controller = AgentController(registry)
        call = AgentToolCall(call_id="c1", tool_name="does.not_exist")
        result = controller.execute(call, AgentMode.ASK, AgentScope.PROJECT)
        self.assertEqual(result.status, AgentResultStatus.DENIED)
        self.assertEqual(result.reason_code, "unknown_tool")
        self.assertEqual(calls, [])

    def test_read_only_tool_succeeds_in_every_mode(self) -> None:
        registry = build_registry()
        controller = AgentController(registry)
        for mode in AgentMode.ALL:
            call = AgentToolCall(call_id=f"c-{mode}", tool_name="test.tool")
            result = controller.execute(call, mode, AgentScope.PROJECT)
            self.assertEqual(result.status, AgentResultStatus.SUCCESS)
            self.assertEqual(result.data, {"ok": True})

    def test_denied_tool_never_invokes_the_handler(self) -> None:
        calls = []

        def handler(call):
            calls.append(call)
            return {}

        registry = build_registry(risk=AgentRisk.PROHIBITED, handler=handler)
        controller = AgentController(registry)
        call = AgentToolCall(call_id="c1", tool_name="test.tool")
        result = controller.execute(call, AgentMode.ACT, AgentScope.PROJECT, approved=True)
        self.assertEqual(result.status, AgentResultStatus.DENIED)
        self.assertEqual(calls, [])

    def test_approval_required_never_invokes_the_handler_without_approval(self) -> None:
        calls = []

        def handler(call):
            calls.append(call)
            return {}

        registry = build_registry(risk=AgentRisk.MUTATING, handler=handler)
        controller = AgentController(registry)
        call = AgentToolCall(call_id="c1", tool_name="test.tool")
        result = controller.execute(call, AgentMode.ACT, AgentScope.PROJECT)
        self.assertEqual(result.status, AgentResultStatus.APPROVAL_REQUIRED)
        self.assertEqual(calls, [])

    def test_scope_outside_allowed_scopes_never_invokes_the_handler(self) -> None:
        calls = []

        def handler(call):
            calls.append(call)
            return {}

        registry = build_registry(handler=handler)
        controller = AgentController(registry)
        call = AgentToolCall(call_id="c1", tool_name="test.tool")
        result = controller.execute(call, AgentMode.ASK, AgentScope.PLUGINS)
        self.assertEqual(result.status, AgentResultStatus.DENIED)
        self.assertEqual(result.reason_code, "scope_not_allowed")
        self.assertEqual(calls, [])

    def test_handler_exception_becomes_sanitized_failure(self) -> None:
        def handler(call):
            raise RuntimeError("credential=super-secret traceback detail")

        registry = build_registry(handler=handler)
        controller = AgentController(registry)
        call = AgentToolCall(call_id="c1", tool_name="test.tool")
        result = controller.execute(call, AgentMode.ASK, AgentScope.PROJECT)
        self.assertEqual(result.status, AgentResultStatus.FAILED)
        self.assertNotIn("super-secret", result.message)
        self.assertNotIn("Traceback", result.message)

    def test_handler_returning_unsupported_data_becomes_failure(self) -> None:
        class NotJson:
            pass

        registry = build_registry(handler=lambda call: NotJson())
        controller = AgentController(registry)
        call = AgentToolCall(call_id="c1", tool_name="test.tool")
        result = controller.execute(call, AgentMode.ASK, AgentScope.PROJECT)
        self.assertEqual(result.status, AgentResultStatus.FAILED)
        self.assertEqual(result.reason_code, "invalid_result")


class ArgumentSchemaGatingTests(unittest.TestCase):
    def test_missing_required_argument_fails_without_invoking_handler(self) -> None:
        calls = []

        def handler(call):
            calls.append(call)
            return {}

        registry = build_registry(handler=handler, input_schema=LAYER_ID_SCHEMA)
        controller = AgentController(registry)
        call = AgentToolCall(call_id="c1", tool_name="test.tool")
        result = controller.execute(call, AgentMode.ASK, AgentScope.PROJECT)
        self.assertEqual(result.status, AgentResultStatus.FAILED)
        self.assertEqual(result.reason_code, "invalid_arguments")
        self.assertEqual(calls, [])

    def test_unknown_argument_fails_without_invoking_handler(self) -> None:
        calls = []

        def handler(call):
            calls.append(call)
            return {}

        registry = build_registry(handler=handler, input_schema=LAYER_ID_SCHEMA)
        controller = AgentController(registry)
        call = AgentToolCall(
            call_id="c1",
            tool_name="test.tool",
            arguments={"layer_id": "l1", "unexpected": 1},
        )
        result = controller.execute(call, AgentMode.ASK, AgentScope.PROJECT)
        self.assertEqual(result.status, AgentResultStatus.FAILED)
        self.assertEqual(result.reason_code, "invalid_arguments")
        self.assertEqual(calls, [])

    def test_wrong_type_argument_fails_without_invoking_handler(self) -> None:
        calls = []

        def handler(call):
            calls.append(call)
            return {}

        registry = build_registry(handler=handler, input_schema=LAYER_ID_SCHEMA)
        controller = AgentController(registry)
        call = AgentToolCall(call_id="c1", tool_name="test.tool", arguments={"layer_id": 123})
        result = controller.execute(call, AgentMode.ASK, AgentScope.PROJECT)
        self.assertEqual(result.status, AgentResultStatus.FAILED)
        self.assertEqual(result.reason_code, "invalid_arguments")
        self.assertEqual(calls, [])

    def test_valid_arguments_reach_the_handler(self) -> None:
        registry = build_registry(
            handler=lambda call: {"layer_id": call.arguments["layer_id"]},
            input_schema=LAYER_ID_SCHEMA,
        )
        controller = AgentController(registry)
        call = AgentToolCall(call_id="c1", tool_name="test.tool", arguments={"layer_id": "l1"})
        result = controller.execute(call, AgentMode.ASK, AgentScope.PROJECT)
        self.assertEqual(result.status, AgentResultStatus.SUCCESS)
        self.assertEqual(result.data, {"layer_id": "l1"})


class ApprovalTypeConfusionTests(unittest.TestCase):
    def test_string_false_does_not_authorize(self) -> None:
        calls = []
        registry = build_registry(
            risk=AgentRisk.MUTATING, handler=lambda call: calls.append(call) or {}
        )
        controller = AgentController(registry)
        result = controller.execute(
            AgentToolCall(call_id="c1", tool_name="test.tool"),
            AgentMode.ACT,
            AgentScope.PROJECT,
            approved="false",
        )
        self.assertNotEqual(result.status, AgentResultStatus.SUCCESS)
        self.assertEqual(result.reason_code, "invalid_approval_value")
        self.assertEqual(calls, [])

    def test_string_true_does_not_authorize(self) -> None:
        calls = []
        registry = build_registry(
            risk=AgentRisk.MUTATING, handler=lambda call: calls.append(call) or {}
        )
        controller = AgentController(registry)
        result = controller.execute(
            AgentToolCall(call_id="c1", tool_name="test.tool"),
            AgentMode.ACT,
            AgentScope.PROJECT,
            approved="true",
        )
        self.assertNotEqual(result.status, AgentResultStatus.SUCCESS)
        self.assertEqual(result.reason_code, "invalid_approval_value")
        self.assertEqual(calls, [])

    def test_integer_one_does_not_authorize(self) -> None:
        registry = build_registry(risk=AgentRisk.MUTATING)
        controller = AgentController(registry)
        result = controller.execute(
            AgentToolCall(call_id="c1", tool_name="test.tool"),
            AgentMode.ACT,
            AgentScope.PROJECT,
            approved=1,
        )
        self.assertNotEqual(result.status, AgentResultStatus.SUCCESS)
        self.assertEqual(result.reason_code, "invalid_approval_value")

    def test_integer_zero_does_not_authorize(self) -> None:
        registry = build_registry(risk=AgentRisk.MUTATING)
        controller = AgentController(registry)
        result = controller.execute(
            AgentToolCall(call_id="c1", tool_name="test.tool"),
            AgentMode.ACT,
            AgentScope.PROJECT,
            approved=0,
        )
        self.assertNotEqual(result.status, AgentResultStatus.SUCCESS)
        self.assertEqual(result.reason_code, "invalid_approval_value")

    def test_none_does_not_authorize(self) -> None:
        registry = build_registry(risk=AgentRisk.MUTATING)
        controller = AgentController(registry)
        result = controller.execute(
            AgentToolCall(call_id="c1", tool_name="test.tool"),
            AgentMode.ACT,
            AgentScope.PROJECT,
            approved=None,
        )
        self.assertNotEqual(result.status, AgentResultStatus.SUCCESS)
        self.assertEqual(result.reason_code, "invalid_approval_value")

    def test_container_does_not_authorize(self) -> None:
        registry = build_registry(risk=AgentRisk.MUTATING)
        controller = AgentController(registry)
        result = controller.execute(
            AgentToolCall(call_id="c1", tool_name="test.tool"),
            AgentMode.ACT,
            AgentScope.PROJECT,
            approved=[True],
        )
        self.assertNotEqual(result.status, AgentResultStatus.SUCCESS)
        self.assertEqual(result.reason_code, "invalid_approval_value")

    def test_invalid_approval_never_invokes_the_handler(self) -> None:
        calls = []
        registry = build_registry(
            risk=AgentRisk.MUTATING, handler=lambda call: calls.append(call) or {}
        )
        controller = AgentController(registry)
        for index, bad_value in enumerate(("false", "true", 1, 0, None, [True])):
            controller.execute(
                AgentToolCall(call_id=f"c-bad-{index}", tool_name="test.tool"),
                AgentMode.ACT,
                AgentScope.PROJECT,
                approved=bad_value,
            )
        self.assertEqual(calls, [])

    def test_approval_cannot_override_prohibited_through_the_controller(self) -> None:
        registry = build_registry(risk=AgentRisk.PROHIBITED)
        controller = AgentController(registry)
        for mode in AgentMode.ALL:
            result = controller.execute(
                AgentToolCall(call_id=f"c-{mode}", tool_name="test.tool"),
                mode,
                AgentScope.PROJECT,
                approved=True,
            )
            self.assertEqual(result.status, AgentResultStatus.DENIED)

    def test_approval_cannot_override_deny_through_the_controller(self) -> None:
        registry = build_registry(risk=AgentRisk.MUTATING)
        controller = AgentController(registry)
        result = controller.execute(
            AgentToolCall(call_id="c1", tool_name="test.tool"),
            AgentMode.ASK,
            AgentScope.PROJECT,
            approved=True,
        )
        self.assertEqual(result.status, AgentResultStatus.DENIED)

    def test_approval_cannot_override_preview_only_through_the_controller(self) -> None:
        registry = build_registry(risk=AgentRisk.MUTATING)
        controller = AgentController(registry)
        result = controller.execute(
            AgentToolCall(call_id="c1", tool_name="test.tool"),
            AgentMode.PLAN,
            AgentScope.PROJECT,
            approved=True,
        )
        self.assertEqual(result.status, AgentResultStatus.DENIED)

    def test_real_boolean_true_authorizes(self) -> None:
        registry = build_registry(risk=AgentRisk.MUTATING)
        controller = AgentController(registry)
        result = controller.execute(
            AgentToolCall(call_id="c1", tool_name="test.tool"),
            AgentMode.ACT,
            AgentScope.PROJECT,
            approved=True,
        )
        self.assertEqual(result.status, AgentResultStatus.SUCCESS)

    def test_real_boolean_false_requires_approval(self) -> None:
        registry = build_registry(risk=AgentRisk.MUTATING)
        controller = AgentController(registry)
        result = controller.execute(
            AgentToolCall(call_id="c1", tool_name="test.tool"),
            AgentMode.ACT,
            AgentScope.PROJECT,
            approved=False,
        )
        self.assertEqual(result.status, AgentResultStatus.APPROVAL_REQUIRED)


class RunLimitTests(unittest.TestCase):
    def test_new_run_state_is_bound_to_controller_limits(self) -> None:
        registry = build_registry()
        limits = AgentRunLimits(max_tool_calls_per_run=1, max_tool_calls_per_turn=1)
        controller = AgentController(registry, limits)
        run_state = controller.new_run_state()
        self.assertEqual(run_state.limits, limits)

    def test_run_state_denies_after_per_run_limit(self) -> None:
        registry = build_registry()
        limits = AgentRunLimits(max_tool_calls_per_run=1, max_tool_calls_per_turn=1)
        controller = AgentController(registry, limits)
        run_state = controller.new_run_state()
        run_state.start_turn()
        first = controller.execute(
            AgentToolCall(call_id="c1", tool_name="test.tool"),
            AgentMode.ASK,
            AgentScope.PROJECT,
            run_state=run_state,
        )
        second = controller.execute(
            AgentToolCall(call_id="c2", tool_name="test.tool"),
            AgentMode.ASK,
            AgentScope.PROJECT,
            run_state=run_state,
        )
        self.assertEqual(first.status, AgentResultStatus.SUCCESS)
        self.assertEqual(second.status, AgentResultStatus.DENIED)
        self.assertEqual(second.reason_code, "run_call_limit_exceeded")

    def test_run_state_denies_after_per_turn_limit(self) -> None:
        registry = build_registry()
        limits = AgentRunLimits(max_tool_calls_per_run=10, max_tool_calls_per_turn=1)
        controller = AgentController(registry, limits)
        run_state = controller.new_run_state()
        run_state.start_turn()
        first = controller.execute(
            AgentToolCall(call_id="c1", tool_name="test.tool"),
            AgentMode.ASK,
            AgentScope.PROJECT,
            run_state=run_state,
        )
        second = controller.execute(
            AgentToolCall(call_id="c2", tool_name="test.tool"),
            AgentMode.ASK,
            AgentScope.PROJECT,
            run_state=run_state,
        )
        self.assertEqual(first.status, AgentResultStatus.SUCCESS)
        self.assertEqual(second.status, AgentResultStatus.DENIED)
        self.assertEqual(second.reason_code, "turn_call_limit_exceeded")

    def test_tool_call_before_start_turn_is_denied(self) -> None:
        registry = build_registry()
        limits = AgentRunLimits()
        controller = AgentController(registry, limits)
        run_state = controller.new_run_state()
        result = controller.execute(
            AgentToolCall(call_id="c1", tool_name="test.tool"),
            AgentMode.ASK,
            AgentScope.PROJECT,
            run_state=run_state,
        )
        self.assertEqual(result.status, AgentResultStatus.DENIED)
        self.assertEqual(result.reason_code, "no_active_turn")

    def test_mismatched_run_state_is_denied_without_invoking_the_handler(self) -> None:
        calls = []
        registry = build_registry(handler=lambda call: calls.append(call) or {"ok": True})
        strict_limits = AgentRunLimits(max_tool_calls_per_run=1, max_tool_calls_per_turn=1)
        controller = AgentController(registry, strict_limits)

        # A foreign run state built with much larger (more permissive) limits.
        permissive_limits = AgentRunLimits(
            max_tool_calls_per_run=500, max_tool_calls_per_turn=50
        )
        foreign_state = AgentRunState(permissive_limits)
        foreign_state.start_turn()

        result = controller.execute(
            AgentToolCall(call_id="c1", tool_name="test.tool"),
            AgentMode.ASK,
            AgentScope.PROJECT,
            run_state=foreign_state,
        )
        self.assertEqual(result.status, AgentResultStatus.DENIED)
        self.assertEqual(result.reason_code, "run_state_mismatch")
        self.assertEqual(calls, [])

    def test_run_state_cannot_bypass_controller_limits_across_many_calls(self) -> None:
        # Even if a caller tries to reuse a permissive foreign state across
        # many calls, the controller must reject every one of them rather
        # than allow the foreign state's larger budget to apply.
        registry = build_registry()
        strict_limits = AgentRunLimits(max_tool_calls_per_run=1, max_tool_calls_per_turn=1)
        controller = AgentController(registry, strict_limits)
        permissive_limits = AgentRunLimits(
            max_tool_calls_per_run=500, max_tool_calls_per_turn=50
        )
        foreign_state = AgentRunState(permissive_limits)
        foreign_state.start_turn()
        for index in range(5):
            result = controller.execute(
                AgentToolCall(call_id=f"c{index}", tool_name="test.tool"),
                AgentMode.ASK,
                AgentScope.PROJECT,
                run_state=foreign_state,
            )
            self.assertEqual(result.status, AgentResultStatus.DENIED)
            self.assertEqual(result.reason_code, "run_state_mismatch")


class ResultBoundingTests(unittest.TestCase):
    def test_controller_limit_bounds_result_strings(self) -> None:
        registry = build_registry(handler=lambda call: {"text": "x" * 50})
        limits = AgentRunLimits(max_result_text_chars=10)
        controller = AgentController(registry, limits)
        result = controller.execute(
            AgentToolCall(call_id="c1", tool_name="test.tool"),
            AgentMode.ASK,
            AgentScope.PROJECT,
        )
        self.assertEqual(result.status, AgentResultStatus.FAILED)
        self.assertEqual(result.reason_code, "invalid_result")

    def test_controller_limit_allows_strings_within_bound(self) -> None:
        registry = build_registry(handler=lambda call: {"text": "x" * 10})
        limits = AgentRunLimits(max_result_text_chars=10)
        controller = AgentController(registry, limits)
        result = controller.execute(
            AgentToolCall(call_id="c1", tool_name="test.tool"),
            AgentMode.ASK,
            AgentScope.PROJECT,
        )
        self.assertEqual(result.status, AgentResultStatus.SUCCESS)


class CheckCapacityTests(unittest.TestCase):
    """Finding 2: the non-mutating batch preflight on AgentRunState."""

    def _fresh_state(self, **limit_kwargs) -> AgentRunState:
        state = AgentRunState(AgentRunLimits(**limit_kwargs))
        state.start_turn()
        return state

    def test_check_capacity_does_not_mutate_counters(self) -> None:
        state = self._fresh_state(max_tool_calls_per_run=5, max_tool_calls_per_turn=3)
        state.check_capacity(2)
        self.assertEqual(state.tool_calls_this_run, 0)
        self.assertEqual(state.tool_calls_this_turn, 0)

    def test_check_capacity_rejects_a_batch_over_remaining_run_quota(self) -> None:
        from planx_smartmodeler.core.agent.controller import RunLimitExceededError

        state = self._fresh_state(
            max_tool_calls_per_run=3, max_tool_calls_per_turn=3, max_turns=5
        )
        state.note_tool_call()
        state.note_tool_call()  # two used, one remains
        with self.assertRaises(RunLimitExceededError) as ctx:
            state.check_capacity(2)
        self.assertEqual(ctx.exception.reason_code, "run_call_limit_exceeded")

    def test_check_capacity_rejects_a_batch_over_the_turn_quota(self) -> None:
        from planx_smartmodeler.core.agent.controller import RunLimitExceededError

        state = self._fresh_state(max_tool_calls_per_run=10, max_tool_calls_per_turn=2)
        with self.assertRaises(RunLimitExceededError) as ctx:
            state.check_capacity(3)
        self.assertEqual(ctx.exception.reason_code, "turn_call_limit_exceeded")

    def test_check_capacity_requires_an_active_turn(self) -> None:
        from planx_smartmodeler.core.agent.controller import RunLimitExceededError

        state = AgentRunState(AgentRunLimits())  # no start_turn()
        with self.assertRaises(RunLimitExceededError) as ctx:
            state.check_capacity(1)
        self.assertEqual(ctx.exception.reason_code, "no_active_turn")


if __name__ == "__main__":
    unittest.main()
