-- =============================================================================
-- urbis geometry functions (revision rounds 2-3). Applied by apply_lookup_views / the update script
-- AFTER lookup_views.sql. All geometry logic for the geo_query tool lives here, in the database
-- next to the data, so the SQL handed to the caller is just
--     WHERE ST_DWithin(t.geom, (SELECT urbis.geo_target('<plan json>'::jsonb)), <tolerance>)
-- and the report/map call exactly the same function (no second implementation to drift).
--
-- A plan is JSON built by app/geo/builder.py:
--   {"rel": "SELF|ON|NEAR|WITHIN|INTERSECTION|BETWEEN|FROM_TO",
--    "ents": [{"gid": "STREET:1858", "type": "STREET", "role": "main|anchor|area|start|via|end"} |
--             {"wkt": "POINT(..)", "type": "ADDRESS", "role": "start"}],
--    "buffer": null | metres,  "modifier": null | "TRAM",  "half_width": 40,
--    "clean": {"close": 2, "hole": 6000, "spur": 0.75, "part": 30}  | false}
--
-- Every geometry that enters or leaves these functions is forced to EPSG:3812 (urbis.to3812).
-- =============================================================================

DROP FUNCTION IF EXISTS urbis.geo_entity_edges(jsonb);
DROP FUNCTION IF EXISTS urbis._geo_pair(jsonb, jsonb, bigint[], text);
DROP FUNCTION IF EXISTS urbis.geo_route(jsonb, bigint[], text);
DROP FUNCTION IF EXISTS urbis.geo_surfaces_along(geometry, bigint[], double precision);

CREATE OR REPLACE FUNCTION urbis.geo_entity_geom(e jsonb) RETURNS geometry
LANGUAGE plpgsql STABLE AS $$
DECLARE g geometry;
BEGIN
    IF e ? 'wkt' THEN RETURN urbis.to3812(ST_SetSRID(ST_GeomFromText(e->>'wkt'), 3812)); END IF;
    SELECT geom INTO g FROM urbis.gazetteer WHERE id = e->>'gid';
    IF g IS NULL THEN SELECT geom INTO g FROM urbis.road_class_gazetteer WHERE id = e->>'gid'; END IF;
    RETURN urbis.to3812(g);
END $$;

-- Routable edge ids of a street-like entity (Region street group, or a belt road by name / by ref).
-- expand = true: ALL street groups of the same name class within 2.5 km (so the same road split into pieces by a park, a canal
-- or a spelling change is ONE scope); used for the MAIN street of "X between Y and Z", where the route must be able to cross such gaps.
CREATE OR REPLACE FUNCTION urbis.geo_entity_edges(e jsonb, expand boolean DEFAULT false) RETURNS bigint[]
LANGUAGE plpgsql STABLE AS $$
DECLARE v_gid text := e->>'gid'; r bigint[]; keys text[]; g0 geometry;
BEGIN
    IF v_gid IS NULL THEN RETURN NULL; END IF;
    IF v_gid LIKE 'STREET:%' THEN
        SELECT sg.axis_ids, sg.name_keys, sg.geom INTO r, keys, g0 FROM urbis.street_groups sg WHERE sg.gid = v_gid;
        IF expand AND keys IS NOT NULL THEN
            SELECT array_agg(DISTINCT x) INTO r FROM (
                SELECT unnest(sg.axis_ids) AS x FROM urbis.street_groups sg
                WHERE sg.name_keys && keys AND ST_DWithin(sg.geom, g0, 2500)) q;
        END IF;
    ELSIF v_gid LIKE 'OUTSIDE_STREET:name:%' THEN
        SELECT array_agg(re.id) INTO r FROM urbis.v_route_edges re
        WHERE re.origin = 'O' AND re.name_md5 = substr(v_gid, length('OUTSIDE_STREET:name:') + 1);
    ELSIF v_gid LIKE 'OUTSIDE_STREET:ref:%' THEN
        SELECT array_agg(re.id) INTO r FROM urbis.v_route_edges re
        WHERE re.origin = 'O' AND re.ref_md5s @> ARRAY[substr(v_gid, length('OUTSIDE_STREET:ref:') + 1)];
    END IF;
    IF r IS NULL OR cardinality(r) = 0 THEN RETURN NULL; END IF;
    RETURN r;
END $$;

-- Terminals of an anchor inside a network: the graph nodes a path may start/end at, with the cost
-- already "spent" to reach them (edge = the edge a point was projected on, m = position on it).
--   * street-like anchor : every node of its edges (unscoped) / the scope nodes lying ON it (scoped)
--                          -> routing goes between AXES, never to the centroid of one segment.
--   * point-like anchor  : the point is projected on the nearest edge; both ends are candidates.
CREATE OR REPLACE FUNCTION urbis._geo_terminals(a jsonb, scope bigint[], net text)
RETURNS TABLE(node bigint, off double precision, edge bigint, m double precision)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    et text := CASE net WHEN 'RAIL' THEN 'urbis.v_rail_edges' ELSE 'urbis.v_route_edges' END;
    nt text := CASE net WHEN 'RAIL' THEN 'urbis.rail_nodes'   ELSE 'urbis.route_nodes'   END;
    edges bigint[]; g geometry; pt geometry; er record; mm double precision;
