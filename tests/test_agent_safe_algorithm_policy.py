"""Pure-Python tests for the deny-by-default SafeAlgorithmPolicy.

QGIS-free: live parameter definitions are represented as :class:`ParamSpec`
views, exactly as the runtime validator builds them from real QGIS parameters.
"""
from __future__ import annotations

import unittest

from planx_smartmodeler.core.agent.proposals import ProposalReason
from planx_smartmodeler.core.agent.safe_algorithm_policy import (
    BOOL,
    DISTANCE,
    EXPRESSION,
    MULTI_VECTOR,
    NUMBER,
    OutputSpec,
    STRING_TEXT,
    VECTOR_LAYER,
    AllowedAlgorithm,
    ParamSpec,
    SafeAlgorithmPolicy,
    default_policy,
    kind_matches,
)


def _p(name, type_names, *, dest=False, optional=False, default=False) -> ParamSpec:
    return ParamSpec(
        name=name,
        is_destination=dest,
        type_names=frozenset(type_names),
        is_optional=optional,
        has_default=default,
    )


# A faithful native:buffer parameter set (as ParamSpec views).
def _buffer_params():
    return [
        _p("INPUT", {"QgsProcessingParameterFeatureSource"}),
        _p("DISTANCE", {"QgsProcessingParameterDistance", "QgsProcessingParameterNumber"}),
        _p("SEGMENTS", {"QgsProcessingParameterNumber"}, default=True),
        _p("DISSOLVE", {"QgsProcessingParameterBoolean"}, default=True),
        _p("OUTPUT", {"QgsProcessingParameterFeatureSink"}, dest=True),
    ]


class KindMatchTests(unittest.TestCase):
    def test_distance_matches_number_param(self) -> None:
        self.assertTrue(kind_matches(DISTANCE, _p("D", {"QgsProcessingParameterNumber"})))

    def test_vector_does_not_match_raster(self) -> None:
        self.assertFalse(
            kind_matches(VECTOR_LAYER, _p("R", {"QgsProcessingParameterRasterLayer"}))
        )


class PolicyDefaultAllowlistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = default_policy()

    def test_allowlist_is_pinned(self) -> None:
        # Asserted against the shipped constant itself: the policy deliberately
        # exposes no "list the allowlist" accessor, so membership can only ever
        # be tested one id at a time by trusted code.
        from planx_smartmodeler.core.agent.safe_algorithm_policy import _DEFAULT_ALLOWLIST

        self.assertEqual(len(_DEFAULT_ALLOWLIST), 26)
        self.assertIsNotNone(self.policy.record_for("native:buffer"))
        self.assertIsNotNone(self.policy.record_for("native:cellstatistics"))
        self.assertIsNotNone(self.policy.record_for("native:extractbyattribute"))
        self.assertIsNotNone(self.policy.record_for("native:extractbylocation"))
        self.assertIsNotNone(self.policy.record_for("native:joinattributestable"))
        self.assertIsNotNone(self.policy.record_for("native:mergevectorlayers"))
        self.assertIsNotNone(self.policy.record_for("native:randomextract"))
        self.assertIsNotNone(self.policy.record_for("native:fieldcalculator"))
        self.assertIsNotNone(
            self.policy.record_for("quickosm:downloadosmdataextentquery")
        )
        for geometry in ("points", "lines", "polygons"):
            self.assertIsNotNone(
                self.policy.record_for(f"smartmodeler:osm_download_{geometry}")
            )
        self.assertIsNotNone(
            self.policy.record_for("zero2agentosm:download_preset")
        )
        self.assertIsNotNone(
            self.policy.record_for("zero2agentosm:download_custom_tag")
        )
        self.assertIsNotNone(
            self.policy.record_for("zero2agentosm:download_advanced")
        )
        self.assertIsNone(self.policy.record_for("native:refactorfields"))

    def test_zero2agent_osm_signatures_are_narrow_network_adapters(self) -> None:
        preset_params = (
            _p("PRESET", {"QgsProcessingParameterEnum"}, default=True),
            _p("EXTENT", {"QgsProcessingParameterExtent"}),
            _p("OUTPUT_POINTS", {"QgsProcessingParameterFeatureSink"}, dest=True),
            _p("OUTPUT_LINES", {"QgsProcessingParameterFeatureSink"}, dest=True),
            _p("OUTPUT_POLYGONS", {"QgsProcessingParameterFeatureSink"}, dest=True),
        )
        decision = self.policy.is_runnable(
            "zero2agentosm:download_preset", preset_params
        )
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.record.network_access)
        self.assertEqual(
            decision.record.destinations,
            ("OUTPUT_POINTS", "OUTPUT_LINES", "OUTPUT_POLYGONS"),
        )

        advanced_params = [
            _p("MATCH_MODE", {"QgsProcessingParameterEnum"}, default=True),
            _p("GEOMETRY", {"QgsProcessingParameterEnum"}, default=True),
        ]
        for index in range(1, 5):
            advanced_params.extend(
                (
                    _p(
                        f"KEY_{index}",
                        {"QgsProcessingParameterString"},
                        optional=index > 1,
                    ),
                    _p(
                        f"VALUE_{index}",
                        {"QgsProcessingParameterString"},
                        optional=True,
                        default=True,
                    ),
                )
            )
        advanced_params.extend(
            (
                _p("EXTENT", {"QgsProcessingParameterExtent"}),
                _p(
                    "OUTPUT_POINTS",
                    {"QgsProcessingParameterFeatureSink"},
                    dest=True,
                ),
                _p(
                    "OUTPUT_LINES",
                    {"QgsProcessingParameterFeatureSink"},
                    dest=True,
                ),
                _p(
                    "OUTPUT_POLYGONS",
                    {"QgsProcessingParameterFeatureSink"},
                    dest=True,
                ),
            )
        )
        advanced_decision = self.policy.is_runnable(
            "zero2agentosm:download_advanced", advanced_params
        )
        self.assertTrue(advanced_decision.allowed)
        self.assertTrue(advanced_decision.record.network_access)
        self.assertEqual(
            advanced_decision.record.required_params,
            ("KEY_1", "EXTENT"),
        )

    def test_builtin_osm_algorithms_are_narrow_network_adapters(self) -> None:
        params = (
            _p("KEY", {"QgsProcessingParameterString"}),
            _p("VALUE", {"QgsProcessingParameterString"}, optional=True, default=True),
            _p("EXTENT", {"QgsProcessingParameterExtent"}),
            _p("OUTPUT", {"QgsProcessingParameterFeatureSink"}, dest=True),
        )
        for geometry in ("points", "lines", "polygons"):
            algorithm_id = f"smartmodeler:osm_download_{geometry}"
            with self.subTest(algorithm_id=algorithm_id):
                decision = self.policy.is_runnable(algorithm_id, params)
                self.assertTrue(decision.allowed)
                self.assertTrue(decision.record.network_access)
                self.assertEqual(decision.record.destinations, ("OUTPUT",))
                self.assertEqual(
                    decision.record.required_params,
                    ("KEY", "EXTENT"),
                )

    def test_quickosm_extent_adapter_is_narrow_and_signature_pinned(self) -> None:
        params = (
            _p("KEY", {"QgsProcessingParameterString"}),
            _p("VALUE", {"QgsProcessingParameterString"}, optional=True),
            _p("TYPE_MULTI_REQUEST", {"QgsProcessingParameterString"}, optional=True),
            _p("EXTENT", {"QgsProcessingParameterExtent"}),
            _p("TIMEOUT", {"QgsProcessingParameterNumber"}, default=True),
            _p("SERVER", {"QgsProcessingParameterString"}, default=True),
            _p(
                "FILE",
                {"QgsProcessingParameterFileDestination"},
                dest=True,
                optional=True,
            ),
        )
        outputs = (
            OutputSpec(
                "OUTPUT_MULTIPOLYGONS",
                frozenset({"QgsProcessingOutputVectorLayer"}),
            ),
        )
        decision = self.policy.is_runnable(
            "quickosm:downloadosmdataextentquery", params, outputs
        )
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.record.network_access)
        self.assertEqual(decision.record.result_outputs, ("OUTPUT_MULTIPOLYGONS",))
        self.assertNotIn("SERVER", decision.record.bindable)

    def test_quickosm_adapter_rejects_output_signature_drift(self) -> None:
        params = (
            _p("KEY", {"QgsProcessingParameterString"}),
            _p("VALUE", {"QgsProcessingParameterString"}, optional=True),
            _p("TYPE_MULTI_REQUEST", {"QgsProcessingParameterString"}, optional=True),
            _p("EXTENT", {"QgsProcessingParameterExtent"}),
            _p("TIMEOUT", {"QgsProcessingParameterNumber"}, default=True),
            _p("SERVER", {"QgsProcessingParameterString"}, default=True),
            _p(
                "FILE",
                {"QgsProcessingParameterFileDestination"},
                dest=True,
                optional=True,
            ),
        )
        decision = self.policy.is_runnable(
            "quickosm:downloadosmdataextentquery", params, ()
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, ProposalReason.SIGNATURE_MISMATCH)

    def test_random_extract_run_signature(self) -> None:
        # Live signature probed identical on 3.44.12 LTR and 4.2.0. Unlike
        # randomselection, this has a sink and therefore creates a new layer.
        params = (
            ParamSpec("INPUT", False, frozenset({"QgsProcessingParameterFeatureSource"}), False, False),
            ParamSpec("METHOD", False, frozenset({"QgsProcessingParameterEnum"}), False, True),
            ParamSpec("NUMBER", False, frozenset({"QgsProcessingParameterNumber"}), False, True),
            ParamSpec("OUTPUT", True, frozenset({"QgsProcessingParameterFeatureSink"}), False, False),
        )
        decision = self.policy.is_runnable("native:randomextract", params)
        self.assertTrue(decision.allowed)
        record = decision.record
        self.assertIsNotNone(record)
        self.assertEqual(record.destinations, ("OUTPUT",))

    def test_extract_by_attribute_run_signature(self) -> None:
        params = (
            ParamSpec("INPUT", False, frozenset({"QgsProcessingParameterFeatureSource"}), False, False),
            ParamSpec("FIELD", False, frozenset({"QgsProcessingParameterField"}), False, False),
            ParamSpec("OPERATOR", False, frozenset({"QgsProcessingParameterEnum"}), False, True),
            ParamSpec("VALUE", False, frozenset({"QgsProcessingParameterString"}), False, False),
            ParamSpec("OUTPUT", True, frozenset({"QgsProcessingParameterFeatureSink"}), False, False),
            ParamSpec("FAIL_OUTPUT", True, frozenset({"QgsProcessingParameterFeatureSink"}), True, False),
        )
        decision = self.policy.is_runnable("native:extractbyattribute", params)
        self.assertTrue(decision.allowed)
        self.assertIsNotNone(decision.record)
        self.assertEqual(decision.record.destinations, ("OUTPUT",))
        self.assertEqual(
            decision.record.optional_destinations, ("FAIL_OUTPUT",)
        )

    def test_extract_by_attribute_rejects_required_fail_output_drift(self) -> None:
        params = (
            ParamSpec("INPUT", False, frozenset({"QgsProcessingParameterFeatureSource"}), False, False),
            ParamSpec("FIELD", False, frozenset({"QgsProcessingParameterField"}), False, False),
            ParamSpec("OPERATOR", False, frozenset({"QgsProcessingParameterEnum"}), False, True),
            ParamSpec("VALUE", False, frozenset({"QgsProcessingParameterString"}), False, False),
            ParamSpec("OUTPUT", True, frozenset({"QgsProcessingParameterFeatureSink"}), False, False),
            ParamSpec("FAIL_OUTPUT", True, frozenset({"QgsProcessingParameterFeatureSink"}), False, False),
        )
        decision = self.policy.is_runnable("native:extractbyattribute", params)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, ProposalReason.SIGNATURE_MISMATCH)

    def test_extract_by_attribute_rejects_non_layer_fail_output_drift(self) -> None:
        params = (
            ParamSpec("INPUT", False, frozenset({"QgsProcessingParameterFeatureSource"}), False, False),
            ParamSpec("FIELD", False, frozenset({"QgsProcessingParameterField"}), False, False),
            ParamSpec("OPERATOR", False, frozenset({"QgsProcessingParameterEnum"}), False, True),
            ParamSpec("VALUE", False, frozenset({"QgsProcessingParameterString"}), False, False),
            ParamSpec("OUTPUT", True, frozenset({"QgsProcessingParameterFeatureSink"}), False, False),
            ParamSpec("FAIL_OUTPUT", True, frozenset({"QgsProcessingParameterFileDestination"}), True, False),
        )
        decision = self.policy.is_runnable("native:extractbyattribute", params)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, ProposalReason.SIGNATURE_MISMATCH)

    def test_extract_by_location_run_signature(self) -> None:
        # Live signature probed identical on 3.44.12 LTR and 4.2.0.
        params = (
            ParamSpec("INPUT", False, frozenset({"QgsProcessingParameterFeatureSource"}), False, False),
            ParamSpec("PREDICATE", False, frozenset({"QgsProcessingParameterEnum"}), False, True),
            ParamSpec("INTERSECT", False, frozenset({"QgsProcessingParameterFeatureSource"}), False, False),
            ParamSpec("OUTPUT", True, frozenset({"QgsProcessingParameterFeatureSink"}), False, False),
        )
        decision = self.policy.is_runnable("native:extractbylocation", params)
        self.assertTrue(decision.allowed)
        self.assertIsNotNone(decision.record)

    def test_join_attributes_table_run_signature(self) -> None:
        # OUTPUT is materialized; reviewed optional NON_MATCHING is left unset.
        # FIELDS_TO_COPY and PREFIX are tolerated unbound. Probed identical on
        # both runtimes.
        params = (
            ParamSpec("INPUT", False, frozenset({"QgsProcessingParameterFeatureSource"}), False, False),
            ParamSpec("FIELD", False, frozenset({"QgsProcessingParameterField"}), False, False),
            ParamSpec("INPUT_2", False, frozenset({"QgsProcessingParameterFeatureSource"}), False, False),
            ParamSpec("FIELD_2", False, frozenset({"QgsProcessingParameterField"}), False, False),
            ParamSpec("FIELDS_TO_COPY", False, frozenset({"QgsProcessingParameterField"}), True, False),
            ParamSpec("METHOD", False, frozenset({"QgsProcessingParameterEnum"}), False, True),
            ParamSpec("DISCARD_NONMATCHING", False, frozenset({"QgsProcessingParameterBoolean"}), False, True),
            ParamSpec("PREFIX", False, frozenset({"QgsProcessingParameterString"}), True, False),
            ParamSpec("OUTPUT", True, frozenset({"QgsProcessingParameterFeatureSink"}), True, False),
            ParamSpec("NON_MATCHING", True, frozenset({"QgsProcessingParameterFeatureSink"}), True, False),
        )
        decision = self.policy.is_runnable("native:joinattributestable", params)
        self.assertTrue(decision.allowed)
        record = decision.record
        self.assertIsNotNone(record)
        self.assertEqual(record.destinations, ("OUTPUT",))
        self.assertEqual(record.optional_destinations, ("NON_MATCHING",))

    def test_merge_vector_layers_run_signature(self) -> None:
        # LAYERS is pinned as MULTI_VECTOR: the same QGIS class as a multi-raster
        # input, distinguished only by the vector layer-type the planner demands.
        params = (
            ParamSpec(
                "LAYERS",
                False,
                frozenset({"QgsProcessingParameterMultipleLayers"}),
                False,
                False,
                source_type="vector",
            ),
            ParamSpec("CRS", False, frozenset({"QgsProcessingParameterCrs"}), True, False),
            ParamSpec("ADD_SOURCE_FIELDS", False, frozenset({"QgsProcessingParameterBoolean"}), False, True),
            ParamSpec("OUTPUT", True, frozenset({"QgsProcessingParameterFeatureSink"}), False, False),
        )
        decision = self.policy.is_runnable("native:mergevectorlayers", params)
        self.assertTrue(decision.allowed)
        record = decision.record
        self.assertIsNotNone(record)
        self.assertEqual(self.policy.expected_kind(record, "LAYERS"), MULTI_VECTOR)

    def test_merge_vector_layers_rejects_a_raster_multi_input_signature(self) -> None:
        params = (
            ParamSpec(
                "LAYERS",
                False,
                frozenset({"QgsProcessingParameterMultipleLayers"}),
                False,
                False,
                source_type="raster",
            ),
            ParamSpec("CRS", False, frozenset({"QgsProcessingParameterCrs"}), True, False),
            ParamSpec(
                "ADD_SOURCE_FIELDS",
                False,
                frozenset({"QgsProcessingParameterBoolean"}),
                False,
                True,
            ),
            ParamSpec(
                "OUTPUT",
                True,
                frozenset({"QgsProcessingParameterFeatureSink"}),
                False,
                False,
            ),
        )
        decision = self.policy.is_runnable("native:mergevectorlayers", params)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, ProposalReason.SIGNATURE_MISMATCH)

    def test_the_policy_cannot_enumerate_its_allowlist(self) -> None:
        for accessor in ("allowed_ids", "allowlist", "algorithms", "ids"):
            self.assertFalse(
                hasattr(self.policy, accessor),
                f"SafeAlgorithmPolicy must not expose {accessor!r}.",
            )

    def test_faithful_buffer_is_runnable(self) -> None:
        decision = self.policy.is_runnable("native:buffer", _buffer_params())
        self.assertTrue(decision.allowed)
        self.assertIsNotNone(decision.record)

    def test_unknown_algorithm_denied(self) -> None:
        decision = self.policy.is_runnable("thirdparty:refactorfields", _buffer_params())
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, ProposalReason.PROVIDER_NOT_TRUSTED)

    def test_structurally_safe_native_algorithm_is_runnable(self) -> None:
        params = (
            _p("INPUT", {"QgsProcessingParameterFeatureSource"}),
            _p("OUTPUT", {"QgsProcessingParameterFeatureSink"}, dest=True),
        )
        decision = self.policy.is_runnable("native:boundary", params)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.record.bindable, {"INPUT": VECTOR_LAYER})
        self.assertEqual(decision.record.destinations, ("OUTPUT",))

    def test_structurally_safe_native_domain_text_is_runnable(self) -> None:
        params = (
            _p("INPUT", {"QgsProcessingParameterFeatureSource"}),
            _p("FIELD_NAME", {"QgsProcessingParameterString"}, default=True),
            _p("OUTPUT", {"QgsProcessingParameterFeatureSink"}, dest=True),
        )
        decision = self.policy.is_runnable("native:addfield", params)
        self.assertTrue(decision.allowed)
        self.assertEqual(
            decision.record.bindable["FIELD_NAME"], STRING_TEXT
        )

    def test_native_expression_text_remains_blocked(self) -> None:
        params = (
            _p("INPUT", {"QgsProcessingParameterFeatureSource"}),
            _p("EXPRESSION", {"QgsProcessingParameterString"}),
            _p("OUTPUT", {"QgsProcessingParameterFeatureSink"}, dest=True),
        )
        decision = self.policy.is_runnable(
            "native:hypotheticalexpression", params
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.reason_code, ProposalReason.UNSUPPORTED_PARAMETER
        )

    def test_structurally_safe_qgis_and_planx_algorithms_are_runnable(self) -> None:
        params = (
            _p("INPUT", {"QgsProcessingParameterFeatureSource"}),
            _p("DISTANCE", {"QgsProcessingParameterNumber"}, default=True),
            _p("OUTPUT", {"QgsProcessingParameterFeatureSink"}, dest=True),
        )
        self.assertTrue(self.policy.is_runnable("qgis:somevectoroperation", params).allowed)
        self.assertTrue(
            self.policy.is_runnable("planx_cartolab:safeoperation", params).allowed
        )

    def test_planx_space_syntax_domain_text_is_safely_bindable(self) -> None:
        params = (
            _p("NETWORK", {"QgsProcessingParameterFeatureSource"}),
            _p("RADII", {"QgsProcessingParameterString"}, default=True),
            _p("OUTPUT", {"QgsProcessingParameterFeatureSink"}, dest=True),
        )
        decision = self.policy.is_runnable("planx:spacesyntax", params)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.record.bindable["RADII"], STRING_TEXT)

    def test_planx_resource_or_code_strings_remain_blocked(self) -> None:
        for name in ("FILES", "SQL_QUERY", "SERVER_URL", "EXPRESSION"):
            with self.subTest(name=name):
                params = (
                    _p("INPUT", {"QgsProcessingParameterFeatureSource"}),
                    _p(name, {"QgsProcessingParameterString"}, default=True),
                    _p("OUTPUT", {"QgsProcessingParameterFeatureSink"}, dest=True),
                )
                decision = self.policy.is_runnable("planx:unsafe_text", params)
                self.assertFalse(decision.allowed)
                self.assertEqual(
                    decision.reason_code, ProposalReason.UNSUPPORTED_PARAMETER
                )

    def test_external_provider_is_not_structurally_trusted(self) -> None:
        self.assertFalse(
            self.policy.is_runnable("gdal:buffervectors", _buffer_params()).allowed
        )

    def test_unreviewed_expression_signature_remains_blocked(self) -> None:
        params = (
            _p("INPUT", {"QgsProcessingParameterFeatureSource"}),
            _p("EXPRESSION", {"QgsProcessingParameterExpression"}, optional=True),
            _p("OUTPUT", {"QgsProcessingParameterFeatureSink"}, dest=True),
        )
        decision = self.policy.is_runnable("native:extractbyexpression", params)
        self.assertFalse(decision.allowed)

    def test_field_calculator_expression_signature_is_pinned(self) -> None:
        params = (
            _p("INPUT", {"QgsProcessingParameterFeatureSource"}),
            _p("FIELD_NAME", {"QgsProcessingParameterString"}),
            _p("FIELD_TYPE", {"QgsProcessingParameterEnum"}, default=True),
            _p("FIELD_LENGTH", {"QgsProcessingParameterNumber"}, default=True),
            _p("FIELD_PRECISION", {"QgsProcessingParameterNumber"}, default=True),
            _p("FORMULA", {"QgsProcessingParameterExpression"}),
            _p("OUTPUT", {"QgsProcessingParameterFeatureSink"}, dest=True),
        )
        decision = self.policy.is_runnable("native:fieldcalculator", params)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.record.bindable["FORMULA"], EXPRESSION)

    def test_structural_policy_rejects_file_destination(self) -> None:
        params = (
            _p("INPUT", {"QgsProcessingParameterFeatureSource"}),
            _p("OUTPUT", {"QgsProcessingParameterFileDestination"}, dest=True),
        )
        decision = self.policy.is_runnable("native:package", params)
        self.assertFalse(decision.allowed)

    def test_structural_policy_rejects_no_output_and_network_ids(self) -> None:
        no_output = (_p("INPUT", {"QgsProcessingParameterFeatureSource"}),)
        self.assertFalse(
            self.policy.is_runnable("native:randomselection", no_output).allowed
        )
        network = (
            _p("INPUT", {"QgsProcessingParameterFeatureSource"}),
            _p("OUTPUT", {"QgsProcessingParameterFeatureSink"}, dest=True),
        )
        self.assertFalse(
            self.policy.is_runnable("native:batchnominatimgeocoder", network).allowed
        )

    def test_blocked_term_denied_even_if_listed(self) -> None:
        # A hostile allowlist entry whose id carries a blocked term still denies.
        record = AllowedAlgorithm(
            "native:executesql", {"INPUT": VECTOR_LAYER}, ("INPUT",), ("OUTPUT",)
        )
        policy = SafeAlgorithmPolicy({"native:executesql": record})
        decision = policy.is_runnable(
            "native:executesql",
            [
                _p("INPUT", {"QgsProcessingParameterFeatureSource"}),
                _p("OUTPUT", {"QgsProcessingParameterFeatureSink"}, dest=True),
            ],
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, ProposalReason.SIDE_EFFECT_BLOCKED)

    def test_missing_required_input_denies(self) -> None:
        params = [p for p in _buffer_params() if p.name != "INPUT"]
        decision = self.policy.is_runnable("native:buffer", params)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, ProposalReason.SIGNATURE_MISMATCH)

    def test_required_input_wrong_type_denies(self) -> None:
        params = _buffer_params()
        params[0] = _p("INPUT", {"QgsProcessingParameterRasterLayer"})  # was vector
        decision = self.policy.is_runnable("native:buffer", params)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, ProposalReason.SIGNATURE_MISMATCH)

    def test_missing_destination_denies(self) -> None:
        params = [p for p in _buffer_params() if p.name != "OUTPUT"]
        decision = self.policy.is_runnable("native:buffer", params)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, ProposalReason.SIGNATURE_MISMATCH)

    def test_new_required_parameter_denies(self) -> None:
        params = _buffer_params()
        params.append(_p("SCRIPT", {"QgsProcessingParameterString"}))  # required, no default
        decision = self.policy.is_runnable("native:buffer", params)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, ProposalReason.SIGNATURE_MISMATCH)

    def test_new_optional_parameter_tolerated(self) -> None:
        # Cross-version tolerance: an added optional/defaulted param is fine.
        params = _buffer_params()
        params.append(_p("GRID_SIZE", {"QgsProcessingParameterNumber"}, optional=True))
        decision = self.policy.is_runnable("native:buffer", params)
        self.assertTrue(decision.allowed)

    def test_unpinned_extra_destination_denies(self) -> None:
        params = _buffer_params()
        params.append(_p("REPORT", {"QgsProcessingParameterFileDestination"}, dest=True))
        decision = self.policy.is_runnable("native:buffer", params)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, ProposalReason.SIGNATURE_MISMATCH)

    def test_expected_kind_lookup(self) -> None:
        record = self.policy.record_for("native:buffer")
        self.assertEqual(self.policy.expected_kind(record, "DISTANCE"), DISTANCE)
        self.assertEqual(self.policy.expected_kind(record, "DISSOLVE"), BOOL)
        self.assertIsNone(self.policy.expected_kind(record, "OUTPUT"))  # not bindable
        self.assertIsNone(self.policy.expected_kind(record, "NONSUCH"))

    def test_label_safe_string_params(self) -> None:
        record = self.policy.record_for("native:countpointsinpolygon")
        self.assertEqual(record.label_safe_string_params, ("FIELD",))
        record = self.policy.record_for("native:buffer")
        self.assertEqual(record.label_safe_string_params, ())

    def test_number_kind_alias_used_for_segments(self) -> None:
        # SEGMENTS is bindable as NUMBER; a NUMBER param satisfies it.
        record = self.policy.record_for("native:buffer")
        self.assertEqual(self.policy.expected_kind(record, "SEGMENTS"), NUMBER)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
