"""Pure-Python tests for the strict agent_turn provider envelope/parser."""
from __future__ import annotations

import json
import unittest

from planx_smartmodeler.core.agent.contracts import AgentToolCall
from planx_smartmodeler.core.agent.protocol import (
    ACTION_FINAL,
    ACTION_TOOL_CALLS,
    MAX_ARGUMENTS_JSON_CHARS,
    MAX_ASSISTANT_TEXT_CHARS,
    MAX_RAW_RESPONSE_CHARS,
    AgentTurn,
    ProtocolError,
    agent_turn_response_schema,
    parse_agent_turn,
)


def _turn_json(
    action: str,
    assistant_text: str = "",
    tool_calls=None,
    proposal_kind: str = "none",
    proposal_json: str = "",
) -> str:
    return json.dumps(
        {
            "action": action,
            "assistant_text": assistant_text,
            "tool_calls": tool_calls if tool_calls is not None else [],
            "proposal_kind": proposal_kind,
            "proposal_json": proposal_json,
        }
    )


class ResponseSchemaTests(unittest.TestCase):
    def test_schema_is_json_serializable(self) -> None:
        schema = agent_turn_response_schema(3)
        json.dumps(schema)  # must not raise
        self.assertEqual(schema["type"], "object")
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(schema["properties"]["tool_calls"]["maxItems"], 3)

    def test_rejects_non_positive_max_calls(self) -> None:
        for bad in (0, -1, True, "3"):
            with self.assertRaises(ProtocolError):
                agent_turn_response_schema(bad)


class ValidTurnParsingTests(unittest.TestCase):
    def test_valid_final_turn(self) -> None:
        turn = parse_agent_turn(_turn_json(ACTION_FINAL, "The answer."), 3)
        self.assertIsInstance(turn, AgentTurn)
        self.assertTrue(turn.is_final)
        self.assertEqual(turn.assistant_text, "The answer.")
        self.assertEqual(turn.tool_calls, ())

    def test_valid_one_call_turn(self) -> None:
        raw = _turn_json(
            ACTION_TOOL_CALLS,
            "",
            [{"call_id": "c1", "tool_name": "project.summary", "arguments_json": "{}"}],
        )
        turn = parse_agent_turn(raw, 3)
        self.assertFalse(turn.is_final)
        self.assertEqual(len(turn.tool_calls), 1)
        self.assertIsInstance(turn.tool_calls[0], AgentToolCall)
        self.assertEqual(turn.tool_calls[0].tool_name, "project.summary")

    def test_valid_multi_call_turn_preserves_order(self) -> None:
        raw = _turn_json(
            ACTION_TOOL_CALLS,
            "checking",
            [
                {"call_id": "c1", "tool_name": "project.summary", "arguments_json": "{}"},
                {
                    "call_id": "c2",
                    "tool_name": "layer.describe",
                    "arguments_json": json.dumps({"layer_id": "l1"}),
                },
            ],
        )
        turn = parse_agent_turn(raw, 3)
        self.assertEqual([call.call_id for call in turn.tool_calls], ["c1", "c2"])
        self.assertEqual(turn.tool_calls[1].arguments, {"layer_id": "l1"})


