#!/usr/bin/env python3
"""
Общая палитра по кадрам 2–10 первого экрана.

На полосе студии стоят девять кадров (со 2-го по 10-й), поэтому палитра и доля
цветного считаются по ним вместе, а не по одному скриншоту. Пиксели всех девяти
кадров складываются в одну выборку, дальше всё как в color.py: OKLab, порог
цветности 0,045, 3 нейтральных + 4 цветных оттенка.

Что получится в папке съёмки:
  colors9.csv        — доля цветного и палитра по кадрам 2–10, десктоп и телефон
  spread9.csv        — разброс палитр между студиями по тем же кадрам (десктоп = 100)
  palettes9/         — полоски палитр: .svg (Figma, Illustrator, InDesign 2020+),
                       .pdf (InDesign, если установлен reportlab) и .png для просмотра

Запуск (из папки, где лежат capture.py и color.py):
  python color9.py capture-2026-11-11
  python color9.py capture-2026-11-11 --frames 2-10 --desktop-size 141.7x85
  python color9.py capture-2026-11-11 --only 01,02

Размер по умолчанию — как у карт зон: десктоп 141,7 × 85 мм (цвета идут слева
направо), телефон 38,6 × 85 мм (сверху вниз). Без обводки, соседние цвета
заходят друг на друга на 0,2 %, чтобы при печати не было швов.
"""

import argparse
import csv
import itertools
import pathlib
import re
import statistics
import sys

import numpy as np
from PIL import Image, ImageDraw

from color import (CHROMA_THRESHOLD, COLOR_K, NEUTRAL_K, WORK_WIDTH, kmeans,
                   palette_distance, srgb_to_oklab)

FRAME_RE = re.compile(r"^(\d{2})-(.+?)-(1-desktop|2-mobile)-f(\d{2})\.png$", re.I)
FORMATS = {"1-desktop": "десктоп", "2-mobile": "телефон"}
KMEANS_SAMPLE = 200_000          # столько пикселей идёт в k-means; доля цветного — по всем


def load_pixels(path):
    im = Image.open(path).convert("RGB")
    if im.width > WORK_WIDTH:
        im = im.resize((WORK_WIDTH, round(im.height * WORK_WIDTH / im.width)), Image.BILINEAR)
    return np.asarray(im, dtype=np.float64).reshape(-1, 3)


def analyse_frames(paths):
    """Одна палитра на все переданные кадры."""
    rgb = np.concatenate([load_pixels(p) for p in paths])
    lab = srgb_to_oklab(rgb)
    colored = np.hypot(lab[:, 1], lab[:, 2]) > CHROMA_THRESHOLD
    total = len(rgb)

    rng = np.random.default_rng(0)
    palette = []   # (hex, доля %, oklab, цветной?)
    for mask, k, is_col in ((~colored, NEUTRAL_K, False), (colored, COLOR_K, True)):
        idx = np.flatnonzero(mask)
        if idx.size == 0:
            continue
        share_of_total = idx.size / total
        if idx.size > KMEANS_SAMPLE:
            idx = rng.choice(idx, KMEANS_SAMPLE, replace=False)
        sub_lab, sub_rgb = lab[idx], rgb[idx]
        labels, kk = kmeans(sub_lab, k)
        for i in range(kk):
            sel = labels == i
            if not sel.any():
                continue
            mean_rgb = sub_rgb[sel].mean(0).round().astype(int)
            share = 100 * share_of_total * sel.sum() / len(idx)
            palette.append(("#%02x%02x%02x" % tuple(mean_rgb), share, sub_lab[sel].mean(0), is_col))
    palette.sort(key=lambda p: (not p[3], -p[1]))
    main = next((p for p in palette if p[3]), None)
    return {"colored_share": round(100 * float(colored.mean()), 1),
            "palette": palette,
            "main_color": main[0] if main else "",
            "main_color_share": round(main[1], 1) if main else ""}


def strip_order(palette):
    """Порядок в полоске, как в color.py: сначала нейтральные, потом цветные, крупные первыми."""
    return sorted(palette, key=lambda p: (p[3], -p[1]))


def segments(palette, length):
    """Отрезки полоски: (цвет, начало, длина) с нахлёстом 0,2 % — без швов и без обводки."""
    total = sum(p[1] for p in palette) or 1
    pos, out = 0.0, []
    for hx, share, _, _ in strip_order(palette):
        seg = length * share / total
        out.append((hx, pos, seg + length * 0.002))
        pos += seg
    return out


def write_svg(palette, path, w_mm, h_mm, vertical):
    parts = []
    W, H = round(w_mm * 10), round(h_mm * 10)       # единица viewBox = 0,1 мм
    for hx, a, l in segments(palette, H if vertical else W):
        if vertical:
            parts.append(f'<rect x="0" y="{a:.2f}" width="{W}" height="{l:.2f}" fill="{hx}"/>')
        else:
            parts.append(f'<rect x="{a:.2f}" y="0" width="{l:.2f}" height="{H}" fill="{hx}"/>')
    path.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w_mm}mm" height="{h_mm}mm" '
        f'viewBox="0 0 {W} {H}">' + "".join(parts) + "</svg>", encoding="utf-8")


