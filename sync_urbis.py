#!/usr/bin/env python3
"""
sync_urbis.py
=============

Downloads the latest UrbIS Vector dataset (GPKG format, scale 1:4000, Lambert 72 /
EPSG:31370) published by urbisdownload.datastore.brussels, extracts the GeoPackage,
stores it at /srv/resources/urbis/UrbISVector.gpkg and loads a selection of its
layers into a PostgreSQL/PostGIS database (schema "urbis" of database "mcp_db"),
reprojecting geometries to EPSG:3812 (Belgian Lambert 2008) on the way in.

No system GDAL is required: reading/writing of the GeoPackage is done with
geopandas + pyogrio, whose wheels bundle their own GDAL build. This makes the
script work out of the box inside the "python:3.13-slim" based container
described in /srv/geo-mcp/Containerfile, as long as the packages listed at the
bottom of this file (see REQUIREMENTS_TXT_SNIPPET) are added to requirements.txt.

Typical usage
-------------
    # normal run: try today, then up to 9 days back, load everything into postgres
    python sync_urbis.py --db-password secret

    # only (re)download/extract, skip the database load
    python sync_urbis.py --skip-db

    # do not touch the network at all, reuse the file already on disk
    python sync_urbis.py --skip-download --db-password secret

Database connection parameters are read from an .env file (default:
/srv/geo-mcp/.env, override with --env-file), expected to define:
    PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE
Real environment variables already set when the script runs take precedence
over the .env file, and explicit --db-* CLI flags take precedence over both.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Optional

import requests

log = logging.getLogger("sync_urbis")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

URL_TEMPLATE = (
    "https://urbisdownload.datastore.brussels/UrbIS/Vector/M8/UrbIS-Vector/"
    "GPKG/UrbISVector_31370_GPKG_04000_{date_str}.zip"
)

# Name of the .gpkg file inside the "gpkg/" folder of the zip archive.
INNER_GPKG_NAME = "UrbISVector_04000.gpkg"

DEFAULT_DEST_DIR = Path("/srv/resources/urbis")
DEFAULT_DEST_GPKG = DEFAULT_DEST_DIR / "UrbISVector.gpkg"
DEFAULT_TABLES_SQL = DEFAULT_DEST_DIR / "tables.sql"

SOURCE_CRS = "EPSG:31370"
TARGET_CRS = "EPSG:3812"

# STIB (Brussels public transport) stops, fetched separately from the
# UrbIS GeoPackage -- a live WFS endpoint, not part of the weekly GPKG
# download. Already in EPSG:3812 (srsName requested explicitly), so no
# reprojection needed on the way in. See technical reference doc section
# 3 / revision note 3.txt: used as an extra gazetteer source (type
# TRANSIT_STOP) so prompts referencing a named stop ("the tram stop at
# Meiser") can resolve to a real point, same as a STREET or POI today.
STIB_STOPS_WFS_URL = (
    "https://data.mobility.brussels/geoserver/bm_public_transport/wfs"
    "?service=wfs&version=1.1.0&request=GetFeature"
    "&typeName=bm_public_transport:stib_stops"
    "&outputFormat=json&srsName=EPSG:3812"
)

# STIB line geometries (tram/metro/bus), same WFS server as the stops. Attributes
# used: `line` (line number) and `mode` (only Tram, Metro, Bus are kept).
STIB_LINES_WFS_URL = (
    "https://data.mobility.brussels/geoserver/bm_public_transport/wfs"
    "?service=wfs&version=1.1.0&request=GetFeature"
    "&typeName=bm_public_transport:stib_lines"
    "&outputFormat=json&srsName=EPSG:3812"
)
STIB_LINE_MODES = {"tram": "Tram", "metro": "Metro", "bus": "Bus"}

# Railway lines (Infrabel) from the same server.
INFRABEL_LINES_WFS_URL = (
    "https://data.mobility.brussels/geoserver/bm_public_transport/wfs"
    "?service=wfs&version=1.1.0&request=GetFeature"
    "&typeName=bm_public_transport:infrabel_lines"
    "&outputFormat=json&srsName=EPSG:3812"
)

# OpenStreetMap (Overpass). Several public mirrors are tried in turn; override with
# OVERPASS_URLS="url1,url2". Overpass asks clients to send a descriptive User-Agent.
OVERPASS_URLS = [
    u.strip() for u in os.environ.get(
        "OVERPASS_URLS",
        "https://overpass-api.de/api/interpreter,https://overpass.kumi.systems/api/interpreter,"
        "https://overpass.private.coffee/api/interpreter",
    ).split(",") if u.strip()
]
OSM_OVERPASS_URL = OVERPASS_URLS[0]  # kept for backward compatibility
OVERPASS_USER_AGENT = os.environ.get("OVERPASS_USER_AGENT", "geo-mcp-sync/1.0 (UrbIS gazetteer loader)")
OSM_BRUSSELS_BBOX = (50.76, 4.24, 50.92, 4.48)  # south, west, north, east
OSM_CATEGORY_TAGS = ("amenity", "tourism", "historic", "shop", "leisure", "office", "man_made", "heritage")
OSM_ALIAS_TAGS = ("alt_name", "official_name", "short_name")
OSM_POI_QUERY = """
[out:json][timeout:300];
(
  nwr["name"]["amenity"]({bbox});
  nwr["name"]["tourism"]({bbox});
  nwr["name"]["historic"]({bbox});
  nwr["name"]["shop"]({bbox});
  nwr["name"]["leisure"]({bbox});
  nwr["name"]["office"]({bbox});
  nwr["name"]["man_made"]({bbox});
  nwr["name"]["heritage"]({bbox});
  nwr["name"]["wikidata"]({bbox});
);
out center tags;
""".strip()
OSM_ROAD_CLASSES = ("^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|"
                    "motorway_link|trunk_link|primary_link|secondary_link|tertiary_link)$")
REFERENCE_BUFFER_M = 5000  # belt around the Region for roads / neighbouring municipalities

DEFAULT_DB_NAME = "mcp_db"
DEFAULT_SCHEMA = "urbis"
DEFAULT_ENV_FILE = Path("/srv/geo-mcp/.env")

# Layers that carry a geometry column: loaded with reprojection 31370 -> 3812.
# Scope: only the tables explicitly requested for the geospatial-context
# lookup, plus the tables they reference via foreign key (inspire_id).
GEOMETRY_LAYERS = [
    "StreetSurfaces",       # -> StreetTypeDomain, PlaceNames, StreetHierarchyDomain, LevelDomain, StreetAdministratorDomain
    "StreetAxes",           # -> centreline network topology (STARTSN_ID/ENDSN_ID); used for real routing, see sql/lookup_views.sql's v_street_axes_routing / street_axes_nodes
    "StatisticalSectors",   # -> Municipalities, MonitoringDistricts
    "MonitoringDistricts",
    "Region",
    "PointsOfInterest",     # -> PointOfInterestTypeDomain
    "Addresses",            # -> PlaceNames, Municipalities
    "Municipalities",       # referenced by StatisticalSectors / Addresses
]

# Layers without geometry: loaded as plain tables, no reprojection needed.
ATTRIBUTE_LAYERS = [
    "LevelDomain",
    "PlaceNames",
    "PointOfInterestTypeDomain",
    "StreetAdministratorDomain",
    "StreetTypeDomain",
    "StreetHierarchyDomain",
]


# --------------------------------------------------------------------------- #
# Download / extraction
# --------------------------------------------------------------------------- #

def candidate_dates(days_back: int) -> Iterable[date]:
    """Yield today, then the previous `days_back - 1` days."""
    today = date.today()
    for offset in range(days_back):
        yield today - timedelta(days=offset)


def try_download_zip(day: date, dest_zip: Path, timeout: int = 60) -> bool:
    """Attempt to download the zip for a given date. Returns True on success."""
    url = URL_TEMPLATE.format(date_str=day.strftime("%Y%m%d"))
    log.info("Trying %s", url)
    try:
        with requests.get(url, stream=True, timeout=timeout) as resp:
            if resp.status_code != 200:
                log.info("  -> HTTP %s, not available", resp.status_code)
                return False
            content_type = resp.headers.get("Content-Type", "")
            if "zip" not in content_type and not url.endswith(".zip"):
                log.info("  -> unexpected content-type %r", content_type)
            with open(dest_zip, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        fh.write(chunk)
        # A valid zip must be openable and contain the expected inner file.
        if not zipfile.is_zipfile(dest_zip):
            log.info("  -> downloaded file is not a valid zip")
            dest_zip.unlink(missing_ok=True)
            return False
        log.info("  -> download OK (%s)", url)
        return True
    except requests.RequestException as exc:
        log.info("  -> request failed: %s", exc)
        dest_zip.unlink(missing_ok=True)
        return False


def extract_inner_gpkg(zip_path: Path, extract_to_dir: Path) -> Path:
    """Extract the .../gpkg/UrbISVector_04000.gpkg member from the zip archive."""
    with zipfile.ZipFile(zip_path) as zf:
        member = None
        for name in zf.namelist():
            if name.replace("\\", "/").lower().endswith("gpkg/" + INNER_GPKG_NAME.lower()):
                member = name
                break
        if member is None:
            # Fallback: any file ending with the inner gpkg name, wherever it is.
            for name in zf.namelist():
                if name.lower().endswith(INNER_GPKG_NAME.lower()):
                    member = name
                    break
        if member is None:
            raise FileNotFoundError(
                f"Could not find {INNER_GPKG_NAME} inside {zip_path.name}"
            )
        log.info("Extracting %s", member)
        extracted_path = zf.extract(member, path=extract_to_dir)
        return Path(extracted_path)


def is_valid_gpkg(path: Path) -> bool:
    """Check that a file exists and can be opened as a GeoPackage with data."""
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        import pyogrio

        layers = pyogrio.list_layers(str(path))
        return len(layers) > 0
    except Exception as exc:  # noqa: BLE001
        log.warning("File %s failed GeoPackage validation: %s", path, exc)
        return False


def download_latest_gpkg(
    dest_gpkg: Path,
    days_back: int = 10,
    use_cached: bool = True,
) -> Path:
    """
    Download the most recent available UrbIS Vector zip (trying `days_back`
    days starting today), extract the inner .gpkg, and atomically replace
    `dest_gpkg` with it. Falls back to the existing cached file if every
    download attempt fails.

    Returns the path to the (now up to date) GeoPackage file.
    """
    dest_gpkg.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=dest_gpkg.parent) as tmp_dir:
        tmp_dir_path = Path(tmp_dir)
        for day in candidate_dates(days_back):
            tmp_zip = tmp_dir_path / f"urbis_{day:%Y%m%d}.zip"
            if not try_download_zip(day, tmp_zip):
                continue
            try:
                extracted = extract_inner_gpkg(tmp_zip, tmp_dir_path)
            except Exception as exc:  # noqa: BLE001
                log.warning("Extraction failed for %s: %s", tmp_zip.name, exc)
                continue
            if not is_valid_gpkg(extracted):
                log.warning("Extracted file for %s failed validation", day)
                continue

            # Atomically replace the destination file (same filesystem: tmp_dir
            # was created inside dest_gpkg.parent).
            os.replace(extracted, dest_gpkg)
            _write_sidecar_metadata(dest_gpkg, day)
            size_mb = dest_gpkg.stat().st_size / (1024 * 1024)
            log.info(
                "Saved new GeoPackage (source date %s) to %s (%.1f MB)",
                day,
                dest_gpkg.resolve(),
                size_mb,
            )
            return dest_gpkg

        # No download succeeded: fall back to the cached copy if allowed.
        if use_cached and is_valid_gpkg(dest_gpkg):
            log.warning(
                "No new file could be downloaded in the last %s day(s); "
                "reusing existing file at %s",
                days_back,
                dest_gpkg,
            )
            return dest_gpkg

    raise RuntimeError(
        f"Could not download a valid UrbIS Vector GeoPackage in the last "
        f"{days_back} day(s), and no usable cached file was found at {dest_gpkg}."
    )


def _sidecar_path(dest_gpkg: Path) -> Path:
    return dest_gpkg.with_suffix(dest_gpkg.suffix + ".meta.json")


def _write_sidecar_metadata(dest_gpkg: Path, source_date: date) -> None:
    meta = {"source_date": source_date.isoformat()}
    _sidecar_path(dest_gpkg).write_text(json.dumps(meta))


def use_existing_gpkg(dest_gpkg: Path) -> Path:
    """Validate and return the already-downloaded GeoPackage, without any network call."""
    if not is_valid_gpkg(dest_gpkg):
        raise RuntimeError(
            f"--use-cached / --skip-download was requested but {dest_gpkg} "
            f"does not exist or is not a valid GeoPackage."
        )
    log.info("Using existing cached GeoPackage at %s", dest_gpkg)
    return dest_gpkg


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

@dataclass
class DbConfig:
    host: str
    port: int
    user: str
    password: Optional[str]
    dbname: str
    schema: str

    def admin_dsn(self, admin_db: str = "postgres") -> str:
        return (
            f"host={self.host} port={self.port} user={self.user} "
            f"dbname={admin_db}" + (f" password={self.password}" if self.password else "")
        )

    def dsn(self) -> str:
        return (
            f"host={self.host} port={self.port} user={self.user} "
            f"dbname={self.dbname}" + (f" password={self.password}" if self.password else "")
        )

    def sqlalchemy_url(self) -> str:
        from urllib.parse import quote_plus

        pwd = quote_plus(self.password) if self.password else ""
        auth = f"{self.user}:{pwd}" if pwd else self.user
        return f"postgresql+psycopg2://{auth}@{self.host}:{self.port}/{self.dbname}"


def ensure_database_and_extensions(cfg: DbConfig, retries: int = 3, retry_delay: float = 3.0) -> None:
    """Create the database if needed, then ensure postgis/pgvector extensions and schema exist."""
    import time

    import psycopg2
    from psycopg2 import sql

    def _connect_with_retry(dsn: str):
        last_exc: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                return psycopg2.connect(dsn)
            except psycopg2.OperationalError as exc:  # noqa: PERF203
                last_exc = exc
                if attempt < retries:
                    log.warning(
                        "Could not connect to Postgres (attempt %d/%d): %s. Retrying in %.0fs...",
                        attempt,
                        retries,
                        exc,
                        retry_delay,
                    )
                    time.sleep(retry_delay)
        assert last_exc is not None
        raise RuntimeError(
            f"Could not connect to Postgres at {cfg.host}:{cfg.port} after {retries} attempt(s): "
            f"{last_exc}\n"
            "Hint: if this script is running inside a podman/container, "
            "'localhost' refers to the container itself, not the host machine. "
            "Point PGHOST at the real database host (e.g. its container name on a "
            "shared podman network, or 'host.containers.internal' if the database "
            "runs on the host), or run the container with --network=host."
        ) from last_exc

    # 1) CREATE DATABASE must run outside a transaction, against another database.
    conn = _connect_with_retry(cfg.admin_dsn("postgres"))
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (cfg.dbname,))
            if cur.fetchone() is None:
                log.info("Creating database %s", cfg.dbname)
                cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cfg.dbname)))
            else:
                log.info("Database %s already exists", cfg.dbname)
    finally:
        conn.close()

    # 2) Extensions + schema, inside the target database.
    conn = _connect_with_retry(cfg.dsn())
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            log.info("Ensuring postgis and vector extensions")
            cur.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            cur.execute("CREATE EXTENSION IF NOT EXISTS pgrouting;")
            log.info("Ensuring schema %s", cfg.schema)
            cur.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(cfg.schema))
            )
    finally:
        conn.close()


def _resolve_layer_name(available: list[str], wanted: str) -> Optional[str]:
    for name in available:
        if name.lower() == wanted.lower():
            return name
    return None


def _get_fid_column(gpkg_path: Path, layer: str) -> str:
    """
    Look up the name of the column GDAL is treating as this layer's feature ID
    (primary key). GeoPackage columns declared as INTEGER PRIMARY KEY (e.g.
    "inspire_id") are used internally as the FID and are otherwise silently
    excluded from the regular attribute columns when reading.
    """
    import pyogrio

    info = pyogrio.read_info(str(gpkg_path), layer=layer)
    return info.get("fid_column") or "fid"


def _lowercase_columns(df, geometry: bool = False):
    """Lowercase every column name so Postgres never needs to quote identifiers."""
    if geometry:
        orig_geom_col = df.geometry.name
        df = df.rename(columns=str.lower)
        new_geom_col = orig_geom_col.lower()
        if new_geom_col != df.geometry.name:
            df = df.set_geometry(new_geom_col)
        return df
    df.columns = [str(c).lower() for c in df.columns]
    return df


def _read_geometry_layer(gpkg_path: Path, layer: str):
    """Read a layer with geopandas/pyogrio, restoring the FID column if GDAL stripped it."""
    import geopandas as gpd

    fid_col = _get_fid_column(gpkg_path, layer)
    gdf = gpd.read_file(gpkg_path, layer=layer, engine="pyogrio", fid_as_index=True)
    if fid_col and fid_col.lower() not in [c.lower() for c in gdf.columns]:
        gdf.index.name = fid_col
        gdf = gdf.reset_index()
    else:
        gdf = gdf.reset_index(drop=True)
    return _lowercase_columns(gdf, geometry=True)


def _read_attribute_layer(gpkg_path: Path, layer: str):
    """Read a non-geometry layer with pyogrio, restoring the FID column if GDAL stripped it."""
    import pyogrio

    fid_col = _get_fid_column(gpkg_path, layer)
    df = pyogrio.read_dataframe(gpkg_path, layer=layer, read_geometry=False, fid_as_index=True)
    if fid_col and fid_col.lower() not in [c.lower() for c in df.columns]:
        df.index.name = fid_col
        df = df.reset_index()
    else:
        df = df.reset_index(drop=True)
    return _lowercase_columns(df, geometry=False)


def load_geometry_layers(gpkg_path: Path, cfg: DbConfig) -> list[str]:
    import pyogrio
    from sqlalchemy import create_engine

    engine = create_engine(cfg.sqlalchemy_url())
    available = [row[0] for row in pyogrio.list_layers(str(gpkg_path))]
    loaded: list[str] = []

    for wanted in GEOMETRY_LAYERS:
        layer = _resolve_layer_name(available, wanted)
        if layer is None:
            log.warning("Layer %r not found in GeoPackage, skipping", wanted)
            continue
        log.info("Loading geometry layer %r", layer)
        gdf = _read_geometry_layer(gpkg_path, layer)
        if gdf.crs is None:
            gdf = gdf.set_crs(SOURCE_CRS)
        gdf = gdf.to_crs(TARGET_CRS)
        table_name = wanted.lower()
        gdf.to_postgis(
            name=table_name,
            con=engine,
            schema=cfg.schema,
            if_exists="replace",
            index=False,
        )
        log.info(
            "  -> wrote %d rows to %s.%s (EPSG:3812, columns: %s)",
            len(gdf),
            cfg.schema,
            table_name,
            ", ".join(gdf.columns),
        )
        loaded.append(table_name)

    return loaded


def load_attribute_layers(gpkg_path: Path, cfg: DbConfig) -> list[str]:
    import pyogrio
    from sqlalchemy import create_engine

    engine = create_engine(cfg.sqlalchemy_url())
    available = [row[0] for row in pyogrio.list_layers(str(gpkg_path))]
    loaded: list[str] = []

    for wanted in ATTRIBUTE_LAYERS:
        layer = _resolve_layer_name(available, wanted)
        if layer is None:
            log.warning("Layer %r not found in GeoPackage, skipping", wanted)
            continue
        log.info("Loading attribute layer %r", layer)
        df = _read_attribute_layer(gpkg_path, layer)
        table_name = wanted.lower()
        df.to_sql(
            name=table_name,
            con=engine,
            schema=cfg.schema,
            if_exists="replace",
            index=False,
        )
        log.info(
            "  -> wrote %d rows to %s.%s (columns: %s)",
            len(df),
            cfg.schema,
            table_name,
            ", ".join(df.columns),
        )
        loaded.append(table_name)

    return loaded


def load_stib_stops(cfg: DbConfig, timeout: int = 60) -> bool:
    """
    Fetch STIB (Brussels public transport) stops from the live WFS endpoint
    and load them into urbis.stib_stops, EPSG:3812 (already requested at
    that SRID, no reprojection needed). Run right after the GeoPackage
    layers so lookup_views.sql's gazetteer refresh (which unions in this
    table as a TRANSIT_STOP source) sees fresh data on the same sync run.

    Unlike the GeoPackage layers this is a live network call to a service
    outside urbisdownload.datastore.brussels, so a failure here is logged
    and swallowed rather than failing the whole sync -- the rest of the
    UrbIS load should not be blocked by this endpoint being flaky.
    """
    import geopandas as gpd
    from sqlalchemy import create_engine

    log.info("Fetching STIB stops from %s", STIB_STOPS_WFS_URL)
    try:
        resp = requests.get(STIB_STOPS_WFS_URL, timeout=timeout)
        resp.raise_for_status()
        gdf = gpd.GeoDataFrame.from_features(resp.json()["features"], crs=TARGET_CRS)
    except Exception as exc:  # noqa: BLE001
        log.error(
            "Failed to fetch/parse STIB stops (%s) -- skipping this source for "
            "this sync run; urbis.stib_stops keeps its previous contents if any.",
            exc,
        )
        return False

    gdf = _lowercase_columns(gdf, geometry=True)
    if gdf.crs is None:
        gdf = gdf.set_crs(TARGET_CRS)
    elif gdf.crs.to_epsg() != int(TARGET_CRS.split(":")[1]):
        gdf = gdf.to_crs(TARGET_CRS)

    engine = create_engine(cfg.sqlalchemy_url())
    gdf.to_postgis(
        name="stib_stops",
        con=engine,
        schema=cfg.schema,
        if_exists="replace",
        index=False,
    )
    log.info(
        "  -> wrote %d rows to %s.stib_stops (EPSG:3812, columns: %s)",
        len(gdf),
        cfg.schema,
        ", ".join(gdf.columns),
    )
    return True


# --------------------------------------------------------------------------- #
# Reference data sources (STIB lines, rail lines, OSM POIs with aliases,
# roads + municipalities in a 5 km belt around the Region)
# --------------------------------------------------------------------------- #

def _scalar_text(v) -> str:
    """Cell -> clean text. Lists (WFS arrays) -> first element; 8.0 -> '8'; None/NaN -> ''."""
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    if v is None:
        return ""
    if isinstance(v, float):
        if v != v:  # NaN
            return ""
        if v.is_integer():
            return str(int(v))
    return str(v).strip()


def _stringify_complex_columns(gdf):
    """to_postgis cannot store dict/list cells in every column type; keep them as JSON text."""
    geom = gdf.geometry.name
    for c in list(gdf.columns):
        if c == geom:
            continue
        if gdf[c].map(lambda v: isinstance(v, (list, tuple, dict, set))).any():
            gdf[c] = gdf[c].map(
                lambda v: json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (list, tuple, dict, set)) else v
            )
    return gdf


def _write_gdf(cfg: DbConfig, gdf, table: str) -> None:
    from sqlalchemy import create_engine

    engine = create_engine(cfg.sqlalchemy_url())
    gdf.to_postgis(name=table, con=engine, schema=cfg.schema, if_exists="replace", index=False)
    log.info("  -> wrote %d rows to %s.%s (EPSG:3812, columns: %s)", len(gdf), cfg.schema, table, ", ".join(gdf.columns))


def _fetch_wfs_gdf(url: str, label: str, timeout: int = 120):
    """GeoJSON WFS -> GeoDataFrame in EPSG:3812 with lowercase columns; None on any failure."""
    import geopandas as gpd

    log.info("Fetching %s from %s", label, url)
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        feats = resp.json().get("features") or []
        if not feats:
            raise ValueError("the WFS returned no features")
        gdf = gpd.GeoDataFrame.from_features(feats, crs=TARGET_CRS)
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to fetch/parse %s (%s) -- skipping this source for this sync run.", label, exc)
        return None
    gdf = _lowercase_columns(gdf, geometry=True)
    if gdf.crs is None:
        gdf = gdf.set_crs(TARGET_CRS)
    elif gdf.crs.to_epsg() != int(TARGET_CRS.split(":")[1]):
        gdf = gdf.to_crs(TARGET_CRS)
    return gdf


def prepare_stib_lines(gdf):
    """Keep only attributes `line` and `mode`, modes Tram/Metro/Bus (case-insensitive)."""
    missing = [c for c in ("line", "mode") if c not in gdf.columns]
    if missing:
        raise ValueError(f"STIB lines layer has no column(s) {missing}; columns found: {list(gdf.columns)}")
    geom = gdf.geometry.name
    out = gdf[["line", "mode", geom]].copy()
    out["line"] = out["line"].map(_scalar_text)
    out["mode"] = out["mode"].map(lambda v: STIB_LINE_MODES.get(_scalar_text(v).lower()))
    out = out[out["mode"].notna() & (out["line"] != "")]
    return out[~out.geometry.isna() & ~out.geometry.is_empty]


def load_stib_lines(cfg: DbConfig, timeout: int = 120) -> bool:
    """STIB line geometries (attributes: line, mode in Tram/Metro/Bus) -> urbis.stib_lines.
    Feeds the TRANSIT_LINE gazetteer type (\"tram 8\", \"metro 1\", \"bus 71\")."""
    gdf = _fetch_wfs_gdf(STIB_LINES_WFS_URL, "STIB lines", timeout)
    if gdf is None:
        return False
    try:
        out = prepare_stib_lines(gdf)
    except Exception as exc:  # noqa: BLE001
        log.error("STIB lines: %s", exc)
        return False
    if out.empty:
        log.error("STIB lines: no Tram/Metro/Bus feature left after filtering -- leaving urbis.stib_lines untouched")
        return False
    log.info("  STIB lines kept: %s", out.groupby("mode")["line"].nunique().to_dict())
    _write_gdf(cfg, out, "stib_lines")
    return True


# Infrabel layer on data.mobility.brussels: linecnum (line number, e.g. 50) + linecalpha (letter part, e.g. A)
# -> "50A". If linecalpha already holds the whole code ("50A") it is used as is.
RAIL_REF_PARTS = ("linecnum", "linecalpha")
RAIL_REF_COLUMNS = ("linenumber", "line_number", "linenr", "lineno", "line_id", "ligne", "lijn", "line", "linecode",
                    "code", "numero", "nummer", "ref", "id")
RAIL_NAME_COLUMNS = ("name", "nom", "naam", "linename", "line_name", "description", "omschrijving")


def prepare_rail_lines(gdf):
    """Keep every original column, add `line_ref` (best-effort line number) and `rail_name`.
    The WFS attribute names are not known in advance, so the chosen columns are returned
    (and logged) so they can be corrected quickly."""
    out = _stringify_complex_columns(gdf.copy())
    cols = set(out.columns)
    name_col = next((c for c in RAIL_NAME_COLUMNS if c in cols and out[c].notna().any()), None)
    parts = [c for c in RAIL_REF_PARTS if c in cols and out[c].notna().any()]
    if parts:
        def _code(row):
            num = _scalar_text(row.get("linecnum"))
            alpha = _scalar_text(row.get("linecalpha"))
            return alpha if (num and alpha.startswith(num)) else num + alpha
        refs = out.apply(_code, axis=1)
        ref_col = "+".join(parts)
    else:
        ref_col = next((c for c in RAIL_REF_COLUMNS if c in cols and out[c].notna().any()), None)
        refs = out[ref_col].map(_scalar_text) if ref_col else None
    out["line_ref"] = [(refs.iloc[i] if refs is not None and refs.iloc[i] else f"L{i + 1}") for i in range(len(out))]
    out["rail_name"] = out[name_col].map(_scalar_text) if name_col else ""
    out = out[~out.geometry.isna() & ~out.geometry.is_empty]
    return out, {"ref_column": ref_col, "name_column": name_col, "columns": list(gdf.columns)}


def load_infrabel_lines(cfg: DbConfig, timeout: int = 120) -> bool:
    """Railway lines (Infrabel, via data.mobility.brussels WFS) -> urbis.infrabel_lines.
    Feeds the RAILWAY gazetteer type (\"the railway between Jette and Bockstael\")."""
    gdf = _fetch_wfs_gdf(INFRABEL_LINES_WFS_URL, "Infrabel railway lines", timeout)
    if gdf is None:
        return False
    out, meta = prepare_rail_lines(gdf)
    log.info("  Infrabel columns: %s | line number column: %s | name column: %s",
             meta["columns"], meta["ref_column"], meta["name_column"])
    if out.empty:
        log.error("Infrabel lines: no usable geometry -- leaving urbis.infrabel_lines untouched")
        return False
    _write_gdf(cfg, out, "infrabel_lines")
    return True


# ---- Overpass (OSM) -------------------------------------------------------- #

def overpass_query(ql: str, read_timeout: int = 360, attempts_per_mirror: int = 2, pause: float = 5.0) -> dict:
    """POST an Overpass QL query, trying each mirror (with retries). A 200 whose `remark`
    reports a runtime error/timeout is treated as a failure (Overpass then returns partial data)."""
    last: Exception | None = None
    for url in OVERPASS_URLS:
        for attempt in range(1, attempts_per_mirror + 1):
            try:
                r = requests.post(url, data={"data": ql}, headers={"User-Agent": OVERPASS_USER_AGENT}, timeout=read_timeout)
                if r.status_code in (429, 502, 503, 504):
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]!r}")
                r.raise_for_status()
                data = r.json()
                remark = str(data.get("remark") or "")
                if remark and ("error" in remark.lower() or "timed out" in remark.lower()):
                    raise RuntimeError(f"Overpass remark: {remark[:200]}")
                return data
            except Exception as exc:  # noqa: BLE001
                last = exc
                log.warning("Overpass %s attempt %d/%d failed: %s", url, attempt, attempts_per_mirror, exc)
                time.sleep(pause * attempt)
    raise RuntimeError(f"all Overpass mirrors failed; last error: {last}")


