import graphistry
import pandas as pd
from graphistry import e_forward, n


def build_graph():
    nodes = pd.DataFrame({"id": ["Alice", "Bob", "Carol", "Dave", "Eve"]})
    edges = pd.DataFrame(
        {
            "src": ["Alice", "Alice", "Bob", "Carol", "Dave"],
            "dst": ["Bob", "Carol", "Dave", "Dave", "Eve"],
        }
    )
    return graphistry.nodes(nodes, "id").edges(edges, "src", "dst")


def main():
    graph = build_graph()
    result = graph.gfql([n({"id": "Alice"}), e_forward(), n()], engine="pandas")

    print(f"Graph: {len(graph._nodes)} nodes, {len(graph._edges)} edges")
    print("Query: Alice's outgoing relationships (one hop)")
    print("\nNodes in matching paths (including Alice):")
    print(result._nodes[["id"]].sort_values("id").to_string(index=False))
    print("\nMatching relationships:")
    print(result._edges[["src", "dst"]].sort_values(["src", "dst"]).to_string(index=False))


if __name__ == "__main__":
    main()
