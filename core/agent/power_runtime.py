"""Validation and execution boundary for explicitly enabled Power Mode."""
from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from qgis.PyQt.QtCore import QCoreApplication, QProcess, QProcessEnvironment
from qgis.core import (
    QgsApplication,
    QgsFeature,
    QgsField,
    QgsProject,
    QgsTransaction,
    QgsVectorFileWriter,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QMetaType

from .power_mode import (
    PowerModeSettings,
    PowerResourceRegistry,
    ScriptLibrary,
    resolve_connection,
)
from .proposals import (
    PROPOSAL_KIND_PYTHON_RUN,
    PROPOSAL_KIND_SQL_RUN,
    PROPOSAL_KIND_TRUSTED_SCRIPT_RUN,
    ProposalReason,
    PythonRunProposal,
    SqlRunProposal,
    TrustedScriptRunProposal,
    sql_operation_class,
)

_RISK_IMPORTS = {
    "ctypes", "ftplib", "http", "os", "pathlib", "requests", "shutil",
    "socket", "subprocess", "urllib",
}


def classify_sql(statement: str) -> str:
    """Compatibility alias for the shared strict SQL lexical policy."""
    return sql_operation_class(statement)


def source_risks(source: str) -> List[str]:
    risks = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ["Python syntax is invalid."]
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".", 1)[0])
    flagged = sorted(imports & _RISK_IMPORTS)
    if flagged:
        risks.append("High-impact imports: " + ", ".join(flagged))
    return risks


