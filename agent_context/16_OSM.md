# OSM acquisition

Use Processing, not manual UI instructions. For roads, buildings and trees
together, search once for `zero2agentosm:download_preset`, describe it, choose
the live enum entry named **Urban context — roads, buildings & trees**, bind
the current canvas with `{"map_extent":true}`, and propose one
`processing_run`. It returns temporary point, line and polygon outputs.

If 02Agent OSM Downloader is unavailable, use SmartModeler's built-in reviewed
current-extent algorithms by geometry:
`smartmodeler:osm_download_points`, `smartmodeler:osm_download_lines`, or
`smartmodeler:osm_download_polygons`. Bind only plain `KEY`/optional `VALUE`
OSM tags and `EXTENT`; never invent an endpoint, file, raw Overpass query or
credential. A single-tag fallback may require separate approved runs for
different geometry families; state that limitation honestly.
