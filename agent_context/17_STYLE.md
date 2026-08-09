# Layer styling and classification

Classifying a layer is styling, not Processing. There is no Jenks algorithm to
search for: use a `layer_style` proposal with a `graduated` renderer. Never
answer that classification is unavailable because no Processing algorithm
matches.

Steps for a classification or symbology request:

1. Resolve the target with `layer.list`, and confirm the field with
   `layer.describe`. A graduated renderer needs a **numeric** field; a
   categorized renderer does not.
2. Call `layer.style` on that layer. It is a read-only inspection and its
   `context_token` is required by the proposal.
3. Return one `layer_style` proposal.

`renderer.family` is `single_symbol`, `categorized`, `graduated`, or `keep`.
`categorized` and `graduated` require `field`; `single_symbol` and `keep` do
not.

For `graduated`, `renderer.method` chooses how the classes are cut:

- `natural_breaks` — Jenks natural breaks; the default choice for an uneven
  distribution such as building area or population.
- `quantile` — equal feature counts per class.
- `equal_interval` — equal value ranges. This is the default when `method` is
  omitted.

`method` is optional and only meaningful for `graduated`. Ask for the number of
classes only when the user has not implied one; otherwise use a sensible 5.

Palette colours are exact `#RRGGBB` or `#RRGGBBAA` strings and are used as the
gradient ends for `graduated` and per-class for `categorized`. Opacity is
between 0.0 and 1.0.

Labels are part of the same proposal: `{"enabled":true,"field":"name"}` turns
on labelling with that field.