class PowerRuntime:
    """Keeps Power resources local and never exposes connection/script paths."""

    def __init__(
        self,
        settings: PowerModeSettings,
        resources: PowerResourceRegistry,
        scripts: ScriptLibrary,
    ) -> None:
        self.settings = settings
        self.resources = resources
        self.scripts = scripts
        self._owned_tempdirs: List[tempfile.TemporaryDirectory] = []
        self._active_process: Optional[QProcess] = None
        self._cancel_requested = False

    def cancel(self) -> None:
        """Cancel the isolated child process, if one is active."""
        self._cancel_requested = True
        process = self._active_process
        if process is not None and process.state() != QProcess.ProcessState.NotRunning:
            process.kill()

    def validate(self, proposal: Any) -> Tuple[bool, Dict[str, Any], Dict[str, Any], str]:
        if not self.settings.enabled():
            return False, {}, {}, "Power Mode is disabled."
        if isinstance(proposal, SqlRunProposal):
            return self._validate_sql(proposal)
        if isinstance(proposal, TrustedScriptRunProposal):
            return self._validate_trusted(proposal)
        if isinstance(proposal, PythonRunProposal):
            return self._validate_python(proposal)
        return False, {}, {}, "Unknown Power Mode proposal."

    def _validate_sql(self, proposal: SqlRunProposal):
        resource = self.resources.resolve(proposal.connection_token, "database")
        if (
            resource is None
            or proposal.context_token != proposal.connection_token
            or resource.provider != proposal.provider
        ):
            return False, {}, {}, "The database receipt is missing or expired."
        connection = resolve_connection(resource)
        if connection is None:
            return False, {}, {}, "The database connection is no longer available."
        actual = classify_sql(proposal.statement)
        if actual != proposal.operation:
            return False, {}, {}, "The declared SQL operation class is incorrect."
        destructive = actual != "select"
        preview = {
            "kind": PROPOSAL_KIND_SQL_RUN,
            "title": proposal.title,
            "target": f"{resource.provider}: {resource.display_name}",
            "summary": proposal.summary,
            "warnings": list(proposal.warnings)
            + (
                ["This SQL can change database state and cannot be undone by SmartModeler."]
                if destructive
                else []
            ),
            "operations": [{"summary": proposal.statement, "destructive": destructive}],
            "source": proposal.statement,
            "source_language": "sql",
            "destructive": destructive,
            "second_confirmation": destructive,
            "transaction_support": _transaction_available(connection, resource.provider),
        }
        ingredients = {
            "display_name": resource.display_name,
            "connection": connection,
            "provider": resource.provider,
            "statement": proposal.statement,
            "operation": actual,
            "output_name": proposal.output_name or "SQL result",
        }
        return True, preview, ingredients, ""

    def _validate_trusted(self, proposal: TrustedScriptRunProposal):
        resource = self.resources.resolve(proposal.context_token, "script")
        if resource is None or resource.resource_id != proposal.script_id:
            return False, {}, {}, "The trusted-script receipt is missing or expired."
        try:
            item = self.scripts.get(proposal.script_id)
            source = item.source_path.read_text(encoding="utf-8")
        except (OSError, ValueError, UnicodeError):
            return False, {}, {}, "The trusted script is unavailable or changed."
        if item.script_hash != proposal.script_hash:
            return False, {}, {}, "The trusted script hash changed."
        if set(proposal.parameters) - set(item.parameters):
            return False, {}, {}, "The script proposal contains an undeclared parameter."
        return self._python_preview(
            proposal, source, item.name, proposal.execution_mode, proposal.parameters
        )

    def _validate_python(self, proposal: PythonRunProposal):
        resource = self.resources.resolve(proposal.context_token, "python")
        if resource is None:
            return False, {}, {}, "The generated-code receipt is missing or expired."
        project = QgsProject.instance()
        if project is None or any(project.mapLayer(layer_id) is None for layer_id in proposal.input_layer_ids):
            return False, {}, {}, "One or more selected input layers are unavailable."
        return self._python_preview(
            proposal,
            proposal.source,
            "Generated PyQGIS",
            proposal.execution_mode,
            {},
        )

    def _python_preview(
        self, proposal: Any, source: str, name: str, mode: str, parameters: Dict[str, Any]
    ):
        try:
            compile(source, "<SmartModeler Power Mode>", "exec")
        except SyntaxError:
            return False, {}, {}, "Python source does not compile."
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        risks = source_risks(source)
        warnings = list(proposal.warnings) + risks + [
            "Full Python runs with the current user's permissions and is not a security sandbox."
        ]
        if mode == "live":
            warnings.append("Live mode can change or crash the current QGIS session and has no rollback.")
        preview = {
            "kind": proposal.kind,
            "title": proposal.title,
            "target": f"{name} ({mode})",
            "summary": proposal.summary,
            "warnings": warnings,
            "operations": [{"summary": f"Execute Python SHA-256 {digest}", "destructive": True}],
            "source": source,
            "source_language": "python",
            "source_hash": digest,
            "destructive": True,
            "second_confirmation": mode == "live",
        }
        ingredients = {
            "display_name": name,
            "source": source,
            "execution_mode": mode,
            "parameters": parameters,
            "input_layer_ids": tuple(getattr(proposal, "input_layer_ids", ())),
            "timeout_seconds": int(getattr(proposal, "timeout_seconds", 120)),
            "output_names": tuple(getattr(proposal, "output_names", ())),
        }
        return True, preview, ingredients, ""

    def execute_sql(self, ingredients: Dict[str, Any]) -> Tuple[List[Any], str]:
        connection = ingredients["connection"]
        statement = ingredients["statement"]
        operation = ingredients["operation"]
        if operation == "select":
            result = connection.execSql(statement)
            columns = [str(item)[:128] for item in list(result.columns())[:100]]
            rows = list(result.rows())[:100_000]
            layer = _rows_layer(columns, rows, ingredients["output_name"])
            return [layer], f"SQL returned {len(rows)} row(s)."

        provider = ingredients["provider"]
        transaction = _create_transaction(connection, provider)
        if transaction is not None:
            ok, error = transaction.begin(20)
            if not ok:
                raise RuntimeError("The database transaction could not start.")
            ok, error = transaction.executeSql(statement, True, "SmartModeler SQL")
            if not ok:
                with contextlib.suppress(Exception):
                    transaction.rollback()
                raise RuntimeError("SQL execution failed and was rolled back.")
            ok, error = transaction.commit()
            if not ok:
                with contextlib.suppress(Exception):
                    transaction.rollback()
                raise RuntimeError("SQL commit failed and was rolled back.")
        else:
            connection.execSql(statement)
        return [], "SQL completed."

    def execute_python(self, ingredients: Dict[str, Any]) -> Tuple[List[Any], str]:
        self._cancel_requested = False
        if ingredients["execution_mode"] == "live":
            return self._execute_live(ingredients)
        return self._execute_subprocess(ingredients)

    def _execute_live(self, ingredients: Dict[str, Any]) -> Tuple[List[Any], str]:
        from qgis.core import QgsPythonRunner
        import qgis.utils

        namespace = qgis.utils.__dict__
        project = QgsProject.instance()
        layer_ids = list(ingredients["input_layer_ids"])
        namespace["_smartmodeler_power_parameters"] = dict(ingredients["parameters"])
        namespace["_smartmodeler_power_layer_ids"] = layer_ids
        preamble = (
            "import qgis.utils as _smartmodeler_utils\n"
            "from qgis.core import QgsProject\n"
            "smartmodeler_parameters = dict("
            "_smartmodeler_utils._smartmodeler_power_parameters)\n"
            "smartmodeler_input_layer_ids = list("
            "_smartmodeler_utils._smartmodeler_power_layer_ids)\n"
            "smartmodeler_input_layers = [QgsProject.instance().mapLayer(_layer_id) "
            "for _layer_id in smartmodeler_input_layer_ids]\n"
            "smartmodeler_input_layers = [_layer for _layer in "
            "smartmodeler_input_layers if _layer is not None]\n"
            "smartmodeler_output_dir = ''\n"
        )
        before = set(QgsProject.instance().mapLayers())
        try:
            if not QgsPythonRunner.run(preamble + "\n" + ingredients["source"]):
                after_ids = set(project.mapLayers()) - before
                if after_ids:
                    project.removeMapLayers(list(after_ids))
                raise RuntimeError("The live Python runner reported failure.")
            after = project.mapLayers()
            layers = [layer for layer_id, layer in after.items() if layer_id not in before]
            return layers, f"Live Python added {len(layers)} layer(s)."
        finally:
            namespace.pop("_smartmodeler_power_parameters", None)
            namespace.pop("_smartmodeler_power_layer_ids", None)

    def _execute_subprocess(self, ingredients: Dict[str, Any]) -> Tuple[List[Any], str]:
        tempdir = tempfile.TemporaryDirectory(prefix="smartmodeler-power-")
        self._owned_tempdirs.append(tempdir)
        root = Path(tempdir.name)
        input_path = root / "inputs.gpkg"
        project = QgsProject.instance()
        input_names = []
        for index, layer_id in enumerate(ingredients["input_layer_ids"]):
            layer = project.mapLayer(layer_id) if project is not None else None
            if not isinstance(layer, QgsVectorLayer):
                continue
            name = f"input_{index}"
            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "GPKG"
            options.layerName = name
            options.actionOnExistingFile = (
                QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile
                if index == 0
                else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
            )
            result = QgsVectorFileWriter.writeAsVectorFormatV3(
                layer, str(input_path), project.transformContext(), options
            )
            if result[0] != QgsVectorFileWriter.WriterError.NoError:
                raise RuntimeError("An input layer could not be snapshotted.")
            input_names.append(name)

        manifest = {
            "input_gpkg": str(input_path),
            "input_names": input_names,
            "parameters": ingredients["parameters"],
            "output_gpkg": str(root / "outputs.gpkg"),
            "output_names": list(ingredients["output_names"]),
        }
        manifest_path = root / "manifest.json"
        result_path = root / "result.json"
        source_path = root / "user_code.py"
        wrapper_path = root / "runner.py"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        source_path.write_text(ingredients["source"], encoding="utf-8")
        wrapper_path.write_text(_SUBPROCESS_WRAPPER, encoding="utf-8")
        program, arguments = _python_command(wrapper_path, manifest_path, result_path)
        process = QProcess()
        process.setProcessEnvironment(QProcessEnvironment.systemEnvironment())
        process.setProgram(program)
        process.setArguments(arguments)
        self._active_process = process
        try:
            process.start()
            if not process.waitForStarted(10_000):
                raise RuntimeError("The isolated QGIS Python process could not start.")
            deadline = time.monotonic() + int(ingredients["timeout_seconds"])
            while process.state() != QProcess.ProcessState.NotRunning:
                process.waitForFinished(100)
                QCoreApplication.processEvents()
                if self._cancel_requested:
                    process.kill()
                    process.waitForFinished(5_000)
                    raise RuntimeError("The isolated Python run was canceled.")
                if time.monotonic() >= deadline:
                    process.kill()
                    process.waitForFinished(5_000)
                    raise RuntimeError("The isolated Python run exceeded its timeout.")
            if process.exitCode() != 0 or not result_path.is_file():
                raise RuntimeError("The isolated Python run failed.")
        finally:
            self._active_process = None
        data = json.loads(result_path.read_text(encoding="utf-8"))
        layers = []
        for name in data.get("outputs", [])[:20]:
            layer = QgsVectorLayer(
                f"{manifest['output_gpkg']}|layername={name}", str(name), "ogr"
            )
            if layer.isValid():
                layers.append(layer)
        return layers, f"Isolated Python produced {len(layers)} layer(s)."


