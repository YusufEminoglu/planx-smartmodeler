from __future__ import annotations

import math
import unittest

from planx_smartmodeler.core.agent.contracts import (
    AgentResultStatus,
    AgentRisk,
    AgentRunLimits,
    AgentScope,
    AgentToolCall,
    AgentToolResult,
    AgentToolSpec,
    ContractError,
    validate_json_value,
    validate_tool_arguments,
)
from planx_smartmodeler.core.agent.registry import (
    AgentToolRegistry,
    ToolRegistrationError,
)

EMPTY_OBJECT_SCHEMA = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}


def make_spec(
    name: str = "project.summary",
    risk: str = AgentRisk.READ_ONLY,
    input_schema=None,
) -> AgentToolSpec:
    return AgentToolSpec(
        name=name,
        title="Title",
        description="Description",
        risk=risk,
        input_schema=input_schema if input_schema is not None else dict(EMPTY_OBJECT_SCHEMA),
        allowed_scopes=(AgentScope.PROJECT,),
    )


class ToolSpecTests(unittest.TestCase):
    def test_valid_tool_identifier(self) -> None:
        spec = make_spec("project.summary")
        self.assertEqual(spec.name, "project.summary")
        self.assertEqual(
            spec.public_description(),
            {
                "name": "project.summary",
                "title": "Title",
                "description": "Description",
                "risk": AgentRisk.READ_ONLY,
                "allowed_scopes": [AgentScope.PROJECT],
                "input_schema": EMPTY_OBJECT_SCHEMA,
            },
        )

    def test_invalid_tool_identifiers_are_rejected(self) -> None:
        for bad_name in ("", "NoDots", "has space.tool", "1.leadingdigit", "trailing.", ".leading"):
            with self.assertRaises(ContractError):
                make_spec(bad_name)

    def test_unknown_risk_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            make_spec(risk="not_a_real_risk")

    def test_empty_allowed_scopes_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            AgentToolSpec(
                name="a.b",
                title="t",
                description="d",
                risk=AgentRisk.READ_ONLY,
                input_schema=dict(EMPTY_OBJECT_SCHEMA),
                allowed_scopes=(),
            )

    def test_title_and_description_bounds(self) -> None:
        with self.assertRaises(ContractError):
            AgentToolSpec(
                name="a.b",
                title="x" * 121,
                description="d",
                risk=AgentRisk.READ_ONLY,
                input_schema=dict(EMPTY_OBJECT_SCHEMA),
                allowed_scopes=(AgentScope.PROJECT,),
            )
        with self.assertRaises(ContractError):
            AgentToolSpec(
                name="a.b",
                title="t",
                description="x" * 501,
                risk=AgentRisk.READ_ONLY,
                input_schema=dict(EMPTY_OBJECT_SCHEMA),
                allowed_scopes=(AgentScope.PROJECT,),
            )

    def test_public_description_schema_is_a_deep_copy(self) -> None:
        spec = make_spec("a.b", input_schema={
            "type": "object",
            "properties": {"query": {"type": "string", "maxLength": 10}},
            "required": [],
            "additionalProperties": False,
        })
        description = spec.public_description()
        description["input_schema"]["properties"]["query"]["maxLength"] = 999999
        description["input_schema"]["properties"]["injected"] = {"type": "string"}
        # Mutating the returned copy must never affect the registered spec.
        self.assertEqual(spec.input_schema["properties"]["query"]["maxLength"], 10)
        self.assertNotIn("injected", spec.input_schema["properties"])

    def test_schema_must_be_object_type(self) -> None:
        with self.assertRaises(ContractError):
            make_spec("a.b", input_schema={"type": "string"})

    def test_schema_must_declare_additional_properties_false(self) -> None:
        with self.assertRaises(ContractError):
            make_spec(
                "a.b",
                input_schema={"type": "object", "properties": {}, "required": []},
            )

    def test_schema_required_key_must_have_a_property(self) -> None:
        with self.assertRaises(ContractError):
            make_spec(
                "a.b",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": ["missing"],
                    "additionalProperties": False,
                },
            )

    def test_schema_rejects_unsupported_property_type(self) -> None:
        with self.assertRaises(ContractError):
            make_spec(
                "a.b",
                input_schema={
                    "type": "object",
                    "properties": {"flag": {"type": "boolean"}},
                    "required": [],
                    "additionalProperties": False,
                },
            )

    def test_schema_missing_a_required_top_level_keyword_is_rejected(self) -> None:
        # "required" itself is missing entirely -- not merely empty.
        with self.assertRaises(ContractError):
            make_spec(
                "a.b",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            )

    def test_schema_rejects_unknown_top_level_keyword(self) -> None:
        with self.assertRaises(ContractError):
            make_spec(
                "a.b",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                    "$schema": "https://example.invalid/schema",
                },
            )

    def test_schema_rejects_unknown_property_keyword(self) -> None:
        # "pattern" would be silently ignored by dispatch validation, so an
        # advertised-but-unenforced constraint must fail closed instead.
        with self.assertRaises(ContractError):
            make_spec(
                "a.b",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string", "pattern": ".*"}},
                    "required": [],
                    "additionalProperties": False,
                },
            )

    def test_schema_rejects_inverted_string_bounds(self) -> None:
        with self.assertRaises(ContractError):
            make_spec(
                "a.b",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 10, "maxLength": 5}
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            )

    def test_schema_rejects_inverted_integer_bounds(self) -> None:
        with self.assertRaises(ContractError):
            make_spec(
                "a.b",
                input_schema={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 10, "maximum": 5}
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            )

    def test_schema_rejects_non_string_property_name(self) -> None:
        with self.assertRaises(ContractError):
            make_spec(
                "a.b",
                input_schema={
                    "type": "object",
                    "properties": {1: {"type": "string"}},
                    "required": [],
                    "additionalProperties": False,
                },
            )

    def test_schema_rejects_non_json_extension_keyword(self) -> None:
        # An arbitrary Python object stored under an unrecognized keyword
        # must be rejected, not silently carried through to a public
        # description that promises plain JSON.
        with self.assertRaises(ContractError):
            make_spec(
                "a.b",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "extension": object()}
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            )

    def test_schema_rejects_duplicate_required_entries(self) -> None:
        with self.assertRaises(ContractError):
            make_spec(
                "a.b",
                input_schema={
                    "type": "object",
                    "properties": {"layer_id": {"type": "string"}},
                    "required": ["layer_id", "layer_id"],
                    "additionalProperties": False,
                },
            )

    def test_mutating_the_original_schema_dict_after_construction_has_no_effect(self) -> None:
        schema = {
            "type": "object",
            "properties": {"query": {"type": "string", "maxLength": 3}},
            "required": [],
            "additionalProperties": False,
        }
        spec = make_spec("a.b", input_schema=schema)
        schema["properties"]["query"]["maxLength"] = 999999
        schema["properties"]["injected"] = {"type": "string"}
        with self.assertRaises(ContractError):
            validate_tool_arguments(spec.input_schema, {"query": "xxxx"})

    def test_mutating_a_spec_returned_by_get_spec_is_impossible(self) -> None:
        registry = AgentToolRegistry()
        registry.register(
            make_spec(
                "a.b",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string", "maxLength": 3}},
                    "required": [],
                    "additionalProperties": False,
                },
            ),
            lambda call: {},
        )
        spec = registry.get_spec("a.b")
        with self.assertRaises(TypeError):
            spec.input_schema["properties"]["query"]["maxLength"] = 999999
        # The four-character call must still fail after the mutation attempt.
        with self.assertRaises(ContractError):
            validate_tool_arguments(registry.get_spec("a.b").input_schema, {"query": "xxxx"})

    def test_mutating_a_spec_returned_by_list_specs_is_impossible(self) -> None:
        registry = AgentToolRegistry()
        registry.register(
            make_spec(
                "a.b",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string", "maxLength": 3}},
                    "required": [],
                    "additionalProperties": False,
                },
            ),
            lambda call: {},
        )
        spec = registry.list_specs()[0]
        with self.assertRaises(TypeError):
            spec.input_schema["properties"]["query"]["maxLength"] = 999999
        with self.assertRaises(TypeError):
            spec.input_schema["properties"]["injected"] = {"type": "string"}

    def test_second_spec_can_be_constructed_from_a_registered_specs_frozen_schema(
        self,
    ) -> None:
        # Regression for Phase 01 acceptance follow-up P2-001: constructing a
        # new AgentToolSpec directly from another registered spec's already
        # frozen (MappingProxyType/tuple) input_schema used to raise
        # "TypeError: cannot pickle 'mappingproxy' object".
        original_schema = {
            "type": "object",
            "properties": {"query": {"type": "string", "maxLength": 10}},
            "required": [],
            "additionalProperties": False,
        }
        first = make_spec("a.one", input_schema=original_schema)
        second = make_spec("a.two", input_schema=first.input_schema)

        # Both specs remain immutable and independent.
        with self.assertRaises(TypeError):
            first.input_schema["properties"]["query"]["maxLength"] = 999
        with self.assertRaises(TypeError):
            second.input_schema["properties"]["query"]["maxLength"] = 999
        self.assertEqual(
            first.input_schema["properties"]["query"]["maxLength"], 10
        )
        self.assertEqual(
            second.input_schema["properties"]["query"]["maxLength"], 10
        )

        # Mutating the original caller-owned plain schema still has no effect.
        original_schema["properties"]["query"]["maxLength"] = 999
        self.assertEqual(
            first.input_schema["properties"]["query"]["maxLength"], 10
        )
        self.assertEqual(
            second.input_schema["properties"]["query"]["maxLength"], 10
        )

        # public_description() remains a fresh, plain JSON-compatible tree.
        import json

        for spec in (first, second):
            description = spec.public_description()
            json.dumps(description)
            self.assertIsInstance(description["input_schema"], dict)
            self.assertIsInstance(description["input_schema"]["properties"], dict)

    def test_public_tool_descriptions_are_plain_json_compatible(self) -> None:
        import json

        registry = AgentToolRegistry()
        registry.register(
            make_spec(
                "a.b",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string", "maxLength": 3}},
                    "required": [],
                    "additionalProperties": False,
                },
            ),
            lambda call: {},
        )
        for description in registry.public_tool_descriptions():
            json.dumps(description)  # must not raise
            self.assertIsInstance(description["input_schema"], dict)
            self.assertIsInstance(description["input_schema"]["properties"], dict)
            self.assertIsInstance(description["input_schema"]["required"], list)


