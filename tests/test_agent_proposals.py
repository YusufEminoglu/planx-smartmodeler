"""Pure-Python tests for proposal parsing and detached model-patch validation.

QGIS-free: uses a fake algorithm catalog and a plain deepcopy clone so the
proposal contracts and the detached applier are exercised without a Processing
registry. Live-layer/live-graph glue (target resolution, token verification)
is covered by the runtime validator's QGIS smoke coverage.
"""
from __future__ import annotations

import copy
import json
import unittest

from planx_smartmodeler.core.agent import context as agent_context
from planx_smartmodeler.core.agent.proposals import (
    LayerStyleProposal,
    ModelPatchProposal,
    ProposalError,
    ProposalReason,
    build_model_patch_preview,
    parse_proposal,
)
from planx_smartmodeler.core.graph_model import GraphModel, NodeDefinition, SocketType


# -- fake catalog / graph fixtures -----------------------------------------

_ALGORITHMS = {
    "smart:input_layer": {"inputs": [], "outputs": [("OUTPUT", SocketType.VECTOR)]},
    "smart:raster_layer": {"inputs": [], "outputs": [("OUTPUT", SocketType.RASTER)]},
    "smart:number": {"inputs": [], "outputs": [("OUTPUT", SocketType.NUMBER)]},
    "native:buffer": {
        "inputs": [("INPUT", SocketType.VECTOR, True), ("DISTANCE", SocketType.NUMBER, False)],
        "outputs": [("OUTPUT", SocketType.VECTOR)],
    },
    "native:centroid": {
        "inputs": [("INPUT", SocketType.VECTOR, True)],
        "outputs": [("OUTPUT", SocketType.VECTOR)],
    },
    "native:withfile": {
        "inputs": [("FILEIN", SocketType.FILE, False)],
        "outputs": [("OUTPUT", SocketType.VECTOR)],
    },
    "native:multi": {
        "inputs": [
            ("NUM", SocketType.NUMBER, False),
            ("FLAG", SocketType.BOOLEAN, False),
            ("TXT", SocketType.STRING, False),
            ("FLD", SocketType.FIELD, False),
            ("VEC", SocketType.VECTOR, False),
            ("RAS", SocketType.RASTER, False),
            ("ANYIN", SocketType.ANY, False),
            ("MULTI", SocketType.VECTOR, False, True),
        ],
        "outputs": [("OUTPUT", SocketType.VECTOR)],
    },
}


class FakeCatalog:
    def __init__(self, layer_choices=None) -> None:
        self._layer_choices = layer_choices or {"vector": {"L_vec": "Vec"}, "raster": {"L_ras": "Ras"}}

    def algorithm_exists(self, algorithm_id: str) -> bool:
        return algorithm_id in _ALGORITHMS

    def ai_algorithm_allowed(self, algorithm_id: str) -> bool:
        return "command" not in algorithm_id and "shell" not in algorithm_id

    def create_node(self, algorithm_id: str, node_id: str, title: str) -> NodeDefinition:
        spec = _ALGORITHMS[algorithm_id]
        node = NodeDefinition(node_id=node_id, title=title, algorithm_id=algorithm_id)
        if algorithm_id in ("smart:input_layer", "smart:raster_layer"):
            node.parameters["LAYER"] = ""
        if algorithm_id in ("smart:number", "smart:slider"):
            node.parameters["VALUE"] = 0
        for port in spec["inputs"]:
            allows_multiple = port[3] if len(port) > 3 else False
            node.add_input(
                port[0], port[0], port[1], required=port[2], allows_multiple=allows_multiple
            )
        for port in spec["outputs"]:
            node.add_output(port[0], port[0], port[1])
        return node

    def layer_choices(self, socket_type: str):
        return dict(self._layer_choices.get(socket_type, {}))


def make_two_node_graph() -> GraphModel:
    graph = GraphModel("Base model")
    catalog = FakeCatalog()
    source = catalog.create_node("smart:input_layer", "src", "Source")
    source.parameters["LAYER"] = "L_vec"
    buffer_node = catalog.create_node("native:buffer", "buf", "Buffer")
    buffer_node.parameters["DISTANCE"] = 5.0
    graph.add_node(source)
    graph.add_node(buffer_node)
    graph.add_edge("src", "OUTPUT", "buf", "INPUT")
    return graph


