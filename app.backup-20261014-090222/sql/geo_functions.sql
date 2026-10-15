-- =============================================================================
-- urbis geometry functions (revision round 2). Applied by apply_lookup_views / the update script
-- AFTER lookup_views.sql. All geometry logic for the geo_query tool lives here, in the database
-- next to the data, so the SQL handed to the caller is just
--     WHERE ST_DWithin(t.geom, (SELECT urbis.geo_target('<plan json>'::jsonb)), <tolerance>)
-- and the report/map call exactly the same function (no second implementation to drift).
--
-- A plan is JSON built by app/geo/builder.py:
--   {"rel": "SELF|ON|NEAR|WITHIN|INTERSECTION|BETWEEN|FROM_TO",
--    "ents": [{"gid": "STREET:1858", "type": "STREET", "role": "main|anchor|area|start|via|end"} |
--             {"wkt": "POINT(..)", "type": "ADDRESS", "role": "start"}],
--    "buffer": null | metres,  "modifier": null | "TRAM",  "half_width": 40}
-- =============================================================================

CREATE OR REPLACE FUNCTION urbis.geo_entity_geom(e jsonb) RETURNS geometry
LANGUAGE plpgsql STABLE AS $$
DECLARE g geometry;
BEGIN
    IF e ? 'wkt' THEN RETURN ST_SetSRID(ST_GeomFromText(e->>'wkt'), 3812); END IF;
    SELECT geom INTO g FROM urbis.gazetteer WHERE id = e->>'gid';
    IF g IS NULL THEN SELECT geom INTO g FROM urbis.road_class_gazetteer WHERE id = e->>'gid'; END IF;
    RETURN g;
END $$;

-- Routable edge ids of a street-like entity (Region street group, or a belt road by name / by ref).
CREATE OR REPLACE FUNCTION urbis.geo_entity_edges(e jsonb) RETURNS bigint[]
LANGUAGE plpgsql STABLE AS $$
DECLARE v_gid text := e->>'gid'; r bigint[];
BEGIN
    IF v_gid IS NULL THEN RETURN NULL; END IF;
    IF v_gid LIKE 'STREET:%' THEN
        SELECT sg.axis_ids INTO r FROM urbis.street_groups sg WHERE sg.gid = v_gid;
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
    IF net <> 'RAIL' THEN edges := urbis.geo_entity_edges(a); END IF;
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

-- Cheapest connection between two anchors. Returns no row if there is none; a row with a NULL
-- path if the two anchors touch (two streets that cross: nothing lies between them).
CREATE OR REPLACE FUNCTION urbis._geo_pair(a jsonb, b jsonb, scope bigint[], net text)
RETURNS TABLE(edge_ids bigint[], path geometry)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    et text := CASE net WHEN 'RAIL' THEN 'urbis.v_rail_edges' ELSE 'urbis.v_route_edges' END;
    esql text; sa jsonb; sb jsonb; sn bigint[]; tn bigint[];
    best record; ids bigint[]; g geometry; ps geometry; pe geometry; ta record; tb record;
    ea record; ma double precision; mb double precision;
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
    IF ta.edge IS NOT NULL THEN ps := urbis._geo_piece(et, ta.edge, ta.m, ta.node); END IF;
    IF tb.edge IS NOT NULL THEN pe := urbis._geo_piece(et, tb.edge, tb.m, tb.node); END IF;

    edge_ids := COALESCE(ids, ARRAY[]::bigint[]);
    IF ta.edge IS NOT NULL THEN edge_ids := edge_ids || ta.edge; END IF;
    IF tb.edge IS NOT NULL THEN edge_ids := edge_ids || tb.edge; END IF;
    SELECT ST_CollectionExtract(ST_Collect(x), 2) INTO path FROM unnest(ARRAY[g, ps, pe]) x WHERE x IS NOT NULL;  -- flat MultiLineString
    RETURN NEXT;
END $$;

-- Chain of anchors (start, via..., end). NULL result = no connection for at least one leg.
CREATE OR REPLACE FUNCTION urbis.geo_route(anchors jsonb, scope bigint[], net text DEFAULT 'ROUTE')
RETURNS TABLE(edge_ids bigint[], path geometry)
LANGUAGE plpgsql STABLE AS $$
DECLARE i int; n int := jsonb_array_length(anchors); ids bigint[] := ARRAY[]::bigint[]; gs geometry[] := ARRAY[]::geometry[]; p record; ok boolean;
BEGIN
    IF n < 2 THEN RETURN; END IF;
    FOR i IN 0 .. n - 2 LOOP
        ok := false;
        FOR p IN SELECT * FROM urbis._geo_pair(anchors->i, anchors->(i + 1), scope, net) LOOP
            ok := true;
            ids := ids || p.edge_ids;
            IF p.path IS NOT NULL THEN gs := gs || p.path; END IF;
        END LOOP;
        IF NOT ok THEN RETURN; END IF;
    END LOOP;
    edge_ids := ids;
    path := CASE WHEN cardinality(gs) = 0 THEN NULL ELSE ST_CollectionExtract(ST_Collect(gs), 2) END;
    RETURN NEXT;
