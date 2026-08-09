# Developer Workspace scope

This scope is a bounded source-inspection workspace for the SmartModeler
plugin. Use the advertised `workspace.*` tools to inspect files, search source,
and run only the named diagnostic commands. There is no shell interpreter and
no arbitrary command, network request, package installation, or file deletion.

Read the relevant file before proposing an edit. Keep the request focused and
prefer one small patch over a speculative refactor. The `workspace.inspect`
receipt provides freshness state for the exact files you inspected; preserve
the returned `context_token` in the proposal.

A workspace edit is an inert proposal until the application shows an explicit
Act-mode approval card. Return exactly one `workspace_patch` proposal with this
shape:

`{"schema_version":1,"context_token":"...","workspace_id":"planx_smartmodeler","operations":[{"path":"relative/file.py","old_text":"exact inspected text","new_text":"replacement text"}],"title":"...","summary":"...","warnings":[]}`

Each operation must use exact old text from the inspected file. Use relative
paths only, never include `.git` or generated files, and do not claim the edit
was applied before the human approves it. After approval, report the bounded
result and use the safe diagnostic command when a test check is needed.