def model_patch_json(operations, token="tok", title="Patch", summary="Improve", warnings=None):
    return json.dumps(
        {
            "schema_version": 1,
            "context_token": token,
            "title": title,
            "summary": summary,
            "operations": operations,
            "warnings": warnings or [],
        }
    )


def renderer_dict(family, field="", class_count=0, palette=None, opacity=1.0):
    return {
        "family": family,
        "field": field,
        "class_count": class_count,
        "palette": palette or [],
        "opacity": opacity,
    }


def labels_dict(enabled=False, field=""):
    return {"enabled": enabled, "field": field}


def style_json(renderer, labels, token="tok", layer_id="L_vec", title="Style", summary="Intent", warnings=None):
    return json.dumps(
        {
            "schema_version": 1,
            "context_token": token,
            "target_layer_id": layer_id,
            "title": title,
            "summary": summary,
            "renderer": renderer,
            "labels": labels,
            "warnings": warnings or [],
        }
    )


def add_node_op(node_id, algorithm_id, title="Node", parameters=None):
    return {
        "op": "add_node",
        "node_id": node_id,
        "algorithm_id": algorithm_id,
        "title": title,
        "parameters": parameters or [],
    }


def connect_op(from_node, to_node, from_output="OUTPUT", to_input="INPUT"):
    return {
        "op": "connect",
        "from_node": from_node,
        "from_output": from_output,
        "to_node": to_node,
        "to_input": to_input,
    }


def layer_param(layer_id):
    return [{"name": "LAYER", "value": layer_id}]


ADD_BUFFER = add_node_op("b2", "native:buffer", "Second buffer", [{"name": "DISTANCE", "value": 10}])


# -- proposal parsing (contract) -------------------------------------------


class ModelPatchParsingTests(unittest.TestCase):
    def test_every_operation_shape_parses(self) -> None:
        ops = [
            ADD_BUFFER,
            {"op": "remove_node", "node_id": "old"},
            {"op": "set_parameter", "node_id": "b2", "name": "DISTANCE", "value": 3},
            {"op": "connect", "from_node": "a", "from_output": "OUTPUT", "to_node": "b2", "to_input": "INPUT"},
            {"op": "disconnect", "edge_id": "e_a_OUTPUT__to__b_INPUT"},
            {"op": "rename_node", "node_id": "b2", "title": "Renamed"},
            {"op": "set_model_metadata", "name": "New name", "description": "Desc"},
        ]
        proposal = parse_proposal("model_patch", model_patch_json(ops))
        self.assertIsInstance(proposal, ModelPatchProposal)
        self.assertEqual(len(proposal.operations), 7)
        self.assertTrue(proposal.operations[1].is_destructive)
        self.assertTrue(proposal.operations[4].is_destructive)
        self.assertFalse(proposal.operations[0].is_destructive)

    def test_unknown_kind_rejected(self) -> None:
        with self.assertRaises(ProposalError) as ctx:
            parse_proposal("free_form", "{}")
        self.assertEqual(ctx.exception.reason_code, ProposalReason.UNKNOWN_KIND)

    def test_extra_top_level_key_rejected(self) -> None:
        raw = json.loads(model_patch_json([ADD_BUFFER]))
        raw["extra"] = 1
        with self.assertRaises(ProposalError):
            parse_proposal("model_patch", json.dumps(raw))

    def test_missing_top_level_key_rejected(self) -> None:
        raw = json.loads(model_patch_json([ADD_BUFFER]))
        del raw["summary"]
        with self.assertRaises(ProposalError):
            parse_proposal("model_patch", json.dumps(raw))

    def test_unknown_operation_rejected(self) -> None:
        with self.assertRaises(ProposalError):
            parse_proposal("model_patch", model_patch_json([{"op": "explode", "node_id": "a"}]))

    def test_extra_operation_key_rejected(self) -> None:
        op = dict(ADD_BUFFER)
        op["surprise"] = 1
        with self.assertRaises(ProposalError):
            parse_proposal("model_patch", model_patch_json([op]))

    def test_duplicate_parameter_name_rejected(self) -> None:
        op = {
            "op": "add_node",
            "node_id": "b2",
            "algorithm_id": "native:buffer",
            "title": "Buf",
            "parameters": [{"name": "DISTANCE", "value": 1}, {"name": "DISTANCE", "value": 2}],
        }
        with self.assertRaises(ProposalError):
            parse_proposal("model_patch", model_patch_json([op]))

    def test_non_finite_value_rejected(self) -> None:
        raw = model_patch_json(
            [{"op": "set_parameter", "node_id": "a", "name": "X", "value": 0}]
        ).replace('"value": 0', '"value": NaN')
        with self.assertRaises(ProposalError):
            parse_proposal("model_patch", raw)

    def test_duplicate_json_keys_rejected(self) -> None:
        raw = (
            '{"schema_version": 1, "schema_version": 1, "context_token": "t", '
            '"title": "a", "summary": "b", "operations": [], "warnings": []}'
        )
        with self.assertRaises(ProposalError):
            parse_proposal("model_patch", raw)

    def test_operation_count_bound(self) -> None:
        ops = [dict(ADD_BUFFER, node_id=f"n{i}") for i in range(41)]
        with self.assertRaises(ProposalError) as ctx:
            parse_proposal("model_patch", model_patch_json(ops))
        self.assertEqual(ctx.exception.reason_code, ProposalReason.LIMIT_EXCEEDED)

    def test_empty_operations_rejected(self) -> None:
        with self.assertRaises(ProposalError):
            parse_proposal("model_patch", model_patch_json([]))

    def test_invalid_node_id_rejected(self) -> None:
        op = dict(ADD_BUFFER, node_id="has space")
        with self.assertRaises(ProposalError):
            parse_proposal("model_patch", model_patch_json([op]))

    def test_oversized_json_rejected(self) -> None:
        big = "x" * 61000
        op = {
            "op": "set_parameter",
            "node_id": "a",
            "name": "X",
            "value": big,
        }
        with self.assertRaises(ProposalError) as ctx:
            parse_proposal("model_patch", model_patch_json([op]))
        self.assertEqual(ctx.exception.reason_code, ProposalReason.LIMIT_EXCEEDED)

    def test_to_dict_is_fresh_and_detached(self) -> None:
        proposal = parse_proposal("model_patch", model_patch_json([ADD_BUFFER]))
        one = proposal.to_dict()
        one["operations"][0]["title"] = "mutated"
        two = proposal.to_dict()
        self.assertNotEqual(two["operations"][0]["title"], "mutated")


