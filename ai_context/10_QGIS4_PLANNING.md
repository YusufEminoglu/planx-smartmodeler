# QGIS 4 workflow planning rules

1. Use only algorithm ids present in the runtime algorithm catalog.
2. Use exact, case-sensitive input and output port ids from that catalog.
3. Connect compatible data types. A vector output cannot feed a raster input.
4. Keep the graph acyclic and give every node a unique short ASCII id.
5. Put literal values in `parameters`; put data flow in `edges`.
6. Do not invent layer ids. Use an id from runtime project context or leave the
   corresponding input unconfigured and explain it in `warnings`.
7. Call out CRS, distance-unit, resolution, invalid-geometry, and field-name
   assumptions when they affect correctness.
8. Never overwrite an input dataset. Destination outputs should remain temporary
   unless the user explicitly provides a safe output destination.
9. Do not add decorative or non-executable nodes.
10. Prefer native QGIS algorithms when equivalent choices exist.
11. Required project layers, multi-layer collections, files, extents, CRS values,
    and other runtime-only inputs may be left empty for SmartModeler's guided
    setup. Never replace them with guessed paths, ids, or placeholder strings.
