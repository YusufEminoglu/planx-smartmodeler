# Changelog

All notable changes to SmartModeler GIS are documented here. The project follows Keep a Changelog and Semantic Versioning.

## [0.4.0] - 2026-07-23

- Added Phase 05 **approved safe Processing and current-model execution** to the
  Agent Workspace. Two new proposal kinds, `processing_run` and `model_run`,
  bring the V1 set to four. A `processing_run` may name only an algorithm on a
  shipped, hardcoded **reviewed allowlist of twelve native algorithms**, and only
  while its live signature still matches the reviewed one; there is no generic
  "run any algorithm" path, and provider output or user text can never extend the
  allowlist. A `model_run` names no algorithm and no parameters at all: it runs
  the *current* workflow, whose every Processing node must independently pass the
  same deny-by-default policy.
- Execution still requires a **separate, explicit human click** on the approval
  card, distinct from the click that created it, guarded by the same one-shot
  approval nonce as Phase 04. Ask stays read-only, Plan stays preview-only, and
  there is at most **one pending action and one running action** at a time. Live
  state -- freshness receipt, algorithm signature, and layer/field identity -- is
  revalidated at the click; a stale proposal is rejected, never repaired.
- Every run writes to **temporary layers only**. Destinations are forced by the
  application to a temporary output and cannot be expressed by a proposal at all,
  so no file, folder, database, or network output is reachable. A failed or
  cancelled run adds no layer and leaves the project unchanged, a late result
  arriving after cancel or shutdown adds nothing, and every Processing failure
  message is replaced with a bounded, path-free, credential-free sentence.
- Runs report live progress and can be **cancelled**, and the result layers of
  the last run can be removed with **Undo last agent action** -- but only while
  each result still matches the identity fingerprint recorded when it was added,
  so a result the human renamed or edited is never removed. The action ledger
  gained `running`, `completed` and `canceled` outcomes. The read-only
  `processing.describe` inspection now also issues the run freshness receipt.
  Still **no** plugin invocation, no persistence across restarts, no MCP or
  subprocess, no second network stack, and no new dependency.
- Added Phase 04 **explicit human approval, atomic apply and safe Undo** to the
  Agent Workspace. A validated `model_patch` or `layer_style` proposal in **Act**
  mode now produces a single pending action shown on a read-only approval card;
  nothing changes until the human **explicitly clicks Apply**. Provider output
  can never approve, apply, or undo, and there is no Approve-all, remembered, or
  background approval. Plan stays preview-only and Ask stays read-only.
- Apply is **atomic and stale-safe**. At the click boundary the live context
  token and the reviewed proposal digest are re-verified; a stale or changed
  target is rejected, not repaired. A model patch is rebuilt and validated on a
  detached clone and installed through one trusted model-window seam, rolling
  back to the exact prior graph on any failure. A style/labeling change captures
  the renderer, labeling, opacity and project-dirty state first and rolls every
  component back on failure. Applable style families are `keep`, `single_symbol`,
  `categorized`, `graduated` and `raster_gray`; category/class values stay local
  and never reach the provider or the ledger.
- Added a single-level **Undo last agent action** that reverts the most recent
  applied model or style change only while the live target still matches the
  action's post-state fingerprint, so it can never overwrite a later user edit.
  A bounded in-session **action ledger** records what was proposed, approved,
  rejected, applied, failed, superseded or undone, with no raw parameters,
  feature/category values, paths, tokens, digests or secrets, and is cleared on
  **New chat** and shutdown. Still **no** Processing/plugin execution, no
  persistence, no MCP/subprocess/second network stack, and no new dependency.
- Added Phase 03 **rich read-only understanding and inert proposals** to the
  Agent Workspace. The read-only registry grows from eight to **eleven** tools
  with `layer.style` (bounded renderer/labeling summary), `model.describe`
  (safe graph topology), and `plugin.describe` (bounded installed plugin
  metadata). None of them expose a source, feature value, category/rule value,
  style/label expression, baseline model parameter value, or credential.
- The Agent Workspace can now show two kinds of **validated, review-only
  proposals** in Plan or Act mode: a `model_patch` (suggested SmartModeler
  graph edits) and a `layer_style` (suggested symbology/labeling intent).
  Proposals are **never applied**: a model patch is validated only on a
  detached graph clone (the live graph is left byte-for-byte unchanged) and a
  style proposal is checked against the live layer's fields without ever
  touching its renderer, labels, opacity, or the project's dirty state. There
  is no Apply, Accept, Approve, Run, Execute, Export, or Save action. **Act is
  proposal-only in this phase.** No Processing execution, plugin invocation, or
  project/layer/model mutation was added.
