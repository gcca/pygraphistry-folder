from importlib import import_module
import unittest

from graphistry import e_forward, n

build_graph = import_module("demos.e01-pd").build_graph


class DemoTest(unittest.TestCase):
    def test_build_graph(self):
        graph = build_graph()
        self.assertEqual(len(graph._nodes), 5)
        self.assertEqual(len(graph._edges), 5)

    def test_alice_one_hop(self):
        result = build_graph().gfql([n({"id": "Alice"}), e_forward(), n()], engine="pandas")
        self.assertEqual(sorted(result._nodes["id"]), ["Alice", "Bob", "Carol"])
        self.assertEqual(
            sorted(zip(result._edges["src"], result._edges["dst"])),
            [("Alice", "Bob"), ("Alice", "Carol")],
        )


if __name__ == "__main__":
    unittest.main()
