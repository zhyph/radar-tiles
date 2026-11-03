#!/usr/bin/env python3
"""Builds camera tiles from OpenStreetMap extracts (Geofabrik), for the Radar Alert app.

Subcommands
  plan INDEX [ONLY]                     print the JSON list of regions to process
  region ROOT INDEX OUT.json [ONLY]     download every extract under ROOT one at a time,
                                        collect its cameras, delete it; write OUT.json
  collect OUT.json PBF...               collect cameras from local .osm.pbf files
  tiles OUT_DIR CAMERAS.json...         merge camera lists and write the 1-degree tiles

Tiles: <out>/t/<lat>_<lon>.json, named by the south-west corner of the 1 degree square
(e.g. t/-24_-47.json), plus <out>/manifest.json. Empty squares get no file, so a missing
tile means "no cameras there" as long as the manifest is reachable.

What counts as a camera (per the OSM wiki):
  * nodes tagged highway=speed_camera
  * "device" members of type=enforcement relations with enforcement=maxspeed,
    traffic_signals or average_speed (these carry the maxspeed and the camera kind)

Data (c) OpenStreetMap contributors, available under the ODbL 1.0.
"""
import json
import math
import os
import shutil
import sys
import tempfile
import time
import urllib.request
import zlib
from datetime import datetime, timezone

USER_AGENT = "radar-tiles/0.1 (+https://github.com/zhyph/radar-tiles)"
# When one relation lists several values (traffic_signals;maxspeed), the first match wins.
KIND_PRIORITY = (("maxspeed", "SPEED"), ("average_speed", "AVERAGE"), ("traffic_signals", "RED_LIGHT"))


def kind_of(enforcement):
    values = {v.strip() for v in (enforcement or "").split(";")}
    for value, kind in KIND_PRIORITY:
        if value in values:
            return kind
    return None


def parse_maxspeed(value):
    """'50' -> (50, False); '40 mph' -> (40, True); anything else -> (0, False)."""
    v = (value or "").strip().lower()
    mph = v.endswith("mph")
    digits = v.replace("mph", "").replace("km/h", "").strip()
    try:
        return int(digits), mph
    except ValueError:
        return 0, mph


# ---- Reading extracts ---------------------------------------------------------------------

def relation_devices(path):
    """Pass 1: {node id: (kind, maxspeed, mph)} for the devices of enforcement relations."""
    import osmium
    devices = {}
    proc = osmium.FileProcessor(path, osmium.osm.RELATION).with_filter(osmium.filter.KeyFilter("enforcement"))
    for rel in proc:
        tags = rel.tags
        kind = kind_of(tags.get("enforcement")) if tags.get("type") == "enforcement" else None
        if kind is None:
            continue
        speed, mph = parse_maxspeed(tags.get("maxspeed"))
        for m in rel.members:
            if m.role == "device" and m.type == "n":
                devices.setdefault(m.ref, (kind, speed, mph))
    return devices


def collect(path, cameras):
    """Adds the cameras of one extract to `cameras` ({id: dict}); returns the extract timestamp."""
    import osmium
    devices = relation_devices(path)

    # Device nodes of relations (whatever their own tags), found by id.
    if devices:
        proc = osmium.FileProcessor(path, osmium.osm.NODE).with_filter(osmium.filter.IdFilter(devices.keys()))
        for n in proc:
            if n.location.valid():
                kind, speed, mph = devices[n.id]
                cameras[n.id] = camera(n.id, n.location.lat, n.location.lon, speed, mph, kind)

    # Plain speed-camera nodes; a relation's richer info (kind, maxspeed) wins if both exist.
    proc = osmium.FileProcessor(path, osmium.osm.NODE).with_filter(osmium.filter.KeyFilter("highway"))
    for n in proc:
        if n.tags.get("highway") == "speed_camera" and n.id not in cameras and n.location.valid():
            speed, mph = parse_maxspeed(n.tags.get("maxspeed"))
            cameras[n.id] = camera(n.id, n.location.lat, n.location.lon, speed, mph, "SPEED")

    header = osmium.FileProcessor(path).header
    return header.get("osmosis_replication_timestamp") or header.get("timestamp") or ""


def camera(node_id, lat, lon, speed, mph, kind):
    return {"id": node_id, "lat": round(lat, 6), "lon": round(lon, 6), "max": speed, "mph": mph, "kind": kind}


# ---- Geofabrik regions --------------------------------------------------------------------

def leaf_extracts(index_path, root=None, only=""):
    """Leaf extracts (no sub-regions) of the Geofabrik index: [(id, pbf url)], optionally under
    one top-level region and filtered by a substring of the id."""
    feats = [f["properties"] for f in json.load(open(index_path))["features"]]
    parent = {p["id"]: p.get("parent") for p in feats}
    has_children = {p for p in parent.values() if p}

    def top(i):
        while parent.get(i):
            i = parent[i]
        return i

    out = []
    for p in feats:
        url = p.get("urls", {}).get("pbf")
        if p["id"] in has_children or not url or top(p["id"]) == "antarctica":
            continue
        if root and top(p["id"]) != root:
            continue
        if only and only not in p["id"]:
            continue
        out.append((p["id"], url))
    return out


