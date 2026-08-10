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
EXTENT_PARAM = "QgsProcessingParameterExtent"
EXPRESSION_PARAM = "QgsProcessingParameterExpression"
FILE_DEST = "QgsProcessingParameterFileDestination"
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
    source_type="",
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
        source_type=source_type,
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
    spec("INPUT", MULTI, source_type="raster"),
    spec("REFERENCE_LAYER", RASTER_PARAM),
    spec("STATISTIC", ENUM_PARAM, default=True, options=("Sum", "Mean")),
    spec("IGNORE_NODATA", BOOL_PARAM, default=True),
    spec("OUTPUT_NODATA_VALUE", NUMBER_PARAM, default=True),
    spec("OUTPUT", "QgsProcessingParameterRasterDestination", destination=True),
]
EXTRACTLOC_PARAMS = [
    spec("INPUT", SOURCE),
    spec("PREDICATE", ENUM_PARAM, default=True,
         options=("intersect", "contain", "disjoint", "equal", "touch",
                  "overlap", "are within", "cross")),
    spec("INTERSECT", SOURCE),
    spec("OUTPUT", SINK, destination=True),
]
JOIN_PARAMS = [
    spec("INPUT", SOURCE),
    spec("FIELD", FIELD_PARAM),
    spec("INPUT_2", SOURCE),
    spec("FIELD_2", FIELD_PARAM),
    spec("FIELDS_TO_COPY", FIELD_PARAM, optional=True),
    spec("METHOD", ENUM_PARAM, default=True, options=("one-to-many", "one-to-one")),
    spec("DISCARD_NONMATCHING", BOOL_PARAM, default=True),
    spec("PREFIX", STRING_PARAM, optional=True),
    spec("OUTPUT", SINK, destination=True, optional=True),
    spec("NON_MATCHING", SINK, destination=True, optional=True),
]
MERGE_PARAMS = [
    spec("LAYERS", MULTI, source_type="vector"),
    spec("CRS", CRS_PARAM, optional=True),
    spec("OUTPUT", SINK, destination=True),
    spec("ADD_SOURCE_FIELDS", BOOL_PARAM, default=True),
]
QUICKOSM_PARAMS = [
    spec("KEY", STRING_PARAM),
    spec("VALUE", STRING_PARAM, optional=True),
    spec("TYPE_MULTI_REQUEST", STRING_PARAM, optional=True),
    spec("EXTENT", EXTENT_PARAM),
    spec("TIMEOUT", NUMBER_PARAM, default=True),
    spec("SERVER", STRING_PARAM, default=True),
    spec("FILE", FILE_DEST, destination=True, optional=True),
]
SMARTMODELER_OSM_PARAMS = [
    spec("KEY", STRING_PARAM),
    spec("VALUE", STRING_PARAM, optional=True, default=True),
    spec("EXTENT", EXTENT_PARAM),
    spec("OUTPUT", SINK, destination=True),
]
FIELD_CALCULATOR_PARAMS = [
    spec("INPUT", SOURCE),
    spec("FIELD_NAME", STRING_PARAM),
    spec(
        "FIELD_TYPE",
        ENUM_PARAM,
        default=True,
        options=("Float", "Integer", "String", "Date"),
    ),
    spec("FIELD_LENGTH", NUMBER_PARAM, default=True),
    spec("FIELD_PRECISION", NUMBER_PARAM, default=True),
    spec("FORMULA", EXPRESSION_PARAM),
    spec("OUTPUT", SINK, destination=True),
]

