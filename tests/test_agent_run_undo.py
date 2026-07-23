"""QGIS-free tests for the result-layer Undo of an approved run.

Undo of a run must remove exactly the layers that run created, and only while
each is still recognisably the run's own output. If the human renamed, edited,
or removed one, the destructive Undo is refused rather than forced.
"""
from __future__ import annotations

import json
import unittest

from planx_smartmodeler.core.agent.context_tokens import ContextTokenService
from planx_smartmodeler.core.agent.pending_action import build_pending_action
from planx_smartmodeler.core.agent.proposals import (
    PROPOSAL_KIND_MODEL_RUN,
    PROPOSAL_KIND_PROCESSING_RUN,
    parse_proposal,
)
from planx_smartmodeler.core.agent.runtime_apply import (
    ApplyReason,
    RuntimeApplyCoordinator,
)


class FakeCrs:
    def __init__(self, authid="EPSG:3857"):
        self._authid = authid

    def authid(self):
        return self._authid


class FakeLayer:
    def __init__(self, layer_id, name, features=3):
        self._id = layer_id
        self._name = name
        self.features = features

    def id(self):
        return self._id

    def name(self):
        return self._name

    def setName(self, name):
        self._name = name

    def crs(self):
        return FakeCrs()

    def featureCount(self):
        return self.features


class FakeProject:
    def __init__(self, *layers):
        self.layers = {layer.id(): layer for layer in layers}

    def mapLayer(self, layer_id):
        return self.layers.get(layer_id)

    def removeMapLayer(self, layer_id):
        self.layers.pop(layer_id, None)


class NullAdapter:
    def current_graph(self):
        return None

    def install_graph(self, graph):
        raise RuntimeError("not used")


def coordinator(project):
    return RuntimeApplyCoordinator(
        NullAdapter(), ContextTokenService(b"0" * 32), project_provider=lambda: project
    )


class RunResultUndoTests(unittest.TestCase):
    def setUp(self):
        self.input_layer = FakeLayer("L_in", "Roads")
        self.result = FakeLayer("L_out", "Buffer - OUTPUT")
        self.other = FakeLayer("L_other", "Something the user added")
        self.project = FakeProject(self.input_layer, self.result, self.other)
        self.apply = coordinator(self.project)

    def record(self, layer_ids=("L_out",), kind=PROPOSAL_KIND_PROCESSING_RUN):
        return self.apply.record_run_result("a1", kind, "Buffer", "Buffer roads", list(layer_ids))

    # -- recording ---------------------------------------------------------

    def test_a_finished_run_records_its_result_layers(self):
        applied = self.record()
        self.assertIsNotNone(applied)
        self.assertEqual([lid for lid, _fp in applied.result_layers], ["L_out"])
        self.assertTrue(applied.is_destructive)

    def test_a_model_run_records_its_result_layers_too(self):
        self.assertIsNotNone(self.record(kind=PROPOSAL_KIND_MODEL_RUN))

    def test_a_run_that_added_nothing_offers_no_undo(self):
        self.assertIsNone(self.record(layer_ids=()))

    def test_a_result_that_cannot_be_resolved_offers_no_undo(self):
        self.assertIsNone(self.record(layer_ids=("L_gone",)))

    def test_a_non_run_kind_is_never_recorded_as_a_run(self):
        self.assertIsNone(self.record(kind="layer_style"))

    # -- eligibility -------------------------------------------------------

    def test_undo_is_offered_while_the_result_is_untouched(self):
        self.assertTrue(self.apply.can_undo(self.record()))

    def test_a_renamed_result_blocks_the_destructive_undo(self):
        applied = self.record()
        self.result.setName("My analysis")
        self.assertFalse(self.apply.can_undo(applied))

    def test_an_edited_result_blocks_the_destructive_undo(self):
        applied = self.record()
        self.result.features = 99
        self.assertFalse(self.apply.can_undo(applied))

    def test_a_removed_result_blocks_the_destructive_undo(self):
        applied = self.record()
        self.project.removeMapLayer("L_out")
        self.assertFalse(self.apply.can_undo(applied))

    def test_a_partially_modified_multi_layer_result_blocks_undo(self):
        second = FakeLayer("L_out2", "Buffer - OUTPUT2")
        self.project.layers[second.id()] = second
        applied = self.record(layer_ids=("L_out", "L_out2"))
        second.setName("Kept by the user")
        self.assertFalse(self.apply.can_undo(applied))

    # -- undo --------------------------------------------------------------

    def test_undo_removes_only_the_run_result_layers(self):
        applied = self.record()
        result = self.apply.undo(applied)
        self.assertTrue(result.ok)
        self.assertEqual(set(self.project.layers), {"L_in", "L_other"})

    def test_undo_removes_every_layer_of_a_multi_output_run(self):
        second = FakeLayer("L_out2", "Buffer - OUTPUT2")
        self.project.layers[second.id()] = second
        applied = self.record(layer_ids=("L_out", "L_out2"))
        self.assertTrue(self.apply.undo(applied).ok)
        self.assertEqual(set(self.project.layers), {"L_in", "L_other"})

    def test_an_ineligible_undo_removes_nothing(self):
        applied = self.record()
        self.result.setName("My analysis")
        result = self.apply.undo(applied)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, ApplyReason.UNDO_NOT_ELIGIBLE)
        self.assertIn("L_out", self.project.layers)

    def test_undo_never_reruns_anything(self):
        applied = self.record()
        self.apply.undo(applied)
        # A second undo of the same record is refused; nothing is recreated.
        self.assertFalse(self.apply.undo(applied).ok)
        self.assertNotIn("L_out", self.project.layers)


class RunNeverUsesTheApplyPathTests(unittest.TestCase):
    def test_a_run_pending_action_cannot_be_applied(self):
        proposal = parse_proposal(
            PROPOSAL_KIND_PROCESSING_RUN,
            json.dumps(
                {
                    "schema_version": 1,
                    "context_token": "tok",
                    "algorithm_id": "native:buffer",
                    "title": "Buffer",
                    "summary": "Buffer the roads.",
                    "inputs": {"INPUT": {"layer": "L_in"}},
                    "warnings": [],
                }
            ),
        )
        pending = build_pending_action(
            PROPOSAL_KIND_PROCESSING_RUN,
            proposal,
            {"title": "Buffer", "target": "Buffer"},
            "native:buffer",
            "tok",
            "act",
            "project",
            now=0.0,
        )
        result = coordinator(FakeProject()).apply(pending)
        self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main()
