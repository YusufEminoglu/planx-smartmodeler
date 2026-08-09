"""Regression tests for authority-neutral provider proposal recovery."""
from __future__ import annotations

import json
import unittest

from planx_smartmodeler.core.agent.contracts import (
    AgentMode,
    AgentRisk,
    AgentRunLimits,
    AgentScope,
    AgentToolSpec,
)
from planx_smartmodeler.core.agent.controller import AgentController
from planx_smartmodeler.core.agent.proposal_recovery import recover_agent_turn
from planx_smartmodeler.core.agent.proposals import (
    PROPOSAL_KIND_LAYER_STYLE,
    PROPOSAL_KIND_PROCESSING_RUN,
    ProposalValidation,
)
from planx_smartmodeler.core.agent.registry import AgentToolRegistry
from planx_smartmodeler.core.agent.run_loop import AgentRunLoop, RunEventKind


def _turn(kind: str, proposal: dict) -> str:
    return json.dumps(
        {
            "action": "proposal",
            "assistant_text": "Ready.",
            "tool_calls": [],
            "proposal_kind": kind,
            "proposal_json": json.dumps(proposal),
        }
    )


def _processing_proposal(*, token=None) -> dict:
    proposal = {
        "schema_version": 1,
        "algorithm_id": "native:randomextract",
        "title": "Random three",
        "summary": "Create a temporary three-feature layer.",
        "inputs": {
            "INPUT": {"layer": "layer_15"},
            "METHOD": {"enum": 0},
            "NUMBER": {"number": 3},
        },
        "warnings": [],
    }
    if token is not None:
        proposal["context_token"] = token
    return proposal


def _style_proposal(*, token=None, count=99, colors=5) -> dict:
    proposal = {
        "schema_version": 1,
        "target_layer_id": "roads",
        "title": "Style roads",
        "summary": "Use a clear road classification.",
        "renderer": {
            "family": "categorized",
            "field": "highway",
            "class_count": count,
            "palette": [
                "#1B9E77",
                "#D95F02",
                "#7570B3",
                "#E7298A",
                "#66A61E",
                "#E6AB02",
                "#A6761D",
                "#666666",
                "#1F78B4",
                "#B2DF8A",
                "#FB9A99",
                "#CAB2D6",
                "#FFFF99",
            ][:colors],
            "opacity": 0.9,
        },
    }
    if token is not None:
        proposal["context_token"] = token
    return proposal


