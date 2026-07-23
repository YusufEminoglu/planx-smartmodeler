from __future__ import annotations

import unittest

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


class FakeMarkdown:
    def __init__(self, name: str, content: str) -> None:
        self.name = name
        self.content = content

    def read_text(self, encoding: str) -> str:
        self.asserted_encoding = encoding
        return self.content


if __name__ == "__main__":
    unittest.main()
