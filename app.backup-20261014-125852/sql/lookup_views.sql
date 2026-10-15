-- =============================================================================
-- urbis lookup layer: gazetteer + enriched views used by the geo-mcp resolver
-- Run after sync_urbis.py has (re)loaded the source tables in schema "urbis".
-- Idempotent: safe to re-run after every weekly UrbIS sync.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;
-- postgis and vector are already ensured by sync_urbis.py

SET search_path TO urbis, public;

-- -----------------------------------------------------------------------------
-- 0a. COORDINATE SYSTEM SAFETY (revision round 3: "ST_Transform: Input geometry has unknown (0) SRID" and
--     "Operation on mixed SRID geometries (0 != 3812)" broke 14 of 28 prompts).
--     Everything the geo layer computes with is EPSG:3812. The old code never noticed a wrong stored SRID because it
--     rebuilt geometries from WKT with an explicit SRID; the round-2 code reads the stored geometries.
--       * urbis.to3812(g)        : any geometry -> EPSG:3812. SRID 0 is resolved from the coordinates
--                                  (lon/lat -> 4326, X < 400 000 -> Belgian Lambert 72 / 31370, else already 3812).
--       * urbis.normalize_srids(): converts every geometry column of every urbis TABLE that holds anything but 3812
--                                  (drops the dependent views first; they are rebuilt below). Returns one report row per column.
--       * urbis.srid_audit()     : SRID histogram of every geometry column of tables AND views (read-only diagnostic).
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION urbis.to3812(g geometry) RETURNS geometry
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE
        WHEN g IS NULL THEN NULL
        WHEN ST_SRID(g) = 3812 THEN g
        WHEN ST_SRID(g) <> 0 THEN ST_Transform(g, 3812)
        WHEN ST_IsEmpty(g) THEN ST_SetSRID(g, 3812)
        WHEN ST_XMin(g) >= -180 AND ST_XMax(g) <= 180 AND ST_YMin(g) >= -90 AND ST_YMax(g) <= 90
            THEN ST_Transform(ST_SetSRID(g, 4326), 3812)
        WHEN ST_XMax(g) < 400000 THEN ST_Transform(ST_SetSRID(g, 31370), 3812)
        ELSE ST_SetSRID(g, 3812)
    END
$$;


-- valid polygonal geometry (self-intersections repaired, SRID kept) or NULL when nothing polygonal is left; NULLs are
-- ignored by ST_Union, so an unrepairable sliver can no longer poison a union (and lose its SRID)
CREATE OR REPLACE FUNCTION urbis.valid_poly(g geometry) RETURNS geometry
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE
        WHEN g IS NULL OR ST_IsEmpty(g) THEN NULL
        WHEN ST_IsValid(g) THEN g
        ELSE (SELECT CASE WHEN ST_IsEmpty(x) THEN NULL ELSE ST_SetSRID(x, ST_SRID(g)) END
              FROM (SELECT ST_CollectionExtract(ST_MakeValid(g), 3) AS x) q)
    END
$$;

CREATE OR REPLACE FUNCTION urbis.srid_audit()
RETURNS TABLE(relation text, kind text, col text, srid integer, n bigint)
LANGUAGE plpgsql STABLE AS $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT c.relname, c.relkind::text AS k, a.attname
        FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace ns ON ns.oid = c.relnamespace
        JOIN pg_type t ON t.oid = a.atttypid
        WHERE ns.nspname = 'urbis' AND c.relkind IN ('r', 'm', 'v') AND t.typname = 'geometry' AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY c.relname, a.attname
    LOOP
        RETURN QUERY EXECUTE format('SELECT %L::text, %L::text, %L::text, ST_SRID(%I)::int, count(*) FROM urbis.%I WHERE %I IS NOT NULL GROUP BY 4 ORDER BY 4',
                                    r.relname, CASE r.k WHEN 'r' THEN 'table' WHEN 'm' THEN 'matview' ELSE 'view' END, r.attname,
                                    r.attname, r.relname, r.attname);
    END LOOP;
END $$;

CREATE OR REPLACE FUNCTION urbis.normalize_srids()
RETURNS TABLE(relation text, col text, rows_not_3812 bigint, action text)
LANGUAGE plpgsql AS $$
DECLARE r record; n bigint; dropped boolean := false;
BEGIN
    FOR r IN
        SELECT c.relname, a.attname
        FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace ns ON ns.oid = c.relnamespace
        JOIN pg_type t ON t.oid = a.atttypid
        WHERE ns.nspname = 'urbis' AND c.relkind = 'r' AND t.typname = 'geometry' AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY c.relname, a.attname
    LOOP
        EXECUTE format('SELECT count(*) FROM urbis.%I WHERE %I IS NOT NULL AND ST_SRID(%I) <> 3812', r.relname, r.attname, r.attname) INTO n;
        relation := r.relname; col := r.attname; rows_not_3812 := n;
        IF n > 0 THEN
            IF NOT dropped THEN   -- a column type cannot change while views depend on it; they are all rebuilt by this script
                DROP MATERIALIZED VIEW IF EXISTS urbis.gazetteer CASCADE;
                DROP MATERIALIZED VIEW IF EXISTS urbis.road_class_gazetteer CASCADE;
                DROP MATERIALIZED VIEW IF EXISTS urbis.mv_addresses_lookup CASCADE;
                DROP MATERIALIZED VIEW IF EXISTS urbis.street_groups CASCADE;
                DROP MATERIALIZED VIEW IF EXISTS urbis.v_route_edges CASCADE;
                DROP MATERIALIZED VIEW IF EXISTS urbis.v_rail_edges CASCADE;
                DROP MATERIALIZED VIEW IF EXISTS urbis.v_street_axes_routing CASCADE;
                DROP MATERIALIZED VIEW IF EXISTS urbis.street_axes_nodes CASCADE;
                dropped := true;
            END IF;
            EXECUTE format('ALTER TABLE urbis.%I ALTER COLUMN %I TYPE geometry USING urbis.to3812(%I)', r.relname, r.attname, r.attname);
            action := 'CONVERTED to EPSG:3812';
        ELSE
            action := 'ok';
        END IF;
        RETURN NEXT;
    END LOOP;
END $$;

-- -----------------------------------------------------------------------------
-- 0. STIB STOPS PLACEHOLDER: app/sync_urbis.py::load_stib_stops() writes
--    the real table (real columns, real data) via to_postgis(if_exists=
--    'replace') from a live WFS fetch -- this is only here so the
--    gazetteer materialized view below always has something to compile
--    against, even on a database where sync_urbis.py hasn't successfully
--    run --skip-stib=false yet (or has only ever run with --skip-stib).
--    Column set confirmed from a real sync run: gid, line, mode,
--    stop_id, name_fr, name_nl, geometry.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS urbis.stib_stops (
    gid      bigint,
    line     text,
    mode     text,
    stop_id  text,
    name_fr  text,
    name_nl  text,
    geometry geometry(Point, 3812)
);

-- Same reasoning as urbis.stib_stops above: app/sync_urbis.py::
-- load_osm_pois() writes the real table via to_postgis(if_exists=
-- 'replace') from a live Overpass API fetch (revision note 3.txt --
-- OSM POIs fill gaps in UrbIS's own PointsOfInterest, e.g. Stoclet
-- Palace). This placeholder just keeps the gazetteer compiling before
-- the first successful fetch. UNTESTED against the live endpoint -- see
-- load_osm_pois()'s docstring.
CREATE TABLE IF NOT EXISTS urbis.osm_pois (
    osm_id   text,
    name     text,
    category text,
    geometry geometry(Point, 3812)
);
-- Alias columns (name:nl / name:fr / name:en / alt+official+short names ';'-joined) and
-- wikidata id. ADD COLUMN IF NOT EXISTS upgrades a placeholder created by an older version.
ALTER TABLE urbis.osm_pois ADD COLUMN IF NOT EXISTS name_nl   text;
ALTER TABLE urbis.osm_pois ADD COLUMN IF NOT EXISTS name_fr   text;
ALTER TABLE urbis.osm_pois ADD COLUMN IF NOT EXISTS name_en   text;
ALTER TABLE urbis.osm_pois ADD COLUMN IF NOT EXISTS alt_name  text;
ALTER TABLE urbis.osm_pois ADD COLUMN IF NOT EXISTS wikidata  text;

