from __future__ import annotations

import hashlib
import html
import importlib.util
import json
import os
import re
import subprocess
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

BASE = Path(os.environ.get("BASE_BUILDER", "/tmp/base_builder.py"))
spec = importlib.util.spec_from_file_location("base_builder", BASE)
if spec is None or spec.loader is None:
    raise RuntimeError(f"cannot import {BASE}")
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)

GEOFABRIK = {
    "北海道": "https://download.geofabrik.de/asia/japan/hokkaido-latest.osm.pbf",
    "東北": "https://download.geofabrik.de/asia/japan/tohoku-latest.osm.pbf",
    "関東": "https://download.geofabrik.de/asia/japan/kanto-latest.osm.pbf",
    "中部": "https://download.geofabrik.de/asia/japan/chubu-latest.osm.pbf",
    "関西": "https://download.geofabrik.de/asia/japan/kansai-latest.osm.pbf",
    "中国": "https://download.geofabrik.de/asia/japan/chugoku-latest.osm.pbf",
    "四国": "https://download.geofabrik.de/asia/japan/shikoku-latest.osm.pbf",
    "九州": "https://download.geofabrik.de/asia/japan/kyushu-latest.osm.pbf",
}
OHM_STATE = "https://s3.amazonaws.com/planet.openhistoricalmap.org/planet/state.txt"
OHM_BUCKET = "https://s3.amazonaws.com/planet.openhistoricalmap.org"
LINE_VALUES = {
    "rail", "tram", "light_rail", "subway", "narrow_gauge", "monorail",
    "funicular", "preserved", "miniature", "abandoned", "disused",
    "razed", "dismantled", "demolished", "removed", "historic",
}
HIST_VALUES = {
    "abandoned", "disused", "razed", "dismantled", "demolished",
    "removed", "historic",
}
LIFECYCLE_KEYS = [
    "abandoned:railway", "disused:railway", "demolished:railway",
    "razed:railway", "dismantled:railway", "removed:railway",
    "former:railway", "was:railway",
]
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": b.UA})


def decode_json(raw: bytes):
    for enc in ("utf-8-sig", "cp932", "shift_jis"):
        try:
            return json.loads(raw.decode(enc))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    return json.loads(raw.decode("utf-8", errors="replace"))


def patched_read_features(path: Path):
    if path.suffix.lower() in {".json", ".geojson"}:
        return decode_json(path.read_bytes()).get("features", [])
    return b.load_shapefile(path)


b.read_features = patched_read_features


