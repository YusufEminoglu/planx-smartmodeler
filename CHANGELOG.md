# Changelog

All notable changes to SmartModeler GIS are documented here. The project follows Keep a Changelog and Semantic Versioning.

## [0.14.0] - 2026-07-29

### Separate token counters and reviewed cross-plugin actions

- Shows provider-reported input and output usage separately in both Agent
  Workspace and Workflow Studio (`Input … · Output …`), while keeping the
  provider total—including any reasoning/cache difference—in the tooltip.
- Resolves installed plugins by package id, exact visible metadata name, or one
  unambiguous specific alias. Requests for `02viz Studio` and
  `02viz - Geospatial Visualization Studio` now correctly resolve to the
  installed `zero2viz` package instead of being reported as not installed.
- Introduces the general `plugin_action` approval contract for explicitly
  reviewed cross-plugin adapters. Capability inspection lists only
  application-owned package/action ids and signs the plugin's live
  version/enabled/loaded state; Apply verifies all of it again and never calls
  a method named by the provider.
- Ships the first adapter with 02viz: select a live project vector layer, open
  02viz Studio, and invoke its offline smart chart suggestion/render path.
  Feature values remain local to QGIS/02viz and no export path, custom code,
  expression, URL, or network input is exposed to the AI.
- Expands pure regression coverage to 738 passing tests, including visible-name
  ambiguity, capability receipts, strict plugin-action parsing, path rejection,
  risk classification, and the split token display.

## [0.13.0] - 2026-07-29

### General QGIS agent execution and reviewed OSM acquisition

- Replaces misleading generic refusals with stable, live-signature reasons for
  unreviewed providers, known side effects, unsupported parameter types, unsafe
  destinations, missing layer outputs, and signature drift. The assistant is
  explicitly forbidden from guessing “network/external code” when that is not
  the reported cause.
- Broadens structural execution across native QGIS and PlanX algorithms:
  bounded first-party domain strings and current-canvas extent parameters now
  have dedicated tagged bindings, while expressions, code, queries, paths,
  servers, credentials and connection-shaped parameters remain blocked.
  `planx:spacesyntax` and comparable PlanX analyses can therefore run directly
  and inside safe workflows instead of being rejected for their `RADII` text.
- Adds a reviewed QuickOSM current-map-extent adapter. It accepts only plain OSM
  key/value tags, reads the extent from the live QGIS canvas, pins the Overpass
  endpoint and timeout, creates a QGIS-owned temporary GeoPackage, and publishes
  only the requested multipolygon result. Raw Overpass, arbitrary URLs and user
  output paths cannot be expressed. The approval card identifies the network
  request and temporary download as high risk and not undoable.
- Signs Processing output definitions as well as parameters, validates reviewed
  result keys against their live vector-output classes, reports runnability in
  both search and describe, and preserves the one-click-per-action boundary.
- Adds real-QGIS regression probes for PlanX and QuickOSM bindings plus compact
  network, parser, policy, planner and risk tests. The pure suite contains 731
  passing tests. Live QGIS 4.2 runs successfully downloaded a bounded building
  sample from Overpass and executed Space Syntax with `100, 400, n` radii.

## [0.12.1] - 2026-07-29

### Honest token usage and clearer capability boundaries

- Adds a compact provider-reported token counter to Agent Workspace and
  Workflow Studio. The original visible label showed the session/window total;
  its tooltip separated input and output counts. OpenAI, Anthropic, Gemini,
  DeepSeek/OpenAI-compatible/Azure and Ollama response shapes are normalized.
  No count is estimated when a provider omits or malforms usage metadata.
- Makes multi-layer styling requests explicit: one approval card still changes
  one layer, so the assistant must identify the first visual target and say
  that remaining layers require separately reviewable actions instead of
  implying the entire layout was styled.
- Clarifies plugin capability refusals. Successfully inspecting QuickOSM proves
  installation and Processing ownership but does not authorize its Overpass
  network request, optional GeoPackage output, or plugin UI. The assistant now
  states that exact boundary and the matching manual Processing path without
  describing the plugin as broken.
