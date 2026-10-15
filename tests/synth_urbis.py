"""
tests/synth_urbis.py
=====================
Builds a small SYNTHETIC "urbis" schema in a scratch PostGIS database (never your real one) with the
same table/column layout as the real UrbIS load, then applies sql/lookup_views.sql and
sql/geo_functions.sql to it. The city is a 2000 x 2000 m square ("Region") on a grid, with the traps
found in the revision rounds built in on purpose:

  * Avenue Louise = ONE street stored under THREE placename ids (only one carries the axes),
    crossed by three vertical streets whose junction polygons carry the CROSSING street's id
  * a T-junction side street (Rue Delta) that must NOT end up in Louise's geometry
  * Avenue de Jette (x=1000): the part above y=1000 has tram-type axes (SC)
  * Chaussee de Ninove / Ninoofsesteenweg y=1500: two placenames ("Ninoofsesteenweg" /
    "Ninoofse Steenweg") + belt (OSM) pieces east and west of the Region, crossed by the R0
    (a belt way WITHOUT a node at the crossing) on both sides
  * Avenue Marcel Thiry (x=1750) crossing Avenue Theodore De Cuyper (y=1750), addresses with
    box-number duplicates; Rue Alpha addresses 29 / 90 on one edge
  * same-name collisions: Rue du Chien Vert (street) / Chien Vert (STIB stop) / CHIEN VERT (POIs),
    Place Meiser / Meiser stop / MEISER district, Grand-Place, Churchill roundabout, Mutsaard
  * a railway with two stations and a branch, tram line 8, a Highway-class (E40) piece in the Region

All coordinates are metres relative to (OX, OY) in EPSG:3812.
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg2

OX, OY = 150000.0, 170000.0
APP_SQL = Path(__file__).resolve().parent.parent / "app" / "sql"


def dsn(dbname: str = "geotest") -> str:
    return (f"host={os.environ.get('PGHOST', 'localhost')} port={os.environ.get('PGPORT', '5432')} "
            f"user={os.environ.get('PGUSER', 'postgres')} password={os.environ.get('PGPASSWORD', 'pw')} dbname={dbname}")


def P(x, y):
    return f"{OX + x} {OY + y}"


def poly(x0, y0, x1, y1):
    return f"POLYGON(({P(x0,y0)},{P(x1,y0)},{P(x1,y1)},{P(x0,y1)},{P(x0,y0)}))"


def line(*pts):
    return "LINESTRING(" + ",".join(P(x, y) for x, y in pts) + ")"


def point(x, y):
    return f"POINT({P(x,y)})"


DDL = """
DROP SCHEMA IF EXISTS urbis CASCADE;
CREATE SCHEMA urbis;
CREATE EXTENSION IF NOT EXISTS postgis; CREATE EXTENSION IF NOT EXISTS pgrouting;
CREATE EXTENSION IF NOT EXISTS pg_trgm;  CREATE EXTENSION IF NOT EXISTS unaccent; CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE urbis.region (inspire_id bigint, namefre text, namedut text, geometry geometry);
CREATE TABLE urbis.municipalities (inspire_id bigint, namefre text, namedut text, geometry geometry);
CREATE TABLE urbis.statisticalsectors (id bigint, inspire_id text, namefre text, namedut text, geometry geometry);
CREATE TABLE urbis.monitoringdistricts (inspire_id bigint, namefre text, namedut text, geometry geometry);
CREATE TABLE urbis.pointsofinterest (inspire_id bigint, namefre text, namedut text, geometry geometry);
CREATE TABLE urbis.placenames (inspire_id bigint, namefre text, namedut text);
CREATE TABLE urbis.streetsurfaces (inspire_id bigint, street_id bigint, type_id bigint, hierarchy_id bigint,
                                   lvl_id bigint, admin_id bigint, geometry geometry);
CREATE TABLE urbis.streetaxes (inspire_id double precision, startsn_id double precision, endsn_id double precision,
                               street_id double precision, type_id double precision, hierarchy_id double precision, geometry geometry);
