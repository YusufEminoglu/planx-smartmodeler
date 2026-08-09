"""Opt-in multi-case DeepSeek acceptance matrix for SmartModeler.

This test sends short, independent tasks through both shipped AI entry points.
Each case uses an in-memory layer, applies only a bounded Processing operation,
and removes the generated result before the next case.  The matrix is opt-in
because every case uses a billable external provider request.
"""
from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import QgsApplication, QgsFeature, QgsGeometry, QgsProject, QgsPointXY, QgsVectorLayer

from qgis_deepseek_live import _profile, _request, _usage_text


CASES = (
    {
        "name": "buffer",
        "algorithm_id": "native:buffer",
        "modeler_prompt": (
            "Create exactly a two-node workflow named Matrix Buffer. Use one "
            "smart:input_layer connected to native:buffer. Set DISTANCE to 5, "
            "SEGMENTS to 5, DISSOLVE to false. Return exactly two nodes and one "
            "edge with the catalog port ids. Keep the input configurable."
        ),
        "agent_prompt": (
            "Inspect the active vector layer, then prepare one reviewed Processing "
            "run using native:buffer. Set DISTANCE to 5, SEGMENTS to 5, and "
            "DISSOLVE to false. Do not use Python, SQL, Power Mode, or network."
        ),
    },
    {
        "name": "fix geometries",
        "algorithm_id": "native:fixgeometries",
        "modeler_prompt": (
            "Create exactly a two-node workflow named Matrix Fix Geometries. Use "
            "smart:input_layer connected to native:fixgeometries. Use the exact "
            "catalog port ids and no invented paths. Return two nodes and one edge."
        ),
        "agent_prompt": (
            "Inspect the active vector layer, then prepare one reviewed Processing "
            "run using native:fixgeometries with the active layer as INPUT. Do not "
            "use Python, SQL, Power Mode, or network."
        ),
    },
    {
        "name": "single parts",
        "algorithm_id": "native:multiparttosingleparts",
        "modeler_prompt": (
            "Create exactly a two-node workflow named Matrix Single Parts. Use "
            "smart:input_layer connected to native:multiparttosingleparts with no "
            "extra parameters. Return two nodes and one edge using exact catalog ids."
        ),
        "agent_prompt": (
            "Inspect the active vector layer, then prepare one reviewed Processing "
            "run using native:multiparttosingleparts with the active layer as INPUT. "
            "Do not use Python, SQL, Power Mode, or network."
        ),
    },
    {
        "name": "deduplicate geometries",
        "algorithm_id": "native:deleteduplicategeometries",
        "modeler_prompt": (
            "Create exactly a two-node workflow named Matrix Deduplicate. Use "
            "smart:input_layer connected to native:deleteduplicategeometries. "
            "Return two nodes and one edge with exact catalog port ids and no paths."
        ),
        "agent_prompt": (
            "Inspect the active vector layer, then prepare one reviewed Processing "
            "run using native:deleteduplicategeometries with the active layer as "
            "INPUT. Do not use Python, SQL, Power Mode, or network."
        ),
    },
    {
        "name": "filter low category",
        "algorithm_id": "native:extractbyattribute",
        "modeler_prompt": (
            "Create exactly a two-node workflow named Matrix Category Filter. Use "
            "smart:input_layer connected to native:extractbyattribute. Set FIELD "
            "to the exact existing field name category (do not use Filter or invent "
            "a field), OPERATOR to the exact live catalog option for equals, and "
            "VALUE to low. Return two nodes and one edge with exact catalog port "
            "ids; keep input configurable."
        ),
        "agent_prompt": (
            "Inspect the active vector layer and its fields, then prepare one reviewed "
            "Processing run using native:extractbyattribute. Set FIELD to the exact "
            "existing field name category (not the word Filter), use the equals "
            "operator, and set VALUE to low. Do not use Python, SQL, Power Mode, "
            "or network."
        ),
    },
)