class MalformedTurnRejectionTests(unittest.TestCase):
    def test_oversized_raw_response_is_rejected_before_json_parsing(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_agent_turn("x" * (MAX_RAW_RESPONSE_CHARS + 1), 3)

    def test_empty_response_is_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_agent_turn("   ", 3)

    def test_markdown_fenced_response_is_rejected_not_repaired(self) -> None:
        raw = "```json\n" + _turn_json(ACTION_FINAL, "hi") + "\n```"
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_leading_prose_is_rejected(self) -> None:
        raw = "Sure, here you go:\n" + _turn_json(ACTION_FINAL, "hi")
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_trailing_prose_is_rejected(self) -> None:
        raw = _turn_json(ACTION_FINAL, "hi") + "\nHope that helps!"
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_top_level_array_is_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_agent_turn("[]", 3)

    def test_json_substring_extracted_from_prose_is_rejected(self) -> None:
        raw = "blah " + _turn_json(ACTION_FINAL, "hi") + " blah"
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_invalid_action_is_rejected(self) -> None:
        raw = json.dumps({"action": "do_it", "assistant_text": "", "tool_calls": []})
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_non_string_assistant_text_is_rejected(self) -> None:
        raw = json.dumps({"action": ACTION_FINAL, "assistant_text": 5, "tool_calls": []})
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_oversized_assistant_text_is_rejected(self) -> None:
        raw = _turn_json(ACTION_FINAL, "x" * (MAX_ASSISTANT_TEXT_CHARS + 1))
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_final_with_calls_is_rejected(self) -> None:
        raw = _turn_json(
            ACTION_FINAL,
            "hi",
            [{"call_id": "c1", "tool_name": "project.summary", "arguments_json": "{}"}],
        )
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_tool_calls_with_empty_list_is_rejected(self) -> None:
        raw = _turn_json(ACTION_TOOL_CALLS, "", [])
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_tool_calls_exceeding_the_per_turn_limit_is_rejected(self) -> None:
        calls = [
            {"call_id": f"c{i}", "tool_name": "project.summary", "arguments_json": "{}"}
            for i in range(4)
        ]
        raw = _turn_json(ACTION_TOOL_CALLS, "", calls)
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_unexpected_top_level_field_is_rejected(self) -> None:
        raw = json.dumps(
            {
                "action": ACTION_FINAL,
                "assistant_text": "hi",
                "tool_calls": [],
                "approved": True,
            }
        )
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_missing_top_level_field_is_rejected(self) -> None:
        raw = json.dumps({"action": ACTION_FINAL, "assistant_text": "hi"})
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_invalid_call_id_is_rejected(self) -> None:
        raw = _turn_json(
            ACTION_TOOL_CALLS,
            "",
            [{"call_id": "has space", "tool_name": "project.summary", "arguments_json": "{}"}],
        )
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_repeated_call_id_within_the_turn_is_rejected(self) -> None:
        raw = _turn_json(
            ACTION_TOOL_CALLS,
            "",
            [
                {"call_id": "c1", "tool_name": "project.summary", "arguments_json": "{}"},
                {"call_id": "c1", "tool_name": "layer.list", "arguments_json": "{}"},
            ],
        )
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_non_dotted_tool_name_is_rejected(self) -> None:
        raw = _turn_json(
            ACTION_TOOL_CALLS,
            "",
            [{"call_id": "c1", "tool_name": "notdotted", "arguments_json": "{}"}],
        )
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_unknown_dotted_tool_name_still_parses(self) -> None:
        # The parser only enforces the dotted shape; whether the tool is
        # actually registered is the registry's/controller's job, not the
        # parser's -- an unknown-but-well-shaped tool name must still parse.
        raw = _turn_json(
            ACTION_TOOL_CALLS,
            "",
            [{"call_id": "c1", "tool_name": "does.not_exist", "arguments_json": "{}"}],
        )
        turn = parse_agent_turn(raw, 3)
        self.assertEqual(turn.tool_calls[0].tool_name, "does.not_exist")

    def test_non_object_arguments_json_is_rejected(self) -> None:
        raw = _turn_json(
            ACTION_TOOL_CALLS,
            "",
            [{"call_id": "c1", "tool_name": "project.summary", "arguments_json": "[1,2]"}],
        )
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_invalid_json_arguments_json_is_rejected(self) -> None:
        raw = _turn_json(
            ACTION_TOOL_CALLS,
            "",
            [{"call_id": "c1", "tool_name": "project.summary", "arguments_json": "{not json}"}],
        )
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_oversized_arguments_json_string_is_rejected(self) -> None:
        oversized = json.dumps({"query": "x" * MAX_ARGUMENTS_JSON_CHARS})
        raw = _turn_json(
            ACTION_TOOL_CALLS,
            "",
            [{"call_id": "c1", "tool_name": "processing.search", "arguments_json": oversized}],
        )
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_nested_oversized_arguments_json_is_rejected(self) -> None:
        arguments_json = json.dumps({"nested": {"list": [{"inner": "x" * 5000}]}})
        raw = _turn_json(
            ACTION_TOOL_CALLS,
            "",
            [{"call_id": "c1", "tool_name": "project.summary", "arguments_json": arguments_json}],
        )
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_non_finite_arguments_json_is_rejected(self) -> None:
        raw = _turn_json(
            ACTION_TOOL_CALLS,
            "",
            [
                {
                    "call_id": "c1",
                    "tool_name": "project.summary",
                    "arguments_json": '{"value": NaN}',
                }
            ],
        )
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_unexpected_call_field_is_rejected(self) -> None:
        raw = _turn_json(
            ACTION_TOOL_CALLS,
            "",
            [
                {
                    "call_id": "c1",
                    "tool_name": "project.summary",
                    "arguments_json": "{}",
                    "approved": True,
                }
            ],
        )
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_missing_call_field_is_rejected(self) -> None:
        raw = _turn_json(
            ACTION_TOOL_CALLS, "", [{"call_id": "c1", "tool_name": "project.summary"}]
        )
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_non_string_raw_text_is_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_agent_turn(None, 3)  # type: ignore[arg-type]

    def test_invalid_max_tool_calls_per_turn_is_rejected(self) -> None:
        for bad in (0, -1, True):
            with self.assertRaises(ProtocolError):
                parse_agent_turn(_turn_json(ACTION_FINAL, "hi"), bad)


class StrictJsonLoadingTests(unittest.TestCase):
    """Finding 3: duplicate keys and deep nesting must fail closed."""

    def test_duplicate_top_level_key_is_rejected(self) -> None:
        # Two "action" keys: standard json keeps the last value; the strict
        # loader must reject the duplicate instead.
        raw = (
            '{"action": "tool_calls", "action": "final", '
            '"assistant_text": "x", "tool_calls": []}'
        )
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_duplicate_key_in_call_object_is_rejected(self) -> None:
        raw = (
            '{"action": "tool_calls", "assistant_text": "", "tool_calls": ['
            '{"call_id": "c1", "call_id": "c1", "tool_name": "a.b", '
            '"arguments_json": "{}"}]}'
        )
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_duplicate_key_inside_arguments_json_is_rejected(self) -> None:
        raw = _turn_json(
            ACTION_TOOL_CALLS,
            "",
            [{"call_id": "c1", "tool_name": "a.b", "arguments_json": '{"x": 1, "x": 2}'}],
        )
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_deeply_nested_response_becomes_protocol_error_not_recursion_error(self) -> None:
        # Valid JSON, well under MAX_RAW_RESPONSE_CHARS, but deep enough to
        # overflow the decoder's recursion. Must surface as a bounded
        # ProtocolError, never an uncaught RecursionError.
        raw = "[" * 20000 + "]" * 20000
        self.assertLess(len(raw), MAX_RAW_RESPONSE_CHARS)
        try:
            with self.assertRaises(ProtocolError):
                parse_agent_turn(raw, 3)
        except RecursionError:  # pragma: no cover - the fix must prevent this
            self.fail("A deeply nested response escaped as RecursionError.")

    def test_deeply_nested_arguments_json_becomes_protocol_error(self) -> None:
        deep = "[" * 15000 + "]" * 15000
        raw = _turn_json(
            ACTION_TOOL_CALLS,
            "",
            [{"call_id": "c1", "tool_name": "a.b", "arguments_json": deep}],
        )
        try:
            with self.assertRaises(ProtocolError):
                parse_agent_turn(raw, 3)
        except RecursionError:  # pragma: no cover
            self.fail("Deeply nested arguments_json escaped as RecursionError.")


VALID_MODEL_PATCH_JSON = json.dumps(
    {
        "schema_version": 1,
        "context_token": "tok",
        "title": "Add a report",
        "summary": "Adds a summary node",
        "operations": [{"op": "set_model_metadata", "name": "New", "description": "D"}],
        "warnings": [],
    }
)


class ProposalProtocolTests(unittest.TestCase):
    def test_schema_has_exactly_five_required_fields(self) -> None:
        schema = agent_turn_response_schema(3)
        self.assertEqual(
            set(schema["required"]),
            {"action", "assistant_text", "tool_calls", "proposal_kind", "proposal_json"},
        )
        self.assertEqual(
            set(schema["properties"]["proposal_kind"]["enum"]),
            {"none", "model_patch", "layer_style", "processing_run", "model_run"},
        )

    def test_valid_proposal_turn_parses(self) -> None:
        raw = _turn_json(
            "proposal", "Here is a patch.", proposal_kind="model_patch", proposal_json=VALID_MODEL_PATCH_JSON
        )
        turn = parse_agent_turn(raw, 3)
        self.assertTrue(turn.is_proposal)
        self.assertEqual(turn.proposal_kind, "model_patch")
        self.assertIsNotNone(turn.proposal)

    def test_legacy_three_key_shape_is_rejected(self) -> None:
        raw = json.dumps({"action": "final", "assistant_text": "hi", "tool_calls": []})
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_tool_call_cannot_smuggle_a_proposal(self) -> None:
        raw = _turn_json(
            ACTION_TOOL_CALLS,
            "",
            [{"call_id": "c1", "tool_name": "project.summary", "arguments_json": "{}"}],
            proposal_kind="model_patch",
            proposal_json=VALID_MODEL_PATCH_JSON,
        )
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_final_cannot_carry_a_proposal(self) -> None:
        raw = _turn_json(
            ACTION_FINAL, "done", proposal_kind="model_patch", proposal_json=VALID_MODEL_PATCH_JSON
        )
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_proposal_cannot_also_call_a_tool(self) -> None:
        raw = _turn_json(
            "proposal",
            "here",
            [{"call_id": "c1", "tool_name": "project.summary", "arguments_json": "{}"}],
            proposal_kind="model_patch",
            proposal_json=VALID_MODEL_PATCH_JSON,
        )
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_proposal_kind_none_for_proposal_action_rejected(self) -> None:
        raw = _turn_json("proposal", "here", proposal_kind="none", proposal_json=VALID_MODEL_PATCH_JSON)
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_empty_proposal_json_rejected(self) -> None:
        raw = _turn_json("proposal", "here", proposal_kind="model_patch", proposal_json="")
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_proposal_json_non_object_rejected(self) -> None:
        raw = _turn_json("proposal", "here", proposal_kind="model_patch", proposal_json="[1,2]")
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_unknown_proposal_kind_rejected(self) -> None:
        raw = _turn_json("proposal", "here", proposal_kind="free_form", proposal_json="{}")
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_duplicate_key_inside_proposal_json_rejected(self) -> None:
        bad = (
            '{"schema_version": 1, "schema_version": 1, "context_token": "t", '
            '"title": "a", "summary": "b", "operations": [], "warnings": []}'
        )
        raw = _turn_json("proposal", "here", proposal_kind="model_patch", proposal_json=bad)
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)

    def test_malformed_proposal_never_reaches_runtime(self) -> None:
        # A structurally invalid proposal fails at parse time -> ProtocolError,
        # so no proposal object is ever produced for the runtime validator.
        bad = json.dumps(
            {"schema_version": 2, "context_token": "t", "title": "a", "summary": "b", "operations": [], "warnings": []}
        )
        raw = _turn_json("proposal", "here", proposal_kind="model_patch", proposal_json=bad)
        with self.assertRaises(ProtocolError):
            parse_agent_turn(raw, 3)


if __name__ == "__main__":
    unittest.main()