- Expands provider usage, malformed-count, cumulative UI and future QGIS smoke
  coverage. The pure regression suite now contains 719 passing tests.

## [0.12.0] - 2026-07-29

### Resilient, lower-token Agent proposals

- Adds a narrow local recovery boundary for provider proposals that omit or
  malform a freshness token. SmartModeler reuses a trusted same-run inspection
  receipt or performs exactly one controller-gated read-only inspection, then
  sends the proposal through the unchanged strict parser and live validator.
  This avoids another paid provider turn without expanding execution authority.
- Repairs only mechanical layer-style cardinality errors: renderer class counts
  are synchronized with a bounded palette, common inert label/warning defaults
  are filled, and all target, field, renderer and approval checks still fail
  closed through the normal validator.
- Makes clarification continuity explicit: a user-supplied field such as
  `facility` is reused when it exists in the inspected schema, and successful
  identical inspections are not repeated. Plural styling requests are handled
  as one reviewable layer proposal at a time.
- Keeps reviewed optional Processing sinks in the live-signature check while
  leaving them unmaterialized. Attribute extraction now adds only the requested
  matching layer instead of also adding an empty `FAIL_OUTPUT`; table joins
  likewise omit an unrequested `NON_MATCHING` layer.
- Adds regression coverage for cached-receipt reuse, automatic inspection,
  quota enforcement, style recovery, optional-output signature drift and
  one-primary-output planning. The pure suite now contains 715 passing tests;
  QGIS 4.2 and 3.44 LTR registry probes confirm the affected live signatures.

## [0.11.0] - 2026-07-29

### Broad live algorithm intelligence

- Replaces Agent Chat's narrow fixed execution list with a deny-by-default
  structural policy for first-party QGIS and PlanX algorithms. Live signatures
  qualify only when every input has a constrained tagged binding, every
  destination is a temporary map layer, and no opaque file/folder/database/
  expression or known network/project side effect is present.
- Audits 694 live QGIS/PlanX algorithms: all remain searchable and explainable,
  while 318 currently qualify for reviewed one-step temporary-layer runs.
- Processing search now reports and ranks runnable matches, defaults to eight
  results instead of twenty-five, and describe returns a stable rejection
  reason so the provider can try the next safe alternative rather than stop.
- Compresses the repeated Agent instruction context by about 52% while keeping
  exact proposal schemas, binding rules, recovery behavior, and guardrails.
- Updates new Gemini profiles to `gemini-3.6-flash` and omits its deprecated
  temperature parameter, preventing future request failures while using the
  lower-token agentic Flash generation. Existing model selections are retained.

## [0.10.2] - 2026-07-29

### Agent random extraction

- Agent Chat now treats "randomly choose N features as a new layer" as
  `native:randomextract`, exposes that reviewed temporary-output algorithm to
  one-step run proposals, and explicitly distinguishes it from the
  selection-state-only `native:randomselection`.
- Adds a headless acceptance harness and verifies the full describe, proposal
  validation, approved execution, result ownership, and source-state
  preservation path on QGIS 4.2 and QGIS 3.44 LTR.

## [0.10.1] - 2026-07-29

### Real-world AI workflow planning

- Separates Workflow Studio's reviewable graph-planning catalog from Agent
  Chat's narrow execution allowlist. The local PlanX Processing provider and
  reviewed `native:randomextract` step can now be proposed in Studio without
  expanding Agent `processing_run` authority.
- Adds bounded live enum index labels and safe defaults to AI algorithm
  signatures, so providers can distinguish choices such as random feature
  count versus percentage and street buffer versus concave hull.
- Treats provider JSON `null` parameters as explicitly unconfigured values,
  matching the published graph contract. Connected and optional inputs may be
  left unset while required unconnected inputs still fail closed.

### Verification