def _osm_alias_text(tags: dict) -> str:
    """alt_name / official_name / short_name, ';'-joined (OSM already uses ';' as separator)."""
    vals: list[str] = []
    for k in OSM_ALIAS_TAGS:
        for part in str(tags.get(k) or "").split(";"):
            part = part.strip()
            if part and part not in vals:
                vals.append(part)
    return ";".join(vals)


def parse_overpass_pois(elements: list[dict]) -> list[dict]:
    rows, seen = [], set()
    for el in elements:
        tags = el.get("tags") or {}
        name = tags.get("name")
        if not name:
            continue
        if el.get("type") == "node":
            lon, lat = el.get("lon"), el.get("lat")
        else:
            c = el.get("center") or {}
            lon, lat = c.get("lon"), c.get("lat")
        if lon is None or lat is None:
            continue
        oid = f"{el.get('type')}/{el.get('id')}"
        if oid in seen:
            continue
        seen.add(oid)
        rows.append({
            "osm_id": oid, "name": name,
            "name_nl": tags.get("name:nl"), "name_fr": tags.get("name:fr"), "name_en": tags.get("name:en"),
            "alt_name": _osm_alias_text(tags) or None,
            "category": next((tags[k] for k in OSM_CATEGORY_TAGS if k in tags), "landmark"),
            "wikidata": tags.get("wikidata"),
            "lon": lon, "lat": lat,
        })
    return rows