def write_pdf(palette, path, w_mm, h_mm, vertical):
    try:
        from reportlab.lib.units import mm
        from reportlab.pdfgen import canvas
    except ImportError:
        return False
    c = canvas.Canvas(str(path), pagesize=(w_mm * mm, h_mm * mm))
    for hx, a, l in segments(palette, h_mm if vertical else w_mm):
        r, g, b = (int(hx[i:i + 2], 16) / 255 for i in (1, 3, 5))
        c.setFillColorRGB(r, g, b)
        if vertical:   # сверху вниз
            c.rect(0, (h_mm - a - l) * mm, w_mm * mm, l * mm, fill=1, stroke=0)
        else:
            c.rect(a * mm, 0, l * mm, h_mm * mm, fill=1, stroke=0)
    c.showPage()
    c.save()
    return True


def write_png(palette, path, w_mm, h_mm, vertical):
    k = 10                                          # 10 точек на мм
    W, H = round(w_mm * k), round(h_mm * k)
    im = Image.new("RGB", (W, H), (252, 252, 251))
    d = ImageDraw.Draw(im)
    for hx, a, l in segments(palette, H if vertical else W):
        if vertical:
            d.rectangle([0, int(a), W, int(a + l) + 1], fill=hx)
        else:
            d.rectangle([int(a), 0, int(a + l) + 1, H], fill=hx)
    im.save(path)


def parse_range(text):
    a, b = (int(v) for v in text.split("-"))
    return set(range(a, b + 1))


def main():
    ap = argparse.ArgumentParser(description="Общая палитра по кадрам 2–10")
    ap.add_argument("root", help="папка съёмки (в ней frames/)")
    ap.add_argument("--frames", default="2-10", help="какие кадры брать, по умолчанию 2-10")
    ap.add_argument("--only", default="", help="номера студий через запятую, например 01,02")
    ap.add_argument("--desktop-size", default="141.7x85",
                    help="размер палитры десктопа в мм, по умолчанию как карта зон: 141.7x85")
    ap.add_argument("--phone-size", default="38.6x85",
                    help="размер палитры телефона в мм, по умолчанию как карта зон: 38.6x85")
    args = ap.parse_args()

    root = pathlib.Path(args.root)
    frames_dir = root / "frames"
    if not frames_dir.exists():
        print(f"Нет папки {frames_dir}")
        return 1
    want = parse_range(args.frames)
    sizes = {"1-desktop": tuple(float(v) for v in args.desktop_size.lower().split("x")),
             "2-mobile": tuple(float(v) for v in args.phone_size.lower().split("x"))}
    only = {s.strip().zfill(2) for s in args.only.split(",") if s.strip()}
    out = root / "palettes9"
    out.mkdir(exist_ok=True)

    groups = {}
    for path in sorted(frames_dir.iterdir()):
        mt = FRAME_RE.match(path.name)
        if not mt or int(mt.group(4)) not in want:
            continue
        if only and mt.group(1) not in only:
            continue
        groups.setdefault((mt.group(1), mt.group(2), mt.group(3)), []).append(path)

    rows, by_format, pdf_ok = [], {f: [] for f in FORMATS}, None
    for (num, slug, fmt), paths in sorted(groups.items()):
        res = analyse_frames(paths)
        stem = f"{num}-{slug}-{fmt}-palette9"
        w_mm, h_mm = sizes[fmt]
        vertical = h_mm > w_mm                     # узкая высокая палитра идёт сверху вниз
        write_svg(res["palette"], out / f"{stem}.svg", w_mm, h_mm, vertical)
        write_png(res["palette"], out / f"{stem}.png", w_mm, h_mm, vertical)
        ok = write_pdf(res["palette"], out / f"{stem}.pdf", w_mm, h_mm, vertical)
        pdf_ok = ok if pdf_ok is None else (pdf_ok and ok)
        by_format[fmt].append((num, slug, res["palette"]))
        used = ",".join(sorted({FRAME_RE.match(p.name).group(4) for p in paths}))
        rows.append({
            "№": num, "студия": slug, "формат": FORMATS[fmt],
            "кадры": used, "кадров": len(paths),
            "доля цветного, %": res["colored_share"],
            "главный цвет": res["main_color"], "его доля, %": res["main_color_share"],
            "палитра": "  ".join(f"{h} {s:.1f}%" for h, s, _, _ in res["palette"]),
        })
        flag = "" if len(paths) == len(want) else f"  (кадров {len(paths)} из {len(want)})"
        print(f"{num} {slug:14} {FORMATS[fmt]:8} цветного {res['colored_share']:5.1f} %  "
              f"главный {res['main_color']}{flag}")

    if not rows:
        print("Кадры не найдены: проверьте папку frames/ и параметр --frames")
        return 1

    with open(root / "colors9.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), delimiter=";")
        w.writeheader()
        w.writerows(rows)

    spread = {}
    for fmt, items in by_format.items():
        pairs = list(itertools.combinations(items, 2))
        if pairs:
            spread[fmt] = statistics.mean(palette_distance(a[2], b[2]) for a, b in pairs)
    base = spread.get("1-desktop")
    with open(root / "spread9.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["формат", "студий", "разброс (OKLab)", "индекс, десктоп = 100"])
        for fmt, name in FORMATS.items():
            if fmt in spread:
                idx = round(100 * spread[fmt] / base) if base else ""
                w.writerow([name, len(by_format[fmt]), round(spread[fmt], 4), idx])
                print(f"разброс · {name:8} {spread[fmt]:.4f}  индекс {idx}")

    print(f"\nГотово: colors9.csv, spread9.csv, palettes9/ "
          f"(десктоп {args.desktop_size} мм, телефон {args.phone_size} мм)")
    if pdf_ok is False:
        print("PDF не сделаны: нет reportlab. Установите: pip install reportlab — или берите SVG.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