def _randomized_cases(seed: int) -> list[dict]:
    """Build equally difficult but non-identical prompts for one live run."""
    rng = random.Random(seed)
    filter_field, filter_value = rng.choice(
        (("category", "low"), ("category", "high"), ("zone_class", "core"), ("zone_class", "edge"))
    )
    distance = rng.choice((3, 5, 7))
    segments = rng.choice((3, 5, 8))
    dissolve = rng.choice(("false", "true"))
    title_words = rng.choice(("Small", "Compact", "Quick", "Basic", "Focused"))
    lead_words = rng.choice(
        (
            "First inspect the active vector layer and then",
            "Use the active vector layer after inspecting it, then",
            "Begin with a precise active-layer inspection; next",
            "After checking the active layer fields,",
        )
    )
    variants = []
    for template in CASES:
        case = dict(template)
        case["modeler_prompt"] = case["modeler_prompt"].replace(
            "Matrix ", f"{title_words} Matrix "
        )
        case["agent_prompt"] = case["agent_prompt"].replace(
            "Inspect the active vector layer, then", f"{lead_words}"
        )
        case["agent_prompt"] += rng.choice(
            (
                " Return the reviewed proposal as the terminal JSON envelope.",
                " The run is temporary and must be proposed, not claimed as already executed.",
                " Do not stop at an explanation: emit the inert proposal after inspection.",
            )
        )
        if case["algorithm_id"] == "native:buffer":
            case["modeler_prompt"] = case["modeler_prompt"].replace(
                "DISTANCE to 5", f"DISTANCE to {distance}"
            ).replace("SEGMENTS to 5", f"SEGMENTS to {segments}").replace(
                "DISSOLVE to false", f"DISSOLVE to {dissolve}"
            )
            case["agent_prompt"] = case["agent_prompt"].replace(
                "DISTANCE to 5", f"DISTANCE to {distance}"
            ).replace("SEGMENTS to 5", f"SEGMENTS to {segments}").replace(
                "DISSOLVE to false", f"DISSOLVE to {dissolve}"
            )
        if case["algorithm_id"] == "native:extractbyattribute":
            case["name"] = f"filter {filter_field} {filter_value}"
            case["modeler_prompt"] = case["modeler_prompt"].replace(
                "category", filter_field
            ).replace("VALUE to low", f"VALUE to {filter_value}")
            case["agent_prompt"] = case["agent_prompt"].replace(
                "category", filter_field
            ).replace("VALUE to low", f"VALUE to {filter_value}")
        variants.append(case)
    rng.shuffle(variants)
    return variants


def _make_source(layer_name: str) -> QgsVectorLayer:
    layer = QgsVectorLayer(
        "Point?crs=EPSG:4326&field=id:integer&field=category:string(20)&"
        "field=zone_class:string(20)&field=value:double",
        layer_name,
        "memory",
    )
    features = []
    for feature_id, x, category, zone_class, value in (
        (1, 0.01, "low", "core", 1.0),
        (2, 0.02, "high", "edge", 2.0),
        (3, 0.03, "low", "edge", 3.0),
        (4, 0.04, "high", "core", 4.0),
    ):
        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, x)))
        feature.setAttributes([feature_id, category, zone_class, value])
        features.append(feature)
    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    QgsProject.instance().addMapLayer(layer)
    return layer


def _run_modeler_case(api_key: str, source: QgsVectorLayer, case: dict) -> str:
    from planx_smartmodeler.core.ai_mcp_bridge import AiMcpBridge
    from planx_smartmodeler.core.algorithm_catalog import AlgorithmCatalog
    from planx_smartmodeler.core.execution_engine import ExecutionStatus, GraphExecutionEngine
    from planx_smartmodeler.core.prompt_context import PromptContextLoader

    prompt = case["modeler_prompt"]
    catalog = AlgorithmCatalog.compact_ai_catalog(prompt, 20)
    system_prompt = PromptContextLoader().build("", catalog, "")
    response, usage = _request(api_key, _profile(), system_prompt, prompt)
    graph = AiMcpBridge.parse_response(response).graph
    ids = {node.algorithm_id for node in graph.nodes.values()}
    expected = {"smart:input_layer", case["algorithm_id"]}
    if ids != expected or len(graph.edges) != 1:
        raise RuntimeError(
            f"unexpected graph shape: nodes={sorted(ids)!r}, edges={len(graph.edges)}"
        )
    AlgorithmCatalog.autobind_unique_project_layers(graph)
    report = GraphExecutionEngine().execute(graph)
    if report.status != ExecutionStatus.COMPLETED or not report.added_layer_ids:
        raise RuntimeError(f"graph execution failed: {report.message}")
    added = list(report.added_layer_ids)
    for layer_id in added:
        QgsProject.instance().removeMapLayer(layer_id)
    return f"Modeler PASS ({case['name']}, {_usage_text(usage)})"


