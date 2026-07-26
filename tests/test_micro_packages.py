import unittest

from planx_smartmodeler.core.graph_model import NodeDefinition, SocketType
from planx_smartmodeler.core.micro_packages import (
    MicroPackageCatalog,
    MicroPackageError,
)


class FakeCatalog:
    ALGORITHMS = {
        "smart:input_layer",
        "smart:raster_layer",
        "smart:number",
        "native:buffer",
        "native:extractbyexpression",
        "native:slope",
        "native:centroids",
        "native:clip",
    }

    @classmethod
    def algorithm_exists(cls, algorithm_id):
        return algorithm_id in cls.ALGORITHMS

    @classmethod
    def create_node(cls, algorithm_id, node_id=None, title=None):
        if not cls.algorithm_exists(algorithm_id):
            raise ValueError("Unavailable")
        node = NodeDefinition(
            node_id,
            title or algorithm_id,
            algorithm_id=algorithm_id,
        )
        if algorithm_id == "smart:input_layer":
            node.parameters["LAYER"] = ""
            node.add_output("OUTPUT", "Output", SocketType.VECTOR)
        elif algorithm_id == "smart:raster_layer":
            node.parameters["LAYER"] = ""
            node.add_output("OUTPUT", "Output", SocketType.RASTER)
        elif algorithm_id == "smart:number":
            node.parameters["VALUE"] = 0.0
            node.add_output("OUTPUT", "Output", SocketType.NUMBER)
        elif algorithm_id == "native:buffer":
            node.add_input("INPUT", "Input", SocketType.VECTOR, required=True)
            node.add_input("DISTANCE", "Distance", SocketType.NUMBER)
            node.add_output("OUTPUT", "Output", SocketType.VECTOR)
        elif algorithm_id == "native:extractbyexpression":
            node.add_input("INPUT", "Input", SocketType.VECTOR, required=True)
            node.add_input("EXPRESSION", "Expression", SocketType.STRING)
            node.add_output("OUTPUT", "Output", SocketType.VECTOR)
        elif algorithm_id == "native:slope":
            node.add_input("INPUT", "Input", SocketType.RASTER, required=True)
            node.add_output("OUTPUT", "Output", SocketType.RASTER)
        elif algorithm_id == "native:centroids":
            node.add_input("INPUT", "Input", SocketType.VECTOR, required=True)
            node.add_output("OUTPUT", "Output", SocketType.VECTOR)
        elif algorithm_id == "native:clip":
            node.add_input("INPUT", "Input", SocketType.VECTOR, required=True)
            node.add_input("OVERLAY", "Overlay", SocketType.VECTOR, required=True)
            node.add_output("OUTPUT", "Output", SocketType.VECTOR)
        return node


class MicroPackageCatalogTests(unittest.TestCase):
    def test_shipped_catalog_builds_every_available_graph(self):
        summaries = MicroPackageCatalog.available(FakeCatalog)
        self.assertEqual(len(summaries), 5)
        for summary in summaries:
            graph = MicroPackageCatalog.instantiate(
                summary.package_id, FakeCatalog
            )
            self.assertEqual(len(graph.nodes), summary.node_count)
            self.assertTrue(graph.outputs_declared)
            self.assertTrue(graph.outputs)
            self.assertEqual(
                len(graph.get_topological_order()), len(graph.nodes)
            )
            self.assertTrue(any(node.x for node in graph.nodes.values()))

    def test_unavailable_algorithm_hides_package_and_fails_instantiation(self):
        class MissingSlopeCatalog(FakeCatalog):
            ALGORITHMS = FakeCatalog.ALGORITHMS - {"native:slope"}

        available_ids = {
            summary.package_id
            for summary in MicroPackageCatalog.available(MissingSlopeCatalog)
        }
        self.assertNotIn("terrain_slope", available_ids)
        with self.assertRaisesRegex(MicroPackageError, "unavailable"):
            MicroPackageCatalog.instantiate(
                "terrain_slope", MissingSlopeCatalog
            )

    def test_schema_rejects_unknown_version_and_dangling_connection(self):
        data = {
            "schema_version": 999,
            "packages": [],
        }
        with self.assertRaisesRegex(MicroPackageError, "version"):
            MicroPackageCatalog._validate_catalog(data)

        package = {
            "id": "invalid",
            "name": "Invalid",
            "description": "",
            "tags": [],
            "nodes": [
                {
                    "id": "source",
                    "algorithm_id": "smart:number",
                    "title": "Source",
                    "parameters": {},
                }
            ],
            "connections": [
                {
                    "from_node": "source",
                    "from_port": "OUTPUT",
                    "to_node": "missing",
                    "to_port": "INPUT",
                }
            ],
            "outputs": [],
        }
        with self.assertRaisesRegex(MicroPackageError, "Dangling"):
            MicroPackageCatalog._validate_catalog(
                {"schema_version": 1, "packages": [package]}
            )


if __name__ == "__main__":
    unittest.main()
