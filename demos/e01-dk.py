import duckdb


def build_graph():
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE nodes AS FROM (VALUES ('Alice'), ('Bob'), ('Carol'), ('Dave'), ('Eve')) t(id)"
    )
    con.execute(
        """
        CREATE TABLE edges AS FROM (VALUES
            ('Alice', 'Bob'), ('Alice', 'Carol'), ('Bob', 'Dave'), ('Carol', 'Dave'), ('Dave', 'Eve')
        ) t(src, dst)
        """
    )
    return con


def one_hop(con, source):
    nodes = con.execute(
        "SELECT id FROM nodes WHERE id = ? OR id IN (SELECT dst FROM edges WHERE src = ?) ORDER BY id",
        [source, source],
    ).df()
    edges = con.execute(
        "SELECT src, dst FROM edges WHERE src = ? ORDER BY src, dst",
        [source],
    ).df()
    return nodes, edges


def main():
    con = build_graph()
    node_count = con.execute("SELECT count(*) FROM nodes").fetchall()[0][0]
    edge_count = con.execute("SELECT count(*) FROM edges").fetchall()[0][0]
    nodes, edges = one_hop(con, "Alice")

    print(f"Graph: {node_count} nodes, {edge_count} edges")
    print("Query: Alice's outgoing relationships (one hop)")
    print("\nNodes in matching paths (including Alice):")
    print(nodes.to_string(index=False))
    print("\nMatching relationships:")
    print(edges.to_string(index=False))


if __name__ == "__main__":
    main()
