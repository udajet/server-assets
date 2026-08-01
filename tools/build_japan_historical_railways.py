#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import hashlib
import html
import json
import math
import os
import re
import sys
import time
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import requests

ROOT = Path.cwd()
WORK = ROOT / "work_historical_railways"
OUT = ROOT / "out_historical_railways"
WORK.mkdir(exist_ok=True)
OUT.mkdir(exist_ok=True)

UA = "JapanHistoricalRailwaysKMZ/1.0 (public-data integration build)"
HEADERS = {"User-Agent": UA}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

MLIT_URLS = [
    "https://nlftp.mlit.go.jp/ksj/gml/data/N05/N05-25/N05-25_GML.zip",
    "https://nlftp.mlit.go.jp/ksj/gml/data/N05/N05-24/N05-24_GML.zip",
]
NAEBO = {
    "北海道": "https://naeboworks.com/haisen/_hokkaido.kml",
    "東北": "https://naeboworks.com/haisen/_touhoku.kml",
    "関東": "https://naeboworks.com/haisen/_kantou.kml",
    "信越": "https://naeboworks.com/haisen/_shinetsu.kml",
    "東海": "https://naeboworks.com/haisen/_toukai.kml",
    "北陸": "https://naeboworks.com/haisen/_hokuriku.kml",
    "関西": "https://naeboworks.com/haisen/_kansai.kml",
    "中国・四国": "https://naeboworks.com/haisen/_chugoku.kml",
    "九州": "https://naeboworks.com/haisen/_kyushu.kml",
}
OSM_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
OHM_ENDPOINTS = ["https://overpass-api.openhistoricalmap.org/api/interpreter"]
TRACK_RE = "^(rail|tram|light_rail|subway|narrow_gauge|monorail|funicular|preserved|abandoned|disused|razed|dismantled|historic)$"
LIFECYCLE_KEYS = [
    "abandoned:railway", "disused:railway", "demolished:railway",
    "razed:railway", "removed:railway", "former:railway", "was:railway",
]
HIST_VALUES = {"abandoned", "disused", "razed", "dismantled", "historic"}

REGION_BOXES = [
    (24.0, 122.0, 29.0, 132.0),
    (29.0, 128.0, 35.0, 136.0),
    (33.0, 134.0, 39.0, 142.0),
    (37.0, 138.0, 43.0, 146.0),
    (41.0, 139.0, 46.5, 146.5),
]

REPORT: dict[str, Any] = {
    "title": "Japan public-data maximum-coverage historical railway KMZ",
    "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "important_limitation": "No public dataset can prove literal completeness for every railway ever built. This build integrates all retrievable features from the listed sources.",
    "sources": {},
    "counts": defaultdict(int),
    "failed_tiles": [],
    "samples": defaultdict(list),
}


def log(msg: str) -> None:
    print(msg, flush=True)


