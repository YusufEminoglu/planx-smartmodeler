"""Opt-in DeepSeek multi-step acceptance test for OSM and area analysis.

The scenario is intentionally harder than the small matrix: DeepSeek must
inspect and apply four reviewed Processing proposals in sequence, carrying the
resulting project layer into the next stage. The OSM response is cached by
default so the test measures AI planning and plugin integration without an
unbounded external request. Set ``SMARTMODELER_DEEPSEEK_HARD_NETWORK=1`` to
exercise the pinned Overpass path instead.
"""
from __future__ import annotations

import contextlib
import os
import random
import sys
import time
from pathlib import Path

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsApplication,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
)

from qgis_deepseek_live import _profile, _request, _usage_text


def _extent_layer() -> QgsVectorLayer:
    # The sibling downloader deliberately rejects extents above 100 km².
    # Keep this bounded Konak test window below that limit while retaining all
    # seeded neighborhood fixtures and a realistic Overpass request size.
    layer = QgsVectorLayer(
        "Polygon?crs=EPSG:4326",
        "Konak district extent",
        "memory",
    )
    feature = QgsFeature(layer.fields())
    feature.setGeometry(
        QgsGeometry.fromPolygonXY(
            [[
                QgsPointXY(27.05, 38.38),
                QgsPointXY(27.15, 38.38),
                QgsPointXY(27.15, 38.46),
                QgsPointXY(27.05, 38.46),
                QgsPointXY(27.05, 38.38),
            ]]
        )
    )
    layer.dataProvider().addFeature(feature)
    layer.updateExtents()
    QgsProject.instance().addMapLayer(layer)
    return layer


def _seed_osm_response(layer: QgsVectorLayer) -> None:
    """Seed the sibling downloader cache with three neighborhood polygons."""
    from zero2agent_osm_downloader.core.query import TagSpec, build_query
    from zero2agent_osm_downloader.processing.osm_algorithms import _CACHE

    bbox = layer.extent()
    query = build_query(
        (
            TagSpec("boundary", "administrative", "polygon"),
            TagSpec("admin_level", "10", "polygon"),
        ),
        (bbox.yMinimum(), bbox.xMinimum(), bbox.yMaximum(), bbox.xMaximum()),
        "all",
    )
    _CACHE[query] = (
        time.monotonic(),
        {
            "elements": [
                {
                    "type": "way",
                    "id": 901,
                    "tags": {
                        "boundary": "administrative",
                        "admin_level": "10",
                        "name": "Alsancak",
                    },
                    "geometry": [
                        {"lat": 38.42, "lon": 27.08},
                        {"lat": 38.42, "lon": 27.10},
                        {"lat": 38.43, "lon": 27.10},
                        {"lat": 38.43, "lon": 27.08},
                        {"lat": 38.42, "lon": 27.08},
                    ],
                },
                {
                    "type": "way",
                    "id": 902,
                    "tags": {
                        "boundary": "administrative",
                        "admin_level": "10",
                        "name": "Goztepe",
                    },
                    "geometry": [
                        {"lat": 38.38, "lon": 27.09},
                        {"lat": 38.38, "lon": 27.11},
                        {"lat": 38.39, "lon": 27.11},
                        {"lat": 38.39, "lon": 27.09},
                        {"lat": 38.38, "lon": 27.09},
                    ],
                },
                {
                    "type": "way",
                    "id": 903,
                    "tags": {
                        "boundary": "administrative",
                        "admin_level": "10",
                        "name": "Basmane",
                    },
                    "geometry": [
                        {"lat": 38.43, "lon": 27.11},
                        {"lat": 38.43, "lon": 27.13},
                        {"lat": 38.44, "lon": 27.13},
                        {"lat": 38.44, "lon": 27.11},
                        {"lat": 38.43, "lon": 27.11},
                    ],
                },
            ]
        },
    )


def _activate_sibling_plugin(plugins_root: str):
    """Load the sibling plugin lifecycle, not only its Processing provider.

    Headless QGIS does not populate ``qgis.utils.available_plugins`` from the
    workspace automatically. The Agent uses that registry to distinguish an
    installed plugin from an arbitrary provider, so the hard acceptance test
    must bootstrap the same installed-and-enabled state as the GUI.
    """
    import qgis.utils as qgis_utils

    if plugins_root not in qgis_utils.plugin_paths:
        qgis_utils.plugin_paths.append(plugins_root)
    qgis_utils.updateAvailablePlugins()
    from zero2agent_osm_downloader import classFactory

    plugin = classFactory(None)
    plugin.initGui()
    package_name = "zero2agent_osm_downloader"
    qgis_utils.plugins[package_name] = plugin
    if package_name not in qgis_utils.active_plugins:
        qgis_utils.active_plugins.append(package_name)
    return plugin, qgis_utils, package_name


