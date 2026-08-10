from __future__ import annotations

import unittest
from pathlib import Path

from planx_smartmodeler.core.prompt_context import PromptContextLoader


class PromptContextTests(unittest.TestCase):
    def test_markdown_order_and_runtime_boundaries(self) -> None:
        loader = PromptContextLoader()
        loader.files = lambda: [FakeMarkdown(
            "10_FIRST.md", "first"), FakeMarkdown("20_SECOND.md", "second")]
        context = loader.build(
            "- name=roads",
            "- native:buffer | inputs=[INPUT:vector] | outputs=[OUTPUT:vector]",
            '{"nodes":[{"id":"roads"}]}',
        )
        self.assertLess(context.index("first"), context.index("second"))
        self.assertIn("untrusted data", context)
        self.assertIn("native:buffer", context)
        self.assertIn("Current workflow baseline", context)
        self.assertIn('"id":"roads"', context)

    def test_static_context_is_bounded(self) -> None:
        loader = PromptContextLoader()
        loader.files = lambda: [FakeMarkdown("00_LARGE.md", "x" * 50000)]
        context = loader.static_context()
        self.assertLessEqual(len(context), PromptContextLoader.MAX_STATIC_CHARS + 30)

    def test_runtime_context_is_bounded(self) -> None:
        loader = PromptContextLoader()
        loader.files = lambda: []
        oversized = "x" * (PromptContextLoader.MAX_RUNTIME_CHARS + 5000)
        context = loader.build(oversized, oversized)
        self.assertLess(len(context), PromptContextLoader.MAX_RUNTIME_CHARS * 2 + 500)

    def test_agent_context_loads_only_task_specific_packs(self) -> None:
        loader = PromptContextLoader(
            context_dir=Path(__file__).resolve().parents[1] / "agent_context"
        )
        basic = loader.agent_context("List my layers", "project")
        expression = loader.agent_context("Use rand(1, 15)", "project")
        osm = loader.agent_context("Download OSM roads and buildings", "project")
        python_off = loader.agent_context("Run a PyQGIS script", "project")
        python_on = loader.agent_context(
            "Run a PyQGIS script", "project", power_enabled=True
        )
        generic_power_on = loader.agent_context(
            "hazır", "project", power_enabled=True
        )
        # The Current model scope accepts only model_patch/model_run, and the
        # compact core documents neither. Without this pack Workflow Studio
        # asked a provider for a payload whose schema it had never been shown,
        # and got three different guesses in a row.
        workflow = loader.agent_context("Build a slope workflow", "current_model")
        self.assertIn("model_patch", workflow)
        self.assertIn('"op":"add_node"', workflow)
        self.assertIn("operations", workflow)
        self.assertNotIn('"op":"add_node"', basic)

        self.assertIn("SmartModeler Agent core contract", basic)
        self.assertNotIn("# QGIS expressions", basic)
        self.assertIn("# QGIS expressions", expression)
        self.assertIn("# OSM acquisition", osm)
        self.assertNotIn("# Power Mode", python_off)
        self.assertIn("# Power Mode", python_on)
        self.assertIn("# Power Mode", generic_power_on)
        # The core pack is the fixed per-turn cost, so it stays bounded. The
        # budget was raised from 5 000 once: two silent-failure classes seen in
        # real runs -- a choice bound by miscounted index, and a multi-run
        # request answered with prose instead of the first proposal -- are only
        # preventable from this always-loaded pack, and squeezing the wording
        # further was costing clarity rather than tokens.
        # Raised again to 5 900 for the worst class yet: an owner session where
        # a filter returned nothing and the agent explained the absence
        # ("all buildings must be over 400 m2") instead of reading the field.
        # A fabricated conclusion is not task-specific, so the rule against it
        # cannot live in an on-demand pack.
        self.assertLess(len(basic), 5_900)


class FakeMarkdown:
    def __init__(self, name: str, content: str) -> None:
        self.name = name
        self.content = content

    def read_text(self, encoding: str) -> str:
        self.asserted_encoding = encoding
        return self.content


if __name__ == "__main__":
    unittest.main()
