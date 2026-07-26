# Iterative workflow editing

When a current workflow baseline is supplied, treat the request as an edit turn,
not a new workflow request. Return the complete updated graph in the normal output
contract.

1. Preserve existing node ids so user configuration and graph history remain stable.
2. Preserve unrelated nodes, literal parameters, layer ids, and connections.
3. Add, remove, reconnect, rename, or reconfigure only what the user asks for or
   what is strictly necessary to keep the graph executable.
4. Reuse an existing suitable node instead of creating a duplicate.
5. Keep already configured runtime inputs unless they are incompatible with the
   requested change.
6. Summarize the actual edit in `summary`; do not describe the whole workflow as
   if it were newly created.
7. A request to simplify or remove a step may intentionally delete nodes, but
   unrelated branches must remain intact.
8. Existing parameter values are replaced by
   `__SMARTMODELER_RETAIN_LOCAL_VALUE__` before the workflow leaves QGIS. Return
   that token unchanged for every value that should be preserved. It is resolved
   locally after validation and is invalid on new or replaced nodes.
9. Omitting an existing parameter also preserves its local value. Use an explicit
   non-token value only when the requested edit requires changing that parameter.
