"""QGIS-free tests for the pure run planner.

Every security decision for an approved execution lives in
``core/agent/run_planner.py``: which parameters a proposal may bind, whether a
tagged binding may satisfy the live parameter kind, whether the referenced
layer/field/choice actually exists, and whether the current graph may run at
all. These tests build the small immutable views the QGIS adapter would
otherwise supply, so the policy is proven without a Processing registry.
"""
from __future__ import annotations

import json
import unittest

from planx_smartmodeler.core.agent.proposals import (
    PROPOSAL_KIND_PROCESSING_RUN,
    ProposalError,
    ProposalReason,
    parse_proposal,
)
from planx_smartmodeler.core.agent.run_planner import (
    LayerView,
    RASTER,
    VECTOR,
    plan_model_run,
    plan_processing_run,
)
from planx_smartmodeler.core.agent.safe_algorithm_policy import (
    ParamSpec,
    SafeAlgorithmPolicy,
    default_policy,
)
from planx_smartmodeler.core.graph_model import GraphModel, NodeDefinition, SocketType

SOURCE = "QgsProcessingParameterFeatureSource"
RASTER_PARAM = "QgsProcessingParameterRasterLayer"
MULTI = "QgsProcessingParameterMultipleLayers"
FIELD_PARAM = "QgsProcessingParameterField"
NUMBER_PARAM = "QgsProcessingParameterNumber"
DISTANCE_PARAM = "QgsProcessingParameterDistance"
BOOL_PARAM = "QgsProcessingParameterBoolean"
ENUM_PARAM = "QgsProcessingParameterEnum"
CRS_PARAM = "QgsProcessingParameterCrs"
STRING_PARAM = "QgsProcessingParameterString"
SINK = "QgsProcessingParameterFeatureSink"


def spec(
    name,
    class_name,
    *,
    destination=False,
    optional=False,
    default=False,
    options=(),
    minimum=None,
    maximum=None,
):
    type_names = {class_name}
    if class_name == DISTANCE_PARAM:
        type_names.add(NUMBER_PARAM)
    return ParamSpec(
        name=name,
        is_destination=destination,
        type_names=frozenset(type_names),
        is_optional=optional,
        has_default=default,
        options=tuple(options),
        minimum=minimum,
        maximum=maximum,
    )


BUFFER_PARAMS = [
    spec("INPUT", SOURCE),
    spec("DISTANCE", DISTANCE_PARAM, default=True, minimum=-1e308, maximum=1e308),
    spec("SEGMENTS", NUMBER_PARAM, default=True, minimum=1.0, maximum=1e308),
    spec("DISSOLVE", BOOL_PARAM, default=True),
    spec("OUTPUT", SINK, destination=True),
]
DISSOLVE_PARAMS = [
    spec("INPUT", SOURCE),
    spec("FIELD", FIELD_PARAM, optional=True),
    spec("OUTPUT", SINK, destination=True),
]
FIX_PARAMS = [
    spec("INPUT", SOURCE),
    spec("METHOD", ENUM_PARAM, default=True, options=("Linework", "Structure")),
    spec("OUTPUT", SINK, destination=True),
]
REPROJECT_PARAMS = [
    spec("INPUT", SOURCE),
    spec("TARGET_CRS", CRS_PARAM, default=True),
    spec("OUTPUT", SINK, destination=True),
]
COUNT_PARAMS = [
    spec("POLYGONS", SOURCE),
    spec("POINTS", SOURCE),
    spec("FIELD", STRING_PARAM, default=True),
    spec("OUTPUT", SINK, destination=True),
]
CELLSTATS_PARAMS = [
    spec("INPUT", MULTI),
    spec("REFERENCE_LAYER", RASTER_PARAM),
    spec("STATISTIC", ENUM_PARAM, default=True, options=("Sum", "Mean")),
    spec("IGNORE_NODATA", BOOL_PARAM, default=True),
    spec("OUTPUT_NODATA_VALUE", NUMBER_PARAM, default=True),
    spec("OUTPUT", "QgsProcessingParameterRasterDestination", destination=True),
]