END $$;

-- The road surface ALONG a path: only polygons of the streets the path runs on (street_id in the
-- street groups of its edges) plus the junction polygons lying on it -- never adjacent roads --
-- cut flat at the ends of the path (so "from number 52" really starts at number 52).
-- Belt parts of the path (no surfaces there) become a 10 m-wide band.
CREATE OR REPLACE FUNCTION urbis.geo_surfaces_along(p geometry, edge_ids bigint[], half_w double precision DEFAULT 40)
RETURNS geometry LANGUAGE plpgsql STABLE AS $$
DECLARE
    sids bigint[]; pids bigint[]; corridor geometry; res geometry; reg geometry; outside geometry;
BEGIN
    IF p IS NULL THEN RETURN NULL; END IF;
    SELECT array_agg(DISTINCT street_id) INTO sids FROM urbis.v_route_edges WHERE id = ANY(edge_ids) AND street_id IS NOT NULL;
    SELECT array_agg(DISTINCT x) INTO pids FROM (SELECT unnest(sg.pids) AS x FROM urbis.street_groups sg WHERE sg.pids && sids) q;
    pids := COALESCE(pids, sids, ARRAY[]::bigint[]);
    corridor := ST_Buffer(p, half_w, 'endcap=flat join=round');
    SELECT ST_CollectionExtract(ST_Union(ST_Intersection(
               CASE WHEN ST_IsValid(s.geometry) THEN s.geometry ELSE ST_CollectionExtract(ST_MakeValid(s.geometry), 3) END,
               corridor)), 3) INTO res
    FROM urbis.streetsurfaces s
    LEFT JOIN urbis.streettypedomain t ON t.inspire_id = s.type_id
    WHERE ST_Intersects(s.geometry, corridor)
      AND (t.type IS NULL OR t.type NOT IN ('MS', 'RS', 'RT', 'MT'))
      AND (s.street_id = ANY(pids)
           OR (t.type IN ('I', 'IC', 'IL') AND ST_DWithin(s.geometry, p, 3)));
    SELECT geom INTO reg FROM urbis.gazetteer WHERE type = 'REGION' LIMIT 1;
    IF reg IS NOT NULL THEN
        outside := ST_Difference(p, reg);
        IF outside IS NOT NULL AND NOT ST_IsEmpty(outside) THEN
            res := ST_Union(res, ST_Buffer(outside, 10, 'endcap=flat join=round'));
        END IF;
    END IF;
    IF res IS NULL OR ST_IsEmpty(res) THEN res := ST_Buffer(p, 12, 'endcap=flat join=round'); END IF;
    RETURN res;
END $$;

-- Region part of a numbered road (E40, A12 ...): the Highway-class surfaces connected to its belt part.
CREATE OR REPLACE FUNCTION urbis.geo_ref_extend(g geometry) RETURNS geometry
LANGUAGE sql STABLE AS $$
    WITH h AS (
        SELECT v.geom, ST_ClusterDBSCAN(v.geom, eps := 30, minpoints := 1) OVER () AS cid FROM (
        SELECT CASE WHEN ST_IsValid(s.geometry) THEN s.geometry ELSE ST_CollectionExtract(ST_MakeValid(s.geometry), 3) END AS geom
        FROM urbis.streetsurfaces s
        JOIN urbis.streethierarchydomain hd ON hd.inspire_id = s.hierarchy_id AND hd.hierarchy = 'H') v)
    SELECT ST_Union(geom) FROM h WHERE cid IN (SELECT cid FROM h WHERE ST_DWithin(geom, g, 60))
$$;