class PureRecoveryTests(unittest.TestCase):
    def test_missing_processing_receipt_requests_exact_describe(self) -> None:
        outcome = recover_agent_turn(
            _turn(PROPOSAL_KIND_PROCESSING_RUN, _processing_proposal()),
            4,
            {},
        )
        self.assertIsNone(outcome.turn)
        self.assertEqual(outcome.inspection.tool_name, "processing.describe")
        self.assertEqual(
            dict(outcome.inspection.arguments),
            {"algorithm_id": "native:randomextract"},
        )

    def test_cached_receipt_is_injected_without_changing_inputs(self) -> None:
        proposal = _processing_proposal()
        outcome = recover_agent_turn(
            _turn(PROPOSAL_KIND_PROCESSING_RUN, proposal),
            4,
            {(PROPOSAL_KIND_PROCESSING_RUN, "native:randomextract"): "trusted"},
        )
        self.assertIsNotNone(outcome.turn)
        recovered = outcome.turn.proposal
        self.assertEqual(recovered.context_token, "trusted")
        self.assertEqual(dict(recovered.inputs)["NUMBER"].value, 3)
        self.assertEqual(recovered.algorithm_id, "native:randomextract")

    def test_blank_proposal_note_is_filled_without_changing_authority(self) -> None:
        proposal = _processing_proposal(token="trusted")
        raw = json.loads(_turn(PROPOSAL_KIND_PROCESSING_RUN, proposal))
        raw["assistant_text"] = "   "
        outcome = recover_agent_turn(json.dumps(raw), 4, {})
        self.assertIsNotNone(outcome.turn)
        self.assertEqual(
            outcome.turn.assistant_text,
            "A validated proposal is ready.",
        )
        recovered = outcome.turn.proposal
        self.assertEqual(recovered.algorithm_id, "native:randomextract")
        self.assertEqual(dict(recovered.inputs)["NUMBER"].value, 3)

    def test_final_label_with_complete_proposal_is_promoted_safely(self) -> None:
        raw = json.loads(
            _turn(
                PROPOSAL_KIND_PROCESSING_RUN,
                _processing_proposal(token="trusted"),
            )
        )
        raw["action"] = "final"
        outcome = recover_agent_turn(json.dumps(raw), 4, {})
        self.assertIsNotNone(outcome.turn)
        self.assertTrue(outcome.turn.is_proposal)
        self.assertEqual(outcome.turn.proposal.algorithm_id, "native:randomextract")

    def test_legacy_processing_parameters_become_typed_inputs_without_output(self) -> None:
        legacy = {
            "action": "proposal",
            "assistant_text": "Run ready.",
            "tool_calls": [],
            "proposal_kind": "run",
            "proposal_json": json.dumps(
                {
                    "algorithm_id": "native:buffer",
                    "parameters": {
                        "INPUT": "layer_1",
                        "DISTANCE": 5,
                        "SEGMENTS": 5,
                        "DISSOLVE": False,
                        "OUTPUT": "C:/should-never-be-used.gpkg",
                    },
                    "temporary_output": True,
                }
            ),
        }
        raw = json.dumps(legacy)
        outcome = recover_agent_turn(raw, 4, {})
        self.assertIsNone(outcome.turn)
        self.assertEqual(outcome.inspection.tool_name, "processing.describe")
        recovered = recover_agent_turn(
            raw,
            4,
            {("processing_run", "native:buffer"): "trusted"},
        )
        self.assertIsNotNone(recovered.turn)
        inputs = dict(recovered.turn.proposal.inputs)
        self.assertEqual(inputs["INPUT"].tag, "layer")
        self.assertEqual(inputs["DISTANCE"].tag, "distance")
        self.assertNotIn("OUTPUT", inputs)

    def test_legacy_processing_decorations_alias_and_enum_label_are_recovered(self) -> None:
        legacy = {
            "action": "proposal",
            "assistant_text": "Run ready.",
            "tool_calls": [],
            "proposal_kind": "run_processing",
            "proposal_json": json.dumps(
                {
                    "algorithm_id": "native:extractbyattribute",
                    "parameters": {
                        "INPUT": "layer_1",
                        "FIELD": "category",
                        "OPERATOR": "equals",
                        "VALUE": "low",
                    },
                    "temporary_output": True,
                    "reviewed": True,
                    "kind": "processing_run",
                }
            ),
        }
        raw = json.dumps(legacy)
        outcome = recover_agent_turn(
            raw,
            4,
            {("processing_run", "native:extractbyattribute"): "trusted"},
        )
        self.assertIsNotNone(outcome.turn)
        inputs = dict(outcome.turn.proposal.inputs)
        self.assertEqual(inputs["FIELD"].tag, "field")
        self.assertEqual(inputs["OPERATOR"].tag, "enum_string")
        self.assertNotIn("reviewed", outcome.turn.proposal.to_dict())

    def test_terminal_proposal_with_trailing_json_is_recovered_without_extra_authority(self) -> None:
        proposal = _processing_proposal(token="trusted")
        raw = _turn(PROPOSAL_KIND_PROCESSING_RUN, proposal)
        trailing = raw + json.dumps(
            {
                "action": "tool_calls",
                "tool_calls": [{"tool_name": "script.run"}],
            }
        )
        outcome = recover_agent_turn(trailing, 4, {})
        self.assertIsNotNone(outcome.turn)
        self.assertEqual(outcome.turn.proposal.algorithm_id, "native:randomextract")
        self.assertEqual(outcome.turn.tool_calls, ())

    def test_placeholder_context_token_requests_trusted_reinspection(self) -> None:
        proposal = _processing_proposal(token="context_token")
        outcome = recover_agent_turn(
            _turn(PROPOSAL_KIND_PROCESSING_RUN, proposal),
            4,
            {},
        )
        self.assertIsNone(outcome.turn)
        self.assertEqual(outcome.inspection.tool_name, "processing.describe")

    def test_non_string_proposal_note_is_not_repaired(self) -> None:
        raw = json.loads(
            _turn(
                PROPOSAL_KIND_PROCESSING_RUN,
                _processing_proposal(token="trusted"),
            )
        )
        raw["assistant_text"] = 7
        outcome = recover_agent_turn(json.dumps(raw), 4, {})
        self.assertIsNone(outcome.turn)

    def test_style_count_tracks_palette_and_safe_defaults_are_filled(self) -> None:
        outcome = recover_agent_turn(
            _turn(
                PROPOSAL_KIND_LAYER_STYLE,
                _style_proposal(token="trusted", count=99, colors=5),
            ),
            4,
            {},
        )
        self.assertIsNotNone(outcome.turn)
        proposal = outcome.turn.proposal
        self.assertEqual(proposal.renderer.class_count, 5)
        self.assertEqual(len(proposal.renderer.palette), 5)
        self.assertFalse(proposal.labels.enabled)
        self.assertEqual(proposal.warnings, ())

    def test_oversized_style_palette_is_bounded_to_twelve(self) -> None:
        outcome = recover_agent_turn(
            _turn(
                PROPOSAL_KIND_LAYER_STYLE,
                _style_proposal(token="trusted", count=100, colors=13),
            ),
            4,
            {},
        )
        self.assertIsNotNone(outcome.turn)
        self.assertEqual(outcome.turn.proposal.renderer.class_count, 12)
        self.assertEqual(len(outcome.turn.proposal.renderer.palette), 12)

    def test_non_proposal_and_duplicate_json_are_not_repaired(self) -> None:
        final = json.dumps(
            {
                "action": "final",
                "assistant_text": "Done.",
                "tool_calls": [],
                "proposal_kind": "none",
                "proposal_json": "",
            }
        )
        self.assertIsNone(recover_agent_turn(final, 4, {}).turn)
        duplicate = (
            '{"action":"proposal","action":"proposal","assistant_text":"x",'
            '"tool_calls":[],"proposal_kind":"processing_run",'
            '"proposal_json":"{}"}'
        )
        outcome = recover_agent_turn(duplicate, 4, {})
        self.assertIsNone(outcome.turn)
        self.assertIsNone(outcome.inspection)


    def test_explicit_typed_binding_alias_is_normalized(self) -> None:
        proposal = _processing_proposal(token="trusted")
        proposal["inputs"]["NUMBER"] = {"type": "number", "value": 3}
        outcome = recover_agent_turn(
            _turn(PROPOSAL_KIND_PROCESSING_RUN, proposal),
            4,
            {},
        )
        self.assertIsNotNone(outcome.turn)
        self.assertEqual(dict(outcome.turn.proposal.inputs)["NUMBER"].value, 3)

    def test_blank_layer_extent_requests_layer_list(self) -> None:
        proposal = _processing_proposal(token="trusted")
        proposal["algorithm_id"] = "zero2agentosm:download_advanced"
        proposal["inputs"] = {"EXTENT": {"layer_extent": ""}}
        outcome = recover_agent_turn(
            _turn(PROPOSAL_KIND_PROCESSING_RUN, proposal),
            4,
            {},
        )
        self.assertIsNone(outcome.turn)
        self.assertEqual(outcome.inspection.tool_name, "layer.list")


