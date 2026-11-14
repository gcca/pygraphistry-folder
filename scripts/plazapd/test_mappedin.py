import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from scripts.plazapd.mappedin import ENTITY_SCHEMA, SDK_DIR, SDK_PACKAGE, MissingMappedinDependency, load_tables, normalize, prepare


def fixture():
    entities = []
    for kind, source_id, collection in (("floor", "f", "floors"),
                                         ("enterprise-location", "a", "locations"),
                                         ("enterprise-location", "b", "locations")):
        record = dict.fromkeys(ENTITY_SCHEMA)
        record.update(entity_id=f"{kind}:{source_id}", id=source_id, type=kind,
                      collection=collection, name='Niño, "café"\n',
                      external_id="" if source_id == "a" else None,
                      floor_id=None if kind == "floor" else "f",
                      navigable=True if kind == "enterprise-location" else None,
                      properties_json=json.dumps({"type": "tenant"}), geometry_json=None)
        entities.append(record)
    return {"format_version": 1, "collected_at": "2026-11-14T00:00:00Z",
            "collections": ["floors", "locations", "nodes"], "entities": entities,
            "relationships": [{"src": "enterprise-location:a", "dst": "floor:f", "relation": "floor"}],
            "routing": {"target_ids": ["a", "b"]}, "distances": [
                {"from_id": "a", "to_id": "b", "distance_m": 12.345678901234567, "status": "reachable"},
                {"from_id": "b", "to_id": "a", "distance_m": None, "status": "unreachable"}]}


class MappedinTests(unittest.TestCase):
    def raw(self, root, snapshot=None):
        raw = root / "source"
        raw.mkdir()
        (raw / "map.json").write_text('{}')
        (raw / "snapshot.json").write_text(json.dumps(snapshot or fixture()))
        return raw

    def test_roundtrip_and_offline_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = self.raw(root)
            destination = root / "dataset"
            with patch("subprocess.run", side_effect=AssertionError("Offline replay contacted Node")):
                manifest = prepare(data_dir=destination, raw_dir=raw)
                prepare(data_dir=root / "replayed", raw_dir=destination / "raw")
            baseline = load_tables(destination)
            for format in ("json", "csv", "parquet"):
                for name, table in load_tables(destination, format).items():
                    pd.testing.assert_frame_equal(baseline[name], table, check_exact=True)
            self.assertEqual(baseline["locations"].iloc[0].external_id, "")
            self.assertTrue(pd.isna(baseline["locations"].iloc[1].external_id))
            self.assertEqual(manifest["routing"]["unreachable_pairs"], 1)
            self.assertEqual(len(baseline["stores"]), 2)
            self.assertEqual(len(baseline["nodes"]), 0)
            self.assertEqual(manifest["collected_at"], "2026-11-14T00:00:00Z")
            self.assertEqual(manifest["files"], json.loads((root / "replayed" / "manifest.json").read_text())["files"])

    def test_invalid_snapshots(self):
        changes = [
            lambda s: s["entities"].append(s["entities"][0]),
            lambda s: s["relationships"][0].update(dst="floor:missing"),
            lambda s: s["entities"][1].update(floor_id="missing"),
            lambda s: s["distances"].pop(),
            lambda s: s["distances"][1].update(from_id="a", to_id="b"),
            lambda s: s["distances"][0].update(distance_m=-1),
            lambda s: s["distances"][0].update(distance_m=float("inf")),
            lambda s: s["distances"][1].update(distance_m=0),
            lambda s: s["routing"].update(target_ids=["a", "missing"]),
        ]
        for change in changes:
            snapshot = copy.deepcopy(fixture())
            change(snapshot)
            with self.subTest(change=change), self.assertRaises(ValueError):
                normalize(snapshot)

    def test_failures_preserve_previous_dataset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = self.raw(root)
            destination = root / "dataset"
            prepare(data_dir=destination, raw_dir=raw)
            previous = (destination / "manifest.json").read_bytes()
            with patch("scripts.plazapd.mappedin.SDK_PACKAGE", raw / "map.json"), patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1)):
                with self.assertRaises(ValueError):
                    prepare(data_dir=destination)
            self.assertEqual(previous, (destination / "manifest.json").read_bytes())
            (raw / "snapshot.json").write_text('{}')
            with self.assertRaises(ValueError):
                prepare(data_dir=destination, raw_dir=raw)
            self.assertEqual(previous, (destination / "manifest.json").read_bytes())

    def test_publication_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = self.raw(root)
            destination = root / "dataset"
            prepare(data_dir=destination, raw_dir=raw)
            previous = (destination / "manifest.json").read_bytes()
            rename = Path.rename

            def fail(source, target):
                if source.name == "dataset" and source.parent.name.startswith(".mappedin-"):
                    raise OSError("Publication failed")
                return rename(source, target)

            with patch.object(Path, "rename", fail), self.assertRaises(OSError):
                prepare(data_dir=destination, raw_dir=raw)
            self.assertEqual(previous, (destination / "manifest.json").read_bytes())

    def test_refuses_unrelated_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            (destination / "keep").write_text("user data")
            with patch("scripts.plazapd.mappedin.SDK_PACKAGE", destination / "keep"), self.assertRaises(ValueError):
                prepare(data_dir=destination)
            self.assertEqual((destination / "keep").read_text(), "user data")

    def test_missing_sdk_install_hint(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "not-created"
            with patch("scripts.plazapd.mappedin.SDK_PACKAGE", destination), self.assertRaisesRegex(
                MissingMappedinDependency, "npm ci --prefix scripts/plazapd/mappedin"
            ):
                prepare(data_dir=destination)
            self.assertFalse(destination.exists())

    def test_node_exporter(self):
        if not SDK_PACKAGE.is_file():
            self.skipTest("Install SDK: npm ci --prefix scripts/plazapd/mappedin")
        result = subprocess.run(["node", "--test", str(SDK_DIR / "test_export.mjs")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
