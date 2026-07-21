"""Smart Proposal & Contextual Tip Engine for SmartModeler GIS (QGIS 4)."""
from typing import List, Dict, Any
from .graph_model import SocketType, NodeDefinition, NodePort


class ProposalRecommendation:
    """A recommended next node action or algorithm proposal."""

    def __init__(self, alg_id: str, title: str, category: str, description: str, icon_name: str = "node_add.png"):
        self.alg_id = alg_id
        self.title = title
        self.category = category
        self.description = description
        self.icon_name = icon_name


class SmartProposalEngine:
    """Analyzes context in the canvas (e.g. selected node output port) to recommend optimal next algorithms."""

    # Contextual mapping database for instant smart recommendations
    PROPOSAL_RULES: Dict[str, List[Dict[str, str]]] = {
        SocketType.VECTOR: [
            {"alg_id": "native:buffer", "title": "Buffer", "category": "Vector Geometry", "description": "Create distance buffers around vector features."},
            {"alg_id": "native:clip", "title": "Clip", "category": "Vector Overlay", "description": "Cut vector layer boundaries using a mask."},
            {"alg_id": "native:extractbyattribute", "title": "Extract by Attribute", "category": "Vector Selection", "description": "Filter features matching expression/attribute criteria."},
            {"alg_id": "native:centroids", "title": "Centroids", "category": "Vector Geometry", "description": "Calculate geometric centroids for polygon features."},
            {"alg_id": "native:joinattributestable", "title": "Join Attributes by Field", "category": "Table Operations", "description": "Join external data attributes based on key fields."},
            {"alg_id": "native:fieldcalculator", "title": "Field Calculator", "category": "Vector Table", "description": "Compute new attribute column values using expressions."},
        ],
        SocketType.RASTER: [
            {"alg_id": "gdal:contour", "title": "Contour Lines", "category": "Raster Surface", "description": "Generate vector elevation contours from DEM raster."},
            {"alg_id": "native:slope", "title": "Slope Calculation", "category": "Raster Terrain", "description": "Compute terrain slope angles in degrees or percentage."},
            {"alg_id": "native:aspect", "title": "Aspect Calculation", "category": "Raster Terrain", "description": "Compute terrain aspect direction angles."},
            {"alg_id": "native:rastercalculator", "title": "Raster Calculator", "category": "Raster Analysis", "description": "Apply mathematical raster expressions across bands."},
            {"alg_id": "native:zonalstatisticsfb", "title": "Zonal Statistics", "category": "Raster & Vector", "description": "Calculate raster statistics grouped by polygon zones."},
        ],
        SocketType.NUMBER: [
            {"alg_id": "smart:slider", "title": "Value Slider Widget", "category": "Parameters", "description": "Connect an interactive numeric slider control."},
            {"alg_id": "native:buffer", "title": "Pass as Buffer Distance", "category": "Vector Geometry", "description": "Use numeric output directly as distance input."},
        ]
    }

    @classmethod
    def get_proposals_for_port(cls, port: NodePort) -> List[ProposalRecommendation]:
        """Returns smart proposals based on a selected port socket."""
        rules = cls.PROPOSAL_RULES.get(port.socket_type, [])
        if not rules and port.is_output:
            rules = cls.PROPOSAL_RULES.get(SocketType.VECTOR, [])

        results = []
        for r in rules:
            results.append(ProposalRecommendation(
                alg_id=r["alg_id"],
                title=r["title"],
                category=r["category"],
                description=r["description"]
            ))
        return results

    @classmethod
    def get_starter_templates(cls) -> List[Dict[str, Any]]:
        """Returns micro-package starter templates for single-click model loading."""
        return [
            {
                "id": "tpl_isochrone",
                "name": "15-Min Urban Isochrone Grid",
                "description": "Calculates buffer zones and intersects grid population cells.",
                "nodes": ["Vector Input", "Buffer (800m)", "Grid Generator", "Zonal Statistics"]
            },
            {
                "id": "tpl_extrusion_3d",
                "name": "3D Massing Extrusion & Roof",
                "description": "Generates 3D volumetric massing with floor height calculation.",
                "nodes": ["Building Footprints", "Field Calc (Height)", "Extrude 3D", "Roof Generator"]
            },
            {
                "id": "tpl_suitability",
                "name": "MCDA Land Suitability Overlay",
                "description": "Reclassifies slopes and buffers to produce a weighted suitability map.",
                "nodes": ["DEM Raster", "Slope", "Buffer Constraints", "Weighted Raster Math"]
            }
        ]
