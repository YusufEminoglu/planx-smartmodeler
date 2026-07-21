# SmartModeler GIS — Next-Generation QGIS 4 Model Designer

[![QGIS Version](https://img.shields.io/badge/QGIS-4.0%2B-brightgreen.svg)](https://qgis.org)
[![License](https://img.shields.io/badge/License-GPL%20v3-blue.svg)](LICENSE)
[![Release](https://img.shields.io/badge/Release-v0.1.0-orange.svg)](https://github.com/YusufEminoglu/planx-smartmodeler/releases)
[![Python](https://img.shields.io/badge/Python-3.12%2B-yellow.svg)](https://python.org)

> **SmartModeler GIS** is an intuitive, modern visual node-graph studio designed to replace and elevate the native QGIS Graphical Modeler experience in **QGIS 4.0+**. Featuring interactive node widgets, context-aware smart proposals, micro-package presets, and 100% bidirectional `.model3` compatibility.

---

## 🌟 Why SmartModeler GIS?

Native QGIS Graphical Modeler is a powerful batch DAG tool, but its user interface has remained static for years. **SmartModeler GIS** redesigns the entire visual workflow experience from the ground up for QGIS 4:

| Feature | Native QGIS Modeler | SmartModeler GIS |
| :--- | :--- | :--- |
| **Canvas UI & Performance** | Legacy QGraphicsView layout | **Hardware-Accelerated Qt6 Canvas** with smooth Bezier cables & dark theme |
| **Smart Recommendations** | Manual search in processing tree | **Smart Proposal Bar** offering instant contextual algorithm suggestions |
| **Interactive Controls** | Static parameter dialogs | **Embedded Inline Widgets** (Sliders, Toggles, Color Pickers) |
| **Workflow Starter Templates** | Empty canvas start | **Micro-Package Presets** (Isochrones, 3D Massing, MCDA Suitability) |
| **Live Wire Inspection** | Must run whole model to see output | **Live Wire Data Inspector** showing intermediate feature counts & fields |
| **QGIS Compatibility** | Standard `.model3` | **100% Bidirectional Export & Import** with standard QGIS `.model3` |

---

## 🚀 Key Highlights

1. **Smart Next-Step Proposals:**
   - As you connect node ports or select sockets, the top tip bar dynamically offers recommended next steps (e.g. connecting a Vector output suggests *Buffer*, *Clip*, *Extract by Attribute*, or *Centroids*).

2. **Micro-Package Presets:**
   - Double-click starter templates in the sidebar palette to instantly instantiate complete multi-node workflows (e.g., *15-Minute Urban Isochrone Grid*, *3D Massing Extrusion & Roof*, *MCDA Land Suitability Overlay*).

3. **Color-Coded Typed Sockets:**
   - Clear visual differentiation between socket data types:
     - 🟢 **Vector Layer**
     - 🔵 **Raster Surface**
     - 🟠 **Numeric Parameter**
     - 🟣 **String / Expression**
     - ⚪ **Field Column**

4. **Directed Acyclic Graph (DAG) Engine:**
   - Real-time cycle detection preventing infinite loops.
   - Topological sorting engine for sequential or multi-threaded background evaluation via `QgsTask`.

---

## 🏗 System Architecture

```text
+-----------------------------------------------------------------------+
|                    SmartModeler Window (Qt6 UI)                       |
|                                                                       |
|  +-----------------------------------------------------------------+  |
|  |                 Smart Proposal Bar (Auto-Tips)                  |  |
|  +-----------------------------------------------------------------+  |
|                                                                       |
|  +---------------+  +---------------------------+  +---------------+  |
|  | Node Palette  |  |   Qt6 Canvas View         |  | Live Wire     |  |
|  |  & Presets    |  |  (QGraphicsScene / View)  |  | Inspector     |  |
|  |               |  |                           |  |               |  |
|  | [Buffer]      |  |   [Input] -> [Buffer]     |  | Node ID: #102 |  |
|  | [Clip]        |  |                 |         |  | Features: 540 |  |
|  | [3D Extrude]  |  |                 v         |  | Params: d=800 |  |
|  | [Presets]     |  |              [Output]     |  |               |  |
|  +---------------+  +---------------------------+  +---------------+  |
+-----------------------------------+-----------------------------------+
                                    |
                    +---------------+---------------+
                    |  DAG Graph & Model3 Engine    |
                    |  (Topological Sort / Export)  |
                    +-------------------------------+
```

---

## 💻 Installation & Requirements

### System Requirements
- **QGIS Minimum Version:** `4.0` (PyQt6 / Qt6, Python 3.12+)
- **OS Support:** Windows, macOS, Linux

### Quick Installation
1. Download `planx_smartmodeler.zip` from the latest [GitHub Release](https://github.com/YusufEminoglu/planx-smartmodeler/releases).
2. Open QGIS 4 $\rightarrow$ Menu `Plugins` $\rightarrow$ `Manage and Install Plugins...` $\rightarrow$ `Install from ZIP`.
3. Select `planx_smartmodeler.zip` and click **Install Plugin**.
4. Access **SmartModeler GIS** from the `Vector` menu or toolbar icon.

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
├── icons/                    # High-res action and node icons
├── core/                     # Graph Engine, .model3 bridge & Smart Proposals
│   ├── graph_model.py        # DAG model, topological sorting & cycle check
│   ├── proposal_engine.py    # Context-aware auto-suggestion tip engine
│   └── model3_serializer.py # Bidirectional .model3 XML / JSON serializer
└── gui/                      # Qt6 Hardware-Accelerated Viewport & Widgets
    ├── canvas_view.py        # QGraphicsView viewport with zoom/pan
    ├── canvas_scene.py       # QGraphicsScene node graph manager
    ├── node_graphics_item.py # Sleek node card item rendering
    ├── port_graphics_item.py # Socket pins with hover feedback
    ├── connection_graphics_item.py # Bezier curve connection cables
    ├── smart_proposal_bar.py # Top proposal recommendation bar
    ├── node_palette_widget.py# Sidebar algorithm & preset browser
    └── wire_inspector_widget.py # Intermediate data inspection panel
```

---

## 📄 License & Attribution

Distributed under the **GNU General Public License v3.0** (`GPL-3`).  
Developed by **Yusuf Eminoğlu** as part of the **PlanX QGIS Plugin Ecosystem**. Feedback and contributions from educational workflows at Dokuz Eylul University, Department of City and Regional Planning.
