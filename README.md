# SmartModeler GIS

[![QGIS](https://img.shields.io/badge/QGIS-3.44%2B%20%7C%204.0%2B-589632.svg)](https://qgis.org)
[![Release](https://img.shields.io/badge/release-1.2.1-2f80ed.svg)](CHANGELOG.md)
[![License](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](LICENSE)
[![Documentation](https://img.shields.io/badge/📖_Reference_Manual-13a0a0)](https://yusufeminoglu.github.io/planx-smartmodeler/)

SmartModeler GIS is a QGIS 3.44+ and QGIS 4 visual studio for building and running real QGIS Processing workflows. It combines a typed node canvas, live algorithm discovery, validated AI planning, native `.model3` interchange, and a focused Qt5/Qt6 interface.

## 📖 Documentation

**[Comprehensive Academic Reference Manual](https://yusufeminoglu.github.io/planx-smartmodeler/)** — OSM algorithms, Workflow Studio, AI agent architecture, workflow patterns, and 83 DOI-verified academic references. Hosted on GitHub Pages.

Version 1.2.1 is the current production release. Every action proposed by a connected
provider remains inert until it passes local validation and the user explicitly
approves it; deterministic offline workflow generation remains available
without a network connection.

## Current capabilities

- Discovers installed algorithms directly from the QGIS Processing registry.
- Audits the complete live registry on both supported QGIS runtimes: every
  installed algorithm must construct a typed node and preserve its port schema
  through a bounded SmartModeler JSON round-trip.
- Builds typed, acyclic graphs and rejects incompatible or duplicate connections.
- Configures layers, multi-layer collections, files, extents, CRS values, and
  other parameters with native QGIS Processing controls.
- Opens **Run setup**: one sheet showing every step in run order, where each
  step's connected inputs come from, and every open input editable in place with
  the project's layers offered in a combo -- while safely auto-binding the sole
  compatible project layer when unambiguous.
- Executes an immutable workflow snapshot in a cancellable QGIS background
  task, keeping the Studio responsive even when a provider emits no progress.
  Every canvas/edit action is locked during the run except visible **Cancel**
  (`Esc`); active nodes run in topological order and unselected conditional
  branches are pruned. Algorithms marked `NoThreading` by QGIS are never sent
  to a worker; Studio refuses the run and instructs you to export `.model3`
  and run it manually in native QGIS Model Designer instead of risking a
  cross-thread provider call.
- Adds only explicitly published vector and raster model results to the current
  project; legacy Studio graphs without declarations retain terminal-output
  behavior. The entire output contract is validated first and committed on the
  main QGIS thread as one set. Structured run reports distinguish completion,
  cancellation, failure, and partial execution and retain exact result-layer
  identities for safe cleanup.
- Imports and exports bounded, versioned SmartModeler JSON and native QGIS
  `.model3` files, and exports the workflow as a runnable QGIS Python algorithm.
  V3 documents rebuild ports from the live Processing registry instead of
  trusting stored schemas; V2 documents migrate through the same validation.
  A workflow whose
  inputs are not bound yet still saves: each unbound required input becomes a
  model input, so the `.model3` opens in the QGIS Model Designer and asks for it.
- Preserves native boolean, string, number, field, CRS, extent, enum, map-layer,
  vector/raster, and multi-layer model parameters; ordered mixed static/model/
  child sources; inactive/configured children; conditional child dependencies;
  edited input defaults; and explicitly published model outputs with their
  public metadata. Configuration-dependent algorithms rebuild their live port
  schema from the stored configuration before validation or execution.
- Tracks the complete editable document with general Undo/Redo, dirty-state
  indication, guarded New/Open/Close, atomic Save/Save As, and crash recovery.
- Ranks contextual next steps against the selected live output and compatible
  target inputs. Each proposal explains and previews its target connection,
  then adds and auto-connects the node as one undoable edit.
- Ships fifteen versioned, schema-validated micro-package workflows. They build
  deterministic graphs directly, without an AI profile or network request,
  and are hidden when a required Processing algorithm is unavailable.
- Edits workflow name, description, and the exact public output contract in
  Model Properties, including explicit zero outputs, selected subsets, and
  published intermediate Processing layer results. Smart/scalar/file outputs
  cannot be published, and unavailable mandatory results fail the run.
- Generates workflows through deterministic offline rules or, for every
  configured connected provider, the shared multi-turn Agent Workspace.
  Workflow Studio submits connected requests in `Current model` + `Act` mode,
  so building and improving a graph use the same live model inspections,
  validated proposal contract, explicit approval card, recovery logic, and
  run limits as the assistant dock. Offline generation remains local and keeps
  its one-step **Undo AI** recovery.
- Offers a separate **Agent Workspace** dock with bounded, read-only project,
  layer, symbology/labeling, Processing, model, and plugin inspections through
  a fail-closed policy engine, plus a bounded, provider-neutral **Agent Chat**
  conversation over a capability-routed registry of **eighteen** read-only tools
  using any configured non-offline
  AI connection (OpenAI, Anthropic, Gemini, DeepSeek, Ollama, OpenAI-compatible,
  Azure OpenAI). Every provider turn is a strict, locally re-validated
  structured envelope; mode, scope, and every tool call's execution stay under
  application control, never provider control, and the tool set is
  metadata-only (no feature values, source URIs, style/label expressions,
  baseline model parameter values, or credentials). Quick inspections keep
  working with the `offline` profile, which is not treated as a language model.
- Uses a dedicated node-and-spark Agent Workspace toolbar icon that remains
  distinct from the main Workflow Studio action at small QGIS toolbar sizes.
- In Plan or Act mode the Agent Workspace can show eight kinds of **validated
  proposals**: a model-workflow patch, a vector/raster symbology-and-labeling
  intent, a single reviewed Processing run, a run of your current workflow, and
  one explicitly reviewed cross-plugin action, plus opt-in SQL, trusted-script,
  and generated-PyQGIS runs.
  A proposal is inert data validated locally (a model patch only on a
  detached graph clone; a style proposal only against the live layer's fields).
  In **Plan** it stays review-only with a **Not applied** status. In **Act** it
  produces a single pending action on a read-only approval card, and it is
  applied only when **you explicitly click Apply** -- the AI never approves,
  applies, or undoes anything, and there is no Approve-all/remembered/background
  approval. Apply re-checks the live state and proposal integrity at the click,
  commits one atomic change (rolling back on any failure), and records the
  outcome in a bounded in-session action ledger.
- An approved **run** executes either a signature-pinned reviewed algorithm or a
  first-party QGIS/PlanX algorithm whose live signature passes the local
  structural policy: constrained typed inputs, temporary map-layer destinations,
  bounded domain text/current-canvas extent bindings, and no opaque
  file/folder/database or unreviewed network/project side effects.
  This includes PlanX analyses such as Space Syntax whose radii are safe domain
  text. The same policy checks every workflow step again at Run time. There
  is no "run any algorithm" path, and neither the AI nor a prompt can weaken
  these rules. Runs show progress, can
  be **cancelled**, and write results to **temporary layers**: no user-selected
  file, folder or database destination can be expressed. Agent results are accepted
  only from the exact engine/result ledger; missing, duplicate, scalar,
  oversized, or already-present layers fail closed. A failed or cancelled run
  verifies cleanup and never claims or removes an unrelated project layer.
  Workflow Studio and Agent Workspace share one execution slot, so they cannot
  run or apply competing changes to the same graph/project concurrently.
- **Power Mode is explicit and off by default.** When enabled, the Agent can
  inspect stored PostGIS/GeoPackage connection metadata through opaque receipts,
  propose one complete SQL statement, use a managed hash-pinned trusted script,
  or show complete generated PyQGIS source. SQL/Python is never hidden behind a
  generic tool call: the approval card shows the full source. Database writes
  and DDL require a second confirmation; live in-process Python requires a
  second confirmation and carries a no-rollback warning. Generated Python
  defaults to a cancellable, timeout-bounded separate QGIS process, snapshots
  selected vector inputs, and imports only its requested/new vector outputs.
  This is process isolation, not a security sandbox: code still has the current
  user's filesystem and network permissions.
- QGIS expressions are a typed, reviewed capability instead of generic text.
  Agent Workspace can run `native:fieldcalculator` with formulas such as
  `rand(1, 15)`, `$area`, quoted field references, `CASE`, and ordinary QGIS
  math/geometry/string/date functions. The live `QgsExpression` parser checks
  syntax and referenced fields before approval; custom Python, dynamic
  evaluation, environment and filesystem functions remain blocked.
- **Undo last agent action** reverts the most recent applied model or style
  change, or removes the result layers of the most recent run, but only while the
  live target still matches that action's post-state, so it never overwrites or
  removes a later edit of yours. Applable style families are `keep`,
  `single_symbol`, `categorized`, `graduated` and `raster_gray`.
- **Plugin-aware, honestly.** `plugin.capabilities` tells you what an installed
  plugin can be used for: it identifies the plugin's live Processing provider by
  asking the provider registry which Python package defined it, then lists that
  provider's algorithms. It never imports, instantiates, or reads the plugin --
  not even one attribute -- so a mapping is either proved or reported as
  unproved; a look-alike name is never presented as a confirmation. Each
  Processing algorithm is independently marked runnable or blocked from its
  live signature. UI-only plugins are resolved by package or visible name.
  SmartModeler drives no arbitrary button or method; a plugin can be controlled
  only through an application-reviewed `plugin_action` adapter shown on its own
  approval card. The first adapter opens 02viz on one selected vector layer and
  renders 02viz's offline smart chart suggestion.
- **Shared AI connections for companion plugins.** Trusted PlanX companions
  such as 02Agent OSM Downloader can open SmartModeler's AI Connections and
  Agent Workspace through a narrow public bridge. They receive only
  display-safe profile/provider/model information; API secrets remain in
  SmartModeler's session memory or encrypted QGIS vault.
- A chat session can carry a task across several steps: after an action finishes,
  a short sanitized note of what happened stays in the conversation so you can
  say "now style the result". The agent never continues on its own -- you ask
  each time -- and a session is capped at ten actions. Same-run inspection
  receipts are reused, explicit field clarifications are authoritative when the
  inspected schema contains that field, and identical successful inspections
  are not repeated. A mechanically missing proposal receipt can be restored
  locally with one bounded read-only inspection, avoiding another provider turn
  while preserving the normal strict validation and approval boundary.
- A compact token label shows provider-reported **last request**, **chat input**
  and **cached input** counts; hover for input/output/total and the conservative
  local estimate. The Agent sends only a compact core prompt plus intent-specific
  expression, OSM, or Power packs, advertises only the relevant tools, retains
  six bounded exchanges, compresses tool traces, and caps structured output.
  If a provider repeats an already successful inspection, the result is reused
  without executing the tool again and the orchestrator issues three escalating
  strategy interventions: finish from existing evidence, make one materially
  different call, or name the exact blocker. Only a fourth consecutive repeated
  turn, after all three interventions were ignored, ends the run as unresponsive.
  It never blocks a task solely because of the local estimate. Confirmation is
  requested only when a task crosses 300,000 estimated input tokens, at each
  subsequent 100,000-token milestone, or when one next request alone is
  estimated at 100,000 tokens or more.
- Reviewed optional Processing result sinks remain signature-checked but are
  left unset unless they are the requested output. For example, **Extract by
  attribute** adds the matching temporary layer without also cluttering the
  project with an unrequested `FAIL_OUTPUT` layer. Explicit equality and numeric
  threshold requests (`<`, `<=`, `>`, `>=`, below/above wording) are prepared
  locally without spending a provider turn. An exact field-name lookup checks
  the complete live layer schema, so a target beyond the bounded 100-field
  preview is not falsely reported as missing.
- SmartModeler now ships its own dependency-free OSM Processing provider.
  Geometry-specific point, line, and polygon algorithms accept a plain key/value
  tag such as `highway=bus_stop`, `highway=*`, or `building=*` plus the live
  canvas extent. They try three pinned Overpass mirrors, enforce area/response/
  feature limits, honor QGIS proxy settings and cancellation, and create one
  temporary vector layer without QuickOSM. Raw queries, endpoints, URLs and
  paths are not exposed. The older QuickOSM adapter remains an explicit fallback.
  Every OSM request is shown as a high-risk approval; Undo removes the result
  layer but cannot undo a completed network request.
- Extent inputs can safely reference either the current canvas or the extent of
  a named project layer. Processing parameters with configured defaults are
  omitted unless the user explicitly asks to override them, so plugin algorithms
  run with their real QGIS defaults instead of triggering unnecessary questions.
- `layer.list` marks and prioritizes the live active layer. Active-layer extent
  requests therefore use its exact id directly, and a missing display note on
  an otherwise valid proposal is repaired locally without another paid AI turn.
- When the optional 02Agent OSM Downloader is installed, Agent Workspace can
  run its curated thematic preset or custom-tag Processing algorithms with the
  same temporary-output and explicit network-approval boundary. The built-in
  SmartModeler OSM provider remains the standalone fallback.
  Its combined Urban Context preset obtains roads, building footprints,
  individual trees and tree rows in one bounded request and one approval card.

## AI providers

The profile-based AI settings screen supports:

- OpenAI Responses API
- Anthropic Messages API
- Google Gemini API (`gemini-3.6-flash` preset)
- DeepSeek API (`deepseek-v4-flash` preset)
- Ollama
- OpenAI-compatible services and local runtimes
- Azure OpenAI
- SmartModeler Offline, which never sends a network request

Models, timeouts, endpoints where appropriate, project context, and algorithm-catalog limits are configurable per profile. API keys are never written to plugin JSON or ordinary `QgsSettings`. Session-only memory storage works without a password; optionally, the QGIS Authentication Database can encrypt the key across restarts. Its master password is a QGIS password—not the provider API key—and SmartModeler opens it only after the explicit **Unlock vault** action. Legacy plaintext settings are migrated and removed.

AI is a planner, not an execution authority. The provider receives compact
Markdown instructions plus metadata-only project layers and an on-demand,
bounded search of the live Processing registry. Runnable matches are ranked
first; only the chosen algorithm's typed signature is described. Workflow
Studio can plan with safe native/QGIS and PlanX algorithms, while Agent Chat
independently rechecks its stricter structural execution policy. Live enum
meanings and safe defaults keep choice indices unambiguous. Feature values are
not included. Returned JSON must pass the shipped schema, installed-algorithm,
parameter, socket-type, and DAG checks. `null` means deliberately unconfigured.
With Power Mode off, AI output cannot request Python, shell commands, database
actions, arbitrary downloads, user-selected filesystem changes, or arbitrary
network actions. SmartModeler's bounded point/line/polygon OSM algorithms and
the legacy QuickOSM fallback are application-owned and separately approved.
Power Mode deliberately adds the separately reviewed SQL/Python contracts
described above; it never adds shell-command proposals or background approval.

The auditable instruction set lives in [`ai_context/`](ai_context/):

- `00_ROLE.md` defines the GIS planning role.
- `10_QGIS4_PLANNING.md` defines QGIS workflow practices.
- `15_ITERATIVE_EDITING.md` defines preservation rules for repeated AI edits.
- `20_GRAPH_CONTRACT.md` defines the exact graph response contract.
- `30_GUARDRAILS.md` defines trust boundaries and prohibited actions.

## Basic use

1. Open **Plugins > SmartModeler GIS > SmartModeler GIS - Workflow Studio**.
   The adjacent **Agent Workspace** action opens the supervised assistant dock.
2. Add installed algorithms from the palette or choose a starter workflow.
3. Connect compatible ports, then double-click a node to configure it or open
   **Run setup** to review and fill in the whole workflow at once.
4. Use **Validate** and then **Run**.
5. Optionally configure an AI profile and describe the workflow in the prompt bar.
6. Save a portable SmartModeler JSON file, a QGIS `.model3` model, or a QGIS
   Python algorithm.

### Example workflow gallery

The **Example workflows** gallery places ten complete showcase graphs before
the smaller starter recipes. Double-click one to load and fit the whole graph;
the inputs remain deliberately unconfigured so the example never guesses which
project layers to use.

- **Walkable City Atlas** — 13 nodes for pedestrian corridors, reachable
  buildings, amenity counts, and service cells.
- **Blue-Green Resilience** — 14 nodes with parallel park and water influence
  branches, served buildings, a combined network, and an overlap core.
- **Urban Morphology Lab** — 11 nodes deriving building edges, centres,
  Voronoi cells, generalized grain, and continuous urban fabric.
- **Flood Readiness Atlas** — 15 mixed raster/vector nodes for slope, aspect,
  hillshade, flood influence, and terrain-enriched exposure points.
- **Growth Constraint Atlas** — 13 nodes for protected buffers, developable
  land, settlement fabric, growth cells, and a strategic envelope.

Additional showcases include PlanX Network Centrality (12 nodes), Urban
Resilience Heat (15), PlanX Settlement Fabric (13), 15-Minute Transit City
(15), and Suitability Constraints (16). Together they add parallel branches,
multiple public outputs, and intentionally open inputs for attractive first-run
diagrams without guessing project layers.

Every showcase uses installed QGIS Processing algorithms, exports to native
`.model3`, and is validated without accessing feature values or changing the
open project.

### Agent Workspace: Ask, Plan, Act

The dock has one selector that decides how far the agent may go. It is always
visible, and the panel states its meaning in plain words underneath.

| Mode | What the agent may do | What you have to do |
|---|---|---|
| **Ask** | Answer, and inspect read-only. It cannot propose anything. | Nothing. |
| **Plan** | Propose one change, shown as **Not applied**. | Nothing — there is no Apply or Run control in Plan. |
| **Act** | Prepare **one** action on an approval card. | Click **Apply** (or **Run**) yourself. Nothing happens until you do. |

The approval card names the exact target, carries a **risk badge** and says
whether the action can be undone. A card that has gone stale stops being
approvable rather than failing at the click. `Ctrl+Enter` sends a message; there
is deliberately **no keyboard shortcut for Apply, Run or Undo**.

Practical limits worth knowing before you rely on it:

- Results of a run are **temporary layers**. Save anything you want to keep.
- **Undo** is one level deep and only while the target is untouched. If you edit
  the layer or model afterwards, Undo steps aside rather than overwriting you.
- A chat session is capped at **ten** actions. **New chat** resets it, and also
  clears the conversation, the ledger and the freshness tokens.
- The agent never continues by itself. Every step is one you asked for.

### Keyboard and accessibility

| Command | Keyboard path |
|---|---|
| Find and add an algorithm | `Ctrl+F`, type, arrow to a result, `Enter` |
| Configure a selected node | `Enter` while the canvas has focus |
| Connect two ports | `Ctrl+Shift+C`, choose source and target, activate Connect |
| Remove selected nodes or edges | `Delete` while the canvas has focus |
| Fit the graph | `F` on the canvas or `Ctrl+Shift+F` anywhere in Studio |
| Undo or redo graph edits | `Ctrl+Z` / `Ctrl+Y` while the canvas has focus |
| Run or cancel | `Ctrl+R` / `Esc` |

The Node Inspector contains a keyboard- and screen-reader-accessible workflow
outline with every node, execution state, input, output, and connection. Primary
Studio, settings, run, approval, and help controls expose accessible names.
Focus indicators use a two-pixel high-contrast ring, and SmartModeler respects
the QGIS/system font instead of forcing a font family or base size. The same
Quick start, Keyboard, Privacy/Safety, and Support guidance is available inside
QGIS through **SmartModeler GIS - Help and Safety**.

## Privacy

SmartModeler makes a provider request only after you submit a Planner prompt or
Agent message to a connected, non-offline profile. That request contains what
you typed, SmartModeler's static instructions, and the metadata enabled in the
selected profile.

The Workflow Studio Planner can include bounded project layer metadata and
installed Processing signatures. **Improve current** also includes the redacted
workflow structure: model name/description, node and algorithm identifiers,
parameter names, connections, and local-only retention tokens in place of
existing parameter values.

Agent Chat can include bounded inspection results such as project title and
layer count; layer IDs, names, visibility, provider type, geometry, CRS, field
names/types, and feature count; installed plugin names/versions; and workflow
structure/validation summaries. Scope and mode restrict which inspection is
available.

SmartModeler does not automatically collect feature or attribute **values**,
source paths, data-source URIs or connection strings, style/label expressions,
credentials/API keys, or the project file path. Anything you type into your own
message is part of that message, so do not paste secrets or private values.
Storage, training, and retention after delivery are governed by the provider
you configured and are outside SmartModeler's control.

The **SmartModeler Offline** profile sends no network request at all, and every
quick inspection in the dock works with it. API keys are held in the QGIS
Authentication Database when you have unlocked it, otherwise in memory for the
session only; they are never written to plugin JSON or ordinary settings, and the
dock clears its working copy on cancel, close and unload. The action ledger is
in-session only and is **not** an audit trail — it is cleared by New chat and by
unload.

## Troubleshooting

**"Agent Chat needs a configured AI connection."** The active profile is
`offline`. Quick inspections still work; open **AI connections...** to configure a
provider for chat.

**The Run or Apply button is greyed out.** Either no action is pending, a run is
already executing (one at a time, by design), the proposal has gone stale, or the
session has reached its ten-action cap. The panel says which.

**"The target changed; Undo is no longer available."** Something edited the layer,
model or result layers after the action. This is intentional: Undo will not
overwrite a later change of yours.

**A run failed with a generic message.** Processing failure text routinely embeds
file paths and connection strings, so it is replaced rather than shown. The full
message is in the QGIS **Processing log**.

**A plugin shows as `declared_unconfirmed` or `candidate_only`.** No live
Processing provider could be *proved* to come from that package — it may be
disabled, may have failed to load, or may register under another package name.
The panel reports what it can prove, and nothing more.

**QGIS asks for a master password.** Not during loading — only after you choose
**Unlock vault** in AI connections. That password is the QGIS vault password, not
your provider API key.

**A provider endpoint is rejected.** Remote endpoints must use HTTPS. Plain HTTP
is accepted only for loopback hosts such as `localhost` and `127.0.0.1`. Check
the provider-specific endpoint, deployment/model name, API version, and timeout.

**A provider connected but returned an invalid workflow.** Connectivity alone is
not enough: the response must satisfy the local SmartModeler graph contract.
Try a model with reliable structured-output support or simplify the prompt.

**An API key must be rotated or removed.** Open **AI connections**, replace the
key and save, or delete the profile. A session-only key disappears when QGIS
closes. A persisted key is stored in the QGIS Authentication Database and must
be managed with that database unlocked.

## Requirements and installation

- QGIS 3.44 LTR or newer (including QGIS 4)
- No pip or external Python dependencies

For a Hub release, open **Plugins > Manage and Install Plugins**, search for
**SmartModeler GIS**, and choose Install. For a manual or development build, use
**Install from ZIP**; the ZIP root must be `planx_smartmodeler/`.

After installation, the **Plugins > SmartModeler GIS** submenu contains Workflow
Studio, Agent Workspace, and Help and Safety. The first two also have toolbar
actions.

## Support

Report reproducible bugs and feature requests at the
[SmartModeler GIS issue tracker](https://github.com/YusufEminoglu/planx-smartmodeler/issues).
Include QGIS and plugin versions, exact steps, and relevant Processing log
messages. Remove private paths, data-source details, and credentials first.

## Development checks

From the physical plugin monorepo root:

```powershell
py -3 packaging/pf.py verify planx_smartmodeler
python -m pytest planx_smartmodeler\tests -q
python -m unittest discover -s planx_smartmodeler\tests -v
python -m flake8 planx_smartmodeler
python packaging\validate_plugin.py planx_smartmodeler
python packaging\hub_security_scan.py planx_smartmodeler
.\packaging\Build-PluginZip.ps1 -PluginDir planx_smartmodeler
```

The unit suite is pure Python and needs no QGIS: modules that touch `qgis.core`
are exercised through the small stub convention at the top of
`tests/test_agent_runtime_tools.py`. `tests/test_agent_fuzz.py` is a seeded
property/fuzz suite over the untrusted-input boundaries — it uses only the
standard library, and its fixed seed makes any failure reproducible.

`tests/qgis_smoke.py` is the real-QGIS harness: catalog discovery, native
Processing execution, progressless task cancellation, atomic result ownership,
Qt widget construction, `.model3` round-tripping, and the full agent
proposal/approval/run/undo path. The distributed plugin supports QGIS 3.44 LTR
and QGIS 4. Older QGIS 3 releases are not covered by the current compatibility
guarantee.
`tests/qgis_modeler_catalog_matrix.py` separately walks every live Processing
algorithm without executing arbitrary side effects, constructs its typed node,
and verifies the complete port schema after JSON round-trip.
Run the harness under both, each with its own throwaway profile:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
$env:QGIS_CUSTOM_CONFIG_PATH = "$env:TEMP\smoke_profile"
& C:\OSGeo4W\bin\python-qgis.bat planx_smartmodeler\tests\qgis_smoke.py      # QGIS 4
& C:\OSGeo4W\bin\python-qgis-ltr.bat planx_smartmodeler\tests\qgis_smoke.py  # QGIS 3 LTR
```

Pass a script *file*; a multi-line `-c` argument fails silently under those
launchers.

## Architecture

```text
gui/                 Qt window, canvas, accessible graph outline, palette,
                     inspector, model properties, help, and dialogs
gui/agent_dock.py    Agent Workspace panel: the only place a human click
                     turns a proposal into an action
core/graph_model.py  Pure-Python typed DAG and validation
core/algorithm_catalog.py
                     Live QGIS Processing registry bridge
core/execution_engine.py
                     Cancellable snapshot execution, structured reports, and
                     main-thread atomic result loading
core/model3_serializer.py
                     SmartModeler JSON and native QGIS model bridge
core/document_codec.py
                     Bounded V3 JSON schema, typed values and V2 migration
core/proposal_engine.py
                     Ranked live-port next-step recommendations
core/micro_packages.py
                     Versioned workflow package schema and graph builder
core/ai_*.py         Provider profiles, network client and graph validator
core/agent/          Agent Workspace core, split by trust:
                       pure, QGIS-free, unit-tested security logic
                       (contracts, protocol, proposals, run_planner,
                       run_state, safe_algorithm_policy, pending_action,
                       plugin_capabilities, action_risk, action_ledger)
                       and thin QGIS adapters that may only *narrow* what
                       the pure layer permits (runtime_tools,
                       runtime_proposals, runtime_apply, run_coordinator)
agent_context/       Auditable Markdown context and guardrails for the agent
ai_context/          Auditable Markdown context and guardrails for the planner
resources/           Shipped micro-package workflow schemas
tests/               Pure unit tests, seeded fuzz suite, real-QGIS smoke harness
docs/                Versioned V1.0 delivery and acceptance plan
```

The split above is the design, not a filing convention: every security decision
lives in a module that imports no QGIS, so it can be unit-tested and reasoned
about directly, and the QGIS-side adapter is only allowed to make the outcome
*more* restrictive — never less.

## License

GPL-3.0. Developed by Yusuf Eminoglu as part of the PlanX QGIS Plugin Ecosystem.