- DeepSeek generated and SmartModeler executed a real Konak acceptance graph:
  prepare 6,414 OSM roads into 16,092 network segments, randomly select 15 of
  161 bus stops, and create 15 individual plus one merged 320 m walking
  isochrone with 1,491 reached street pieces.
- 697 pure-Python tests and the real QGIS 4.2.0 smoke suite pass.

## [0.10.0] - 2026-07-27

### Accessible workflow authoring

- Adds keyboard activation for algorithms and starter workflows, `Ctrl+F`
  search focus, canvas-local document Undo/Redo, Enter-to-configure, and a
  `Ctrl+Shift+C` connection dialog for pointer-free graph construction.
- Adds a synchronized screen-reader workflow outline containing node state,
  ports, and connection sources, plus accessible names/descriptions and a
  deliberate focus order across primary Studio controls.
- Replaces hidden item-view focus outlines with a two-pixel contrast ring,
  respects the QGIS/system base font, enlarges port hit targets, and darkens
  node header colors for readable white labels.

### Honest and recoverable UX

- Missing Processing algorithms now remain visibly unavailable in Run Setup
  and disable Run instead of being described as ready.
- Run Setup Cancel restores parameters, ordered sources, and dirty state after
  any sheet rebuild; closing AI settings cancels an active connection test.
- Moves actions from Vector to the standard Plugins menu, focuses Agent input
  on open, and adds an in-application Quick Start, Keyboard, Privacy/Safety,
  and Support guide.
- Adds an English-fallback Qt translation lifecycle without claiming shipped
  non-English coverage, corrects provider privacy boundaries and the current
  sixteen-algorithm Agent allowlist, and documents Hub installation/support.

## [0.9.0] - 2026-07-27

### Cancellable workflow execution

- Runs Workflow Studio models through the QGIS task manager so the window
  remains responsive even when a Processing provider emits no progress events.
- Honors QGIS `NoThreading` algorithm flags: such workflows are never sent to
  a worker; Studio refuses the run and instructs the user to export `.model3`
  and run it manually in native QGIS Model Designer.
- Adds a visible Cancel action with `Esc`, disables every other Studio action
  and canvas edit while a run is live, and forwards cancellation to both the
  task and Processing feedback.
- Executes against an immutable workflow snapshot. Closing or unloading during
  a run cancels it, waits for terminal cleanup, suppresses stale UI callbacks,
  and never commits prepared results.

### Structured and atomic results

- Replaced exception-only execution outcomes with structured prepared,
  completed, failed, canceled, and partial reports containing bounded result
  summaries, node counts, failure location, and exact added-layer identities.
- Defers project output commit to the main QGIS thread and validates the full
  public-output contract before mutation. A rejected multi-output commit rolls
  back earlier additions and reports any layer QGIS refused to remove.
- Uses the engine's exact result ledger for supervised Agent runs instead of a
  whole-project before/after diff, so unrelated layers added concurrently are
  never claimed or removed.
- Rejects missing, scalar, duplicate, oversized, or existing-project Agent
  results. A cancel arriving during the layer-add boundary is terminal; cleanup
  is verified before cancellation or failure is reported.
- Enforces one plugin-global execution slot across Workflow Studio and Agent
  Workspace, including model/style apply boundaries, so the two surfaces
  cannot race over one graph or project.

### Verification

- 695 pure-Python tests plus real-QGIS adversarial coverage for cancellation,
  nested execution, immutable snapshots, partial failures, active unload,
  atomic output rollback, rollback failure, exact ownership, and late cancel.
- QGIS 4.2.0 and QGIS 3.44.12 LTR run a progressless five-second Processing
  fixture and prove that Cancel terminates it early without project mutation.

## [0.8.0] - 2026-07-26

### Ranked contextual workflow proposals

- Ranks installed next-step algorithms against the selected live output socket
  and a compatible target input instead of showing type-only shortcuts.
