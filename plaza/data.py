import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = ROOT.parent / "mappdn" / "data" / "catalog.json"
DEFAULT_DATA_DIR = ROOT / "data" / "plazacc"
EVENTS_URL = "https://overlord.plazasanmiguel.io/kiosk/api/v1/event/"
FORMATS = ("parquet", "json", "csv")
SCHEMAS = {
    "brands": {
        "id": "Int64", "name": "string", "description": "string",
        "directory_id": "Int64", "is_active": "boolean", "is_new": "boolean",
        "subcategory_id": "Int64", "logo": "string", "store_photo": "string",
        "phone": "string", "website": "string", "facebook": "string",
        "instagram": "string", "tiktok": "string",
    },
    "stores": {"id": "string", "brand_id": "Int64", "location_id": "Int64"},
    "locations": {"id": "Int64", "name": "string"},
    "categories": {"id": "Int64", "name": "string"},
    "subcategories": {"id": "Int64", "name": "string", "category_id": "Int64"},
    "events": {
        "id": "Int64", "title": "string", "description": "string",
        "start_time": "string", "end_time": "string", "image": "string",
        "category_id": "Int64", "venue_id": "string",
    },
    "event_categories": {"id": "Int64", "name": "string", "display_name": "string"},
    "event_venues": {
        "id": "string", "name": "string", "display_name": "string",
        "external_id": "string", "location_id": "Int64",
    },
}
REFERENCES = (
    ("brands", "subcategory_id", "subcategories"),
    ("stores", "brand_id", "brands"),
    ("stores", "location_id", "locations"),
    ("subcategories", "category_id", "categories"),
    ("events", "category_id", "event_categories"),
    ("events", "venue_id", "event_venues"),
    ("event_venues", "location_id", "locations"),
)


def normalize_name(value):
    return " ".join((value or "").casefold().split())


