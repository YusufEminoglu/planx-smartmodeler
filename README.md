# SmartModeler GIS — Next-Generation QGIS Graphical Modeler

> **SmartModeler GIS** is a modern, intuitive, visual node-graph modeler designed to replace and elevate the native QGIS Graphical Modeler experience in **QGIS 4.0+**.

---

## 🌟 Key Features

- **Modern Node-Graph Editor:** Hardware-accelerated Qt6 `QGraphicsView` canvas with dark/light themes, smooth Bezier connections, and zoom/pan.
- **Smart Next-Step Proposals:** Context-aware tip bar recommending the most relevant next processing algorithms as you connect nodes or select sockets.
- **Interactive Inline Widgets:** Sliders, dropdowns, toggles, and color pickers embedded directly inside node cards for instant tweaking.
- **Micro-Package Presets:** One-click workflow templates (Isochrone Analysis, 3D Building Extrusion, Land Suitability, Parcel Buffer & Clip).
- **Live Data Wire Inspection:** Hover or click any cable to view live feature counts, sample attributes, and intermediate geometry bounding boxes.
- **Native QGIS 4 .model3 Invariant:** 100% bidirectional import/export with QGIS native `QgsProcessingModelAlgorithm` model files.

---

## 🚀 Installation & Requirements

- **QGIS Minimum Version:** `4.0` (Requires PyQt6 / Qt6, Python 3.12+)
- **Dependencies:** Standard QGIS 4 Python environment (`qgis.core`, `qgis.gui`, `qgis.PyQt`).

---

## 📁 Repository Structure

```text
planx_smartmodeler/
├── __init__.py               # Plugin factory entry point
├── metadata.txt              # QGIS 4 plugin manifest
├── main_plugin.py            # Main plugin lifecycle & toolbar actions
├── README.md                 # Technical documentation
├── CHANGELOG.md              # Sürüm geçmişi
├── LICENSE                   # GPL-3
├── icons/                    # Action and node icons
├── gui/                      # Qt6 / PyQt6 Canvas & UI components
└── core/                     # DAG Engine, .model3 bridge & Smart Proposal engine
```

---

## 📄 License

Distributed under the GNU General Public License v3.0 (`GPL-3`).
Copyright (c) 2026 Yusuf Eminoğlu.