def load_osm_pois(cfg: DbConfig, timeout: int = 360) -> bool:
    """Named OSM POIs around Brussels (Overpass) -> urbis.osm_pois, EPSG:3812.
    Loads the default name PLUS aliases: name:nl, name:fr, name:en and alt/official/short names
    (the gazetteer view turns each distinct alias into an extra searchable row).
    Besides the five classic tags, any named object with `wikidata`/`heritage`/`office`/`man_made`
    is kept (landmarks such as Palais Stoclet are often tagged that way).
    Best effort: a failure is logged with its cause and swallowed, the previous table is kept."""
    import geopandas as gpd
    from shapely.geometry import Point

    query_ql = OSM_POI_QUERY.format(bbox=",".join(str(x) for x in OSM_BRUSSELS_BBOX))
    log.info("Fetching OSM POIs (Overpass mirrors: %s) bbox %s", ", ".join(OVERPASS_URLS), OSM_BRUSSELS_BBOX)
    try:
        elements = overpass_query(query_ql, read_timeout=timeout).get("elements", [])
        rows = parse_overpass_pois(elements)
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to fetch/parse OSM POIs (%s) -- skipping; urbis.osm_pois keeps its previous contents.", exc)
        return False
    if not rows:
        log.warning("OSM Overpass query returned no usable named POIs -- leaving urbis.osm_pois untouched")
        return False
    gdf = gpd.GeoDataFrame(
        [{k: v for k, v in r.items() if k not in ("lon", "lat")} | {"geometry": Point(r["lon"], r["lat"])} for r in rows],
        crs="EPSG:4326",
    ).to_crs(TARGET_CRS)
    n_alias = int(gdf[["name_nl", "name_fr", "name_en", "alt_name"]].notna().any(axis=1).sum())
    log.info("  OSM POIs: %d (%d with name:nl/fr/en or alias tags)", len(gdf), n_alias)
    _write_gdf(cfg, gdf, "osm_pois")
    return True