class LayerStyleParsingTests(unittest.TestCase):
    def test_keep_family(self) -> None:
        proposal = parse_proposal(
            "layer_style",
            style_json(renderer_dict("keep"), labels_dict()),
        )
        self.assertIsInstance(proposal, LayerStyleProposal)
        self.assertTrue(proposal.is_vector_family)

    def test_single_symbol_family(self) -> None:
        parse_proposal(
            "layer_style",
            style_json(
                renderer_dict("single_symbol", class_count=1, palette=["#ff0000"], opacity=0.5),
                labels_dict(),
            ),
        )

    def test_graduated_family_requires_matching_palette(self) -> None:
        good = style_json(
            renderer_dict("graduated", field="pop", class_count=3, palette=["#000000", "#777777", "#ffffff"]),
            labels_dict(enabled=True, field="name"),
        )
        proposal = parse_proposal("layer_style", good)
        self.assertEqual(proposal.renderer.palette, ("#000000", "#777777", "#FFFFFF"))
        bad = style_json(
            renderer_dict("graduated", field="pop", class_count=3, palette=["#000000", "#ffffff"]),
            labels_dict(),
        )
        with self.assertRaises(ProposalError):
            parse_proposal("layer_style", bad)

    def test_raster_pseudocolor_family(self) -> None:
        parse_proposal(
            "layer_style",
            style_json(
                renderer_dict("raster_pseudocolor", class_count=2, palette=["#000000", "#ffffff"]),
                labels_dict(),
                layer_id="L_ras",
            ),
        )

    def test_raster_family_forbids_labels(self) -> None:
        with self.assertRaises(ProposalError):
            parse_proposal(
                "layer_style",
                style_json(
                    renderer_dict("raster_gray"),
                    labels_dict(enabled=True, field="name"),
                    layer_id="L_ras",
                ),
            )

    def test_invalid_color_rejected(self) -> None:
        with self.assertRaises(ProposalError):
            parse_proposal(
                "layer_style",
                style_json(renderer_dict("single_symbol", class_count=1, palette=["red"]), labels_dict()),
            )

    def test_class_palette_mismatch_rejected(self) -> None:
        with self.assertRaises(ProposalError):
            parse_proposal(
                "layer_style",
                style_json(
                    renderer_dict("categorized", field="pop", class_count=2, palette=["#000000"]),
                    labels_dict(),
                ),
            )

    def test_opacity_out_of_range_rejected(self) -> None:
        with self.assertRaises(ProposalError):
            parse_proposal(
                "layer_style",
                style_json(renderer_dict("keep", opacity=2.0), labels_dict()),
            )

    def test_enabled_labels_require_field(self) -> None:
        with self.assertRaises(ProposalError):
            parse_proposal(
                "layer_style",
                style_json(renderer_dict("keep"), labels_dict(enabled=True, field="")),
            )

    def test_disabled_labels_require_empty_field(self) -> None:
        with self.assertRaises(ProposalError):
            parse_proposal(
                "layer_style",
                style_json(renderer_dict("keep"), labels_dict(enabled=False, field="name")),
            )

    def test_unknown_family_rejected(self) -> None:
        with self.assertRaises(ProposalError):
            parse_proposal(
                "layer_style",
                style_json(renderer_dict("heatmap"), labels_dict()),
            )

    def test_to_dict_detached(self) -> None:
        proposal = parse_proposal(
            "layer_style",
            style_json(renderer_dict("single_symbol", class_count=1, palette=["#00ff00"]), labels_dict()),
        )
        one = proposal.to_dict()
        one["renderer"]["palette"].append("#ffffff")
        self.assertEqual(len(proposal.to_dict()["renderer"]["palette"]), 1)


