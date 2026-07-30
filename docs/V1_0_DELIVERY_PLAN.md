# SmartModeler GIS V1.0 delivery plan

Date: 2026-07-26

This is the release program from the audited v0.5.3 baseline to a production
V1.0. Each milestone is a separately validated, committed, tagged, and pushed
release. A milestone is not accepted from code review alone.

## Product boundary

V1.0 is a QGIS 4 visual Processing studio and a human-supervised GIS assistant.
It builds typed workflows, preserves editable documents, exchanges native QGIS
models, executes work with explicit ownership and cancellation, and keeps all
AI/provider authority below application-owned validation and user approval.

V1.0 does not provide unattended autonomy, arbitrary algorithm execution,
feature-value inspection, arbitrary plugin invocation, shell execution, or
background approval. The post-baseline opt-in Power Mode adds reviewed SQL and
Python/PyQGIS proposals with full-source cards, explicit approval, stronger
confirmations, bounded subprocess execution, and no claim of sandboxing.

## Milestones

| Version | Scope | Exit criteria |
|---|---|---|
| v0.5.3 | Audited baseline | Existing candidate accepted on QGIS 4 and 3.44 LTR; release ZIP, tag, and push complete |
| v0.6.0 | Security and document safety | Metadata-only provider context; deny-by-default AI graph policy; live signature hardening; edit-aware Agent Undo; general undo/redo, dirty state, guarded New/Open/Close, atomic Save/Save As, crash recovery |
| v0.7.0 | Interchange | Versioned typed SmartModeler JSON; bounded fail-closed import; `.model3` parameter, dependency, multi-input, and output round-trip parity |
| v0.8.0 | Modeler UX | Ranked contextual proposals with reason/preview/auto-connect; schema-driven micro-packages; graph metadata and declared output controls |
| v0.9.0 | Runtime | Cancellable non-reentrant Studio execution; explicit result ownership; deterministic unload/close; structured failed/cancelled/partial outcomes |
| v0.10.0 | Accessibility | Keyboard-complete graph actions, focus order, accessible names/descriptions, contrast and screen-reader checks |
| v1.0.0-rc.1 | Release candidate | CI and local release gate; full license/metadata/docs audit; LF-only package; malformed-file and migration corpus; clean Hub security scan |
| v1.0.0 | General availability | QGIS 4 clean-profile ZIP install and manual workflow QA; QGIS 3.44 compatibility regressions; reproducible package manifest/hash; final tag, push, and release records |

## Mandatory gate for every milestone

1. Review the live diff for regressions, privacy claims, and trust-boundary drift.
2. Run the pure-Python unit, contract, seeded fuzz, and document-state suites.
3. Run Flake8 on every touched Python file with the repository line-length rule.
4. Compile every touched Python module.
5. Run the real-QGIS smoke harness on QGIS 4 and QGIS 3.44 LTR.
6. Run the adversarial security probe on both QGIS runtimes.
7. Run strict plugin validation and the Hub-equivalent security scan.
8. Build the Hub ZIP and verify root, manifest, file count, and SHA-256.
9. Update `CHANGELOG.md`, `metadata.txt`, and capability claims together.
10. Commit without AI co-author trailers, create the matching annotated tag,
    and push the branch and tag only after all gates pass.

## Final manual acceptance

- Install the built ZIP into a clean QGIS 4 profile.
- Create, edit, undo, redo, save, close, reopen, and recover a workflow.
- Import and export SmartModeler JSON, `.model3`, and Python output.
- Run and cancel representative vector, raster, multi-vector, and multi-raster
  workflows and verify exact result-layer ownership.
- Exercise offline and one connected provider profile without exposing local
  parameter values.
- Exercise Ask, Plan, Act, approval expiry, stale context, cancellation, Undo,
  unload, and shutdown paths.
- Verify keyboard-only operation and screen-reader labels.
- Rebuild the ZIP from the tagged commit and compare its manifest and hash.