def _region_zone(cfg: DbConfig, buffer_m: float):
    """(region, buffer, bbox_latlon) in EPSG:3812 / (south, west, north, east)."""
    import geopandas as gpd
    import psycopg2
    from shapely import wkt as shp_wkt

    conn = psycopg2.connect(cfg.dsn())
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT ST_AsText(ST_Union(geometry)) FROM {cfg.schema}.region")
            row = cur.fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        raise RuntimeError(f"{cfg.schema}.region is empty -- load the UrbIS layers first")
    region = shp_wkt.loads(row[0])
    buf = region.buffer(buffer_m)
    minx, miny, maxx, maxy = gpd.GeoSeries([buf], crs=TARGET_CRS).to_crs("EPSG:4326").total_bounds
    return region, buf, (miny, minx, maxy, maxx)


def _split_bbox(bbox, nx: int = 2, ny: int = 2):
    s, w, n, e = bbox
    out = []
    for i in range(ny):
        for j in range(nx):
            out.append((s + (n - s) * i / ny, w + (e - w) * j / nx, s + (n - s) * (i + 1) / ny, w + (e - w) * (j + 1) / nx))
    return out


def parse_overpass_roads(elements: list[dict]) -> list[dict]:
    rows = []
    for el in elements:
        geom = el.get("geometry") or []
        if el.get("type") != "way" or len(geom) < 2:
            continue
        t = el.get("tags") or {}
        rows.append({
            "osm_id": f"way/{el['id']}", "name": t.get("name"), "name_nl": t.get("name:nl"), "name_fr": t.get("name:fr"),
            "ref": t.get("ref"), "highway": t.get("highway"),
            "coords": [(p["lon"], p["lat"]) for p in geom],
        })
    return rows