# -- detached model-patch validation ---------------------------------------


class ModelPatchApplyTests(unittest.TestCase):
    def _preview(self, base, proposal, catalog=None):
        return build_model_patch_preview(
            base,
            proposal,
            catalog or FakeCatalog(),
            clone_fn=copy.deepcopy,
            max_nodes=80,
            max_edges=240,
        )

    def test_no_model_context_builds_a_detached_new_graph(self) -> None:
        proposal = parse_proposal(
            "model_patch",
            model_patch_json(
                [
                    add_node_op("src", "smart:input_layer", "Source", layer_param("L_vec")),
                    ADD_BUFFER,
                    connect_op("src", "b2"),
                ]
            ),
        )
        preview = self._preview(GraphModel("New"), proposal)
        self.assertEqual(preview["candidate_node_count"], 2)
        self.assertEqual(preview["candidate_edge_count"], 1)

    def test_add_and_connect_on_populated_graph_leaves_original_unchanged(self) -> None:
        base = make_two_node_graph()
        before = agent_context.canonical_model_state(base)
        proposal = parse_proposal(
            "model_patch",
            model_patch_json(
                [add_node_op("cen", "native:centroid", "Centroid"), connect_op("buf", "cen")]
            ),
        )
        preview = self._preview(base, proposal)
        self.assertEqual(preview["candidate_node_count"], 3)
        self.assertEqual(agent_context.canonical_model_state(base), before)

    def test_remove_node_marked_destructive(self) -> None:
        base = make_two_node_graph()
        proposal = parse_proposal(
            "model_patch", model_patch_json([{"op": "remove_node", "node_id": "buf"}])
        )
        preview = self._preview(base, proposal)
        self.assertTrue(preview["destructive"])
        self.assertTrue(preview["operations"][0]["destructive"])
        self.assertIn("buf", base.nodes)  # original untouched

    def test_disconnect_and_rename_and_metadata(self) -> None:
        base = make_two_node_graph()
        edge_id = next(iter(base.edges))
        proposal = parse_proposal(
            "model_patch",
            model_patch_json(
                [
                    {"op": "disconnect", "edge_id": edge_id},
                    {"op": "rename_node", "node_id": "buf", "title": "Renamed"},
                    {"op": "set_model_metadata", "name": "Better", "description": "d"},
                ]
            ),
        )
        preview = self._preview(base, proposal)
        self.assertEqual(preview["candidate_edge_count"], 0)
        self.assertEqual(len(base.edges), 1)  # original edge intact

    def test_unavailable_algorithm_rejected(self) -> None:
        proposal = parse_proposal(
            "model_patch",
            model_patch_json([add_node_op("x", "native:doesnotexist", "X")]),
        )
        with self.assertRaises(ProposalError) as ctx:
            self._preview(GraphModel("g"), proposal)
        self.assertEqual(ctx.exception.reason_code, ProposalReason.VALIDATION_FAILED)

    def test_restricted_algorithm_rejected(self) -> None:
        _ALGORITHMS["native:command_run"] = {"inputs": [], "outputs": [("OUTPUT", SocketType.ANY)]}
        try:
            proposal = parse_proposal(
                "model_patch",
                model_patch_json([add_node_op("x", "native:command_run", "X")]),
            )
            with self.assertRaises(ProposalError):
                self._preview(GraphModel("g"), proposal)
        finally:
            del _ALGORITHMS["native:command_run"]

    def test_duplicate_node_id_rejected(self) -> None:
        base = make_two_node_graph()
        proposal = parse_proposal(
            "model_patch",
            model_patch_json([add_node_op("buf", "native:buffer", "Dup")]),
        )
        with self.assertRaises(ProposalError):
            self._preview(base, proposal)

    def test_unknown_node_for_set_parameter_rejected(self) -> None:
        proposal = parse_proposal(
            "model_patch",
            model_patch_json([{"op": "set_parameter", "node_id": "ghost", "name": "X", "value": 1}]),
        )
        with self.assertRaises(ProposalError):
            self._preview(GraphModel("g"), proposal)

    def test_unknown_parameter_rejected(self) -> None:
        base = make_two_node_graph()
        proposal = parse_proposal(
            "model_patch",
            model_patch_json([{"op": "set_parameter", "node_id": "buf", "name": "NOPE", "value": 1}]),
        )
        with self.assertRaises(ProposalError):
            self._preview(base, proposal)

    def test_file_parameter_rejected(self) -> None:
        proposal = parse_proposal(
            "model_patch",
            model_patch_json(
                [add_node_op("f", "native:withfile", "F", [{"name": "FILEIN", "value": "/etc/passwd"}])]
            ),
        )
        with self.assertRaises(ProposalError) as ctx:
            self._preview(GraphModel("g"), proposal)
        self.assertEqual(ctx.exception.reason_code, ProposalReason.VALIDATION_FAILED)

    def test_invalid_project_layer_binding_rejected(self) -> None:
        proposal = parse_proposal(
            "model_patch",
            model_patch_json(
                [add_node_op("s", "smart:input_layer", "S", layer_param("not_a_layer"))]
            ),
        )
        with self.assertRaises(ProposalError):
            self._preview(GraphModel("g"), proposal)

    def test_incompatible_socket_connection_rejected(self) -> None:
        proposal = parse_proposal(
            "model_patch",
            model_patch_json(
                [
                    add_node_op("r", "smart:raster_layer", "R"),
                    add_node_op("b2", "native:buffer", "B"),
                    connect_op("r", "b2"),
                ]
            ),
        )
        with self.assertRaises(ProposalError):
            self._preview(GraphModel("g"), proposal)

    def test_cycle_connection_rejected(self) -> None:
        proposal = parse_proposal(
            "model_patch",
            model_patch_json(
                [
                    add_node_op("a", "native:buffer", "A"),
                    add_node_op("b", "native:buffer", "B"),
                    connect_op("a", "b"),
                    connect_op("b", "a"),
                ]
            ),
        )
        with self.assertRaises(ProposalError):
            self._preview(GraphModel("g"), proposal)

    def test_duplicate_connection_into_single_input_rejected(self) -> None:
        proposal = parse_proposal(
            "model_patch",
            model_patch_json(
                [
                    add_node_op("s1", "smart:input_layer", "S1", layer_param("L_vec")),
                    add_node_op("s2", "smart:input_layer", "S2", layer_param("L_vec")),
                    add_node_op("b2", "native:buffer", "B"),
                    {"op": "connect", "from_node": "s1", "from_output": "OUTPUT", "to_node": "b2", "to_input": "INPUT"},
                    {"op": "connect", "from_node": "s2", "from_output": "OUTPUT", "to_node": "b2", "to_input": "INPUT"},
                ]
            ),
        )
        with self.assertRaises(ProposalError):
            self._preview(GraphModel("g"), proposal)

    def test_candidate_validation_issues_are_reported(self) -> None:
        # A buffer with no configured INPUT is structurally valid but incomplete.
        proposal = parse_proposal(
            "model_patch",
            model_patch_json([add_node_op("b2", "native:buffer", "B")]),
        )
        preview = self._preview(GraphModel("g"), proposal)
        self.assertTrue(preview["incomplete"])
        self.assertTrue(any("Required input" in issue for issue in preview["validation_issues"]))

    def test_max_nodes_bound(self) -> None:
        ops = [
            {"op": "add_node", "node_id": f"n{i}", "algorithm_id": "native:buffer", "title": f"N{i}", "parameters": []}
            for i in range(3)
        ]
        proposal = parse_proposal("model_patch", model_patch_json(ops))
        with self.assertRaises(ProposalError) as ctx:
            build_model_patch_preview(
                GraphModel("g"), proposal, FakeCatalog(), clone_fn=copy.deepcopy, max_nodes=2, max_edges=240
            )
        self.assertEqual(ctx.exception.reason_code, ProposalReason.LIMIT_EXCEEDED)

    def test_original_unchanged_after_failure_at_every_operation_position(self) -> None:
        base = make_two_node_graph()
        before = agent_context.canonical_model_state(base)
        # A three-op patch whose failing op is placed first, middle, then last.
        good_a = {"op": "rename_node", "node_id": "buf", "title": "Rename A"}
        good_b = {"op": "set_parameter", "node_id": "buf", "name": "DISTANCE", "value": 9}
        bad = {"op": "remove_node", "node_id": "ghost"}
        for ordering in ([bad, good_a, good_b], [good_a, bad, good_b], [good_a, good_b, bad]):
            proposal = parse_proposal("model_patch", model_patch_json(ordering))
            with self.assertRaises(ProposalError):
                self._preview(base, proposal)
            self.assertEqual(agent_context.canonical_model_state(base), before)