-- Placeholders for the other live sources (written for real by app/sync_urbis.py).
-- Plain `geometry` (untyped) so whatever geopandas writes later replaces them cleanly.
CREATE TABLE IF NOT EXISTS urbis.stib_lines (line text, mode text, geometry geometry);
CREATE TABLE IF NOT EXISTS urbis.infrabel_lines (line_ref text, rail_name text, geometry geometry);
CREATE TABLE IF NOT EXISTS urbis.outside_roads (
    osm_id text, name text, name_nl text, name_fr text, ref text, highway text, geometry geometry);
CREATE TABLE IF NOT EXISTS urbis.neighbour_municipalities (
    osm_id text, nis text, name text, name_nl text, name_fr text, geometry geometry);

-- -----------------------------------------------------------------------------
-- 0b. PERFORMANCE INDEXES ON RAW URBIS TABLES: these are plain tables
--     loaded fresh by app/sync_urbis.py's to_postgis(if_exists='replace')
--     on every sync, which drops any indexes along with the table itself
--     -- so these need to be (re)created here on every apply, same as the
--     views below. Confirmed missing entirely: every ST_Intersects check
--     and street_id join against urbis.streetsurfaces throughout this
--     file and app/geo/builder.py's corridor logic was a sequential scan.
-- -----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_streetsurfaces_geometry ON urbis.streetsurfaces USING gist (geometry);
CREATE INDEX IF NOT EXISTS idx_streetsurfaces_street_id ON urbis.streetsurfaces (street_id);
CREATE INDEX IF NOT EXISTS idx_streetsurfaces_type_id ON urbis.streetsurfaces (type_id);
CREATE INDEX IF NOT EXISTS idx_streetsurfaces_hierarchy_id ON urbis.streetsurfaces (hierarchy_id);
CREATE INDEX IF NOT EXISTS idx_outside_roads_geom ON urbis.outside_roads USING gist (geometry);
CREATE INDEX IF NOT EXISTS idx_infrabel_lines_geom ON urbis.infrabel_lines USING gist (geometry);

-- every geometry column of every urbis table must be EPSG:3812 from here on (report row per column; 'ok' = nothing to do)
SELECT * FROM urbis.normalize_srids();

-- -----------------------------------------------------------------------------
-- 3c. ROUTING NETWORK (rewritten for the axis-to-axis routing of revision round 2).
--     * v_street_axes_routing: one row per Region StreetAxes segment. cost is now
--       length x a hierarchy factor (urbis.route_cost_factors), so main roads are CHEAPER and the
--       "most usable road" between two streets prefers them over local roads.
--       type_code (StreetTypeDomain.type: S, C, SC, IC, ...) is exposed so tram segments can be
--       found ("avenue with a tram" = axes of type C / SC / IC / AC).
--     * v_route_edges: the Region axes + the OSM roads of the 5 km belt (noded at every crossing so
--       "where the R0 crosses the Ninoofsesteenweg" is a real graph node) + short bridge edges that
--       join the two networks at the border. Region edges keep their UrbIS ids; belt edges (10^12+), bridges (2*10^12+) and
--       rail edges (5*10^12+) and belt/rail nodes (10^13+ / 2*10^13+, from rounded coordinates) cannot collide with them.
--     * v_rail_edges: Infrabel lines, noded the same way (for "railway between station A and B").
--     * route_nodes / rail_nodes: one point per node id.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS urbis.route_cost_factors (hierarchy text PRIMARY KEY, factor numeric NOT NULL);
INSERT INTO urbis.route_cost_factors (hierarchy, factor) VALUES
    ('H', 0.6), ('MER', 0.7), ('MAR', 0.8), ('IDR', 0.9), ('DC', 1.0), ('DR', 1.1), ('T', 2.0)
ON CONFLICT (hierarchy) DO NOTHING;

DROP MATERIALIZED VIEW IF EXISTS urbis.v_street_axes_routing CASCADE;

CREATE MATERIALIZED VIEW urbis.v_street_axes_routing AS
SELECT
    sa.inspire_id::bigint   AS id,
    sa.startsn_id::bigint   AS source,
    sa.endsn_id::bigint     AS target,
    ST_Length(sa.geometry) * COALESCE(cf.factor, 1.0)  AS cost,
    ST_Length(sa.geometry) * COALESCE(cf.factor, 1.0)  AS reverse_cost,
    sa.street_id::bigint    AS street_id,
    sa.hierarchy_id::bigint AS hierarchy_id,
    urbis.to3812(sa.geometry) AS geometry,
    ttd.type                AS type_code,
    hd.hierarchy            AS hierarchy_code,
    ST_Length(sa.geometry)  AS length_m
FROM urbis.streetaxes sa
LEFT JOIN urbis.streettypedomain ttd ON ttd.inspire_id = sa.type_id::bigint
LEFT JOIN urbis.streethierarchydomain hd ON hd.inspire_id = sa.hierarchy_id::bigint
LEFT JOIN urbis.route_cost_factors cf ON cf.hierarchy = hd.hierarchy
WHERE sa.startsn_id IS NOT NULL
  AND sa.endsn_id IS NOT NULL
  AND (ttd.type IS NULL OR ttd.type NOT IN ('MS', 'RS', 'RT', 'MT'));

CREATE UNIQUE INDEX IF NOT EXISTS ux_street_axes_routing_id ON urbis.v_street_axes_routing (id);
CREATE INDEX IF NOT EXISTS idx_street_axes_routing_source ON urbis.v_street_axes_routing (source);
CREATE INDEX IF NOT EXISTS idx_street_axes_routing_target ON urbis.v_street_axes_routing (target);
CREATE INDEX IF NOT EXISTS idx_street_axes_routing_geom ON urbis.v_street_axes_routing USING gist (geometry);
CREATE INDEX IF NOT EXISTS idx_street_axes_routing_street_id ON urbis.v_street_axes_routing (street_id);

DROP MATERIALIZED VIEW IF EXISTS urbis.street_axes_nodes CASCADE;

CREATE MATERIALIZED VIEW urbis.street_axes_nodes AS
SELECT node_id, ST_Centroid(ST_Collect(geom)) AS geom
FROM (
    SELECT source AS node_id, ST_StartPoint(geometry) AS geom FROM urbis.v_street_axes_routing
    UNION ALL
    SELECT target AS node_id, ST_EndPoint(geometry) AS geom FROM urbis.v_street_axes_routing
) endpoints
GROUP BY node_id;

CREATE UNIQUE INDEX IF NOT EXISTS ux_street_axes_nodes_id ON urbis.street_axes_nodes (node_id);
CREATE INDEX IF NOT EXISTS idx_street_axes_nodes_geom ON urbis.street_axes_nodes USING gist (geom);

-- Belt roads: noded, attributed, with node ids from rounded coordinates.
DROP MATERIALIZED VIEW IF EXISTS urbis.v_route_edges CASCADE;

