"""Agent Workspace dock: a model-independent QGIS panel for bounded,
read-only quick inspections and a bounded, multi-turn, provider-neutral
Agent Chat.

Quick actions and Agent Chat both execute exclusively through
``AgentController`` and the trusted read-only registry. This module owns Qt
widgets, signal wiring, and async orchestration between the pure
``AgentRunLoop`` state machine and ``AiNetworkClient``; it never parses
provider text, builds a prompt, or executes a tool call itself.
"""
from __future__ import annotations

import contextlib
import json
import time
import uuid
from typing import Any, Optional

from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDockWidget,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.agent.action_ledger import ActionLedger, ActionStatus
from ..core.agent.context_tokens import ContextTokenService
from ..core.agent.contracts import (
    AgentMode,
    AgentResultStatus,
    AgentScope,
    AgentToolCall,
    ContractError,
)
from ..core.agent.controller import AgentController
from ..core.agent.pending_action import build_pending_action, proposal_digest
from ..core.agent.proposals import ProposalReason
from ..core.agent.run_coordinator import RunCoordinator
from ..core.agent.run_loop import AgentRunLoop, RunEventKind, RunLoopError
from ..core.agent.runtime_apply import RUN_KINDS, RuntimeApplyCoordinator
from ..core.agent.runtime_proposals import RuntimeProposalValidator
from ..core.agent.runtime_tools import ModelProvider, build_default_registry
from ..core.ai_client import AiNetworkClient, StructuredResponseContract
from ..core.ai_settings import AiSettingsStore, PROVIDERS
from ..core.prompt_context import PromptContextLoader
from .theme import STUDIO_STYLE

_AGENT_CONTEXT_DIR_NAME = "agent_context"
_AGENT_TURN_SCHEMA_NAME = "agent_turn"
_AGENT_TURN_SCHEMA_DESCRIPTION = "Return the next agent_turn object."


def _load_static_instructions() -> str:
    from pathlib import Path

    context_dir = Path(__file__).resolve().parent.parent / _AGENT_CONTEXT_DIR_NAME
    return PromptContextLoader(context_dir=context_dir).static_context()


class _NullModelAdapter:
    """Fallback model adapter when no trusted apply adapter was injected.

    Reports no current model so a model-patch apply fails closed with a clear
    target-missing reason, and never installs a graph. Layer-style apply does
    not use this adapter and remains available.
    """

    def __init__(self, model_provider: ModelProvider) -> None:
        self._model_provider = model_provider

    def current_graph(self) -> Any:
        return None

    def install_graph(self, graph: Any) -> None:
        raise RuntimeError("No model-apply adapter is available in this dock.")