CREATE OR REPLACE FUNCTION urbis.geo_plan(plan jsonb)
RETURNS TABLE(target geometry, path geometry, mode text, note text)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    rel text := upper(COALESCE(plan->>'rel', 'SELF'));
    buf double precision := NULLIF(plan->>'buffer', '')::double precision;
    half_w double precision := COALESCE(NULLIF(plan->>'half_width', '')::double precision, 40);
    modifier text := upper(plan->>'modifier');
    ents jsonb := COALESCE(plan->'ents', '[]'::jsonb);
    main jsonb; anchors jsonb; area geometry; base geometry; g geometry; e jsonb;
    scope bigint[]; r record; routed boolean := false; tram_ids bigint[]; prims geometry[] := ARRAY[]::geometry[];
BEGIN
    SELECT x INTO main FROM jsonb_array_elements(ents) x WHERE x->>'role' = 'main' LIMIT 1;
    SELECT ST_Union(urbis.geo_entity_geom(x)) INTO area FROM jsonb_array_elements(ents) x WHERE x->>'role' = 'area';
    SELECT jsonb_agg(x ORDER BY CASE x->>'role' WHEN 'start' THEN 0 WHEN 'via' THEN 1 ELSE 2 END, ord)
      INTO anchors
      FROM jsonb_array_elements(ents) WITH ORDINALITY t(x, ord) WHERE x->>'role' IN ('start', 'via', 'end');

    -- ------------------------------------------------------------------ BETWEEN / FROM_TO
    IF rel IN ('BETWEEN', 'FROM_TO') AND anchors IS NOT NULL AND jsonb_array_length(anchors) >= 2 THEN
        IF main IS NOT NULL AND main->>'type' = 'RAILWAY' THEN
            FOR r IN SELECT * FROM urbis.geo_route(anchors, NULL, 'RAIL') LOOP
                path := r.path; mode := 'rail_between';
                target := ST_Buffer(r.path, COALESCE(buf, 20));
                routed := true;
            END LOOP;
        ELSE
            scope := CASE WHEN main IS NOT NULL THEN urbis.geo_entity_edges(main) END;
            FOR r IN SELECT * FROM urbis.geo_route(anchors, scope, 'ROUTE') LOOP
                path := r.path; mode := CASE WHEN scope IS NULL THEN 'route' ELSE 'route_on_main_street' END;
                target := urbis.geo_surfaces_along(r.path, r.edge_ids, half_w);
                routed := true;
            END LOOP;
            IF NOT routed AND scope IS NOT NULL THEN
                note := 'no path along the main street between the anchors; used the shortest path instead';
                FOR r IN SELECT * FROM urbis.geo_route(anchors, NULL, 'ROUTE') LOOP
                    path := r.path; mode := 'route_fallback_unscoped';
                    target := urbis.geo_surfaces_along(r.path, r.edge_ids, half_w);
                    routed := true;
                END LOOP;
            END IF;
        END IF;
        IF NOT routed THEN
            SELECT ST_MakeLine(array_agg(CASE WHEN ST_GeometryType(gg) = 'ST_Point' THEN gg ELSE ST_Centroid(gg) END)) INTO g
            FROM (SELECT urbis.geo_entity_geom(x) AS gg FROM jsonb_array_elements(anchors) x) q WHERE gg IS NOT NULL;
            path := g; mode := 'fallback_line';
            note := COALESCE(note || '; ', '') || 'no network path found; straight corridor between the anchors';
            target := ST_Buffer(g, 25);
        ELSIF target IS NULL THEN
            mode := 'anchors_touch'; note := 'the anchors touch each other (nothing lies between them)';
        END IF;
        IF area IS NOT NULL AND target IS NOT NULL THEN target := ST_Intersection(target, area); END IF;
        RETURN NEXT; RETURN;
    END IF;

    -- ------------------------------------------------------------------ SELF / ON / NEAR / WITHIN / INTERSECTION
    FOR e IN SELECT x FROM jsonb_array_elements(ents) x WHERE x->>'role' IN ('main', 'anchor', 'start', 'via', 'end') LOOP
        IF modifier = 'TRAM' AND (e->>'gid') LIKE 'STREET:%' THEN
            SELECT array_agg(id), ST_CollectionExtract(ST_Collect(geometry), 2) INTO tram_ids, g FROM urbis.v_route_edges
            WHERE id = ANY(urbis.geo_entity_edges(e)) AND type_code IN ('C', 'SC', 'IC', 'AC');
            IF tram_ids IS NOT NULL THEN
                path := g; mode := 'tram_axes';
                g := urbis.geo_surfaces_along(g, tram_ids, half_w);
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
    target := base;
    RETURN NEXT;
END $$;

CREATE OR REPLACE FUNCTION urbis.geo_target(plan jsonb) RETURNS geometry
LANGUAGE sql STABLE AS $$ SELECT target FROM urbis.geo_plan(plan) $$;
