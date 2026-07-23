# Output contract

Return exactly one JSON object with these keys:

- `title`: concise workflow name.
- `summary`: one sentence describing the pipeline.
- `nodes`: array of node objects.
- `edges`: array of directed edge objects.
- `warnings`: array of short user-visible assumptions or required inputs.

Each node object must contain `id`, `algorithm_id`, `title`, and `parameters`.
`parameters` is an array of objects with exactly `name` and `value` keys. Values
may be strings, numbers, booleans, null, or arrays of strings.
Each edge must contain `from_node`, `from_output`, `to_node`, and `to_input`.
All five top-level keys are required. Do not wrap JSON in Markdown fences.

Before returning, internally verify: ids are unique; algorithms exist in the
catalog; ports exist; socket types match; every edge points forward in an acyclic
graph; and no secret or executable code appears in parameters.