VEC = LayerView("L_vec", "Roads", VECTOR, frozenset({"name", "class"}))
VEC2 = LayerView("L_vec2", "Districts", VECTOR, frozenset({"code"}))
RAS = LayerView("L_ras", "Elevation", RASTER)
RAS2 = LayerView("L_ras2", "Slope", RASTER)
# The owner's failing session in miniature: OSM buildings still in Web
# Mercator, carrying an "alan_m2" that Field Calculator wrote as text.
MERCATOR = LayerView(
    "L_merc",
    "Buildings",
    VECTOR,
    frozenset({"alan_m2", "area_num", "name"}),
    field_types={
        "alan_m2": "string",
        "area_num": "integer",
        "name": "string",
    },
    area_safe_crs=False,
)
METRIC = LayerView(
    "L_utm",
    "Buildings UTM",
    VECTOR,
    frozenset({"alan_m2", "area_num"}),
    field_types={"alan_m2": "string", "area_num": "integer"},
    area_safe_crs=True,
)
_LAYERS = {
    view.layer_id: view for view in (VEC, VEC2, RAS, RAS2, MERCATOR, METRIC)
}


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

    def test_a_negative_buffer_distance_is_allowed(self):
        # Shrinking a polygon inward is an ordinary QGIS operation and the live
        # DISTANCE parameter's minimum really is -1.8e308, probed on 3.44 LTR
        # and 4.2. Refusing it at parse time made "-2 m buffer" inexpressible.
        plan = self.plan(
            "native:buffer",
            {"INPUT": {"layer": "L_vec"}, "DISTANCE": {"distance": -2}},
            BUFFER_PARAMS,
        )
        self.assertEqual(plan.binding_for("DISTANCE").value, -2)

    def test_a_parameter_with_a_real_floor_still_enforces_it(self):
        # SEGMENTS's live minimum is 1.0, so the live definition -- not a guess
        # made at parse time -- is what rejects a negative here.
        self.assert_rejects(
            "native:buffer",
            {"INPUT": {"layer": "L_vec"}, "SEGMENTS": {"number": -5}},
            BUFFER_PARAMS,
            ProposalReason.VALIDATION_FAILED,
        )

    def test_a_choice_label_matches_past_qgis_qualifiers(self):
        # Live QGIS labels carry qualifiers a model rarely reproduces exactly:
        # "Decimal (double)", "Quantile (Equal Count)". Requiring a byte-exact
        # match rejected correct choices ("A choice label does not match any
        # live option") on requests that were not wrong in any way.
        params = [
            spec("INPUT", SOURCE),
            spec(
                "METHOD",
                ENUM_PARAM,
                default=True,
                options=("Decimal (double)", "Integer (32 bit)", "Text (string)"),
            ),
            spec("OUTPUT", SINK, destination=True),
        ]
        for spoken, expected in (
            ("Decimal (double)", 0),
            ("decimal", 0),
            ("Decimal(double)", 0),
            ("text", 2),
            ("Text (string)", 2),
        ):
            with self.subTest(label=spoken):
                plan = self.plan(
                    "native:fixgeometries",
                    {"INPUT": {"layer": "L_vec"}, "METHOD": {"enum_string": spoken}},
                    params,
                )
                self.assertEqual(plan.binding_for("METHOD").value, expected)

    def test_an_ambiguous_choice_label_still_fails_closed(self):
        # "integer" prefixes two live options, so guessing would silently pick
        # a different one -- exactly what a label binding exists to prevent.
        params = [
            spec("INPUT", SOURCE),
            spec(
                "METHOD",
                ENUM_PARAM,
                default=True,
                options=("Integer (32 bit)", "Integer (64 bit)", "Text (string)"),
            ),
            spec("OUTPUT", SINK, destination=True),
        ]
        self.assert_rejects(
            "native:fixgeometries",
            {"INPUT": {"layer": "L_vec"}, "METHOD": {"enum_string": "integer"}},
            params,
            ProposalReason.VALIDATION_FAILED,
        )

    def test_plan_never_contains_a_destination_binding(self):
        plan = self.plan("native:buffer", {"INPUT": {"layer": "L_vec"}}, BUFFER_PARAMS)
        self.assertIsNone(plan.binding_for("OUTPUT"))

    def test_field_calculator_keeps_qgis_expression_as_a_typed_binding(self):
        plan = self.plan(
            "native:fieldcalculator",
            {
                "INPUT": {"layer": "L_vec"},
                "FIELD_NAME": {"string": "floors"},
                "FIELD_TYPE": {"enum_string": "Integer"},
                "FORMULA": {"expression": "rand(1, 15)"},
            },
            FIELD_CALCULATOR_PARAMS,
        )
        formula = plan.binding_for("FORMULA")
        self.assertEqual(formula.kind, "expression")
        self.assertEqual(formula.tag, "expression")
        self.assertEqual(formula.value, "rand(1, 15)")
        self.assertEqual(plan.binding_for("FIELD_TYPE").value, 1)

    def test_new_field_name_drops_surrounding_whitespace(self):
        # QGIS stores a new field name verbatim. A provider that echoes
        # "area_m2 " produced a run that reported success while the requested
        # "area_m2" did not exist on the result, so nothing downstream -- a
        # later stage or the person who asked -- could reach the value.
        plan = self.plan(
            "native:fieldcalculator",
            {
                "INPUT": {"layer": "L_vec"},
                "FIELD_NAME": {"string": "  area_m2 "},
                "FIELD_TYPE": {"enum_string": "Float"},
                "FORMULA": {"expression": "$area"},
            },
            FIELD_CALCULATOR_PARAMS,
        )
        self.assertEqual(plan.binding_for("FIELD_NAME").value, "area_m2")
        # The approval card must show the field that will really be created.
        self.assertIn("FIELD_NAME: area_m2", plan.preview_lines)

    def test_new_field_name_keeps_interior_spaces(self):
        # Interior spaces are legal in QGIS field names and may be exactly what
        # the user asked for; only the surrounding whitespace is noise.
        plan = self.plan(
            "native:fieldcalculator",
            {
                "INPUT": {"layer": "L_vec"},
                "FIELD_NAME": {"string": " net area "},
                "FIELD_TYPE": {"enum_string": "Float"},
                "FORMULA": {"expression": "$area"},
            },
            FIELD_CALCULATOR_PARAMS,
        )
        self.assertEqual(plan.binding_for("FIELD_NAME").value, "net area")

    def test_whitespace_only_new_field_name_is_rejected(self):
        self.assert_rejects(
            "native:fieldcalculator",
            {
                "INPUT": {"layer": "L_vec"},
                "FIELD_NAME": {"string": "   "},
                "FIELD_TYPE": {"enum_string": "Float"},
                "FORMULA": {"expression": "$area"},
            },
            FIELD_CALCULATOR_PARAMS,
            ProposalReason.VALIDATION_FAILED,
        )

    def test_ordinary_label_strings_are_not_normalized(self):
        # Only parameters that name a new field are normalized. A comparison
        # value is data, and trimming it would silently change the run.
        params = [
            spec("INPUT", SOURCE),
            spec("FIELD", FIELD_PARAM),
            spec("OPERATOR", ENUM_PARAM, default=True, options=("=", "!=")),
            spec("VALUE", STRING_PARAM, optional=True),
            spec("OUTPUT", SINK, destination=True),
        ]
        plan = self.plan(
            "native:extractbyattribute",
            {
                "INPUT": {"layer": "L_vec"},
                "FIELD": {"field": "name", "layer_param": "INPUT"},
                "VALUE": {"string": " keep me "},
            },
            params,
        )
        self.assertEqual(plan.binding_for("VALUE").value, " keep me ")

    def test_planx_space_syntax_accepts_reviewed_domain_text(self):
        params = [
            spec("NETWORK", SOURCE),
            spec("RADII", STRING_PARAM, default=True),
            spec("OUTPUT", SINK, destination=True),
        ]
        policy = default_policy()
        decision = policy.is_runnable("planx:spacesyntax", params)
        self.assertTrue(decision.allowed)
        plan = plan_processing_run(
            proposal(
                "planx:spacesyntax",
                {
                    "NETWORK": {"layer": "L_vec"},
                    "RADII": {"text": "400, 800, n"},
                },
            ),
            policy,
            decision.record,
            params,
            lookup,
        )
        self.assertEqual(plan.binding_for("RADII").value, "400, 800, n")
        self.assertEqual(plan.destinations, ("OUTPUT",))

    def test_quickosm_plan_uses_canvas_extent_and_pinned_network_settings(self):
        plan = self.plan(
            "quickosm:downloadosmdataextentquery",
            {
                "KEY": {"osm_tag": "building"},
                "EXTENT": {"map_extent": True},
            },
            QUICKOSM_PARAMS,
        )
        self.assertEqual(plan.destinations, ("OUTPUT_MULTIPOLYGONS",))
        self.assertEqual(plan.parameter_destinations, ())
        self.assertEqual(plan.internal_destinations, ("FILE",))
        self.assertTrue(plan.network_access)
        self.assertTrue(plan.temporary_file)
        self.assertEqual(plan.binding_for("KEY").value, "building")
        self.assertIs(plan.binding_for("EXTENT").value, True)
        self.assertEqual(
            dict(plan.fixed_values)["SERVER"],
            "https://overpass-api.de/api/interpreter",
        )

    def test_builtin_osm_plan_needs_no_file_or_plugin(self):
        plan = self.plan(
            "smartmodeler:osm_download_polygons",
            {
                "KEY": {"osm_tag": "building"},
                "VALUE": {"osm_tag": "*"},
                "EXTENT": {"map_extent": True},
            },
            SMARTMODELER_OSM_PARAMS,
        )
        self.assertEqual(plan.destinations, ("OUTPUT",))
        self.assertEqual(plan.parameter_destinations, ("OUTPUT",))
        self.assertEqual(plan.internal_destinations, ())
        self.assertTrue(plan.network_access)
        self.assertFalse(plan.temporary_file)
        self.assertEqual(plan.binding_for("KEY").value, "building")
        self.assertIs(plan.binding_for("EXTENT").value, True)

    def test_builtin_osm_plan_accepts_a_live_layer_extent(self):
        plan = self.plan(
            "smartmodeler:osm_download_polygons",
            {
                "KEY": {"osm_tag": "building"},
                "EXTENT": {"layer_extent": "L_vec"},
            },
            SMARTMODELER_OSM_PARAMS,
        )
        extent = plan.binding_for("EXTENT")
        self.assertEqual(extent.tag, "layer_extent")
        self.assertEqual(extent.layer_ids, ("L_vec",))
        self.assertEqual(plan.input_layer_ids, ("L_vec",))

    def test_osm_parameters_accept_generic_string_aliases(self):
        plan = self.plan(
            "smartmodeler:osm_download_polygons",
            {
                "KEY": {"string": "building"},
                "VALUE": {"text": "*"},
                "EXTENT": {"map_extent": True},
            },
            SMARTMODELER_OSM_PARAMS,
        )
        self.assertEqual(plan.binding_for("KEY").tag, "osm_tag")
        self.assertEqual(plan.binding_for("KEY").value, "building")
        self.assertEqual(plan.binding_for("VALUE").tag, "osm_tag")

    def test_quickosm_plan_requires_key_and_extent(self):
        self.assert_rejects(
            "quickosm:downloadosmdataextentquery",
            {"KEY": {"osm_tag": "building"}},
            QUICKOSM_PARAMS,
            ProposalReason.VALIDATION_FAILED,
        )

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

    def test_extract_by_location_resolves_two_layers_and_predicate(self):
        plan = self.plan(
            "native:extractbylocation",
            {
                "INPUT": {"layer": "L_vec"},
                "PREDICATE": {"enum": 0},
                "INTERSECT": {"layer": "L_vec2"},
            },
            EXTRACTLOC_PARAMS,
        )
        self.assertEqual(plan.binding_for("INPUT").layer_ids, ("L_vec",))
        self.assertEqual(plan.binding_for("INTERSECT").layer_ids, ("L_vec2",))
        self.assertEqual(plan.binding_for("PREDICATE").value, 0)

    def test_join_binds_each_field_to_its_own_input_layer(self):
        plan = self.plan(
            "native:joinattributestable",
            {
                "INPUT": {"layer": "L_vec"},
                "FIELD": {"field": "class", "layer_param": "INPUT"},
                "INPUT_2": {"layer": "L_vec2"},
                "FIELD_2": {"field": "code", "layer_param": "INPUT_2"},
                "METHOD": {"enum": 1},
            },
            JOIN_PARAMS,
        )
        self.assertEqual(plan.binding_for("FIELD").value, "class")
        self.assertEqual(plan.binding_for("FIELD_2").value, "code")
        self.assertEqual(plan.destinations, ("OUTPUT",))

    def test_merge_accepts_multiple_vector_layers(self):
        plan = self.plan(
            "native:mergevectorlayers",
            {"LAYERS": {"layers": ["L_vec", "L_vec2"]}, "CRS": {"crs": "EPSG:3857"}},
            MERGE_PARAMS,
        )
        self.assertEqual(plan.binding_for("LAYERS").layer_ids, ("L_vec", "L_vec2"))
        self.assertEqual(plan.binding_for("CRS").value, "EPSG:3857")

    def test_merge_rejects_a_raster_layer_bound_to_the_vector_multilayer(self):
        # MULTI_VECTOR shares its QGIS class with a multi-raster input; the
        # planner's layer-type demand is the only thing keeping a raster out.
        self.assert_rejects(
            "native:mergevectorlayers",
            {"LAYERS": {"layers": ["L_vec", "L_ras"]}},
            MERGE_PARAMS,
            ProposalReason.VALIDATION_FAILED,
        )

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

    def test_active_layer_scope_repairs_a_stale_primary_input(self):
        plan = self.plan(
            "native:buffer",
            {"INPUT": {"layer": "L_vec2"}},
            BUFFER_PARAMS,
            active_layer_id="L_vec",
            require_active_layer=True,
        )
        self.assertEqual(plan.input_layer_ids, ("L_vec",))

    def test_active_layer_scope_rejects_when_there_is_no_active_layer(self):
        self.assert_rejects(
            "native:buffer",
            {"INPUT": {"layer": "L_vec"}},
            BUFFER_PARAMS,
            ProposalReason.TARGET_MISSING,
            active_layer_id="",
            require_active_layer=True,
        )

    def test_active_layer_scope_keeps_every_layer_of_a_multi_primary(self):
        # Pinning a multi-layer primary to the single active layer turned
        # "merge these two" into a one-layer merge that still reported success.
        plan = self.plan(
            "native:mergevectorlayers",
            {"LAYERS": {"layers": ["L_vec", "L_vec2"]}},
            MERGE_PARAMS,
            active_layer_id="L_vec",
            require_active_layer=True,
        )
        self.assertEqual(plan.input_layer_ids, ("L_vec", "L_vec2"))

    def test_active_layer_scope_accepts_the_active_layer_second(self):
        plan = self.plan(
            "native:mergevectorlayers",
            {"LAYERS": {"layers": ["L_vec2", "L_vec"]}},
            MERGE_PARAMS,
            active_layer_id="L_vec",
            require_active_layer=True,
        )
        self.assertEqual(plan.input_layer_ids, ("L_vec2", "L_vec"))

    def test_active_layer_scope_rejects_a_multi_primary_without_the_active_layer(self):
        # The scope guarantee is membership, not absence of a check: a merge
        # that never touches the active layer must still fail closed.
        self.assert_rejects(
            "native:mergevectorlayers",
            {"LAYERS": {"layers": ["L_vec2"]}},
            MERGE_PARAMS,
            ProposalReason.TARGET_MISSING,
            active_layer_id="L_vec",
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


FILTER_PARAMS = [
    spec("INPUT", SOURCE),
    spec("FIELD", FIELD_PARAM),
    spec(
        "OPERATOR",
        ENUM_PARAM,
        default=True,
        options=("=", "≠", ">", ">=", "<", "<=", "begins with", "contains"),
    ),
    spec("VALUE", STRING_PARAM, optional=True),
    spec("OUTPUT", SINK, destination=True),
]

LESS_THAN = 4


class SemanticTrapTests(unittest.TestCase):
    """The runs QGIS executes happily and answers wrongly.

    Each case below passes every structural check -- the parameter exists, the
    field exists, the enum index is in range -- and still produces a confidently
    wrong layer. All three were reproduced against QGIS 3.44 LTR and 4.2 before
    these tests were written; see CHANGELOG 1.5.40 and the end-to-end
    ``qgis_area_threshold_smoke``.
    """

    def plan(self, algorithm_id, inputs, params, **kwargs):
        return plan_processing_run(
            proposal(algorithm_id, inputs),
            default_policy(),
            record(algorithm_id),
            params,
            lookup,
            **kwargs,
        )

    def assert_rejects(self, algorithm_id, inputs, params, reason=None):
        with self.assertRaises(ProposalError) as caught:
            self.plan(algorithm_id, inputs, params)
        self.assertEqual(
            caught.exception.reason_code,
            reason or ProposalReason.VALIDATION_FAILED,
        )
        return caught.exception

    # -- trap 1: lexicographic comparison on a text field -------------------

    def test_a_numeric_less_than_on_a_text_field_is_rejected(self):
        # QGIS compares '1097' < '400' as text and returns True, so the filter
        # keeps the largest buildings and drops the mid-sized ones.
        error = self.assert_rejects(
            "native:extractbyattribute",
            {
                "INPUT": {"layer": "L_merc"},
                "FIELD": {"field": "alan_m2", "layer_param": "INPUT"},
                "OPERATOR": {"enum": LESS_THAN},
                "VALUE": {"string": "400"},
            },
            FILTER_PARAMS,
        )
        self.assertIn("letter by letter", str(error))

    def test_the_same_comparison_on_a_numeric_field_is_allowed(self):
        plan = self.plan(
            "native:extractbyattribute",
            {
                "INPUT": {"layer": "L_merc"},
                "FIELD": {"field": "area_num", "layer_param": "INPUT"},
                "OPERATOR": {"enum": LESS_THAN},
                "VALUE": {"string": "400"},
            },
            FILTER_PARAMS,
        )
        self.assertEqual(plan.binding_for("VALUE").value, "400")

    def test_an_equality_test_on_a_text_field_stays_allowed(self):
        # '=' on text is exactly right; only ordering is the trap.
        plan = self.plan(
            "native:extractbyattribute",
            {
                "INPUT": {"layer": "L_merc"},
                "FIELD": {"field": "name", "layer_param": "INPUT"},
                "OPERATOR": {"enum": 0},
                "VALUE": {"string": "400"},
            },
            FILTER_PARAMS,
        )
        self.assertEqual(plan.binding_for("OPERATOR").value, 0)

    def test_a_text_comparison_against_a_word_stays_allowed(self):
        plan = self.plan(
            "native:extractbyattribute",
            {
                "INPUT": {"layer": "L_merc"},
                "FIELD": {"field": "name", "layer_param": "INPUT"},
                "OPERATOR": {"enum": LESS_THAN},
                "VALUE": {"string": "Mecidiye"},
            },
            FILTER_PARAMS,
        )
        self.assertEqual(plan.binding_for("VALUE").value, "Mecidiye")

    # -- trap 2: retyping a field by recalculating it ------------------------

    def test_recalculating_an_existing_field_with_a_new_type_is_rejected(self):
        error = self.assert_rejects(
            "native:fieldcalculator",
            {
                "INPUT": {"layer": "L_utm"},
                "FIELD_NAME": {"string": "alan_m2"},
                "FIELD_TYPE": {"enum": 1},  # Integer, over an existing String
                "FORMULA": {"expression": '"alan_m2"'},
            },
            FIELD_CALCULATOR_PARAMS,
        )
        self.assertIn("NEW field name", str(error))

    def test_writing_the_conversion_to_a_new_field_is_allowed(self):
        plan = self.plan(
            "native:fieldcalculator",
            {
                "INPUT": {"layer": "L_utm"},
                "FIELD_NAME": {"string": "alan_int"},
                "FIELD_TYPE": {"enum": 1},
                "FORMULA": {"expression": '"alan_m2"'},
            },
            FIELD_CALCULATOR_PARAMS,
        )
        self.assertEqual(plan.binding_for("FIELD_NAME").value, "alan_int")

    def test_recalculating_an_existing_field_with_its_own_type_is_allowed(self):
        plan = self.plan(
            "native:fieldcalculator",
            {
                "INPUT": {"layer": "L_utm"},
                "FIELD_NAME": {"string": "area_num"},
                "FIELD_TYPE": {"enum": 1},  # Integer over an existing Integer
                "FORMULA": {"expression": '"area_num" * 2'},
            },
            FIELD_CALCULATOR_PARAMS,
        )
        self.assertEqual(plan.binding_for("FIELD_NAME").value, "area_num")

    # -- trap 3: a geometry measure on a distorting CRS ---------------------

    def test_area_on_a_mercator_layer_is_rejected(self):
        error = self.assert_rejects(
            "native:fieldcalculator",
            {
                "INPUT": {"layer": "L_merc"},
                "FIELD_NAME": {"string": "alan_yeni"},
                "FIELD_TYPE": {"enum": 0},
                "FORMULA": {"expression": "$area"},
            },
            FIELD_CALCULATOR_PARAMS,
        )
        self.assertIn("native:reprojectlayer", str(error))

    def test_area_on_a_metric_layer_is_allowed(self):
        plan = self.plan(
            "native:fieldcalculator",
            {
                "INPUT": {"layer": "L_utm"},
                "FIELD_NAME": {"string": "alan_yeni"},
                "FIELD_TYPE": {"enum": 0},
                "FORMULA": {"expression": "$area"},
            },
            FIELD_CALCULATOR_PARAMS,
        )
        self.assertEqual(plan.binding_for("FORMULA").value, "$area")

    def test_a_non_geometric_formula_on_a_mercator_layer_is_allowed(self):
        # Only measures depend on the CRS; an ordinary field expression does not.
        plan = self.plan(
            "native:fieldcalculator",
            {
                "INPUT": {"layer": "L_merc"},
                "FIELD_NAME": {"string": "etiket"},
                "FIELD_TYPE": {"enum": 2},
                "FORMULA": {"expression": 'upper("name")'},
            },
            FIELD_CALCULATOR_PARAMS,
        )
        self.assertEqual(plan.binding_for("FORMULA").value, 'upper("name")')

    def test_the_ellipsoidal_area_function_is_caught_too(self):
        # area($geometry) is ellipsoidal only when a project ellipsoid is set;
        # by default QGIS measures it in the layer CRS just like $area.
        self.assert_rejects(
            "native:fieldcalculator",
            {
                "INPUT": {"layer": "L_merc"},
                "FIELD_NAME": {"string": "alan_yeni"},
                "FIELD_TYPE": {"enum": 0},
                "FORMULA": {"expression": "area($geometry)"},
            },
            FIELD_CALCULATOR_PARAMS,
        )


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
        self.assert_rejects(graph, ProposalReason.NO_LAYER_OUTPUT)

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
