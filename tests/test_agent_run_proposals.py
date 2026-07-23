"""Pure-Python tests for the Phase 05 run-proposal contracts.

QGIS-free: exercises the strict `processing_run` / `model_run` parsers, their
tagged input bindings, bounds, roundtrip `to_dict()`, and digest stability. Live
registry/layer/policy validation is covered by the runtime validator and QGIS
smoke tests, not here.
"""
from __future__ import annotations

import json
import unittest

from planx_smartmodeler.core.agent.pending_action import proposal_digest
from planx_smartmodeler.core.agent.proposals import (
    PROPOSAL_KIND_MODEL_RUN,
    PROPOSAL_KIND_PROCESSING_RUN,
    ModelRunProposal,
    ProcessingRunProposal,
    ProposalError,
    ProposalReason,
    parse_proposal,
)


def _valid_processing_run() -> dict:
    return {
        "schema_version": 1,
        "context_token": "tok-abc123",
        "algorithm_id": "native:buffer",
        "title": "Buffer roads",
        "summary": "Buffer the roads layer by 50 metres.",
        "inputs": {
            "INPUT": {"layer": "roads_a1b2c3"},
            "DISTANCE": {"distance": 50},
            "SEGMENTS": {"number": 5},
            "DISSOLVE": {"bool": False},
            "METHOD": {"enum": 1},
            "TARGET_CRS": {"crs": "EPSG:3857"},
            "FIELD": {"string": "NUMPOINTS"},
            "GROUP_FIELD": {"field": "type", "layer_param": "INPUT"},
            "LAYERS": {"layers": ["a_1", "b_2"]},
            "LABEL": {"enum_string": "Repair"},
        },
        "warnings": ["The output is temporary."],
    }


def _valid_model_run() -> dict:
    return {
        "schema_version": 1,
        "context_token": "tok-model-1",
        "title": "Run current model",
        "summary": "Run the current SmartModeler graph.",
        "warnings": [],
    }


def _parse_pr(data: dict) -> ProcessingRunProposal:
    return parse_proposal(PROPOSAL_KIND_PROCESSING_RUN, json.dumps(data))


def _parse_mr(data: dict) -> ModelRunProposal:
    return parse_proposal(PROPOSAL_KIND_MODEL_RUN, json.dumps(data))


