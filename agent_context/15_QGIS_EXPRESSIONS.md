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
   A measurement is a decimal: `$area`, `$length`, `$perimeter`, a ratio or a
   density must use the live **Decimal (double)** option, never the integer
   one. An integer area silently rounds every value, so a threshold such as
   "400 m² and under" then selects the wrong buildings. Bind the type by label
   so the option cannot be miscounted.
4. Return the validated `processing_run` proposal. Never claim that Field
   Calculator is manual-only when its live description is runnable.

## `$area` is only metres in a CRS that measures metres

`$area`, `$length` and `$perimeter` are computed in the **layer's own CRS**,
and two families give a wrong number without any warning:

- a **geographic** CRS (EPSG:4326 and friends) measures degrees, so the result
  is not an area at all;
- a **Mercator** CRS — EPSG:3857, which is what every OSM/XYZ download hands
  over — reports metres inflated by `1/cos²(latitude)`. That is **1.76× at 41°
  north**: a real 324 m² building measures 569 m², so "smaller than 400 m²"
  silently discards it, and in a district of ordinary 250–400 m² footprints the
  filter returns *nothing at all*.

`layer.list` and `layer.describe` report `area_safe_crs` for exactly this.
When it is `false`, propose `native:reprojectlayer` to a local metric CRS
(a UTM zone, or EPSG:5254 for Türkiye) **first**, then calculate on the result.
A measure bound to a layer whose CRS is not area-safe is rejected by the run
planner, so proposing one anyway costs the user a turn.

## Recalculating a field never changes its type

`native:fieldcalculator` keeps the **existing** type when `FIELD_NAME` already
exists on the input: the `FIELD_TYPE` you bind is ignored, the run reports
success, and the field is exactly what it was. Converting `alan_m2` from text
to integer by recalculating `alan_m2` therefore does nothing at all, while
looking like it worked. Always write a conversion to a **new** field name.

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