- Shows the proposed connection, target port, reason, and rank before apply.
- Applies a recommendation as one add-and-connect document edit; stale source
  or target signatures fail closed, and one Undo restores the prior graph.

### Schema-driven micro-packages

- Replaced prompt-key starter workflows with five shipped, versioned workflow
  schemas covering buffer, filter-buffer, slope, centroids, and overlay clip.
- Validates package fields, bounds, algorithm availability, live parameters,
  connections, and declared outputs before a graph reaches the canvas.
- Builds packages deterministically without an AI profile or network request;
  unavailable packages are omitted from the palette.

### Model contract controls

- Added Model Properties for workflow name, description, explicit output mode,
  public output names/descriptions, and mandatory flags.
- Allows a model to publish zero results, a selected subset, or an intermediate
  Processing layer output. Smart inputs and scalar/file outputs are excluded
  consistently from the dialog, JSON, native `.model3`, and Studio runtime.
  Missing mandatory layer results fail before any result mutates the project
  instead of disappearing or leaving a partial result set. The controls
  participate in document Undo and dirty state.

### Verification

- 694 pure-Python tests, including shipped package schema, graph fixtures, and
  fail-closed public-output contract coverage.
- QGIS 4.2.0 and QGIS 3.44.12 LTR smoke coverage for all five packages,
  ranked auto-connect with Undo, and non-terminal public output editing.

## [0.7.1] - 2026-07-26

### Fixed

- Reconstructs Processing nodes from their stored algorithm configuration
  before validating ports, connections, parameters, or outputs.
- Uses the same configured algorithm schema for native import, SmartModeler
  JSON reload, native export, and Studio execution.
- Adds a real-QGIS configuration-sensitive provider fixture whose input and
  output signature changes in `initAlgorithm(configuration)`, preventing a
  regression to the provider's unconfigured base schema.

## [0.7.0] - 2026-07-26

### SmartModeler documents are versioned, bounded, and registry-validated

- Replaced the permissive JSON reader with the V3 document codec. It enforces a
  4 MiB file limit, bounded node/edge/output/parameter collections, finite
  coordinates and numbers, bounded nesting/text, exact fields, unique JSON
  keys, and typed primitive/list/tuple/dictionary values.
- Rebuilds every node and port from the live Processing registry. Stored files
  can no longer inject fake ports or parameters.
- Migrates valid `SmartModelerGIS_v2` documents through the same live-signature
  validation and writes them back as V3.
- Rejects unavailable algorithms, dangling dependencies/outputs, mismatched
  ordered sources, unknown source kinds, malformed graphs, cycles, and future
  format versions before they reach the canvas.

### Native QGIS model interchange is semantic instead of lossy

- Preserves QGIS model parameter definitions through the official
  `toVariantMap()` and `parameterFromVariantMap()` APIs.
- Imports vector, raster, number, boolean, string, field, CRS, extent, enum,
  generic map-layer, multi-vector, and multi-raster inputs as distinct typed
  SmartModeler nodes. Default-less required inputs remain visibly unconfigured
  instead of silently becoming `0` or `False`.
- Preserves ordered mixtures of static values, model parameters, and child
  outputs on multi-input parameters. Studio execution and exported `.model3`
  files now consume the same ordered source list.
- Preserves explicit child dependencies including conditional branches.
- Preserves inactive children and algorithm configuration maps. Studio runs
  skip inactive descendants and prune branches whose conditional output is
  false, matching native model control flow.
- Preserves only the model outputs actually published by the native model,
  including public name, description, mandatory flag, and default value.
  Studio runs load that same declared subset, including non-terminal outputs;
  an explicit zero-output model adds no result layer.
- Applies edited SmartModeler model-input values back to the native parameter
  default instead of restoring the value captured at import time.
- Fails closed when a native model uses an unsupported expression source rather
  than silently dropping it.

### Graph integrity

- Includes explicit dependencies in deterministic topological sorting and cycle
  checks.
- Purges dependency/output references when a node is removed and invalidates a
  preserved source order when the user changes its parameter or connections.
