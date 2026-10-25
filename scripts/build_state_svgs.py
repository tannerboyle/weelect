"""Build SVG outlines from U.S. Census Bureau cartographic boundary file cb_2023_us_state_500k (public domain).

Usage: python scripts/build_state_svgs.py path/to/cb_2023_us_state_500k   (no .shp extension)
Writes static/states/<USPS>.svg (one per state/DC/territory, fill=currentColor) and static/us-outline.svg
(continental-US silhouette used as page background).  Needs: pip install pyshp shapely
"""
import math, os, sys
import shapefile
from shapely.geometry import shape, MultiPolygon, Polygon
from shapely.ops import unary_union

SRC = sys.argv[1]
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
os.makedirs(os.path.join(OUT, "states"), exist_ok=True)


def albers(lat1, lat2, lat0, lon0):
    r = math.radians
    n = (math.sin(r(lat1)) + math.sin(r(lat2))) / 2
    C = math.cos(r(lat1)) ** 2 + 2 * n * math.sin(r(lat1))
    rho0 = math.sqrt(C - 2 * n * math.sin(r(lat0))) / n
    def f(lon, lat):
        rho = math.sqrt(C - 2 * n * math.sin(r(lat))) / n
        th = n * r(lon - lon0)
        return rho * math.sin(th), rho0 - rho * math.cos(th)   # y up; flipped when writing the SVG
    return f

CONUS = albers(29.5, 45.5, 37.5, -96)
PROJ = {"AK": albers(55, 65, 50, -154), "HI": albers(8, 18, 13, -157)}


def local_proj(geom):
    """Simple equirectangular for territories / fallback."""
    x0, y0, x1, y1 = geom.bounds
    lat0 = (y0 + y1) / 2
    k = math.cos(math.radians(lat0))
    return lambda lon, lat: ((lon - (x0 + x1) / 2) * k, lat - lat0)


def project(geom, f):
    def pr(poly):
        ext = [f(x, y) for x, y in poly.exterior.coords]
        holes = [[f(x, y) for x, y in h.coords] for h in poly.interiors]
        return Polygon(ext, holes)
    polys = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
    return MultiPolygon([pr(p) for p in polys])


def to_path(geom, W=300.0, pad=6, drop_area=0.0):
    x0, y0, x1, y1 = geom.bounds
    s = (W - 2 * pad) / max(x1 - x0, y1 - y0)
    w, h = (x1 - x0) * s + 2 * pad, (y1 - y0) * s + 2 * pad
    polys = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
    d = []
    for p in polys:
        if p.area * s * s < drop_area:
            continue
        for ring in [p.exterior] + list(p.interiors):
            pts = [((x - x0) * s + pad, (y1 - y) * s + pad) for x, y in ring.coords]
            d.append("M" + "L".join(f"{x:.1f},{y:.1f}" for x, y in pts[:-1]) + "Z")
    return "".join(d), w, h


r = shapefile.Reader(SRC)
rows = {}
for sr in r.shapeRecords():
    rec = sr.record.as_dict()
    rows[rec["STUSPS"]] = (shape(sr.shape.__geo_interface__), rec["NAME"])

# per-state icons
for st, (g, name) in rows.items():
    f = PROJ.get(st) or (CONUS if st not in ("PR", "GU", "VI", "AS", "MP") else local_proj(g))
    pg = project(g, f)
    pg = pg.simplify(0.0015 * max(pg.bounds[2] - pg.bounds[0], pg.bounds[3] - pg.bounds[1]), preserve_topology=True)
    d, w, h = to_path(pg, W=240, drop_area=0.25)
    with open(os.path.join(OUT, "states", f"{st}.svg"), "w") as fh:
        fh.write(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.0f} {h:.0f}" role="img" aria-label="{name} outline">'
                 f'<path fill="currentColor" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round" fill-rule="evenodd" d="{d}"/></svg>')

# continental silhouette (union of the lower 48 + DC), outline only
lower48 = [g for st, (g, n) in rows.items() if st not in ("AK", "HI", "PR", "GU", "VI", "AS", "MP")]
u = project(unary_union(lower48), CONUS)
u = u.simplify(0.0006 * (u.bounds[2] - u.bounds[0]), preserve_topology=True)
d, w, h = to_path(u, W=1200, pad=4, drop_area=4.0)
with open(os.path.join(OUT, "us-outline.svg"), "w") as fh:
    fh.write(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.0f} {h:.0f}" fill="none" stroke="#1d3a78" stroke-width="2" stroke-linejoin="round">'
             f'<path d="{d}"/></svg>')
print("done", len(rows), "states; outline", round(w), "x", round(h))