BEGIN
    IF net <> 'RAIL' THEN edges := urbis.geo_entity_edges(a, false); END IF;
    IF edges IS NOT NULL THEN
        EXECUTE format('SELECT ST_Collect(geometry) FROM %s WHERE id = ANY($1)', et) INTO g USING edges;
        IF scope IS NULL THEN
            RETURN QUERY EXECUTE format(
                'SELECT DISTINCT q.n, 0::double precision, NULL::bigint, NULL::double precision FROM (
                   SELECT source AS n FROM %1$s WHERE id = ANY($1) UNION SELECT target FROM %1$s WHERE id = ANY($1)) q', et)
                USING edges;
            RETURN;
        END IF;
        RETURN QUERY EXECUTE format(
            'SELECT n.node_id, 0::double precision, NULL::bigint, NULL::double precision FROM %2$s n
              WHERE n.node_id IN (SELECT source FROM %1$s WHERE id = ANY($1) UNION SELECT target FROM %1$s WHERE id = ANY($1))
                AND ST_DWithin(n.geom, $2, 10)', et, nt) USING scope, g;
        IF FOUND THEN RETURN; END IF;
        -- the anchor does not touch the scope: take the scope node nearest to it, paying the gap
        RETURN QUERY EXECUTE format(
            'SELECT n.node_id, ST_Distance(n.geom, $2), NULL::bigint, NULL::double precision FROM %2$s n
              WHERE n.node_id IN (SELECT source FROM %1$s WHERE id = ANY($1) UNION SELECT target FROM %1$s WHERE id = ANY($1))
              ORDER BY n.geom <-> $2 LIMIT 1', et, nt) USING scope, g;
        RETURN;
    END IF;

    g := urbis.geo_entity_geom(a);
    IF g IS NULL THEN RETURN; END IF;
    pt := CASE WHEN ST_GeometryType(g) = 'ST_Point' THEN g ELSE ST_Centroid(g) END;
    EXECUTE format('SELECT id, source, target, cost, geometry FROM %s
                     WHERE ($1::bigint[] IS NULL OR id = ANY($1)) ORDER BY geometry <-> $2 LIMIT 1', et)
        INTO er USING scope, pt;
    IF er.id IS NULL THEN RETURN; END IF;
    mm := ST_LineLocatePoint(er.geometry, pt);
    RETURN QUERY VALUES (er.source, mm * er.cost, er.id, mm), (er.target, (1 - mm) * er.cost, er.id, mm);
END $$;

CREATE OR REPLACE FUNCTION urbis._geo_piece(et text, eid bigint, m double precision, n bigint) RETURNS geometry
LANGUAGE plpgsql STABLE AS $$
DECLARE er record;
BEGIN
    IF m IS NULL OR m < 0.0005 OR m > 0.9995 THEN RETURN NULL; END IF;
    EXECUTE format('SELECT source, geometry FROM %s WHERE id = $1', et) INTO er USING eid;
    IF er.geometry IS NULL THEN RETURN NULL; END IF;
    IF n = er.source THEN RETURN ST_LineSubstring(er.geometry, 0, m);
    ELSE RETURN ST_LineSubstring(er.geometry, m, 1); END IF;
END $$;


-- Cut rectangle just BEYOND a point of an edge: subtracting it from the selected road surface ends the selection flat at a
-- house number ("from number 52") instead of at the end of the road section.  forward = the removed side lies towards the edge's end.
CREATE OR REPLACE FUNCTION urbis._geo_cut_dir(et text, eid bigint, m double precision, forward boolean, depth double precision DEFAULT 150)
RETURNS geometry LANGUAGE plpgsql STABLE AS $$
DECLARE g geometry; q geometry; az double precision; ox double precision; oy double precision; nx double precision; ny double precision; qx double precision; qy double precision;
BEGIN
    EXECUTE format('SELECT geometry FROM %s WHERE id = $1', et) INTO g USING eid;
    IF g IS NULL OR m IS NULL THEN RETURN NULL; END IF;
    q := ST_LineInterpolatePoint(g, m);
    az := ST_Azimuth(ST_LineInterpolatePoint(g, GREATEST(m - 0.02, 0)), ST_LineInterpolatePoint(g, LEAST(m + 0.02, 1)));
    IF az IS NULL THEN RETURN NULL; END IF;
    IF NOT forward THEN az := az + pi(); END IF;
    ox := sin(az); oy := cos(az); nx := -oy; ny := ox; qx := ST_X(q); qy := ST_Y(q);
    RETURN ST_SetSRID(ST_MakePolygon(ST_MakeLine(ARRAY[
        ST_MakePoint(qx + nx * depth, qy + ny * depth), ST_MakePoint(qx - nx * depth, qy - ny * depth),
        ST_MakePoint(qx - nx * depth + ox * depth, qy - ny * depth + oy * depth),
        ST_MakePoint(qx + nx * depth + ox * depth, qy + ny * depth + oy * depth),
        ST_MakePoint(qx + nx * depth, qy + ny * depth)])), 3812);
END $$;

CREATE OR REPLACE FUNCTION urbis._geo_cut(et text, eid bigint, m double precision, n bigint) RETURNS geometry
LANGUAGE plpgsql STABLE AS $$
DECLARE src bigint;
BEGIN
    EXECUTE format('SELECT source FROM %s WHERE id = $1', et) INTO src USING eid;
    RETURN urbis._geo_cut_dir(et, eid, m, n = src);   -- the kept piece starts at node n: remove what lies beyond the point, away from n
END $$;

-- Cheapest connection between two anchors. Returns no row if there is none; a row with a NULL
-- path if the two anchors touch (two streets that cross: nothing lies between them).
-- cut_a / cut_b: end the selection flat at that anchor (only done for ADDRESS anchors at the ends of the route).
CREATE OR REPLACE FUNCTION urbis._geo_pair(a jsonb, b jsonb, scope bigint[], net text, cut_a boolean DEFAULT false, cut_b boolean DEFAULT false)
RETURNS TABLE(edge_ids bigint[], path geometry, cuts geometry)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    et text := CASE net WHEN 'RAIL' THEN 'urbis.v_rail_edges' ELSE 'urbis.v_route_edges' END;
    esql text; sa jsonb; sb jsonb; sn bigint[]; tn bigint[];
    best record; ids bigint[]; g geometry; ps geometry; pe geometry; ta record; tb record;
    ma double precision; mb double precision; cs geometry[] := ARRAY[]::geometry[];
BEGIN
    SELECT jsonb_agg(to_jsonb(t)) INTO sa FROM urbis._geo_terminals(a, scope, net) t;
    SELECT jsonb_agg(to_jsonb(t)) INTO sb FROM urbis._geo_terminals(b, scope, net) t;
    IF sa IS NULL OR sb IS NULL THEN RETURN; END IF;

    -- both anchors project on the very same edge: the stretch between them is on that edge
    IF (sa->0->>'edge') IS NOT NULL AND (sa->0->>'edge') = (sb->0->>'edge') THEN
        ma := (sa->0->>'m')::double precision; mb := (sb->0->>'m')::double precision;
        EXECUTE format('SELECT geometry FROM %s WHERE id = $1', et) INTO g USING (sa->0->>'edge')::bigint;
        IF abs(ma - mb) > 0.0005 THEN
            edge_ids := ARRAY[(sa->0->>'edge')::bigint];
            path := ST_LineSubstring(g, least(ma, mb), greatest(ma, mb));
            IF cut_a AND a->>'type' = 'ADDRESS' THEN
                cs := cs || urbis._geo_cut_dir(et, edge_ids[1], ma, ma > mb);
            END IF;
            IF cut_b AND b->>'type' = 'ADDRESS' THEN
                cs := cs || urbis._geo_cut_dir(et, edge_ids[1], mb, mb > ma);
            END IF;
            SELECT ST_Union(x) INTO cuts FROM unnest(cs) x WHERE x IS NOT NULL;   -- union: two cut rectangles may overlap
            RETURN NEXT; RETURN;
        END IF;
    END IF;

    SELECT array_agg(DISTINCT (x->>'node')::bigint) INTO sn FROM jsonb_array_elements(sa) x;
    SELECT array_agg(DISTINCT (x->>'node')::bigint) INTO tn FROM jsonb_array_elements(sb) x;
    esql := CASE WHEN scope IS NULL THEN format('SELECT id, source, target, cost, reverse_cost FROM %s', et)
                 ELSE format('SELECT id, source, target, cost, reverse_cost FROM %s WHERE id = ANY(%L::bigint[])%s', et, scope::text,
                      CASE WHEN net = 'RAIL' THEN '' ELSE ' OR origin = ''B''' END) END;  -- bridges join Region and belt

    WITH ts AS (SELECT * FROM jsonb_to_recordset(sa) AS t(node bigint, off double precision, edge bigint, m double precision)),
         te AS (SELECT * FROM jsonb_to_recordset(sb) AS t(node bigint, off double precision, edge bigint, m double precision)),
         c  AS (SELECT start_vid, end_vid, agg_cost FROM pgr_dijkstraCost(esql, sn, tn, false)
                UNION ALL
                SELECT ts.node, te.node, 0::double precision FROM ts JOIN te ON ts.node = te.node)
    SELECT c.start_vid, c.end_vid, c.agg_cost + ts.off + te.off AS total INTO best
    FROM c JOIN ts ON ts.node = c.start_vid JOIN te ON te.node = c.end_vid
    ORDER BY total LIMIT 1;
    IF best.start_vid IS NULL THEN RETURN; END IF;

    IF best.start_vid <> best.end_vid THEN
        SELECT array_agg(d.edge ORDER BY d.path_seq) INTO ids
        FROM pgr_dijkstra(esql, best.start_vid, best.end_vid, false) d WHERE d.edge <> -1;
    END IF;
    IF ids IS NOT NULL THEN
        EXECUTE format('SELECT ST_Collect(geometry) FROM %s WHERE id = ANY($1)', et) INTO g USING ids;
    END IF;

    SELECT * INTO ta FROM jsonb_to_recordset(sa) AS t(node bigint, off double precision, edge bigint, m double precision) WHERE node = best.start_vid LIMIT 1;
    SELECT * INTO tb FROM jsonb_to_recordset(sb) AS t(node bigint, off double precision, edge bigint, m double precision) WHERE node = best.end_vid LIMIT 1;
    IF ta.edge IS NOT NULL THEN
        ps := urbis._geo_piece(et, ta.edge, ta.m, ta.node);
        IF cut_a AND a->>'type' = 'ADDRESS' THEN cs := cs || urbis._geo_cut(et, ta.edge, ta.m, ta.node); END IF;
    END IF;
    IF tb.edge IS NOT NULL THEN
        pe := urbis._geo_piece(et, tb.edge, tb.m, tb.node);
        IF cut_b AND b->>'type' = 'ADDRESS' THEN cs := cs || urbis._geo_cut(et, tb.edge, tb.m, tb.node); END IF;
    END IF;

    edge_ids := COALESCE(ids, ARRAY[]::bigint[]);
    IF ta.edge IS NOT NULL THEN edge_ids := edge_ids || ta.edge; END IF;
    IF tb.edge IS NOT NULL THEN edge_ids := edge_ids || tb.edge; END IF;
    SELECT ST_CollectionExtract(ST_Collect(x), 2) INTO path FROM unnest(ARRAY[g, ps, pe]) x WHERE x IS NOT NULL;  -- flat MultiLineString
    SELECT ST_Union(x) INTO cuts FROM unnest(cs) x WHERE x IS NOT NULL;
    RETURN NEXT;
END $$;

-- Chain of anchors (start, via..., end). NULL result = no connection for at least one leg.
-- Cut planes are only produced at the FIRST and LAST anchor of the chain (a "via" address must not cut the route itself).
CREATE OR REPLACE FUNCTION urbis.geo_route(anchors jsonb, scope bigint[], net text DEFAULT 'ROUTE')
RETURNS TABLE(edge_ids bigint[], path geometry, cuts geometry)
LANGUAGE plpgsql STABLE AS $$
DECLARE i int; n int := jsonb_array_length(anchors); ids bigint[] := ARRAY[]::bigint[]; gs geometry[] := ARRAY[]::geometry[];
        cs geometry[] := ARRAY[]::geometry[]; p record; ok boolean;
BEGIN
    IF n < 2 THEN RETURN; END IF;
    FOR i IN 0 .. n - 2 LOOP
        ok := false;
        FOR p IN SELECT * FROM urbis._geo_pair(anchors->i, anchors->(i + 1), scope, net, i = 0, i = n - 2) LOOP
            ok := true;
            ids := ids || p.edge_ids;
            IF p.path IS NOT NULL THEN gs := gs || p.path; END IF;
            IF p.cuts IS NOT NULL THEN cs := cs || p.cuts; END IF;
        END LOOP;
        IF NOT ok THEN RETURN; END IF;
    END LOOP;
    edge_ids := ids;
    path := CASE WHEN cardinality(gs) = 0 THEN NULL ELSE ST_CollectionExtract(ST_Collect(gs), 2) END;
    cuts := CASE WHEN cardinality(cs) = 0 THEN NULL ELSE ST_Union(cs) END;
    RETURN NEXT;
END $$;

-- =============================================================================
-- CLEANING (revision round 3, items 3/5/6/14/20/23/24/28: "clean up small donuts, small lines created because of topological
-- errors"). A union of UrbIS road polygons that do not quite touch leaves hairline gaps (drawn as lines inside the result),
-- tiny holes and thin spurs. In this order:
--   1. closing  (buffer +close, -close, mitre joins): fills gaps / hairlines narrower than 2*close metres;
--   2. holes smaller than hole_m2 are filled (small donuts; also fills a roundabout's centre island -> "the whole roundabout");
--   3. opening  (buffer -spur, +spur): removes threads and spurs narrower than 2*spur metres;
--   4. parts smaller than part_m2 are dropped (never the last one).
-- Lines and points are returned unchanged.
-- =============================================================================
CREATE OR REPLACE FUNCTION urbis._geo_drop_holes(g geometry, hole_m2 double precision) RETURNS geometry
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT ST_SetSRID(ST_Multi(ST_Collect(
        ST_MakePolygon(ST_ExteriorRing(p.geom),
            COALESCE(ARRAY(SELECT ST_ExteriorRing(h.geom) FROM ST_DumpRings(p.geom) h
                           WHERE h.path[1] > 0 AND ST_Area(h.geom) >= hole_m2), ARRAY[]::geometry[])))), ST_SRID(g))
    FROM ST_Dump(g) p
    WHERE ST_GeometryType(p.geom) = 'ST_Polygon'
$$;

CREATE OR REPLACE FUNCTION urbis.geo_clean(g geometry, close_m double precision DEFAULT 2, hole_m2 double precision DEFAULT 6000,
                                           spur_m double precision DEFAULT 0.75, part_m2 double precision DEFAULT 30)
RETURNS geometry LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE r geometry; kept geometry; srid int;
BEGIN
    IF g IS NULL OR ST_IsEmpty(g) OR ST_Dimension(g) < 2 THEN RETURN g; END IF;   -- lines and points are never touched
    srid := ST_SRID(g);
    r := urbis.valid_poly(g);
    IF r IS NULL THEN RETURN g; END IF;
    IF close_m > 0 THEN
        r := ST_Buffer(ST_Buffer(r, close_m, 'join=mitre mitre_limit=2'), -close_m, 'join=mitre mitre_limit=2');
    END IF;
    r := COALESCE(urbis.valid_poly(r), r);
    IF hole_m2 > 0 THEN r := COALESCE(urbis._geo_drop_holes(r, hole_m2), r); END IF;
    IF spur_m > 0 THEN
        kept := ST_Buffer(ST_Buffer(r, -spur_m, 'join=mitre mitre_limit=2'), spur_m, 'join=mitre mitre_limit=2');   -- mitre: corners stay square
        IF kept IS NOT NULL AND NOT ST_IsEmpty(kept) THEN r := kept; END IF;
    END IF;
    IF part_m2 > 0 THEN
        kept := (SELECT ST_Multi(ST_Collect(d.geom)) FROM ST_Dump(r) d WHERE ST_GeometryType(d.geom) = 'ST_Polygon' AND ST_Area(d.geom) >= part_m2);
        IF kept IS NOT NULL AND NOT ST_IsEmpty(kept) THEN r := kept; END IF;
    END IF;
    r := urbis.valid_poly(r);
    RETURN ST_SetSRID(r, srid);
END $$;

-- ST_Area(g) / area of its minimum bounding circle: ~0.5 for a square / roundabout, ~0.02 for a long road strip
CREATE OR REPLACE FUNCTION urbis._geo_is_compact(g geometry, max_area double precision DEFAULT 60000) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT g IS NOT NULL AND NOT ST_IsEmpty(g) AND ST_Area(g) BETWEEN 200 AND max_area
           AND ST_Area(g) / NULLIF(ST_Area(ST_MinimumBoundingCircle(g)), 0) >= 0.2
$$;

-- Does the road that polygon `s` (street sid) belongs to run PARALLEL to path p near it (within 30 degrees)?
-- Used for lanes / service roads with another street id that lie alongside the route (they are part of the street's width),
-- never for side streets (perpendicular).
CREATE OR REPLACE FUNCTION urbis._geo_parallel(s geometry, sid bigint, p geometry) RETURNS boolean
LANGUAGE plpgsql STABLE AS $$
DECLARE cp geometry; l geometry; f double precision; azp double precision; azs double precision; ax geometry; d double precision;
BEGIN
    IF sid IS NULL THEN RETURN false; END IF;
    cp := ST_ClosestPoint(p, ST_Centroid(s));
    SELECT dd.geom INTO l FROM ST_Dump(p) dd ORDER BY ST_Distance(dd.geom, cp) LIMIT 1;
    IF l IS NULL OR ST_GeometryType(l) <> 'ST_LineString' THEN RETURN false; END IF;
    f := ST_LineLocatePoint(l, cp);
    azp := ST_Azimuth(ST_LineInterpolatePoint(l, GREATEST(f - 0.02, 0)), ST_LineInterpolatePoint(l, LEAST(f + 0.02, 1)));
    SELECT e.geometry INTO ax FROM urbis.v_street_axes_routing e
    WHERE e.street_id = sid AND ST_Intersects(e.geometry, s) ORDER BY ST_Length(ST_Intersection(e.geometry, s)) DESC LIMIT 1;
    IF azp IS NULL OR ax IS NULL THEN RETURN false; END IF;
    azs := ST_Azimuth(ST_StartPoint(ax), ST_EndPoint(ax));
    IF azs IS NULL THEN RETURN false; END IF;
    d := abs(azp - azs); d := d - pi() * floor(d / pi());
    RETURN least(d, pi() - d) < radians(30);
END $$;

-- =============================================================================
-- The ROAD along a path (revision round 3: "whole width of the street", roundabouts / squares / crossroads whole, the arm of a
-- side street NOT, no gaps along the track).  Geometry decides, not street names:
--   core   = every street surface the path itself runs over, WHOLE (carriageway, intersection, place, roundabout ring ...), whatever
--            its street_id: the Region axes follow the real road, the surface names do not always (item 28: "a lot of gaps");
--   sides  = surfaces next to the core that belong to the same street(s) -- sidewalks, parking, medians, lanes -- when at least 70 %
--            of them lies within 20 m of the core (a perpendicular side street leaves the core quickly, so it does not qualify), plus
--            lanes of ANOTHER street id when they run parallel to the path;
--   gaps   = path stretches no surface covers (missing data, belt roads) become a 10 m wide band, so the track has no holes;
--   cuts   = half-plane rectangles beyond a house-number end of the route.
-- =============================================================================
CREATE OR REPLACE FUNCTION urbis.geo_surfaces_along(p geometry, edge_ids bigint[], half_w double precision DEFAULT 40, cuts geometry DEFAULT NULL)
RETURNS geometry LANGUAGE plpgsql STABLE AS $$
DECLARE
    p3 geometry; core geometry; core_ids bigint[]; sids bigint[]; pids bigint[]; dil geometry; side geometry; new_side geometry;
    taken bigint[]; new_ids bigint[]; res geometry; uncovered geometry; rnd int;
BEGIN
    IF p IS NULL THEN RETURN NULL; END IF;
    p := urbis.to3812(p);
    p3 := ST_Buffer(p, 2);

    SELECT array_agg(s.inspire_id::bigint), ST_Union(urbis.valid_poly(s.geometry)), array_agg(DISTINCT s.street_id::bigint) FILTER (WHERE s.street_id IS NOT NULL)
      INTO core_ids, core, sids
    FROM urbis.streetsurfaces s LEFT JOIN urbis.streettypedomain t ON t.inspire_id = s.type_id
    WHERE ST_Intersects(s.geometry, p3) AND (t.type IS NULL OR t.type NOT IN ('MS', 'RS', 'RT', 'MT'));

    IF core IS NOT NULL THEN
        SELECT array_agg(DISTINCT x) INTO pids FROM (
            SELECT unnest(sg.pids) AS x FROM urbis.street_groups sg
            WHERE sg.pids && (COALESCE(sids, ARRAY[]::bigint[])
                              || COALESCE((SELECT array_agg(DISTINCT re.street_id) FROM urbis.v_route_edges re WHERE re.id = ANY(edge_ids) AND re.street_id IS NOT NULL), ARRAY[]::bigint[]))
            UNION SELECT unnest(COALESCE(sids, ARRAY[]::bigint[]))) q;
        taken := core_ids;
        -- two growth rounds: round 1 = sidewalks / parking / medians right beside the road, round 2 = lanes beyond them
        -- (never further than half_w from the path; a side street leaves the road, so < 70 % of it ever lies in the growth zone)
        FOR rnd IN 1 .. 2 LOOP
            dil := ST_SimplifyPreserveTopology(ST_Buffer(CASE WHEN side IS NULL THEN core ELSE ST_Union(core, side) END, 20), 3);
            SELECT ST_Union(v.g), array_agg(v.iid) INTO new_side, new_ids FROM (
                SELECT urbis.valid_poly(s.geometry) AS g, s.street_id::bigint AS sid, s.inspire_id::bigint AS iid
                FROM urbis.streetsurfaces s LEFT JOIN urbis.streettypedomain t ON t.inspire_id = s.type_id
                WHERE ST_Intersects(s.geometry, dil)
                  AND NOT (s.inspire_id::bigint = ANY(taken))
                  AND ST_DWithin(s.geometry, p, half_w)
                  AND (t.type IS NULL OR t.type NOT IN ('MS', 'RS', 'RT', 'MT'))
            ) v
            WHERE v.g IS NOT NULL AND ST_Area(v.g) > 0
              AND ST_Area(ST_Intersection(v.g, dil)) / ST_Area(v.g) >= 0.7
              AND (v.sid = ANY(COALESCE(pids, ARRAY[]::bigint[]))
                   OR (v.sid IS NULL AND ST_Area(ST_Intersection(v.g, dil)) / ST_Area(v.g) >= 0.9)   -- unnamed sidewalk / parking right beside the road
                   OR urbis._geo_parallel(v.g, v.sid, p));
            EXIT WHEN new_side IS NULL;
            side := CASE WHEN side IS NULL THEN new_side ELSE ST_Union(side, new_side) END;
            taken := taken || new_ids;
        END LOOP;
        res := CASE WHEN side IS NULL THEN core ELSE ST_Union(core, side) END;
    END IF;

    -- stretches of the track that no surface covers (belt roads, data holes): a 10 m wide band
    IF res IS NULL THEN uncovered := p; ELSE uncovered := ST_Difference(p, ST_Buffer(res, 1)); END IF;
    IF uncovered IS NOT NULL AND NOT ST_IsEmpty(uncovered) AND ST_Length(uncovered) > 1 THEN
        uncovered := ST_Buffer(uncovered, 10, 'endcap=flat join=round');
        res := CASE WHEN res IS NULL THEN uncovered ELSE ST_Union(res, uncovered) END;
    END IF;

    IF cuts IS NOT NULL AND res IS NOT NULL THEN res := ST_Difference(urbis.valid_poly(res), ST_MakeValid(cuts)); END IF;
    IF res IS NULL OR ST_IsEmpty(res) THEN res := ST_Buffer(p, 12, 'endcap=flat join=round'); END IF;
    RETURN urbis.to3812(res);
END $$;

-- Region part of a numbered road (E40, A12 ...): the Highway-class surfaces connected to its belt part.
CREATE OR REPLACE FUNCTION urbis.geo_ref_extend(g geometry) RETURNS geometry
LANGUAGE sql STABLE AS $$
    WITH h AS (
        SELECT v.geom, ST_ClusterDBSCAN(v.geom, eps := 30, minpoints := 1) OVER () AS cid FROM (
            SELECT urbis.valid_poly(s.geometry) AS geom
            FROM urbis.streetsurfaces s
            JOIN urbis.streethierarchydomain hd ON hd.inspire_id = s.hierarchy_id AND hd.hierarchy = 'H') v
        WHERE v.geom IS NOT NULL)
    SELECT ST_Union(geom) FROM h WHERE cid IN (SELECT cid FROM h WHERE ST_DWithin(geom, g, 60))
$$;

-- forces EPSG:3812 and cleans polygonal results (zones keep their official boundary: mode 'area' is never cleaned)
CREATE OR REPLACE FUNCTION urbis._geo_finish(g geometry, mode text, plan jsonb) RETURNS geometry
LANGUAGE plpgsql STABLE AS $$
DECLARE c jsonb := plan->'clean'; r geometry;
BEGIN
    IF g IS NULL THEN RETURN NULL; END IF;
    r := urbis.to3812(g);
    IF COALESCE(c, 'true'::jsonb) = 'false'::jsonb THEN RETURN r; END IF;
    IF mode IS DISTINCT FROM 'area' AND ST_Dimension(r) = 2 AND ST_Area(r) < 50000000 THEN
        r := urbis.geo_clean(r,
                COALESCE((c->>'close')::double precision, 2), COALESCE((c->>'hole')::double precision, 6000),
                COALESCE((c->>'spur')::double precision, 0.75), COALESCE((c->>'part')::double precision, 30));
    END IF;
    RETURN r;
END $$;

CREATE OR REPLACE FUNCTION urbis.geo_plan(plan jsonb)
RETURNS TABLE(target geometry, path geometry, mode text, note text)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    rel text := upper(COALESCE(plan->>'rel', 'SELF'));
    buf double precision := NULLIF(plan->>'buffer', '')::double precision;
    half_w double precision := COALESCE(NULLIF(plan->>'half_width', '')::double precision, 40);
    modifier text := upper(plan->>'modifier');
    ents jsonb := COALESCE(plan->'ents', '[]'::jsonb);
    main jsonb; anchors jsonb; area geometry; base geometry; g geometry; e jsonb; x jsonb;
    scope bigint[]; r record; routed boolean := false; tram_ids bigint[]; prims geometry[] := ARRAY[]::geometry[];
BEGIN
    SELECT y INTO main FROM jsonb_array_elements(ents) y WHERE y->>'role' = 'main' LIMIT 1;
    SELECT ST_Union(urbis.geo_entity_geom(y)) INTO area FROM jsonb_array_elements(ents) y WHERE y->>'role' = 'area';
    SELECT jsonb_agg(y ORDER BY CASE y->>'role' WHEN 'start' THEN 0 WHEN 'via' THEN 1 ELSE 2 END, ord)
      INTO anchors
      FROM jsonb_array_elements(ents) WITH ORDINALITY t(y, ord) WHERE y->>'role' IN ('start', 'via', 'end');

    -- ------------------------------------------------------------------ BETWEEN / FROM_TO
    IF rel IN ('BETWEEN', 'FROM_TO') AND anchors IS NOT NULL AND jsonb_array_length(anchors) >= 2 THEN
        IF main IS NOT NULL AND main->>'type' = 'RAILWAY' THEN
            FOR r IN SELECT * FROM urbis.geo_route(anchors, NULL, 'RAIL') LOOP
                path := r.path; mode := 'rail_between';
                target := ST_Buffer(r.path, COALESCE(buf, 20));
                routed := true;
            END LOOP;
        ELSE
            scope := CASE WHEN main IS NOT NULL THEN urbis.geo_entity_edges(main, true) END;
            FOR r IN SELECT * FROM urbis.geo_route(anchors, scope, 'ROUTE') LOOP
                path := r.path; mode := CASE WHEN scope IS NULL THEN 'route' ELSE 'route_on_main_street' END;
                target := urbis.geo_surfaces_along(r.path, r.edge_ids, half_w, r.cuts);
                routed := true;
            END LOOP;
            IF NOT routed AND scope IS NOT NULL THEN
                note := 'no path along the main street between the anchors; used the shortest path instead';
                FOR r IN SELECT * FROM urbis.geo_route(anchors, NULL, 'ROUTE') LOOP
                    path := r.path; mode := 'route_fallback_unscoped';
                    target := urbis.geo_surfaces_along(r.path, r.edge_ids, half_w, r.cuts);
                    routed := true;
                END LOOP;
            END IF;
            -- a square / roundabout that IS an anchor ("from Square X via Place Y to Place Z") is taken whole
            IF routed AND target IS NOT NULL THEN
                FOR x IN SELECT y FROM jsonb_array_elements(anchors) y WHERE (y->>'gid') LIKE 'STREET:%' LOOP
                    g := urbis.geo_entity_geom(x);
                    IF urbis._geo_is_compact(g) THEN target := ST_Union(target, g); END IF;
                END LOOP;
            END IF;
        END IF;
        IF NOT routed THEN
            SELECT ST_MakeLine(array_agg(CASE WHEN ST_GeometryType(gg) = 'ST_Point' THEN gg ELSE ST_Centroid(gg) END)) INTO g
            FROM (SELECT urbis.geo_entity_geom(y) AS gg FROM jsonb_array_elements(anchors) y) q WHERE gg IS NOT NULL;
            path := g; mode := 'fallback_line';
            note := COALESCE(note || '; ', '') || 'no network path found; straight corridor between the anchors';
            target := ST_Buffer(g, 25);
        ELSIF target IS NULL THEN
            mode := 'anchors_touch'; note := 'the anchors touch each other (nothing lies between them)';
        END IF;
        IF area IS NOT NULL AND target IS NOT NULL THEN target := ST_Intersection(target, area); END IF;
        target := urbis._geo_finish(target, mode, plan);
        path := urbis.to3812(path);
        RETURN NEXT; RETURN;
    END IF;

    -- ------------------------------------------------------------------ SELF / ON / NEAR / WITHIN / INTERSECTION
    FOR e IN SELECT y FROM jsonb_array_elements(ents) y WHERE y->>'role' IN ('main', 'anchor', 'start', 'via', 'end') LOOP
        IF modifier = 'TRAM' AND (e->>'gid') LIKE 'STREET:%' THEN
            SELECT array_agg(id), ST_CollectionExtract(ST_Collect(geometry), 2) INTO tram_ids, g FROM urbis.v_route_edges
            WHERE id = ANY(urbis.geo_entity_edges(e, false)) AND type_code IN ('C', 'SC', 'IC', 'AC');
            IF tram_ids IS NOT NULL THEN
                path := g; mode := 'tram_axes';
                g := urbis.geo_surfaces_along(g, tram_ids, half_w, NULL);
            ELSE
                note := 'the street has no axis of a tram type (C / SC / IC / AC); returned the whole street';
                g := urbis.geo_entity_geom(e); mode := 'street_no_tram';
            END IF;
        ELSE
            g := urbis.geo_entity_geom(e);
            IF (e->>'gid') LIKE 'OUTSIDE_STREET:ref:%' AND g IS NOT NULL THEN
                g := COALESCE(ST_Union(g, urbis.geo_ref_extend(g)), g);
                note := COALESCE(note || '; ', '') || 'numbered road: belt part plus the connected Highway-class surfaces of the Region';
            END IF;
        END IF;
        IF g IS NOT NULL THEN prims := prims || g; END IF;
    END LOOP;

    IF rel = 'INTERSECTION' AND cardinality(prims) >= 2 THEN
        base := ST_Intersection(prims[1], prims[2]);
    ELSIF cardinality(prims) > 0 THEN
        base := ST_Union(prims);
    END IF;
    IF area IS NOT NULL THEN
        base := CASE WHEN base IS NULL THEN area ELSE ST_Intersection(base, area) END;
    END IF;
    mode := COALESCE(mode, CASE WHEN area IS NOT NULL AND cardinality(prims) = 0 THEN 'area' ELSE 'geometry' END);
    IF base IS NOT NULL AND buf IS NOT NULL AND buf > 0 THEN base := ST_Buffer(base, buf); END IF;
    target := urbis._geo_finish(base, mode, plan);
    path := urbis.to3812(path);
    RETURN NEXT;
END $$;

CREATE OR REPLACE FUNCTION urbis.geo_target(plan jsonb) RETURNS geometry
LANGUAGE sql STABLE AS $$ SELECT target FROM urbis.geo_plan(plan) $$;