def _lines_only(geom):
    """Keep only the linear part of an intersection result (None if there is none)."""
    from shapely.ops import linemerge

    if geom is None or geom.is_empty:
        return None
    if geom.geom_type in ("LineString", "MultiLineString"):
        return geom
    if geom.geom_type == "GeometryCollection":
        parts = [g for g in geom.geoms if g.geom_type in ("LineString", "MultiLineString")]
        return linemerge(parts) if parts else None
    return None


def build_outside_roads(rows: list[dict], region, buf):
    """GeoDataFrame (EPSG:3812) of the road pieces that lie in the belt (buffer minus the Region)."""
    import geopandas as gpd
    from shapely.geometry import LineString

    gdf = gpd.GeoDataFrame(
        [{k: v for k, v in r.items() if k != "coords"} | {"geometry": LineString(r["coords"])} for r in rows],
        crs="EPSG:4326",
    ).to_crs(TARGET_CRS)
    zone = buf.difference(region)
    gdf["geometry"] = gdf.geometry.intersection(zone).map(_lines_only)
    gdf = gdf[gdf.geometry.notna()]
    return gdf.reset_index(drop=True)


def load_outside_roads(cfg: DbConfig, buffer_m: float = REFERENCE_BUFFER_M, timeout: int = 360) -> bool:
    """Named / numbered roads (OSM `highway`) in a belt of `buffer_m` around the Region, clipped to
    that belt -> urbis.outside_roads. Reference/context data only (names such as Ninoofsesteenweg
    or R0 beyond the border); the gazetteer exposes it as type OUTSIDE_STREET with a score penalty."""
    try:
        region, buf, bbox = _region_zone(cfg, buffer_m)
        rows: dict[str, dict] = {}
        for tile in _split_bbox(bbox):
            b = ",".join(f"{x:.6f}" for x in tile)
            ql = (f'[out:json][timeout:300];(way["highway"~"{OSM_ROAD_CLASSES}"]["name"]({b});'
                  f'way["highway"~"{OSM_ROAD_CLASSES}"]["ref"]({b}););out tags geom;')
            log.info("  roads tile %s", b)
            for r in parse_overpass_roads(overpass_query(ql, read_timeout=timeout).get("elements", [])):
                rows[r["osm_id"]] = r
        if not rows:
            log.warning("No OSM roads returned for the belt -- leaving urbis.outside_roads untouched")
            return False
        gdf = build_outside_roads(list(rows.values()), region, buf)
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to build outside roads (%s) -- skipping; urbis.outside_roads keeps its previous contents.", exc)
        return False
    if gdf.empty:
        log.warning("No road left in the belt after clipping -- leaving urbis.outside_roads untouched")
        return False
    log.info("  outside roads: %d pieces, %d distinct names", len(gdf), gdf["name"].nunique())
    _write_gdf(cfg, gdf, "outside_roads")
    return True


