#!/usr/bin/env python3
"""Download the geoBoundaries UKR ADM1 GeoJSON file and check it."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from meshalerts.oblast import ISO_TO_NAME  # noqa: E402

METADATA_URL = "https://www.geoboundaries.org/api/current/gbOpen/UKR/ADM1/"
EXPECTED_FEATURES = 27


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="Download the full-resolution file.")
    parser.add_argument("--out-dir", default=str(ROOT / "data"), help="Output directory.")
    args = parser.parse_args()

    field = "gjDownloadURL" if args.full else "simplifiedGeometryGeoJSON"
    meta = requests.get(METADATA_URL, timeout=30)
    meta.raise_for_status()
    url = meta.json().get(field)
    if not url:
        print(f"Metadata has no field {field}.", file=sys.stderr)
        return 1

    print(f"Download {url}")
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    data = resp.json()

    features = data.get("features", [])
    if len(features) != EXPECTED_FEATURES:
        print(f"Expected {EXPECTED_FEATURES} features. Got {len(features)}.", file=sys.stderr)
        return 1
    unknown = [
        f.get("properties", {}).get("shapeISO")
        for f in features
        if f.get("properties", {}).get("shapeISO") not in ISO_TO_NAME
    ]
    if unknown:
        print(f"Unknown shapeISO values: {unknown}", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "geoBoundaries-UKR-ADM1.geojson"
    out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {out} ({out.stat().st_size // 1024} KB, {len(features)} features).")
    print("Data license: ODbL 1.0 (geoBoundaries).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