def _field_calculator_binding_hint(field_name: str) -> str:
    """Return the live typed binding shape for the area-calculation stage."""
    algorithm = QgsApplication.processingRegistry().algorithmById(
        "native:fieldcalculator"
    )
    if algorithm is None:
        return ""
    definition = algorithm.parameterDefinition("FIELD_TYPE")
    options = list(definition.options()) if definition is not None else []
    for index, label in enumerate(options):
        folded = str(label).casefold()
        if "decimal" in folded or "double" in folded:
            return (
                'Use these exact typed input forms: FIELD_NAME={"string": '
                + '"'
                + field_name
                + '"}, FIELD_TYPE={"enum": '
                + str(index)
                + '}, FORMULA={"expression": "$area"}. '
                f"The live FIELD_TYPE option at index {index} is {label}."
            )
    return (
        "Use exact typed input forms: FIELD_NAME={\"string\": "
        "<requested field>}, FIELD_TYPE={\"enum\": <live decimal option>}, "
        "FORMULA={\"expression\": \"$area\"}."
    )


def _stage(
    api_key: str,
    source: QgsVectorLayer,
    prompt: str,
    expected_algorithm: str,
    output_kind: str = "vector",
) -> tuple[QgsVectorLayer, str]:
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
        "Use the advertised tools. Inspect the current project state before "
        "proposing exactly one reviewed Processing run. The harness applies the "
        "approved temporary result before the next stage.",
        proposal_validator=validator.validate,
        instruction_provider=lambda text, scope, power: PromptContextLoader(
            context_dir=Path(__file__).resolve().parents[1] / "agent_context"
        ).agent_context(text, scope, power_enabled=power),
        power_enabled_provider=lambda: False,
        active_layer_id_provider=lambda: source.id(),
    )
    # Each stage receives the previous stage's output as its active source.
    # ACTIVE_LAYER scope makes that hand-off a trusted validator requirement,
    # so a provider cannot accidentally reuse the original extent or an older
    # temporary result when several layers coexist in the project.
    event = loop.start(prompt, AgentMode.ACT, AgentScope.ACTIVE_LAYER)
    usages = []
    turns_left = 10
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
    if not ingredients or ingredients["algorithm_id"] != expected_algorithm:
        actual = ingredients["algorithm_id"] if ingredients else "none"
        raise RuntimeError(f"expected {expected_algorithm}, received {actual}")

    project = QgsProject.instance()
    before = set(project.mapLayers())
    finished = []
    failed = []
    coordinator = RunCoordinator(lambda: None)
    coordinator.run_finished.connect(finished.append)
    coordinator.run_failed.connect(lambda reason, message: failed.append((reason, message)))
    refusal = coordinator.start_processing_run(
        f"deepseek_hard_{expected_algorithm}",
        f"DeepSeek hard stage: {expected_algorithm}",
        ingredients["display_name"],
        ingredients["algorithm_id"],
        ingredients["run_parameters"],
        ingredients["destinations"],
    )
    deadline = time.time() + 30.0
    while not finished and not failed and time.time() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    if refusal or failed or len(finished) != 1:
        raise RuntimeError(f"stage apply failed: refusal={refusal!r}, failures={failed!r}")

    candidates = []
    for layer_id in set(project.mapLayers()) - before:
        layer = project.mapLayer(layer_id)
        if isinstance(layer, QgsVectorLayer):
            if output_kind == "polygon" and layer.geometryType() != 2:
                project.removeMapLayer(layer_id)
                continue
            candidates.append(layer)
        else:
            project.removeMapLayer(layer_id)
    if len(candidates) != 1:
        raise RuntimeError(f"expected one {output_kind} output, got {len(candidates)}")
    usage = usages[-1] if usages else None
    return candidates[0], f"{expected_algorithm}: turns={loop.turns_used}, {_usage_text(usage)}"