def run(cmd):
    cmd = [str(x) for x in cmd]
    b.log("RUN " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def download(url: str, dest: Path, min_bytes=100):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size >= min_bytes:
        return dest
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(5):
        try:
            b.log(f"DOWNLOAD {url}")
            with SESSION.get(url, stream=True, timeout=(30, 3600)) as r:
                r.raise_for_status()
                with tmp.open("wb") as f:
                    for chunk in r.iter_content(1024 * 1024):
                        if chunk:
                            f.write(chunk)
            if tmp.stat().st_size < min_bytes:
                raise RuntimeError("download too small")
            tmp.replace(dest)
            return dest
        except Exception as exc:
            b.log(f"download retry {attempt + 1}: {exc}")
            tmp.unlink(missing_ok=True)
            if attempt == 4:
                raise
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("download failed")


def filter_export(input_pbf: Path, stem: Path):
    filtered = stem.with_suffix(".filtered.pbf")
    seq = stem.with_suffix(".geojsonseq")
    filters = ["w/railway"] + [f"w/{key}" for key in LIFECYCLE_KEYS]
    run(["osmium", "tags-filter", input_pbf, *filters,
         "-o", filtered, "--overwrite"])
    run(["osmium", "export", filtered, "-f", "geojsonseq",
         "-o", seq, "--overwrite"])
    return filtered, seq


def process_seq(seq: Path, source: str, region_hint=""):
    features = []
    seen = set()
    with seq.open(encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip("\x1e\r\n ")
            if not raw:
                continue
            try:
                feat = json.loads(raw)
            except json.JSONDecodeError:
                continue
            props = feat.get("properties") or {}
            railway = str(props.get("railway") or "")
            lifecycle = next(
                (key for key in LIFECYCLE_KEYS
                 if props.get(key) not in (None, "")),
                "",
            )
            if railway not in LINE_VALUES and not lifecycle:
                continue
            historical = (
                railway in HIST_VALUES or bool(lifecycle)
                or bool(props.get("end_date"))
            )
            status = "歴史・廃止等" if historical else "現存・状態不明"
            ident = str(props.get("@id") or props.get("id") or "")
            name = (
                props.get("name:ja") or props.get("name")
                or props.get("old_name") or ident or "名称不明"
            )
            rows = [f"出典: {html.escape(source)}", f"ID: {html.escape(ident)}"]
            for key in [
                "railway", "name", "name:ja", "old_name", "operator",
                "usage", "service", "start_date", "end_date",
                *LIFECYCLE_KEYS,
            ]:
                if props.get(key) not in (None, ""):
                    rows.append(
                        f"{html.escape(key)}: {html.escape(str(props[key]))}"
                    )
            for coords in b.iter_geojson_lines(feat.get("geometry")):
                signature = ";".join(
                    f"{lon:.6f},{lat:.6f}" for lon, lat in coords
                )
                digest = hashlib.blake2b(
                    signature.encode(), digest_size=16
                ).digest()
                if digest in seen:
                    continue
                seen.add(digest)
                item = b.feature(
                    str(name), coords, "<br>".join(rows), status,
                    source, props,
                )
                if region_hint:
                    item["region"] = region_hint
                features.append(item)
    return features


def load_osm():
    root = b.WORK / "pbf_osm"
    root.mkdir(exist_ok=True)
    all_features = []
    details = {}
    for region, url in GEOFABRIK.items():
        input_pbf = download(url, root / f"{region}.osm.pbf", 100000)
        filtered, seq = filter_export(input_pbf, root / f"{region}.railways")
        features = process_seq(seq, "OpenStreetMap", region)
        all_features.extend(features)
        details[region] = {
            "url": url,
            "ways": len(features),
            "download_bytes": input_pbf.stat().st_size,
        }
        for path in (input_pbf, filtered, seq):
            Path(path).unlink(missing_ok=True)
        b.log(f"OSM {region}: {len(features)}; total={len(all_features)}")
    b.REPORT["sources"]["OpenStreetMap"] = {
        "ways": len(all_features),
        "regions": details,
        "license": "ODbL 1.0",
    }
    return all_features


def latest_ohm_url():
    state = SESSION.get(OHM_STATE, timeout=90).text.strip()
    if state.endswith(".osm.pbf") and "full-history" not in state:
        return state.replace("http://", "https://")
    listing = SESSION.get(OHM_BUCKET + "?prefix=planet/", timeout=180).text
    keys = re.findall(r"<Key>(planet/planet-[^<]+?\.osm\.pbf)</Key>", listing)
    keys = [key for key in keys if "full-history" not in key]
    if not keys:
        raise RuntimeError("OHM planet URL not found")
    return OHM_BUCKET + "/" + sorted(keys)[-1]


def load_ohm():
    root = b.WORK / "pbf_ohm"
    root.mkdir(exist_ok=True)
    url = latest_ohm_url()
    planet = download(url, root / "planet.osm.pbf", 1000000)
    japan = root / "japan.osm.pbf"
    run([
        "osmium", "extract", "-b", "121.0,23.0,155.5,47.7",
        planet, "-o", japan, "--overwrite", "--strategy=smart",
    ])
    filtered, seq = filter_export(japan, root / "japan.railways")
    features = process_seq(seq, "OpenHistoricalMap")
    b.REPORT["sources"]["OpenHistoricalMap"] = {
        "ways": len(features),
        "planet_url": url,
        "planet_bytes": planet.stat().st_size,
        "license": "CC0 except where noted",
    }
    for path in (planet, japan, filtered, seq):
        Path(path).unlink(missing_ok=True)
    b.log(f"OHM: {len(features)}")
    return features


def sanitized_naebo(layer_root: Path):
    links = []
    total = 0
    for label, url in b.NAEBO.items():
        dest = layer_root / "naeboworks" / (label.replace("・", "_") + ".kml")
        try:
            download(url, dest, 100)
            text = dest.read_text(encoding="utf-8", errors="replace")
            text = re.sub(
                r"<(?:(?:\w+):)?href>\s*https?://.*?</(?:(?:\w+):)?href>",
                "<href></href>", text, flags=re.I | re.S,
            )
            dest.write_text(text, encoding="utf-8")
            ET.fromstring(text.encode())
            count = len(re.findall(r"<(?:\w+:)?LineString\b", text, re.I))
            total += count
            links.append((
                f"苗穂工房・{label}",
                str(dest.relative_to(layer_root.parent)).replace(os.sep, "/"),
                count,
            ))
        except Exception as exc:
            b.log(f"Naebo {label} failed: {exc}")
    b.REPORT["sources"]["NaeboWorks"] = {
        "downloaded_regions": len(links),
        "embedded_lines_reported": total,
        "license": "CC BY 4.0",
    }
    return links


def station_writer(path: Path, stations):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(b.kml_doc_start("国交省・廃止駅等"))
        f.write(
            '<Style id="station"><IconStyle><scale>0.55</scale></IconStyle>'
            '<LabelStyle><scale>0</scale></LabelStyle></Style>'
        )
        for station in stations:
            lon, lat = station["coord"]
            f.write(
                f'<Placemark><name>{b.esc(station["name"])}</name>'
                f'<styleUrl>#station</styleUrl><Point><coordinates>'
                f'{lon},{lat},0</coordinates></Point></Placemark>\n'
            )
        f.write("</Document></kml>")
    return len(stations)


b.download_naebo = sanitized_naebo
b.write_station_kml = station_writer

mlit, stations = b.load_mlit()
b.log(f"MLIT: {len(mlit)}")
osm = load_osm()
ohm = load_ohm()
created = b.build_kmz(mlit, stations, osm, ohm)
target = b.OUT / "Japan_Historical_Railways_Offline_MaxCoverage.kmz"
if created != target:
    created.replace(target)

external = []
xml_errors = []
line_count = point_count = kml_count = 0
with zipfile.ZipFile(target) as archive:
    for name in archive.namelist():
        if not name.lower().endswith(".kml"):
            continue
        kml_count += 1
        data = archive.read(name)
        try:
            ET.fromstring(data)
        except Exception as exc:
            xml_errors.append(f"{name}: {exc}")
        line_count += len(re.findall(br"<(?:\w+:)?LineString\b", data, re.I))
        point_count += len(re.findall(br"<(?:\w+:)?Point\b", data, re.I))
        for value in re.findall(
            br"<(?:\w+:)?href>(.*?)</(?:\w+:)?href>",
            data, re.I | re.S,
        ):
            href = value.decode("utf-8", errors="replace").strip()
            if re.match(r"(?i)https?://", href):
                external.append({"file": name, "href": href})

audit = {
    "kmz_bytes": target.stat().st_size,
    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    "kml_files": kml_count,
    "LineString": line_count,
    "Point": point_count,
    "xml_errors": xml_errors,
    "external_hrefs": external,
}
b.REPORT["offline_audit"] = audit
(b.OUT / "coverage_report.json").write_text(
    json.dumps(b.REPORT, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
if not mlit or not osm or not ohm or line_count <= 0 or xml_errors or external:
    raise RuntimeError("offline audit failed: " + json.dumps(audit, ensure_ascii=False))
print(json.dumps(audit, ensure_ascii=False, indent=2), flush=True)