def utc_time(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError(f"Event timestamp has no timezone: {value}")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def fetch_json(url):
    request = Request(url, headers={"User-Agent": "PyGraphistry Plaza demo/0.1"})
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def fetch_events(fetch=fetch_json):
    pages = []
    visited = set()
    url = EVENTS_URL
    while url:
        if url in visited or urlsplit(url).scheme != "https" or urlsplit(url).netloc != urlsplit(EVENTS_URL).netloc:
            raise ValueError("Invalid event pagination URL")
        visited.add(url)
        payload = fetch(url)
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ValueError("The event feed must contain a results list")
        pages.append({"url": url, "response": payload})
        following = payload.get("next")
        if following is not None and not isinstance(following, str):
            raise ValueError("Invalid event pagination value")
        url = urljoin(url, following) if following else None
    return {
        "source": EVENTS_URL,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "pages": pages,
    }


def snapshot_events(snapshot):
    pages = snapshot.get("pages")
    if not isinstance(pages, list) or not pages:
        raise ValueError("The events file must be a saved raw/events.json snapshot")
    events = []
    for page in pages:
        response = page["response"]
        if not isinstance(response.get("results"), list):
            raise ValueError("Invalid saved event page")
        events.extend(response["results"])
    count = pages[0]["response"].get("count")
    if count is not None and count != len(events):
        raise ValueError("Event count does not match the collected pages")
    return events


def validate_tables(tables):
    if set(tables) != set(SCHEMAS):
        raise ValueError("Dataset tables do not match the schema")
    for name, schema in SCHEMAS.items():
        frame = tables[name]
        if list(frame.columns) != list(schema):
            raise ValueError(f"Unexpected columns in {name}")
        if frame["id"].isna().any() or frame["id"].duplicated().any():
            raise ValueError(f"Missing or duplicate IDs in {name}")
        label = "title" if name == "events" else "name"
        if label in frame and (frame[label].isna() | frame[label].str.strip().eq("")).any():
            raise ValueError(f"Missing names in {name}")
    for table, column, parent in REFERENCES:
        if not tables[table][column].dropna().isin(tables[parent]["id"]).all():
            raise ValueError(f"Unknown reference: {table}.{column}")
    for column in ("brand_id", "location_id"):
        if tables["stores"][column].isna().any():
            raise ValueError(f"Missing store {column}")


def normalize_data(catalog, events):
    rows = {name: {} for name in SCHEMAS}

    def add(table, row):
        existing = rows[table].get(row["id"])
        if existing is not None and existing != row:
            raise ValueError(f"Conflicting {table} ID: {row['id']}")
        rows[table][row["id"]] = row

    def add_subcategory(item):
        category = item["category"]
        add("categories", {"id": category["id"], "name": category["name"]})
        add("subcategories", {"id": item["id"], "name": item["name"], "category_id": category["id"]})

    if not isinstance(catalog.get("brands"), list) or not catalog["brands"]:
        raise ValueError("The catalog must contain brands")
    for item in catalog["subcategories"]:
        add_subcategory(item)
    for item in catalog.get("locations", []):
        add("locations", {"id": item["id"], "name": item["name"]})
    for brand in catalog["brands"]:
        if brand["id"] in rows["brands"]:
            raise ValueError(f"Duplicate brand ID: {brand['id']}")
        subcategory = brand["subcategory"]
        add_subcategory(subcategory)
        fields = {
            "id": "id", "name": "name", "description": "description",
            "directory_id": "directoryId", "is_active": "isActive", "is_new": "isNew",
            "logo": "logo", "store_photo": "storePhoto", "phone": "phone",
            "website": "website", "facebook": "facebook", "instagram": "instagram", "tiktok": "tiktok",
        }
        row = {column: brand.get(source) for column, source in fields.items()}
        row["subcategory_id"] = subcategory["id"]
        add("brands", row)
        for store in brand.get("stores", []):
            location = store["location"]
            add("locations", {"id": location["id"], "name": location["name"]})
            store_id = f"{brand['id']}:{location['id']}"
            if store_id in rows["stores"]:
                raise ValueError(f"Duplicate brand/location store: {store_id}")
            add("stores", {"id": store_id, "brand_id": brand["id"], "location_id": location["id"]})

    location_names = {}
    for location in rows["locations"].values():
        location_names.setdefault(normalize_name(location["name"]), set()).add(location["id"])
    for event in events:
        if event["id"] in rows["events"]:
            raise ValueError(f"Duplicate event ID: {event['id']}")
        category = event.get("category")
        if category:
            add("event_categories", {key: category.get(key) for key in SCHEMAS["event_categories"]})
        venue = event.get("landmark") or {}
        venue_id = None
        names = [normalize_name(venue.get(key)) for key in ("name", "display_name")]
        if any(names):
            venue_id = hashlib.sha256(json.dumps(names, ensure_ascii=False).encode()).hexdigest()
            matches = set().union(*(location_names.get(name, set()) for name in names if name))
            if len(matches) > 1:
                raise ValueError("An event venue matches multiple directory locations")
            add("event_venues", {
                "id": venue_id, "name": venue.get("name") or venue.get("display_name"),
                "display_name": venue.get("display_name"), "external_id": venue.get("external_id"),
                "location_id": next(iter(matches), None),
            })
        add("events", {
            "id": event["id"], "title": event["title"], "description": event.get("description"),
            "start_time": utc_time(event.get("start_time")), "end_time": utc_time(event.get("end_time")),
            "image": event.get("image"), "category_id": category["id"] if category else None,
            "venue_id": venue_id,
        })
    tables = {
        name: pd.DataFrame(list(rows[name].values()), columns=schema).astype(schema).sort_values("id").reset_index(drop=True)
        for name, schema in SCHEMAS.items()
    }
    validate_tables(tables)
    return tables


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_tables(data_dir=DEFAULT_DATA_DIR, format="parquet"):
    if format not in FORMATS:
        raise ValueError(f"Unknown data format: {format}")
    data_dir = Path(data_dir)
    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest["format_version"] != 1:
        raise ValueError("Unsupported dataset version")
    tables = {}
    for name, schema in SCHEMAS.items():
        entry = manifest["tables"][name]
        if entry["columns"] != schema:
            raise ValueError(f"Unexpected schema for {name}")
        path = data_dir / format / f"{name}.{format}"
        if format == "parquet":
            frame = pd.read_parquet(path, engine="pyarrow")
        elif format == "json":
            frame = pd.DataFrame(json.loads(path.read_text(encoding="utf-8")), columns=schema)
        else:
            with path.open(encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream, quoting=csv.QUOTE_NOTNULL)
                if reader.fieldnames != list(schema):
                    raise ValueError(f"Unexpected CSV columns for {name}")
                records = list(reader)
            for row in records:
                for column, dtype in schema.items():
                    if row[column] is not None and dtype == "Int64":
                        row[column] = int(row[column])
                    elif row[column] is not None and dtype == "boolean":
                        if row[column] not in ("True", "False"):
                            raise ValueError(f"Invalid boolean in {name}.{column}")
                        row[column] = row[column] == "True"
            frame = pd.DataFrame(records, columns=schema)
        tables[name] = frame.astype(schema)
        if len(frame) != entry["row_count"]:
            raise ValueError(f"Unexpected row count for {name}")
    validate_tables(tables)
    return tables


def prepare(catalog=DEFAULT_CATALOG, data_dir=DEFAULT_DATA_DIR, events_file=None, fetch=fetch_json):
    catalog_path = Path(catalog)
    catalog_bytes = catalog_path.read_bytes()
    catalog = json.loads(catalog_bytes)
    snapshot = json.loads(Path(events_file).read_text(encoding="utf-8")) if events_file else fetch_events(fetch)
    tables = normalize_data(catalog, snapshot_events(snapshot))
    destination = Path(data_dir).absolute()
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise ValueError("The data directory must be a regular directory")
    if destination.exists() and any(destination.iterdir()):
        manifest_path = destination / "manifest.json"
        if not manifest_path.is_file() or json.loads(manifest_path.read_text()).get("format_version") != 1:
            raise ValueError("Refusing to replace a directory without a Plaza dataset manifest")
    manifest = {
        "format_version": 1,
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        "sources": {
            "catalog": {
                "path": os.path.relpath(catalog_path, ROOT),
                "retrieved_at": catalog.get("retrieved_at"),
                "urls": catalog.get("sources", {}),
                "sha256": hashlib.sha256(catalog_bytes).hexdigest(),
            },
            "events": {"url": snapshot["source"], "retrieved_at": snapshot["retrieved_at"]},
        },
        "tables": {name: {"columns": schema, "row_count": len(tables[name])} for name, schema in SCHEMAS.items()},
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".plaza-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "dataset"
        for name in ("raw", *FORMATS):
            (staged / name).mkdir(parents=True)
        (staged / "raw" / "catalog.json").write_bytes(catalog_bytes)
        write_json(staged / "raw" / "events.json", snapshot)
        for name, frame in tables.items():
            records = json.loads(frame.to_json(orient="records", force_ascii=False))
            frame.to_parquet(staged / "parquet" / f"{name}.parquet", index=False, engine="pyarrow")
            write_json(staged / "json" / f"{name}.json", records)
            with (staged / "csv" / f"{name}.csv").open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(SCHEMAS[name]), quoting=csv.QUOTE_NOTNULL)
                writer.writeheader()
                writer.writerows(records)
        write_json(staged / "manifest.json", manifest)
        for format in FORMATS:
            loaded = load_tables(staged, format)
            for name, frame in tables.items():
                pd.testing.assert_frame_equal(frame, loaded[name])
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
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--events-file", type=Path, help="Replay a saved raw/events.json snapshot offline")
    args = parser.parse_args()
    try:
        manifest = prepare(args.catalog, args.data_dir, args.events_file)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f"Data preparation failed: {error}\n")
    print(f"Saved equivalent {', '.join(FORMATS)} tables to {args.data_dir}")
    for name, entry in manifest["tables"].items():
        print(f"  {name}: {entry['row_count']}")


if __name__ == "__main__":
    main()