def run_hard_workflow(api_key: str, seed: int | None = None) -> str:
    seed = seed if seed is not None else random.SystemRandom().randrange(1, 2_147_483_647)
    rng = random.Random(seed)
    area_field = rng.choice(("area_m2", "surface_m2", "neighborhood_m2"))
    analysis_algorithm = rng.choice(("native:centroids", "native:boundingboxes"))
    extent = _extent_layer()
    fieldcalc_hint = _field_calculator_binding_hint(area_field)
    if not os.environ.get("SMARTMODELER_DEEPSEEK_HARD_NETWORK"):
        _seed_osm_response(extent)
    try:
        neighborhood, first = _stage(
            api_key,
            extent,
            "Inspect the Konak district extent polygon. Use the installed 02Agent "
            "OSM Downloader Processing provider and prepare one reviewed run of "
            "zero2agentosm:download_advanced. Download polygon neighborhood "
            "boundaries by matching ALL tags boundary=administrative and "
            "admin_level=10 within the current extent. First call "
            "processing.resolve with this exact algorithm_id and limit=8; use "
            "the returned required bindings and layer_extent alternative. Do not "
            "invent a URL or claim the download already ran.",
            "zero2agentosm:download_advanced",
            "polygon",
        )
        projected, second = _stage(
            api_key,
            neighborhood,
            "Use the polygon layer just produced by the reviewed OSM download. "
            "Prepare one reviewed native:reprojectlayer run with TARGET_CRS "
            "EPSG:3857 so the next area calculation is in square metres.",
            "native:reprojectlayer",
            "polygon",
        )
        measured, third = _stage(
            api_key,
            projected,
            f"Use the reprojected neighborhood polygons. Prepare one reviewed "
            f"native:fieldcalculator run that adds a numeric field named {area_field} "
            "with FIELD_TYPE Decimal (double), FORMULA $area, and temporary output. "
            f"{fieldcalc_hint} Do not use Python or SQL.",
            "native:fieldcalculator",
            "polygon",
        )

        def _area_values(layer: QgsVectorLayer) -> list[float]:
            if layer.fields().indexOf(area_field) < 0:
                return []
            values = []
            for feature in layer.getFeatures():
                value = feature[area_field]
                if value is None:
                    return []
                try:
                    values.append(float(value))
                except (TypeError, ValueError):
                    return []
            return values if values and len(values) == layer.featureCount() else []

        # A successful Processing receipt is not enough for this acceptance
        # workflow: the calculated field must contain values before the next
        # stage uses it. Ask DeepSeek for one bounded semantic repair when a
        # provider-specific field-calculator proposal produced an empty or
        # unpopulated field. The source result is removed before retrying so
        # the provider can only select the intended reprojected layer.
        repair_attempts = 0
        while not _area_values(measured) and repair_attempts < 2:
            repair_attempts += 1
            project = QgsProject.instance()
            project.removeMapLayer(measured.id())
            measured, repair = _stage(
                api_key,
                projected,
                f"The previous Field Calculator result did not populate the "
                f"requested field {area_field}. Inspect the reprojected polygon "
                f"layer again and prepare one corrected reviewed "
                f"native:fieldcalculator run. FIELD_NAME must be the exact "
                f"string {area_field}; FIELD_TYPE must be the live Double "
                f"option; FORMULA must be exactly the QGIS expression $area "
                f"without extra quotes; use temporary output. {fieldcalc_hint} "
                f"Do not use "
                f"Python or SQL, and do not claim execution.",
                "native:fieldcalculator",
                "polygon",
            )
            third = f"{third}; semantic_repair_{repair_attempts}={repair}"
        analyzed, fourth = _stage(
            api_key,
            measured,
            f"Use the polygon layer containing the {area_field} field. Prepare one "
            f"reviewed {analysis_algorithm} run as a simple follow-up geometry "
            "analysis with temporary output. Inspect before proposing and do not "
            "use Python or SQL.",
            analysis_algorithm,
            "vector",
        )
        areas = _area_values(measured)
        if not areas:
            raise RuntimeError(f"area field {area_field!r} was not populated")
        mean = sum(areas) / len(areas)
        return (
            f"DEEPSEEK HARD WORKFLOW PASS: seed={seed}; neighborhoods={len(areas)}; "
            f"area_field={area_field}; mean_area_m2={mean:.2f}; "
            f"analysis_output={analyzed.name()}; stages=[{first}; {second}; {third}; {fourth}]"
        )
    finally:
        for layer_id in list(QgsProject.instance().mapLayers()):
            QgsProject.instance().removeMapLayer(layer_id)


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    api_key = os.environ.pop("SMARTMODELER_DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        api_key = os.environ.pop("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("SKIP: Set SMARTMODELER_DEEPSEEK_API_KEY to run the hard live workflow.", flush=True)
        return 2
    app = QgsApplication([], False)
    app.initQgis()
    source_root = Path(__file__).resolve().parents[1]
    plugins_root = str(source_root.parent)
    if str(source_root.parent) not in sys.path:
        sys.path.insert(0, plugins_root)
    plugin_python = os.path.normpath(os.path.join(QgsApplication.prefixPath(), "python", "plugins"))
    if plugin_python not in sys.path:
        sys.path.insert(0, plugin_python)
    try:
        from processing.core.Processing import Processing
        from planx_smartmodeler.processing.provider import SmartModelerProcessingProvider
        from zero2agent_osm_downloader.processing.provider import AgentOsmProvider

        Processing.initialize()
        registry = QgsApplication.processingRegistry()
        sibling_plugin, qgis_utils, sibling_package = _activate_sibling_plugin(
            plugins_root
        )
        added = []
        for provider_type in (SmartModelerProcessingProvider, AgentOsmProvider):
            if registry.providerById(provider_type.PROVIDER_ID) is None:
                provider = provider_type()
                registry.addProvider(provider)
                added.append(provider)
        try:
            seed_text = os.environ.get("SMARTMODELER_DEEPSEEK_HARD_SEED", "").strip()
            seed = int(seed_text) if seed_text else None
            print(run_hard_workflow(api_key, seed), flush=True)
            return 0
        finally:
            for provider in reversed(added):
                registry.removeProvider(provider)
            sibling_plugin.unload()
            qgis_utils.plugins.pop(sibling_package, None)
            with contextlib.suppress(ValueError):
                qgis_utils.active_plugins.remove(sibling_package)
    except Exception as error:
        print(f"DEEPSEEK HARD WORKFLOW FAIL: {type(error).__name__}: {error}", flush=True)
        return 1
    finally:
        api_key = ""
        app.exitQgis()


if __name__ == "__main__":
    raise SystemExit(main())
