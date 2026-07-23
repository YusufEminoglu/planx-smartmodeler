# SmartModeler GIS

[![QGIS](https://img.shields.io/badge/QGIS-4.0%2B-589632.svg)](https://qgis.org)
[![License](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](LICENSE)

SmartModeler GIS is a QGIS 4-only visual studio for building and running real QGIS Processing workflows. It combines a typed node canvas, live algorithm discovery, validated AI planning, native `.model3` interchange, and a focused dark Qt 6 interface.

## Current capabilities

- Discovers installed algorithms directly from the QGIS Processing registry.
- Builds typed, acyclic graphs and rejects incompatible or duplicate connections.
- Configures layers, multi-layer collections, files, extents, CRS values, and
  other parameters with native QGIS Processing controls.
- Guides Run and Validate through any required inputs that are still missing,
  while safely auto-binding the sole compatible project layer when unambiguous.
- Executes nodes in topological order through the QGIS Processing framework.
- Adds terminal vector and raster results to the current project.
- Imports and exports SmartModeler JSON and native QGIS `.model3` files.
- Offers contextual next-step proposals and executable starter workflows.
- Generates workflows through offline rules or a configured AI provider.
- Improves the current canvas over repeated AI turns while preserving unrelated
  nodes and parameters, previews the proposed graph changes, and provides one-step
  **Undo AI** recovery.
- Offers a separate **Agent Workspace** dock with bounded, read-only project,
  layer, symbology/labeling, Processing, model, and plugin inspections through
  a fail-closed policy engine, plus a bounded, provider-neutral **Agent Chat**
  conversation over **eleven** read-only tools using any configured non-offline
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
3. Connect compatible ports and double-click nodes to configure parameters.
4. Use **Validate** and then **Run Model**.
5. Optionally configure an AI profile and describe the workflow in the prompt bar.
6. Save a portable SmartModeler JSON file or export a QGIS `.model3` model.

## Requirements and installation

- QGIS 4.0 or newer
- No pip or external Python dependencies

Install the release ZIP through **Plugins > Manage and Install Plugins > Install from ZIP**. The ZIP root must be `planx_smartmodeler/`.

## Development checks

From the physical plugin monorepo root:

```powershell
python -m unittest discover -s planx_smartmodeler\tests -v
python packaging\validate_plugin.py planx_smartmodeler
python packaging\hub_security_scan.py planx_smartmodeler
.\packaging\Build-PluginZip.ps1 -PluginDir planx_smartmodeler
```

`tests/qgis_smoke.py` is an additional real-QGIS smoke harness for catalog discovery, a native Buffer execution, Qt widget construction, and native `.model3` round-tripping. On Windows, pass its path to `qgis_process` with forward slashes.

## Architecture

```text
gui/                 Qt 6 window, canvas, palette, inspector and dialogs
core/graph_model.py  Pure-Python typed DAG and validation
core/algorithm_catalog.py
                     Live QGIS Processing registry bridge
core/execution_engine.py
                     Sequential Processing execution and result loading
core/model3_serializer.py
                     SmartModeler JSON and native QGIS model bridge
core/ai_*.py         Provider profiles, network client and graph validator
ai_context/          Auditable Markdown context and guardrails
tests/               Pure unit tests and real-QGIS smoke harness
```

## License

GPL-3.0. Developed by Yusuf Eminoglu as part of the PlanX QGIS Plugin Ecosystem.