CREATE MATERIALIZED VIEW urbis.v_route_edges AS
WITH noded AS (
    SELECT row_number() OVER () AS n, d.geom AS geometry
    FROM (
        -- split into atomic parts first and keep only lines: ST_Node rejects (nested) GeometryCollections
        SELECT (ST_Dump(ST_Node(ST_Collect(p.geom)))).geom AS geom
        FROM (SELECT (ST_Dump(urbis.to3812(ST_Force2D(ST_CurveToLine(o.geometry))))).geom AS geom
              FROM urbis.outside_roads o
              WHERE o.geometry IS NOT NULL AND NOT ST_IsEmpty(o.geometry)) p
        WHERE ST_GeometryType(p.geom) = 'ST_LineString'
    ) d
    WHERE ST_GeometryType(d.geom) = 'ST_LineString' AND ST_Length(d.geom) > 0.5
),
attributed AS (
    SELECT DISTINCT ON (nd.n)
        nd.n, nd.geometry, o.name, o.name_fr, o.name_nl, o.ref, o.highway
    FROM noded nd
    JOIN urbis.outside_roads o
      ON ST_DWithin(o.geometry, ST_LineInterpolatePoint(nd.geometry, 0.5), 0.5)
    ORDER BY nd.n, (o.name IS NULL), o.osm_id
),
belt AS (
    SELECT
        1000000000000 + a.n AS id,
        10000000000000 + round(ST_X(ST_StartPoint(a.geometry)))::bigint * 1000000 + round(ST_Y(ST_StartPoint(a.geometry)))::bigint AS source,
        10000000000000 + round(ST_X(ST_EndPoint(a.geometry)))::bigint   * 1000000 + round(ST_Y(ST_EndPoint(a.geometry)))::bigint   AS target,
        ST_Length(a.geometry) * COALESCE(cf.factor, 1.0) AS cost,
        ST_Length(a.geometry) * COALESCE(cf.factor, 1.0) AS reverse_cost,
        NULL::bigint AS street_id,
        NULL::bigint AS hierarchy_id,
        a.geometry,
        NULL::text AS type_code,
        h.hc AS hierarchy_code,
        ST_Length(a.geometry) AS length_m,
        'O'::text AS origin,
        a.name, a.ref,
        ARRAY(SELECT DISTINCT k FROM unnest(ARRAY[
            regexp_replace(unaccent(lower(COALESCE(a.name, ''))),    '[^a-z0-9]', '', 'g'),
            regexp_replace(unaccent(lower(COALESCE(a.name_fr, ''))), '[^a-z0-9]', '', 'g'),
            regexp_replace(unaccent(lower(COALESCE(a.name_nl, ''))), '[^a-z0-9]', '', 'g')]) k WHERE k <> '') AS name_keys,
        CASE WHEN a.name IS NOT NULL AND a.name <> '' THEN md5(unaccent(lower(a.name))) END AS name_md5,
        ARRAY(SELECT md5(btrim(r)) FROM unnest(string_to_array(COALESCE(a.ref, ''), ';')) r WHERE btrim(r) <> '') AS ref_md5s
    FROM attributed a
    CROSS JOIN LATERAL (SELECT CASE
            WHEN a.highway IN ('motorway', 'motorway_link') THEN 'H'
            WHEN a.highway IN ('trunk', 'trunk_link')       THEN 'MER'
            WHEN a.highway IN ('primary', 'primary_link')   THEN 'MAR'
            WHEN a.highway IN ('secondary', 'secondary_link') THEN 'IDR'
            WHEN a.highway IN ('tertiary', 'tertiary_link') THEN 'DC'
            ELSE 'DR' END AS hc) h
    LEFT JOIN urbis.route_cost_factors cf ON cf.hierarchy = h.hc
),
region AS (
    SELECT id, source, target, cost, reverse_cost, street_id, hierarchy_id, geometry, type_code,
           hierarchy_code, length_m, 'R'::text AS origin, NULL::text AS name, NULL::text AS ref,
           ARRAY[]::text[] AS name_keys, NULL::text AS name_md5, ARRAY[]::text[] AS ref_md5s
    FROM urbis.v_street_axes_routing
),
belt_nodes AS (
    SELECT DISTINCT ON (node_id) node_id, geom FROM (
        SELECT source AS node_id, ST_StartPoint(geometry) AS geom FROM belt
        UNION ALL SELECT target, ST_EndPoint(geometry) FROM belt) x
    ORDER BY node_id
),
bridges AS (
    SELECT
        2000000000000 + row_number() OVER (ORDER BY bn.node_id) AS id,
        bn.node_id AS source, rn.node_id AS target,
        GREATEST(ST_Distance(bn.geom, rn.geom), 0.1) AS cost,
        GREATEST(ST_Distance(bn.geom, rn.geom), 0.1) AS reverse_cost,
        NULL::bigint AS street_id, NULL::bigint AS hierarchy_id,
        ST_MakeLine(bn.geom, rn.geom) AS geometry,
        NULL::text AS type_code, NULL::text AS hierarchy_code,
        GREATEST(ST_Distance(bn.geom, rn.geom), 0.1) AS length_m,
        'B'::text AS origin, NULL::text AS name, NULL::text AS ref,
        ARRAY[]::text[] AS name_keys, NULL::text AS name_md5, ARRAY[]::text[] AS ref_md5s
    FROM belt_nodes bn
    CROSS JOIN LATERAL (
        SELECT n.node_id, n.geom FROM urbis.street_axes_nodes n
        ORDER BY n.geom <-> bn.geom LIMIT 1) rn
    WHERE ST_Distance(bn.geom, rn.geom) <= 25
)
SELECT * FROM region
UNION ALL SELECT * FROM belt
UNION ALL SELECT * FROM bridges;

CREATE UNIQUE INDEX IF NOT EXISTS ux_route_edges_id ON urbis.v_route_edges (id);
CREATE INDEX IF NOT EXISTS idx_route_edges_geom ON urbis.v_route_edges USING gist (geometry);
CREATE INDEX IF NOT EXISTS idx_route_edges_street_id ON urbis.v_route_edges (street_id);
CREATE INDEX IF NOT EXISTS idx_route_edges_name_keys ON urbis.v_route_edges USING gin (name_keys);
CREATE INDEX IF NOT EXISTS idx_route_edges_name_md5 ON urbis.v_route_edges (name_md5);
CREATE INDEX IF NOT EXISTS idx_route_edges_ref_md5s ON urbis.v_route_edges USING gin (ref_md5s);

DROP MATERIALIZED VIEW IF EXISTS urbis.route_nodes CASCADE;
CREATE MATERIALIZED VIEW urbis.route_nodes AS
SELECT node_id, ST_Centroid(ST_Collect(geom)) AS geom FROM (
    SELECT source AS node_id, ST_StartPoint(geometry) AS geom FROM urbis.v_route_edges
    UNION ALL
    SELECT target, ST_EndPoint(geometry) FROM urbis.v_route_edges) e
GROUP BY node_id;
CREATE UNIQUE INDEX IF NOT EXISTS ux_route_nodes_id ON urbis.route_nodes (node_id);
CREATE INDEX IF NOT EXISTS idx_route_nodes_geom ON urbis.route_nodes USING gist (geom);

-- Railway network (Infrabel), noded; same column layout as v_route_edges so the same routing code runs on it.
DROP MATERIALIZED VIEW IF EXISTS urbis.v_rail_edges CASCADE;
CREATE MATERIALIZED VIEW urbis.v_rail_edges AS
WITH noded AS (
    SELECT row_number() OVER () AS n, d.geom AS geometry
    FROM (SELECT (ST_Dump(ST_Node(ST_Collect(p.geom)))).geom AS geom
          FROM (SELECT (ST_Dump(urbis.to3812(ST_Force2D(ST_CurveToLine(r.geometry))))).geom AS geom
                FROM urbis.infrabel_lines r WHERE r.geometry IS NOT NULL AND NOT ST_IsEmpty(r.geometry)) p
          WHERE ST_GeometryType(p.geom) = 'ST_LineString') d
    WHERE ST_GeometryType(d.geom) = 'ST_LineString' AND ST_Length(d.geom) > 0.5
)
SELECT
    5000000000000 + n AS id,
    20000000000000 + round(ST_X(ST_StartPoint(geometry)))::bigint * 1000000 + round(ST_Y(ST_StartPoint(geometry)))::bigint AS source,
    20000000000000 + round(ST_X(ST_EndPoint(geometry)))::bigint   * 1000000 + round(ST_Y(ST_EndPoint(geometry)))::bigint   AS target,
    ST_Length(geometry) AS cost, ST_Length(geometry) AS reverse_cost,
    NULL::bigint AS street_id, NULL::bigint AS hierarchy_id, geometry,
    NULL::text AS type_code, NULL::text AS hierarchy_code, ST_Length(geometry) AS length_m,
    'T'::text AS origin, NULL::text AS name, NULL::text AS ref,
    ARRAY[]::text[] AS name_keys, NULL::text AS name_md5, ARRAY[]::text[] AS ref_md5s