class RunLoopRecoveryTests(unittest.TestCase):
    @staticmethod
    def _registry(calls):
        registry = AgentToolRegistry()

        def describe(call):
            calls.append(call)
            return {
                "available": True,
                "algorithm_id": call.arguments["algorithm_id"],
                "context_token": "trusted",
            }

        registry.register(
            AgentToolSpec(
                name="processing.describe",
                title="Describe processing algorithm",
                description="Returns one algorithm signature.",
                risk=AgentRisk.READ_ONLY,
                input_schema={
                    "type": "object",
                    "properties": {
                        "algorithm_id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 200,
                        }
                    },
                    "required": ["algorithm_id"],
                    "additionalProperties": False,
                },
                allowed_scopes=(AgentScope.PROJECT,),
            ),
            describe,
        )
        return registry

    def test_missing_token_auto_describes_once_and_finishes_proposal(self) -> None:
        calls = []
        registry = self._registry(calls)

        def validate(kind, proposal, mode, scope):
            self.assertEqual(kind, PROPOSAL_KIND_PROCESSING_RUN)
            self.assertEqual(proposal.context_token, "trusted")
            self.assertEqual(mode, AgentMode.ACT)
            self.assertEqual(scope, AgentScope.PROJECT)
            return ProposalValidation.success(
                {"kind": kind, "title": proposal.title, "applied": False}
            )

        loop = AgentRunLoop(
            AgentController(registry),
            "instructions",
            proposal_validator=validate,
        )
        started = loop.start("Randomly extract three.", AgentMode.ACT, AgentScope.PROJECT)
        event = loop.submit_provider_response(
            started.request.request_token,
            _turn(PROPOSAL_KIND_PROCESSING_RUN, _processing_proposal()),
        )
        self.assertEqual(event.kind, RunEventKind.PROPOSAL)
        self.assertEqual(len(calls), 1)
        self.assertEqual(loop.tool_calls_used, 1)
        self.assertEqual(len(event.tool_events), 1)
        self.assertEqual(event.tool_events[0]["tool_name"], "processing.describe")

    def test_cached_receipt_avoids_a_repeated_inspection(self) -> None:
        calls = []
        registry = self._registry(calls)
        loop = AgentRunLoop(
            AgentController(registry),
            "instructions",
            proposal_validator=lambda kind, proposal, mode, scope: ProposalValidation.success(
                {"kind": kind, "title": proposal.title, "applied": False}
            ),
        )
        started = loop.start("Randomly extract three.", AgentMode.ACT, AgentScope.PROJECT)
        inspected = json.dumps(
            {
                "action": "tool_calls",
                "assistant_text": "Inspecting the exact algorithm.",
                "tool_calls": [
                    {
                        "call_id": "describe_once",
                        "tool_name": "processing.describe",
                        "arguments_json": json.dumps(
                            {"algorithm_id": "native:randomextract"}
                        ),
                    }
                ],
                "proposal_kind": "none",
                "proposal_json": "",
            }
        )
        next_turn = loop.submit_provider_response(
            started.request.request_token,
            inspected,
        )
        event = loop.submit_provider_response(
            next_turn.request.request_token,
            _turn(PROPOSAL_KIND_PROCESSING_RUN, _processing_proposal()),
        )
        self.assertEqual(event.kind, RunEventKind.PROPOSAL)
        self.assertEqual(len(calls), 1)
        self.assertEqual(loop.tool_calls_used, 1)
        self.assertEqual(event.tool_events, ())

    def test_recovery_never_bypasses_the_tool_call_limit(self) -> None:
        calls = []
        registry = self._registry(calls)
        limits = AgentRunLimits(
            max_turns=12,
            max_tool_calls_per_run=1,
            max_tool_calls_per_turn=1,
        )
        loop = AgentRunLoop(
            AgentController(registry, limits=limits),
            "instructions",
            proposal_validator=lambda kind, proposal, mode, scope: ProposalValidation.success(
                {"kind": kind, "title": proposal.title, "applied": False}
            ),
        )
        started = loop.start("Randomly extract three.", AgentMode.ACT, AgentScope.PROJECT)
        inspected = json.dumps(
            {
                "action": "tool_calls",
                "assistant_text": "Inspecting.",
                "tool_calls": [
                    {
                        "call_id": "capacity",
                        "tool_name": "processing.describe",
                        "arguments_json": json.dumps(
                            {"algorithm_id": "native:randomextract"}
                        ),
                    }
                ],
                "proposal_kind": "none",
                "proposal_json": "",
            }
        )
        next_turn = loop.submit_provider_response(
            started.request.request_token,
            inspected,
        )
        # A different target has no cached receipt and would need another
        # inspection, but the recovery path must fail closed at the same quota.
        different = _processing_proposal()
        different["algorithm_id"] = "native:buffer"
        event = loop.submit_provider_response(
            next_turn.request.request_token,
            _turn(PROPOSAL_KIND_PROCESSING_RUN, different),
        )
        self.assertEqual(event.kind, RunEventKind.FAILED)
        self.assertEqual(event.reason_code, "malformed_provider_turn")
        self.assertEqual(len(calls), 1)
        self.assertEqual(loop.tool_calls_used, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
