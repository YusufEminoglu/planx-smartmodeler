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
    MULTI_VECTOR,
    NUMBER,
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

        self.assertEqual(len(_DEFAULT_ALLOWLIST), 16)
        self.assertIsNotNone(self.policy.record_for("native:buffer"))
        self.assertIsNotNone(self.policy.record_for("native:cellstatistics"))
        self.assertIsNotNone(self.policy.record_for("native:extractbyattribute"))
        self.assertIsNotNone(self.policy.record_for("native:extractbylocation"))
        self.assertIsNotNone(self.policy.record_for("native:joinattributestable"))
        self.assertIsNotNone(self.policy.record_for("native:mergevectorlayers"))
        self.assertIsNone(self.policy.record_for("native:refactorfields"))

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
        # Two pinned sinks (OUTPUT, NON_MATCHING); the optional FIELDS_TO_COPY and
        # PREFIX are tolerated unbound. Probed identical on both runtimes.
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
        self.assertEqual(record.destinations, ("OUTPUT", "NON_MATCHING"))

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
        decision = self.policy.is_runnable("native:refactorfields", _buffer_params())
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, ProposalReason.ALGORITHM_NOT_ALLOWED)

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
        self.assertEqual(decision.reason_code, ProposalReason.ALGORITHM_NOT_ALLOWED)

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
