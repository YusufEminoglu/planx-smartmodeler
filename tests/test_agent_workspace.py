"""Pure regression tests for the bounded Developer Workspace boundary."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from planx_smartmodeler.core.agent.contracts import AgentScope
from planx_smartmodeler.core.agent.context_tokens import ContextTokenService
from planx_smartmodeler.core.agent.prompt_builder import select_tools_for_request
from planx_smartmodeler.core.agent.proposals import (
    ProposalError,
    WorkspacePatchOperation,
    WorkspacePatchProposal,
    parse_proposal,
)
from planx_smartmodeler.core.agent.workspace import WorkspaceError, WorkspaceManager
from planx_smartmodeler.core.prompt_context import PromptContextLoader


_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE_RELATIVE = "tests/workspace_fixture.txt"
_FIXTURE_PATH = _PLUGIN_ROOT / _FIXTURE_RELATIVE
_FIXTURE_MARKER = "_".join(("WORKSPACE", "FIXTURE", "SENTINEL", "A"))
_FIXTURE_TEXT = f"{_FIXTURE_MARKER} = 1\n"


def patch_for(path: str, old: str, new: str) -> WorkspacePatchProposal:
    return WorkspacePatchProposal(
        context_token="opaque-token",
        workspace_id="planx_smartmodeler",
        operations=(WorkspacePatchOperation(path, old, new),),
        title="Update source",
        summary="Make one reviewed source change.",
    )


class WorkspaceManagerTests(unittest.TestCase):
    def test_read_search_preview_apply_and_undo_are_bounded(self) -> None:
        original = _FIXTURE_PATH.read_text(encoding="utf-8")
        try:
            _FIXTURE_PATH.write_text(_FIXTURE_TEXT, encoding="utf-8")
            manager = WorkspaceManager(_PLUGIN_ROOT)
            self.assertEqual(manager.read(_FIXTURE_RELATIVE)["content"], _FIXTURE_TEXT)
            self.assertEqual(
                manager.search(_FIXTURE_MARKER.casefold(), "tests")["matches"][0]["line"], 1
            )
            proposal = patch_for(_FIXTURE_RELATIVE, _FIXTURE_TEXT, "VALUE = 2\n")
            preview = manager.preview(proposal)
            self.assertEqual(preview["operation_count"], 1)
            self.assertIn(f"-{_FIXTURE_MARKER} = 1", preview["source"])
            _result, backups = manager.apply(proposal)
            self.assertEqual(_FIXTURE_PATH.read_text(encoding="utf-8"), "VALUE = 2\n")
            manager.undo(backups)
            self.assertEqual(_FIXTURE_PATH.read_text(encoding="utf-8"), _FIXTURE_TEXT)
        finally:
            _FIXTURE_PATH.write_text(original, encoding="utf-8")

    def test_stale_exact_text_and_root_escape_fail_closed(self) -> None:
        original = _FIXTURE_PATH.read_text(encoding="utf-8")
        try:
            _FIXTURE_PATH.write_text(_FIXTURE_TEXT, encoding="utf-8")
            manager = WorkspaceManager(_PLUGIN_ROOT)
            _FIXTURE_PATH.write_text(f"{_FIXTURE_MARKER} = 9\n", encoding="utf-8")
            with self.assertRaises(WorkspaceError):
                manager.apply(
                    patch_for(_FIXTURE_RELATIVE, _FIXTURE_TEXT, f"{_FIXTURE_MARKER} = 2\n")
                )
            for bad in ("../outside.py", "C:/outside.py", "/outside.py", ".git/config"):
                with self.assertRaises(WorkspaceError):
                    manager.read(bad)
        finally:
            _FIXTURE_PATH.write_text(original, encoding="utf-8")

    def test_command_runner_accepts_only_fixed_diagnostics(self) -> None:
        manager = WorkspaceManager(_PLUGIN_ROOT)
        with self.assertRaises(WorkspaceError):
            manager.command("powershell -Command Remove-Item *")
        # Pure Python has no QGIS QProcess; the production command boundary
        # fails closed instead of falling back to a shell.
        with self.assertRaises(WorkspaceError):
            manager.command("git_status")


class WorkspaceContractTests(unittest.TestCase):
    def test_parser_accepts_exact_patch_and_rejects_extra_or_traversal_keys(self) -> None:
        payload = {
            "schema_version": 1,
            "context_token": "receipt",
            "workspace_id": "planx_smartmodeler",
            "operations": [{"path": "module.py", "old_text": "a", "new_text": "b"}],
            "title": "Edit source",
            "summary": "Update one source value.",
            "warnings": [],
        }
        proposal = parse_proposal("workspace_patch", json.dumps(payload))
        self.assertEqual(proposal.operations[0].path, "module.py")
        payload["operations"][0]["path"] = "../module.py"
        with self.assertRaises(ProposalError):
            parse_proposal("workspace_patch", json.dumps(payload))
        payload["operations"][0]["path"] = "module.py"
        payload["unexpected"] = True
        with self.assertRaises(ProposalError):
            parse_proposal("workspace_patch", json.dumps(payload))
        payload.pop("unexpected")
        payload["operations"].append(
            {"path": "module.py", "old_text": "a", "new_text": "c"}
        )
        with self.assertRaises(ProposalError):
            parse_proposal("workspace_patch", json.dumps(payload))

    def test_context_receipt_changes_when_inspected_file_changes(self) -> None:
        original = _FIXTURE_PATH.read_text(encoding="utf-8")
        try:
            _FIXTURE_PATH.write_text("a", encoding="utf-8")
            manager = WorkspaceManager(_PLUGIN_ROOT)
            tokens = ContextTokenService(secret=b"x" * 32)
            state = manager.state((_FIXTURE_RELATIVE,))
            token = tokens.issue("workspace_patch", manager.workspace_id, state)
            self.assertTrue(tokens.verify(token, "workspace_patch", manager.workspace_id, state))
            _FIXTURE_PATH.write_text("b", encoding="utf-8")
            self.assertFalse(
                tokens.verify(
                    token,
                    "workspace_patch",
                    manager.workspace_id,
                    manager.state((_FIXTURE_RELATIVE,)),
                )
            )
        finally:
            _FIXTURE_PATH.write_text(original, encoding="utf-8")

    def test_workspace_scope_routes_only_workspace_tools_and_loads_contract(self) -> None:
        names = (
            "workspace.list",
            "workspace.read",
            "workspace.inspect",
            "workspace.search",
            "workspace.command",
            "layer.list",
        )
        from planx_smartmodeler.core.agent.contracts import AgentToolSpec

        schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
        specs = [
            AgentToolSpec(name, name, name, "read_only", schema, (AgentScope.WORKSPACE,))
            for name in names[:-1]
        ] + [AgentToolSpec("layer.list", "layer.list", "layer.list", "read_only", schema, (AgentScope.PROJECT,))]
        selected = select_tools_for_request(specs, AgentScope.WORKSPACE, "inspect the source")
        self.assertEqual({item.name for item in selected}, set(names[:-1]))
        context = PromptContextLoader(_PLUGIN_ROOT / "agent_context").agent_context(
            "inspect source", AgentScope.WORKSPACE
        )
        self.assertIn("workspace_patch", context)
        self.assertIn("exact old text", context)


if __name__ == "__main__":
    unittest.main()
