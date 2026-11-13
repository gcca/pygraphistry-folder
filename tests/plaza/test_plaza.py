import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from plaza.data import EVENTS_URL, FORMATS, fetch_events, load_tables, normalize_data, prepare, snapshot_events
from plaza.demo import build_graph, explore_graph


def catalog_fixture():
    location = {"id": 18, "name": "Plaza central, 1er nivel"}
    subcategory = {"id": 7, "name": "Café y cultura", "category": {"id": 5, "name": "Tiendas"}}
    return {
        "retrieved_at": "2026-11-12T01:50:18+00:00",
        "sources": {"brands": "https://example.test/brands/"},
        "locations": [location],
        "subcategories": [subcategory],
        "brands": [
            {
                "id": 81, "name": "Café, música y \"arte\"", "description": "Primera línea\nSegunda línea",
                "directoryId": 63, "isActive": True, "isNew": False, "subcategory": subcategory,
                "stores": [{"location": location}], "storePhoto": None, "phone": "", "website": "NA",
            },
            {
                "id": 82, "name": "Otra tienda", "description": "null", "directoryId": 64,
                "isActive": False, "isNew": True, "subcategory": subcategory, "stores": [], "phone": None,
            },
        ],
    }


def event_fixture(event_id=1048, venue_name="Plaza Central, 1er nivel"):
    return {
        "id": event_id, "title": "Música en vivo", "description": None,
        "start_time": "2026-11-13T16:00:00-05:00", "end_time": "2026-11-13T22:00:00Z",
        "image": "https://example.test/event.jpg",
        "category": {"id": 5, "name": "familiar", "display_name": "Para toda la familia"},
        "landmark": {"name": venue_name, "display_name": venue_name, "external_id": "LOC-REUSED"},
    }


def snapshot_fixture(events):
    return {
        "source": EVENTS_URL, "retrieved_at": "2026-11-13T13:00:00+00:00",
        "pages": [{"url": EVENTS_URL, "response": {"count": len(events), "next": None, "results": events}}],
    }


class PlazaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.catalog = self.root / "catalog.json"
        self.catalog.write_text(json.dumps(catalog_fixture(), ensure_ascii=False), encoding="utf-8")
        self.events_file = self.root / "events.json"
        self.events_file.write_text(json.dumps(snapshot_fixture([event_fixture()])), encoding="utf-8")
        self.destination = self.root / "dataset"

    def prepare_offline(self):
        with patch.object(socket.socket, "connect", side_effect=AssertionError("Network used")):
            return prepare(self.catalog, self.destination, self.events_file)

    def snapshot_bytes(self):
        return {str(path.relative_to(self.destination)): path.read_bytes() for path in self.destination.rglob("*") if path.is_file()}

    def test_all_formats_preserve_values_types_and_graphs_offline(self):
        manifest = self.prepare_offline()
        self.assertEqual(manifest["sources"]["catalog"]["retrieved_at"], catalog_fixture()["retrieved_at"])
        self.assertEqual((self.destination / "raw" / "catalog.json").read_bytes(), self.catalog.read_bytes())
        tables_by_format = {}
        graphs = []
        results = []
        with patch.object(socket.socket, "connect", side_effect=AssertionError("Network used")):
            for format in FORMATS:
                tables = load_tables(self.destination, format)
                tables_by_format[format] = tables
                graph = build_graph(tables)
                original_nodes = graph._nodes.copy(deep=True)
                original_edges = graph._edges.copy(deep=True)
                results.append(explore_graph(graph))
                pd.testing.assert_frame_equal(graph._nodes, original_nodes)
                pd.testing.assert_frame_equal(graph._edges, original_edges)
                graphs.append(graph)
        for tables in tables_by_format.values():
            for name, frame in tables.items():
                pd.testing.assert_frame_equal(frame, tables_by_format["parquet"][name])
            self.assertEqual(tables["brands"].loc[0, "phone"], "")
            self.assertTrue(pd.isna(tables["brands"].loc[1, "phone"]))
            self.assertEqual(tables["brands"].loc[0, "website"], "NA")
            self.assertEqual(tables["brands"].loc[1, "description"], "null")
            self.assertEqual(tables["brands"].loc[0, "name"], 'Café, música y "arte"')
            self.assertIn("\n", tables["brands"].loc[0, "description"])
            self.assertFalse(tables["brands"].loc[1, "is_active"])
            self.assertEqual(tables["events"].loc[0, "start_time"], "2026-11-13T21:00:00Z")
        for graph, queries in zip(graphs, results):
            pd.testing.assert_frame_equal(graph._nodes, graphs[0]._nodes)
            pd.testing.assert_frame_equal(graph._edges, graphs[0]._edges)
            for title, result in queries.items():
                pd.testing.assert_frame_equal(result._nodes, results[0][title]._nodes)
                pd.testing.assert_frame_equal(result._edges, results[0][title]._edges)
            shared = list(queries.values())[2]
            self.assertEqual(set(shared._nodes["kind"]), {"event", "venue", "location", "store", "brand"})
            self.assertTrue(graph._nodes["id"].is_unique)
            self.assertTrue(graph._edges["src"].isin(graph._nodes["id"]).all())
            self.assertTrue(graph._edges["dst"].isin(graph._nodes["id"]).all())

    def test_missing_venues_and_reused_external_ids_do_not_invent_links(self):
        first = event_fixture(1, "  PLAZA   CENTRAL, 1er nivel  ")
        second = event_fixture(2, "Terraza Universitaria, 2do nivel")
        second["landmark"]["name"] = "frente a wong 2do nivel"
        third = event_fixture(3)
        third["landmark"] = None
        tables = normalize_data(catalog_fixture(), [first, second, third])
        self.assertEqual(len(tables["events"]), 3)
        self.assertEqual(len(tables["event_venues"]), 2)
        self.assertEqual(tables["event_venues"]["location_id"].notna().sum(), 1)
        self.assertEqual(tables["event_venues"]["external_id"].nunique(), 1)
        self.assertTrue(pd.isna(tables["events"].loc[2, "venue_id"]))
        graph = build_graph(tables)
        self.assertIn("frente a wong 2do nivel", set(graph._nodes.loc[graph._nodes["kind"] == "venue", "source_name"]))
        shared = list(explore_graph(graph).values())[2]
        self.assertEqual(set(shared._nodes.loc[shared._nodes["kind"] == "event", "source_id"]), {"1"})
        self.assertIn("event:3", set(graph._nodes["id"]))
        self.assertNotIn("event:3", set(graph._edges.loc[graph._edges["relationship"] == "held_at", "src"]))

    def test_empty_event_feed_has_typed_empty_tables_in_every_format(self):
        self.events_file.write_text(json.dumps(snapshot_fixture([])), encoding="utf-8")
        self.prepare_offline()
        for format in FORMATS:
            tables = load_tables(self.destination, format)
            self.assertTrue(tables["events"].empty)
            self.assertTrue(tables["event_venues"].empty)
            graph = build_graph(tables)
            self.assertTrue(list(explore_graph(graph).values())[2]._edges.empty)

    def test_fetch_follows_pagination_and_retains_original_responses(self):
        first = {"count": 2, "next": "?page=2", "results": [event_fixture(1)]}
        second = {"count": 2, "next": None, "results": [event_fixture(2)]}
        fetch = Mock(side_effect=[first, second])
        snapshot = fetch_events(fetch)
        self.assertEqual([call.args[0] for call in fetch.call_args_list], [EVENTS_URL, EVENTS_URL + "?page=2"])
        self.assertEqual([event["id"] for event in snapshot_events(snapshot)], [1, 2])
        self.assertEqual(snapshot["pages"][0]["response"], first)
        self.assertIn("retrieved_at", snapshot)

    def test_invalid_or_cyclic_pagination_is_rejected(self):
        cases = [
            {"results": [], "next": EVENTS_URL},
            {"results": [], "next": "https://example.test/events"},
            {"results": [], "next": 7},
            {"unexpected": []},
        ]
        for response in cases:
            with self.subTest(response=response), self.assertRaises(ValueError):
                fetch_events(lambda url: response)

    def test_network_validation_and_serialization_failures_preserve_previous_data(self):
        self.prepare_offline()
        before = self.snapshot_bytes()
        with self.assertRaises(OSError):
            prepare(self.catalog, self.destination, fetch=Mock(side_effect=OSError("Network failed")))
        self.assertEqual(before, self.snapshot_bytes())
        duplicate = event_fixture()
        self.events_file.write_text(json.dumps(snapshot_fixture([duplicate, duplicate])), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.prepare_offline()
        self.assertEqual(before, self.snapshot_bytes())
        self.events_file.write_text(json.dumps(snapshot_fixture([event_fixture()])), encoding="utf-8")
        with patch.object(pd.DataFrame, "to_parquet", side_effect=OSError("Disk failed")):
            with self.assertRaises(OSError):
                self.prepare_offline()
        self.assertEqual(before, self.snapshot_bytes())

    def test_successful_replacement_and_rollback_after_publication_failure(self):
        self.prepare_offline()
        before = self.snapshot_bytes()
        original_rename = Path.rename

        def rename(path, target):
            if path.name == "dataset" and path.parent.name.startswith(".plaza-"):
                raise OSError("Publication failed")
            return original_rename(path, target)

        with patch.object(Path, "rename", rename):
            with self.assertRaises(OSError):
                self.prepare_offline()
        self.assertEqual(before, self.snapshot_bytes())
        snapshot = snapshot_fixture([event_fixture(42)])
        self.events_file.write_text(json.dumps(snapshot), encoding="utf-8")
        self.prepare_offline()
        self.assertEqual(load_tables(self.destination)["events"]["id"].tolist(), [42])

    def test_duplicate_stores_invalid_timestamps_and_missing_references_are_rejected(self):
        catalog = catalog_fixture()
        catalog["brands"][0]["stores"] *= 2
        with self.assertRaises(ValueError):
            normalize_data(catalog, [])
        event = event_fixture()
        event["start_time"] = "2026-11-13T21:00:00"
        with self.assertRaises(ValueError):
            normalize_data(catalog_fixture(), [event])
        self.prepare_offline()
        path = self.destination / "json" / "stores.json"
        records = json.loads(path.read_text())
        records[0]["brand_id"] = 999999
        path.write_text(json.dumps(records))
        with self.assertRaises(ValueError):
            load_tables(self.destination, "json")

    def test_preparation_does_not_replace_unrelated_directory(self):
        self.destination.mkdir()
        unrelated = self.destination / "keep.txt"
        unrelated.write_text("keep")
        with self.assertRaises(ValueError):
            self.prepare_offline()
        self.assertEqual(unrelated.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
