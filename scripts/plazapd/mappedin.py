import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

import pandas as pd

from plaza.data import FORMATS, ROOT, write_json


SDK_DIR = Path(__file__).resolve().parent / "mappedin"
SDK_PACKAGE = SDK_DIR / "node_modules" / "@mappedin" / "mappedin-js" / "package.json"
DEFAULT_DATA_DIR = ROOT / "data" / "plazapd"
DEFAULT_ENV = ROOT.parent / "companions" / "env" / "mappdn" / "prod-envs"
ENTITY_SCHEMA = {
    "entity_id": "string", "id": "string", "type": "string", "collection": "string",
    "name": "string", "external_id": "string", "description": "string", "floor_id": "string",
    "hidden": "boolean", "navigable": "boolean", "amenity": "string", "elevation": "Float64",
    "properties_json": "string", "geometry_json": "string",
}
RELATION_SCHEMA = {"src": "string", "dst": "string", "relation": "string"}
DISTANCE_SCHEMA = {"from_id": "string", "to_id": "string", "distance_m": "Float64", "status": "string"}


class MissingMappedinDependency(ValueError):
    pass


def frame(rows, schema):
    return pd.DataFrame(rows, columns=schema).astype(schema)


def normalize(snapshot):
    if snapshot.get("format_version") != 1:
        raise ValueError("Unsupported Mappedin snapshot version")
    entities = frame(snapshot["entities"], ENTITY_SCHEMA).sort_values("entity_id").reset_index(drop=True)
    if entities.entity_id.isna().any() or entities.entity_id.duplicated().any() or entities.id.isna().any():
        raise ValueError("Invalid or duplicate entity identities")
    if not (entities.entity_id == entities.type + ":" + entities.id).all():
        raise ValueError("Entity identity does not match source ID")
    relations = frame(snapshot["relationships"], RELATION_SCHEMA).sort_values(list(RELATION_SCHEMA)).reset_index(drop=True)
    if relations.isna().any().any() or relations.duplicated().any():
        raise ValueError("Invalid or duplicate relationships")
    for column in ("src", "dst"):
        if not relations[column].isin(entities.entity_id).all():
            raise ValueError("Dangling relationship reference")
    floors = entities.loc[entities.type == "floor", "id"]
    if not entities.floor_id.dropna().isin(floors).all():
        raise ValueError("Dangling floor reference")
    routes = frame(snapshot["distances"], DISTANCE_SCHEMA).sort_values(["from_id", "to_id"]).reset_index(drop=True)
    targets = snapshot["routing"]["target_ids"]
    expected_targets = set(entities.loc[(entities.type == "enterprise-location") & entities.navigable.fillna(False), "id"])
    if len(targets) != len(set(targets)) or set(targets) != expected_targets:
        raise ValueError("Routing targets do not match navigable locations")
    if len(routes) != len(targets) * (len(targets) - 1):
        raise ValueError("Incomplete all-pairs distance matrix")
    if routes[["from_id", "to_id", "status"]].isna().any().any() or routes.duplicated(["from_id", "to_id"]).any():
        raise ValueError("Invalid or duplicate route pairs")
    if not routes.from_id.isin(targets).all() or not routes.to_id.isin(targets).all() or (routes.from_id == routes.to_id).any():
        raise ValueError("Invalid route endpoints")
    if not routes.status.isin(["reachable", "unreachable"]).all():
        raise ValueError("Invalid route status")
    reached = routes.status == "reachable"
    values = routes.loc[reached, "distance_m"]
    if values.isna().any() or not values.map(lambda v: float('-inf') < v < float('inf') and v >= 0).all():
        raise ValueError("Invalid walking distance")
    if routes.loc[~reached, "distance_m"].notna().any():
        raise ValueError("Unreachable routes must have null distances")
    tables = {"entities": entities, "relationships": relations, "distances": routes}
    schemas = {"entities": ENTITY_SCHEMA, "relationships": RELATION_SCHEMA, "distances": DISTANCE_SCHEMA}
    for collection in snapshot["collections"]:
        if not collection.replace('_', '').isalnum() or collection in tables:
            raise ValueError("Invalid collection name")
        tables[collection] = entities.loc[entities.collection == collection].reset_index(drop=True)
        schemas[collection] = ENTITY_SCHEMA
    locations = entities.loc[entities.type == "enterprise-location"]
    for name, kind in (("stores", "tenant"), ("amenities", "amenities")):
        tables[name] = locations.loc[locations.properties_json.map(lambda v: json.loads(v).get("type") == kind)].reset_index(drop=True)
        schemas[name] = ENTITY_SCHEMA
    return tables, schemas


