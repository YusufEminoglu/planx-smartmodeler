# Changelog

All notable changes to SmartModeler GIS are documented here. The project follows Keep a Changelog and Semantic Versioning.

## [0.5.0] - 2026-07-24

### Fixes from the first owner run in real QGIS

- **Agent Chat no longer dies on its third tool call.** The run budget for a
  whole turn was the 12 000-character bound meant for a single typed message,
  while the fixed context — the static instructions plus every advertised
  tool's schema — is already 10 878 characters in project scope. Two tool
  results overran it and the run ended with "The required context … does not
  fit within the configured prompt budget." The agent turn budget is now its
  own 60 000-character limit, and when a long run does approach it the oldest
  events of the run's own trace are folded into one marker instead of ending
  the run. Turn and tool-call allowances were raised to match (12 turns, 24
  calls per run, 4 per turn).
- **A provider that renumbers its tool calls each turn is no longer refused.**
  Reusing the id `c1` on a second turn ended the run with "The AI reused a tool
  call id from an earlier turn". Call ids only label results within one turn
  and per-turn uniqueness is still enforced, so a repeated id is now
  disambiguated in the run's own record instead. DeepSeek could not complete a
  second turn before this.
- **The agent can count.** `layer.describe` now reports a layer's feature
  count, and a new read-only `layer.field_values` tool returns the distinct
  values of one attribute with how many features carry each. "How many of these
  are bus stops?" is answerable, and a categorized-style proposal can now match
  the real data instead of inventing its classes. Counts only — never a
  feature, an id, or a geometry — bounded to 60 distinct values and a 200 000
  feature scan, and it says so honestly when a layer was too large to finish.
- Asked for a change while in Ask mode, the agent now names the mode that can
  do it and offers to prepare the proposal, instead of only reporting that it
  is read-only.

### Saving, exporting and running a workflow

- **An unfinished workflow saves.** Saving to `.model3` refused to write the
  file whenever QGIS reported the model invalid, which an AI-planned workflow
  with unbound inputs always is — the work was simply unsavable. Now a required
  child input with no upstream connection and no usable value becomes a **model
  input**, so the exported model opens in the QGIS Model Designer and asks for
  the layer; a literal the algorithm itself rejects is dropped rather than
  invalidating the whole model; and if anything is still open, the save is
  offered rather than refused.
- **Export as a QGIS Python algorithm** (`*.py`) alongside `.model3` and the
  SmartModeler project format — the same code QGIS' own *Export as Python
  Algorithm* produces.
- **Run now opens one sheet showing the whole workflow** in run order: each
  step, where its connected inputs come from, and every open input editable in
  place with the project's layers offered in a combo. It replaces the chain of
  one modal dialog per unconfigured node, which hid the flow and gave no way
  back. A **Run setup** toolbar button opens the same sheet at any time, and
  Cancel restores every parameter.

### Hardening, clarity and documentation

- **Fixed a real hole found by the new fuzz suite:** abbreviated IPv4 hosts such
  as `127.1`, `10.1` and `192.168.1` were accepted as ordinary public URLs. Every
  browser resolves them to loopback or private addresses, but Python's
  `ipaddress` does not parse them, so they missed the IP-literal checks entirely
  and passed as two-label DNS names. A host whose rightmost label is all digits
  is now rejected — no real top-level domain is numeric.
- Added `tests/test_agent_fuzz.py`, a **seeded property/fuzz suite** over the
  untrusted-input boundaries: the provider envelope (random text, random JSON,
  nesting bombs, duplicate keys, oversized payloads), bounded text, the run-failure
  sanitizer, the deny-by-default algorithm policy, and the public-URL validator.
  Standard library only, fixed seed, no new dependency.
- The **approval card now carries a risk badge** — computed by a new pure
  `core/agent/action_risk.py` from the action kind and the already-validated
  destructive flag, never from provider text, and never an input to any decision.
  An unrecognized kind fails closed to "high risk, not reversible".
- The current **mode's meaning is stated on screen**: Ask is read-only, Plan is
  review-only, Act prepares one action that still needs your click.
- A **stale approval card now visibly stops being approvable** instead of staying
  clickable until it fails. The timer can only *disable*: it never creates,
  extends, repairs or re-arms an action, and the authoritative expiry check
  remains at the click.
- The conversation transcript is now a **bounded rolling window** rather than an
  unbounded buffer that grew for the life of the panel.
- Panel heights are now derived from font metrics instead of fixed pixels, so the
  proposal, approval, ledger and prompt boxes show their intended number of lines
  at any display scaling; the whole panel scrolls rather than clipping controls in
  a narrow dock.
- Every control has an accessible name and a deliberate tab order. `Ctrl+Enter`
  sends a message — and it is the only accelerator, so **no keyboard shortcut can
  reach Apply, Run or Undo**.
- An **Offline** profile now says so up front, next to the profile name, instead
  of only failing when you press Send. Undo explains when and why it is available.
- `plugin.capabilities` now enumerates algorithms only for providers that can
  actually be attributed to the requested package, instead of for every installed
  provider. Identical output, proportionally less work on a profile with many
  plugins installed.
- Documented the **privacy** boundary (what leaves the machine and what never
  does), added **troubleshooting** for the messages users actually hit, and
  described the Ask/Plan/Act workflow and its limits in the README.
- Wrote a V1 **threat model** covering all thirteen vectors in the plan, each with
  its control, where that control lives, and the test that proves it — including
  the residual risks that are disclosed rather than solved.

### Phase 06

- Added Phase 06 **plugin-aware assistance**. A twelfth and final read-only
  tool, `plugin.capabilities`, reports what an installed plugin can actually be
  used for. It maps a plugin to its live Processing provider(s) by asking the
  **provider registry** which Python package defined each provider, never by
  touching the plugin: the plugin is never imported, instantiated, or read from,
  not even one attribute, because an attribute can be a property that runs
  third-party code. A mapping is therefore either **proved** or reported as
  unproved (`declared_unconfirmed`, `candidate_only`, `ui_only_or_unmapped`) --
  a resemblance is never presented as a confirmation, and an unconfirmed
  provider contributes no algorithm listing.
- Executing a plugin algorithm remains **unavailable** and is now said so up
  front rather than discovered by failure. The reviewed run allowlist is
  unchanged at the twelve core QGIS algorithms and still cannot be enumerated or
  extended.
- `processing.search` and `processing.describe` now expose the safe parameter
  *contract* -- provider id, required/multiple/destination flags, enum option
  labels, numeric bounds, output types, and whether that one algorithm is on the
  reviewed run list. Parameter **default values are deliberately never exposed**,
  since a third-party default can be a file path or a connection string.
- Added **supervised multi-step continuation**. After an action finishes, one
  bounded, sanitized line (kind, status, safe target -- no parameters, ids,
  paths, tokens, or feature values) enters session memory so a later turn can
  refer to it. The agent still never continues by itself: nothing is sent to the
  provider as a consequence of an action completing. A chat session may complete
  at most **ten** actions; **New chat** resets the count along with memory,
  tokens and the ledger.

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
