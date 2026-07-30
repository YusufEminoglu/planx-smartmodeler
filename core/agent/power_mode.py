"""Application-owned Power Mode settings, resources and trusted script library."""
from __future__ import annotations

import hashlib
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from qgis.core import QgsApplication, QgsProviderRegistry, QgsSettings

POWER_SETTINGS_KEY = "SmartModelerGIS/Agent/powerModeEnabled"
RESOURCE_TTL_SECONDS = 300.0


class PowerModeSettings:
    """One explicit, default-off persistent Power Mode switch."""

    def __init__(self, settings: Optional[QgsSettings] = None) -> None:
        self.settings = settings or QgsSettings()

    def enabled(self) -> bool:
        value = self.settings.value(POWER_SETTINGS_KEY, False)
        if isinstance(value, str):
            return value.strip().casefold() in ("1", "true", "yes", "on")
        return bool(value)

    def set_enabled(self, enabled: bool) -> None:
        self.settings.setValue(POWER_SETTINGS_KEY, bool(enabled))


@dataclass(frozen=True)
class PowerResource:
    kind: str
    provider: str
    resource_id: str
    display_name: str
    expires_at: float


class PowerResourceRegistry:
    """Short-lived opaque handles; provider-visible tokens never contain URIs."""

    def __init__(self) -> None:
        self._resources: Dict[str, PowerResource] = {}

    def rotate(self) -> None:
        self._resources.clear()

    def issue(
        self, kind: str, provider: str, resource_id: str, display_name: str
    ) -> str:
        self._purge()
        token = secrets.token_urlsafe(24)
        self._resources[token] = PowerResource(
            kind=str(kind),
            provider=str(provider),
            resource_id=str(resource_id),
            display_name=str(display_name)[:160],
            expires_at=time.monotonic() + RESOURCE_TTL_SECONDS,
        )
        return token

    def resolve(self, token: str, kind: str) -> Optional[PowerResource]:
        self._purge()
        item = self._resources.get(str(token))
        if item is None or item.kind != kind:
            return None
        return item

    def _purge(self) -> None:
        now = time.monotonic()
        self._resources = {
            key: value
            for key, value in self._resources.items()
            if value.expires_at > now
        }


def _connection_is_geopackage(connection: Any) -> bool:
    """Inspect locally without returning the URI to a caller."""
    try:
        uri = str(connection.uri() or "").casefold()
    except Exception:
        uri = ""
    return ".gpkg" in uri or "geopackage" in uri


def database_connections(
    resources: PowerResourceRegistry,
) -> List[Dict[str, Any]]:
    """Return names/providers only for stored PostGIS and GeoPackage connections."""
    rows: List[Dict[str, Any]] = []
    registry = QgsProviderRegistry.instance()
    for provider_id in ("postgres", "ogr"):
        metadata = registry.providerMetadata(provider_id)
        if metadata is None:
            continue
        try:
            connections = metadata.connections(False)
        except Exception:
            connections = {}
        if not isinstance(connections, dict):
            continue
        for name, connection in sorted(connections.items()):
            if provider_id == "ogr" and not _connection_is_geopackage(connection):
                continue
            display = str(name)[:160]
            rows.append(
                {
                    "connection_token": resources.issue(
                        "database", provider_id, str(name), display
                    ),
                    "provider": provider_id,
                    "name": display,
                    "transaction_support": _supports_transactions(connection),
                }
            )
    return rows[:50]


def _supports_transactions(connection: Any) -> bool:
    try:
        capabilities = connection.capabilities()
        enum = type(connection).Capability
        transaction_flag = getattr(enum, "Transaction", None)
        return bool(transaction_flag is not None and capabilities & transaction_flag)
    except Exception:
        return False


def resolve_connection(resource: PowerResource) -> Any:
    metadata = QgsProviderRegistry.instance().providerMetadata(resource.provider)
    if metadata is None:
        return None
    try:
        return metadata.findConnection(resource.resource_id, False)
    except Exception:
        return None