def parse_overpass_boundaries(elements: list[dict]) -> list[dict]:
    """Relations (admin boundaries) -> polygons in EPSG:4326, assembled from their member ways."""
    from shapely.geometry import LineString
    from shapely.ops import polygonize, unary_union

    out = []
    for el in elements:
        if el.get("type") != "relation":
            continue
        outer, inner = [], []
        for m in el.get("members", []):
            g = m.get("geometry") or []
            if m.get("type") != "way" or len(g) < 2:
                continue
            (inner if m.get("role") == "inner" else outer).append(LineString([(p["lon"], p["lat"]) for p in g]))
        if not outer:
            continue
        shell = unary_union(list(polygonize(unary_union(outer))))
        if inner:
            shell = shell.difference(unary_union(list(polygonize(unary_union(inner)))))
        if shell.is_empty:
            continue
        t = el.get("tags") or {}
        out.append({"osm_id": f"relation/{el['id']}", "nis": t.get("ref:NIS"), "name": t.get("name"),
                    "name_nl": t.get("name:nl"), "name_fr": t.get("name:fr"), "geometry": shell})
    return out


def build_neighbour_municipalities(rows: list[dict], region, buf):
    import geopandas as gpd

    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326").to_crs(TARGET_CRS)
    inside = gdf.geometry.intersection(region).area / gdf.geometry.area
    gdf = gdf[(inside < 0.5) & gdf.geometry.intersects(buf)]  # drop the 19 communes (already in UrbIS)
    return gdf.reset_index(drop=True)


def load_neighbour_municipalities(cfg: DbConfig, buffer_m: float = REFERENCE_BUFFER_M, timeout: int = 360) -> bool:
    """Municipalities (OSM admin_level 8) touching the belt around the Region, excluding the 19
    Brussels communes -> urbis.neighbour_municipalities (names: name, name:nl, name:fr, ref:NIS)."""
    try:
        region, buf, (s, w, n, e) = _region_zone(cfg, buffer_m)
        ql = f'[out:json][timeout:300];rel["boundary"="administrative"]["admin_level"="8"]({s:.6f},{w:.6f},{n:.6f},{e:.6f});out tags geom;'
        rows = parse_overpass_boundaries(overpass_query(ql, read_timeout=timeout).get("elements", []))
        gdf = build_neighbour_municipalities(rows, region, buf) if rows else None
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to build neighbouring municipalities (%s) -- skipping.", exc)
        return False
    if gdf is None or gdf.empty:
        log.warning("No neighbouring municipality found -- leaving urbis.neighbour_municipalities untouched")
        return False
    log.info("  neighbouring municipalities: %s", ", ".join(sorted(str(x) for x in gdf["name"])))
    _write_gdf(cfg, gdf, "neighbour_municipalities")
    return True


def load_reference_sources(cfg: DbConfig, args) -> list[str]:
    """Run every live (non-GeoPackage) source; returns the names of the tables written."""
    done: list[str] = []
    steps = [
        ("stib_stops", args.skip_stib, lambda: load_stib_stops(cfg)),
        ("stib_lines", args.skip_stib_lines, lambda: load_stib_lines(cfg)),
        ("infrabel_lines", args.skip_rail, lambda: load_infrabel_lines(cfg)),
        ("osm_pois", args.skip_osm_pois, lambda: load_osm_pois(cfg)),
        ("outside_roads", args.skip_outside, lambda: load_outside_roads(cfg, args.outside_buffer_m)),
        ("neighbour_municipalities", args.skip_outside, lambda: load_neighbour_municipalities(cfg, args.outside_buffer_m)),
    ]
    for table, skip, fn in steps:
        if skip:
            log.info("Skipping %s (flag)", table)
        elif fn():
            done.append(table)
    log.info("Reference sources loaded: %s", ", ".join(done) or "none")
    return done



def _get_column_info(conn, schema: str, table: str):
    """
    Return (columns, geom_info) for a table:
      columns  -- list of (column_name, data_type, udt_name, is_nullable) in order
      geom_info -- dict of {column_name: (geometry_type, srid)} for PostGIS columns
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type, udt_name, is_nullable
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema, table),
        )
        columns = cur.fetchall()

        cur.execute(
            """
            SELECT f_geometry_column, type, srid
            FROM geometry_columns
            WHERE f_table_schema = %s AND f_table_name = %s
            """,
            (schema, table),
        )
        geom_info = {row[0]: (row[1], row[2]) for row in cur.fetchall()}

    return columns, geom_info


def _describe_table_ddl(columns, geom_info, schema: str, table: str) -> str:
    """Build a human-readable CREATE TABLE statement from column/geometry metadata."""
    lines = []
    for column_name, data_type, udt_name, is_nullable in columns:
        if column_name in geom_info:
            geom_type, srid = geom_info[column_name]
            col_type = f"geometry({geom_type},{srid})"
        elif udt_name and data_type == "USER-DEFINED":
            col_type = udt_name
        else:
            col_type = data_type
        nullability = "" if is_nullable == "YES" else " NOT NULL"
        lines.append(f"    {column_name} {col_type}{nullability}")

    joined = ",\n".join(lines)
    return f"CREATE TABLE {schema}.{table} (\n{joined}\n);"


GEOMETRY_SAMPLE_CHARS = 10


def _format_sample_row(row, col_names: list[str], geom_columns: set[str]) -> tuple:
    """Truncate any geometry column's text representation to GEOMETRY_SAMPLE_CHARS characters."""
    display_values = []
    for col_name, value in zip(col_names, row):
        if col_name in geom_columns and value is not None:
            text_value = str(value)
            if len(text_value) > GEOMETRY_SAMPLE_CHARS:
                text_value = text_value[:GEOMETRY_SAMPLE_CHARS] + "..."
            display_values.append(text_value)
        else:
            display_values.append(value)
    return tuple(display_values)