FROM noded;
CREATE UNIQUE INDEX IF NOT EXISTS ux_rail_edges_id ON urbis.v_rail_edges (id);
CREATE INDEX IF NOT EXISTS idx_rail_edges_geom ON urbis.v_rail_edges USING gist (geometry);

DROP MATERIALIZED VIEW IF EXISTS urbis.rail_nodes CASCADE;
CREATE MATERIALIZED VIEW urbis.rail_nodes AS
SELECT node_id, ST_Centroid(ST_Collect(geom)) AS geom FROM (
    SELECT source AS node_id, ST_StartPoint(geometry) AS geom FROM urbis.v_rail_edges
    UNION ALL
    SELECT target, ST_EndPoint(geometry) FROM urbis.v_rail_edges) e
GROUP BY node_id;
CREATE UNIQUE INDEX IF NOT EXISTS ux_rail_nodes_id ON urbis.rail_nodes (node_id);
CREATE INDEX IF NOT EXISTS idx_rail_nodes_geom ON urbis.rail_nodes USING gist (geom);

-- -----------------------------------------------------------------------------
-- 3d. STREET GROUPS: ONE row per real-world street (revision rounds 2-3).
--     A street is stored under several placename ids (Avenue Louise = 3), under spelling variants that change from one
--     municipality to the next (Avenue de Tervuren / Avenue de Tervueren, Ninoofsesteenweg / Ninoofse Steenweg) and
--     with a French and a Dutch name. Therefore:
--       1. NAME CLASS: two placenames are the same street name if they share the FR key or the NL key (keys ignore
--          case, accents, spaces, punctuation) or if their keys are near-identical (trigram similarity >= 0.85,
--          same first 4 letters, length within 2). Classes = connected components of that key graph.
--       2. each class's surfaces are clustered spatially (250 m: wider than a park, tunnel mouth or large junction
--          that interrupts the road, narrower than the distance between two unrelated streets of the same name);
--       3. geometry = the cluster's OWN surfaces + junction polygons (type I / IC / IL, any street_id) that touch >= 2
--          of them -- only the crossings BETWEEN two segments, no side streets;
--       4. axis_ids = the street's routable edges: Region axes + belt roads of the same name that continue it;
--       5. variants = every distinct (FR, NL) spelling, so each one is searchable (alias rows in the gazetteer).
--     Polygons are repaired (ST_MakeValid) before any union and every result is forced to EPSG:3812, non-empty.
-- -----------------------------------------------------------------------------
DROP MATERIALIZED VIEW IF EXISTS urbis.street_groups CASCADE;

CREATE MATERIALIZED VIEW urbis.street_groups AS
WITH RECURSIVE names AS (
    SELECT pn.inspire_id AS pid, pn.namefre, pn.namedut,
           NULLIF(regexp_replace(unaccent(lower(COALESCE(pn.namefre, ''))), '[^a-z0-9]', '', 'g'), '') AS fr_key,
           NULLIF(regexp_replace(unaccent(lower(COALESCE(pn.namedut, ''))), '[^a-z0-9]', '', 'g'), '') AS nl_key
    FROM urbis.placenames pn
    WHERE pn.namefre IS NOT NULL OR pn.namedut IS NOT NULL
),
keys AS (
    SELECT k, dense_rank() OVER (ORDER BY k) AS kid
    FROM (SELECT fr_key AS k FROM names WHERE fr_key IS NOT NULL UNION SELECT nl_key FROM names WHERE nl_key IS NOT NULL) x
),
key_edges AS (
    SELECT kf.kid AS a, kn.kid AS b FROM names n JOIN keys kf ON kf.k = n.fr_key JOIN keys kn ON kn.k = n.nl_key
    UNION
    SELECT a.kid, b.kid FROM keys a JOIN keys b
      ON a.kid < b.kid AND left(a.k, 4) = left(b.k, 4) AND abs(length(a.k) - length(b.k)) <= 2
     AND length(a.k) >= 8 AND similarity(a.k, b.k) >= 0.85
),
adj AS (SELECT a, b FROM key_edges UNION SELECT b, a FROM key_edges),
reach(kid, root) AS (
    SELECT kid, kid FROM keys
    UNION
    SELECT adj.b, reach.root FROM reach JOIN adj ON adj.a = reach.kid
),
klass AS (SELECT kid, min(root) AS cls FROM reach GROUP BY kid),
keyed AS (
    SELECT n.*, COALESCE(kf.cls, kn.cls) AS gkey
    FROM names n LEFT JOIN keys f ON f.k = n.fr_key LEFT JOIN klass kf ON kf.kid = f.kid
                 LEFT JOIN keys g ON g.k = n.nl_key LEFT JOIN klass kn ON kn.kid = g.kid
),
surf AS (
    SELECT v.pid, v.gkey, v.geometry,
           ST_ClusterDBSCAN(v.geometry, eps := 250, minpoints := 1) OVER (PARTITION BY v.gkey) AS cid
    FROM (
        SELECT k.pid, k.gkey, urbis.valid_poly(ss.geometry) AS geometry
        FROM urbis.streetsurfaces ss
        JOIN keyed k ON k.pid = ss.street_id
        LEFT JOIN urbis.streettypedomain ttd ON ttd.inspire_id = ss.type_id
        WHERE ttd.type IS NULL OR ttd.type NOT IN ('MS', 'RS', 'RT', 'MT')
    ) v
    WHERE v.geometry IS NOT NULL AND NOT ST_IsEmpty(v.geometry)
),
clusters AS (
    SELECT s.gkey, s.cid, array_agg(DISTINCT s.pid) AS pids, MIN(s.pid) AS min_pid,
           ST_Union(s.geometry) AS g0
    FROM surf s
    GROUP BY s.gkey, s.cid
),
enriched AS (
    SELECT c.*, ax.edge_ids, ax.axes_geom, jn.junction_geom
    FROM clusters c
    LEFT JOIN LATERAL (
        SELECT array_agg(e.id) AS edge_ids, ST_Collect(e.geometry) AS axes_geom
        FROM urbis.v_street_axes_routing e
        WHERE e.street_id = ANY(c.pids) AND ST_DWithin(e.geometry, c.g0, 250)   -- same distance as the clustering: an axis may continue where the surfaces carry another id
    ) ax ON true
    LEFT JOIN LATERAL (
        SELECT ST_Union(urbis.valid_poly(j.geometry)) AS junction_geom
        FROM urbis.streetsurfaces j
        JOIN urbis.streettypedomain jt ON jt.inspire_id = j.type_id AND jt.type IN ('I', 'IC', 'IL')
        WHERE ax.axes_geom IS NOT NULL
          AND (j.street_id IS NULL OR j.street_id <> ALL (c.pids))
          AND ST_Intersects(j.geometry, ax.axes_geom)
          AND (SELECT count(*) FROM urbis.streetsurfaces own
               WHERE own.street_id = ANY(c.pids) AND ST_Intersects(own.geometry, j.geometry)) >= 2
    ) jn ON true
),
final AS (
    SELECT e.*,
           (SELECT mode() WITHIN GROUP (ORDER BY n.namefre) FROM keyed n WHERE n.pid = ANY(e.pids)) AS name_fr,
           (SELECT mode() WITHIN GROUP (ORDER BY n.namedut) FROM keyed n WHERE n.pid = ANY(e.pids)) AS name_nl,
           (SELECT array_agg(DISTINCT k) FROM keyed n, unnest(ARRAY[n.fr_key, n.nl_key]) k
             WHERE n.pid = ANY(e.pids) AND k IS NOT NULL) AS name_keys,
           (SELECT jsonb_agg(DISTINCT jsonb_build_object('fr', n.namefre, 'nl', n.namedut)) FROM keyed n WHERE n.pid = ANY(e.pids)) AS variants,
           urbis.to3812(ST_Buffer(ST_Buffer(CASE WHEN e.junction_geom IS NULL THEN e.g0 ELSE ST_Union(e.g0, e.junction_geom) END, 1), -1)) AS geom
    FROM enriched e
),
ranked AS (
    SELECT f.*, row_number() OVER (PARTITION BY f.min_pid ORDER BY ST_Area(f.geom) DESC) AS rn
    FROM final f
    WHERE f.geom IS NOT NULL AND NOT ST_IsEmpty(f.geom)
)
SELECT
    CASE WHEN r.rn = 1 THEN 'STREET:' || r.min_pid::text ELSE 'STREET:' || r.min_pid::text || '.' || r.rn::text END AS gid,
    r.min_pid, r.pids, r.name_fr, r.name_nl,
    unaccent(lower(r.name_fr)) AS name_fr_norm, unaccent(lower(r.name_nl)) AS name_nl_norm,
    r.name_keys, r.variants,
    COALESCE(r.edge_ids, ARRAY[]::bigint[]) || COALESCE((
        SELECT array_agg(o.id) FROM urbis.v_route_edges o
        WHERE o.origin = 'O' AND o.name_keys && r.name_keys
          AND EXISTS (SELECT 1 FROM urbis.v_route_edges o2
                      WHERE o2.origin = 'O' AND o2.name_keys && r.name_keys
                        AND ST_DWithin(o2.geometry, r.geom, 30)
                        AND ST_DWithin(o2.geometry, o.geometry, 3000))
    ), ARRAY[]::bigint[]) AS axis_ids,
    r.geom, urbis.to3812(ST_Centroid(r.geom)) AS centroid