def describe_database(
    resources: PowerResourceRegistry,
    connection_token: str,
    limit: int = 40,
    *,
    schema_name: str = "",
    table_name: str = "",
) -> Dict[str, Any]:
    resource = resources.resolve(connection_token, "database")
    if resource is None:
        return {"available": False}
    connection = resolve_connection(resource)
    if connection is None:
        return {"available": False}
    rows = []
    columns = []
    try:
        schemas = list(connection.schemas()) or [""]
    except Exception:
        schemas = [""]
    for schema in schemas[:20]:
        try:
            tables = connection.tables(str(schema))
        except Exception:
            tables = []
        for table in list(tables)[: max(1, int(limit))]:
            name = ""
            try:
                name = str(table.tableName())
            except Exception:
                name = str(table)
            rows.append({"schema": str(schema)[:128], "table": name[:160]})
            if len(rows) >= limit:
                break
        if len(rows) >= limit:
            break
    if table_name:
        try:
            fields = connection.fields(str(schema_name), str(table_name))
        except Exception:
            fields = []
        for field in list(fields)[:100]:
            column = None
            try:
                column = {
                    "name": str(field.name())[:128],
                    "type": str(field.typeName())[:80],
                }
            except Exception:
                column = None
            if column is not None:
                columns.append(column)
    fresh = resources.issue(
        "database", resource.provider, resource.resource_id, resource.display_name
    )
    return {
        "available": True,
        "connection_token": fresh,
        "provider": resource.provider,
        "name": resource.display_name,
        "transaction_support": _supports_transactions(connection),
        "tables": rows,
        "selected_table": {
            "schema": str(schema_name)[:128],
            "table": str(table_name)[:160],
            "columns": columns,
        } if table_name else None,
        "truncated": len(rows) >= limit,
    }


@dataclass(frozen=True)
class TrustedScript:
    script_id: str
    name: str
    description: str
    script_hash: str
    source_path: Path
    parameters: Dict[str, Any]


class ScriptLibrary:
    """Managed, hash-pinned copies of scripts approved by the user."""

    def __init__(self, root: Optional[Path] = None) -> None:
        base = Path(QgsApplication.qgisSettingsDirPath())
        self.root = root or base / "planx_smartmodeler" / "scripts"

    def import_script(
        self,
        source_path: Path,
        *,
        name: str = "",
        description: str = "",
        parameters: Optional[Dict[str, Any]] = None,
    ) -> TrustedScript:
        source = Path(source_path).read_bytes()
        if len(source) > 500_000:
            raise ValueError("Script exceeds the 500 KB library limit.")
        compile(source.decode("utf-8"), str(source_path), "exec")
        script_id = uuid.uuid4().hex
        digest = hashlib.sha256(source).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        managed = self.root / f"{script_id}.py"
        manifest = self.root / f"{script_id}.json"
        managed.write_bytes(source)
        data = {
            "schema_version": 1,
            "script_id": script_id,
            "name": (name or Path(source_path).stem)[:160],
            "description": str(description)[:500],
            "script_hash": digest,
            "parameters": dict(parameters or {}),
        }
        manifest.write_text(
            json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        return self.get(script_id)

    def list(self) -> Tuple[TrustedScript, ...]:
        if not self.root.is_dir():
            return ()
        scripts = []
        for manifest in sorted(self.root.glob("*.json"))[:100]:
            item = self._read_manifest(manifest)
            if item is not None:
                scripts.append(item)
        return tuple(scripts)

    def get(self, script_id: str) -> TrustedScript:
        item = self._read_manifest(self.root / f"{script_id}.json")
        if item is None:
            raise ValueError("Trusted script is missing or its hash changed.")
        return item

    def _read_manifest(self, manifest: Path) -> Optional[TrustedScript]:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            script_id = str(data["script_id"])
            source_path = self.root / f"{script_id}.py"
            source = source_path.read_bytes()
            digest = hashlib.sha256(source).hexdigest()
            if digest != str(data["script_hash"]):
                return None
            params = data.get("parameters", {})
            if not isinstance(params, dict):
                return None
            return TrustedScript(
                script_id=script_id,
                name=str(data.get("name", script_id))[:160],
                description=str(data.get("description", ""))[:500],
                script_hash=digest,
                source_path=source_path,
                parameters=params,
            )
        except (OSError, ValueError, KeyError, TypeError, UnicodeError):
            return None