class ProcessingRunParseTests(unittest.TestCase):
    def test_valid_processing_run_parses(self) -> None:
        proposal = _parse_pr(_valid_processing_run())
        self.assertIsInstance(proposal, ProcessingRunProposal)
        self.assertEqual(proposal.kind, PROPOSAL_KIND_PROCESSING_RUN)
        self.assertEqual(proposal.algorithm_id, "native:buffer")
        names = {name for name, _ in proposal.inputs}
        self.assertIn("INPUT", names)
        self.assertIn("GROUP_FIELD", names)

    def test_to_dict_roundtrips_and_carries_no_token(self) -> None:
        proposal = _parse_pr(_valid_processing_run())
        tree = proposal.to_dict()
        self.assertEqual(tree["kind"], PROPOSAL_KIND_PROCESSING_RUN)
        self.assertNotIn("context_token", tree)  # token never leaves toward digest/UI
        # to_dict must be JSON-serializable and stable.
        json.dumps(tree)
        self.assertEqual(tree["inputs"]["INPUT"], {"layer": "roads_a1b2c3"})
        self.assertEqual(
            tree["inputs"]["GROUP_FIELD"], {"field": "type", "layer_param": "INPUT"}
        )
        self.assertEqual(tree["inputs"]["LAYERS"], {"layers": ["a_1", "b_2"]})

    def test_digest_is_stable_and_token_independent(self) -> None:
        a = _valid_processing_run()
        b = _valid_processing_run()
        b["context_token"] = "a-different-token"
        self.assertEqual(proposal_digest(_parse_pr(a)), proposal_digest(_parse_pr(b)))

    def test_extra_key_rejected(self) -> None:
        data = _valid_processing_run()
        data["surprise"] = 1
        with self.assertRaises(ProposalError) as ctx:
            _parse_pr(data)
        self.assertEqual(ctx.exception.reason_code, ProposalReason.MALFORMED)

    def test_missing_key_rejected(self) -> None:
        data = _valid_processing_run()
        del data["inputs"]
        with self.assertRaises(ProposalError):
            _parse_pr(data)

    def test_algorithm_id_must_be_provider_name(self) -> None:
        for bad in ("C:/x", "native buffer", "http://x", "buffer", "native:buf/fer"):
            data = _valid_processing_run()
            data["algorithm_id"] = bad
            with self.assertRaises(ProposalError):
                _parse_pr(data)

    def test_untagged_binding_rejected(self) -> None:
        data = _valid_processing_run()
        data["inputs"]["INPUT"] = "roads_a1b2c3"  # bare string, no tag
        with self.assertRaises(ProposalError):
            _parse_pr(data)

    def test_binding_with_two_tags_rejected(self) -> None:
        data = _valid_processing_run()
        data["inputs"]["INPUT"] = {"layer": "x", "number": 1}
        with self.assertRaises(ProposalError):
            _parse_pr(data)

    def test_unknown_binding_tag_rejected(self) -> None:
        data = _valid_processing_run()
        data["inputs"]["INPUT"] = {"expression": "1+1"}
        with self.assertRaises(ProposalError):
            _parse_pr(data)

    def test_layer_id_may_not_be_path_or_uri(self) -> None:
        for bad in ("C:/data/x.gpkg", "../secret", "file:///x", "a/b", "a\\b", "with\nnewline"):
            data = _valid_processing_run()
            data["inputs"]["INPUT"] = {"layer": bad}
            with self.assertRaises(ProposalError):
                _parse_pr(data)

    def test_destination_binding_cannot_be_expressed(self) -> None:
        # There is no tag that yields a destination/path; a "string" that looks
        # like a path is rejected by the safe-text rule.
        data = _valid_processing_run()
        data["inputs"]["OUTPUT"] = {"string": "C:/out/result.gpkg"}
        with self.assertRaises(ProposalError):
            _parse_pr(data)

    def test_non_finite_number_rejected(self) -> None:
        raw = json.dumps(_valid_processing_run()).replace('"number": 5', '"number": 1e400')
        with self.assertRaises(ProposalError):
            parse_proposal(PROPOSAL_KIND_PROCESSING_RUN, raw)

    def test_negative_distance_rejected(self) -> None:
        data = _valid_processing_run()
        data["inputs"]["DISTANCE"] = {"distance": -5}
        with self.assertRaises(ProposalError):
            _parse_pr(data)

    def test_enum_index_out_of_range_rejected(self) -> None:
        data = _valid_processing_run()
        data["inputs"]["METHOD"] = {"enum": 9999}
        with self.assertRaises(ProposalError):
            _parse_pr(data)

    def test_bad_crs_rejected(self) -> None:
        data = _valid_processing_run()
        data["inputs"]["TARGET_CRS"] = {"crs": "not a crs"}
        with self.assertRaises(ProposalError):
            _parse_pr(data)

    def test_credential_parameter_name_rejected(self) -> None:
        data = _valid_processing_run()
        data["inputs"]["api_key"] = {"string": "hello"}
        with self.assertRaises(ProposalError):
            _parse_pr(data)

    def test_too_many_bindings_rejected(self) -> None:
        data = _valid_processing_run()
        data["inputs"] = {f"P{i}": {"number": i} for i in range(31)}
        with self.assertRaises(ProposalError) as ctx:
            _parse_pr(data)
        self.assertEqual(ctx.exception.reason_code, ProposalReason.LIMIT_EXCEEDED)

    def test_too_many_layers_rejected(self) -> None:
        data = _valid_processing_run()
        data["inputs"]["LAYERS"] = {"layers": [f"l{i}" for i in range(26)]}
        with self.assertRaises(ProposalError):
            _parse_pr(data)

    def test_wrong_schema_version_rejected(self) -> None:
        for bad in (2, 1.0, "1", True):
            data = _valid_processing_run()
            data["schema_version"] = bad
            with self.assertRaises(ProposalError):
                _parse_pr(data)

    def test_duplicate_json_key_rejected(self) -> None:
        raw = (
            '{"schema_version":1,"context_token":"t","algorithm_id":"native:buffer",'
            '"title":"a","summary":"b","inputs":{},"inputs":{},"warnings":[]}'
        )
        with self.assertRaises(ProposalError):
            parse_proposal(PROPOSAL_KIND_PROCESSING_RUN, raw)


class ModelRunParseTests(unittest.TestCase):
    def test_valid_model_run_parses(self) -> None:
        proposal = _parse_mr(_valid_model_run())
        self.assertIsInstance(proposal, ModelRunProposal)
        self.assertEqual(proposal.kind, PROPOSAL_KIND_MODEL_RUN)

    def test_to_dict_has_no_algorithm_or_params(self) -> None:
        tree = _parse_mr(_valid_model_run()).to_dict()
        self.assertEqual(tree["kind"], PROPOSAL_KIND_MODEL_RUN)
        self.assertNotIn("algorithm_id", tree)
        self.assertNotIn("inputs", tree)
        self.assertNotIn("context_token", tree)

    def test_extra_key_rejected(self) -> None:
        data = _valid_model_run()
        data["algorithm_id"] = "native:buffer"
        with self.assertRaises(ProposalError):
            _parse_mr(data)

    def test_digest_token_independent(self) -> None:
        a = _valid_model_run()
        b = _valid_model_run()
        b["context_token"] = "other"
        self.assertEqual(proposal_digest(_parse_mr(a)), proposal_digest(_parse_mr(b)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