def download(url, dest, attempts=3):
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as out:
                shutil.copyfileobj(resp, out, 1 << 20)
            return
        except Exception as e:  # network hiccup: back off and retry
            print(f"  download failed ({e}), attempt {attempt}/{attempts}", file=sys.stderr)
            if attempt == attempts:
                raise
            time.sleep(30 * attempt)


def cmd_plan(index, only=""):
    print(json.dumps(sorted(roots_with_extracts(index, only))))


def roots_with_extracts(index, only):
    feats = [f["properties"] for f in json.load(open(index))["features"]]
    parent = {p["id"]: p.get("parent") for p in feats}

    def top(i):
        while parent.get(i):
            i = parent[i]
        return i

    return {top(i) for i, _ in leaf_extracts(index, only=only)}


def fresh_enough(root, state_json):
    """True when a previous result for this region is recent enough to reuse (no downloads).
    Age is the OSM data timestamp, not the file date. The limit is 10-19 days, fixed per region
    by a hash, so regions do not all come due in the same weekly run."""
    try:
        stamp = json.load(open(state_json))["osm"]
        data_time = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (OSError, ValueError, KeyError):
        return False
    limit_days = 10 + zlib.crc32(root.encode()) % 10
    return (datetime.now(timezone.utc) - data_time).days < limit_days


def cmd_region(root, index, out_json, only=""):
    # STATE_DIR holds the previous run's per-region results; FORCE=1 ignores them.
    state_json = os.path.join(os.environ.get("STATE_DIR", ""), os.path.basename(out_json))
    if os.environ.get("STATE_DIR") and not only and not os.environ.get("FORCE") \
            and fresh_enough(root, state_json):
        shutil.copyfile(state_json, out_json)
        print(f"{root}: previous data is still fresh, nothing downloaded", file=sys.stderr)
        return
    cameras, newest = {}, ""
    tmp = tempfile.mkdtemp(prefix="extract-")
    extracts = leaf_extracts(index, root, only)
    print(f"{root}: {len(extracts)} extracts", file=sys.stderr)
    for i, (name, url) in enumerate(extracts, 1):
        path = os.path.join(tmp, name.replace("/", "_") + ".osm.pbf")
        print(f"[{i}/{len(extracts)}] {name}", file=sys.stderr)
        download(url, path)
        try:
            stamp = collect(path, cameras)
            newest = max(newest, str(stamp))
        finally:
            os.remove(path)  # keep the runner's disk free
        print(f"  {len(cameras)} cameras so far", file=sys.stderr)
    save_cameras(out_json, cameras, newest)


# ---- Output -------------------------------------------------------------------------------

def save_cameras(path, cameras, osm_timestamp):
    with open(path, "w") as f:
        json.dump({"osm": osm_timestamp, "cameras": list(cameras.values())}, f, separators=(",", ":"))
    print(f"saved {len(cameras)} cameras to {path}", file=sys.stderr)


def write_tiles(cameras, out_dir, osm_timestamp):
    tiles = {}
    for c in cameras.values():
        tiles.setdefault((math.floor(c["lat"]), math.floor(c["lon"])), []).append(c)
    tile_dir = os.path.join(out_dir, "t")
    os.makedirs(tile_dir, exist_ok=True)
    generated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for (lat, lon), cams in tiles.items():
        cams.sort(key=lambda c: c["id"])
        with open(os.path.join(tile_dir, f"{lat}_{lon}.json"), "w") as f:
            json.dump({"generated": generated, "osm": osm_timestamp, "cameras": cams}, f, separators=(",", ":"))
    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump({"generated": generated, "osm": osm_timestamp, "tiles": len(tiles),
                   "cameras": len(cameras)}, f)
    return len(tiles)


def cmd_tiles(out_dir, inputs):
    cameras, newest = {}, ""
    for path in inputs:
        data = json.load(open(path))
        newest = max(newest, str(data.get("osm", "")))
        for c in data["cameras"]:
            cameras.setdefault(c["id"], c)  # extracts overlap slightly at borders
    if not cameras:
        sys.exit("no cameras found; refusing to publish an empty dataset")
    n = write_tiles(cameras, out_dir, newest)
    print(f"wrote {n} tiles, {len(cameras)} cameras to {out_dir}", file=sys.stderr)


def cmd_collect(out_json, paths):
    cameras, newest = {}, ""
    for path in paths:
        newest = max(newest, str(collect(path, cameras)))
        print(f"{path}: {len(cameras)} cameras so far", file=sys.stderr)
    save_cameras(out_json, cameras, newest)


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__)
    cmd, args = argv[1], argv[2:]
    if cmd == "plan" and len(args) in (1, 2):
        cmd_plan(*args)
    elif cmd == "region" and len(args) in (3, 4):
        cmd_region(*args)
    elif cmd == "collect" and len(args) >= 2:
        cmd_collect(args[0], args[1:])
    elif cmd == "tiles" and len(args) >= 2:
        cmd_tiles(args[0], args[1:])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
