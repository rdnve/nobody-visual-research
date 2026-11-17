#!/usr/bin/env python3
"""
Замер «Цвет» по скриншотам, снятым capture.py.

Для каждого скриншота:
  — доля цветного: пиксели с цветностью (chroma) в OKLab выше 0,045;
    нейтральные (белое, серое, чёрное) отделяются, иначе фон перебивает всё;
  — палитра: до 3 нейтральных и до 4 цветных оттенков с их долями;
  — главный цвет: самый крупный цветной оттенок;
  — полоска палитры в palettes/.

По каждому формату (десктоп, телефон, карточка ссылки) — разброс палитр
между студиями: среднее расстояние между палитрами в OKLab. Десктоп = 100,
остальные форматы в процентах от него.

По десяти кадрам первого экрана (frames/…-fNN.png) — медиана и размах доли
цветного: если экран движется, цвет меняется от кадра к кадру.

Запуск:
  python color.py capture-2026-11-11
"""

import csv
import itertools
import pathlib
import re
import statistics
import sys

import numpy as np
from PIL import Image, ImageDraw

CHROMA_THRESHOLD = 0.045
NEUTRAL_K, COLOR_K = 3, 4
WORK_WIDTH = 480                      # картинка уменьшается для скорости
FORMATS = {"1-desktop": "десктоп", "2-mobile": "телефон", "3-og": "карточка ссылки"}
NAME_RE = re.compile(r"^(\d{2})-(.+?)-(1-desktop|2-mobile|3-og)\.(png|jpe?g|webp)$", re.I)
FRAME_RE = re.compile(r"^(\d{2})-(.+?)-(1-desktop|2-mobile)-f(\d{2})\.png$", re.I)


def srgb_to_oklab(rgb):
    c = rgb / 255.0
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    l = 0.4122214708 * c[:, 0] + 0.5363325363 * c[:, 1] + 0.0514459929 * c[:, 2]
    m = 0.2119034982 * c[:, 0] + 0.6806995451 * c[:, 1] + 0.1073969566 * c[:, 2]
    s = 0.0883024619 * c[:, 0] + 0.2817188376 * c[:, 1] + 0.6299787005 * c[:, 2]
    l, m, s = np.cbrt(l), np.cbrt(m), np.cbrt(s)
    return np.stack([
        0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
        1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
        0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s,
    ], axis=1)


def kmeans(x, k, iters=20, seed=0):
    """Простой k-means (k-means++ старт). Возвращает метки и число кластеров."""
    if len(x) == 0:
        return np.array([], dtype=int), 0
    k = min(k, len(np.unique(x.round(3), axis=0)))
    rng = np.random.default_rng(seed)
    centers = [x[rng.integers(len(x))]]
    for _ in range(1, k):
        d = np.min(((x[:, None, :] - np.array(centers)[None]) ** 2).sum(-1), axis=1)
        centers.append(x[rng.choice(len(x), p=d / d.sum())] if d.sum() > 0 else x[rng.integers(len(x))])
    centers = np.array(centers)
    for _ in range(iters):
        labels = np.argmin(((x[:, None, :] - centers[None]) ** 2).sum(-1), axis=1)
        new = np.array([x[labels == i].mean(0) if np.any(labels == i) else centers[i] for i in range(k)])
        if np.allclose(new, centers):
            break
        centers = new
    return labels, k


def analyse(path):
    im = Image.open(path).convert("RGB")
    if im.width > WORK_WIDTH:
        im = im.resize((WORK_WIDTH, round(im.height * WORK_WIDTH / im.width)), Image.BILINEAR)
    rgb = np.asarray(im, dtype=np.float64).reshape(-1, 3)
    lab = srgb_to_oklab(rgb)
    chroma = np.hypot(lab[:, 1], lab[:, 2])
    colored = chroma > CHROMA_THRESHOLD
    total = len(rgb)

    palette = []   # (hex, доля %, oklab, цветной?)
    for mask, k, is_col in ((~colored, NEUTRAL_K, False), (colored, COLOR_K, True)):
        if not mask.any():
            continue
        sub_lab, sub_rgb = lab[mask], rgb[mask]
        labels, kk = kmeans(sub_lab, k)
        for i in range(kk):
            sel = labels == i
            if not sel.any():
                continue
            mean_rgb = sub_rgb[sel].mean(0).round().astype(int)
            palette.append(("#%02x%02x%02x" % tuple(mean_rgb), 100 * sel.sum() / total,
                            sub_lab[sel].mean(0), is_col))
    palette.sort(key=lambda p: (not p[3], -p[1]))
    main_color = next((p for p in palette if p[3]), None)
    return {"colored_share": round(100 * colored.mean(), 1),
            "palette": palette,
            "main_color": main_color[0] if main_color else "",
            "main_color_share": round(main_color[1], 1) if main_color else ""}