VEC = LayerView("L_vec", "Roads", VECTOR, frozenset({"name", "class"}))
VEC2 = LayerView("L_vec2", "Districts", VECTOR, frozenset({"code"}))
RAS = LayerView("L_ras", "Elevation", RASTER)
RAS2 = LayerView("L_ras2", "Slope", RASTER)
_LAYERS = {view.layer_id: view for view in (VEC, VEC2, RAS, RAS2)}


def lookup(layer_id):
    return _LAYERS.get(layer_id)


def proposal(algorithm_id, inputs, token="tok"):
    return parse_proposal(
        PROPOSAL_KIND_PROCESSING_RUN,
        json.dumps(
            {
                "schema_version": 1,
                "context_token": token,
                "algorithm_id": algorithm_id,
                "title": "Run it",
                "summary": "A reviewed run.",
                "inputs": inputs,
                "warnings": [],
            }
        ),
    )


def record(algorithm_id):
    return default_policy().record_for(algorithm_id)


class ProcessingRunPlannerTests(unittest.TestCase):
    def plan(self, algorithm_id, inputs, params, **kwargs):
        return plan_processing_run(
            proposal(algorithm_id, inputs),
            default_policy(),
            record(algorithm_id),
            params,
            lookup,
            **kwargs,
        )

    def assert_rejects(self, algorithm_id, inputs, params, reason, **kwargs):
        with self.assertRaises(ProposalError) as caught:
            self.plan(algorithm_id, inputs, params, **kwargs)
        self.assertEqual(caught.exception.reason_code, reason)

    # -- happy paths -------------------------------------------------------

    def test_buffer_plan_resolves_layer_and_scalars(self):
        plan = self.plan(
            "native:buffer",
            {"INPUT": {"layer": "L_vec"}, "DISTANCE": {"distance": 25}, "DISSOLVE": {"bool": True}},
            BUFFER_PARAMS,
        )
        self.assertEqual(plan.algorithm_id, "native:buffer")
        self.assertEqual(plan.destinations, ("OUTPUT",))
        self.assertEqual(plan.input_layer_ids, ("L_vec",))
        self.assertEqual(plan.binding_for("INPUT").layer_ids, ("L_vec",))
        self.assertEqual(plan.binding_for("DISTANCE").value, 25)
        self.assertIs(plan.binding_for("DISSOLVE").value, True)

    def test_plan_never_contains_a_destination_binding(self):
        plan = self.plan("native:buffer", {"INPUT": {"layer": "L_vec"}}, BUFFER_PARAMS)
        self.assertIsNone(plan.binding_for("OUTPUT"))

    def test_field_binding_resolves_against_its_named_input_layer(self):
        plan = self.plan(
            "native:dissolve",
            {"INPUT": {"layer": "L_vec"}, "FIELD": {"field": "class", "layer_param": "INPUT"}},
            DISSOLVE_PARAMS,
        )
        self.assertEqual(plan.binding_for("FIELD").value, "class")

    def test_enum_string_is_resolved_to_a_live_option_index(self):
        plan = self.plan(
            "native:fixgeometries",
            {"INPUT": {"layer": "L_vec"}, "METHOD": {"enum_string": "structure"}},
            FIX_PARAMS,
        )
        self.assertEqual(plan.binding_for("METHOD").value, 1)

    def test_multiple_raster_binding_is_accepted_for_cellstatistics(self):
        plan = self.plan(
            "native:cellstatistics",
            {
                "INPUT": {"layers": ["L_ras", "L_ras2"]},
                "REFERENCE_LAYER": {"layer": "L_ras"},
                "STATISTIC": {"enum": 1},
            },
            CELLSTATS_PARAMS,
        )
        self.assertEqual(plan.binding_for("INPUT").layer_ids, ("L_ras", "L_ras2"))
        self.assertEqual(plan.input_layer_ids, ("L_ras", "L_ras2"))

    def test_preview_lines_name_layers_not_identifiers_or_paths(self):
        plan = self.plan(
            "native:buffer",
            {"INPUT": {"layer": "L_vec"}, "DISTANCE": {"distance": 25}},
            BUFFER_PARAMS,
        )
        text = "\n".join(plan.preview_lines)
        self.assertIn("Roads", text)
        self.assertNotIn("L_vec", text)

    # -- unbindable / unsafe parameters ------------------------------------

    def test_binding_the_output_destination_is_rejected(self):
        self.assert_rejects(
            "native:buffer",
            {"INPUT": {"layer": "L_vec"}, "OUTPUT": {"string": "result"}},
            BUFFER_PARAMS,
            ProposalReason.UNSAFE_PARAMETER,
        )

    def test_binding_an_unreviewed_parameter_is_rejected(self):
        # CREATE_OPTIONS exists live but is deliberately not in the allowlist.
        params = CELLSTATS_PARAMS + [spec("CREATE_OPTIONS", STRING_PARAM, optional=True)]
        self.assert_rejects(
            "native:cellstatistics",
            {
                "INPUT": {"layers": ["L_ras"]},
                "REFERENCE_LAYER": {"layer": "L_ras"},
                "CREATE_OPTIONS": {"string": "COMPRESS=DEFLATE"},
            },
            params,
            ProposalReason.UNSAFE_PARAMETER,
        )

    def test_a_string_cannot_be_bound_to_a_layer_parameter(self):
        self.assert_rejects(
            "native:buffer",
            {"INPUT": {"string": "roads"}},
            BUFFER_PARAMS,
            ProposalReason.UNSAFE_PARAMETER,
        )

    def test_a_number_cannot_be_bound_to_a_field_parameter(self):
        self.assert_rejects(
            "native:dissolve",
            {"INPUT": {"layer": "L_vec"}, "FIELD": {"number": 3}},
            DISSOLVE_PARAMS,
            ProposalReason.UNSAFE_PARAMETER,
        )

    def test_a_retyped_parameter_is_a_signature_mismatch(self):
        params = [
            spec("INPUT", SOURCE),
            spec("DISTANCE", STRING_PARAM, default=True),
            spec("OUTPUT", SINK, destination=True),
        ]
        self.assert_rejects(
            "native:buffer",
            {"INPUT": {"layer": "L_vec"}, "DISTANCE": {"distance": 5}},
            params,
            ProposalReason.SIGNATURE_MISMATCH,
        )

    def test_a_parameter_absent_from_the_live_algorithm_is_rejected(self):
        params = [spec("INPUT", SOURCE), spec("OUTPUT", SINK, destination=True)]
        self.assert_rejects(
            "native:buffer",
            {"INPUT": {"layer": "L_vec"}, "DISTANCE": {"distance": 5}},
            params,
            ProposalReason.SIGNATURE_MISMATCH,
        )

    # -- live-state resolution --------------------------------------------

    def test_an_unknown_layer_id_is_rejected(self):
        self.assert_rejects(
            "native:buffer",
            {"INPUT": {"layer": "L_missing"}},
            BUFFER_PARAMS,
            ProposalReason.TARGET_MISSING,
        )

    def test_a_raster_cannot_satisfy_a_vector_input(self):
        self.assert_rejects(
            "native:buffer",
            {"INPUT": {"layer": "L_ras"}},
            BUFFER_PARAMS,
            ProposalReason.VALIDATION_FAILED,
        )

    def test_a_vector_cannot_satisfy_a_multiple_raster_input(self):
        self.assert_rejects(
            "native:cellstatistics",
            {"INPUT": {"layers": ["L_vec"]}, "REFERENCE_LAYER": {"layer": "L_ras"}},
            CELLSTATS_PARAMS,
            ProposalReason.VALIDATION_FAILED,
        )

    def test_a_field_missing_from_the_bound_layer_is_rejected(self):
        self.assert_rejects(
            "native:dissolve",
            {"INPUT": {"layer": "L_vec"}, "FIELD": {"field": "nope", "layer_param": "INPUT"}},
            DISSOLVE_PARAMS,
            ProposalReason.VALIDATION_FAILED,
        )

    def test_a_field_bound_to_a_parameter_that_is_not_an_input_layer_is_rejected(self):
        self.assert_rejects(
            "native:dissolve",
            {"INPUT": {"layer": "L_vec"}, "FIELD": {"field": "class", "layer_param": "OUTPUT"}},
            DISSOLVE_PARAMS,
            ProposalReason.VALIDATION_FAILED,
        )

    def test_an_enum_index_outside_the_live_options_is_rejected(self):
        self.assert_rejects(
            "native:fixgeometries",
            {"INPUT": {"layer": "L_vec"}, "METHOD": {"enum": 7}},
            FIX_PARAMS,
            ProposalReason.VALIDATION_FAILED,
        )

    def test_an_unknown_enum_label_is_rejected(self):
        self.assert_rejects(
            "native:fixgeometries",
            {"INPUT": {"layer": "L_vec"}, "METHOD": {"enum_string": "magic"}},
            FIX_PARAMS,
            ProposalReason.VALIDATION_FAILED,
        )

    def test_a_number_below_the_live_minimum_is_rejected(self):
        self.assert_rejects(
            "native:buffer",
            {"INPUT": {"layer": "L_vec"}, "SEGMENTS": {"number": 0}},
            BUFFER_PARAMS,
            ProposalReason.VALIDATION_FAILED,
        )

    def test_an_over_long_label_string_is_rejected(self):
        self.assert_rejects(
            "native:countpointsinpolygon",
            {
                "POLYGONS": {"layer": "L_vec"},
                "POINTS": {"layer": "L_vec2"},
                "FIELD": {"string": "n" * 400},
            },
            COUNT_PARAMS,
            ProposalReason.LIMIT_EXCEEDED,
        )

    def test_a_missing_required_input_is_rejected(self):
        self.assert_rejects(
            "native:countpointsinpolygon",
            {"POLYGONS": {"layer": "L_vec"}},
            COUNT_PARAMS,
            ProposalReason.VALIDATION_FAILED,
        )

    def test_a_crs_binding_is_carried_through_for_the_adapter_to_validate(self):
        plan = self.plan(
            "native:reprojectlayer",
            {"INPUT": {"layer": "L_vec"}, "TARGET_CRS": {"crs": "EPSG:3857"}},
            REPROJECT_PARAMS,
        )
        self.assertEqual(plan.binding_for("TARGET_CRS").value, "EPSG:3857")

    # -- active-layer scope ------------------------------------------------

    def test_active_layer_scope_requires_the_primary_input_to_be_active(self):
        plan = self.plan(
            "native:buffer",
            {"INPUT": {"layer": "L_vec"}},
            BUFFER_PARAMS,
            active_layer_id="L_vec",
            require_active_layer=True,
        )
        self.assertEqual(plan.input_layer_ids, ("L_vec",))

    def test_active_layer_scope_rejects_a_different_primary_input(self):
        self.assert_rejects(
            "native:buffer",
            {"INPUT": {"layer": "L_vec2"}},
            BUFFER_PARAMS,
            ProposalReason.TARGET_MISSING,
            active_layer_id="L_vec",
            require_active_layer=True,
        )

    def test_active_layer_scope_rejects_when_there_is_no_active_layer(self):
        self.assert_rejects(
            "native:buffer",
            {"INPUT": {"layer": "L_vec"}},
            BUFFER_PARAMS,
            ProposalReason.TARGET_MISSING,
            active_layer_id="",
            require_active_layer=True,
        )


