# Building a Workflow Studio graph

In the **Current model** scope the only proposals that exist are `model_patch`
and `model_run`. A `processing_run` or a `layer_style` is rejected here, so a
request phrased as a task ("buffer the roads and dissolve overlaps") must be
answered as a *graph edit*, not as a run.

Nothing executes in this scope. A workflow names algorithms, parameters and the
connections between them; the user chooses input layers later in Run setup, so
**do not bind input layers** and do not ask which layer to use.

Before adding a node, confirm the algorithm with `processing.resolve` (or
`processing.search` then `processing.describe`) and use only ids and parameter
names it reports. Call `model.describe` once for the current graph and echo its
`context_token`.

## `model_patch` — the exact payload

The envelope keys are exactly `schema_version`, `context_token`, `title`,
`summary`, `operations`, `warnings`. There is **no** `nodes` key and **no**
`connections` key: every change is one entry in `operations`.

```json
{
  "schema_version":1,
  "context_token":"<model.describe token>",
  "title":"Slope suitability workflow",
  "summary":"Compute slope from the DEM and reclassify it into bands.",
  "operations":[
    {"op":"add_node","node_id":"slope1","algorithm_id":"native:slope",
      "title":"Slope","parameters":[{"name":"Z_FACTOR","value":1}]},
    {"op":"add_node","node_id":"rc1","algorithm_id":"native:reclassifybytable",
      "title":"Reclassify","parameters":[]},
    {"op":"connect","from_node":"slope1","from_output":"OUTPUT",
      "to_node":"rc1","to_input":"INPUT_RASTER"}
  ],
  "warnings":[]
}
```

Every `add_node` requires **all four** of `node_id`, `algorithm_id`, `title`
and `parameters`. `parameters` is an **array of objects**, each
`{"name":"PARAM","value":<value>}` — never an object keyed by parameter name,
and never omitted. Use `[]` when a node needs no parameter set.

Other exact operations:

- `{"op":"set_parameter","node_id":"n","name":"P","value":1}`
- `{"op":"rename_node","node_id":"n","title":"Title"}`
- `{"op":"connect","from_node":"a","from_output":"OUTPUT","to_node":"b","to_input":"INPUT"}`
- `{"op":"disconnect","edge_id":"id"}`
- `{"op":"remove_node","node_id":"n"}`
- `{"op":"set_model_metadata","name":"Name","description":"..."}`

## Replacing versus improving

"Replace the workflow" means remove the nodes that are not part of the
requested result with `remove_node`, then add the new ones. "Improve the
workflow" means leave every unrelated node, parameter and connection alone.

If the request names no recognisable operation, say so and ask for the
algorithm — do not resolve the same guess repeatedly.