CREATE TABLE urbis.streettypedomain (inspire_id bigint, type text, typename text, descriptionfre text, descriptiondut text);
CREATE TABLE urbis.streethierarchydomain (inspire_id bigint, hierarchy text, hierarchyname text, hierarchylevel text,
                                          descriptionfre text, descriptiondut text);
CREATE TABLE urbis.streetadministratordomain (inspire_id bigint, admin text, descriptionfre text, descriptiondut text);
CREATE TABLE urbis.leveldomain (inspire_id bigint, lvl int);
CREATE TABLE urbis.addresses (inspire_id bigint, street_id bigint, statsector_id double precision, policenum text,
                              boxnumber text, parentid double precision, bu_id double precision, capa_id double precision,
                              zipcode_id bigint, municipality_id bigint, cartoangle real, geometry geometry);
"""

TYPES = {1: "S", 2: "I", 3: "IC", 4: "IL", 5: "C", 6: "SC", 7: "MS", 8: "P", 9: "SW", 10: "M", 11: "K"}
HIER = {1: "H", 2: "MER", 3: "MAR", 4: "IDR", 5: "DC", 6: "DR", 7: "T"}
T = {v: k for k, v in TYPES.items()}
H = {v: k for k, v in HIER.items()}


class Builder:
    def __init__(self, cur):
        self.cur = cur
        self.sid = 0
        self.aid = 0
        self.nodes: dict[tuple[float, float], int] = {}

    def node(self, x, y):
        return self.nodes.setdefault((x, y), 100000 + len(self.nodes) + 1)

    def ins(self, sql, *args):
        self.cur.execute(sql, args)

    def surface(self, street_id, ty, hier, g):
        self.sid += 1
        self.ins("INSERT INTO urbis.streetsurfaces VALUES (%s,%s,%s,%s,1,1,ST_GeomFromText(%s,3812))",
                 self.sid, street_id, T[ty], H[hier], g)

    def axis(self, street_id, ty, hier, a, b):
        self.aid += 1
        self.ins("INSERT INTO urbis.streetaxes VALUES (%s,%s,%s,%s,%s,%s,ST_GeomFromText(%s,3812))",
                 self.aid, self.node(*a), self.node(*b), street_id, T[ty], H[hier], line(a, b))

    def chain(self, street_id, hier, pts, tram_from=None, axis_street_id=None):
        """Axes between consecutive points; points on a street must contain every junction node."""
        for a, b in zip(pts, pts[1:]):
            ty = "SC" if tram_from is not None and min(a[1], b[1]) >= tram_from else "S"
            self.axis(axis_street_id or street_id, ty, hier, a, b)


def build(cur, corrupt_srid: bool = False):
    cur.execute(DDL)
    b = Builder(cur)
    for k, v in TYPES.items():
        cur.execute("INSERT INTO urbis.streettypedomain VALUES (%s,%s,%s,%s,%s)", (k, v, v, v, v))
    for k, v in HIER.items():
        cur.execute("INSERT INTO urbis.streethierarchydomain VALUES (%s,%s,%s,%s,%s,%s)",
                    (k, v, {"H": "Highway", "MER": "Metropolitan Road", "MAR": "Main Road", "IDR": "Inter-District Road",
                            "DC": "District Collector", "DR": "District Road", "T": "Trail"}[v], f"A{k-1}", v, v))
    cur.execute("INSERT INTO urbis.streetadministratordomain VALUES (1,'REGION_RBC','Tronçon géré par la Région (RBC)','Sectie beheerd door het Gewest (BHG)'),"
                "(2,'MUN_RBC','Tronçon géré par la commune (RBC)','Sectie beheerd door de gemeeente (BHG)')")
    cur.execute("INSERT INTO urbis.leveldomain VALUES (1,0)")

    cur.execute("INSERT INTO urbis.region VALUES (1,'Région de Bruxelles-Capitale','Brussels Hoofdstedelijk Gewest',ST_GeomFromText(%s,3812))", (poly(0, 0, 2000, 2000),))
    for i, (n, nl, box) in enumerate([("Etterbeek", "Etterbeek", (0, 0, 1000, 1000)), ("Oudergem", "Auderghem", (1000, 0, 2000, 1000)),
                                      ("Jette", "Jette", (1000, 1000, 2000, 2000)), ("Ixelles", "Elsene", (0, 1000, 1000, 2000))], 1):
        cur.execute("INSERT INTO urbis.municipalities VALUES (%s,%s,%s,ST_GeomFromText(%s,3812))", (i, n, nl, poly(*box)))
    cur.execute("INSERT INTO urbis.monitoringdistricts VALUES (1,'GRAND PLACE','GROTE MARKT',ST_GeomFromText(%s,3812)),"
                "(2,'MUTSAARD','MUTSAARD',ST_GeomFromText(%s,3812)),(3,'CHURCHILL','CHURCHILL',ST_GeomFromText(%s,3812)),"
                "(4,'MEISER','MEISER',ST_GeomFromText(%s,3812))",
                (poly(100, 1850, 200, 1950), poly(1600, 100, 1800, 300), poly(300, 1600, 400, 1700), poly(700, 700, 800, 800)))
    cur.execute("INSERT INTO urbis.statisticalsectors VALUES (1,'21001A01-','Secteur Test','Testsector',ST_GeomFromText(%s,3812)), (2,'21002B02-','MUTSAARD (AVENUE)','MUTSAARD (AVENUE)',ST_GeomFromText(%s,3812))",
                (poly(0, 0, 200, 200), poly(1650, 150, 1750, 250)))

    # ---- Avenue Louise: 3 placename ids, axes only on 1858
    for pid in (751, 1858, 3919):
        cur.execute("INSERT INTO urbis.placenames VALUES (%s,'Avenue Louise','Louizalaan')", (pid,))
    for pid, x0, x1 in ((751, 0, 485), (1858, 515, 985), (1858, 1015, 1485), (3919, 1515, 2000)):
        b.surface(pid, "S", "MAR", poly(x0, 985, x1, 1015))
    L = [(0, 1000), (500, 1000), (750, 1000), (1000, 1000), (1500, 1000), (2000, 1000)]
    b.chain(1858, "MAR", L)

    # ---- crossing streets (junction polygons carry the CROSSING street's id / NULL)
    names = {11: ("Rue Alpha", "Alphastraat", 500), 12: ("Avenue de Jette", "Jetselaan", 1000), 13: ("Rue Gamma", "Gammastraat", 1500)}
    sections = {
        11: [(0, 485, 11), (515, 985, 11), (1015, 1485, 11), (1515, 2000, 11)],
        12: [(0, 485, 12), (515, 735, 12), (765, 985, 12), (1015, 1485, 12), (1515, 2000, 12)],
        # section 1515-1600 carries ANOTHER street id (99) although the axes stay Rue Gamma: the "gaps" of round 3 (item 28)
        13: [(0, 485, 13), (515, 735, 13), (765, 985, 13), (1015, 1485, 13), (1515, 1600, 99), (1600, 1735, 13), (1765, 2000, 13)],
    }
    cur.execute("INSERT INTO urbis.placenames VALUES (99,'Rue Autre','Anderestraat')")
    for pid, (fr, nl, x) in names.items():
        cur.execute("INSERT INTO urbis.placenames VALUES (%s,%s,%s)", (pid, fr, nl))
        hier = "MAR" if pid == 12 else "DR"
        for y0, y1, sid in sections[pid]:
            b.surface(sid, "S", hier, poly(x - 10, y0, x + 10, y1))
        pts = [(x, 0), (x, 500)] + ([(x, 750)] if pid in (12, 13) else []) + [(x, 1000), (x, 1500)] + ([(x, 1750)] if pid == 13 else []) + [(x, 2000)]
        b.chain(pid, hier, pts, tram_from=1000 if pid == 12 else None)
        b.surface(None, "I", "DR", poly(x - 15, 1485, x + 15, 1515))           # crossing with the Ninoofsesteenweg
        if pid in (12, 13):
            b.surface(None, "I", "DR", poly(x - 15, 735, x + 15, 765))         # crossing with Avenue Large
    b.surface(11, "I", "DR", poly(485, 985, 515, 1015))   # Alpha x Louise  (id of the crossing street)
    b.surface(12, "I", "MAR", poly(985, 985, 1015, 1015))  # Beta  x Louise
    b.surface(13, "I", "DR", poly(1485, 985, 1515, 1015))  # Gamma x Louise
    b.surface(None, "I", "DR", poly(1485, 1735, 1515, 1765))  # Gamma x Theodore De Cuyper
    # ---- T-junction side street Rue Delta (x=750, y>1000): must NOT become part of Avenue Louise
    cur.execute("INSERT INTO urbis.placenames VALUES (14,'Rue Delta','Deltastraat')")
    b.surface(14, "S", "DR", poly(740, 1015, 760, 2000))
    b.surface(14, "I", "DR", poly(735, 1015, 765, 1045))
    b.chain(14, "DR", [(750, 1000), (750, 2000)])

    # ---- Rue de la Loi (y=500), one id, junction squares with NULL street_id
    cur.execute("INSERT INTO urbis.placenames VALUES (1679,'Rue de la Loi','Wetstraat')")
    xs = [0, 500, 1000, 1500, 2000]
    for x0, x1 in ((0, 485), (515, 985), (1015, 1485), (1515, 2000)):
        b.surface(1679, "S", "MER", poly(x0, 485, x1, 515))
    for x in (500, 1000, 1500):
        b.surface(None, "I", "MER", poly(x - 15, 485, x + 15, 515))
    b.chain(1679, "MER", [(x, 500) for x in xs])

    # ---- Ninoofsesteenweg / Chaussee de Ninove (y=1500): two placenames, two spellings
    cur.execute("INSERT INTO urbis.placenames VALUES (21,'Chaussée de Ninove','Ninoofsesteenweg'),(22,'Chaussée de Ninove','Ninoofse Steenweg')")
    for x0, x1, sid in ((0, 485, 21), (515, 985, 21), (1015, 1485, 22), (1515, 1735, 22), (1765, 2000, 22)):
        b.surface(sid, "S", "MAR", poly(x0, 1485, x1, 1515))
    b.surface(None, "I", "DR", poly(1735, 1485, 1765, 1515))   # Marcel Thiry x Ninoofsesteenweg
    b.chain(21, "MAR", [(0, 1500), (500, 1500), (1000, 1500), (1500, 1500), (1750, 1500), (2000, 1500)])

    # ---- Marcel Thiry x Theodore De Cuyper (Jette), addresses
    cur.execute("INSERT INTO urbis.placenames VALUES (31,'Avenue Marcel Thiry','Marcel Thirylaan'),(32,'Avenue Théodore De Cuyper','Théodore De Cuyperlaan')")
    b.surface(31, "S", "DR", poly(1740, 1515, 1760, 1735))
    b.surface(31, "S", "DR", poly(1740, 1765, 1760, 2000))
    b.surface(None, "I", "DR", poly(1735, 1735, 1765, 1765))
    b.surface(32, "S", "DR", poly(1510, 1740, 1735, 1760))
    b.surface(32, "S", "DR", poly(1765, 1740, 2000, 1760))
    b.chain(31, "DR", [(1750, 1500), (1750, 1750), (1750, 2000)])
    b.chain(32, "DR", [(1500, 1750), (1750, 1750), (2000, 1750)])
    A = 0
    for num, box, x, y in (("50", None, 1738, 1620), ("52", None, 1762, 1650), ("52", "0001", 1762, 1651), ("52", "0002", 1762, 1652),
                           ("54", None, 1762, 1700), ("160", None, 1738, 1900)):
        A += 1
        cur.execute("INSERT INTO urbis.addresses VALUES (%s,31,NULL,%s,%s,NULL,NULL,NULL,1090,3,0,ST_GeomFromText(%s,3812))", (A, num, box, point(x, y)))
    for num, y in (("29", 100), ("90", 450), ("48", 200)):
        A += 1
        cur.execute("INSERT INTO urbis.addresses VALUES (%s,11,NULL,%s,NULL,NULL,NULL,NULL,1000,1,0,ST_GeomFromText(%s,3812))", (A, num, point(512, y)))

    # ---- Avenue Large (y=750, between Avenue de Jette and Rue Gamma): the WIDTH of a street (round 3, item 5):
    #      carriageway + two sidewalks (same street id) + parking (NO street id) + a service lane with ANOTHER street id,
    #      and a side street Rue Stub (x=1250, going north) that must NOT come along
    cur.execute("INSERT INTO urbis.placenames VALUES (70,'Avenue Large','Grote Laan'),(71,'Contre-allée Large','Large Zijstraat'),(72,'Rue Stub','Stubstraat')")
    b.surface(70, "S", "MAR", poly(1015, 743, 1485, 757))
    b.surface(70, "SW", "MAR", poly(1015, 735, 1485, 743))
    b.surface(70, "SW", "MAR", poly(1015, 757, 1485, 765))
    b.surface(None, "K", "DR", poly(1015, 765, 1485, 772))
    b.surface(71, "S", "DR", poly(1015, 775, 1485, 785))
    b.surface(72, "S", "DR", poly(1243, 790, 1257, 950))                 # side street: leaves the road
    b.surface(72, "S", "DR", poly(1243, 765, 1257, 790))
    b.chain(70, "MAR", [(1000, 750), (1250, 750), (1500, 750)])
    b.axis(71, "S", "DR", (1015, 780), (1485, 780))                      # the lane's own axis (not connected to the network)
    b.axis(72, "S", "DR", (1250, 750), (1250, 950))
    # ---- Place Compact: a square at the end of a route (whole square wanted)
    cur.execute("INSERT INTO urbis.placenames VALUES (74,'Place Compact','Compact Plein')")
    b.surface(74, "P", "DR", poly(1060, 560, 1140, 640))
    b.axis(74, "S", "DR", (1000, 500), (1100, 600))
    # ---- spelling variants of ONE road (item 18): Tervueren / Tervuren, FR and NL names, 240 m apart
    cur.execute("INSERT INTO urbis.placenames VALUES (80,'Avenue de Tervueren','Tervurenlaan'),(81,'Avenue de Tervuren','Tervurenlaan'),(82,'Avenue de Tervueren',NULL)")
    b.surface(80, "S", "MAR", poly(100, 1400, 400, 1414))
    b.surface(81, "S", "MAR", poly(640, 1400, 900, 1414))
    b.surface(82, "S", "MAR", poly(400, 1400, 420, 1414))
    # addresses on Avenue Theodore De Cuyper (round 3, item 23: address -> address across a crossing)
    for num, x, y in (("20", 1850, 1738), ("22", 1860, 1762)):
        cur.execute("INSERT INTO urbis.addresses VALUES (%s,32,NULL,%s,NULL,NULL,NULL,NULL,1090,3,0,ST_GeomFromText(%s,3812))", (900 + int(num), num, point(x, y)))

    # ---- name-collision fixtures
    for pid, fr, nl, g in ((51, "Rue du Chien Vert", "Groene Hondstraat", poly(290, 1100, 310, 1300)),
                           (52, "Place Général Meiser", "Generaal Meiserplein", poly(700, 700, 800, 800)),
                           (53, "Grand-Place - Bruxelles", "Grote Markt - Brussel", poly(100, 1850, 200, 1950)),
                           (54, "Rond-Point Winston Churchill", "Winston Churchillrotonde", poly(300, 1600, 400, 1700)),
                           (55, "Avenue Edmond Machtens", "Edmond Machtenslaan", poly(1300, 100, 1320, 400)),
                           (56, "Boulevard Auguste Reyers", "Auguste Reyerslaan", poly(1190, 1700, 1210, 2000))):
        cur.execute("INSERT INTO urbis.placenames VALUES (%s,%s,%s)", (pid, fr, nl))
        b.surface(pid, "S", "H" if pid == 56 else "DR", g)
    cur.execute("INSERT INTO urbis.pointsofinterest VALUES (1,'CHIEN VERT','GROENE HOND',ST_GeomFromText(%s,3812)),(2,'CHIEN VERT','GROENE HOND',ST_GeomFromText(%s,3812)),"
                "(3,'GRAND-PLACE','GROTE MARKT',ST_GeomFromText(%s,3812)),(4,'GARE DE JETTE','STATION JETTE',ST_GeomFromText(%s,3812)),"
                "(5,'BOCKSTAEL','BOCKSTAEL',ST_GeomFromText(%s,3812)),(6,'MAISON COMMUNALE','GEMEENTEHUIS',ST_GeomFromText(%s,3812))",
                (point(300, 1150), point(305, 1160), point(150, 1900), point(400, 210), point(1400, 205), point(1480, 1180)))

    # ---- placeholders created by lookup_views.sql get replaced here with data
    # (the real tables are written by sync_urbis.py; the script's CREATE TABLE IF NOT EXISTS keeps ours)
    cur.execute("""CREATE TABLE urbis.stib_stops (gid bigint, line text, mode text, stop_id text, name_fr text, name_nl text, geometry geometry);
        INSERT INTO urbis.stib_stops VALUES
         (1,'{12}','Bus','1546','Chien Vert','Groene Hond',ST_GeomFromText(%s,3812)),
         (2,'{12}','Bus','1547','Chien Vert','Groene Hond',ST_GeomFromText(%s,3812)),
         (3,'{8}','Tram','2001','Meiser','Meiser',ST_GeomFromText(%s,3812)),
         (4,'{1}','Metro','3001','Weststation','Weststation',ST_GeomFromText(%s,3812));""",
                (point(800, 1300), point(802, 1301), point(750, 750), point(1250, 1480)))
    cur.execute("""CREATE TABLE urbis.osm_pois (osm_id text, name text, category text, geometry geometry(Point,3812),
                  name_nl text, name_fr text, name_en text, alt_name text, wikidata text);
        INSERT INTO urbis.osm_pois (osm_id,name,category,geometry,name_fr,name_nl) VALUES
         ('node/1','Le Chien Vert','restaurant',ST_GeomFromText(%s,3812),'Le Chien Vert','Le Chien Vert'),
         ('way/2','Palais Stoclet','attraction',ST_GeomFromText(%s,3812),'Palais Stoclet','Stocletpaleis'),
         ('node/3','Rondpoint Churchill','junction',ST_GeomFromText(%s,3812),'Rondpoint Churchill','Rotonde Churchill'),
         ('node/4','Grand Place','square',ST_GeomFromText(%s,3812),'Grand Place','Grote Markt');""",
                (point(310, 1180), point(1700, 1100), point(350, 1650), point(150, 1900)))
    cur.execute("""CREATE TABLE urbis.stib_lines (line text, mode text, geometry geometry);
        INSERT INTO urbis.stib_lines VALUES ('8','Tram',ST_GeomFromText(%s,3812)),('3','Tram',ST_GeomFromText(%s,3812));""",
                (line((0, 300), (2000, 300)), line((0, 1100), (2000, 1100))))
    cur.execute("""CREATE TABLE urbis.infrabel_lines (line_ref text, rail_name text, geometry geometry);
        INSERT INTO urbis.infrabel_lines VALUES ('50','Ligne 50',ST_GeomFromText(%s,3812)),('50','Ligne 50',ST_GeomFromText(%s,3812)),('28','Ligne 28',ST_GeomFromText(%s,3812));""",
                (line((0, 200), (1000, 200)), line((1000, 200), (2000, 200)), line((1000, 200), (1000, 400))))
    cur.execute("INSERT INTO urbis.infrabel_lines VALUES ('99','Ligne test',ST_GeomFromText(%s,3812))",
                (f"GEOMETRYCOLLECTION({line((0, 600), (500, 600))},{point(1, 1)})",))
    cur.execute("""CREATE TABLE urbis.outside_roads (osm_id text, name text, name_nl text, name_fr text, ref text, highway text, geometry geometry);
        INSERT INTO urbis.outside_roads VALUES
         ('w1','Ninoofsesteenweg','Ninoofsesteenweg','Chaussée de Ninove',NULL,'primary',ST_GeomFromText(%s,3812)),
         ('w2','Ninoofsesteenweg','Ninoofsesteenweg','Chaussée de Ninove',NULL,'primary',ST_GeomFromText(%s,3812)),
         ('w3','Ring','Ring','Ring','R0','motorway',ST_GeomFromText(%s,3812)),
         ('w4','Ring','Ring','Ring','R0','motorway',ST_GeomFromText(%s,3812)),
         ('w5','Autoroute de Liège','Luikersteenweg','Autoroute de Liège','E40','motorway',ST_GeomFromText(%s,3812)),
         ('w9','Rue Test','Teststraat','Rue Test',NULL,'tertiary',ST_GeomFromText(%s,3812)),
         ('w10','Rue Multi','Multistraat','Rue Multi',NULL,'tertiary',ST_GeomFromText(%s,3812));""",
                (line((2000, 1500), (2600, 1500)), line((-600, 1500), (0, 1500)), line((2300, 0), (2300, 3000)),
                 line((-400, 0), (-400, 3000)), line((1200, 2000), (1200, 2600)),
                 # sync_urbis writes MultiLineStrings (linemerge); ST_Collect of those is a GeometryCollection, which ST_Node rejects
                 f"GEOMETRYCOLLECTION({line((2100, 100), (2100, 400))},{point(5, 5)})",
                 f"MULTILINESTRING(({P(2200,100)},{P(2200,400)}),({P(2200,400)},{P(2250,450)}))"))
    cur.execute("""CREATE TABLE urbis.neighbour_municipalities (osm_id text, nis text, name text, name_nl text, name_fr text, geometry geometry);""")
    if corrupt_srid:
        # every stored geometry loses its SRID (what the real database showed): the SQL layer must repair it, not crash
        cur.execute("""DO $$ DECLARE r record; BEGIN
            FOR r IN SELECT c.relname, a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
                     JOIN pg_type t ON t.oid = a.atttypid WHERE n.nspname = 'urbis' AND c.relkind = 'r' AND t.typname = 'geometry' AND a.attnum > 0 AND NOT a.attisdropped
            LOOP EXECUTE format('ALTER TABLE urbis.%I ALTER COLUMN %I TYPE geometry USING ST_SetSRID(%I, 0)', r.relname, r.attname, r.attname); END LOOP; END $$;""")


def make_db(dbname: str = "geotest", corrupt_srid: bool = False) -> None:
    admin = psycopg2.connect(dsn("postgres"))
    admin.autocommit = True
    with admin.cursor() as c:
        c.execute(f"DROP DATABASE IF EXISTS {dbname}")
        c.execute(f"CREATE DATABASE {dbname}")
    admin.close()
    conn = psycopg2.connect(dsn(dbname))
    conn.autocommit = True
    with conn.cursor() as cur:
        build(cur, corrupt_srid)
        cur.execute((APP_SQL / "lookup_views.sql").read_text())
        cur.execute((APP_SQL / "geo_functions.sql").read_text())
    conn.close()


if __name__ == "__main__":
    make_db()
    print("scratch database ready")
