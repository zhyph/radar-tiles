# radar-tiles

Static camera tiles for the Radar Alert Android app: speed and enforcement camera locations from
OpenStreetMap, rebuilt weekly and published on GitHub Pages. It exists so the app has a source that
does not depend on the (free, fair-use) public Overpass servers being up.

## How it works

`.github/workflows/build.yml` runs weekly (and on demand):

1. **plan**: reads Geofabrik's extract index and lists the jobs: one per region, split into shards of about 30 extracts for the big regions (a runner job is capped at 6 hours).
2. **collect** (one job per region): downloads each Geofabrik extract of the region one at a time,
   reads the cameras with `pyosmium`, deletes the extract, and uploads the camera list.
   A region whose last result is still fresh (10 to 19 days old, set per region by a hash so they do
   not all come due together) is reused from the `state` branch with no download. Run the workflow
   with `force` to rebuild everything. A change to `SCHEMA` in `build_tiles.py` also forces a rebuild.
3. **publish**: merges the lists, writes the tiles and deploys them to GitHub Pages. It only runs if
   every job succeeded, so a partial dataset is never published and the previous one stays up.
4. **save-state**: runs even if some jobs failed and saves every finished job's result on the
   `state` branch, so a retry only redoes the jobs that failed.

A camera is a node tagged `highway=speed_camera`, or a `device` member of a `type=enforcement`
relation with `enforcement=maxspeed`, `traffic_signals` or `average_speed` (the relation supplies
the kind and the `maxspeed`). See the OSM wiki pages for `highway=speed_camera` and `Relation:enforcement`.

## Output

* `manifest.json`: `{"generated", "osm", "tiles", "cameras"}` (`osm` is the extract timestamp).
* `t/<lat>_<lon>.json`: one tile per 1 degree square, named by its south-west corner (`t/-24_-47.json`).
  A missing tile means no cameras there, provided `manifest.json` is reachable.

```json
{"generated": "...", "osm": "...", "cameras": [
  {"id": 123, "lat": 52.62012, "lon": 13.249668, "max": 60, "mph": false, "kind": "SPEED"}]}
```

`kind` is `SPEED`, `RED_LIGHT` or `AVERAGE`; `max` is 0 when unknown. An optional `dir` (0-359) is the
compass direction the camera faces, from the OSM `direction` tag when it is a plain cardinal value
(`both`, `forward`, `backward` and ranges are left out).

The `state` branch holds the per-region results of the last full run (single commit, force-pushed).

## Run locally

```sh
uv run --with osmium build_tiles.py collect cameras.json berlin.osm.pbf
python build_tiles.py tiles out cameras.json
```

## Data licence

Data &copy; OpenStreetMap contributors, available under the
[Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/). The tiles are a derived
database: keep the attribution and share them under the same licence. The code is MIT licensed.

Scheduled workflows on a public repository are paused after 60 days without repository activity;
re-enable the workflow from the Actions tab if that happens.
