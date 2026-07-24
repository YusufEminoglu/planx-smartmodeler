# SmartModeler GIS

[![QGIS](https://img.shields.io/badge/QGIS-4.0%2B-589632.svg)](https://qgis.org)
[![License](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](LICENSE)

SmartModeler GIS is a QGIS 4-only visual studio for building and running real QGIS Processing workflows. It combines a typed node canvas, live algorithm discovery, validated AI planning, native `.model3` interchange, and a focused dark Qt 6 interface.

## Current capabilities

- Discovers installed algorithms directly from the QGIS Processing registry.
- Builds typed, acyclic graphs and rejects incompatible or duplicate connections.
- Configures layers, multi-layer collections, files, extents, CRS values, and
  other parameters with native QGIS Processing controls.
- Opens **Run setup**: one sheet showing every step in run order, where each
  step's connected inputs come from, and every open input editable in place with
  the project's layers offered in a combo -- while safely auto-binding the sole
  compatible project layer when unambiguous.
- Executes nodes in topological order through the QGIS Processing framework.
- Adds terminal vector and raster results to the current project.
- Imports and exports SmartModeler JSON and native QGIS `.model3` files, and
  exports the workflow as a runnable QGIS Python algorithm. A workflow whose
  inputs are not bound yet still saves: each unbound required input becomes a
  model input, so the `.model3` opens in the QGIS Model Designer and asks for it.
- Offers contextual next-step proposals and executable starter workflows.
- Generates workflows through offline rules or a configured AI provider.
- Improves the current canvas over repeated AI turns while preserving unrelated
  nodes and parameters, previews the proposed graph changes, and provides one-step
  **Undo AI** recovery.
- Offers a separate **Agent Workspace** dock with bounded, read-only project,
  layer, symbology/labeling, Processing, model, and plugin inspections through
  a fail-closed policy engine, plus a bounded, provider-neutral **Agent Chat**
  conversation over **thirteen** read-only tools using any configured non-offline
  AI connection (OpenAI, Anthropic, Gemini, DeepSeek, Ollama, OpenAI-compatible,
  Azure OpenAI). Every provider turn is a strict, locally re-validated
  structured envelope; mode, scope, and every tool call's execution stay under
  application control, never provider control, and the tool set is
  metadata-only (no feature values, source URIs, style/label expressions,
  baseline model parameter values, or credentials). Quick inspections keep
  working with the `offline` profile, which is not treated as a language model.
- In Plan or Act mode the Agent Workspace can show four kinds of **validated
  proposals**: a model-workflow patch, a vector/raster symbology-and-labeling
  intent, a single reviewed Processing run, and a run of your current workflow.
  A proposal is inert data validated locally (a model patch only on a
  detached graph clone; a style proposal only against the live layer's fields).
  In **Plan** it stays review-only with a **Not applied** status. In **Act** it
  produces a single pending action on a read-only approval card, and it is
  applied only when **you explicitly click Apply** -- the AI never approves,
  applies, or undoes anything, and there is no Approve-all/remembered/background
  approval. Apply re-checks the live state and proposal integrity at the click,
  commits one atomic change (rolling back on any failure), and records the
  outcome in a bounded in-session action ledger.
- An approved **run** executes either one algorithm from a shipped, hardcoded
  list of twelve reviewed native algorithms -- and only while its live signature
  still matches the reviewed one -- or your current workflow, whose every step
  must independently pass the same check. There is no "run any algorithm" path,
  and neither the AI nor your prompt can extend the list. Runs show progress, can
  be **cancelled**, and always write to **temporary layers**: no file, folder,
  database, or network output can even be expressed. A failed or cancelled run
  adds no layer and leaves the project unchanged.
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
  unproved; a look-alike name is never presented as a confirmation. Running a
  plugin's algorithm is not available in this version, and the panel says so up
  front instead of letting you find out by failing.
- A chat session can carry a task across several steps: after an action finishes,
  a short sanitized note of what happened stays in the conversation so you can
  say "now style the result". The agent never continues on its own -- you ask
  each time -- and a session is capped at ten actions.
- There is still **no** plugin invocation, file/database/network output, or
  persistence from the Agent Workspace. `plugin.describe` reports only bounded
  installed plugin metadata; it never invokes or reads a plugin, and never
  fetches a URL. The dock works independently of the Workflow Studio.

## AI providers

The profile-based AI settings screen supports:

- OpenAI Responses API
- Anthropic Messages API
- Google Gemini API
- DeepSeek API (`deepseek-v4-flash` preset)
- Ollama
- OpenAI-compatible services and local runtimes
- Azure OpenAI
- SmartModeler Offline, which never sends a network request

Models, timeouts, endpoints where appropriate, project context, and algorithm-catalog limits are configurable per profile. API keys are never written to plugin JSON or ordinary `QgsSettings`. Session-only memory storage works without a password; optionally, the QGIS Authentication Database can encrypt the key across restarts. Its master password is a QGIS password—not the provider API key—and SmartModeler opens it only after the explicit **Unlock vault** action. Legacy plaintext settings are migrated and removed.

AI is a planner, not an execution authority. The provider receives Markdown instructions plus an optional metadata-only description of project layers and a bounded list of installed algorithms. Feature values are not included. Returned JSON must pass the shipped schema, installed-algorithm, parameter, socket-type, and DAG checks before it can modify the canvas. AI output cannot request Python, shell commands, downloads, filesystem changes, or arbitrary network actions.

The auditable instruction set lives in [`ai_context/`](ai_context/):

- `00_ROLE.md` defines the GIS planning role.
- `10_QGIS4_PLANNING.md` defines QGIS workflow practices.
- `15_ITERATIVE_EDITING.md` defines preservation rules for repeated AI edits.
- `20_GRAPH_CONTRACT.md` defines the exact graph response contract.
- `30_GUARDRAILS.md` defines trust boundaries and prohibited actions.

## Basic use

1. Open **Plugins > SmartModeler GIS**.
2. Add installed algorithms from the palette or choose a starter workflow.
3. Connect compatible ports, then double-click a node to configure it or open
   **Run setup** to review and fill in the whole workflow at once.
4. Use **Validate** and then **Run Model**.
5. Optionally configure an AI profile and describe the workflow in the prompt bar.
6. Save a portable SmartModeler JSON file, a QGIS `.model3` model, or a QGIS
   Python algorithm.

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

## Privacy

What leaves your machine, and only when you send a chat turn to a non-offline
profile: your message, the plugin's own instructions, and **bounded metadata** —
layer names, geometry types, CRS identifiers, field names and types, algorithm
names, and installed plugin names and versions.

What never leaves it: feature and attribute **values**, source paths, file names,
data source URIs and connection strings, style and label expressions, credentials
and API keys, and your project file path.

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

## Requirements and installation

- QGIS 4.0 or newer
- No pip or external Python dependencies

Install the release ZIP through **Plugins > Manage and Install Plugins > Install from ZIP**. The ZIP root must be `planx_smartmodeler/`.

## Development checks

From the physical plugin monorepo root:

```powershell
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

`tests/qgis_smoke.py` is the real-QGIS harness: catalog discovery, a native
Buffer execution, Qt widget construction, `.model3` round-tripping, and the full
agent proposal/approval/run/undo path. Run it under **both** supported runtimes,
each with its own throwaway profile:

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
gui/                 Qt 6 window, canvas, palette, inspector and dialogs
gui/agent_dock.py    Agent Workspace panel: the only place a human click
                     turns a proposal into an action
core/graph_model.py  Pure-Python typed DAG and validation
core/algorithm_catalog.py
                     Live QGIS Processing registry bridge
core/execution_engine.py
                     Sequential Processing execution and result loading
core/model3_serializer.py
                     SmartModeler JSON and native QGIS model bridge
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
tests/               Pure unit tests, seeded fuzz suite, real-QGIS smoke harness
```

The split above is the design, not a filing convention: every security decision
lives in a module that imports no QGIS, so it can be unit-tested and reasoned
about directly, and the QGIS-side adapter is only allowed to make the outcome
*more* restrictive — never less.

## License

GPL-3.0. Developed by Yusuf Eminoglu as part of the PlanX QGIS Plugin Ecosystem.