class ParameterSocketValidationTests(unittest.TestCase):
    """P3-R1-002: parameter values must match the target input socket and never
    carry path/URI/connection/credential material, for native inputs too."""

    def _set(self, name, value, node_id="m"):
        graph = GraphModel("Multi")
        graph.add_node(FakeCatalog().create_node("native:multi", "m", "Multi"))
        proposal = parse_proposal(
            "model_patch",
            model_patch_json([{"op": "set_parameter", "node_id": node_id, "name": name, "value": value}]),
        )
        return build_model_patch_preview(
            graph, proposal, FakeCatalog(), clone_fn=copy.deepcopy, max_nodes=80, max_edges=240
        )

    def _assert_accepts(self, name, value):
        self._set(name, value)  # must not raise

    def _assert_rejects(self, name, value):
        with self.assertRaises(ProposalError) as ctx:
            self._set(name, value)
        self.assertEqual(ctx.exception.reason_code, ProposalReason.VALIDATION_FAILED)
        # The rejected raw value must not appear in the failure message.
        self.assertNotIn(str(value), str(ctx.exception))

    def test_number_socket(self) -> None:
        self._assert_accepts("NUM", 5)
        self._assert_accepts("NUM", 2.5)
        self._assert_rejects("NUM", True)
        self._assert_rejects("NUM", "5")
        self._assert_rejects("NUM", "C:\\Sensitive\\secret.txt")

    def test_boolean_socket(self) -> None:
        self._assert_accepts("FLAG", True)
        self._assert_rejects("FLAG", 1)
        self._assert_rejects("FLAG", "true")

    def test_string_socket_rejects_path_uri_connection_credential(self) -> None:
        self._assert_accepts("TXT", "a normal label")
        self._assert_rejects("TXT", "C:\\Users\\secret.txt")
        self._assert_rejects("TXT", "/etc/passwd")
        self._assert_rejects("TXT", "\\\\server\\share")
        self._assert_rejects("TXT", "https://example.com/x")
        self._assert_rejects("TXT", "file:///c:/x")
        self._assert_rejects("TXT", "host=db;password=hunter2")
        self._assert_rejects("TXT", "password")

    def test_field_socket(self) -> None:
        self._assert_accepts("FLD", "population")
        self._assert_rejects("FLD", "/etc/passwd")
        self._assert_rejects("FLD", 5)

    def test_vector_and_raster_socket_layer_binding(self) -> None:
        self._assert_accepts("VEC", "L_vec")
        self._assert_accepts("RAS", "L_ras")
        self._assert_accepts("VEC", "")  # empty clears the input safely
        self._assert_rejects("VEC", "ghost")
        self._assert_rejects("VEC", "L_ras")  # wrong kind
        self._assert_rejects("VEC", ["L_vec"])  # list for a single input
        self._assert_rejects("VEC", "C:\\path\\layer")

    def test_multiple_layers_socket(self) -> None:
        self._assert_accepts("MULTI", ["L_vec"])
        self._assert_rejects("MULTI", "L_vec")  # string for a multiple input
        self._assert_rejects("MULTI", ["ghost"])
        self._assert_rejects("MULTI", [])

    def test_any_socket_conservative_subset(self) -> None:
        self._assert_accepts("ANYIN", 3)
        self._assert_accepts("ANYIN", "safe text")
        self._assert_rejects("ANYIN", "https://x/y")
        self._assert_rejects("ANYIN", ["a", "b"])

    def test_credential_like_parameter_name_rejected(self) -> None:
        self._assert_rejects("password", "x")
        self._assert_rejects("api_key", "x")

    def test_native_layer_input_via_add_node_is_validated(self) -> None:
        # A native algorithm's own VECTOR input must resolve to a live layer.
        proposal = parse_proposal(
            "model_patch",
            model_patch_json(
                [add_node_op("b", "native:buffer", "B", [{"name": "INPUT", "value": "ghost"}])]
            ),
        )
        with self.assertRaises(ProposalError):
            build_model_patch_preview(
                GraphModel("g"), proposal, FakeCatalog(), clone_fn=copy.deepcopy, max_nodes=80, max_edges=240
            )


