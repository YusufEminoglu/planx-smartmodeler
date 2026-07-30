# QGIS expressions

QGIS expressions are a first-class GIS language, not Python and not SQL.
Use the user's expression exactly when it is already valid. Do not replace a
known expression with a manual-UI instruction.

For a request to add a calculated column to one vector layer:

1. Resolve the target with `layer.list` and inspect its fields once with
   `layer.describe` when field references are involved.
2. Search for **field calculator** once and describe
   `native:fieldcalculator`.
   If a function's signature or semantics are uncertain, call
   `expression.search` for that function; it returns the live QGIS built-in
   help. Do not search Processing for an expression function name.
3. Bind `INPUT`, `FIELD_NAME`, the live `FIELD_TYPE` enum, and `FORMULA` using
   `{"expression":"..."}`. Omit optional length/precision settings unless the
   user requested them.
4. Return the validated `processing_run` proposal. Never claim that Field
   Calculator is manual-only when its live description is runnable.

Core syntax:

- Numbers are unquoted: `15`, `3.5`.
- String literals use single quotes: `'residential'`.
- Field references use double quotes: `"height"` or `"building:levels"`.
- Geometry variables include `$geometry`, `$area`, `$length`, `$perimeter`,
  `$x` and `$y`.
- Operators include arithmetic, comparison, `AND`, `OR`, `NOT`, `IS NULL`,
  `LIKE`, `IN`, `CASE`, `||` and parentheses.
- Named arguments use `name:=value`; e.g.
  `clamp(min:=1, value:="floors", max:=15)`.
- `rand(min, max[, seed:=NULL])` returns an integer in the inclusive range.
  `rand(1, 15)` is therefore the direct formula for random floor counts 1–15.
- `$area` is the geometry area in the layer/Processing expression context.
  Use it directly after the user has chosen an appropriate projected CRS.
- Expressions may combine functions: `round($area, 2)`,
  `coalesce("height", 0)`, `CASE WHEN $area > 1000 THEN 'large' ELSE 'small' END`.

The application validates every formula with the live `QgsExpression` parser,
checks referenced fields against the bound input layer, and asks the live
Processing algorithm to validate all parameters before showing the Run card.
Unknown fields, malformed syntax, unavailable functions, custom Python
functions, dynamic `eval`, environment access, filesystem functions and
path/secret-like variables are rejected locally. Ordinary built-in QGIS math,
geometry, conversion, string, date/time, conditional, array/map, aggregate and
overlay functions remain available.

When a user supplies `rand(1,15)`, `$area`, a quoted field, or another complete
formula, do not search repeatedly for a special algorithm named after the
function. The algorithm is Field Calculator; the formula belongs in `FORMULA`.
