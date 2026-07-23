"""QGIS-free tests for the model-patch apply/undo path of the apply coordinator.

The style path is exercised by the real-QGIS smoke test; here a content-addressed
fake serializer + fake model-window adapter + fake catalog prove the atomic
model apply/rollback/undo logic without a Processing registry or QGIS.
"""
from __future__ import annotations

import copy
import json
import unittest

from planx_smartmodeler.core.agent import context as agent_context
from planx_smartmodeler.core.agent.context_tokens import ContextTokenService
from planx_smartmodeler.core.agent.identifiers import MODEL_PROPOSAL_KIND, MODEL_TARGET_ID
from planx_smartmodeler.core.agent.pending_action import build_pending_action
from planx_smartmodeler.core.agent.proposals import parse_proposal
from planx_smartmodeler.core.agent.runtime_apply import (
    ApplyReason,
    RuntimeApplyCoordinator,
)
from planx_smartmodeler.core.graph_model import GraphModel, NodeDefinition, SocketType

_ALGORITHMS = {
    "smart:input_layer": {"inputs": [], "outputs": [("OUTPUT", SocketType.VECTOR)]},
    "native:buffer": {
        "inputs": [("INPUT", SocketType.VECTOR, True), ("DISTANCE", SocketType.NUMBER, False)],
        "outputs": [("OUTPUT", SocketType.VECTOR)],
    },
}


class FakeCatalog:
    def __init__(self) -> None:
        self._layer_choices = {"vector": {"L_vec": "Vec"}, "raster": {"L_ras": "Ras"}}

    def algorithm_exists(self, algorithm_id):
        return algorithm_id in _ALGORITHMS

    def ai_algorithm_allowed(self, algorithm_id):
        return "command" not in algorithm_id and "shell" not in algorithm_id

    def create_node(self, algorithm_id, node_id, title):
        spec = _ALGORITHMS[algorithm_id]
        node = NodeDefinition(node_id=node_id, title=title, algorithm_id=algorithm_id)
        if algorithm_id == "smart:input_layer":
            node.parameters["LAYER"] = ""
        for port in spec["inputs"]:
            node.add_input(port[0], port[0], port[1], required=port[2])
        for port in spec["outputs"]:
            node.add_output(port[0], port[0], port[1])
        return node

    def layer_choices(self, socket_type):
        return dict(self._layer_choices.get(socket_type, {}))


def _canon(graph: GraphModel) -> str:
    return json.dumps(
        {
            "name": graph.name,
            "description": getattr(graph, "description", ""),
            "nodes": {
                nid: {
                    "algo": n.algorithm_id,
                    "title": n.title,
                    "params": {k: n.parameters[k] for k in sorted(n.parameters)},
                }
                for nid, n in sorted(graph.nodes.items())
            },
            "edges": sorted(
                [
                    (e.start_node_id, e.start_port_id, e.end_node_id, e.end_port_id)
                    for e in graph.edges.values()
                ]
            ),
        },
        sort_keys=True,
        default=str,
    )


class FakeSerializer:
    """Content-addressed deepcopy store: export/import faithfully round-trip."""

    def __init__(self) -> None:
        self._store = {}

    def export(self, graph):
        key = _canon(graph)
        self._store.setdefault(key, copy.deepcopy(graph))
        return key

    def import_(self, key):
        graph = self._store.get(key)
        return copy.deepcopy(graph) if graph is not None else None


class FakeModelAdapter:
    def __init__(self, graph, fail_install=False):
        self._graph = graph
        self._fail = fail_install
        self.install_calls = 0

    def current_graph(self):
        return self._graph

    def install_graph(self, graph):
        self.install_calls += 1
        if self._fail:
            raise RuntimeError("install failed")
        self._graph = graph


def _base_graph():
    graph = GraphModel("Base model")
    catalog = FakeCatalog()
    node = catalog.create_node("smart:input_layer", "src", "Source")
    node.parameters["LAYER"] = "L_vec"
    graph.add_node(node)
    return graph


def _rename_patch(graph, token, name="Renamed model"):
    body = {
        "schema_version": 1,
        "context_token": token,
        "title": "Rename",
        "summary": "Give the model a clearer name.",
        "operations": [{"op": "set_model_metadata", "name": name, "description": "d"}],
        "warnings": [],
    }
    return parse_proposal("model_patch", json.dumps(body))


class ModelApplyCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = _base_graph()
        self.tokens = ContextTokenService(secret=b"x" * 32)
        self.serializer = FakeSerializer()
        self.adapter = FakeModelAdapter(self.graph)
        self.catalog = FakeCatalog()

    def _coordinator(self, adapter=None):
        return RuntimeApplyCoordinator(
            adapter or self.adapter,
            self.tokens,
            catalog=self.catalog,
            clone_fn=copy.deepcopy,
            export_fn=self.serializer.export,
            import_fn=self.serializer.import_,
        )

    def _valid_token(self, graph=None):
        graph = graph or self.graph
        return self.tokens.issue(
            MODEL_PROPOSAL_KIND, MODEL_TARGET_ID, agent_context.canonical_model_state(graph)
        )

    def _pending(self, proposal, now=1000.0):
        return build_pending_action(
            "model_patch", proposal, {"title": proposal.title, "destructive": False},
            MODEL_TARGET_ID, proposal.context_token, "act", "current_model", now=now,
        )

    def test_apply_success_replaces_graph_and_records_fingerprint(self) -> None:
        proposal = _rename_patch(self.graph, self._valid_token())
        result = self._coordinator().apply(self._pending(proposal))
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.adapter.current_graph().name, "Renamed model")
        self.assertTrue(result.applied_action.post_fingerprint)
        self.assertIsNotNone(result.applied_action.model_pre_json)

    def test_apply_stale_token_rejects_and_leaves_graph_unchanged(self) -> None:
        proposal = _rename_patch(self.graph, "not-the-live-token")
        result = self._coordinator().apply(self._pending(proposal))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "stale_proposal_context")
        self.assertEqual(self.adapter.current_graph().name, "Base model")
        self.assertEqual(self.adapter.install_calls, 0)

    def test_apply_digest_mismatch_rejects(self) -> None:
        proposal = _rename_patch(self.graph, self._valid_token())
        pending = self._pending(proposal)
        object.__setattr__(pending, "digest", "0" * 64)  # tamper the reviewed digest
        result = self._coordinator().apply(pending)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, ApplyReason.DIGEST_MISMATCH)
        self.assertEqual(self.adapter.install_calls, 0)

    def test_apply_no_open_model_rejects(self) -> None:
        proposal = _rename_patch(self.graph, self._valid_token())
        adapter = FakeModelAdapter(None)
        result = self._coordinator(adapter).apply(self._pending(proposal))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, ApplyReason.TARGET_MISSING)

    def test_apply_rolls_back_when_install_fails(self) -> None:
        proposal = _rename_patch(self.graph, self._valid_token())
        adapter = FakeModelAdapter(self.graph, fail_install=True)
        result = self._coordinator(adapter).apply(self._pending(proposal))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, ApplyReason.FAILED)
        # The live graph is never replaced when install raises before assignment.
        self.assertEqual(adapter.current_graph().name, "Base model")

    def test_apply_invalid_operation_rejects_before_touching_adapter(self) -> None:
        # add_node with an unavailable algorithm fails validation on the clone.
        token = self._valid_token()
        body = {
            "schema_version": 1, "context_token": token, "title": "Add",
            "summary": "Add an unavailable node.",
            "operations": [{
                "op": "add_node", "node_id": "x", "algorithm_id": "native:ghost",
                "title": "Ghost", "parameters": [],
            }],
            "warnings": [],
        }
        proposal = parse_proposal("model_patch", json.dumps(body))
        result = self._coordinator().apply(self._pending(proposal))
        self.assertFalse(result.ok)
        self.assertEqual(self.adapter.install_calls, 0)
        self.assertEqual(self.adapter.current_graph().name, "Base model")

    def test_undo_restores_prior_model(self) -> None:
        coord = self._coordinator()
        result = coord.apply(self._pending(_rename_patch(self.graph, self._valid_token())))
        self.assertTrue(result.ok)
        applied = result.applied_action
        self.assertTrue(coord.can_undo(applied))
        undo = coord.undo(applied)
        self.assertTrue(undo.ok, undo.message)
        self.assertEqual(self.adapter.current_graph().name, "Base model")

    def test_undo_refused_after_intervening_edit(self) -> None:
        coord = self._coordinator()
        result = coord.apply(self._pending(_rename_patch(self.graph, self._valid_token())))
        applied = result.applied_action
        # Simulate a later user edit of the live graph.
        self.adapter.current_graph().name = "User touched"
        self.assertFalse(coord.can_undo(applied))
        undo = coord.undo(applied)
        self.assertFalse(undo.ok)
        self.assertEqual(undo.reason_code, ApplyReason.UNDO_NOT_ELIGIBLE)

    def test_apply_commits_even_if_post_commit_fingerprint_fails(self) -> None:
        # A post-commit export/fingerprint error must NOT report failure (which
        # would imply a rollback) once the mutation is committed.
        proposal = _rename_patch(self.graph, self._valid_token())

        real_export = self.serializer.export
        calls = {"n": 0}

        def flaky_export(graph):
            calls["n"] += 1
            if calls["n"] >= 3:  # pre-state and candidate exported; fail the post one
                raise RuntimeError("post-commit export failure")
            return real_export(graph)

        coord = RuntimeApplyCoordinator(
            self.adapter, self.tokens, catalog=self.catalog, clone_fn=copy.deepcopy,
            export_fn=flaky_export, import_fn=self.serializer.import_,
        )
        result = coord.apply(self._pending(proposal))
        self.assertTrue(result.ok, result.message)
        # The mutation stuck; only Undo eligibility degrades (empty fingerprint).
        self.assertEqual(self.adapter.current_graph().name, "Renamed model")
        self.assertEqual(result.applied_action.post_fingerprint, "")

    def test_apply_is_stale_on_second_apply_after_first_changed_graph(self) -> None:
        coord = self._coordinator()
        pending = self._pending(_rename_patch(self.graph, self._valid_token()))
        self.assertTrue(coord.apply(pending).ok)
        # The same pending applied again is now stale (the graph changed).
        result = coord.apply(pending)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "stale_proposal_context")


if __name__ == "__main__":
    unittest.main()
