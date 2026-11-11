"""GeoJSON load and oblast lookup."""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

import shapely
from shapely.geometry import Point, box, shape
from shapely.ops import nearest_points

log = logging.getLogger(__name__)

METRES_PER_DEGREE = 111_320

ISO_TO_NAME = {
    "UA-05": "Вінницька область",
    "UA-07": "Волинська область",
    "UA-09": "Луганська область",
    "UA-12": "Дніпропетровська область",
    "UA-14": "Донецька область",
    "UA-18": "Житомирська область",
    "UA-21": "Закарпатська область",
    "UA-23": "Запорізька область",
    "UA-26": "Івано-Франківська область",
    "UA-30": "м. Київ",
    "UA-32": "Київська область",
    "UA-35": "Кіровоградська область",
    "UA-40": "Севастополь",
    "UA-43": "Автономна Республіка Крим",
    "UA-46": "Львівська область",
    "UA-48": "Миколаївська область",
    "UA-51": "Одеська область",
    "UA-53": "Полтавська область",
    "UA-56": "Рівненська область",
    "UA-59": "Сумська область",
    "UA-61": "Тернопільська область",
    "UA-63": "Харківська область",
    "UA-65": "Херсонська область",
    "UA-68": "Хмельницька область",
    "UA-71": "Черкаська область",
    "UA-74": "Чернігівська область",
    "UA-77": "Чернівецька область",
}
NAME_TO_ISO = {name: iso for iso, name in ISO_TO_NAME.items()}
NO_API_DATA = {"Автономна Республіка Крим"}
API_NAMES = frozenset(ISO_TO_NAME.values()) - NO_API_DATA

INSIDE = "inside"
NEAR_BORDER = "near_border"
OUTSIDE_NEARBY = "outside_nearby"
OUTSIDE_UKRAINE = "outside_ukraine"


class OblastDataError(RuntimeError):
    """The GeoJSON file is missing or not usable."""


@dataclass
class Resolution:
    oblast: str | None
    status: str
    distance_to_border_m: float | None = None
    candidates: list[str] = field(default_factory=list)


def _metres(lat: float, a: Point, b: Point) -> float:
    dlat = (a.y - b.y) * METRES_PER_DEGREE
    dlon = (a.x - b.x) * METRES_PER_DEGREE * math.cos(math.radians(lat))
    return math.hypot(dlat, dlon)


class OblastIndex:
    def __init__(
        self,
        geojson_path: str | Path,
        border_margin_m: float = 1500,
        outside_margin_m: float = 3000,
    ):
        self.border_margin_m = border_margin_m
        self.outside_margin_m = outside_margin_m
        path = Path(geojson_path)
        if not path.is_file():
            raise OblastDataError(
                f"GeoJSON file not found: {path}. Run scripts/download_geojson.py first."
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            features = data["features"]
        except (ValueError, KeyError, TypeError) as exc:
            raise OblastDataError(f"GeoJSON file {path} is not valid: {exc}") from exc

        self.names: list[str] = []
        geoms = []
        for feature in features:
            iso = (feature.get("properties") or {}).get("shapeISO")
            name = ISO_TO_NAME.get(iso)
            if name is None:
                log.warning("Skip GeoJSON feature with unknown ISO code %r.", iso)
                continue
            geom = shape(feature["geometry"])
            if not geom.is_valid:
                geom = shapely.make_valid(geom)
            shapely.prepare(geom)
            self.names.append(name)
            geoms.append(geom)
        if not geoms:
            raise OblastDataError(f"GeoJSON file {path} has no usable features.")
        self.geoms = geoms
        self.tree = shapely.STRtree(geoms)
        log.info("Loaded %d oblast polygons from %s.", len(geoms), path)

    def _nearby(self, lat: float, lon: float) -> list[int]:
        margin = max(self.border_margin_m, self.outside_margin_m)
        dlat = margin / METRES_PER_DEGREE * 1.5
        dlon = dlat / max(math.cos(math.radians(lat)), 0.01)
        return [int(i) for i in self.tree.query(box(lon - dlon, lat - dlat, lon + dlon, lat + dlat))]

    def _distance_m(self, idx: int, point: Point, lat: float) -> float:
        geom = self.geoms[idx]
        target = geom.boundary if geom.covers(point) else geom
        return _metres(lat, nearest_points(target, point)[0], point)

    def resolve(self, lat: float, lon: float, current: str | None = None) -> Resolution:
        point = Point(lon, lat)
        near = self._nearby(lat, lon)
        inside = next((i for i in near if self.geoms[i].covers(point)), None)
        dists = {i: self._distance_m(i, point, lat) for i in near}
        candidates = sorted(
            {self.names[i] for i, d in dists.items() if d <= self.border_margin_m}
        )

        if inside is not None:
            others = [d for i, d in dists.items() if i != inside]
            to_other = min(others) if others else None
            name = self.names[inside]
            if current and current != name:
                cur_idx = next((i for i in near if self.names[i] == current), None)
                if cur_idx is not None and dists[cur_idx] <= self.border_margin_m:
                    return Resolution(current, NEAR_BORDER, dists[cur_idx], candidates)
            near_border = dists[inside] <= self.border_margin_m or (
                to_other is not None and to_other <= self.border_margin_m
            )
            status = NEAR_BORDER if near_border else INSIDE
            return Resolution(name, status, dists[inside], candidates)

        if dists:
            best = min(dists, key=dists.get)
            if dists[best] <= self.outside_margin_m:
                return Resolution(self.names[best], OUTSIDE_NEARBY, dists[best], candidates)
        return Resolution(None, OUTSIDE_UKRAINE, None, candidates)
