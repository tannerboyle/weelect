"""Hedcut-style stipple portraits (black dots on transparent/cream) from the photos in static/photos/.

Method: grayscale -> CLAHE (local contrast) -> tone density (dark = more dots) + edge boost (so eyes, brows, jawline
read clearly) -> soft elliptical vignette (fades background toward the edges) -> sample points by density -> a few
Lloyd/weighted-Voronoi relaxation steps (evens out spacing like an engraver's dots) -> variable dot radius by darkness.
Output: compact SVG (dots grouped by radius into stroked zero-length paths with round caps).

Usage:
  python scripts/stipple.py L000562 W000817 ...          # specific bioguide IDs -> static/stipple/<ID>.svg
  python scripts/stipple.py --out DIR --all              # everything in static/photos (not run yet)
"""
import argparse, os, sys, time
import cv2, numpy as np
from scipy.spatial import cKDTree

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCALE = 2          # work at 2x for finer dots
N_DOTS = 5200
LLOYD_ITERS = 6
RADII = [0.55, 0.8, 1.05, 1.35, 1.7]   # in output px (1x), 5 size classes


_CASCADE = None


def _face(gray):
    """Largest frontal face box (x, y, w, h) via OpenCV's bundled Haar cascade, or None."""
    global _CASCADE
    if _CASCADE is None:
        _CASCADE = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = _CASCADE.detectMultiScale(gray, 1.1, 5, minSize=(gray.shape[1] // 6, gray.shape[0] // 6))
    if len(faces) == 0:
        return None
    return max(faces, key=lambda f: f[2] * f[3])


def density_map(gray):
    h, w = gray.shape
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(4, 4))
    g = clahe.apply(gray)
    g = cv2.GaussianBlur(g, (0, 0), 1.2)
    tone = 1.0 - g.astype(np.float32) / 255.0
    # stretch tone so highlights stay clean and shadows get dense
    lo, hi = np.percentile(tone, [8, 97])
    tone = np.clip((tone - lo) / max(hi - lo, 1e-3), 0, 1) ** 1.25
    # edge boost
    gx = cv2.Sobel(g.astype(np.float32), cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g.astype(np.float32), cv2.CV_32F, 0, 1, ksize=3)
    edge = cv2.GaussianBlur(np.sqrt(gx * gx + gy * gy), (0, 0), 1.0)
    edge = np.clip(edge / (np.percentile(edge, 99) + 1e-3), 0, 1)
    dens = np.clip(0.8 * tone + 0.45 * edge, 0, 1)
    # elliptical vignette around the head + shoulders (face-detected when possible), fading out the background
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy, rx, ry = w / 2, h * 0.46, w * 0.50, h * 0.56
    face = _face(gray)
    if face is not None:
        fx, fy, fw, fh = face
        cx, cy, rx, ry = fx + fw / 2, fy + fh * 0.75, fw * 1.25, fh * 1.7
    d = np.sqrt(((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2)
    vign = np.clip((1.1 - d) / 0.4, 0, 1)
    return dens * vign, tone


def stipple(path, n_dots=N_DOTS, seed=1):
    img = cv2.imread(path)
    if img is None:
        raise ValueError("unreadable image " + path)
    img = cv2.resize(img, None, fx=SCALE, fy=SCALE, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    dens, tone = density_map(gray)
    rng = np.random.default_rng(seed)
    p = dens.ravel() ** 1.6
    p = p / p.sum()
    idx = rng.choice(p.size, size=n_dots, p=p)
    pts = np.stack([idx % w, idx // w], 1).astype(np.float32) + rng.random((n_dots, 2), dtype=np.float32)
    # weighted Lloyd relaxation on a subsampled pixel grid
    ys, xs = np.mgrid[0:h:2, 0:w:2]
    grid = np.stack([xs.ravel(), ys.ravel()], 1).astype(np.float32)
    wgt = (dens[0:h:2, 0:w:2].ravel() ** 1.6) + 1e-9
    for _ in range(LLOYD_ITERS):
        _, owner = cKDTree(pts).query(grid)
        sw = np.bincount(owner, wgt, n_dots)
        cx = np.bincount(owner, wgt * grid[:, 0], n_dots)
        cy = np.bincount(owner, wgt * grid[:, 1], n_dots)
        ok = sw > 1e-6
        pts[ok, 0], pts[ok, 1] = cx[ok] / sw[ok], cy[ok] / sw[ok]
    pts[:, 0] = np.clip(pts[:, 0], 0, w - 1)
    pts[:, 1] = np.clip(pts[:, 1], 0, h - 1)
    # radius class by local darkness
    t = tone[pts[:, 1].astype(int), pts[:, 0].astype(int)]
    cls = np.clip((t * len(RADII)).astype(int), 0, len(RADII) - 1)
    # drop dots in very light spots
    keep = t > 0.10
    return pts[keep] / SCALE, cls[keep], (w / SCALE, h / SCALE)


def to_svg(pts, cls, size, ink="#16181d"):
    w, h = size
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.0f} {h:.0f}" role="img" aria-label="Stipple portrait">'
             f'<g stroke="{ink}" stroke-linecap="round" fill="none">']
    for c, r in enumerate(RADII):
        sel = pts[cls == c]
        if not len(sel):
            continue
        d = "".join(f"M{x:.1f} {y:.1f}h0" for x, y in sel)
        parts.append(f'<path stroke-width="{2 * r:.2f}" d="{d}"/>')
    parts.append("</g></svg>")
    return "".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out", default=os.path.join(ROOT, "static", "stipple"))
    ap.add_argument("--dots", type=int, default=N_DOTS)
    a = ap.parse_args()
    src = os.path.join(ROOT, "static", "photos")
    ids = sorted(f[:-4] for f in os.listdir(src) if f.endswith(".jpg")) if a.all else a.ids
    os.makedirs(a.out, exist_ok=True)
    for i in ids:
        t0 = time.time()
        pts, cls, size = stipple(os.path.join(src, i + ".jpg"), a.dots)
        svg = to_svg(pts, cls, size)
        open(os.path.join(a.out, i + ".svg"), "w").write(svg)
        print(f"{i}: {len(pts)} dots, {len(svg)/1024:.1f} KB, {time.time()-t0:.2f}s")


if __name__ == "__main__":
    main()
