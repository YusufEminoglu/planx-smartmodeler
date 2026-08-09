"""Opt-in live DeepSeek acceptance test for both AI entry points.

The credential is read once from ``SMARTMODELER_DEEPSEEK_API_KEY`` (or the
generic ``DEEPSEEK_API_KEY`` alias), removed from the process environment, and
never persisted or printed.  The test is intentionally excluded from the
default release registry because it uses a billable external service.

It exercises two distinct paths:

* Workflow Studio: DeepSeek returns a validated graph, which is then executed
  by the real GraphExecutionEngine.
* Agent Workspace: DeepSeek performs the real multi-turn loop, including
  read-only tool calls, a reviewed Processing proposal, and an explicit test
  apply through RunCoordinator.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from qgis.PyQt.QtCore import QCoreApplication, QEventLoop, QTimer
from qgis.core import (
    QgsApplication,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
)


LIVE_TIMEOUT_MS = 105_000


def _profile():
    from planx_smartmodeler.core.ai_settings import AiProfile

    profile = AiProfile.create("deepseek", "Live DeepSeek acceptance")
    profile.include_project_context = True
    profile.include_algorithm_catalog = True
    profile.max_catalog_algorithms = 20
    profile.timeout_seconds = 90
    profile.temperature = 0.0
    return profile


def _request(api_key: str, profile, system_prompt: str, user_prompt: str, contract=None) -> tuple[str, object | None]:
    from planx_smartmodeler.core.ai_client import AiNetworkClient

    client = AiNetworkClient()
    loop = QEventLoop()
    outcome: dict[str, object] = {"response": "", "error": "", "usage": None, "timed_out": False}

    client.succeeded.connect(lambda response: (outcome.update(response=response), loop.quit()))
    client.failed.connect(lambda message: (outcome.update(error=message), loop.quit()))
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(
        lambda: (outcome.update(timed_out=True), client.cancel(), loop.quit())
    )
    client.usage_reported.connect(lambda usage: outcome.update(usage=usage))
    watchdog.start(LIVE_TIMEOUT_MS)
    if contract is None:
        client.generate(profile, api_key, system_prompt, user_prompt)
    else:
        client.generate_structured(profile, api_key, system_prompt, user_prompt, contract)
    loop.exec()
    watchdog.stop()
    response = str(outcome.get("response", ""))
    error = str(outcome.get("error", ""))
    if outcome.get("timed_out"):
        raise RuntimeError("DeepSeek live request exceeded 105 seconds.")
    if error:
        raise RuntimeError(error)
    if not response:
        raise RuntimeError("DeepSeek returned an empty response.")
    if client.is_busy() or client._api_key or client._profile is not None:
        raise RuntimeError("The network client retained transient request state.")
    return response, outcome.get("usage")


def _make_source() -> QgsVectorLayer:
    layer = QgsVectorLayer("Point?crs=EPSG:4326&field=id:integer", "DeepSeek test points", "memory")
    feature = QgsFeature(layer.fields())
    feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(0.01, 0.01)))
    feature.setAttribute("id", 1)
    layer.dataProvider().addFeature(feature)
    layer.updateExtents()
    QgsProject.instance().addMapLayer(layer)
    return layer


def _usage_text(usage: object | None) -> str:
    if usage is None:
        return "usage=omitted"
    return (
        f"usage={getattr(usage, 'input_tokens', 0)}/"
        f"{getattr(usage, 'output_tokens', 0)}/"
        f"{getattr(usage, 'total_tokens', 0)}"
    )


def _run_modeler(api_key: str, source: QgsVectorLayer) -> str:
    from planx_smartmodeler.core.ai_mcp_bridge import AiMcpBridge
    from planx_smartmodeler.core.algorithm_catalog import AlgorithmCatalog
    from planx_smartmodeler.core.execution_engine import GraphExecutionEngine, ExecutionStatus
    from planx_smartmodeler.core.prompt_context import PromptContextLoader

    prompt = (
        "Create a minimal workflow named DeepSeek Buffer Acceptance. Use one "
        "smart:input_layer node connected to native:buffer. Leave the input "
        "layer configurable, set DISTANCE to 5, SEGMENTS to 5, and DISSOLVE "
        "to false. Return exactly 2 nodes and 1 edge. Use exact catalog port "
        "ids and do not invent local layer names or paths."
    )
    catalog = AlgorithmCatalog.compact_ai_catalog(prompt, 20)
    system_prompt = PromptContextLoader().build("", catalog, "")
    response, usage = _request(api_key, _profile(), system_prompt, prompt)
    result = AiMcpBridge.parse_response(response)
    graph = result.graph
    if len(graph.nodes) != 2 or len(graph.edges) != 1:
        raise RuntimeError(f"DeepSeek Modeler graph shape was {len(graph.nodes)} nodes/{len(graph.edges)} edges.")
    if {node.algorithm_id for node in graph.nodes.values()} != {"smart:input_layer", "native:buffer"}:
        raise RuntimeError("DeepSeek Modeler returned an unexpected algorithm set.")
    AlgorithmCatalog.autobind_unique_project_layers(graph)
    report = GraphExecutionEngine().execute(graph)
    if report.status != ExecutionStatus.COMPLETED or not report.added_layer_ids:
        raise RuntimeError(f"DeepSeek Modeler graph did not execute: {report.message}")
    return f"Modeler live PASS: 2-node buffer graph executed; {_usage_text(usage)}"


def _run_agent(api_key: str, source: QgsVectorLayer) -> str:
    from planx_smartmodeler.core.agent.context_tokens import ContextTokenService
    from planx_smartmodeler.core.agent.contracts import AgentMode, AgentScope
    from planx_smartmodeler.core.agent.controller import AgentController
    from planx_smartmodeler.core.agent.run_coordinator import RunCoordinator
    from planx_smartmodeler.core.agent.run_loop import AgentRunLoop, RunEventKind
    from planx_smartmodeler.core.agent.runtime_proposals import RuntimeProposalValidator
    from planx_smartmodeler.core.agent.runtime_tools import build_default_registry
    from planx_smartmodeler.core.ai_client import StructuredResponseContract
    from planx_smartmodeler.core.prompt_context import PromptContextLoader
    from planx_smartmodeler.core.agent.protocol import agent_turn_response_schema

    tokens = ContextTokenService()
    controller = AgentController(
        build_default_registry(
            lambda: None,
            tokens,
            active_layer_provider=lambda: source,
        )
    )
    validator = RuntimeProposalValidator(
        lambda: None,
        tokens,
        active_layer_provider=lambda: source,
    )
    loop = AgentRunLoop(
        controller,
        "Use the advertised tools. Inspect before proposing. Never execute a run before the proposal is reviewed.",
        proposal_validator=validator.validate,
        instruction_provider=lambda text, scope, power: PromptContextLoader().agent_context(
            text, scope, power_enabled=power
        ),
        power_enabled_provider=lambda: False,
    )
    prompt = (
        "Use the active vector layer and create a temporary 5 metre buffer. "
        "Inspect the active layer first, then prepare one reviewed processing "
        "run proposal. Do not use Python, SQL, Power Mode, or network tools."
    )
    event = loop.start(prompt, AgentMode.ACT, AgentScope.ACTIVE_LAYER)
    total_usage: list[object] = []
    max_turns = 8
    while event.kind == RunEventKind.REQUEST_PROVIDER and max_turns:
        max_turns -= 1
        contract = StructuredResponseContract(
            schema=event.request.response_schema,
            name="agent_turn",
            description="Return the next agent_turn object.",
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
            # Mirror the GUI's provider-failure boundary: the network client
            # already performs its own bounded empty-content retry, then the
            # AgentRunLoop gets one final transient recovery turn. This keeps
            # the live harness faithful to the shipped Agent Workflow path.
            recovered = loop.submit_provider_failure(
                event.request.request_token, str(error)
            )
            if recovered is None:
                raise
            event = recovered
            continue
        if usage is not None:
            total_usage.append(usage)
        event = loop.submit_provider_response(event.request.request_token, response)
        if event is None:
            raise RuntimeError("DeepSeek Agent response was ignored as stale.")
    if event.kind == RunEventKind.FAILED:
        raise RuntimeError(f"DeepSeek Agent failed: {event.text}")
    if event.kind != RunEventKind.PROPOSAL:
        raise RuntimeError(f"DeepSeek Agent did not produce a reviewed proposal: {event.text}")
    ingredients = validator.take_last_validated()
    if not ingredients:
        raise RuntimeError("DeepSeek Agent proposal retained no validated run ingredients.")

    finished: list[object] = []
    failed: list[object] = []
    coordinator = RunCoordinator(lambda: None)
    coordinator.run_finished.connect(finished.append)
    coordinator.run_failed.connect(lambda reason, message: failed.append((reason, message)))
    refusal = coordinator.start_processing_run(
        "deepseek_agent_live",
        "DeepSeek active-layer buffer",
        ingredients["display_name"],
        ingredients["algorithm_id"],
        ingredients["run_parameters"],
        ingredients["destinations"],
    )
    deadline = time.time() + 20.0
    while not finished and not failed and time.time() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    if refusal or failed or len(finished) != 1:
        raise RuntimeError(f"DeepSeek Agent apply failed: refusal={refusal!r}, failures={failed!r}")
    return f"Agent live PASS: inspect -> validated proposal -> temporary buffer applied; turns={loop.turns_used}; {_usage_text(total_usage[-1] if total_usage else None)}"


def run_live_checks(api_key: str) -> str:
    source = _make_source()
    try:
        modeler = _run_modeler(api_key, source)
        agent = _run_agent(api_key, source)
        return f"DEEPSEEK LIVE PASS: {modeler}; {agent}"
    finally:
        QgsProject.instance().removeMapLayer(source.id())


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    api_key = os.environ.pop("SMARTMODELER_DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        api_key = os.environ.pop("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("SKIP: Set SMARTMODELER_DEEPSEEK_API_KEY to run the live test.", flush=True)
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
        registry = QgsApplication.processingRegistry()
        if registry.providerById("smartmodeler") is None:
            from planx_smartmodeler.processing.provider import SmartModelerProcessingProvider

            registry.addProvider(SmartModelerProcessingProvider())
        print(run_live_checks(api_key), flush=True)
        return 0
    finally:
        api_key = ""
        app.exitQgis()


if __name__ == "__main__":
    raise SystemExit(main())