def _run_agent_case(api_key: str, source: QgsVectorLayer, case: dict) -> str:
    from planx_smartmodeler.core.agent.context_tokens import ContextTokenService
    from planx_smartmodeler.core.agent.contracts import AgentMode, AgentScope
    from planx_smartmodeler.core.agent.controller import AgentController
    from planx_smartmodeler.core.agent.run_coordinator import RunCoordinator
    from planx_smartmodeler.core.agent.run_loop import AgentRunLoop, RunEventKind
    from planx_smartmodeler.core.agent.runtime_proposals import RuntimeProposalValidator
    from planx_smartmodeler.core.agent.runtime_tools import build_default_registry
    from planx_smartmodeler.core.ai_client import StructuredResponseContract
    from planx_smartmodeler.core.prompt_context import PromptContextLoader

    tokens = ContextTokenService()
    controller = AgentController(
        build_default_registry(lambda: None, tokens, active_layer_provider=lambda: source)
    )
    validator = RuntimeProposalValidator(lambda: None, tokens, active_layer_provider=lambda: source)
    loop = AgentRunLoop(
        controller,
        "Use the advertised tools. Inspect before proposing. Return one reviewed proposal.",
        proposal_validator=validator.validate,
        instruction_provider=lambda text, scope, power: PromptContextLoader(
            context_dir=Path(__file__).resolve().parents[1] / "agent_context"
        ).agent_context(text, scope, power_enabled=power),
        power_enabled_provider=lambda: False,
    )
    event = loop.start(case["agent_prompt"], AgentMode.ACT, AgentScope.ACTIVE_LAYER)
    usages = []
    turns_left = 8
    while event.kind == RunEventKind.REQUEST_PROVIDER and turns_left:
        turns_left -= 1
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
        event = loop.submit_provider_response(event.request.request_token, response)
        if event is None:
            raise RuntimeError("provider response was ignored as stale")
    if event.kind == RunEventKind.FAILED:
        raise RuntimeError(event.text)
    if event.kind != RunEventKind.PROPOSAL:
        raise RuntimeError(f"no reviewed proposal: {event.text}")
    ingredients = validator.take_last_validated()
    if not ingredients or ingredients["algorithm_id"] != case["algorithm_id"]:
        actual = ingredients["algorithm_id"] if ingredients else "none"
        raise RuntimeError(f"wrong validated algorithm: {actual}")

    before = set(QgsProject.instance().mapLayers())
    finished = []
    failed = []
    coordinator = RunCoordinator(lambda: None)
    coordinator.run_finished.connect(finished.append)
    coordinator.run_failed.connect(lambda reason, message: failed.append((reason, message)))
    refusal = coordinator.start_processing_run(
        f"deepseek_matrix_{case['name']}",
        f"DeepSeek matrix: {case['name']}",
        ingredients["display_name"],
        ingredients["algorithm_id"],
        ingredients["run_parameters"],
        ingredients["destinations"],
    )
    deadline = time.time() + 20.0
    while not finished and not failed and time.time() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    for layer_id in set(QgsProject.instance().mapLayers()) - before:
        QgsProject.instance().removeMapLayer(layer_id)
    if refusal or failed or len(finished) != 1:
        raise RuntimeError(f"apply failed: refusal={refusal!r}, failures={failed!r}")
    last_usage = usages[-1] if usages else None
    return f"Agent PASS ({case['name']}, turns={loop.turns_used}, {_usage_text(last_usage)})"


def run_matrix(api_key: str, limit: int = 10, seed: int | None = None) -> tuple[str, bool]:
    if seed is None:
        seed = random.SystemRandom().randrange(1, 2_147_483_647)
    rng = random.Random(seed)
    cases = _randomized_cases(seed)
    selected = cases[: max(1, min(len(cases), (limit + 1) // 2))]
    source = _make_source(
        f"DeepSeek matrix {rng.choice(('alpha', 'beta', 'gamma', 'delta'))} "
        f"{rng.randrange(100, 1000)}"
    )
    passed = []
    failed = []
    try:
        for case in selected:
            channels = [("modeler", _run_modeler_case), ("agent", _run_agent_case)]
            rng.shuffle(channels)
            for label, runner in channels:
                try:
                    result = runner(api_key, source, case)
                except Exception as error:
                    failed.append(f"{label}/{case['name']}: {type(error).__name__}: {str(error)[:240]}")
                    print(f"FAIL {label}/{case['name']}: {failed[-1].split(': ', 1)[-1]}", flush=True)
                else:
                    passed.append(result)
                    print(result, flush=True)
        summary = (
            f"DEEPSEEK MATRIX: seed={seed}, {len(passed)} passed, "
            f"{len(failed)} failed, {len(selected) * 2} total"
        )
        if failed:
            return summary + "\n" + "\n".join(failed), False
        return summary, True
    finally:
        QgsProject.instance().removeMapLayer(source.id())


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    api_key = os.environ.pop("SMARTMODELER_DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        api_key = os.environ.pop("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("SKIP: Set SMARTMODELER_DEEPSEEK_API_KEY to run the live matrix.", flush=True)
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
        provider = registry.providerById("smartmodeler")
        added_provider = None
        if provider is None:
            added_provider = SmartModelerProcessingProvider()
            registry.addProvider(added_provider)
        try:
            limit = int(os.environ.get("SMARTMODELER_DEEPSEEK_MATRIX_LIMIT", "10"))
            seed_text = os.environ.get("SMARTMODELER_DEEPSEEK_MATRIX_SEED", "").strip()
            seed = int(seed_text) if seed_text else None
            summary, passed = run_matrix(api_key, limit, seed)
            print(summary, flush=True)
            return 0 if passed else 1
        finally:
            if added_provider is not None:
                registry.removeProvider(added_provider)
    finally:
        api_key = ""
        app.exitQgis()


if __name__ == "__main__":
    raise SystemExit(main())