def write_tables_sql_doc(loaded_tables: list[str], cfg: DbConfig, dest_path: Path) -> None:
    """
    For every loaded table, write out its (reconstructed) CREATE TABLE
    statement plus the first 5 rows of actual data, to a single .sql file
    for documentation/inspection purposes. Geometry column values are
    truncated to GEOMETRY_SAMPLE_CHARS characters in the sample rows.
    """
    import psycopg2

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    conn = psycopg2.connect(cfg.dsn())
    try:
        with open(dest_path, "w", encoding="utf-8") as fh:
            fh.write(f"-- UrbIS tables in schema {cfg.schema}\n")
            fh.write(f"-- Generated by sync_urbis.py\n\n")
            for table in loaded_tables:
                log.info("Documenting table %s.%s", cfg.schema, table)
                fh.write(f"-- {'=' * 60}\n")
                fh.write(f"-- Table: {cfg.schema}.{table}\n")
                fh.write(f"-- {'=' * 60}\n")
                columns, geom_info = _get_column_info(conn, cfg.schema, table)
                ddl = _describe_table_ddl(columns, geom_info, cfg.schema, table)
                fh.write(ddl + "\n\n")

                select_stmt = f"SELECT * FROM {cfg.schema}.{table} LIMIT 5;"
                fh.write(select_stmt + "\n")
                with conn.cursor() as cur:
                    cur.execute(select_stmt)
                    rows = cur.fetchall()
                    col_names = [desc[0] for desc in cur.description]
                if rows:
                    fh.write(f"-- columns: {', '.join(col_names)}\n")
                    geom_columns = set(geom_info.keys())
                    for row in rows:
                        display_row = _format_sample_row(row, col_names, geom_columns)
                        fh.write(f"-- {display_row}\n")
                else:
                    fh.write("-- (no rows)\n")
                fh.write("\n")
    finally:
        conn.close()

    log.info("Wrote table documentation for %d table(s) to %s", len(loaded_tables), dest_path)


def load_env_file(env_path: Path) -> None:
    """
    Populate os.environ from a .env file (KEY=VALUE per line, '#' comments
    allowed), without overriding variables that are already set in the real
    process environment.
    """
    if not env_path.exists():
        log.warning(
            "Env file %s not found; falling back to process environment "
            "variables / built-in defaults for the database connection.",
            env_path,
        )
        return

    try:
        from dotenv import dotenv_values

        values = dotenv_values(str(env_path))
    except ImportError:
        # Minimal fallback parser if python-dotenv isn't installed.
        values = {}
        for raw_line in env_path.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            values[key] = val

    for key, val in values.items():
        if val is not None and key not in os.environ:
            os.environ[key] = val
    log.debug("Loaded env file %s (%d key(s))", env_path, len(values))


def drop_dependent_views(cfg: DbConfig) -> None:
    """
    Drop the gazetteer/lookup views built on top of the raw UrbIS tables,
    CASCADE, *before* those tables get reloaded.

    load_geometry_layers()/load_attribute_layers() use
    to_postgis(if_exists="replace"), which issues a plain DROP TABLE with
    no CASCADE. That's fine on a first sync, but once sql/lookup_views.sql
    has been applied at least once -- urbis.gazetteer,
    urbis.v_street_segments, urbis.road_class_gazetteer, and
    urbis.mv_addresses_lookup all depend on these tables -- any later
    sync run fails with "cannot drop table ... because other objects
    depend on it". This clears those dependents first; apply_lookup_views()
    (called after the fresh table load, see main()) rebuilds them.
    """
    import psycopg2

    conn = psycopg2.connect(cfg.dsn())
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                f"""
                DROP MATERIALIZED VIEW IF EXISTS {cfg.schema}.gazetteer CASCADE;
                DROP MATERIALIZED VIEW IF EXISTS {cfg.schema}.road_class_gazetteer CASCADE;
                DROP MATERIALIZED VIEW IF EXISTS {cfg.schema}.mv_addresses_lookup CASCADE;
                DROP MATERIALIZED VIEW IF EXISTS {cfg.schema}.v_street_axes_routing CASCADE;
                DROP MATERIALIZED VIEW IF EXISTS {cfg.schema}.street_axes_nodes CASCADE;
                DROP VIEW IF EXISTS {cfg.schema}.v_street_segments CASCADE;
                """
            )
        log.info("Dropped dependent gazetteer/lookup views (if present) ahead of table reload")
    finally:
        conn.close()


def apply_lookup_views(cfg: DbConfig) -> None:
    """
    (Re)apply sql/lookup_views.sql against a freshly reloaded set of
    tables. The file is idempotent (DROP ... IF EXISTS CASCADE /
    CREATE OR REPLACE / CREATE ... IF NOT EXISTS throughout) so it's safe
    to run on every sync, not just the first one -- this is what rebuilds
    everything drop_dependent_views() removed above. Without this step, a
    second sync run would leave the database with fresh raw tables but no
    gazetteer/v_street_segments/road_class_gazetteer/mv_addresses_lookup
    at all, since drop_dependent_views() only drops, it doesn't recreate.
    """
    import psycopg2

    sql_path = Path(__file__).resolve().parent / "sql" / "lookup_views.sql"
    if not sql_path.exists():
        log.warning("sql/lookup_views.sql not found at %s -- skipping", sql_path)
        return

    conn = psycopg2.connect(cfg.dsn())
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql_path.read_text())
        log.info("Applied %s", sql_path)
    finally:
        conn.close()