def download(url: str, dest: Path, timeout: int = 180) -> Path:
    if dest.exists() and dest.stat().st_size > 100:
        return dest
    log(f"download {url}")
    with SESSION.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        with tmp.open("wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)
        tmp.replace(dest)
    return dest


def esc(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def clean_name(v: Any, fallback: str = "名称不明") -> str:
    s = re.sub(r"\s+", " ", str(v or "")).strip()
    return s if s else fallback


def year_value(v: Any) -> int | None:
    if v is None:
        return None
    m = re.search(r"(18|19|20)\d{2}", str(v))
    return int(m.group(0)) if m else None


def avg_coord(coords: list[tuple[float, float]]) -> tuple[float, float]:
    if not coords:
        return (0.0, 0.0)
    return (sum(x for x, _ in coords) / len(coords), sum(y for _, y in coords) / len(coords))


def japan_region(coords: list[tuple[float, float]]) -> str:
    lon, lat = avg_coord(coords)
    if lat >= 41.0:
        return "北海道"
    if lat >= 37.0 and lon >= 138.0:
        return "東北"
    if 35.0 <= lat < 37.8 and lon >= 138.0:
        return "関東"
    if lat >= 35.0 and 135.0 <= lon < 139.0:
        return "中部・北陸"
    if lat >= 33.5 and 134.0 <= lon < 136.5:
        return "関西"
    if lat >= 33.0 and lon < 134.5:
        return "中国"
    if 32.0 <= lat < 34.8 and 132.0 <= lon <= 135.0:
        return "四国"
    if lat < 29.5:
        return "沖縄・南西諸島"
    if lat < 34.0 and lon < 133.0:
        return "九州"
    return "その他"


def reduce_coords(coords: Iterable[Iterable[Any]], min_delta: float = 0.000003) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    last: tuple[float, float] | None = None
    for p in coords:
        try:
            x, y = float(p[0]), float(p[1])
        except Exception:
            continue
        if not (121.0 <= x <= 155.5 and 23.0 <= y <= 47.5):
            continue
        cur = (round(x, 7), round(y, 7))
        if last is None or abs(cur[0] - last[0]) + abs(cur[1] - last[1]) >= min_delta:
            out.append(cur)
            last = cur
    if len(out) >= 2:
        return out
    return []


def iter_geojson_lines(geom: dict[str, Any] | None) -> Iterable[list[tuple[float, float]]]:
    if not geom:
        return
    typ = geom.get("type")
    coords = geom.get("coordinates")
    if typ == "LineString":
        c = reduce_coords(coords or [])
        if c:
            yield c
    elif typ == "MultiLineString":
        for part in coords or []:
            c = reduce_coords(part)
            if c:
                yield c
    elif typ == "GeometryCollection":
        for sub in geom.get("geometries", []):
            yield from iter_geojson_lines(sub)


def feature(name: str, coords: list[tuple[float, float]], desc: str, status: str, source: str, tags: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "name": clean_name(name), "coords": coords, "desc": desc,
        "status": status, "source": source, "tags": tags or {},
        "region": japan_region(coords),
    }


def find_member(base: Path, pattern: str) -> Path | None:
    items = [p for p in base.rglob("*") if p.is_file() and pattern.lower() in p.name.lower()]
    preferred = [p for p in items if p.suffix.lower() in {".geojson", ".json"}]
    return (preferred or items or [None])[0]


def load_shapefile(path: Path) -> list[dict[str, Any]]:
    import shapefile  # pyshp
    sf = shapefile.Reader(str(path), encoding="utf-8")
    fields = [f[0] for f in sf.fields[1:]]
    out = []
    for sr in sf.iterShapeRecords():
        props = dict(zip(fields, sr.record))
        parts = list(sr.shape.parts) + [len(sr.shape.points)]
        if sr.shape.shapeType in (3, 13, 23):
            lines = [sr.shape.points[parts[i]:parts[i + 1]] for i in range(len(parts) - 1)]
            geom = {"type": "MultiLineString", "coordinates": lines}
        elif sr.shape.shapeType in (1, 11, 21):
            geom = {"type": "Point", "coordinates": sr.shape.points[0]}
        else:
            continue
        out.append({"type": "Feature", "properties": props, "geometry": geom})
    return out


def read_features(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() in {".json", ".geojson"}:
        with path.open(encoding="utf-8") as f:
            obj = json.load(f)
        return obj.get("features", [])
    if path.suffix.lower() == ".shp":
        return load_shapefile(path)
    raise RuntimeError(f"unsupported data file {path}")


def load_mlit() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    zpath = WORK / "mlit_N05.zip"
    used_url = None
    for url in MLIT_URLS:
        try:
            download(url, zpath)
            used_url = url
            break
        except Exception as e:
            log(f"MLIT fallback after {e}")
            zpath.unlink(missing_ok=True)
    if not used_url:
        raise RuntimeError("MLIT N05 download failed")
    extract = WORK / "mlit_N05"
    if not extract.exists():
        with zipfile.ZipFile(zpath) as z:
            z.extractall(extract)
    rail_path = find_member(extract, "RailroadSection2")
    station_path = find_member(extract, "Station2")
    if not rail_path:
        raise RuntimeError("RailroadSection2 not found")
    rail_raw = read_features(rail_path)
    station_raw = read_features(station_path) if station_path else []
    rails: list[dict[str, Any]] = []
    stations: list[dict[str, Any]] = []
    for f in rail_raw:
        p = f.get("properties") or {}
        line = p.get("N05_002") or p.get("路線名") or p.get("line_name")
        operator = p.get("N05_001") or p.get("事業者名") or p.get("operator")
        start = p.get("N05_005b") or p.get("N05_004")
        end = p.get("N05_005e") or p.get("N05_005")
        end_year = year_value(end)
        current = str(end).strip() in {"9999", "999", "", "None"} or end_year is None
        status = "現存・時系列収録" if current else "廃止・変更"
        for coords in iter_geojson_lines(f.get("geometry")):
            desc = f"出典: 国土交通省 国土数値情報（鉄道時系列）<br>事業者: {esc(operator)}<br>路線: {esc(line)}<br>開始: {esc(start)}<br>終了: {esc(end)}"
            rails.append(feature(f"{clean_name(line)} / {clean_name(operator, '事業者不明')}", coords, desc, status, "MLIT", p))
    for f in station_raw:
        p = f.get("properties") or {}
        geom = f.get("geometry") or {}
        if geom.get("type") != "Point":
            continue
        c = reduce_coords([geom.get("coordinates", [])], 0)
        if not c:
            continue
        end = p.get("N05_005e") or p.get("N05_005")
        if str(end).strip() in {"9999", "999", "", "None"}:
            continue
        stations.append({
            "name": clean_name(p.get("N05_011") or p.get("駅名") or p.get("station_name")),
            "coord": c[0], "region": japan_region(c), "tags": p,
        })
    REPORT["sources"]["MLIT"] = {"url": used_url, "rail_features": len(rails), "closed_stations": len(stations)}
    return rails, stations


def grid_boxes(step: float = 2.5) -> list[tuple[float, float, float, float]]:
    boxes = []
    for s, w, n, e in REGION_BOXES:
        lat = s
        while lat < n:
            lon = w
            while lon < e:
                boxes.append((lat, lon, min(lat + step, n), min(lon + step, e)))
                lon += step
            lat += step
    # remove exact duplicates caused by overlapping regional coverage
    return sorted(set(tuple(round(v, 3) for v in b) for b in boxes))


def overpass_query(bbox: tuple[float, float, float, float]) -> str:
    s, w, n, e = bbox
    parts = [f'way["railway"~"{TRACK_RE}"]({s},{w},{n},{e});']
    parts.extend(f'way["{k}"]({s},{w},{n},{e});' for k in LIFECYCLE_KEYS)
    return "[out:json][timeout:240][maxsize:1073741824];(" + "".join(parts) + ");out tags geom qt;"


def split_box(b: tuple[float, float, float, float]) -> list[tuple[float, float, float, float]]:
    s, w, n, e = b
    mlat, mlon = (s + n) / 2, (w + e) / 2
    return [(s, w, mlat, mlon), (s, mlon, mlat, e), (mlat, w, n, mlon), (mlat, mlon, n, e)]


def fetch_overpass_box(source: str, endpoints: list[str], bbox: tuple[float, float, float, float], depth: int = 0) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    query = overpass_query(bbox)
    errors = []
    for attempt in range(4):
        ep = endpoints[attempt % len(endpoints)]
        try:
            r = requests.post(ep, data={"data": query}, headers=HEADERS, timeout=300)
            if r.status_code == 429:
                time.sleep(15 + attempt * 10)
                continue
            r.raise_for_status()
            obj = r.json()
            if obj.get("remark") and not obj.get("elements"):
                raise RuntimeError(obj["remark"])
            return obj.get("elements", []), []
        except Exception as exc:
            errors.append(f"{ep}: {type(exc).__name__}: {exc}")
            time.sleep(3 + attempt * 3)
    s, w, n, e = bbox
    if depth < 3 and max(n - s, e - w) > 0.45:
        elements: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for sub in split_box(bbox):
            e2, f2 = fetch_overpass_box(source, endpoints, sub, depth + 1)
            elements.extend(e2)
            failures.extend(f2)
        return elements, failures
    return [], [{"source": source, "bbox": bbox, "errors": errors}]


def load_overpass(source: str, endpoints: list[str]) -> list[dict[str, Any]]:
    boxes = grid_boxes()
    all_elements: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futures = {ex.submit(fetch_overpass_box, source, endpoints, b): b for b in boxes}
        for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            b = futures[fut]
            try:
                els, fails = fut.result()
                all_elements.extend(els)
                failures.extend(fails)
            except Exception as exc:
                failures.append({"source": source, "bbox": b, "errors": [repr(exc)]})
            log(f"{source}: {i}/{len(boxes)} tiles, raw elements={len(all_elements)}")
    by_id: dict[int, dict[str, Any]] = {}
    for el in all_elements:
        if el.get("type") == "way" and el.get("id") is not None:
            by_id[int(el["id"])] = el
    features: list[dict[str, Any]] = []
    for el in by_id.values():
        tags = el.get("tags") or {}
        geom = el.get("geometry") or []
        coords = reduce_coords([(p.get("lon"), p.get("lat")) for p in geom])
        if not coords:
            continue
        railway = str(tags.get("railway") or "")
        lifecycle = next((k for k in LIFECYCLE_KEYS if k in tags), "")
        historical = railway in HIST_VALUES or bool(lifecycle) or bool(tags.get("end_date"))
        status = "歴史・廃止等" if historical else "現存・状態不明"
        name = tags.get("name:ja") or tags.get("name") or tags.get("old_name") or f"{source} way {el['id']}"
        rows = [f"出典: {esc(source)}", f"OSM/OHM way: {el['id']}"]
        for key in ["railway", "name", "old_name", "operator", "usage", "service", "start_date", "end_date", "abandoned:railway", "disused:railway", "razed:railway", "demolished:railway", "removed:railway"]:
            if key in tags:
                rows.append(f"{esc(key)}: {esc(tags[key])}")
        features.append(feature(str(name), coords, "<br>".join(rows), status, source, tags))
    REPORT["failed_tiles"].extend(failures)
    REPORT["sources"][source] = {"ways": len(features), "unique_raw_ways": len(by_id), "failed_subtiles": len(failures), "endpoints": endpoints}
    return features


def collect_samples(all_features: list[dict[str, Any]]) -> None:
    terms = ["山梨交通", "甲府", "汐留", "芝浦", "新橋", "森林鉄道", "鉱山", "軍用"]
    for term in terms:
        matches = []
        for f in all_features:
            hay = f["name"] + " " + json.dumps(f.get("tags", {}), ensure_ascii=False)
            if term.lower() in hay.lower():
                matches.append({"name": f["name"], "source": f["source"], "status": f["status"]})
            if len(matches) >= 20:
                break
        REPORT["samples"][term] = matches


def kml_doc_start(name: str, description: str = "") -> str:
    return f'''<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>{esc(name)}</name><description><![CDATA[{description}]]></description>\n'''


def style_xml(style_id: str, color: str, width: float = 2.0) -> str:
    return f'<Style id="{style_id}"><LineStyle><color>{color}</color><width>{width}</width></LineStyle><PolyStyle><fill>0</fill></PolyStyle></Style>\n'


def write_line_kml(path: Path, name: str, feats: list[dict[str, Any]], color: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as f:
        f.write(kml_doc_start(name, "クリックすると出典・タグを表示します。"))
        f.write(style_xml("line", color, 2.2))
        for x in feats:
            coords = x["coords"]
            if len(coords) < 2:
                continue
            coord_text = " ".join(f"{lon:.7f},{lat:.7f},0" for lon, lat in coords)
            f.write(f'<Placemark><name>{esc(x["name"])}</name><description><![CDATA[{x["desc"]}]]></description><styleUrl>#line</styleUrl><LineString><tessellate>1</tessellate><altitudeMode>clampToGround</altitudeMode><coordinates>{coord_text}</coordinates></LineString></Placemark>\n')
            count += 1
        f.write("</Document></kml>")
    return count


def write_station_kml(path: Path, stations: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(kml_doc_start("国交省・廃止駅等"))
        f.write('<Style id="station"><IconStyle><scale>0.55</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/rail.png</href></Icon></IconStyle><LabelStyle><scale>0</scale></LabelStyle></Style>')
        for s in stations:
            lon, lat = s["coord"]
            f.write(f'<Placemark><name>{esc(s["name"])}</name><styleUrl>#station</styleUrl><Point><coordinates>{lon},{lat},0</coordinates></Point></Placemark>\n')
        f.write("</Document></kml>")
    return len(stations)


def download_naebo(layer_root: Path) -> list[tuple[str, str, int]]:
    links = []
    for label, url in NAEBO.items():
        dest = layer_root / "naeboworks" / (label.replace("・", "_") + ".kml")
        try:
            download(url, dest)
            text = dest.read_text(encoding="utf-8", errors="ignore")
            lines = len(re.findall(r"<LineString\b", text, flags=re.I))
            links.append((f"苗穂工房・{label}", str(dest.relative_to(layer_root.parent)).replace(os.sep, "/"), lines))
        except Exception as e:
            log(f"naeboworks {label} failed: {e}")
    REPORT["sources"]["NaeboWorks"] = {"downloaded_regions": len(links), "embedded_lines_reported": sum(x[2] for x in links), "license": "CC BY 4.0"}
    return links


def build_kmz(mlit: list[dict[str, Any]], stations: list[dict[str, Any]], osm: list[dict[str, Any]], ohm: list[dict[str, Any]]) -> Path:
    bundle = WORK / "bundle"
    layers = bundle / "layers"
    bundle.mkdir(exist_ok=True)
    links: list[tuple[str, str, int, int]] = []  # name, href, visibility, count
    configs = [
        ("MLIT", mlit, "ff0000ff", True),
        ("OpenStreetMap", osm, "ff00a5ff", True),
        ("OpenHistoricalMap", ohm, "ffff00ff", True),
    ]
    for source, feats, color, hist_visible in configs:
        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for x in feats:
            groups[(x["status"], x["region"])].append(x)
        for (status, region), items in sorted(groups.items()):
            safe = re.sub(r"[^0-9A-Za-zぁ-んァ-ン一-龥_-]+", "_", f"{source}_{status}_{region}")
            path = layers / source / f"{safe}.kml"
            count = write_line_kml(path, f"{source}｜{status}｜{region}", items, color)
            visible = 1 if (hist_visible and status != "現存・時系列収録" and status != "現存・状態不明") else 0
            href = str(path.relative_to(bundle)).replace(os.sep, "/")
            links.append((f"{source}｜{status}｜{region}（{count:,}線）", href, visible, count))
    if stations:
        p = layers / "MLIT" / "closed_stations.kml"
        count = write_station_kml(p, stations)
        links.append((f"MLIT｜廃止駅等（{count:,}点）", str(p.relative_to(bundle)).replace(os.sep, "/"), 0, 0))
    naebo_links = download_naebo(layers)
    for n, href, count in naebo_links:
        links.append((f"{n}（内部線数 {count:,}）", href, 1, count))

    description = """
    <h2>日本の歴代鉄道路線・公開データ最大統合版</h2>
    <p>国土交通省の鉄道時系列、OpenStreetMap、OpenHistoricalMap、苗穂工房の公開KMLを、オフラインで開ける単一KMZに収録しています。</p>
    <p><b>重要:</b> 「日本で存在した全路線」を証明できる完全な台帳は存在しません。本ファイルは公開・取得可能なデータを最大限統合したもので、短命な工事軌道、企業構内線、軍事機密線、未記録の森林・鉱山軌道等にはなお欠落し得ます。</p>
    <p>既定では廃止・歴史レイヤーを表示し、現存線は非表示です。左側のチェックボックスで切り替えてください。</p>
    <p>出典: 国土交通省 国土数値情報（鉄道時系列）／OpenStreetMap contributors (ODbL)／OpenHistoricalMap contributors (CC0)／苗穂工房 (CC BY 4.0)。</p>
    """
    master = bundle / "doc.kml"
    with master.open("w", encoding="utf-8") as f:
        f.write(kml_doc_start("日本の歴代鉄道路線・公開データ最大統合版", description))
        f.write('<LookAt><longitude>137.5</longitude><latitude>37.0</latitude><range>2600000</range></LookAt>')
        folders: dict[str, list[tuple[str, str, int, int]]] = defaultdict(list)
        for link in links:
            source = link[0].split("｜", 1)[0].split("・", 1)[0]
            folders[source].append(link)
        for source, items in folders.items():
            f.write(f'<Folder><name>{esc(source)}</name><open>0</open>')
            for name, href, vis, count in items:
                f.write(f'<NetworkLink><name>{esc(name)}</name><visibility>{vis}</visibility><Link><href>{esc(href)}</href></Link></NetworkLink>')
            f.write('</Folder>')
        f.write('</Document></kml>')

    collect_samples(mlit + osm + ohm)
    REPORT["counts"] = dict(REPORT["counts"])
    REPORT["counts"]["MLIT_lines"] = len(mlit)
    REPORT["counts"]["OSM_lines"] = len(osm)
    REPORT["counts"]["OHM_lines"] = len(ohm)
    REPORT["counts"]["MLIT_closed_stations"] = len(stations)
    REPORT["counts"]["master_linked_layers"] = len(links)
    report_path = bundle / "coverage_report.json"
    report_path.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2), encoding="utf-8")
    readme = bundle / "README.txt"
    readme.write_text(
        "日本の歴代鉄道路線・公開データ最大統合版\n\n"
        "Google Earth ProでKMZを開いてください。廃止・歴史レイヤーは既定でON、現存線はOFFです。\n"
        "完全性の保証はありません。詳細はcoverage_report.jsonを参照してください。\n",
        encoding="utf-8",
    )
    out = OUT / "Japan_PublicData_Maximum_Historical_Railways.kmz"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in bundle.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(bundle).as_posix())
    # Audit actual embedded geometry.
    total_lines = 0
    total_points = 0
    with zipfile.ZipFile(out) as z:
        for n in z.namelist():
            if n.lower().endswith(".kml"):
                data = z.read(n)
                total_lines += len(re.findall(br"<LineString\b", data, flags=re.I))
                total_points += len(re.findall(br"<Point\b", data, flags=re.I))
    REPORT["audit"] = {
        "kmz_bytes": out.stat().st_size,
        "embedded_kml_files": len([p for p in bundle.rglob("*.kml")]),
        "actual_LineString_elements": total_lines,
        "actual_Point_elements": total_points,
        "sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
    }
    (OUT / "coverage_report.json").write_text(json.dumps(REPORT, ensure_ascii=False, indent=2), encoding="utf-8")
    if len(mlit) == 0 or len(osm) == 0 or len(ohm) == 0 or total_lines == 0:
        raise RuntimeError(f"required source empty: MLIT={len(mlit)}, OSM={len(osm)}, OHM={len(ohm)}, lines={total_lines}")
    log(json.dumps(REPORT["audit"], ensure_ascii=False, indent=2))
    return out


def main() -> None:
    mlit, stations = load_mlit()
    log(f"MLIT lines={len(mlit)}, closed stations={len(stations)}")
    osm = load_overpass("OpenStreetMap", OSM_ENDPOINTS)
    log(f"OSM ways={len(osm)}")
    ohm = load_overpass("OpenHistoricalMap", OHM_ENDPOINTS)
    log(f"OHM ways={len(ohm)}")
    out = build_kmz(mlit, stations, osm, ohm)
    log(f"created {out} ({out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
