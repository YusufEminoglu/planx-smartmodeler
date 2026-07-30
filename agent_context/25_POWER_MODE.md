# Power Mode

Power Mode is explicit high authority, not a safety sandbox.

- Inspect database connections or scripts before proposing them. Connection
  tokens are opaque; never ask for URIs, paths, usernames or passwords.
- `sql_run` contains exactly one complete statement. Classify SELECT/WITH as
  `select`, data changes as `write`, and schema/permission changes as `ddl`.
  Never claim validation or transaction support until the tool reports it.
- A trusted script is selected by `script_id` and exact `script_hash`; never
  rewrite its code.
- A generated `python_run` contains complete source. `subprocess` is preferred.
  Use `live` only when the user explicitly needs the current QGIS UI/session.
- Both Python modes provide `smartmodeler_parameters`,
  `smartmodeler_input_layer_ids`, `smartmodeler_input_layers`, and
  `smartmodeler_output_dir`. Add result layers to `QgsProject.instance()`.
  Subprocess mode snapshots selected vector inputs, initializes Processing,
  exports requested/new vector result layers, then returns them to the main
  project as temporary layers.
- Full SQL and Python may change or disclose data. State the concrete effect in
  the summary and warnings. The application owns every confirmation.