class OffPortAndUriParameterTests(unittest.TestCase):
    """P3-R2-001: unknown/off-contract parameter names and non-``//`` URI schemes
    must fail closed, and no rejected name/value may appear in the error."""

    def _set_param(self, node_id, name, value, algorithm_id="native:multi"):
        graph = GraphModel("g")
        graph.add_node(FakeCatalog().create_node(algorithm_id, node_id, "N"))
        proposal = parse_proposal(
            "model_patch",
            model_patch_json([{"op": "set_parameter", "node_id": node_id, "name": name, "value": value}]),
        )
        return build_model_patch_preview(
            graph, proposal, FakeCatalog(), clone_fn=copy.deepcopy, max_nodes=80, max_edges=240
        )

    def _add_param(self, algorithm_id, name, value):
        proposal = parse_proposal(
            "model_patch",
            model_patch_json([add_node_op("n1", algorithm_id, "N", [{"name": name, "value": value}])]),
        )
        return build_model_patch_preview(
            GraphModel("g"), proposal, FakeCatalog(), clone_fn=copy.deepcopy, max_nodes=80, max_edges=240
        )

    def _assert_set_rejects(self, name, value, algorithm_id="native:multi"):
        with self.assertRaises(ProposalError) as ctx:
            self._set_param("m", name, value, algorithm_id)
        self.assertEqual(ctx.exception.reason_code, ProposalReason.VALIDATION_FAILED)
        self.assertNotIn(str(name), str(ctx.exception))
        self.assertNotIn(str(value), str(ctx.exception))

    def test_off_port_layer_value_rejected_on_native_node_set_parameter(self) -> None:
        # The special strings LAYER/VALUE are not permitted on a native node.
        self._assert_set_rejects("LAYER", "attacker-controlled")
        self._assert_set_rejects("VALUE", "attacker-controlled")

    def test_off_port_layer_value_rejected_on_native_node_add_node(self) -> None:
        for name in ("LAYER", "VALUE"):
            with self.assertRaises(ProposalError) as ctx:
                self._add_param("native:multi", name, "attacker-controlled")
            self.assertEqual(ctx.exception.reason_code, ProposalReason.VALIDATION_FAILED)
            self.assertNotIn(name, str(ctx.exception))

    def test_smart_node_off_port_parameter_still_accepted(self) -> None:
        # The exact trusted smart:* off-port contract remains valid.
        self._add_param("smart:input_layer", "LAYER", "L_vec")  # must not raise
        self._add_param("smart:number", "VALUE", 5)  # must not raise

    def test_non_slash_uri_schemes_rejected(self) -> None:
        for uri in ("mailto:user@example.com", "ssh:host/path", "urn:example:opaque", "data:text/plain;base64,AA"):
            self._assert_set_rejects("TXT", uri)
            self._assert_set_rejects("ANYIN", uri)

    def test_unknown_path_shaped_name_gives_fixed_safe_error(self) -> None:
        # A path-shaped unknown parameter name must never be echoed in the error.
        secret_name = "C:\\Sensitive\\field.txt"
        with self.assertRaises(ProposalError) as ctx:
            self._set_param("m", secret_name, "x")
        message = str(ctx.exception)
        self.assertEqual(ctx.exception.reason_code, ProposalReason.VALIDATION_FAILED)
        self.assertNotIn("Sensitive", message)
        self.assertNotIn(secret_name, message)
        self.assertNotIn("native:multi", message)


