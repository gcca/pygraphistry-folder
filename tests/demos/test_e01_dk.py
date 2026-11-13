from contextlib import redirect_stdout
from importlib import import_module
import io
import unittest

e01_dk = import_module("demos.e01-dk")
e01_pd = import_module("demos.e01-pd")


def captured(main):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        main()
    return buffer.getvalue()


class DuckDBDemoTest(unittest.TestCase):
    def test_build_graph(self):
        con = e01_dk.build_graph()
        self.assertEqual(con.execute("SELECT count(*) FROM nodes").fetchall()[0][0], 5)
        self.assertEqual(con.execute("SELECT count(*) FROM edges").fetchall()[0][0], 5)

    def test_alice_one_hop(self):
        nodes, edges = e01_dk.one_hop(e01_dk.build_graph(), "Alice")
        self.assertEqual(list(nodes["id"]), ["Alice", "Bob", "Carol"])
        self.assertEqual(list(zip(edges["src"], edges["dst"])), [("Alice", "Bob"), ("Alice", "Carol")])

    def test_output_matches_pandas_demo(self):
        self.assertEqual(captured(e01_dk.main), captured(e01_pd.main))


if __name__ == "__main__":
    unittest.main()