FROM ranked r;

CREATE UNIQUE INDEX IF NOT EXISTS ux_street_groups_gid ON urbis.street_groups (gid);
CREATE INDEX IF NOT EXISTS idx_street_groups_pids ON urbis.street_groups USING gin (pids);
CREATE INDEX IF NOT EXISTS idx_street_groups_keys ON urbis.street_groups USING gin (name_keys);
CREATE INDEX IF NOT EXISTS idx_street_groups_geom ON urbis.street_groups USING gist (geom);



-- -----------------------------------------------------------------------------
-- 1. GAZETTEER: one row per named, resolvable place, across every entity type
--    the geo-detector can recognise. "id" is a stable text key
--    (type:source_id) so it survives weekly REFRESH MATERIALIZED VIEW.
-- -----------------------------------------------------------------------------
DROP MATERIALIZED VIEW IF EXISTS urbis.gazetteer CASCADE;

-- Segment types excluded from the merged per-street geometry (revision
-- note 2.txt): Metro Station, Rail Station, Rail Tunnel, Metro Tunnel.
-- These occasionally share a street_id with an ordinary road segment
-- (e.g. a street running over/beside a station) but aren't "the road"
-- for a street-level query, so they're dropped from the union rather
-- than silently bulging the street's merged geometry.
-- Codes come from StreetTypeDomain.type (see technical reference doc /
-- revision note 2.txt): MS, RS, RT, MT.
CREATE MATERIALIZED VIEW urbis.gazetteer AS
SELECT u.id, u.type, u.source_id, u.source_table, u.name_fr, u.name_nl, u.name_fr_norm, u.name_nl_norm,
       urbis.to3812(u.geom) AS geom, urbis.to3812(u.centroid) AS centroid
