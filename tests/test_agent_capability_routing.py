"""Routing audit: the words a user actually types must reach the capability.

Three separate production failures had one shape. A capability existed, the
request was ordinary, and the keyword table did not contain the word the user
chose -- so the pack never loaded and the Agent reported, correctly for what it
could see, that it could not help:

* "m2 biriminde alan sütüunu aç" missed the expression pack over one typo, so
  the area column was created as an Integer.
* "jenks olarak sınıflandır" missed the style pack, so the Agent answered that
  no styling tool existed and refused even after the user settled for equal
  interval.
* An OSM request phrased around anything but roads/buildings/trees missed the
  downloader pack.

This matrix is the standing guard. Each row is a request phrased the way a
QGIS user in this project actually phrases it -- Turkish first, since that is
the working language -- and the capability it must reach. A new row is cheaper
than another production dead end.
"""
from __future__ import annotations

import unittest

from planx_smartmodeler.core.agent.contracts import AgentScope, AgentToolSpec
from planx_smartmodeler.core.agent.prompt_builder import select_tools_for_request
from planx_smartmodeler.core.prompt_context import PromptContextLoader
from pathlib import Path

EMPTY_SCHEMA = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

ALL_TOOLS = (
    "project.summary",
    "layer.list",
    "layer.describe",
    "layer.style",
    "processing.search",
    "processing.describe",
    "processing.resolve",
    "expression.search",
    "plugin.list",
    "plugin.describe",
    "plugin.capabilities",
)


def tools(scope: str):
    return [
        AgentToolSpec(
            name=name,
            title="Title",
            description="Description",
            risk="read_only",
            input_schema=EMPTY_SCHEMA,
            allowed_scopes=(scope,),
        )
        for name in ALL_TOOLS
    ]


# (request, tools that must be advertised)
PROCESSING = "processing.resolve"
DESCRIBE = "layer.describe"
STYLE = "layer.style"
CAPABILITIES = "plugin.capabilities"

ROUTING_MATRIX = (
    # -- geometry operations, phrased in Turkish --------------------------
    ("binaların etrafına 50 metre tampon oluştur", (PROCESSING,)),
    ("katmanı EPSG:3857'ye dönüştür", (PROCESSING,)),
    ("iki katmanı birleştir", (PROCESSING,)),
    ("iki katmanın kesişimini al", (PROCESSING,)),
    ("poligonların merkez noktalarını çıkar", (PROCESSING,)),
    ("katmanı sınır poligonuyla kırp", (PROCESSING,)),
    ("aynı değere sahip poligonları erit", (PROCESSING,)),
    ("her poligonun içindeki noktaları say", (PROCESSING,)),
    ("rastgele 10 özellik seç", (PROCESSING,)),
    ("bozuk geometrileri onar", (PROCESSING,)),
    ("öznitelik tablosunu diğer katmanla eşleştir", (PROCESSING,)),
    # -- the same operations in English ------------------------------------
    ("buffer the buildings by 50 metres", (PROCESSING,)),
    ("reproject this layer to EPSG:3857", (PROCESSING,)),
    ("dissolve by district", (PROCESSING,)),
    ("clip the roads to the boundary", (PROCESSING,)),
    ("fix invalid geometries", (PROCESSING,)),
    # -- attribute and expression work -------------------------------------
    ("m2 biriminde alan sütunu aç", (PROCESSING, DESCRIBE)),
    ("alan hesapla ve yeni sütun ekle", (PROCESSING, DESCRIBE)),
    ("400 m2 altındaki binaları filtrele", (PROCESSING, DESCRIBE)),
    # -- classification and styling ----------------------------------------
    ("alan sütununa göre jenks olarak sınıflandır", (STYLE, DESCRIBE)),
    ("natural breaks ile 5 sınıfa ayır", (STYLE,)),
    ("quantile sınıflandırma yap", (STYLE,)),
    ("katmanı kategorilere göre renklendir", (STYLE,)),
    ("classify the buildings by area", (STYLE,)),
    ("binalara etiket ekle", (STYLE,)),
    # -- OSM acquisition, beyond roads/buildings/trees ---------------------
    ("map extent içindeki okulları indir", (PROCESSING, CAPABILITIES)),
    ("parkları ve yeşil alanları indir", (PROCESSING, CAPABILITIES)),
    ("nehirleri ve su yollarını indir", (PROCESSING, CAPABILITIES)),
    ("sokakları ve caddeleri indir", (PROCESSING, CAPABILITIES)),
    ("hastane ve eczaneleri getir", (PROCESSING, CAPABILITIES)),
    ("arazi kullanımı verisini indir", (PROCESSING, CAPABILITIES)),
    ("download the schools in the current extent", (PROCESSING, CAPABILITIES)),
    ("download landuse polygons", (PROCESSING, CAPABILITIES)),
)


class CapabilityRoutingTests(unittest.TestCase):
    def test_every_request_reaches_its_capability(self) -> None:
        for request, required in ROUTING_MATRIX:
            for scope in (AgentScope.PROJECT, AgentScope.ACTIVE_LAYER):
                with self.subTest(request=request, scope=scope):
                    selected = {
                        spec.name
                        for spec in select_tools_for_request(tools(scope), scope, request)
                    }
                    for name in required:
                        self.assertTrue(
                            name in selected,
                            f"GAP tools: {request!r} -> {name}",
                        )


# (request, documentation packs that must load)
DOC_MATRIX = (
    ("m2 biriminde alan sütunu aç", "# QGIS expressions"),
    ("alan hesapla", "# QGIS expressions"),
    ("400 m2 altındaki binaları seç", "# QGIS expressions"),
    ("yeni bir sütun ekle", "# QGIS expressions"),
    ("map extent içindeki okulları indir", "# OSM acquisition"),
    ("parkları indir", "# OSM acquisition"),
    ("nehirleri indir", "# OSM acquisition"),
    ("download landuse polygons", "# OSM acquisition"),
    ("sokakları indir", "# OSM acquisition"),
    ("alan sütununa göre jenks olarak sınıflandır", "# Layer styling and classification"),
    ("natural breaks ile 5 sınıfa ayır", "# Layer styling and classification"),
    ("quantile sınıflandırma yap", "# Layer styling and classification"),
    ("katmanı kategorilere göre renklendir", "# Layer styling and classification"),
    ("classify the buildings by area", "# Layer styling and classification"),
    ("binalara etiket ekle", "# Layer styling and classification"),
)


class DocumentationRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loader = PromptContextLoader(
            context_dir=Path(__file__).resolve().parents[1] / "agent_context"
        )

    def test_every_request_loads_its_documentation_pack(self) -> None:
        for request, heading in DOC_MATRIX:
            with self.subTest(request=request):
                text = self.loader.agent_context(request, AgentScope.PROJECT)
                self.assertTrue(
                    heading in text,
                    f"GAP docs: {request!r} -> {heading}",
                )

    def test_an_ordinary_listing_request_stays_lean(self) -> None:
        # The packs are routed, not always-on: a plain question must not drag
        # every reference pack into the fixed per-turn cost.
        text = self.loader.agent_context("katmanlarımı listele", AgentScope.PROJECT)
        self.assertNotIn("# OSM acquisition", text)
        self.assertNotIn("# QGIS expressions", text)
        self.assertNotIn("# Layer styling and classification", text)


if __name__ == "__main__":
    unittest.main()