def _transaction_available(connection: Any, provider: str) -> bool:
    return _create_transaction(connection, provider) is not None


def _create_transaction(connection: Any, provider: str):
    try:
        return QgsTransaction.create(str(connection.uri()), provider)
    except Exception:
        return None


def _rows_layer(columns: List[str], rows: List[Any], name: str) -> QgsVectorLayer:
    layer = QgsVectorLayer("None", name, "memory")
    provider = layer.dataProvider()
    provider.addAttributes(
        [QgsField(column or f"column_{index + 1}", QMetaType.Type.QString)
         for index, column in enumerate(columns)]
    )
    layer.updateFields()
    features = []
    for row in rows:
        feature = QgsFeature(layer.fields())
        values = list(row) if isinstance(row, (list, tuple)) else [row]
        normalized = [
            str(value) if value is not None else None
            for value in values[: len(columns)]
        ]
        normalized.extend([None] * (len(columns) - len(normalized)))
        feature.setAttributes(normalized)
        features.append(feature)
    provider.addFeatures(features)
    layer.updateExtents()
    return layer


def _python_command(
    wrapper: Path, manifest: Path, result: Path
) -> Tuple[str, List[str]]:
    prefix = Path(QgsApplication.prefixPath())
    if os.name == "nt":
        root = prefix.parent.parent
        launcher_name = (
            "python-qgis-ltr.bat"
            if "ltr" in prefix.name.casefold()
            else "python-qgis.bat"
        )
        launcher = root / "bin" / launcher_name
        if not launcher.is_file():
            raise RuntimeError("python-qgis.bat was not found.")
        return (
            os.environ.get("COMSPEC", "cmd.exe"),
            ["/d", "/c", str(launcher), str(wrapper), str(manifest), str(result)],
        )
    executable = sys.executable
    if not executable or not Path(executable).is_file():
        raise RuntimeError("A QGIS Python executable was not found.")
    return executable, [str(wrapper), str(manifest), str(result)]