class WhitespaceControlAndRelativePathParameterTests(unittest.TestCase):
    """P3-R3-001: leading whitespace/control content and ordinary relative-path
    forms must fail closed on both the STRING and ANY parameter sockets, and no
    rejected value may appear in the failure message."""

    def _set(self, name, value):
        graph = GraphModel("g")
        graph.add_node(FakeCatalog().create_node("native:multi", "m", "N"))
        proposal = parse_proposal(
            "model_patch",
            model_patch_json([{"op": "set_parameter", "node_id": "m", "name": name, "value": value}]),
        )
        return build_model_patch_preview(
            graph, proposal, FakeCatalog(), clone_fn=copy.deepcopy, max_nodes=80, max_edges=240
        )

    def _assert_rejects_on_both_sockets(self, value):
        for socket_name in ("TXT", "ANYIN"):  # STRING socket and ANY socket
            with self.assertRaises(ProposalError) as ctx:
                self._set(socket_name, value)
            self.assertEqual(ctx.exception.reason_code, ProposalReason.VALIDATION_FAILED)
            # The rejected raw value must never be echoed in the error.
            self.assertNotIn(value, str(ctx.exception))
            self.assertNotIn("Sensitive", str(ctx.exception))

    def test_leading_whitespace_uri_scheme_rejected(self) -> None:
        self._assert_rejects_on_both_sockets(" mailto:user@example.com")

    def test_leading_whitespace_absolute_windows_path_rejected(self) -> None:
        self._assert_rejects_on_both_sockets(" C:\\Sensitive\\file.gpkg")

    def test_relative_forward_slash_path_rejected(self) -> None:
        self._assert_rejects_on_both_sockets("folder/private.gpkg")

    def test_relative_backslash_path_rejected(self) -> None:
        self._assert_rejects_on_both_sockets("folder\\private.gpkg")

    def test_embedded_newline_with_path_rejected(self) -> None:
        self._assert_rejects_on_both_sockets("normal\nC:\\Sensitive\\file.gpkg")

    def test_ordinary_text_with_internal_spaces_still_accepted(self) -> None:
        # A normal multi-word label with internal spaces must remain valid.
        self._set("TXT", "a normal city boundary label")  # must not raise
        self._set("ANYIN", "another safe label with spaces")  # must not raise