FROM (
-- Streets: ONE row per real-world street, read from urbis.street_groups (section 3d): placename
-- ids sharing a name are merged, only the street's own surfaces + the junction polygons between
-- its segments are part of the geometry (no side streets). id = 'STREET:<lowest placename id>'.
SELECT
    sg.gid                  AS id,
    'STREET'                AS type,
    sg.min_pid              AS source_id,
    'placenames'            AS source_table,
    sg.name_fr, sg.name_nl, sg.name_fr_norm, sg.name_nl_norm,
    sg.geom, sg.centroid
FROM urbis.street_groups sg
UNION ALL
-- ... and every OTHER spelling of the same street (Tervuren / Tervueren, Ninoofsesteenweg / Ninoofse Steenweg, ...): alias rows with
-- id suffix '#n' that the resolver collapses back to the street (same mechanism as the OSM / STIB aliases below)
SELECT sg.gid || '#' || v.n::text, 'STREET', sg.min_pid, 'placenames', v.fr, v.nl,
       unaccent(lower(v.fr)), unaccent(lower(v.nl)), sg.geom, sg.centroid
FROM urbis.street_groups sg
CROSS JOIN LATERAL (
    SELECT (row_number() OVER ())::int + 1 AS n, x->>'fr' AS fr, x->>'nl' AS nl
    FROM jsonb_array_elements(sg.variants) x
    WHERE unaccent(lower(COALESCE(x->>'fr', ''))) IS DISTINCT FROM COALESCE(sg.name_fr_norm, '')
       OR unaccent(lower(COALESCE(x->>'nl', ''))) IS DISTINCT FROM COALESCE(sg.name_nl_norm, '')
) v

UNION ALL
-- Points of interest. GROUP BY per the same reasoning as STREET above --
-- a duplicate inspire_id here can only mean multiple geometry rows for
-- the same named POI (e.g. a multi-part feature), never two distinct
-- places, so merging is always correct and protects the gazetteer's
-- unique id index the same way section 2 below protects STATISTICAL_SECTOR.
SELECT
    'POI:' || poi.inspire_id::text,
    'POI',
    poi.inspire_id,
    'pointsofinterest',
    poi.namefre,
    poi.namedut,
    unaccent(lower(poi.namefre)),
    unaccent(lower(poi.namedut)),
    ST_Union(poi.geometry),
    ST_Centroid(ST_Union(poi.geometry))
FROM urbis.pointsofinterest poi
WHERE poi.namefre IS NOT NULL OR poi.namedut IS NOT NULL
GROUP BY poi.inspire_id, poi.namefre, poi.namedut

UNION ALL
-- Municipalities. Grouped for the same reason as POI/STREET above.
SELECT
    'MUNICIPALITY:' || m.inspire_id::text,
    'MUNICIPALITY',
    m.inspire_id,
    'municipalities',
    m.namefre,
    m.namedut,
    unaccent(lower(m.namefre)),
    unaccent(lower(m.namedut)),
    ST_Union(m.geometry),
    ST_Centroid(ST_Union(m.geometry))
FROM urbis.municipalities m
GROUP BY m.inspire_id, m.namefre, m.namedut

UNION ALL
-- Statistical sectors (neighbourhoods). Note: statisticalsectors.inspire_id
-- is TEXT in this dataset (e.g. '21001A30K') while every other table's
-- inspire_id is BIGINT, so source_id here uses the internal bigint "id"
-- column instead, to keep the UNION ALL's source_id column type consistent.
-- The human-readable INSPIRE code is still preserved in the "id" text key.
-- GROUP BY here fixes a real duplicate-key crash seen against live data
-- (STATISTICAL_SECTOR:21013A51- appeared on two rows) -- some sectors are
-- stored as more than one geometry row under the same inspire_id (e.g. a
-- sector split into disjoint parts), which broke ux_gazetteer_id's unique
-- constraint before this was grouped, same failure mode STREET's
-- aggregation already handled for street segments.
SELECT
    'STATISTICAL_SECTOR:' || s.inspire_id::text,
    'STATISTICAL_SECTOR',
    MIN(s.id),
    'statisticalsectors',
    s.namefre,
    s.namedut,
    unaccent(lower(s.namefre)),
    unaccent(lower(s.namedut)),
    ST_Union(s.geometry),
    ST_Centroid(ST_Union(s.geometry))
FROM urbis.statisticalsectors s
GROUP BY s.inspire_id, s.namefre, s.namedut

UNION ALL
-- Monitoring districts. Grouped for the same reason as the branches above.
SELECT
    'MONITORING_DISTRICT:' || d.inspire_id::text,
    'MONITORING_DISTRICT',
    d.inspire_id,
    'monitoringdistricts',
    d.namefre,
    d.namedut,
    unaccent(lower(d.namefre)),
    unaccent(lower(d.namedut)),
    ST_Union(d.geometry),
    ST_Centroid(ST_Union(d.geometry))
FROM urbis.monitoringdistricts d
GROUP BY d.inspire_id, d.namefre, d.namedut

UNION ALL
-- Region (single row today, kept generic for future regions). Grouped
-- for the same reason as the branches above.
SELECT
    'REGION:' || r.inspire_id::text,
    'REGION',
    r.inspire_id,
    'region',
    r.namefre,
    r.namedut,
    unaccent(lower(r.namefre)),
    unaccent(lower(r.namedut)),
    ST_Union(r.geometry),
    ST_Centroid(ST_Union(r.geometry))
FROM urbis.region r
GROUP BY r.inspire_id, r.namefre, r.namedut

UNION ALL
-- >>> REFERENCE BRANCHES BEGIN (sliced out and tested in isolation; keep the markers)
-- STIB stops. The table has ONE ROW PER STOP (stop_id unique; `line` is an array such as
-- {39,44}). A named stop is usually several physical stops (both directions, tram + bus:
-- "Chien Vert" = 5 stops), so they are merged into ONE row per name and 400 m cluster
-- (same name far apart stays separate). geom = centroid of the cluster (a point, as before).
SELECT
    'TRANSIT_STOP:' || MIN(c.stop_id::text),
    'TRANSIT_STOP',
    MIN(c.gid),
    'stib_stops',
    MIN(c.name_fr),
    MIN(c.name_nl),
    unaccent(lower(MIN(c.name_fr))),
    unaccent(lower(MIN(c.name_nl))),
    ST_Centroid(ST_Collect(c.geometry)),
    ST_Centroid(ST_Collect(c.geometry))
FROM (
    SELECT s.*,
           unaccent(lower(COALESCE(s.name_fr, s.name_nl))) AS nm,
           ST_ClusterDBSCAN(s.geometry, 400, 1)
               OVER (PARTITION BY unaccent(lower(COALESCE(s.name_fr, s.name_nl)))) AS cl
    FROM urbis.stib_stops s
    WHERE COALESCE(s.name_fr, s.name_nl) IS NOT NULL
) c
GROUP BY c.nm, c.cl

UNION ALL
-- STIB lines (Tram / Metro / Bus): "Tram 8" ...
SELECT
    'TRANSIT_LINE:' || lower(x.mode) || ':' || x.line, 'TRANSIT_LINE', NULL, 'stib_lines',
    CASE x.mode WHEN 'Metro' THEN 'Métro' ELSE x.mode END || ' ' || x.line,
    x.mode || ' ' || x.line,
    unaccent(lower(x.mode || ' ' || x.line)),
    unaccent(lower(x.mode || ' ' || x.line)),
    x.g, ST_Centroid(x.g)
FROM (
    SELECT l.mode, l.line, ST_Multi(ST_LineMerge(ST_Union(l.geometry))) AS g
    FROM urbis.stib_lines l
    GROUP BY l.mode, l.line
) x
UNION ALL
-- ... and the spoken variants: "Ligne de tram 8" / "Tramlijn 8" (alias row, id suffix #2)
SELECT
    'TRANSIT_LINE:' || lower(x.mode) || ':' || x.line || '#2', 'TRANSIT_LINE', NULL, 'stib_lines',
    CASE x.mode WHEN 'Tram' THEN 'Ligne de tram ' WHEN 'Metro' THEN 'Ligne de métro ' ELSE 'Ligne de bus ' END || x.line,
    CASE x.mode WHEN 'Tram' THEN 'Tramlijn ' WHEN 'Metro' THEN 'Metrolijn ' ELSE 'Buslijn ' END || x.line,
    unaccent(lower(CASE x.mode WHEN 'Tram' THEN 'Ligne de tram ' WHEN 'Metro' THEN 'Ligne de métro ' ELSE 'Ligne de bus ' END || x.line)),
    unaccent(lower(CASE x.mode WHEN 'Tram' THEN 'Tramlijn ' WHEN 'Metro' THEN 'Metrolijn ' ELSE 'Buslijn ' END || x.line)),
    x.g, ST_Centroid(x.g)
FROM (
    SELECT l.mode, l.line, ST_Multi(ST_LineMerge(ST_Union(l.geometry))) AS g
    FROM urbis.stib_lines l
    GROUP BY l.mode, l.line
) x

UNION ALL
-- Railway lines (Infrabel), one row per line_ref ...
SELECT
    'RAILWAY:' || r.line_ref, 'RAILWAY', NULL, 'infrabel_lines',
    COALESCE(NULLIF(r.nm, ''), 'Ligne ferroviaire ' || r.line_ref),
    COALESCE(NULLIF(r.nm, ''), 'Spoorlijn ' || r.line_ref),
    unaccent(lower(COALESCE(NULLIF(r.nm, ''), 'Ligne ferroviaire ' || r.line_ref))),
    unaccent(lower(COALESCE(NULLIF(r.nm, ''), 'Spoorlijn ' || r.line_ref))),
    r.g, ST_Centroid(r.g)
FROM (
    SELECT line_ref, MIN(rail_name) AS nm, ST_Multi(ST_LineMerge(ST_Union(geometry))) AS g
    FROM urbis.infrabel_lines
    GROUP BY line_ref
) r
UNION ALL
-- ... and the whole railway network under its generic names ("chemin de fer", "spoorweg")
SELECT
    'RAILWAY:ALL' || v.sfx, 'RAILWAY', NULL, 'infrabel_lines',
    v.fr, v.nl, unaccent(lower(v.fr)), unaccent(lower(v.nl)), u.g, ST_Centroid(u.g)
FROM (SELECT ST_Multi(ST_LineMerge(ST_Union(geometry))) AS g FROM urbis.infrabel_lines) u
CROSS JOIN (VALUES ('', 'Chemin de fer', 'Spoorweg'), ('#2', 'Voie ferrée', 'Spoorlijn'), ('#3', 'Railway', 'Railway'))
    AS v(sfx, fr, nl)
WHERE u.g IS NOT NULL

UNION ALL
-- Reference roads in the belt around the Region (OSM), merged per name. CONTEXT ONLY: the
-- resolver applies CONTEXT_PENALTY to this type so a Brussels street always wins a tie.
SELECT
    'OUTSIDE_STREET:name:' || md5(x.nm), 'OUTSIDE_STREET', NULL, 'outside_roads',
    x.name_fr, x.name_nl, unaccent(lower(x.name_fr)), unaccent(lower(x.name_nl)), x.g, ST_Centroid(x.g)
FROM (
    SELECT unaccent(lower(o.name)) AS nm,
           MIN(COALESCE(NULLIF(o.name_fr, ''), o.name)) AS name_fr,
           MIN(COALESCE(NULLIF(o.name_nl, ''), o.name)) AS name_nl,
           ST_Multi(ST_LineMerge(ST_Union(o.geometry))) AS g
    FROM urbis.outside_roads o
    WHERE o.name IS NOT NULL AND o.name <> ''
    GROUP BY unaccent(lower(o.name))
) x
UNION ALL
-- ... and by road number (R0, E40, N8 ...; OSM may list several, ';'-separated)
SELECT
    'OUTSIDE_STREET:ref:' || md5(x.r), 'OUTSIDE_STREET', NULL, 'outside_roads',
    x.r, x.r, unaccent(lower(x.r)), unaccent(lower(x.r)), x.g, ST_Centroid(x.g)
FROM (
    SELECT btrim(rr.r) AS r, ST_Multi(ST_LineMerge(ST_Union(o.geometry))) AS g
    FROM urbis.outside_roads o
    CROSS JOIN LATERAL unnest(string_to_array(o.ref, ';')) AS rr(r)
    WHERE o.ref IS NOT NULL AND btrim(rr.r) <> ''
    GROUP BY btrim(rr.r)
) x

UNION ALL
-- Neighbouring municipalities (OSM admin_level 8, the 19 Brussels communes excluded).
SELECT
    'NEIGHBOUR_MUNICIPALITY:' || COALESCE(m.nis, m.osm_id), 'NEIGHBOUR_MUNICIPALITY', NULL, 'neighbour_municipalities',
    COALESCE(NULLIF(m.name_fr, ''), m.name),
    COALESCE(NULLIF(m.name_nl, ''), m.name),
    unaccent(lower(COALESCE(NULLIF(m.name_fr, ''), m.name))),
    unaccent(lower(COALESCE(NULLIF(m.name_nl, ''), m.name))),
    ST_Multi(m.geometry), ST_Centroid(m.geometry)
FROM urbis.neighbour_municipalities m
WHERE COALESCE(m.name_fr, m.name_nl, m.name) IS NOT NULL

UNION ALL
-- OSM POIs (type stays 'POI'; source_table = 'osm_pois' tells them apart). Main row = default
-- name, name:fr and name:nl when tagged ...
SELECT
    'OSM_POI:' || o.osm_id, 'POI', NULL, 'osm_pois',
    COALESCE(NULLIF(o.name_fr, ''), o.name),
    COALESCE(NULLIF(o.name_nl, ''), o.name),
    unaccent(lower(COALESCE(NULLIF(o.name_fr, ''), o.name))),
    unaccent(lower(COALESCE(NULLIF(o.name_nl, ''), o.name))),
    o.geometry, o.geometry
FROM urbis.osm_pois o
UNION ALL
-- ... plus one alias row per DISTINCT extra name (name:en, alt/official/short names) that
-- differs from the main names, id suffix '#<hash>'; the resolver collapses them back to the place.
SELECT
    'OSM_POI:' || x.osm_id || '#' || left(md5(x.an), 6), 'POI', NULL, 'osm_pois',
    x.a, x.a, x.an, x.an, x.geom, x.geom
FROM (
    SELECT o.osm_id, MIN(btrim(al.a)) AS a, unaccent(lower(btrim(al.a))) AS an,
           (array_agg(o.geometry))[1] AS geom
    FROM urbis.osm_pois o
    CROSS JOIN LATERAL unnest(string_to_array(concat_ws(';', o.name_en, o.alt_name), ';')) AS al(a)
    WHERE btrim(al.a) <> ''
      AND unaccent(lower(btrim(al.a))) IS DISTINCT FROM unaccent(lower(COALESCE(NULLIF(o.name_fr, ''), o.name)))
      AND unaccent(lower(btrim(al.a))) IS DISTINCT FROM unaccent(lower(COALESCE(NULLIF(o.name_nl, ''), o.name)))
    GROUP BY o.osm_id, unaccent(lower(btrim(al.a)))
) x
-- <<< REFERENCE BRANCHES END
) u;

-- Unique key needed for REFRESH MATERIALIZED VIEW CONCURRENTLY
CREATE UNIQUE INDEX IF NOT EXISTS ux_gazetteer_id ON urbis.gazetteer (id);
CREATE INDEX IF NOT EXISTS idx_gazetteer_type ON urbis.gazetteer (type);
CREATE INDEX IF NOT EXISTS idx_gazetteer_name_fr_trgm
    ON urbis.gazetteer USING gin (name_fr_norm gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_gazetteer_name_nl_trgm
    ON urbis.gazetteer USING gin (name_nl_norm gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_gazetteer_geom ON urbis.gazetteer USING gist (geom);
CREATE INDEX IF NOT EXISTS idx_gazetteer_centroid ON urbis.gazetteer USING gist (centroid);

-- -----------------------------------------------------------------------------
-- 2. GAZETTEER EMBEDDINGS: optional semantic fallback (pgvector), keyed off
--    the same stable "id" as the gazetteer. Populated separately by a
--    Python backfill job (see app/geo/embeddings.py) since it needs to call
--    an embedding model, not something plain SQL can do.
--    Dimension must match whichever _EMBEDDING_BACKENDS model you pick
--    (e.g. bge-m3 = 1024, nomic-embed-text = 768, all-minilm = 384) -- adjust
--    the vector(...) size below to match before running the backfill job.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS urbis.gazetteer_embeddings (
    gazetteer_id  text PRIMARY KEY,
    model         text NOT NULL,
    embedding     vector(1024) NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gazetteer_embeddings_ivfflat
    ON urbis.gazetteer_embeddings USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- -----------------------------------------------------------------------------
-- 3. STREET SEGMENTS ENRICHED: human-readable view over StreetSurfaces used
--    once a street name has already been resolved, to filter/describe the
--    matching segments (type, hierarchy, level = tunnel/street/bridge, admin).
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW urbis.v_street_segments AS
SELECT
    ss.inspire_id,
    ss.street_id,
    pn.namefre                     AS street_name_fr,
    pn.namedut                     AS street_name_nl,
    ss.type_id,
    ttd.typename                   AS type_name,
    ttd.descriptionfre             AS type_description_fr,
    ttd.descriptiondut             AS type_description_nl,
    ss.hierarchy_id,
    ss.lvl_id,
    ld.lvl                         AS level_value,
    CASE
        WHEN ld.lvl < 0 THEN 'TUNNEL'
        WHEN ld.lvl = 0 THEN 'STREET_LEVEL'
        WHEN ld.lvl > 0 THEN 'BRIDGE'
    END                             AS level_category,
    ss.admin_id,
    sad.admin                      AS admin_code,
    sad.descriptionfre             AS admin_description_fr,
    sad.descriptiondut             AS admin_description_nl,
    hd.hierarchy                   AS hierarchy_code,
    hd.hierarchyname               AS hierarchy_name,
    hd.hierarchylevel               AS hierarchy_level,
    hd.descriptionfre              AS hierarchy_description_fr,
    hd.descriptiondut              AS hierarchy_description_nl,
    ss.geometry
FROM urbis.streetsurfaces ss
LEFT JOIN urbis.placenames pn               ON pn.inspire_id = ss.street_id
LEFT JOIN urbis.streettypedomain ttd        ON ttd.inspire_id = ss.type_id
LEFT JOIN urbis.leveldomain ld              ON ld.inspire_id = ss.lvl_id
LEFT JOIN urbis.streetadministratordomain sad ON sad.inspire_id = ss.admin_id
LEFT JOIN urbis.streethierarchydomain hd    ON hd.inspire_id = ss.hierarchy_id;
-- hierarchy_id -> StreetHierarchyDomain is now wired in (previously left
-- as a plain id): hierarchy_name is StreetHierarchyDomain's own English
-- label ("Highway", "Metropolitan Road", "Main Road", ...), used directly
-- by the road-classification resolver fallback below (section 3b) so
-- terms like "highway" match without needing an FR/NL translation step.

-- -----------------------------------------------------------------------------
-- 3b. ROAD CLASSIFICATION GAZETTEER: revision notes 1.txt / 8.txt --
--     "highroad"/"highway" and "regional roads" are not named places, they
--     are a *classification* of road (hierarchy: Highway, Main Road, ...;
--     or administrator: REGION_RBC, MUN_VL, ...). This view gives the
--     resolver something matchable for those terms: one row per
--     classification value, geometry = every StreetSurfaces segment of
--     that class, merged. app/geo/resolver.py::resolve_road_class() tries
--     this only as a fallback when a normal STREET name-lookup on
--     urbis.gazetteer comes back empty/low-confidence, and only for
--     entities typed STREET or ANY -- see app/config.py's
--     ROAD_CLASS_SYNONYMS for the English/FR/NL alias list layered on top
--     of these rows for terms ("highroad", "motorway") that don't
--     literally match the domain-table text.
--     Same MS/RS/RT/MT exclusion as the main gazetteer (see section 1).
-- -----------------------------------------------------------------------------
DROP MATERIALIZED VIEW IF EXISTS urbis.road_class_gazetteer CASCADE;

CREATE MATERIALIZED VIEW urbis.road_class_gazetteer AS
-- By hierarchy (Highway, Metropolitan Road, Main Road, Inter-District
-- Road, District Collector, District Road, Trail, ...)
SELECT
    'HIERARCHY:' || hd.hierarchy                              AS id,
    'STREET'                                                   AS type,
    hd.inspire_id                                               AS source_id,
    'streethierarchydomain'                                      AS source_table,
    hd.hierarchyname                                             AS name_fr,  -- English label; no separate FR/NL split in this domain table
    hd.hierarchyname                                             AS name_nl,
    unaccent(lower(hd.hierarchyname))                            AS name_fr_norm,
    unaccent(lower(hd.hierarchyname))                            AS name_nl_norm,
    urbis.to3812(ST_Union(urbis.valid_poly(ss.geometry)))  AS geom,
    urbis.to3812(ST_Centroid(ST_Union(urbis.valid_poly(ss.geometry))))  AS centroid
FROM urbis.streetsurfaces ss
JOIN urbis.streethierarchydomain hd ON hd.inspire_id = ss.hierarchy_id
LEFT JOIN urbis.streettypedomain ttd ON ttd.inspire_id = ss.type_id
WHERE hd.hierarchyname IS NOT NULL
  AND (ttd.type IS NULL OR ttd.type NOT IN ('MS', 'RS', 'RT', 'MT'))
GROUP BY hd.hierarchy, hd.inspire_id, hd.hierarchyname

UNION ALL
-- By administrator (REGION_RBC, REGION_VL, MUN_VL, MUN_RBC, CITY OF
-- BRUSSELS, ...) -- e.g. "regional roads in Etterbeek" (revision 8.txt).
SELECT
    'ADMIN:' || sad.admin,
    'STREET',
    sad.inspire_id,
    'streetadministratordomain',
    sad.descriptionfre,
    sad.descriptiondut,
    unaccent(lower(sad.descriptionfre)),
    unaccent(lower(sad.descriptiondut)),
    urbis.to3812(ST_Union(urbis.valid_poly(ss.geometry))),
    urbis.to3812(ST_Centroid(ST_Union(urbis.valid_poly(ss.geometry))))
FROM urbis.streetsurfaces ss
JOIN urbis.streetadministratordomain sad ON sad.inspire_id = ss.admin_id
LEFT JOIN urbis.streettypedomain ttd ON ttd.inspire_id = ss.type_id
WHERE sad.admin IS NOT NULL
  AND (ttd.type IS NULL OR ttd.type NOT IN ('MS', 'RS', 'RT', 'MT'))
GROUP BY sad.admin, sad.inspire_id, sad.descriptionfre, sad.descriptiondut;

CREATE UNIQUE INDEX IF NOT EXISTS ux_road_class_gazetteer_id ON urbis.road_class_gazetteer (id);
CREATE INDEX IF NOT EXISTS idx_road_class_gazetteer_name_fr_trgm
    ON urbis.road_class_gazetteer USING gin (name_fr_norm gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_road_class_gazetteer_name_nl_trgm
    ON urbis.road_class_gazetteer USING gin (name_nl_norm gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_road_class_gazetteer_geom ON urbis.road_class_gazetteer USING gist (geom);

-- -----------------------------------------------------------------------------
-- 4. ADDRESS LOOKUP: resolves "12 <street name>, <municipality>" style
--    entities. Not part of the generic gazetteer because address matching
--    combines a fuzzy street name with an exact/near-exact house number.
--    Materialized (not a plain view) so the trigram index below is usable;
--    refresh it alongside the gazetteer after every sync_urbis.py run.
-- -----------------------------------------------------------------------------
DROP MATERIALIZED VIEW IF EXISTS urbis.mv_addresses_lookup CASCADE;

CREATE MATERIALIZED VIEW urbis.mv_addresses_lookup AS
SELECT
    a.inspire_id,
    a.street_id,
    pn.namefre                     AS street_name_fr,
    pn.namedut                     AS street_name_nl,
    unaccent(lower(pn.namefre))    AS street_name_fr_norm,
    unaccent(lower(pn.namedut))    AS street_name_nl_norm,
    a.policenum,
    a.boxnumber,
    a.municipality_id,
    m.namefre                      AS municipality_name_fr,
    m.namedut                      AS municipality_name_nl,
    urbis.to3812(a.geometry)       AS geometry
FROM urbis.addresses a
LEFT JOIN urbis.placenames pn      ON pn.inspire_id = a.street_id
LEFT JOIN urbis.municipalities m   ON m.inspire_id = a.municipality_id;

CREATE UNIQUE INDEX IF NOT EXISTS ux_addresses_lookup_inspire_id
    ON urbis.mv_addresses_lookup (inspire_id);
CREATE INDEX IF NOT EXISTS idx_addresses_lookup_street_fr_trgm
    ON urbis.mv_addresses_lookup USING gin (street_name_fr_norm gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_addresses_lookup_street_nl_trgm
    ON urbis.mv_addresses_lookup USING gin (street_name_nl_norm gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_addresses_lookup_geom
    ON urbis.mv_addresses_lookup USING gist (geometry);

-- -----------------------------------------------------------------------------
-- 5. Convenience refresh procedure, call after every sync_urbis.py run.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE urbis.refresh_gazetteer()
LANGUAGE plpgsql
AS $$
BEGIN
    -- order matters: routing network -> street groups -> gazetteer
    REFRESH MATERIALIZED VIEW CONCURRENTLY urbis.v_street_axes_routing;
    REFRESH MATERIALIZED VIEW CONCURRENTLY urbis.street_axes_nodes;
    REFRESH MATERIALIZED VIEW CONCURRENTLY urbis.v_route_edges;
    REFRESH MATERIALIZED VIEW CONCURRENTLY urbis.route_nodes;
    REFRESH MATERIALIZED VIEW CONCURRENTLY urbis.v_rail_edges;
    REFRESH MATERIALIZED VIEW CONCURRENTLY urbis.rail_nodes;
    REFRESH MATERIALIZED VIEW CONCURRENTLY urbis.street_groups;
    REFRESH MATERIALIZED VIEW CONCURRENTLY urbis.gazetteer;
    REFRESH MATERIALIZED VIEW CONCURRENTLY urbis.road_class_gazetteer;
    REFRESH MATERIALIZED VIEW CONCURRENTLY urbis.mv_addresses_lookup;
END;
$$;
