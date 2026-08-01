"""Opt-in headless DeepSeek transport test for the real Workflow Studio path.

The credential is read once from ``SMARTMODELER_DEEPSEEK_API_KEY``, removed
from the process environment immediately, never persisted, and never printed.
This module is intentionally excluded from the default test registry because
CI and normal local verification must not require a billable external service.
"""
from __future__ import annotations

import os
import sys

from qgis.PyQt.QtCore import QEventLoop, QTimer
from qgis.core import QgsApplication


def run_live_check(api_key: str) -> str:
    from planx_smartmodeler.core.ai_client import AiNetworkClient
    from planx_smartmodeler.core.ai_mcp_bridge import AiMcpBridge
    from planx_smartmodeler.core.ai_settings import AiProfile
    from planx_smartmodeler.core.algorithm_catalog import AlgorithmCatalog
    from planx_smartmodeler.core.model3_serializer import Model3Serializer
    from planx_smartmodeler.core.prompt_context import PromptContextLoader

    prompt = (
        "Create an original JSON workflow named Urban Heat Refuge Access. "
        "Use two smart:input_layer nodes for parks and buildings. Repair both "
        "with native:fixgeometries, buffer the repaired parks by 500 metres, "
        "create building centroids, then use native:extractbylocation to keep "
        "centroids intersecting the park buffer. Use exact catalog port ids. "
        "Return 7 nodes, 6 edges, a short summary, and warnings for inputs that "
        "the user must configure. Do not invent layer names or local values."
    )
    catalog = AlgorithmCatalog.compact_ai_catalog(prompt, 40)
    system_prompt = PromptContextLoader().build("", catalog, "")
    profile = AiProfile.create("deepseek", "Headless live test")
    profile.include_project_context = False
    profile.include_algorithm_catalog = True
    profile.timeout_seconds = 90

    client = AiNetworkClient()
    loop = QEventLoop()
    outcome = {"response": "", "error": "", "usage": None, "timed_out": False}

    def succeeded(response: str) -> None:
        outcome["response"] = response
        loop.quit()

    def failed(message: str) -> None:
        outcome["error"] = message
        loop.quit()

    def timed_out() -> None:
        outcome["timed_out"] = True
        client.cancel()
        loop.quit()

    client.succeeded.connect(succeeded)
    client.failed.connect(failed)
    client.usage_reported.connect(
        lambda usage: outcome.update({"usage": usage})
    )
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(timed_out)
    watchdog.start(105_000)
    client.generate(profile, api_key, system_prompt, prompt)
    loop.exec()
    watchdog.stop()

    response = str(outcome["response"])
    if api_key and api_key in response:
        raise RuntimeError("Provider response unexpectedly echoed the credential.")
    api_key = ""
    if outcome["timed_out"]:
        raise RuntimeError("DeepSeek live test exceeded 105 seconds.")
    if outcome["error"]:
        raise RuntimeError(str(outcome["error"]))
    if not response:
        raise RuntimeError("DeepSeek returned an empty workflow response.")
    if (
        client.is_busy()
        or client._api_key
        or client._profile is not None
        or client._system_prompt
        or client._user_prompt
    ):
        raise RuntimeError("The network client retained sensitive request state.")

    result = AiMcpBridge.parse_response(response)
    graph = result.graph
    if len(graph.nodes) != 7 or len(graph.edges) != 6:
        raise RuntimeError(
            "DeepSeek response passed transport but did not satisfy the requested "
            f"graph shape ({len(graph.nodes)} nodes, {len(graph.edges)} edges)."
        )
    if set(node.algorithm_id for node in graph.nodes.values()) != {
        "smart:input_layer",
        "native:fixgeometries",
        "native:buffer",
        "native:centroids",
        "native:extractbylocation",
    }:
        raise RuntimeError("DeepSeek introduced an unexpected algorithm.")
    native_model, fatal, issues = Model3Serializer.build_native_model(graph)
    if native_model is None or fatal or issues:
        raise RuntimeError(
            f"DeepSeek graph failed native model validation: {fatal or issues}"
        )
    usage = outcome["usage"]
    usage_text = (
        f", tokens={usage.input_tokens}/{usage.output_tokens}/{usage.total_tokens}"
        if usage is not None
        else ""
    )
    return (
        "DEEPSEEK LIVE PASS: deepseek-v4-flash, "
        f"nodes={len(graph.nodes)}, edges={len(graph.edges)}{usage_text}"
    )


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    api_key = os.environ.pop("SMARTMODELER_DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("SKIP: SMARTMODELER_DEEPSEEK_API_KEY is not set.", flush=True)
        return 2
    app = QgsApplication([], False)
    app.initQgis()
    plugins_path = os.path.normpath(
        os.path.join(QgsApplication.prefixPath(), "python", "plugins")
    )
    if plugins_path not in sys.path:
        sys.path.insert(0, plugins_path)
    try:
        from processing.core.Processing import Processing

        Processing.initialize()
        print(run_live_check(api_key), flush=True)
        return 0
    finally:
        api_key = ""
        app.exitQgis()


if __name__ == "__main__":
    raise SystemExit(main())
