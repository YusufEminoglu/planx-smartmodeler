"""Ranked, schema-aware next-step proposals for the visual modeler."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from .algorithm_catalog import AlgorithmCatalog
from .graph_model import NodeDefinition, NodePort, SocketType


@dataclass(frozen=True)
class ProposalRecommendation:
    """One compatible add-and-connect action."""

    alg_id: str
    title: str
    category: str
    description: str
    target_port_id: str
    score: int
    reason: str
    preview: str
    source_node_id: str = ""
    source_port_id: str = ""
    icon_name: str = "node_add.png"


class SmartProposalEngine:
    """Rank installed algorithms against one selected output contract."""

    PROPOSAL_RULES: Dict[str, Sequence[Dict[str, object]]] = {
        SocketType.VECTOR: (
            {
                "alg_id": "native:buffer",
                "title": "Buffer",
                "category": "Vector Geometry",
                "description": "Create distance buffers around vector features.",
                "targets": ("INPUT",),
                "weight": 100,
            },
            {
                "alg_id": "native:clip",
                "title": "Clip",
                "category": "Vector Overlay",
                "description": "Use this layer as the features to clip.",
                "targets": ("INPUT",),
                "weight": 92,
            },
            {
                "alg_id": "native:extractbyexpression",
                "title": "Extract by Expression",
                "category": "Vector Selection",
                "description": "Filter features with a QGIS expression.",
                "targets": ("INPUT",),
                "weight": 88,
            },
            {
                "alg_id": "native:centroids",
                "title": "Centroids",
                "category": "Vector Geometry",
                "description": "Calculate geometric centroids.",
                "targets": ("INPUT",),
                "weight": 82,
            },
            {
                "alg_id": "native:fieldcalculator",
                "title": "Field Calculator",
                "category": "Vector Table",
                "description": "Compute a new attribute column.",
                "targets": ("INPUT",),
                "weight": 74,
            },
        ),
        SocketType.RASTER: (
            {
                "alg_id": "native:slope",
                "title": "Slope",
                "category": "Raster Terrain",
                "description": "Compute terrain slope from this raster.",
                "targets": ("INPUT",),
                "weight": 100,
            },
            {
                "alg_id": "native:aspect",
                "title": "Aspect",
                "category": "Raster Terrain",
                "description": "Compute terrain aspect from this raster.",
                "targets": ("INPUT",),
                "weight": 92,
            },
            {
                "alg_id": "gdal:contour",
                "title": "Contour Lines",
                "category": "Raster Surface",
                "description": "Generate vector elevation contours.",
                "targets": ("INPUT",),
                "weight": 84,
            },
        ),
        SocketType.NUMBER: (
            {
                "alg_id": "native:buffer",
                "title": "Buffer distance",
                "category": "Vector Geometry",
                "description": "Use this value as a buffer distance.",
                "targets": ("DISTANCE",),
                "weight": 100,
            },
        ),
    }

    @classmethod
    def get_proposals_for_port(
        cls,
        port: NodePort,
        source_node: Optional[NodeDefinition] = None,
    ) -> List[ProposalRecommendation]:
        """Return only candidates with a compatible live target input."""
        if not port.is_output:
            return []
        rules = cls.PROPOSAL_RULES.get(port.socket_type, ())
        proposals: List[ProposalRecommendation] = []
        for rule in rules:
            algorithm_id = str(rule["alg_id"])
            if not AlgorithmCatalog.algorithm_exists(algorithm_id):
                continue
            try:
                candidate = AlgorithmCatalog.create_node(algorithm_id)
            except ValueError:
                continue
            target = cls._best_target(
                port,
                candidate,
                tuple(str(value) for value in rule.get("targets", ())),
            )
            if target is None:
                continue
            base_score = int(rule.get("weight", 0))
            score = base_score + (15 if target.required else 0)
            source_label = (
                f"{source_node.title}.{port.name}"
                if source_node is not None
                else port.name
            )
            target_label = f"{candidate.title}.{target.name}"
            proposals.append(
                ProposalRecommendation(
                    alg_id=algorithm_id,
                    title=str(rule["title"]),
                    category=str(rule["category"]),
                    description=str(rule["description"]),
                    target_port_id=target.port_id,
                    score=score,
                    reason=(
                        f"{source_label} is {port.socket_type}; "
                        f"{target_label} accepts {target.socket_type}."
                    ),
                    preview=f"Add {candidate.title} and connect to {target.name}.",
                    source_node_id=source_node.node_id if source_node else "",
                    source_port_id=port.port_id if source_node else "",
                )
            )
        return sorted(
            proposals,
            key=lambda item: (-item.score, item.title.lower(), item.alg_id),
        )

    @staticmethod
    def _best_target(
        output: NodePort,
        candidate: NodeDefinition,
        preferred_ids: Sequence[str],
    ) -> Optional[NodePort]:
        compatible = [
            target
            for target in candidate.inputs.values()
            if not target.is_connected()
            and candidate.algorithm_id
            and (
                output.socket_type == target.socket_type
                or SocketType.ANY in (output.socket_type, target.socket_type)
            )
        ]
        if not compatible:
            return None
        preference = {
            port_id: index for index, port_id in enumerate(preferred_ids)
        }
        return min(
            compatible,
            key=lambda target: (
                preference.get(target.port_id, len(preference) + 1),
                not target.required,
                target.port_id,
            ),
        )

    @staticmethod
    def get_starter_templates() -> List[Dict[str, object]]:
        """Return validated, currently available micro-package summaries."""
        from .micro_packages import MicroPackageCatalog

        return [item.to_dict() for item in MicroPackageCatalog.available()]