_SUBPROCESS_WRAPPER = r'''
import json
import sys
from pathlib import Path
from qgis.core import QgsApplication, QgsProject, QgsVectorFileWriter, QgsVectorLayer

manifest_path, result_path = Path(sys.argv[1]), Path(sys.argv[2])
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
app = QgsApplication([], False)
app.initQgis()
processing_plugins = str(Path(QgsApplication.prefixPath()) / "python" / "plugins")
if processing_plugins not in sys.path:
    sys.path.append(processing_plugins)
from processing.core.Processing import Processing
Processing.initialize()
project = QgsProject.instance()
initial = set()
smartmodeler_input_layers = []
for name in manifest["input_names"]:
    layer = QgsVectorLayer(f'{manifest["input_gpkg"]}|layername={name}', name, "ogr")
    if layer.isValid():
        project.addMapLayer(layer)
        initial.add(layer.id())
        smartmodeler_input_layers.append(layer)
smartmodeler_input_layer_ids = [layer.id() for layer in smartmodeler_input_layers]
smartmodeler_parameters = manifest["parameters"]
smartmodeler_output_dir = str(manifest_path.parent)
source = manifest_path.with_name("user_code.py").read_text(encoding="utf-8")
exec(compile(source, "<SmartModeler generated PyQGIS>", "exec"), globals(), globals())
outputs = []
requested = set(manifest["output_names"])
for layer_id, layer in project.mapLayers().items():
    if layer_id in initial or (requested and layer.name() not in requested):
        continue
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = layer.name()
    options.actionOnExistingFile = (
        QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile
        if not outputs else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
    )
    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, manifest["output_gpkg"], project.transformContext(), options
    )
    if result[0] == QgsVectorFileWriter.WriterError.NoError:
        outputs.append(layer.name())
Path(result_path).write_text(json.dumps({"outputs": outputs}), encoding="utf-8")
app.exitQgis()
'''