class ToolArgumentSchemaValidationTests(unittest.TestCase):
    LAYER_DESCRIBE_SCHEMA = {
        "type": "object",
        "properties": {
            "layer_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["layer_id"],
        "additionalProperties": False,
    }

    def test_missing_required_argument_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            validate_tool_arguments(self.LAYER_DESCRIBE_SCHEMA, {})

    def test_unknown_argument_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            validate_tool_arguments(
                self.LAYER_DESCRIBE_SCHEMA, {"layer_id": "l1", "unexpected": 1}
            )

    def test_wrong_type_string_argument_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            validate_tool_arguments(self.LAYER_DESCRIBE_SCHEMA, {"layer_id": 123})

    def test_bool_is_never_accepted_as_integer(self) -> None:
        with self.assertRaises(ContractError):
            validate_tool_arguments(
                self.LAYER_DESCRIBE_SCHEMA, {"layer_id": "l1", "limit": True}
            )
        with self.assertRaises(ContractError):
            validate_tool_arguments(
                self.LAYER_DESCRIBE_SCHEMA, {"layer_id": "l1", "limit": False}
            )

    def test_out_of_range_integer_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            validate_tool_arguments(
                self.LAYER_DESCRIBE_SCHEMA, {"layer_id": "l1", "limit": 0}
            )
        with self.assertRaises(ContractError):
            validate_tool_arguments(
                self.LAYER_DESCRIBE_SCHEMA, {"layer_id": "l1", "limit": 101}
            )

    def test_out_of_range_string_length_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            validate_tool_arguments(self.LAYER_DESCRIBE_SCHEMA, {"layer_id": "x" * 129})
        with self.assertRaises(ContractError):
            validate_tool_arguments(self.LAYER_DESCRIBE_SCHEMA, {"layer_id": ""})

    def test_valid_arguments_pass(self) -> None:
        validate_tool_arguments(self.LAYER_DESCRIBE_SCHEMA, {"layer_id": "l1", "limit": 10})
        validate_tool_arguments(self.LAYER_DESCRIBE_SCHEMA, {"layer_id": "l1"})


class ToolRegistryTests(unittest.TestCase):
    def test_duplicate_registration_is_rejected(self) -> None:
        registry = AgentToolRegistry()
        registry.register(make_spec("a.one"), lambda call: {})
        with self.assertRaises(ToolRegistrationError):
            registry.register(make_spec("a.one"), lambda call: {})

    def test_registration_requires_callable_handler(self) -> None:
        registry = AgentToolRegistry()
        with self.assertRaises(ToolRegistrationError):
            registry.register(make_spec("a.two"), "not-callable")  # type: ignore[arg-type]

    def test_deterministic_registration_order(self) -> None:
        registry = AgentToolRegistry()
        for name in ("z.tool", "a.tool", "m.tool"):
            registry.register(make_spec(name), lambda call: {})
        self.assertEqual(
            [spec.name for spec in registry.list_specs()],
            ["a.tool", "m.tool", "z.tool"],
        )
        self.assertEqual(
            [item["name"] for item in registry.public_tool_descriptions()],
            ["a.tool", "m.tool", "z.tool"],
        )

    def test_unknown_tool_lookup_returns_none(self) -> None:
        registry = AgentToolRegistry()
        self.assertIsNone(registry.get_spec("does.not_exist"))
        self.assertIsNone(registry.get_handler("does.not_exist"))
        self.assertFalse(registry.has_tool("does.not_exist"))


class JsonValueValidationTests(unittest.TestCase):
    def test_accepts_plain_json_values(self) -> None:
        value = {"a": 1, "b": [1, 2, "x", None, True], "c": {"d": 1.5}}
        self.assertEqual(validate_json_value(value), value)

    def test_rejects_nan_and_infinity(self) -> None:
        with self.assertRaises(ContractError):
            validate_json_value(float("nan"))
        with self.assertRaises(ContractError):
            validate_json_value(float("inf"))
        with self.assertRaises(ContractError):
            validate_json_value([1, 2, math.inf])
        with self.assertRaises(ContractError):
            validate_json_value({"x": float("nan")})

    def test_rejects_unsupported_objects(self) -> None:
        class NotJson:
            pass

        with self.assertRaises(ContractError):
            validate_json_value(NotJson())
        with self.assertRaises(ContractError):
            validate_json_value({"x": NotJson()})
        with self.assertRaises(ContractError):
            validate_json_value({1: "non-string key"})

    def test_rejects_oversized_collections(self) -> None:
        with self.assertRaises(ContractError):
            validate_json_value(list(range(501)))
        with self.assertRaises(ContractError):
            validate_json_value({str(i): i for i in range(201)})

    def test_rejects_excessive_nesting(self) -> None:
        value: object = "leaf"
        for _ in range(25):
            value = [value]
        with self.assertRaises(ContractError):
            validate_json_value(value)


class ToolCallTests(unittest.TestCase):
    def test_valid_call(self) -> None:
        call = AgentToolCall(call_id="abc-123", tool_name="project.summary", arguments={"limit": 5})
        self.assertEqual(call.arguments, {"limit": 5})

    def test_invalid_call_id_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            AgentToolCall(call_id="", tool_name="project.summary")
        with self.assertRaises(ContractError):
            AgentToolCall(call_id="has space", tool_name="project.summary")
        with self.assertRaises(ContractError):
            AgentToolCall(call_id="x" * 65, tool_name="project.summary")

    def test_arguments_must_be_json_object(self) -> None:
        with self.assertRaises(ContractError):
            AgentToolCall(
                call_id="abc",
                tool_name="project.summary",
                arguments=["not", "a", "dict"],  # type: ignore[arg-type]
            )

    def test_arguments_reject_non_finite_numbers(self) -> None:
        with self.assertRaises(ContractError):
            AgentToolCall(call_id="abc", tool_name="project.summary", arguments={"value": float("nan")})

    def test_malformed_input_is_not_silently_coerced(self) -> None:
        class Weird:
            def __str__(self) -> str:
                return "weird"

        with self.assertRaises(ContractError):
            AgentToolCall(call_id="abc", tool_name="project.summary", arguments={"value": Weird()})

    def test_million_character_argument_string_is_rejected(self) -> None:
        # Regression for blocking finding 2: a single oversized string
        # argument must be rejected outright, not silently truncated.
        with self.assertRaises(ContractError):
            AgentToolCall(
                call_id="abc", tool_name="project.summary", arguments={"query": "x" * 1_000_000}
            )

    def test_string_argument_at_the_per_string_limit_is_accepted(self) -> None:
        from planx_smartmodeler.core.agent.contracts import MAX_TEXT_ARG_LENGTH

        call = AgentToolCall(
            call_id="abc",
            tool_name="project.summary",
            arguments={"query": "x" * MAX_TEXT_ARG_LENGTH},
        )
        self.assertEqual(len(call.arguments["query"]), MAX_TEXT_ARG_LENGTH)

    def test_string_argument_one_over_the_per_string_limit_is_rejected(self) -> None:
        from planx_smartmodeler.core.agent.contracts import MAX_TEXT_ARG_LENGTH

        with self.assertRaises(ContractError):
            AgentToolCall(
                call_id="abc",
                tool_name="project.summary",
                arguments={"query": "x" * (MAX_TEXT_ARG_LENGTH + 1)},
            )

    def test_many_small_strings_are_rejected_by_the_total_argument_budget(self) -> None:
        # No single string exceeds MAX_TEXT_ARG_LENGTH, but the aggregate
        # serialized size must still be bounded.
        with self.assertRaises(ContractError):
            AgentToolCall(
                call_id="abc",
                tool_name="project.summary",
                arguments={f"k{i}": "x" * 1900 for i in range(20)},
            )

    def test_deeply_nested_oversized_string_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            AgentToolCall(
                call_id="abc",
                tool_name="project.summary",
                arguments={"nested": {"list": [{"inner": "x" * 1_000_000}]}},
            )


class ToolResultTests(unittest.TestCase):
    def test_success_result_carries_validated_data(self) -> None:
        result = AgentToolResult("id1", "project.summary", AgentResultStatus.SUCCESS, {"a": 1})
        self.assertEqual(result.data, {"a": 1})
        self.assertEqual(result.to_dict()["status"], AgentResultStatus.SUCCESS)

    def test_non_success_result_never_carries_data(self) -> None:
        result = AgentToolResult(
            "id1", "project.summary", AgentResultStatus.DENIED, {"should": "be dropped"}, "denied"
        )
        self.assertIsNone(result.data)

    def test_unknown_status_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            AgentToolResult("id1", "project.summary", "not_a_status")

    def test_message_is_bounded(self) -> None:
        result = AgentToolResult(
            "id1", "project.summary", AgentResultStatus.FAILED, None, "x" * 20050
        )
        self.assertEqual(len(result.message), 20000)

    def test_success_result_rejects_unsupported_data(self) -> None:
        class NotJson:
            pass

        with self.assertRaises(ContractError):
            AgentToolResult("id1", "project.summary", AgentResultStatus.SUCCESS, NotJson())

    def test_to_dict_is_plain_and_serializable(self) -> None:
        import json

        result = AgentToolResult("id1", "project.summary", AgentResultStatus.SUCCESS, {"a": [1, 2]})
        json.dumps(result.to_dict())

    def test_success_data_rejects_an_oversized_string(self) -> None:
        with self.assertRaises(ContractError):
            AgentToolResult(
                "id1", "project.summary", AgentResultStatus.SUCCESS, {"text": "x" * 1_000_000}
            )

    def test_success_data_rejects_an_oversized_aggregate_payload(self) -> None:
        with self.assertRaises(ContractError):
            AgentToolResult(
                "id1",
                "project.summary",
                AgentResultStatus.SUCCESS,
                {"items": ["x" * 15000 for _ in range(50)]},
            )

    def test_non_serializable_call_id_is_rejected(self) -> None:
        # Regression for second-review blocking finding 3's exact evidence:
        # AgentToolResult(object(), object(), AgentResultStatus.DENIED) used
        # to construct successfully and only fail later at json.dumps().
        with self.assertRaises(ContractError):
            AgentToolResult(object(), "project.summary", AgentResultStatus.DENIED)

    def test_non_serializable_tool_name_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            AgentToolResult("id1", object(), AgentResultStatus.DENIED)

    def test_empty_call_id_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            AgentToolResult("", "project.summary", AgentResultStatus.DENIED)

    def test_call_id_with_disallowed_characters_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            AgentToolResult("has space", "project.summary", AgentResultStatus.DENIED)

    def test_tool_name_without_a_dot_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            AgentToolResult("id1", "notdotted", AgentResultStatus.DENIED)

    def test_reason_code_must_be_bounded_snake_case(self) -> None:
        with self.assertRaises(ContractError):
            AgentToolResult(
                "id1", "project.summary", AgentResultStatus.DENIED, None, "", "Not-Valid!"
            )
        with self.assertRaises(ContractError):
            AgentToolResult(
                "id1", "project.summary", AgentResultStatus.DENIED, None, "", "x" * 65
            )

    def test_empty_reason_code_is_valid_for_success(self) -> None:
        result = AgentToolResult("id1", "project.summary", AgentResultStatus.SUCCESS, {"a": 1})
        self.assertEqual(result.reason_code, "")

    def test_json_dumps_round_trips_for_every_result_status(self) -> None:
        import json

        cases = [
            AgentToolResult("id1", "project.summary", AgentResultStatus.SUCCESS, {"a": 1}),
            AgentToolResult(
                "id1", "project.summary", AgentResultStatus.DENIED, None, "no", "deny"
            ),
            AgentToolResult(
                "id1",
                "project.summary",
                AgentResultStatus.APPROVAL_REQUIRED,
                None,
                "ask",
                "require_approval",
            ),
            AgentToolResult(
                "id1", "project.summary", AgentResultStatus.FAILED, None, "err", "handler_error"
            ),
        ]
        for result in cases:
            json.dumps(result.to_dict())


class RunLimitsValidationTests(unittest.TestCase):
    def test_default_limits_are_valid(self) -> None:
        AgentRunLimits()

    def test_rejects_the_reported_adversarial_object(self) -> None:
        # Regression for blocking finding 3's exact evidence payload.
        with self.assertRaises(ContractError):
            AgentRunLimits(
                max_turns=-1,
                max_tool_calls_per_run=0,
                max_tool_calls_per_turn=-5,
                max_prompt_chars=1_000_000_000,
                max_result_text_chars=1_000_000_000,
            )

    def test_rejects_zero_and_negative_values(self) -> None:
        with self.assertRaises(ContractError):
            AgentRunLimits(max_turns=0)
        with self.assertRaises(ContractError):
            AgentRunLimits(max_tool_calls_per_run=-1)
        with self.assertRaises(ContractError):
            AgentRunLimits(max_tool_calls_per_turn=0)

    def test_rejects_boolean_values(self) -> None:
        with self.assertRaises(ContractError):
            AgentRunLimits(max_turns=True)

    def test_rejects_absurdly_large_values(self) -> None:
        with self.assertRaises(ContractError):
            AgentRunLimits(max_turns=10_000)
        with self.assertRaises(ContractError):
            AgentRunLimits(max_prompt_chars=1_000_000_000)
        with self.assertRaises(ContractError):
            AgentRunLimits(max_result_text_chars=1_000_000_000)

    def test_rejects_per_turn_limit_exceeding_per_run_limit(self) -> None:
        with self.assertRaises(ContractError):
            AgentRunLimits(max_tool_calls_per_run=2, max_tool_calls_per_turn=5)


if __name__ == "__main__":
    unittest.main()