class StrictScalarContractTests(unittest.TestCase):
    """P3-R1-004: exact schema_version and warning bounds, no coercion."""

    def _model_patch(self, schema_version=1, warnings=None):
        payload = {
            "schema_version": schema_version,
            "context_token": "tok",
            "title": "T",
            "summary": "S",
            "operations": [{"op": "set_model_metadata", "name": "N", "description": "d"}],
            "warnings": warnings if warnings is not None else [],
        }
        return json.dumps(payload)

    def _layer_style(self, schema_version=1, warnings=None):
        payload = {
            "schema_version": schema_version,
            "context_token": "tok",
            "target_layer_id": "L_vec",
            "title": "T",
            "summary": "S",
            "renderer": renderer_dict("keep"),
            "labels": labels_dict(),
            "warnings": warnings if warnings is not None else [],
        }
        return json.dumps(payload)

    def test_schema_version_must_be_integer_one(self) -> None:
        parse_proposal("model_patch", self._model_patch(schema_version=1))
        parse_proposal("layer_style", self._layer_style(schema_version=1))
        for bad in (1.0, True, "1", 2):
            with self.assertRaises(ProposalError):
                parse_proposal("model_patch", self._model_patch(schema_version=bad))
            with self.assertRaises(ProposalError):
                parse_proposal("layer_style", self._layer_style(schema_version=bad))

    def test_warning_length_boundary(self) -> None:
        parse_proposal("model_patch", self._model_patch(warnings=["x" * 500]))
        parse_proposal("layer_style", self._layer_style(warnings=["x" * 500]))
        with self.assertRaises(ProposalError) as ctx:
            parse_proposal("model_patch", self._model_patch(warnings=["x" * 501]))
        self.assertEqual(ctx.exception.reason_code, ProposalReason.LIMIT_EXCEEDED)
        with self.assertRaises(ProposalError):
            parse_proposal("layer_style", self._layer_style(warnings=["x" * 501]))


if __name__ == "__main__":
    unittest.main()