- Replaced separator-concatenated edge IDs with deterministic UUID5 identities,
  preventing collisions when node or port IDs contain underscores.
- Handles multiple data edges between the same node pair without false cycle
  reports.

### Verification

- 690 pure-Python tests, including a malformed-document and V2 migration corpus.
- QGIS 4.2.0 semantic smoke with 784 catalog records.
- QGIS 3.44.12 LTR semantic smoke with 452 catalog records.
- Both runtimes round-trip ten model parameter types plus ordered
  static/model/child sources, inactive/configured children, edited defaults,
  conditional dependencies, branch execution, and declared output metadata.

## [0.6.0] - 2026-07-26

### Security boundaries now match the product's privacy claims

- Removed `layer.field_values`; Agent Workspace now has twelve strictly
  metadata-only inspection tools and never reads feature attributes.
- Redacts every existing workflow parameter before a connected AI request.
  Providers see only a retention token; matching local values are restored
  after response validation, while token use on a new or replaced node is
  rejected.
- Replaced the planner's side-effect blacklist with the application-owned,
  deny-by-default reviewed algorithm policy. File upload/download, directory,
  project-variable, layer-loading, styling, SQL, command, and other unreviewed
  algorithms cannot enter a provider-generated graph.
- Validates every bindable live Processing parameter against its reviewed
  signature, including the vector/raster type of multiple-layer inputs.

### Documents are recoverable and ordinary edits are reversible

- Added bounded general Undo/Redo for node creation, removal, configuration,
  connection changes, movement, auto-layout, setup edits, AI changes, and Agent
  model patches.
- Added current-path and dirty-state tracking, Save and Save As, guarded
  New/Open/Close flows, and periodic recovery of unsaved work.
- Writes SmartModeler JSON, `.model3`, and Python exports through same-directory
  temporary siblings followed by atomic replacement.
- Preserves dirty work on plugin unload and restores crash-recovery snapshots on
  the next Studio launch.

### Agent Undo fails closed after any later target edit

- Fingerprints the complete local QML style rather than the privacy-reduced AI
  summary.
- Watches layer style, attribute, geometry, feature, CRS, name, and edit signals.
  Undo is disabled if a result or styled layer changed after the agent action,
  even when feature count, extent, and other coarse identity fields stayed the
  same.

### Verification

- 673 pure-Python tests.
- QGIS 4.2.0 smoke with 775 installed algorithms.
- QGIS 3.44.12 LTR smoke with 443 installed algorithms.
- 51/51 adversarial probes on both QGIS runtimes.

## [0.5.3] - 2026-07-24

### The agent can run three more everyday GIS requests

The v0.5.2 run made "filter these features into a new layer" a one-click action,
but the very next natural asks — "keep what's inside this boundary", "join this
table onto that layer", "merge these layers into one" — still had no runnable
algorithm behind them. Three more side-effect-safe native algorithms join the
reviewed safe-run allowlist (**sixteen** algorithms now):

- **`native:extractbylocation`** — the spatial sibling of extract-by-attribute.
  "Keep the features of X that intersect / are inside / touch Y." Reads the two
  vector layers and the spatial predicate (bound only as a live option index)
  and writes a forced temporary output.
- **`native:joinattributestable`** — attach the attributes of one layer onto
  another where a key field matches. Each join key binds to its own input layer,
  and both sinks (the joined layer and the non-matching complement) are pinned
  to forced temporary outputs. It runs no expression and takes no path.
- **`native:mergevectorlayers`** — combine several vector layers into one. Its
  multi-layer input is pinned as a new `MULTI_VECTOR` binding kind, so the run
  planner demands vector inputs for it (the existing multi-raster kind demanded
  rasters); a raster bound here is refused.

Every new signature was probed identical on QGIS 3.44.12 LTR and 4.2.0, each
algorithm executes and undoes through the real-QGIS smoke, and the deny-by-
default policy is unchanged: the allowlist still grows only by shipped code and
review, never at runtime, and every destination is still forced to a temporary
output. The tool instructions now point the model at these three runs so it
reaches for them instead of stopping at "I can't".

