"""Opt-in DeepSeek acceptance for the Workflow Studio (``model_patch``) path.

The randomized matrix covers the one-shot modeler bridge and Agent Chat's
``processing_run`` path. Neither touches the scope this file tests: a
multi-algorithm graph edit written as a ``model_patch`` in ``CURRENT_MODEL``
scope. That is the path a real session drove into five consecutive dead ends --
a guessed algorithm id, an over-budget tool-call batch, an empty payload and a
stale graph receipt each ended the whole run and made the owner retype the
request. Every case here is deliberately bigger than one algorithm, because
that is what makes a provider guess.

A pass means the run reached an approval card the user could accept. It is not
a claim about workflow quality: the assertions are that a *reviewed, complete*
patch survived every validation boundary, that it names only algorithms this
QGIS build really has, and that the live graph was never touched on the way.

Opt-in: every case spends billable provider turns.
``SMARTMODELER_DEEPSEEK_WORKFLOW_LIMIT`` bounds the cases (default 4),
``SMARTMODELER_DEEPSEEK_WORKFLOW_SEED`` makes the selection reproducible.
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

from qgis.core import QgsApplication, QgsProject

from qgis_deepseek_live import _profile, _request, _usage_text


# Each case is a request a planner would actually type, plus the floor the
# resulting graph has to clear. ``min_nodes`` is deliberately modest: the point
# is that a *multi-step* patch arrives complete, not that DeepSeek reproduces
# one exact graph.
CASES = (
    {
        "name": "beekeeping suitability",
        "prompt": (
            "Arıcılık faaliyetleri için ideal alanları bulmak istiyorum. DEM'den "
            "eğim ve bakı türet, yola yakınlık ve sıcaklık gibi katmanları da "
            "kullanarak çok kriterli bir yerleşilebilirlik analizi kur. Bunu "
            "Workflow Studio grafiği olarak hazırla."
        ),
        "min_nodes": 3,
    },
    {
        "name": "slope suitability bands",
        "prompt": (
            "Calculate slope from the DEM and classify it into planning "
            "suitability bands. Build this as a Workflow Studio graph."
        ),
        "min_nodes": 2,
    },
    {
        "name": "residential parcel areas",
        "prompt": (
            "Extract residential parcels, calculate their area, then keep only "
            "the parcels over 1000 square metres. Build this as a Workflow "
            "Studio graph."
        ),
        "min_nodes": 3,
    },
    {
        "name": "buildings to walkable centroids",
        "prompt": (
            "Build a workflow that dissolves the building polygons, splits the "
            "result into single parts, computes each part's area and buffers "
            "the centroids by 250 metres."
        ),
        "min_nodes": 4,
    },
    {
        "name": "flood exposure overlay",
        "prompt": (
            "Prepare a workflow that reprojects the parcels to a metric CRS, "
            "clips them with the flood extent and reports the affected area "
            "per parcel."
        ),
        "min_nodes": 3,
    },
    {
        "name": "replace with green network",
        "prompt": (
            "Replace the currently open workflow with a new one: buffer the "
            "green areas, dissolve the overlaps and intersect the result with "
            "the neighbourhood boundaries. Remove the nodes that are not part "
            "of the requested result."
        ),
        "min_nodes": 3,
        "seed_graph": True,
    },
)

# The harness must never be the thing that gives up: the run loop enforces its
# own max_turns, and a lower cap here reported a live run as a failure while it
# was still working (turns=11/12). Budget confirmations are not provider turns
# and are not counted against this.
MAX_PROVIDER_TURNS = 20


def _seeded_graph(seed_nodes: bool):
    """Return a live graph -- empty, or holding an unrelated two-node workflow.

    The non-empty variant is what a "replace the workflow" request actually
    meets, and it is the case where a provider has to reference live node ids
    instead of inventing them.
    """
    from planx_smartmodeler.core.algorithm_catalog import AlgorithmCatalog
    from planx_smartmodeler.core.graph_model import GraphModel

    graph = GraphModel("Live workflow acceptance")
    if not seed_nodes:
        return graph
    source = AlgorithmCatalog.create_node("smart:input_layer", "src", "Source")
    buffer_node = AlgorithmCatalog.create_node("native:buffer", "buf", "Buffer")
    graph.add_node(source)
    graph.add_node(buffer_node)
    if graph.add_edge(source.node_id, "OUTPUT", buffer_node.node_id, "INPUT") is None:
        raise RuntimeError("the seeded acceptance graph could not be connected")
    return graph


def _patch_algorithm_ids(preview: dict) -> list:
    """Pull the algorithm ids out of the bounded preview operation summaries."""
    ids = []
    for operation in preview.get("operations", []) or []:
        summary = str(operation.get("summary", ""))
        if "(" in summary and ")" in summary:
            candidate = summary.split("(", 1)[1].split(")", 1)[0]
            if ":" in candidate:
                ids.append(candidate)
    return ids


def _run_workflow_case(api_key: str, case: dict) -> str:
    from planx_smartmodeler.core.agent.context_tokens import ContextTokenService
    from planx_smartmodeler.core.agent.contracts import AgentMode, AgentScope
    from planx_smartmodeler.core.agent.controller import AgentController
    from planx_smartmodeler.core.agent.run_loop import AgentRunLoop, RunEventKind
    from planx_smartmodeler.core.agent.runtime_proposals import RuntimeProposalValidator
    from planx_smartmodeler.core.agent.runtime_tools import build_default_registry
    from planx_smartmodeler.core.ai_client import StructuredResponseContract
    from planx_smartmodeler.core.model3_serializer import Model3Serializer
    from planx_smartmodeler.core.prompt_context import PromptContextLoader

    graph = _seeded_graph(bool(case.get("seed_graph")))
    before = Model3Serializer.export_to_json(graph)
    tokens = ContextTokenService()
    controller = AgentController(build_default_registry(lambda: graph, tokens))
    validator = RuntimeProposalValidator(lambda: graph, tokens)
    loop = AgentRunLoop(
        controller,
        "Use the advertised tools. Inspect before proposing. Return one reviewed proposal.",
        proposal_validator=validator.validate,
        instruction_provider=lambda text, scope, power: PromptContextLoader(
            context_dir=Path(__file__).resolve().parents[1] / "agent_context"
        ).agent_context(text, scope, power_enabled=power),
        power_enabled_provider=lambda: False,
    )
    event = loop.start(case["prompt"], AgentMode.PLAN, AgentScope.CURRENT_MODEL)
    usages = []
    recoveries = []
    turns_left = MAX_PROVIDER_TURNS
    while turns_left and event.kind in (
        RunEventKind.REQUEST_PROVIDER,
        RunEventKind.BUDGET_CONFIRMATION,
    ):
        if event.kind == RunEventKind.BUDGET_CONFIRMATION:
            # The dock asks the user; a headless acceptance run consents and
            # keeps going, which is what the token notice is for. Consent is
            # not a provider turn.
            event = loop.confirm_budget()
            continue
        turns_left -= 1
        for tool_event in event.tool_events or ():
            if tool_event.get("kind") == "provider_recovery":
                recoveries.append(tool_event.get("strategy", "recovery"))
        contract = StructuredResponseContract(
            schema=event.request.response_schema,
            name="agent_turn",
            description="Return the next agent_turn object as JSON.",
        )
        try:
            response, usage = _request(
                api_key,
                _profile(),
                event.request.system_prompt,
                event.request.user_prompt,
                contract,
            )
        except RuntimeError as error:
            recovered = loop.submit_provider_failure(event.request.request_token, str(error))
            if recovered is None:
                raise
            event = recovered
            continue
        if usage is not None:
            usages.append(usage)
        if os.environ.get("SMARTMODELER_DEEPSEEK_WORKFLOW_DEBUG", "").strip() == "1":
            # A rejected turn is sanitized before it reaches the user, which is
            # correct in the product and useless when the question is "what did
            # the provider actually write?". Opt-in, bounded, never on by default.
            print(f"RAW[{case['name']}] {str(response)[:2000]}", flush=True)
        event = loop.submit_provider_response(event.request.request_token, response)
        if event is None:
            raise RuntimeError("provider response was ignored as stale")
    for tool_event in event.tool_events or ():
        if tool_event.get("kind") == "provider_recovery":
            recoveries.append(tool_event.get("strategy", "recovery"))
    # Budget is the first thing to rule out when a workflow run gives up: a
    # multi-algorithm request that ran out of inspections is a limits problem,
    # not a provider one, and the two look identical in the final message.
    budget = (
        f"[turns={loop.turns_used}/{loop.controller.limits.max_turns}, "
        f"calls={loop.tool_calls_used}/{loop.controller.limits.max_tool_calls_per_run}"
        + (f", recovered={'+'.join(recoveries)}" if recoveries else "")
        + "]"
    )
    if event.kind == RunEventKind.FAILED:
        raise RuntimeError(f"run failed {budget}: {str(event.text)[:240]}")
    if event.kind != RunEventKind.PROPOSAL:
        raise RuntimeError(
            f"no reviewed workflow patch {budget}: {event.kind}: {str(event.text)[:200]}"
        )

    ingredients = validator.take_last_validated()
    if not ingredients or ingredients.get("kind") != "model_patch":
        raise RuntimeError(f"wrong proposal kind: {ingredients.get('kind') if ingredients else 'none'}")
    preview = ingredients.get("preview") or {}
    node_count = int(preview.get("candidate_node_count", 0))
    if node_count < case["min_nodes"]:
        raise RuntimeError(
            f"patch too small: {node_count} nodes, expected at least {case['min_nodes']}"
        )
    registry = QgsApplication.processingRegistry()
    for algorithm_id in _patch_algorithm_ids(preview):
        if algorithm_id.startswith("smart:"):
            continue
        if registry.algorithmById(algorithm_id) is None:
            raise RuntimeError(f"validated patch names a missing algorithm: {algorithm_id}")
    # Validating a patch is inert: the live graph only changes when the user
    # approves the card, which this acceptance deliberately never does.
    if Model3Serializer.export_to_json(graph) != before:
        raise RuntimeError("validating a workflow patch changed the live graph")

    recovery_text = f", recovered={'+'.join(recoveries)}" if recoveries else ""
    return (
        f"Workflow PASS ({case['name']}, nodes={node_count}, "
        f"edges={preview.get('candidate_edge_count', 0)}, turns={loop.turns_used}"
        f"{recovery_text}, {_usage_text(usages[-1] if usages else None)})"
    )


def run_workflow_matrix(api_key: str, limit: int = 4, seed: int | None = None):
    if seed is None:
        seed = random.SystemRandom().randrange(1, 2_147_483_647)
    rng = random.Random(seed)
    cases = list(CASES)
    rng.shuffle(cases)
    selected = cases[: max(1, min(len(cases), limit))]
    passed = []
    failed = []
    for case in selected:
        try:
            result = _run_workflow_case(api_key, case)
        except Exception as error:  # noqa: BLE001 - one case must not end the matrix
            failed.append(f"{case['name']}: {type(error).__name__}: {str(error)[:240]}")
            print(f"FAIL workflow/{case['name']}: {failed[-1].split(': ', 1)[-1]}", flush=True)
        else:
            passed.append(result)
            print(result, flush=True)
    summary = (
        f"DEEPSEEK WORKFLOW MATRIX: seed={seed}, {len(passed)} passed, "
        f"{len(failed)} failed, {len(selected)} total"
    )
    if failed:
        return summary + "\n" + "\n".join(failed), False
    return summary, True


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    api_key = os.environ.pop("SMARTMODELER_DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        api_key = os.environ.pop("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("SKIP: Set SMARTMODELER_DEEPSEEK_API_KEY to run the live workflow matrix.", flush=True)
        return 2
    app = QgsApplication([], False)
    app.initQgis()
    plugins_path = os.path.normpath(os.path.join(QgsApplication.prefixPath(), "python", "plugins"))
    if plugins_path not in sys.path:
        sys.path.insert(0, plugins_path)
    source_root = Path(__file__).resolve().parents[1]
    if str(source_root.parent) not in sys.path:
        sys.path.insert(0, str(source_root.parent))
    try:
        from processing.core.Processing import Processing

        Processing.initialize()
        from planx_smartmodeler.processing.provider import SmartModelerProcessingProvider

        registry = QgsApplication.processingRegistry()
        added_provider = None
        if registry.providerById("smartmodeler") is None:
            added_provider = SmartModelerProcessingProvider()
            registry.addProvider(added_provider)
        try:
            limit = int(os.environ.get("SMARTMODELER_DEEPSEEK_WORKFLOW_LIMIT", "4"))
            seed_text = os.environ.get("SMARTMODELER_DEEPSEEK_WORKFLOW_SEED", "").strip()
            seed = int(seed_text) if seed_text else None
            summary, passed = run_workflow_matrix(api_key, limit, seed)
            print(summary, flush=True)
            return 0 if passed else 1
        finally:
            if added_provider is not None:
                registry.removeProvider(added_provider)
            QgsProject.instance().clear()
    finally:
        api_key = ""
        app.exitQgis()
        # Keep the QgsApplication referenced past this frame. When ``main``
        # returned, dropping the last reference ran the C++ destructor, and
        # on Windows that can sit forever after a suite has already passed --
        # the verify gate then waits on a process with nothing left to do.
        # The module-level ``os._exit`` is the real end of this process.
        globals()["_QGIS_APPLICATION"] = app


if __name__ == "__main__":
    _code = main()
    # Flush, then leave immediately. A headless QgsApplication can sit in
    # Qt/GDAL static teardown after the suite has already printed its
    # result and returned -- observed on Windows, on an unmodified
    # checkout, with every assertion passed -- and the verify gate then
    # waits forever on a process with nothing left to do. The exit code
    # is the suite's own, so a failure still fails.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_code)