def build_graph(*nodes):
    graph = GraphModel("Runnable")
    for node in nodes:
        graph.add_node(node)
    return graph


def processing_node(node_id, algorithm_id, parameters=None):
    node = NodeDefinition(node_id=node_id, title=node_id, algorithm_id=algorithm_id)
    node.add_input("INPUT", "INPUT", SocketType.VECTOR, required=False)
    node.add_output("OUTPUT", "OUTPUT", SocketType.VECTOR)
    node.parameters.update(parameters or {})
    return node


def smart_node(node_id, algorithm_id="smart:input_layer", parameters=None):
    node = NodeDefinition(node_id=node_id, title=node_id, algorithm_id=algorithm_id)
    node.add_output("OUTPUT", "OUTPUT", SocketType.VECTOR)
    node.parameters.update(parameters or {"LAYER": "L_vec"})
    return node


class ModelRunPlannerTests(unittest.TestCase):
    def setUp(self):
        self.policy = default_policy()
        self.params = {"native:buffer": BUFFER_PARAMS, "native:centroids": [
            spec("INPUT", SOURCE),
            spec("ALL_PARTS", BOOL_PARAM, default=True),
            spec("OUTPUT", SINK, destination=True),
        ]}

    def lookup(self, algorithm_id):
        return self.params.get(algorithm_id)

    def plan(self, graph, policy=None):
        return plan_model_run(graph, policy or self.policy, self.lookup)

    def assert_rejects(self, graph, reason, policy=None):
        with self.assertRaises(ProposalError) as caught:
            self.plan(graph, policy)
        self.assertEqual(caught.exception.reason_code, reason)

    def test_a_graph_of_allowlisted_nodes_plans(self):
        graph = build_graph(
            smart_node("src"), processing_node("buf", "native:buffer", {"DISTANCE": 5})
        )
        plan = self.plan(graph)
        self.assertEqual(plan.node_count, 2)
        self.assertEqual(plan.algorithm_ids, ("native:buffer",))

    def test_an_empty_graph_is_rejected(self):
        self.assert_rejects(GraphModel("Empty"), ProposalReason.VALIDATION_FAILED)

    def test_a_missing_graph_is_rejected(self):
        self.assert_rejects(None, ProposalReason.VALIDATION_FAILED)

    def test_a_node_outside_the_allowlist_is_rejected(self):
        self.params["native:pixelstopoints"] = [spec("INPUT", SOURCE)]
        graph = build_graph(smart_node("src"), processing_node("x", "native:pixelstopoints"))
        self.assert_rejects(graph, ProposalReason.ALGORITHM_NOT_ALLOWED)

    def test_a_node_absent_from_the_live_registry_is_rejected(self):
        graph = build_graph(smart_node("src"), processing_node("x", "native:buffer"))
        self.params.pop("native:buffer")
        self.assert_rejects(graph, ProposalReason.ALGORITHM_NOT_ALLOWED)

    def test_a_node_with_a_changed_signature_is_rejected(self):
        self.params["native:buffer"] = [
            spec("INPUT", SOURCE),
            spec("NEW_REQUIRED", STRING_PARAM),
            spec("OUTPUT", SINK, destination=True),
        ]
        graph = build_graph(smart_node("src"), processing_node("buf", "native:buffer"))
        self.assert_rejects(graph, ProposalReason.SIGNATURE_MISMATCH)

    def test_a_configured_file_destination_on_a_node_is_rejected(self):
        graph = build_graph(
            smart_node("src"),
            processing_node("buf", "native:buffer", {"OUTPUT": "C:/tmp/out.gpkg"}),
        )
        self.assert_rejects(graph, ProposalReason.UNSAFE_PARAMETER)

    def test_a_temporary_destination_on_a_node_is_accepted(self):
        graph = build_graph(
            smart_node("src"),
            processing_node("buf", "native:buffer", {"OUTPUT": "TEMPORARY_OUTPUT"}),
        )
        self.assertEqual(self.plan(graph).node_count, 2)

    def test_a_path_valued_node_parameter_is_rejected(self):
        graph = build_graph(
            smart_node("src"),
            processing_node("buf", "native:buffer", {"DISTANCE": "\\\\server\\share\\x"}),
        )
        self.assert_rejects(graph, ProposalReason.UNSAFE_PARAMETER)

    def test_a_smart_input_node_pointing_at_a_file_is_rejected(self):
        graph = build_graph(smart_node("src", parameters={"LAYER": "C:/data/roads.shp"}))
        self.assert_rejects(graph, ProposalReason.UNSAFE_PARAMETER)

    def test_a_narrowed_policy_rejects_a_previously_allowed_node(self):
        graph = build_graph(smart_node("src"), processing_node("buf", "native:buffer"))
        self.assert_rejects(
            graph, ProposalReason.ALGORITHM_NOT_ALLOWED, policy=SafeAlgorithmPolicy({})
        )


if __name__ == "__main__":
    unittest.main()