def refresh_gazetteer(cfg: DbConfig) -> None:
    """
    Call urbis.refresh_gazetteer() (see sql/lookup_views.sql), which refreshes
    the gazetteer and address-lookup materialized views used by the geo-mcp
    resolver. Requires sql/lookup_views.sql to have been applied at least once.
    """
    import psycopg2

    conn = psycopg2.connect(cfg.dsn())
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"CALL {cfg.schema}.refresh_gazetteer();")
        log.info("Refreshed %s.gazetteer and %s.mv_addresses_lookup", cfg.schema, cfg.schema)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    # Load the .env file first (if present) so its values are available as
    # defaults for the --db-* flags below. We do a lightweight pre-parse of
    # --env-file so it can be overridden from the command line too.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    pre_args, _ = pre.parse_known_args(argv)
    load_env_file(pre_args.env_file)

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help=f"Path to the .env file with PG* database settings (default: {DEFAULT_ENV_FILE})",
    )
    p.add_argument(
        "--dest",
        type=Path,
        default=DEFAULT_DEST_GPKG,
        help=f"Where to store the GeoPackage (default: {DEFAULT_DEST_GPKG})",
    )
    p.add_argument(
        "--days-back",
        type=int,
        default=10,
        help="How many days back (including today) to try when looking for a published file",
    )
    p.add_argument(
        "--skip-download",
        action="store_true",
        help="Do not attempt any download; require a valid file already at --dest",
    )
    p.add_argument(
        "--no-cache-fallback",
        action="store_true",
        help="Fail instead of reusing the cached file if no download succeeds",
    )
    p.add_argument(
        "--skip-db",
        action="store_true",
        help="Only download/extract the GeoPackage, do not touch the database",
    )
    p.add_argument(
        "--tables-sql-file",
        type=Path,
        default=DEFAULT_TABLES_SQL,
        help=f"Where to write the per-table CREATE TABLE + sample rows file (default: {DEFAULT_TABLES_SQL})",
    )
    p.add_argument(
        "--skip-tables-sql",
        action="store_true",
        help="Do not write the tables.sql documentation file",
    )
    p.add_argument(
        "--skip-gazetteer-refresh",
        action="store_true",
        help="Do not call urbis.refresh_gazetteer() after loading (see sql/lookup_views.sql)",
    )
    p.add_argument(
        "--skip-stib",
        action="store_true",
        help="Do not fetch STIB public-transport stops (live WFS call, separate from the GeoPackage)",
    )
    p.add_argument(
        "--skip-osm-pois",
        action="store_true",
        help="Do not fetch OSM POIs (live Overpass API call, separate from the GeoPackage)",
    )

    p.add_argument("--skip-stib-lines", action="store_true", help="Do not fetch STIB line geometries (Tram/Metro/Bus)")
    p.add_argument("--skip-rail", action="store_true", help="Do not fetch Infrabel railway lines")
    p.add_argument("--skip-outside", action="store_true",
                   help="Do not build the belt around the Region (roads + neighbouring municipalities, OSM)")
    p.add_argument("--outside-buffer-m", type=float, default=REFERENCE_BUFFER_M,
                   help=f"Width of the belt around the Region in metres (default {REFERENCE_BUFFER_M})")
    p.add_argument(
        "--reference-only",
        action="store_true",
        help="Do NOT download/load the UrbIS GeoPackage: only (re)load the live reference sources (STIB stops/lines, "
             "rail, OSM POIs, belt roads/municipalities), then rebuild the gazetteer views and refresh them",
    )
    p.add_argument("--db-host", default=os.environ.get("PGHOST", "localhost"))
    p.add_argument("--db-port", type=int, default=int(os.environ.get("PGPORT", "5432")))
    p.add_argument("--db-user", default=os.environ.get("PGUSER", "postgres"))
    p.add_argument("--db-password", default=os.environ.get("PGPASSWORD"))
    p.add_argument("--db-name", default=os.environ.get("PGDATABASE", DEFAULT_DB_NAME))
    p.add_argument("--db-schema", default=DEFAULT_SCHEMA)
    p.add_argument(
        "--db-retries",
        type=int,
        default=3,
        help="How many times to retry connecting to Postgres before giving up",
    )
    p.add_argument(
        "--db-retry-delay",
        type=float,
        default=3.0,
        help="Seconds to wait between Postgres connection retries",
    )

    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
    )

    if args.reference_only:
        return _run_reference_only(args)

    try:
        if args.skip_download:
            gpkg_path = use_existing_gpkg(args.dest)
        else:
            gpkg_path = download_latest_gpkg(
                dest_gpkg=args.dest,
                days_back=args.days_back,
                use_cached=not args.no_cache_fallback,
            )
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to obtain a GeoPackage: %s", exc)
        return 1

    if gpkg_path.exists():
        log.info(
            "GeoPackage confirmed on disk at %s (%.1f MB)",
            gpkg_path.resolve(),
            gpkg_path.stat().st_size / (1024 * 1024),
        )
    else:
        log.error("Expected GeoPackage at %s but it is missing after processing", gpkg_path.resolve())
        return 1

    if args.skip_db:
        log.info("Skipping database load (--skip-db)")
        return 0

    cfg = DbConfig(
        host=args.db_host,
        port=args.db_port,
        user=args.db_user,
        password=args.db_password,
        dbname=args.db_name,
        schema=args.db_schema,
    )

    try:
        ensure_database_and_extensions(cfg, retries=args.db_retries, retry_delay=args.db_retry_delay)
        drop_dependent_views(cfg)
        geom_tables = load_geometry_layers(gpkg_path, cfg)
        attr_tables = load_attribute_layers(gpkg_path, cfg)
    except Exception as exc:  # noqa: BLE001
        log.error("Database load failed: %s", exc)
        return 1

    # Live, non-GeoPackage sources (STIB stops/lines, rail, OSM POIs, belt around the
    # Region). Each one is best-effort: a failure is logged and never blocks the UrbIS load.
    geom_tables.extend(load_reference_sources(cfg, args))

    try:
        # Rebuild the gazetteer/lookup views dropped by drop_dependent_views()
        # above, now that the raw tables (and STIB stops) are back in place.
        apply_lookup_views(cfg)
    except Exception as exc:  # noqa: BLE001
        log.error(
            "Failed to apply sql/lookup_views.sql after reload (%s) -- the raw "
            "UrbIS tables loaded fine, but urbis.gazetteer and friends are "
            "still missing/stale until this is re-run successfully.",
            exc,
        )
        return 1

    if not args.skip_tables_sql:
        try:
            write_tables_sql_doc(geom_tables + attr_tables, cfg, args.tables_sql_file)
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to write tables.sql documentation: %s", exc)
            # Documentation is a nice-to-have; don't fail the whole run over it.

    if not args.skip_gazetteer_refresh:
        try:
            refresh_gazetteer(cfg)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "Failed to refresh urbis.gazetteer / urbis.mv_addresses_lookup: %s. "
                "Has sql/lookup_views.sql been applied to this database yet?",
                exc,
            )

    log.info("Done.")
    return 0


def _run_reference_only(args) -> int:
    """--reference-only: skip the (large) GeoPackage; reload live sources, rebuild + refresh views."""
    cfg = DbConfig(host=args.db_host, port=args.db_port, user=args.db_user, password=args.db_password,
                   dbname=args.db_name, schema=args.db_schema)
    try:
        ensure_database_and_extensions(cfg, retries=args.db_retries, retry_delay=args.db_retry_delay)
        drop_dependent_views(cfg)  # the views depend on the tables that are about to be replaced
    except Exception as exc:  # noqa: BLE001
        log.error("Database preparation failed: %s", exc)
        return 1
    loaded = load_reference_sources(cfg, args)
    try:
        apply_lookup_views(cfg)
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to apply sql/lookup_views.sql (%s) -- the gazetteer is missing/stale until this succeeds.", exc)
        return 1
    if not args.skip_gazetteer_refresh:
        try:
            refresh_gazetteer(cfg)
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to refresh the gazetteer: %s", exc)
            return 1
    log.info("Done (reference-only). Tables written: %s. Next: python -m app.geo.embeddings  (embeddings for the new rows)",
             ", ".join(loaded) or "none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
