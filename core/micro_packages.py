"""Versioned, validated micro-package workflow catalog."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .graph_model import GraphModel


class MicroPackageError(ValueError):
    """Raised when a shipped workflow package violates its schema."""


@dataclass(frozen=True)
class MicroPackageSummary:
    package_id: str
    name: str
    description: str
    tags: tuple[str, ...]
    node_count: int

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.package_id,
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
            "node_count": self.node_count,
        }


class MicroPackageCatalog:
    """Loads trusted resources through a strict schema before graph creation."""

    SCHEMA_VERSION = 1
    MAX_PACKAGES = 50
    MAX_NODES = 40
    MAX_CONNECTIONS = 120
    ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    RESOURCE_PATH = (
        Path(__file__).resolve().parent.parent
        / "resources"
        / "micro_packages.json"
    )
    _cache: Optional[Dict[str, Dict[str, Any]]] = None

    @classmethod
    def load(cls) -> Dict[str, Dict[str, Any]]:
        if cls._cache is None:
            try:
                data = json.loads(cls.RESOURCE_PATH.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise MicroPackageError(
                    "The shipped micro-package catalog is unavailable."
                ) from error
            cls._cache = cls._validate_catalog(data)
        return dict(cls._cache)

    @classmethod
    def available(cls, catalog=None) -> List[MicroPackageSummary]:
        if catalog is None:
            from .algorithm_catalog import AlgorithmCatalog

            catalog = AlgorithmCatalog
        result = []
        for package in cls.load().values():
            if not all(
                catalog.algorithm_exists(node["algorithm_id"])
                for node in package["nodes"]
            ):
                continue
            result.append(
                MicroPackageSummary(
                    package["id"],
                    package["name"],
                    package["description"],
                    tuple(package["tags"]),
                    len(package["nodes"]),
                )
            )
        return sorted(
            result,
            key=lambda item: (
                0 if "showcase" in item.tags else 1,
                item.name.lower(),
            ),
        )

    @classmethod
    def instantiate(cls, package_id: str, catalog=None) -> GraphModel:
        if catalog is None:
            from .algorithm_catalog import AlgorithmCatalog

            catalog = AlgorithmCatalog
        package = cls.load().get(package_id)
        if package is None:
            raise MicroPackageError("Unknown micro-package.")
        graph = GraphModel(package["name"])
        graph.description = package["description"]
        for spec in package["nodes"]:
            try:
                node = catalog.create_node(
                    spec["algorithm_id"],
                    spec["id"],
                    spec["title"],
                )
            except ValueError as error:
                raise MicroPackageError(
                    f"Required algorithm is unavailable: {spec['algorithm_id']}"
                ) from error
            for name, value in spec["parameters"].items():
                if (
                    name not in node.inputs
                    and name not in ("LAYER", "VALUE")
                ):
                    raise MicroPackageError(
                        "A micro-package parameter is not in the live signature."
                    )
                node.set_parameter(name, value)
            graph.add_node(node)
        for spec in package["connections"]:
            if graph.add_edge(
                spec["from_node"],
                spec["from_port"],
                spec["to_node"],
                spec["to_port"],
            ) is None:
                raise MicroPackageError(
                    f"Invalid micro-package connection: {graph.last_error}"
                )
        for output in package["outputs"]:
            node = graph.nodes.get(output["node_id"])
            if node is None or output["output_name"] not in node.outputs:
                raise MicroPackageError(
                    "A micro-package output is not in the live signature."
                )
            graph.outputs[output["name"]] = dict(output)
            graph.outputs[output["name"]].pop("name")
        graph.outputs_declared = True
        from .auto_layout import AutoLayoutEngine

        AutoLayoutEngine.apply_layout(graph)
        return graph

    @classmethod
    def _validate_catalog(
        cls, data: Any
    ) -> Dict[str, Dict[str, Any]]:
        if not isinstance(data, dict) or set(data) != {
            "schema_version",
            "packages",
        }:
            raise MicroPackageError("Invalid micro-package catalog fields.")
        if data["schema_version"] != cls.SCHEMA_VERSION:
            raise MicroPackageError("Unsupported micro-package schema version.")
        packages = data["packages"]
        if not isinstance(packages, list) or len(packages) > cls.MAX_PACKAGES:
            raise MicroPackageError("Invalid micro-package collection.")
        validated: Dict[str, Dict[str, Any]] = {}
        for package in packages:
            cls._validate_package(package)
            if package["id"] in validated:
                raise MicroPackageError("Duplicate micro-package id.")
            validated[package["id"]] = package
        return validated

    @classmethod
    def _validate_package(cls, package: Any) -> None:
        expected = {
            "id",
            "name",
            "description",
            "tags",
            "nodes",
            "connections",
            "outputs",
        }
        if not isinstance(package, dict) or set(package) != expected:
            raise MicroPackageError("Invalid micro-package fields.")
        cls._identifier(package["id"], "package")
        cls._text(package["name"], 200)
        cls._text(package["description"], 2_000)
        if (
            not isinstance(package["tags"], list)
            or len(package["tags"]) > 12
        ):
            raise MicroPackageError("Invalid micro-package tags.")
        for tag in package["tags"]:
            cls._text(tag, 50)
        nodes = package["nodes"]
        connections = package["connections"]
        outputs = package["outputs"]
        if (
            not isinstance(nodes, list)
            or not nodes
            or len(nodes) > cls.MAX_NODES
            or not isinstance(connections, list)
            or len(connections) > cls.MAX_CONNECTIONS
            or not isinstance(outputs, list)
            or len(outputs) > cls.MAX_NODES
        ):
            raise MicroPackageError("Invalid micro-package graph collections.")
        node_ids = set()
        for node in nodes:
            if not isinstance(node, dict) or set(node) != {
                "id",
                "algorithm_id",
                "title",
                "parameters",
            }:
                raise MicroPackageError("Invalid micro-package node.")
            node_id = cls._identifier(node["id"], "node")
            if node_id in node_ids:
                raise MicroPackageError("Duplicate micro-package node id.")
            node_ids.add(node_id)
            cls._algorithm_id(node["algorithm_id"])
            cls._text(node["title"], 200)
            if not isinstance(node["parameters"], dict):
                raise MicroPackageError("Invalid micro-package parameters.")
        for connection in connections:
            if not isinstance(connection, dict) or set(connection) != {
                "from_node",
                "from_port",
                "to_node",
                "to_port",
            }:
                raise MicroPackageError("Invalid micro-package connection.")
            if (
                connection["from_node"] not in node_ids
                or connection["to_node"] not in node_ids
            ):
                raise MicroPackageError("Dangling micro-package connection.")
            cls._identifier(connection["from_port"], "port")
            cls._identifier(connection["to_port"], "port")
        output_names = set()
        for output in outputs:
            if not isinstance(output, dict) or set(output) != {
                "name",
                "node_id",
                "output_name",
                "description",
                "mandatory",
                "default",
            }:
                raise MicroPackageError("Invalid micro-package output.")
            name = cls._identifier(output["name"], "output")
            if name in output_names or output["node_id"] not in node_ids:
                raise MicroPackageError("Invalid micro-package output reference.")
            output_names.add(name)
            cls._identifier(output["output_name"], "output port")
            cls._text(output["description"], 500)
            if not isinstance(output["mandatory"], bool):
                raise MicroPackageError("Invalid micro-package output flag.")

    @classmethod
    def _identifier(cls, value: Any, label: str) -> str:
        if not isinstance(value, str) or not cls.ID_PATTERN.fullmatch(value):
            raise MicroPackageError(f"Invalid micro-package {label} id.")
        return value

    @staticmethod
    def _algorithm_id(value: Any) -> str:
        if (
            not isinstance(value, str)
            or len(value) > 128
            or ":" not in value
        ):
            raise MicroPackageError("Invalid micro-package algorithm id.")
        return value

    @staticmethod
    def _text(value: Any, limit: int) -> str:
        if not isinstance(value, str) or len(value) > limit:
            raise MicroPackageError("Invalid micro-package text.")
        return value
