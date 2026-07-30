"""Headless real-QGIS acceptance test for opt-in Agent Power Mode."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

from qgis.PyQt.QtCore import QTimer
from qgis.core import QgsApplication, QgsProject


class _MemorySettings:
    def __init__(self) -> None:
        self.values = {}

    def value(self, key, default=None):
        return self.values.get(key, default)

    def setValue(self, key, value) -> None:
        self.values[key] = value


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    source_root = Path(__file__).resolve().parents[1]
    plugins_root = str(source_root.parent)
    if plugins_root not in sys.path:
        sys.path.insert(0, plugins_root)

    application = QgsApplication([], False)
    application.initQgis()
    processing_plugins = os.path.join(
        QgsApplication.prefixPath(), "python", "plugins"
    )
    if processing_plugins not in sys.path:
        sys.path.append(processing_plugins)
    try:
        from processing.core.Processing import Processing

        Processing.initialize()

        from planx_smartmodeler.core.agent.context_tokens import ContextTokenService
        from planx_smartmodeler.core.agent.contracts import (
            AgentMode,
            AgentResultStatus,
            AgentScope,
            AgentToolCall,
        )
        from planx_smartmodeler.core.agent.controller import AgentController
        from planx_smartmodeler.core.agent.power_mode import (
            PowerModeSettings,
            PowerResourceRegistry,
            ScriptLibrary,
        )
        from planx_smartmodeler.core.agent.power_runtime import PowerRuntime
        from planx_smartmodeler.core.agent.proposals import (
            PROPOSAL_KIND_PYTHON_RUN,
            parse_proposal,
        )
        from planx_smartmodeler.core.agent.run_coordinator import RunCoordinator
        from planx_smartmodeler.core.agent.runtime_proposals import (
            RuntimeProposalValidator,
        )
        from planx_smartmodeler.core.agent.runtime_tools import build_default_registry

        project = QgsProject.instance()
        before = set(project.mapLayers())
        settings = PowerModeSettings(_MemorySettings())
        resources = PowerResourceRegistry()

        with tempfile.TemporaryDirectory(prefix="smartmodeler-script-test-") as root:
            scripts = ScriptLibrary(Path(root) / "library")
            runtime = PowerRuntime(settings, resources, scripts)
            registry = build_default_registry(
                lambda: None,
                ContextTokenService(),
                power_enabled_provider=settings.enabled,
                power_resources=resources,
                script_library=scripts,
            )
            controller = AgentController(registry)

            denied = controller.execute(
                AgentToolCall(
                    call_id="power_off",
                    tool_name="script.list",
                    arguments={},
                ),
                AgentMode.PLAN,
                AgentScope.PROJECT,
            )
            if denied.status != AgentResultStatus.FAILED:
                raise RuntimeError("Power tools were available while Power Mode was off.")

            settings.set_enabled(True)
            script_path = Path(root) / "trusted.py"
            script_path.write_text("trusted_value = 1\n", encoding="utf-8")
            trusted = scripts.import_script(
                script_path,
                name="Trusted smoke script",
                description="Hash-pinned test script",
            )
            if scripts.get(trusted.script_id).script_hash != trusted.script_hash:
                raise RuntimeError("Trusted script hash was not stable.")

            listed = controller.execute(
                AgentToolCall(
                    call_id="power_on",
                    tool_name="script.list",
                    arguments={},
                ),
                AgentMode.PLAN,
                AgentScope.PROJECT,
            )
            if (
                listed.status != AgentResultStatus.SUCCESS
                or listed.data.get("count") != 1
                or not listed.data.get("generated_context_token")
            ):
                raise RuntimeError("Power script discovery did not return bounded receipts.")

            generated_source = (
                "from qgis.core import QgsProject, QgsVectorLayer\n"
                "output = QgsVectorLayer("
                "'Point?crs=EPSG:4326&field=id:integer', "
                "'Power subprocess output', 'memory')\n"
                "QgsProject.instance().addMapLayer(output)\n"
            )
            proposal = parse_proposal(
                PROPOSAL_KIND_PYTHON_RUN,
                json.dumps(
                    {
                        "schema_version": 1,
                        "context_token": listed.data["generated_context_token"],
                        "source": generated_source,
                        "execution_mode": "subprocess",
                        "input_layer_ids": [],
                        "timeout_seconds": 90,
                        "output_names": ["Power subprocess output"],
                        "title": "Create an isolated output",
                        "summary": "Run reviewed PyQGIS in a separate QGIS process.",
                        "warnings": [],
                    }
                ),
            )
            validator = RuntimeProposalValidator(
                lambda: None,
                ContextTokenService(),
                power_runtime=runtime,
            )
            validation = validator.validate(
                PROPOSAL_KIND_PYTHON_RUN,
                proposal,
                AgentMode.ACT,
                AgentScope.PROJECT,
            )
            if not validation.ok:
                raise RuntimeError(
                    f"Generated Python proposal failed validation: "
                    f"{validation.reason_code} {validation.message}"
                )
            ingredients = validator.take_last_validated()
            if (
                not ingredients
                or ingredients["preview"].get("source") != generated_source
                or ingredients["preview"].get("second_confirmation")
            ):
                raise RuntimeError("The isolated Python approval preview was incomplete.")

            finished = []
            failed = []
            coordinator = RunCoordinator(lambda: None, power_runtime=runtime)
            coordinator.run_finished.connect(finished.append)
            coordinator.run_failed.connect(
                lambda reason, message: failed.append((reason, message))
            )
            refusal = coordinator.start_power_run(
                "power_subprocess",
                PROPOSAL_KIND_PYTHON_RUN,
                proposal.title,
                ingredients["display_name"],
                ingredients["power_ingredients"],
            )
            if refusal or failed or len(finished) != 1:
                raise RuntimeError(
                    f"Isolated PyQGIS run failed: refusal={refusal!r}, failures={failed!r}"
                )
            added = set(project.mapLayers()) - before
            if len(added) != 1:
                raise RuntimeError(f"Expected one Power output layer, got {len(added)}.")
            result = project.mapLayer(next(iter(added)))
            if (
                "Generated PyQGIS" not in result.name()
                or not result.isValid()
            ):
                raise RuntimeError("The isolated Power output layer was invalid.")

            cancel_ingredients = dict(ingredients["power_ingredients"])
            cancel_ingredients["source"] = "import time\ntime.sleep(10)\n"
            cancel_ingredients["timeout_seconds"] = 30
            QTimer.singleShot(200, runtime.cancel)
            cancel_started = time.monotonic()
            try:
                runtime.execute_python(cancel_ingredients)
            except RuntimeError as error:
                if "canceled" not in str(error):
                    raise
            else:
                raise RuntimeError("The isolated Python process ignored cancellation.")
            if time.monotonic() - cancel_started > 5:
                raise RuntimeError("Power process cancellation was not responsive.")

            script_path = trusted.source_path
            script_path.write_text("trusted_value = 2\n", encoding="utf-8")
            try:
                scripts.get(trusted.script_id)
            except ValueError:
                pass
            else:
                raise RuntimeError("A modified trusted script retained its trust.")

            for layer_id in set(project.mapLayers()) - before:
                project.removeMapLayer(layer_id)
            for owned in runtime._owned_tempdirs:
                owned.cleanup()
            runtime._owned_tempdirs.clear()

        print(
            "AGENT POWER SMOKE PASS: default-off gating, hash pinning, full "
            "source preview, isolated QGIS execution, cancellation and result "
            "import passed."
        )
        return 0
    finally:
        QgsProject.instance().clear()
        application.exitQgis()


if __name__ == "__main__":
    raise SystemExit(main())