def load_tables(data_dir=DEFAULT_DATA_DIR, format="parquet"):
    if format not in FORMATS:
        raise ValueError("Unsupported export format")
    path = Path(data_dir)
    manifest = json.loads((path / "manifest.json").read_text())
    if manifest.get("dataset") != "mappedin" or manifest.get("format_version") != 1:
        raise ValueError("Not a Mappedin dataset")
    tables = {}
    for name, info in manifest["tables"].items():
        schema = info["columns"]
        file = path / format / f"{name}.{format}"
        if format == "parquet":
            result = pd.read_parquet(file)
        elif format == "json":
            result = pd.DataFrame(json.loads(file.read_text()), columns=schema)
        else:
            with file.open(encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream, quoting=csv.QUOTE_NOTNULL)
                if reader.fieldnames != list(schema):
                    raise ValueError("Unexpected CSV columns")
                records = list(reader)
            for row in records:
                for column, dtype in schema.items():
                    if row[column] is not None and dtype == "boolean":
                        if row[column] not in ("True", "False"):
                            raise ValueError("Invalid CSV boolean")
                        row[column] = row[column] == "True"
                    elif row[column] is not None and dtype == "Float64":
                        row[column] = float(row[column])
            result = pd.DataFrame(records, columns=schema)
        result = result.astype(schema)
        if len(result) != info["row_count"]:
            raise ValueError("Unexpected table count")
        tables[name] = result
    return tables


def prepare(env_file=DEFAULT_ENV, data_dir=DEFAULT_DATA_DIR, raw_dir=None):
    if not raw_dir and not SDK_PACKAGE.is_file():
        raise MissingMappedinDependency(
            "Mappedin SDK is missing. From the repository root, run: "
            "npm ci --prefix scripts/plazapd/mappedin"
        )
    destination = Path(data_dir).absolute()
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise ValueError("Destination must be a regular directory")
    if destination.exists() and any(destination.iterdir()):
        manifest_path = destination / "manifest.json"
        if not manifest_path.is_file() or json.loads(manifest_path.read_text()).get("dataset") != "mappedin":
            raise ValueError("Refusing to replace a directory without a Mappedin manifest")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".mappedin-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "dataset"
        raw = staged / "raw"
        raw.mkdir(parents=True)
        if raw_dir:
            for name in ("map.json", "snapshot.json"):
                shutil.copyfile(Path(raw_dir) / name, raw / name)
        else:
            result = subprocess.run(["node", str(SDK_DIR / "export.mjs"),
                                     str(Path(env_file).absolute()), str(raw)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            if result.returncode:
                raise ValueError("Mappedin collection failed; previous export preserved")
        snapshot = json.loads((raw / "snapshot.json").read_text())
        json.loads((raw / "map.json").read_text())
        tables, schemas = normalize(snapshot)
        for format in FORMATS:
            (staged / format).mkdir()
        for name, table in tables.items():
            records = table.astype(object).where(table.notna(), None).to_dict(orient="records")
            table.to_parquet(staged / "parquet" / f"{name}.parquet", index=False)
            write_json(staged / "json" / f"{name}.json", records)
            with (staged / "csv" / f"{name}.csv").open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(schemas[name]), quoting=csv.QUOTE_NOTNULL)
                writer.writeheader()
                writer.writerows(records)
        manifest = {key: value for key, value in snapshot.items() if key not in ("entities", "relationships", "distances")}
        manifest.update(dataset="mappedin", prepared_at=datetime.now(timezone.utc).isoformat(),
                        tables={name: {"columns": schemas[name], "row_count": len(table)} for name, table in tables.items()})
        manifest["routing"]["reachable_pairs"] = int((tables["distances"].status == "reachable").sum())
        manifest["routing"]["unreachable_pairs"] = int((tables["distances"].status == "unreachable").sum())
        manifest["files"] = {str(path.relative_to(staged)): hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in sorted(staged.rglob("*")) if path.is_file()}
        write_json(staged / "manifest.json", manifest)
        for format in FORMATS:
            for name, loaded in load_tables(staged, format).items():
                pd.testing.assert_frame_equal(tables[name], loaded, check_exact=True)
        backup = Path(temporary) / "previous"
        if destination.exists():
            destination.rename(backup)
        try:
            staged.rename(destination)
        except OSError:
            if backup.exists():
                backup.rename(destination)
            raise
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--raw-dir", type=Path)
    args = parser.parse_args()
    try:
        manifest = prepare(args.env_file, args.data_dir, args.raw_dir)
    except MissingMappedinDependency as error:
        parser.exit(2, f"{error}\n")
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(2, "Mappedin preparation failed; previous dataset preserved.\n")
    print(f"Saved Mappedin dataset to {args.data_dir}")
    for name, table in manifest["tables"].items():
        print(f"  {name}: {table['row_count']}")
    print(f"  unreachable pairs: {manifest['routing']['unreachable_pairs']}")


if __name__ == "__main__":
    main()
