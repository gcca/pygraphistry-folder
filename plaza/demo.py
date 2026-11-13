import argparse

import graphistry
from graphistry import e_forward, e_reverse, n
import pandas as pd

from plaza.data import DEFAULT_DATA_DIR, FORMATS, load_tables


KINDS = {
    "brands": "brand", "stores": "store", "locations": "location",
    "categories": "category", "subcategories": "subcategory", "events": "event",
    "event_categories": "event_category", "event_venues": "venue",
}
RELATIONSHIPS = (
    ("stores", "brand_id", "brand", "id", "store", "has_store"),
    ("stores", "id", "store", "location_id", "location", "located_in"),
    ("brands", "id", "brand", "subcategory_id", "subcategory", "in_subcategory"),
    ("subcategories", "id", "subcategory", "category_id", "category", "in_category"),
    ("events", "id", "event", "venue_id", "venue", "held_at"),
    ("events", "id", "event", "category_id", "event_category", "for_audience"),
    ("event_venues", "id", "venue", "location_id", "location", "matches_location"),
)


def build_graph(tables):
    brands = tables["brands"].set_index("id")["name"]
    locations = tables["locations"].set_index("id")["name"]
    frames = []
    for table, kind in KINDS.items():
        frame = tables[table].copy()
        frame["source_id"] = frame["id"].astype("string")
        frame["id"] = kind + ":" + frame["source_id"]
        frame["kind"] = kind
        if kind == "store":
            frame["name"] = frame["brand_id"].map(brands) + " — " + frame["location_id"].map(locations)
        elif kind == "event":
            frame["name"] = frame["title"]
        elif kind in ("venue", "event_category"):
            frame["source_name"] = frame["name"]
            frame["name"] = frame["display_name"].fillna(frame["name"])
        frames.append(frame)
    nodes = pd.concat(frames, ignore_index=True, sort=False)
    edges = []
    for table, source, source_kind, target, target_kind, relationship in RELATIONSHIPS:
        pairs = tables[table][[source, target]].dropna()
        for source_id, target_id in pairs.itertuples(index=False, name=None):
            edges.append({"src": f"{source_kind}:{source_id}", "dst": f"{target_kind}:{target_id}", "relationship": relationship})
    edges = pd.DataFrame(edges, columns=["src", "dst", "relationship"]).astype("string")
    return graphistry.nodes(nodes, "id").edges(edges, "src", "dst").bind(point_title="name")


def explore_graph(graph):
    return {
        "Brands, stores, and directory zones": graph.gfql([
            n({"kind": "brand"}), e_forward({"relationship": "has_store"}),
            n({"kind": "store"}), e_forward({"relationship": "located_in"}), n({"kind": "location"}),
        ], engine="pandas"),
        "Events and their supplied venues": graph.gfql([
            n({"kind": "event"}), e_forward({"relationship": "held_at"}), n({"kind": "venue"}),
        ], engine="pandas"),
        "Events and brands sharing an exactly matched directory zone": graph.gfql([
            n({"kind": "event"}), e_forward({"relationship": "held_at"}), n({"kind": "venue"}),
            e_forward({"relationship": "matches_location"}), n({"kind": "location"}),
            e_reverse({"relationship": "located_in"}), n({"kind": "store"}),
            e_reverse({"relationship": "has_store"}), n({"kind": "brand"}),
        ], engine="pandas"),
    }


def print_result(title, graph):
    print(f"\n{title}: {len(graph._nodes)} nodes, {len(graph._edges)} edges")
    if graph._edges.empty:
        print("No matching relationships in this snapshot.")
        return
    names = graph._nodes.set_index("id")["name"]
    edges = graph._edges[["src", "relationship", "dst"]].copy()
    edges["src"] = edges["src"].map(names)
    edges["dst"] = edges["dst"].map(names)
    sample = edges.sort_values(["relationship", "src", "dst"]).groupby("relationship", sort=False).head(3)
    print(sample.to_string(index=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=FORMATS, default="parquet")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    args = parser.parse_args()
    try:
        tables = load_tables(args.data_dir, args.format)
        graph = build_graph(tables)
        results = explore_graph(graph)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f"Could not load the graph: {error}\nPrepare the dataset with: pdm run plaza-prepare\n")
    print(f"plazacc: {len(graph._nodes)} nodes, {len(graph._edges)} edges")
    print("Entities:")
    print(graph._nodes.groupby("kind").size().to_string())
    print("\nRelationships:")
    print(graph._edges.groupby("relationship").size().to_string())
    print(f"\nEvents without a supplied venue: {tables['events']['venue_id'].isna().sum()}")
    print(f"Venues without an exact directory-zone match: {tables['event_venues']['location_id'].isna().sum()}")
    print("\nEvent sample (UTC):")
    events = tables["events"].sort_values(["start_time", "id"])[["title", "start_time", "end_time"]].head(5)
    print(events.to_string(index=False) if len(events) else "No events in this snapshot.")
    for title, result in results.items():
        print_result(title, result)


if __name__ == "__main__":
    main()