class AgentWorkspaceDock(QDockWidget):
    """Independent QGIS dock exposing bounded read-only inspections and
    bounded, provider-neutral, multi-turn Agent Chat over the same tools."""

    def __init__(
        self,
        iface: Any,
        model_provider: ModelProvider,
        parent: Optional[QWidget] = None,
        model_apply: Any = None,
    ) -> None:
        super().__init__("Agent Workspace", parent)
        self.setObjectName("SmartModelerAgentWorkspaceDock")
        self.iface = iface
        self._model_provider = model_provider
        # One shared per-dock context-token service issues the freshness tokens
        # for model.describe/layer.style and verifies them at the proposal
        # boundary. New chat rotates its secret, invalidating every open token.
        self.token_service = ContextTokenService()
        self.registry = build_default_registry(model_provider, self.token_service)
        self.controller = AgentController(self.registry)
        self._proposal_validator = RuntimeProposalValidator(
            model_provider,
            self.token_service,
            active_layer_provider=self._active_layer,
        )
        self.run_loop = AgentRunLoop(
            self.controller,
            _load_static_instructions(),
            proposal_validator=self._proposal_validator.validate,
        )
        # Trusted apply/undo boundary. A model_apply adapter (from the plugin,
        # wrapping the Workflow Studio window) enables model-patch apply; without
        # one, model apply fails closed while layer-style apply still works.
        adapter = model_apply if model_apply is not None else _NullModelAdapter(model_provider)
        self._apply_coordinator = RuntimeApplyCoordinator(
            adapter, self.token_service, active_layer_provider=self._active_layer
        )
        self.action_ledger = ActionLedger()
        # The trusted execution boundary. It owns the single running action;
        # nothing the provider says can start, resume, or cancel it.
        self.run_coordinator = RunCoordinator(model_provider, self)
        self.run_coordinator.run_progress.connect(self._on_run_progress)
        self.run_coordinator.run_finished.connect(self._on_run_finished)
        self.run_coordinator.run_failed.connect(self._on_run_failed)
        self.run_coordinator.run_canceled.connect(self._on_run_canceled)

        self.ai_client = AiNetworkClient(self)
        self.ai_client.succeeded.connect(self._on_provider_succeeded)
        self.ai_client.failed.connect(self._on_provider_failed)

        self._active_request_token: Optional[str] = None
        self._active_api_key = ""
        self._active_profile = None
        # At most one pending, human-approvable action and one last-applied
        # action (for a single-level, state-fingerprinted Undo).
        self._pending_action = None
        self._last_applied = None
        # The one approved action currently executing, if any.
        self._running_action = None

        self.setStyleSheet(STUDIO_STYLE)
        self._build_ui()
        self._refresh_profile()

    # -- UI construction ---------------------------------------------------

    def _build_ui(self) -> None:
        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        title = QLabel("Agent Workspace")
        title.setStyleSheet("font-weight: 600; font-size: 12pt;")
        layout.addWidget(title)
        subtitle = QLabel(
            "Inspections are read-only. A model, style, or run proposal takes "
            "effect only after you explicitly click Apply or Run on its approval "
            "card, and the last such action can be undone. A run is limited to a "
            "reviewed list of safe algorithms or your current workflow, always "
            "writes to temporary layers, and never invokes a plugin or writes a "
            "file."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: #9AAAC2;")
        layout.addWidget(subtitle)

        profile_row = QHBoxLayout()
        self.profile_label = QLabel("Profile: -")
        profile_row.addWidget(self.profile_label, 1)
        self.ai_settings_button = QPushButton("AI connections...")
        self.ai_settings_button.clicked.connect(self._open_ai_settings)
        profile_row.addWidget(self.ai_settings_button)
        layout.addLayout(profile_row)

        selectors = QHBoxLayout()
        selectors.addWidget(QLabel("Scope:"))
        self.scope_combo = QComboBox()
        self.scope_combo.addItem("Project", AgentScope.PROJECT)
        self.scope_combo.addItem("Active layer", AgentScope.ACTIVE_LAYER)
        self.scope_combo.addItem("Current model", AgentScope.CURRENT_MODEL)
        self.scope_combo.addItem("Plugins", AgentScope.PLUGINS)
        selectors.addWidget(self.scope_combo, 1)
        selectors.addWidget(QLabel("Mode:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Ask", AgentMode.ASK)
        self.mode_combo.addItem("Plan", AgentMode.PLAN)
        # Act reaches a pending action that still needs a separate, explicit
        # human Apply click; the internal enum value stays AgentMode.ACT.
        self.mode_combo.addItem("Act (approve to apply)", AgentMode.ACT)
        selectors.addWidget(self.mode_combo, 1)
        layout.addLayout(selectors)

        quick_actions = QHBoxLayout()
        self.project_summary_button = QPushButton("Project summary")
        self.project_summary_button.clicked.connect(
            lambda: self._run_quick_tool("project.summary", AgentScope.PROJECT)
        )
        quick_actions.addWidget(self.project_summary_button)
        self.layers_button = QPushButton("Layers")
        self.layers_button.clicked.connect(
            lambda: self._run_quick_tool("layer.list", AgentScope.PROJECT)
        )
        quick_actions.addWidget(self.layers_button)
        self.model_button = QPushButton("Current model")
        self.model_button.clicked.connect(
            lambda: self._run_quick_tool("model.summary", AgentScope.CURRENT_MODEL)
        )
        quick_actions.addWidget(self.model_button)
        self.plugins_button = QPushButton("Plugins")
        self.plugins_button.clicked.connect(
            lambda: self._run_quick_tool("plugin.list", AgentScope.PLUGINS)
        )
        quick_actions.addWidget(self.plugins_button)
        layout.addLayout(quick_actions)

        self.transcript = QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setPlaceholderText(
            "Quick inspection results and Agent Chat conversation appear here."
        )
        layout.addWidget(self.transcript, 1)

        self.proposal_group = QGroupBox("Proposal preview - review only")
        proposal_layout = QVBoxLayout(self.proposal_group)
        proposal_layout.setContentsMargins(8, 8, 8, 8)
        proposal_layout.setSpacing(4)
        self.proposal_status_label = QLabel("No proposal yet.")
        self.proposal_status_label.setWordWrap(True)
        self.proposal_status_label.setStyleSheet("color: #9AAAC2;")
        proposal_layout.addWidget(self.proposal_status_label)
        self.proposal_view = QPlainTextEdit()
        self.proposal_view.setReadOnly(True)
        self.proposal_view.setFixedHeight(150)
        self.proposal_view.setPlaceholderText(
            "A validated model or style proposal appears here. It is never "
            "applied - there is no Apply, Run, or Accept action."
        )
        proposal_layout.addWidget(self.proposal_view)
        layout.addWidget(self.proposal_group)

        # Approval card: shown only for an Act-mode pending action. Apply is
        # never the default/focused button; nothing mutates until a real click.
        self.approval_group = QGroupBox("Approve action - explicit apply required")
        approval_layout = QVBoxLayout(self.approval_group)
        approval_layout.setContentsMargins(8, 8, 8, 8)
        approval_layout.setSpacing(4)
        self.approval_status_label = QLabel("No action awaiting approval.")
        self.approval_status_label.setWordWrap(True)
        self.approval_status_label.setStyleSheet("color: #E0B341;")
        approval_layout.addWidget(self.approval_status_label)
        self.approval_view = QPlainTextEdit()
        self.approval_view.setReadOnly(True)
        self.approval_view.setFixedHeight(120)
        approval_layout.addWidget(self.approval_view)
        approval_buttons = QHBoxLayout()
        # One primary action button for the one pending action. For a run
        # proposal it is relabelled "Run"; there is deliberately no second
        # accept/run widget, so the single one-shot approval nonce keeps
        # guarding every kind of action through exactly one code path.
        self.apply_button = QPushButton("Apply")
        self.apply_button.setAutoDefault(False)
        self.apply_button.setDefault(False)
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self._on_apply_clicked)
        approval_buttons.addWidget(self.apply_button)
        self.reject_button = QPushButton("Reject")
        self.reject_button.setAutoDefault(False)
        self.reject_button.setDefault(False)
        self.reject_button.setEnabled(False)
        self.reject_button.clicked.connect(self._on_reject_clicked)
        approval_buttons.addWidget(self.reject_button)
        approval_layout.addLayout(approval_buttons)

        # Live run state: a bounded progress line and the only control that
        # stays active while an approved run is executing.
        self.run_progress_label = QLabel("")
        self.run_progress_label.setWordWrap(True)
        self.run_progress_label.setStyleSheet("color: #7FB3E8;")
        self.run_progress_label.setVisible(False)
        approval_layout.addWidget(self.run_progress_label)
        self.cancel_run_button = QPushButton("Cancel run")
        self.cancel_run_button.setAutoDefault(False)
        self.cancel_run_button.setDefault(False)
        self.cancel_run_button.setVisible(False)
        self.cancel_run_button.clicked.connect(self._on_cancel_run_clicked)
        approval_layout.addWidget(self.cancel_run_button)
        self.approval_group.setVisible(False)
        layout.addWidget(self.approval_group)

        # Bounded, read-only action ledger plus a single-level Undo control.
        self.ledger_group = QGroupBox("Action ledger")
        ledger_layout = QVBoxLayout(self.ledger_group)
        ledger_layout.setContentsMargins(8, 8, 8, 8)
        ledger_layout.setSpacing(4)
        self.ledger_view = QPlainTextEdit()
        self.ledger_view.setReadOnly(True)
        self.ledger_view.setFixedHeight(90)
        self.ledger_view.setPlaceholderText(
            "Proposed, approved, rejected, applied, failed and undone actions "
            "appear here. No raw values are recorded."
        )
        ledger_layout.addWidget(self.ledger_view)
        self.undo_button = QPushButton("Undo last agent action")
        self.undo_button.setAutoDefault(False)
        self.undo_button.setEnabled(False)
        self.undo_button.clicked.connect(self._on_undo_clicked)
        ledger_layout.addWidget(self.undo_button)
        layout.addWidget(self.ledger_group)

        # A mode/scope change invalidates any pending action (fail closed).
        self.mode_combo.currentIndexChanged.connect(self._on_mode_or_scope_changed)
        self.scope_combo.currentIndexChanged.connect(self._on_mode_or_scope_changed)

        self.status_label = QLabel("Ready.")
        self.status_label.setStyleSheet("color: #9AAAC2;")
        layout.addWidget(self.status_label)

        self.prompt_input = QPlainTextEdit()
        self.prompt_input.setPlaceholderText(
            "Ask a question about your project, layers, Processing, the "
            "current model, or installed plugins."
        )
        self.prompt_input.setFixedHeight(70)
        layout.addWidget(self.prompt_input)

        button_row = QHBoxLayout()
        self.send_button = QPushButton("Send")
        self.send_button.clicked.connect(self._on_send_clicked)
        button_row.addWidget(self.send_button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._on_stop_clicked)
        button_row.addWidget(self.stop_button)
        self.new_chat_button = QPushButton("New chat")
        self.new_chat_button.clicked.connect(self._on_new_chat_clicked)
        button_row.addWidget(self.new_chat_button)
        layout.addLayout(button_row)

        self.setWidget(container)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override signature
        super().showEvent(event)
        self._refresh_profile()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override signature
        # Closing the panel cancels an approved run as well as a chat turn, so
        # no execution continues against a panel the human has dismissed.
        self.run_coordinator.cancel()
        self._cancel_active_run()
        super().closeEvent(event)

    def shutdown(self) -> None:
        """Abort any outstanding request and clear all transient secrets now.

        Called by the plugin's ``unload()`` so no dock/client callback can
        outlive the plugin -- this happens synchronously and does not depend
        on Qt's asynchronous ``deleteLater()`` running an event loop, so a
        late provider callback cannot revive a run after unload. Unlike
        :meth:`_cancel_active_run`, it aborts a still-busy client even when the
        run-loop state has already become terminal.
        """
        if self.ai_client.is_busy():
            self.ai_client.cancel()
        self._active_request_token = None
        self._active_api_key = ""
        self._active_profile = None
        # Cancel and tear down any running action first, so a Processing result
        # that returns after unload adds no layer and revives nothing.
        self.run_coordinator.shutdown()
        if self.run_loop.is_active():
            self.run_loop.cancel()
        # Clear all transient proposal/token/action state so nothing survives
        # unload: no pending action, no undo target, no ledger, no card.
        self._clear_proposal_preview()
        self._clear_all_action_state()
        self.token_service.rotate()
        self._set_controls_active(False)

    # -- profile -------------------------------------------------------

    def _refresh_profile(self) -> None:
        store = AiSettingsStore()
        profile = store.active_profile()
        provider = PROVIDERS[profile.provider_id]
        label = profile.name if profile.name else provider.name
        self.profile_label.setText(f"Profile: {label} ({provider.name})")

    def _open_ai_settings(self) -> None:
        from .ai_settings_dialog import AiSettingsDialog

        dialog = AiSettingsDialog(self)
        dialog.setStyleSheet(STUDIO_STYLE)
        dialog.exec()
        self._refresh_profile()

    # -- quick actions ---------------------------------------------------

    def _run_quick_tool(self, tool_name: str, scope_hint: str) -> None:
        mode = self.mode_combo.currentData() or AgentMode.ASK
        call_id = uuid.uuid4().hex[:32]
        try:
            call = AgentToolCall(call_id=call_id, tool_name=tool_name, arguments={})
        except ContractError as error:
            self._append_line(f"[{tool_name}] rejected: {error}")
            return
        result = self.controller.execute(call, mode, scope_hint)
        self._append_tool_result(tool_name, result)

    def _append_tool_result(self, tool_name: str, result) -> None:
        if result.status == AgentResultStatus.SUCCESS:
            body = json.dumps(result.data, ensure_ascii=False, indent=2)
        else:
            body = result.message or result.status
        self._append_line(f"[{tool_name}] {result.status}\n{body}")

    # -- Agent Chat: sending -------------------------------------------

    def _on_send_clicked(self) -> None:
        if self.run_loop.is_active():
            return
        if self.run_coordinator.is_running():
            self._append_line("A run is in progress. Wait for it to finish or cancel it.")
            return
        text = self.prompt_input.toPlainText().strip()
        if not text:
            return

        store = AiSettingsStore()
        profile = store.active_profile()
        if profile.provider_id == "offline":
            self._append_line(
                "Agent Chat needs a configured AI connection (not Offline). "
                "Open AI connections to set one up. Quick actions above "
                "still work without a provider."
            )
            return
        api_key = store.secret(profile.profile_id)
        errors = profile.validate(api_key)
        if errors:
            self._append_line(
                "AI connection is not ready:\n" + "\n".join(errors)
                + "\n\nOpen AI connections to fix this profile."
            )
            return

        mode = self.mode_combo.currentData() or AgentMode.ASK
        scope = self.scope_combo.currentData() or AgentScope.PROJECT
        bound = self.run_loop.prompt_budget.max_user_message_chars
        if len(text) > bound:
            self._append_line(
                f"Your message exceeds the {bound}-character limit; shorten it and try again."
            )
            return

        self.prompt_input.clear()
        self._append_line(f"> {text}")
        self._active_profile = profile
        self._active_api_key = api_key
        # Starting a new run supersedes any un-applied pending action.
        self._invalidate_pending_action()

        try:
            event = self.run_loop.start(text, mode, scope)
        except RunLoopError as error:
            self._append_line(f"[error] {error}")
            return
        self._set_controls_active(True)
        self._handle_run_event(event)

    def _on_stop_clicked(self) -> None:
        self._cancel_active_run()

    def _cancel_active_run(self) -> None:
        if not self.run_loop.is_active():
            return
        if self.ai_client.is_busy():
            self.ai_client.cancel()
        # Clear the temporary key/profile copy synchronously on cancellation,
        # not only in _finish_run(), so Stop/close never leaves a secret in
        # the live dock object until a later terminal signal.
        self._active_request_token = None
        self._active_api_key = ""
        self._active_profile = None
        self.run_loop.cancel()
        self._append_line("Run cancelled.")
        self._set_controls_active(False)
        self.status_label.setText("Ready.")

    def _on_new_chat_clicked(self) -> None:
        if self.run_loop.is_active():
            self._append_line("Stop the active run before starting a new chat.")
            return
        if self.run_loop.session_memory.is_empty() and not self.transcript.toPlainText().strip():
            self.run_loop.new_chat()
            self.token_service.rotate()
            self._clear_proposal_preview()
            self._clear_all_action_state()
            return
        confirm = QMessageBox.question(
            self,
            "Start a new chat?",
            "This clears the current Agent Chat conversation memory. Continue?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.run_loop.new_chat()
        # Rotate the context-token secret so any proposal prepared against the
        # old conversation can no longer validate, and clear the preview and all
        # pending/applied/ledger action state.
        self.token_service.rotate()
        self.transcript.clear()
        self._clear_proposal_preview()
        self._clear_all_action_state()
        self.status_label.setText("Ready.")

    # -- Agent Chat: event handling --------------------------------------

    def _handle_run_event(self, event) -> None:
        for tool_event in event.tool_events:
            if tool_event.get("kind") == "assistant_note" and tool_event.get("text"):
                self._append_line(f"[assistant] {tool_event['text']}")
            elif tool_event.get("kind") == "tool_result":
                result = tool_event.get("result", {})
                self._append_line(
                    f"[tool: {tool_event.get('tool_name', '')}] {result.get('status', '')}"
                )

        if event.kind == RunEventKind.REQUEST_PROVIDER:
            self._active_request_token = event.request.request_token
            contract = StructuredResponseContract(
                schema=event.request.response_schema,
                name=_AGENT_TURN_SCHEMA_NAME,
                description=_AGENT_TURN_SCHEMA_DESCRIPTION,
            )
            self.status_label.setText(
                f"Turn {self.run_loop.turns_used} - "
                f"{self.run_loop.tool_calls_used} tool call(s) used - waiting for the AI..."
            )
            self.ai_client.generate_structured(
                self._active_profile,
                self._active_api_key,
                event.request.system_prompt,
                event.request.user_prompt,
                contract,
            )
        elif event.kind == RunEventKind.FINAL:
            self._append_line(f"[assistant] {event.text}")
            self._finish_run(
                f"Done - {self.run_loop.turns_used} turn(s), "
                f"{self.run_loop.tool_calls_used} tool call(s) used."
            )
        elif event.kind == RunEventKind.PROPOSAL:
            if event.text:
                self._append_line(f"[assistant] {event.text}")
            self._present_proposal(event.proposal or {})
            self._finish_run(
                f"Proposal ready (Not applied) - {self.run_loop.turns_used} turn(s), "
                f"{self.run_loop.tool_calls_used} tool call(s) used."
            )
        elif event.kind == RunEventKind.FAILED:
            self._append_line(f"[error] {event.text}")
            self._finish_run(
                f"Stopped - {self.run_loop.turns_used} turn(s), "
                f"{self.run_loop.tool_calls_used} tool call(s) used."
            )
        elif event.kind == RunEventKind.CANCELLED:
            self._finish_run("Ready.")

    def _on_provider_succeeded(self, raw_text: str) -> None:
        token = self._active_request_token
        if token is None:
            return
        self._active_request_token = None
        event = self.run_loop.submit_provider_response(token, raw_text)
        if event is None:
            return
        self._handle_run_event(event)

    def _on_provider_failed(self, message: str) -> None:
        token = self._active_request_token
        if token is None:
            return
        self._active_request_token = None
        event = self.run_loop.submit_provider_failure(token, message)
        if event is None:
            return
        self._handle_run_event(event)

    def _finish_run(self, status_text: str) -> None:
        self._active_request_token = None
        self._active_api_key = ""
        self._active_profile = None
        self._set_controls_active(False)
        self.status_label.setText(status_text)

    def _set_controls_active(self, active: bool) -> None:
        self.send_button.setEnabled(not active)
        self.prompt_input.setEnabled(not active)
        self.scope_combo.setEnabled(not active)
        self.mode_combo.setEnabled(not active)
        self.ai_settings_button.setEnabled(not active)
        self.new_chat_button.setEnabled(not active)
        self.stop_button.setEnabled(active)

    def _append_line(self, text: str) -> None:
        self.transcript.appendPlainText(text + "\n")

    # -- proposal preview ------------------------------------------------

    def _active_layer(self) -> Any:
        """Return the QGIS active layer defensively, or ``None``."""
        getter = getattr(self.iface, "activeLayer", None)
        if getter is None:
            return None
        with contextlib.suppress(Exception):
            return getter()
        return None

    def _clear_proposal_preview(self) -> None:
        self.proposal_view.clear()
        self.proposal_status_label.setText("No proposal yet.")

    def _render_proposal_preview(self, preview: dict) -> None:
        """Render a fully validated proposal. Only a valid PROPOSAL event
        reaches this method, so an invalid/stale proposal (handled as a FAILED
        event) never overwrites the last valid preview shown here."""
        kind = str(preview.get("kind", ""))
        title = str(preview.get("title", ""))[:160]
        target = str(preview.get("target", ""))[:200]
        summary = str(preview.get("summary", ""))[:2000]
        lines = [
            f"Kind: {kind}",
            f"Title: {title}",
            f"Target: {target}",
            "Status: Not applied (review only)",
            "",
            f"Summary: {summary}",
        ]
        if kind == "model_patch":
            lines.append("")
            lines.append("Operations:")
            for item in preview.get("operations", [])[:40]:
                flag = " [destructive if applied]" if item.get("destructive") else ""
                lines.append(f"  - {str(item.get('summary', ''))[:300]}{flag}")
            if preview.get("incomplete"):
                lines.append("")
                lines.append(
                    "Candidate is structurally valid but has open validation issues:"
                )
            issues = preview.get("validation_issues", [])
            for issue in issues[:40]:
                lines.append(f"  ! {str(issue)[:300]}")
            if not issues:
                lines.append("")
                lines.append("Candidate validation issues: none")
        elif kind in ("layer_style", "processing_run", "model_run"):
            lines.append("")
            heading = {
                "layer_style": "Intended style (not applied):",
                "processing_run": "Reviewed run inputs (nothing has run):",
                "model_run": "Current workflow to run (nothing has run):",
            }[kind]
            lines.append(heading)
            for change in preview.get("changes", [])[:40]:
                lines.append(f"  - {str(change)[:200]}")
            outputs = preview.get("outputs", [])
            if outputs:
                lines.append("")
                lines.append("Expected results:")
                for output in outputs[:20]:
                    lines.append(f"  - {str(output)[:200]}")
        for warning in preview.get("warnings", [])[:20]:
            lines.append(f"  * warning: {str(warning)[:500]}")
        self.proposal_view.setPlainText("\n".join(lines))
        self.proposal_status_label.setText(
            "Validated proposal below. Not applied - review only in Plan; an Act "
            "proposal adds a separate Apply step below."
        )

    # -- pending action, approval and undo (Phase 04) --------------------

    def _present_proposal(self, preview: dict) -> None:
        """Render the validated proposal and, only for an Act-mode run, build the
        single pending action and show the explicit approval card. Plan-mode
        proposals stay review-only with no Apply control."""
        self._render_proposal_preview(preview)
        # Always consume the trusted boundary's retained ingredients so a Plan
        # proposal can never leave appliable state behind for a later Act run.
        ingredients = self._proposal_validator.take_last_validated()
        if self.run_coordinator.is_running():
            # One running action maximum: a proposal that arrives mid-run stays
            # review-only and creates no pending action.
            self._append_line(
                "[proposal] A run is in progress; this proposal is shown for review only."
            )
            return
        if self.run_loop.mode == AgentMode.ACT and isinstance(ingredients, dict):
            self._create_pending_action(ingredients, preview)
            self._append_line(
                "[proposal] Validated. Review the approval card and click Apply to apply it."
            )
        else:
            self._clear_approval_card()
            self._append_line(
                "[proposal] A validated proposal is shown below (Not applied; review only)."
            )

    def _create_pending_action(self, ingredients: dict, preview: dict) -> None:
        # A new validated Act proposal supersedes any previous pending action.
        if self._pending_action is not None:
            self._record_ledger(self._pending_action, ActionStatus.SUPERSEDED)
            self._pending_action = None
        pending = build_pending_action(
            ingredients["kind"],
            ingredients["proposal"],
            preview,
            ingredients["target_identity"],
            ingredients["context_token"],
            AgentMode.ACT,
            self.run_loop.scope,
            now=time.monotonic(),
        )
        self._pending_action = pending
        self._record_ledger(pending, ActionStatus.PROPOSED)
        self._show_approval_card(pending)

    def _show_approval_card(self, pending) -> None:
        card = pending.to_public_card()
        is_run = pending.kind in RUN_KINDS
        verb = "Run" if is_run else "Apply"
        self.apply_button.setText(verb)
        self.approval_view.setPlainText(self._format_card(card))
        note = "destructive if applied" if card["destructive"] else "reversible via Undo"
        if is_run:
            note = "results go to temporary layers you can remove with Undo"
        self.approval_status_label.setText(
            f"Explicit approval required for this {card['kind']} action ({note}). "
            f"Nothing happens until you click {verb}."
        )
        self.apply_button.setEnabled(True)
        self.reject_button.setEnabled(True)
        # Apply must never be the default/auto-focused button.
        self.apply_button.setDefault(False)
        self.apply_button.setAutoDefault(False)
        self.reject_button.setFocus()
        self.approval_group.setVisible(True)

    @staticmethod
    def _format_card(card: dict) -> str:
        lines = [
            f"Kind: {card.get('kind', '')}",
            f"Title: {card.get('title', '')}",
            f"Target: {card.get('target', '')}",
            "Status: Not applied",
            "",
            f"Summary: {card.get('summary', '')}",
        ]
        for item in card.get("operations", [])[:40]:
            flag = " [destructive if applied]" if item.get("destructive") else ""
            lines.append(f"  - {str(item.get('summary', ''))[:300]}{flag}")
        for change in card.get("changes", [])[:40]:
            lines.append(f"  - {str(change)[:200]}")
        for issue in card.get("validation_issues", [])[:40]:
            lines.append(f"  ! {str(issue)[:300]}")
        for warning in card.get("warnings", [])[:20]:
            lines.append(f"  * warning: {str(warning)[:500]}")
        return "\n".join(lines)

    def _on_apply_clicked(self) -> None:
        pending = self._pending_action
        if pending is None:
            return
        if self.run_coordinator.is_running():
            # One running action maximum; the pending one stays for later.
            self._append_line("[action] A run is already in progress.")
            return
        # One-shot: consume the nonce; a double-click/late signal finds it used.
        if not pending.approval.consume(pending.action_id, pending.approval.nonce):
            return
        self._pending_action = None
        self.apply_button.setEnabled(False)
        self.reject_button.setEnabled(False)
        if pending.is_expired(time.monotonic()):
            self._record_ledger(pending, ActionStatus.EXPIRED, "expired")
            self._append_line("[action] The pending action expired before apply; nothing changed.")
            self._clear_approval_card()
            return
        if pending.kind in RUN_KINDS:
            self._start_run(pending)
            return
        result = self._apply_coordinator.apply(pending)
        if result.ok:
            self._last_applied = result.applied_action
            self._record_ledger(pending, ActionStatus.APPLIED)
            self._append_line(f"[action] Applied the {pending.kind} action.")
            self.status_label.setText("Action applied.")
        else:
            self._record_ledger(pending, ActionStatus.FAILED, result.reason_code)
            self._append_line(f"[action] Not applied: {result.message}")
        self._clear_approval_card()
        self._refresh_undo_button()

    # -- approved run execution (Phase 05) -------------------------------

    def _start_run(self, pending) -> None:
        """Revalidate the approved run against live state, then execute it.

        The nonce has already been consumed by the click, so this path can run
        at most once per pending action. Live state is checked again *here*, not
        trusted from the moment the card was drawn: a stale proposal is rejected,
        never repaired.
        """
        self._running_action = None
        if proposal_digest(pending.proposal) != pending.digest:
            self._fail_run(pending, ProposalReason.VALIDATION_FAILED,
                           "The approved run failed its integrity check; nothing ran.")
            return
        validation = self._proposal_validator.validate(
            pending.kind, pending.proposal, AgentMode.ACT, pending.scope
        )
        ingredients = self._proposal_validator.take_last_validated()
        if not validation.ok or not isinstance(ingredients, dict):
            self._fail_run(pending, validation.reason_code or ProposalReason.VALIDATION_FAILED,
                           validation.message or "The run is no longer valid; nothing ran.")
            return
        self._record_ledger(pending, ActionStatus.APPROVED)
        self._running_action = pending
        self._set_running_ui(True, ingredients.get("display_name", ""))
        self._record_ledger(pending, ActionStatus.RUNNING)
        if pending.kind == "processing_run":
            refused = self.run_coordinator.start_processing_run(
                pending.action_id,
                pending.preview.get("title", ""),
                ingredients.get("display_name", ""),
                ingredients.get("algorithm_id", ""),
                ingredients.get("run_parameters", {}),
                ingredients.get("destinations", ()),
            )
        else:
            refused = self.run_coordinator.start_model_run(
                pending.action_id,
                pending.preview.get("title", ""),
                ingredients.get("display_name", ""),
            )
        if refused:
            self._fail_run(pending, refused, "A run is already in progress; nothing started.")

    def _fail_run(self, pending, reason_code: str, message: str) -> None:
        self._running_action = None
        self._set_running_ui(False, "")
        self._record_ledger(pending, ActionStatus.FAILED, reason_code)
        self._append_line(f"[run] {message}")
        self._clear_approval_card()
        self._refresh_undo_button()

    def _set_running_ui(self, running: bool, target: str) -> None:
        self.apply_button.setEnabled(False)
        self.reject_button.setEnabled(False)
        self.cancel_run_button.setVisible(running)
        self.cancel_run_button.setEnabled(running)
        self.run_progress_label.setVisible(running)
        if running:
            self.approval_group.setVisible(True)
            self.approval_status_label.setText(
                f"Running {target}. Nothing else can be proposed or approved until it "
                "finishes; Cancel stops it and leaves the project unchanged."
            )
            self.run_progress_label.setText("Starting...")
        else:
            self.run_progress_label.setText("")
        self.send_button.setEnabled(not running and not self.run_loop.is_active())
        self.prompt_input.setEnabled(not running and not self.run_loop.is_active())
        self.new_chat_button.setEnabled(not running)
        self.scope_combo.setEnabled(not running)
        self.mode_combo.setEnabled(not running)
        # Progress callbacks pump the event loop so Cancel stays clickable, so
        # every other control that could re-enter this dock -- a modal settings
        # dialog, a quick inspection, Undo -- is disabled for the duration.
        self.ai_settings_button.setEnabled(not running)
        for button in (
            self.project_summary_button,
            self.layers_button,
            self.model_button,
            self.plugins_button,
        ):
            button.setEnabled(not running)
        if running:
            self.undo_button.setEnabled(False)

    def _on_cancel_run_clicked(self) -> None:
        self.run_coordinator.cancel()

    def _on_run_progress(self, percent: int, text: str) -> None:
        self.run_progress_label.setText(f"{int(percent)}% - {str(text)[:120]}")

    def _on_run_finished(self, summary: dict) -> None:
        pending = self._running_action
        self._running_action = None
        self._set_running_ui(False, "")
        names = [str(name)[:120] for name in (summary.get("layer_names") or [])][:20]
        self._last_applied = self._apply_coordinator.record_run_result(
            pending.action_id if pending is not None else "",
            str(summary.get("kind", "")),
            str(summary.get("target", "")),
            str(summary.get("title", "")),
            [str(layer_id) for layer_id in (summary.get("layer_ids") or [])],
        )
        if pending is not None:
            self._record_ledger(pending, ActionStatus.COMPLETED)
        added = ", ".join(names) if names else "no layer"
        self._append_line(f"[run] Finished. Added as temporary layer(s): {added}.")
        self.status_label.setText("Run complete.")
        self._clear_approval_card()
        self._refresh_undo_button()

    def _on_run_failed(self, reason_code: str, message: str) -> None:
        pending = self._running_action
        self._running_action = None
        self._set_running_ui(False, "")
        if pending is not None:
            self._record_ledger(pending, ActionStatus.FAILED, reason_code)
        self._append_line(f"[run] Not completed: {message} The project is unchanged.")
        self.status_label.setText("Run failed.")
        self._clear_approval_card()
        self._refresh_undo_button()

    def _on_run_canceled(self) -> None:
        pending = self._running_action
        self._running_action = None
        self._set_running_ui(False, "")
        if pending is not None:
            self._record_ledger(pending, ActionStatus.CANCELED, ProposalReason.EXECUTION_CANCELED)
        self._append_line("[run] Cancelled. No layer was added and the project is unchanged.")
        self.status_label.setText("Run cancelled.")
        self._clear_approval_card()
        self._refresh_undo_button()

    def _on_reject_clicked(self) -> None:
        pending = self._pending_action
        if pending is None:
            return
        self._pending_action = None
        self._record_ledger(pending, ActionStatus.REJECTED)
        self._append_line("[action] Proposal rejected; nothing was applied.")
        self._clear_approval_card()

    def _on_undo_clicked(self) -> None:
        applied = self._last_applied
        if applied is None:
            return
        if not self._apply_coordinator.can_undo(applied):
            self._append_line("[undo] The target changed; Undo is no longer available.")
            self._last_applied = None
            self._refresh_undo_button()
            return
        result = self._apply_coordinator.undo(applied)
        if result.ok:
            self.action_ledger.record(
                applied.action_id, applied.kind, "", applied.title, ActionStatus.UNDONE,
            )
            self._append_line(f"[undo] Reverted the last {applied.kind} action.")
        else:
            self._append_line(f"[undo] Could not undo: {result.message}")
        self._last_applied = None
        self._refresh_ledger_view()
        self._refresh_undo_button()

    def _on_mode_or_scope_changed(self, *args) -> None:
        # A mode/scope change fails any pending action closed and cancels a
        # running one. (The selectors are disabled while a run executes, so the
        # cancellation here is defense in depth against a programmatic change.)
        if self.run_coordinator.is_running():
            self.run_coordinator.cancel()
        if self._pending_action is not None:
            self._invalidate_pending_action()

    def _invalidate_pending_action(self) -> None:
        if self._pending_action is not None:
            self._record_ledger(self._pending_action, ActionStatus.SUPERSEDED)
            self._pending_action = None
        self._clear_approval_card()

    def _clear_approval_card(self) -> None:
        self.approval_view.clear()
        self.approval_status_label.setText("No action awaiting approval.")
        self.apply_button.setText("Apply")
        self.apply_button.setEnabled(False)
        self.reject_button.setEnabled(False)
        self.cancel_run_button.setVisible(False)
        self.run_progress_label.setVisible(False)
        self.run_progress_label.setText("")
        self.approval_group.setVisible(False)

    def _record_ledger(self, pending, status: str, reason_code: str = "") -> None:
        card = pending.to_public_card()
        self.action_ledger.record(
            pending.action_id,
            pending.kind,
            card.get("target", ""),
            card.get("title", ""),
            status,
            is_destructive=pending.is_destructive,
            reason_code=reason_code,
        )
        self._refresh_ledger_view()

    def _refresh_ledger_view(self) -> None:
        lines = []
        for entry in self.action_ledger.entries()[-12:]:
            data = entry.to_dict()
            flag = " [destructive]" if data["destructive"] else ""
            reason = f" ({data['reason_code']})" if data["reason_code"] else ""
            lines.append(
                f"#{data['seq']} {data['kind']}: {data['status']}{flag} "
                f"- {str(data['title'])[:60]}{reason}"
            )
        self.ledger_view.setPlainText("\n".join(lines))

    def _refresh_undo_button(self) -> None:
        with contextlib.suppress(Exception):
            self.undo_button.setEnabled(self._apply_coordinator.can_undo(self._last_applied))

    def _clear_all_action_state(self) -> None:
        """Clear pending action, running action, last-applied, card and ledger."""
        self.run_coordinator.shutdown()
        self._pending_action = None
        self._running_action = None
        self._last_applied = None
        self.action_ledger.clear()
        self._clear_approval_card()
        self._refresh_ledger_view()
        self._refresh_undo_button()