- Each proposal is bound to an opaque, session-only **context token** issued by
  `model.describe`/`layer.style` and re-checked against current live state, so
  a proposal prepared against stale state is rejected. Tokens authorize
  nothing, are never persisted, and are rotated on **New chat**.
- The provider turn envelope is now a strict five-key object (`action`,
  `assistant_text`, `tool_calls`, `proposal_kind`, `proposal_json`); the legacy
  three-key shape is rejected. Mode, scope, and approval remain controlled by
  the application, never by provider output.
- Added Phase 02 **Agent Chat**: the Agent Workspace dock now supports a
  bounded, multi-turn, provider-neutral conversation over the same eight
  read-only tools, using any configured non-offline AI connection (OpenAI,
  Anthropic, Gemini, DeepSeek, Ollama, OpenAI-compatible, Azure OpenAI). Each
  provider turn is a strict, schema-constrained, locally re-validated
  `agent_turn` envelope; mode, scope, and tool-call approval are always
  controlled by the application, never by provider output. Conversation
  memory is bounded, in-process only (never persisted), and cleared by
  **New chat**. The `offline` profile keeps quick inspections working without
  a network connection but is not treated as a language model. No graph
  mutation, symbology change, Processing execution, plugin invocation, file
  operation, or approval/apply flow was added.
- Added a Phase 01 **Agent Workspace** foundation: a model-independent QGIS
  dock with typed mode/scope/risk contracts, a fail-closed policy engine, a
  bounded metadata-only context builder, and eight read-only inspection tools
  (project summary, layer list/describe, Processing search/describe, current
  model summary/validate, plugin list) executed through a deterministic
  controller. The dock works with or without the Workflow Studio open and
  never mutates the project, features, or plugins. No LLM loop, MCP, or code
  execution is included yet.
- Added iterative **Improve current** AI turns that receive the existing graph,
  preserve unrelated configuration, preview graph differences, and support
  one-step **Undo AI** recovery.
- Stopped routine profile loading and saving from triggering the QGIS master
  password dialog. Session keys now work explicitly without a password, while the
  encrypted vault is opened only through its optional unlock button.
- Replaced raw missing-input workflow failures with a guided Run/Validate setup
  that focuses each incomplete node and marks it amber on the canvas.
- Added native QGIS Processing parameter widgets, including multiple-layer,
  raster, file, extent, CRS, enum, and field-aware inputs.
- Added safe automatic binding when a required port has exactly one compatible
  project layer; ambiguous choices always remain under user control.
- Fixed Gemini `generateContent` structured-output requests by using the stable REST `responseMimeType` and `responseJsonSchema` fields, with a JSON-mode retry for schema compatibility.
- Added a direct DeepSeek profile using the current `deepseek-v4-flash` Chat Completions endpoint and its supported JSON Object mode.
- Rounded the plugin icon tile with transparent corners while preserving the SmartModeler mark.
- Made AI credentials immediately usable when the QGIS authentication vault is locked: keys now fall back to session-only memory, with explicit storage status and an in-dialog vault unlock action; plaintext persistence remains prohibited.
- Replaced the placeholder plugin icon with a purpose-built SmartModeler GIS brand mark optimized for QGIS toolbar sizes.

### Added

- Real QGIS Processing registry discovery, parameter editing, execution, and result loading.
- Native QGIS `.model3` import/export plus versioned SmartModeler JSON projects.
- Multi-profile AI configuration for OpenAI, Anthropic, Gemini, Ollama, OpenAI-compatible services, and Azure OpenAI.
- Encrypted API-key storage through QGIS Authentication Manager and legacy plaintext-key migration.
- Schema-constrained AI graph planning with installed-algorithm, parameter, socket, and DAG validation.
- AI catalog restrictions for download, command/shell, and direct SQL execution algorithms.
- Auditable Markdown role, QGIS planning, graph-contract, and guardrail context files.
- Reworked Qt 6 interface, live algorithm palette, parameter inspector, validation feedback, execution states, and progress reporting.
- Pure graph/context unit tests and a real-QGIS smoke harness.

### Removed

- Prototype-only fake XML export and non-functional online AI placeholder.
- Unused dock-dialog template files.

## [0.3.1] - 2026-07-22

- Clean node palette and focus strictly on GIS graphical modeler workflows.

## [0.3.0] - 2026-07-22

- Add the first AI settings dialog and experimental visual nodes.

## [0.2.1] - 2026-07-22

- Fix a PyQt 6 painter overload mismatch.

## [0.2.0] - 2026-07-22

- Add the initial AI prompt engine, auto-layout, and prompt bar prototypes.

## [0.1.0] - 2026-07-22

- Initial release.
