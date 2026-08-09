"""Headless QGIS acceptance test for the Developer Workspace apply boundary."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from qgis.core import QgsApplication


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    plugin_root = Path(__file__).resolve().parents[1]
    plugins_root = str(plugin_root.parent)
    if plugins_root not in sys.path:
        sys.path.insert(0, plugins_root)
    application = QgsApplication([], False)
    application.initQgis()
    try:
        from planx_smartmodeler.core.agent.context_tokens import ContextTokenService
        from planx_smartmodeler.core.agent.contracts import AgentMode, AgentScope
        from planx_smartmodeler.core.agent.pending_action import build_pending_action
        from planx_smartmodeler.core.agent.proposals import (
            WorkspacePatchOperation,
            WorkspacePatchProposal,
        )
        from planx_smartmodeler.core.agent.runtime_apply import RuntimeApplyCoordinator
        from planx_smartmodeler.core.agent.runtime_proposals import RuntimeProposalValidator
        from planx_smartmodeler.core.agent.workspace import WorkspaceManager

        fixture_path = plugin_root / "tests" / "workspace_fixture.txt"
        relative = "tests/workspace_fixture.txt"
        original = fixture_path.read_text(encoding="utf-8")
        old_text = "QGIS_WORKSPACE_SMOKE = 1\n"
        new_text = "QGIS_WORKSPACE_SMOKE = 2\n"
        try:
            fixture_path.write_text(old_text, encoding="utf-8")
            manager = WorkspaceManager(plugin_root)
            diagnostic = manager.command("git_status")
            if diagnostic["command"] != "git_status":
                raise RuntimeError("workspace diagnostic command did not use the allowlist")
            tokens = ContextTokenService(secret=b"qgis-workspace-smoke" * 2)
            state = manager.state((relative,))
            token = tokens.issue("workspace_patch", manager.workspace_id, state)
            proposal = WorkspacePatchProposal(
                context_token=token,
                workspace_id=manager.workspace_id,
                operations=(WorkspacePatchOperation(relative, old_text, new_text),),
                title="Workspace smoke patch",
                summary="Validate one exact source replacement.",
            )
            validator = RuntimeProposalValidator(
                lambda: None,
                tokens,
                workspace_root_provider=lambda: plugin_root,
            )
            validation = validator.validate(
                proposal.kind, proposal, AgentMode.PLAN, AgentScope.WORKSPACE
            )
            if not validation.ok or "unified diff" not in validation.preview.get("source_language", ""):
                raise RuntimeError(f"workspace validation failed: {validation.message}")
            ingredients = validator.take_last_validated()
            if not ingredients:
                raise RuntimeError("workspace validator did not retain apply ingredients")
            pending = build_pending_action(
                proposal.kind,
                proposal,
                ingredients["preview"],
                ingredients["target_identity"],
                ingredients["context_token"],
                AgentMode.ACT,
                AgentScope.WORKSPACE,
                now=0.0,
            )
            coordinator = RuntimeApplyCoordinator(
                None,
                tokens,
                workspace_root_provider=lambda: plugin_root,
            )
            applied = coordinator.apply(pending)
            if not applied.ok or applied.applied_action is None:
                raise RuntimeError(f"workspace apply failed: {applied.message}")
            if fixture_path.read_text(encoding="utf-8") != new_text:
                raise RuntimeError("workspace apply did not write the exact replacement")
            if not coordinator.can_undo(applied.applied_action):
                raise RuntimeError("workspace apply did not produce an undoable action")
            undone = coordinator.undo(applied.applied_action)
            if not undone.ok or fixture_path.read_text(encoding="utf-8") != old_text:
                raise RuntimeError("workspace undo failed")
        finally:
            fixture_path.write_text(original, encoding="utf-8")
        print("AGENT WORKSPACE SMOKE PASS: exact patch validation, reviewed apply, and guarded undo passed.")
        return 0
    finally:
        application.exitQgis()


if __name__ == "__main__":
    raise SystemExit(main())