## [0.5.2] - 2026-07-24

### The agent can now run a filter and produce a layer

The v0.5.1 owner run showed `layer_style` proposals applying end-to-end, but a
"filter these features into a new layer" request went nowhere: the agent found
`native:extractbyattribute`, described it, checked the field values — and then
had no way to turn that into an action.

- **The instructions never told the model it could propose a run.** `00_ROLE.md`
  listed only `layer_style` and `model_patch`; `processing_run` and `model_run`
  existed in the engine but were invisible to the provider, so it never emitted
  one. All four proposal kinds are now described, and the exact `processing_run`
  and `model_run` payloads — including every tagged input-binding form — are
  documented with a worked `extractbyattribute` example. The doc's own examples
  are extracted and parsed by a test, so they cannot drift from the validator.
- **`native:extractbyattribute` is now on the reviewed safe-run allowlist**
  (thirteen algorithms). It reads one vector layer and writes forced temporary
  outputs — a matching-features layer and its non-matching complement — never a
  path or a disk file. Signatures verified identical on QGIS 3.44.12 LTR and
  4.2.0, and the real-QGIS smoke now filters `highway = bus_stop` into a new
  layer end-to-end and undoes it.

So "keep only the bus stops as a new temporary layer" is now a one-click
`processing_run` proposal in Act mode. (Selecting features in place, as opposed
to extracting them to a new layer, is still not something the tools express.)

### A run no longer fails on an unsettable parameter, and failures are remembered

Two more issues from the same session:

- **"This parameter cannot be set by a proposal."** A reproject run failed
  because the provider tried to bind a parameter the safe policy does not allow
  (only `INPUT` and `TARGET_CRS` are bindable for `native:reprojectlayer`), and
  it had no way to know which parameters those were. `processing.describe` now
  returns a `proposal_binding` for every parameter — the exact tagged form to
  use, or empty when a run may not set it at all — and the instructions tell the
  model to set only those. A parameter it omits keeps the algorithm's default.
- **The agent lost the thread after an error.** A failed run recorded nothing,
  so a follow-up like "why?" started fresh and the agent could only say it did
  not understand. A failed attempt is now kept in the bounded session memory as
  a short note (never the raw proposal or provider text), so the next message
  can refer back to what just happened.

## [0.5.1] - 2026-07-24

### The agent can now actually complete a proposal

The first end-to-end owner run of v0.5.0 showed the agent inspecting correctly
but failing every attempt to *do* anything. Two root causes, both fixed:

- **The proposal schema was never given to the model.** The instructions
  described the response *envelope* but not what goes inside a `layer_style` or
  `model_patch` payload, so the provider guessed field names (`renderer_type`,
  `classes`, `field_name`, `categories`, `symbol_type`, ...) and the validator
  rejected every one with "Invalid layer_style fields: missing ...; unexpected
  ...". The exact shapes are now documented with concrete examples, including
  the renderer families, the class-count/palette rules, and the labels block. A
  new test extracts those very examples from the instructions and parses them,
  so the documentation and the validator can never drift apart again.
- **A provider that omitted an inapplicable key was hard-refused.** A `final`
  or `proposal` response that left out `tool_calls` ended the run with
  "Provider response has unexpected or missing fields: ['tool_calls']". A
  missing optional key now takes its safe default (`tool_calls` → none,
  `proposal_kind` → "none", `proposal_json` → ""), and an unknown extra key a
  provider adds is ignored rather than fatal. Only `action` is still required,
  and the inner tool-argument and proposal validators stay strict — that is
  where authority actually lives.

- When a request is outside every tool (select features, save the nearest N as
  a layer), the agent now offers the closest thing it can build — a Processing
  `model_patch` the user runs from the Studio — instead of only stating that it
  is read-only.

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