def colored_share(path):
    """Только доля цветного — быстро, для кадров."""
    im = Image.open(path).convert("RGB")
    if im.width > WORK_WIDTH:
        im = im.resize((WORK_WIDTH, round(im.height * WORK_WIDTH / im.width)), Image.BILINEAR)
    lab = srgb_to_oklab(np.asarray(im, dtype=np.float64).reshape(-1, 3))
    return round(100 * float((np.hypot(lab[:, 1], lab[:, 2]) > CHROMA_THRESHOLD).mean()), 1)


def palette_strip(palette, path):
    w, h = 1600, 120
    im = Image.new("RGB", (w, h), (252, 252, 251))
    d = ImageDraw.Draw(im)
    x = 0.0
    for hx, share, _, _ in sorted(palette, key=lambda p: (p[3], -p[1])):
        width = w * share / 100
        d.rectangle([int(x), 0, int(x + width) + 1, h], fill=hx)
        x += width
    d.rectangle([0, 0, w - 1, h - 1], outline=(160, 158, 152), width=2)
    im.save(path)


def palette_distance(p, q):
    """Симметричное расстояние между палитрами: для каждого оттенка — ближайший
    оттенок другой палитры, усреднение с весами по долям."""
    def one_way(a, b):
        wa = np.array([s for _, s, _, _ in a])
        la = np.array([l for _, _, l, _ in a])
        lb = np.array([l for _, _, l, _ in b])
        dmin = np.sqrt(((la[:, None] - lb[None]) ** 2).sum(-1)).min(1)
        return float((dmin * wa).sum() / wa.sum())
    return (one_way(p, q) + one_way(q, p)) / 2


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    root = pathlib.Path(sys.argv[1])
    shots, frames = root / "shots", root / "frames"
    (root / "palettes").mkdir(exist_ok=True)

    rows, by_format = [], {f: [] for f in FORMATS}
    for path in sorted(shots.iterdir()):
        mt = NAME_RE.match(path.name)
        if not mt:
            continue
        num, slug, fmt = mt.group(1), mt.group(2), mt.group(3)
        try:
            res = analyse(path)
        except Exception as e:  # noqa: BLE001
            print(f"{path.name}: не прочитался ({type(e).__name__})")
            continue
        palette_strip(res["palette"], root / "palettes" / f"{path.stem}-palette.png")
        by_format[fmt].append((num, slug, res["palette"]))
        rows.append({
            "№": num, "студия": slug, "формат": FORMATS[fmt], "файл": path.name,
            "доля цветного, %": res["colored_share"],
            "главный цвет": res["main_color"], "его доля, %": res["main_color_share"],
            "палитра": "  ".join(f"{h} {s:.1f}%" for h, s, _, _ in res["palette"]),
        })
        print(f"{path.name:40} цветного {res['colored_share']:5.1f} %  главный {res['main_color']}")

    with open(root / "colors.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["№"], delimiter=";")
        w.writeheader()
        w.writerows(rows)

    # разброс палитр по формату
    spread = {}
    for fmt, items in by_format.items():
        pairs = list(itertools.combinations(items, 2))
        if pairs:
            spread[fmt] = statistics.mean(palette_distance(a[2], b[2]) for a, b in pairs)
    base = spread.get("1-desktop")
    with open(root / "spread.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["формат", "студий", "разброс (OKLab)", "индекс, десктоп = 100"])
        for fmt, name in FORMATS.items():
            if fmt in spread:
                idx = round(100 * spread[fmt] / base) if base else ""
                w.writerow([name, len(by_format[fmt]), round(spread[fmt], 4), idx])
                print(f"разброс · {name:16} {spread[fmt]:.4f}  индекс {idx}")

    # кадры первого экрана: медиана и размах доли цветного
    vids = {}
    if frames.exists():
        for path in sorted(frames.iterdir()):
            mt = FRAME_RE.match(path.name)
            if mt:
                key = (mt.group(1), mt.group(2), FORMATS[mt.group(3)])
                vids.setdefault(key, []).append(colored_share(path))
    if vids:
        with open(root / "colors_frames.csv", "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["№", "студия", "формат", "кадров", "медиана доли цветного, %",
                        "минимум", "максимум", "размах"])
            for (num, slug, fmt), vals in sorted(vids.items()):
                w.writerow([num, slug, fmt, len(vals), round(statistics.median(vals), 1),
                            min(vals), max(vals), round(max(vals) - min(vals), 1)])
    print(f"\nГотово: colors.csv, spread.csv{', colors_frames.csv' if vids else ''}, palettes/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
