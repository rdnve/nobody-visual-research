#!/usr/bin/env python3
"""
Съёмка сайтов студий по протоколу визуального исследования
«Никому не нужен ваш вебсайт».

Для каждой студии из studios.csv:
  1. десктоп 1440 × 900 — скриншот первого экрана (без прокрутки);
  2. телефон 390 × 844 (iPhone 13) — скриншот первого экрана;
  3. og:image — картинка, которую мессенджеры показывают в карточке ссылки;
  4. если на первом экране видео — 10 кадров по одному в секунду и постер;
  5. технические проверки: H1/H2, alt, og-теги, robots.txt (в т. ч. ИИ-роботы),
     чем сделана главная картинка, блокируется ли правый клик, видео — файл
     или поток, Pinterest, контакт на первом экране, ссылки на соцсети.

Плашки куки не закрываются: посетитель их видит, значит это данные.

Запуск:
  python capture.py                  # все студии
  python capture.py --only 01,08     # только выбранные номера
  python capture.py --headed         # с видимым окном браузера (если сайт
                                     # показывает пустую страницу или капчу)
"""

import argparse
import csv
import datetime as dt
import json
import pathlib
import html as htmllib
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.robotparser

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

HERE = pathlib.Path(__file__).resolve().parent

# ---------------------------------------------------------------- протокол
DESKTOP = dict(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
MOBILE_DEVICE = "iPhone 13"          # 390 × 844
COMMON = dict(locale="ru-RU", timezone_id="Europe/Moscow", color_scheme="light")
LOAD_TIMEOUT_MS = 60_000             # сколько ждём загрузку страницы
SETTLE_MS = 3_000                    # 3 секунды после загрузки
FRAMES = 10                          # кадров первого экрана
FRAME_STEP_S = 1.0                   # ровно по одному в секунду
CHANGE_LEVEL = 25                    # насколько должен измениться пиксель (0–255)
CHANGE_SHARE = 0.005                 # кадр сменился, если изменилось > 0,5 % экрана

ROBOT_AGENTS = {
    "Googlebot": "Googlebot",
    "Yandex": "YandexBot",
    "GPTBot (ChatGPT)": "GPTBot",
    "ClaudeBot": "ClaudeBot",
    "Google-Extended (Gemini)": "Google-Extended",
    "PerplexityBot": "PerplexityBot",
    "CCBot (Common Crawl)": "CCBot",
}

FALLBACK_STUDIOS = [
    ("01", "artlebedev", "https://www.artlebedev.ru"),
    ("02", "shuka", "https://shuka.design"),
    ("03", "suprematika", "https://suprematika.ru"),
    ("04", "ermolaev", "https://ermolaevbureau.com"),
    ("05", "repina", "https://repinabranding.com"),
    ("06", "getbrand", "https://getbrand.ru"),
    ("07", "plenum", "https://plenum.ru"),
    ("08", "depot", "https://depotwpf.ru"),
    ("09", "ony", "https://ony.ru"),
    ("10", "fabula", "https://fabulabranding.com"),
    ("11", "backbone", "https://backbonebranding.com"),
    ("12", "synthesis", "https://synthesis.uz"),
    ("13", "desique", "https://desique.kz"),
    ("14", "fingers", "https://fingers.kz"),
    ("15", "djam", "https://djam.kz"),
    ("16", "brainteam", "https://brainteam.kz"),
    ("17", "boost", "https://boost.com.kz"),
    ("18", "mano", "https://manobranding.uz"),
    ("19", "tessella", "https://tessella.uz"),
    ("20", "threesixty", "https://threesixty.uz"),
]

# ------------------------------------------------- что смотрим на странице
# Выполняется в браузере. Возвращает словарь с замерами первого экрана.
PAGE_JS = r"""
() => {
  const vw = innerWidth, vh = innerHeight, screenArea = vw * vh;
  const q = s => document.querySelector(s);
  const meta = sel => { const m = q(sel); return m ? (m.getAttribute('content') || '').trim() : ''; };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const shown = el => {
    const cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden' && parseFloat(cs.opacity || '1') > 0.05;
  };
  const visArea = r => Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0)) *
                       Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
  const inFirstScreen = el => { const r = el.getBoundingClientRect(); return visArea(r) > 0 && shown(el); };

  // заголовки
  const h1 = [...document.querySelectorAll('h1')].map(e => clean(e.innerText)).filter(Boolean);
  const h2 = [...document.querySelectorAll('h2')].map(e => clean(e.innerText)).filter(Boolean);

  // картинки и подписи alt
  const imgs = [...document.images];
  const imgsWithAlt = imgs.filter(i => clean(i.getAttribute('alt')).length > 0).length;

  // главная картинка первого экрана: самый большой визуальный элемент
  const cands = [];
  for (const el of document.querySelectorAll('img, video, canvas, iframe, svg')) {
    const a = visArea(el.getBoundingClientRect());
    if (a > 0 && shown(el)) cands.push({el, kind: el.tagName.toLowerCase(), area: a});
  }
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width < 80 || r.height < 80) continue;
    const a = visArea(r);
    if (a <= 0) continue;
    const bg = getComputedStyle(el).backgroundImage;
    if (bg && bg !== 'none' && bg.includes('url(') && shown(el)) cands.push({el, kind: 'background', area: a});
  }
  cands.sort((x, y) => y.area - x.area);
  const main = cands[0] || null;
  let mainVisual = 'нет', mainShare = 0, contextMenuBlocked = null;
  if (main) {
    mainVisual = {img: 'изображение', video: 'видео', canvas: 'canvas/WebGL', iframe: 'встроенный плеер',
                  svg: 'svg', background: 'фон CSS'}[main.kind] || main.kind;
    mainShare = Math.round(1000 * main.area / screenArea) / 10;
    const r = main.el.getBoundingClientRect();
    const cx = Math.min(Math.max((Math.max(r.left, 0) + Math.min(r.right, vw)) / 2, 1), vw - 1);
    const cy = Math.min(Math.max((Math.max(r.top, 0) + Math.min(r.bottom, vh)) / 2, 1), vh - 1);
    const target = document.elementFromPoint(cx, cy) || main.el;
    const ev = new MouseEvent('contextmenu', {bubbles: true, cancelable: true, clientX: cx, clientY: cy, button: 2});
    contextMenuBlocked = !target.dispatchEvent(ev);
  }

  // видео на первом экране (больше 10 % экрана)
  const videos = [];
  for (const v of document.querySelectorAll('video')) {
    const a = visArea(v.getBoundingClientRect());
    if (a < 0.1 * screenArea || !shown(v)) continue;
    const src = v.currentSrc || v.getAttribute('src') ||
                (v.querySelector('source') ? v.querySelector('source').getAttribute('src') : '') || '';
    let type = 'нет адреса';
    if (src.startsWith('blob:')) type = 'поток (blob)';
    else if (/\.m3u8(\?|$)/i.test(src)) type = 'поток HLS';
    else if (/\.mpd(\?|$)/i.test(src)) type = 'поток DASH';
    else if (/\.(mp4|webm|mov|m4v|ogv)(\?|$)/i.test(src)) type = 'файл';
    else if (src) type = 'другое';
    videos.push({src, type, poster: v.getAttribute('poster') || '', autoplay: v.autoplay, muted: v.muted,
                 loop: v.loop, playing: !v.paused && v.readyState > 2,
                 share: Math.round(1000 * a / screenArea) / 10});
  }
  const players = [];
  for (const f of document.querySelectorAll('iframe')) {
    const a = visArea(f.getBoundingClientRect());
    const src = f.getAttribute('src') || '';
    if (a >= 0.1 * screenArea && /youtube|youtu\.be|vimeo|kinescope|rutube|vk\.com\/video|vkvideo/i.test(src))
      players.push({src, share: Math.round(1000 * a / screenArea) / 10});
  }

  // контакт на первом экране
  const contactRe = /связ|контакт|contact|напиш|обсуд|заявк|запрос|бриф|brief|hello|начать проект|start a project|let.?s talk|get in touch|заказ/i;
  const contactHref = /^(tel:|mailto:|https?:\/\/(t\.me|wa\.me|api\.whatsapp\.com))/i;
  let contact = null;
  for (const el of document.querySelectorAll('a, button, [role=button]')) {
    if (!inFirstScreen(el)) continue;
    const t = clean(el.innerText || el.getAttribute('aria-label') || '');
    const href = el.getAttribute('href') || '';
    if (contactRe.test(t) || contactHref.test(href)) {
      const fs = parseFloat(getComputedStyle(el).fontSize) || 0;
      contact = {text: t.slice(0, 60), href: href.slice(0, 120), fontSize: fs};
      break;
    }
  }

  // самый крупный текст на первом экране (главное заявление)
  let biggest = null;
  const root = document.body || document.documentElement;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const n = walker.currentNode;
    if (!clean(n.textContent)) continue;
    const el = n.parentElement;
    if (!el || !inFirstScreen(el)) continue;
    const cs = getComputedStyle(el);
    const fs = parseFloat(cs.fontSize) || 0;
    if (!biggest || fs > biggest.fontSize)
      biggest = {text: clean(n.textContent).slice(0, 80), fontSize: fs,
                 font: (cs.fontFamily || '').split(',')[0].replace(/["']/g, '').trim()};
  }

  // соцсети, на которые ведёт сайт
  const nets = {instagram: /instagram\.com/i, telegram: /t\.me\//i, vk: /vk\.com|vkontakte/i,
                behance: /behance\.net/i, dribbble: /dribbble\.com/i, pinterest: /pinterest\./i,
                youtube: /youtube\.com|youtu\.be/i, facebook: /facebook\.com/i, linkedin: /linkedin\.com/i,
                tiktok: /tiktok\.com/i, x: /twitter\.com|\/\/x\.com/i};
  const socials = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    const h = a.getAttribute('href');
    for (const [k, re] of Object.entries(nets)) if (re.test(h)) socials.add(k);
  }

  // Pinterest: сколько картинок можно закрепить кнопкой браузера
  const nopin = /nopin/i.test(meta('meta[name="pinterest"]'));
  const pinnable = imgs.filter(i => i.naturalWidth >= 200 && i.naturalHeight >= 200 &&
                                    !i.hasAttribute('data-pin-nopin')).length;

  return {
    title: clean(document.title),
    description: meta('meta[name="description"]'),
    meta_robots: meta('meta[name="robots"]'),
    og_title: meta('meta[property="og:title"]'),
    og_description: meta('meta[property="og:description"]'),
    og_image: meta('meta[property="og:image"]') || meta('meta[name="og:image"]'),
    twitter_image: meta('meta[name="twitter:image"]'),
    h1_count: h1.length, h1: h1.slice(0, 3),
    h2_count: h2.length, h2: h2.slice(0, 5),
    img_count: imgs.length, img_with_alt: imgsWithAlt,
    main_visual: mainVisual, main_visual_share: mainShare,
    context_menu_blocked: contextMenuBlocked,
    videos, players,
    contact,
    biggest_text: biggest,
    socials: [...socials].sort(),
    pinterest_nopin: nopin, pinnable_images: pinnable,
    horizontal_overflow: document.documentElement.scrollWidth > vw + 2,
    empty: !document.body || (clean(document.body.innerText).length === 0 && document.images.length === 0 &&
           !document.querySelector('video, canvas, iframe, svg')),
  };
}
"""


# ------------------------------------------------------------- помощники
def load_studios(only):
    path = HERE / "studios.csv"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            rows = [(r["num"].strip().zfill(2), r["slug"].strip(), r["url"].strip())
                    for r in csv.DictReader(f) if r.get("url")]
    else:
        rows = FALLBACK_STUDIOS
    if only:
        keep = {n.strip().zfill(2) for n in only.split(",")}
        rows = [r for r in rows if r[0] in keep]
    return rows


def open_page(page, url):
    """Загрузка по протоколу: ждём событие load (до 60 с), потом ещё 3 секунды.
    Если load так и не наступил, снимаем то, что успело загрузиться."""
    note = ""
    resp = page.goto(url, wait_until="commit", timeout=LOAD_TIMEOUT_MS)
    status = resp.status if resp else None
    try:
        page.wait_for_load_state("load", timeout=LOAD_TIMEOUT_MS)
    except PWTimeout:
        note = "страница не догрузилась за 60 с, снято как есть"
    page.wait_for_timeout(SETTLE_MS)
    return status, note


def with_www(url):
    u = urllib.parse.urlparse(url)
    if u.netloc.startswith("www."):
        return None
    return u._replace(netloc="www." + u.netloc).geturl()


def navigate(browser, ctx_kwargs, url):
    """Открывает сайт. Если сертификат недействителен — открывает всё равно
    и записывает это (посетитель увидел бы предупреждение браузера).
    Если адрес не находится — пробует вариант с www."""
    notes = []
    tried = [url]
    alt = with_www(url)
    if alt:
        tried.append(alt)
    first_error = None
    for target in tried:
        for ignore_cert in (False, True):
            ctx = browser.new_context(**ctx_kwargs, ignore_https_errors=ignore_cert)
            page = ctx.new_page()
            try:
                status, note = open_page(page, target)
                if note:
                    notes.append(note)
                if ignore_cert:
                    notes.append("сертификат сайта недействителен: браузер показывает предупреждение")
                if target != url:
                    notes.append(f"открылся только как {target}")
                return ctx, page, status, notes
            except Exception as e:  # noqa: BLE001
                ctx.close()
                first_error = first_error or e
                msg = str(e)
                if "ERR_CERT" in msg or "SSL" in msg:
                    continue            # повтор с ignore_https_errors
                break                   # другая ошибка — пробуем следующий адрес
    raise first_error


def save_remote(request, url, base_path, referer):
    """Скачивает файл (og:image, постер). Возвращает имя сохранённого файла."""
    if not url:
        return ""
    try:
        full = urllib.parse.urljoin(referer, url)
        r = request.get(full, timeout=30_000)
        if not r.ok:
            return f"ошибка {r.status}"
        ctype = (r.headers.get("content-type") or "").split(";")[0].lower()
        ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
               "image/gif": ".gif", "image/svg+xml": ".svg", "image/avif": ".avif"}.get(ctype)
        if not ext:
            ext = pathlib.Path(urllib.parse.urlparse(full).path).suffix or ".bin"
        out = base_path.with_suffix(ext)
        out.write_bytes(r.body())
        return out.name
    except Exception as e:  # noqa: BLE001
        return f"ошибка: {type(e).__name__}"


def parse_robots(text):
    """Разбор robots.txt по правилам Google: группа по самому точному
    User-agent, внутри — самое длинное совпадающее правило, при равенстве
    побеждает Allow. Поддерживаются * и $."""
    groups, agents, rules, last_was_agent = [], [], [], False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, val = (x.strip() for x in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if not last_was_agent and agents:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(val.lower())
            last_was_agent = True
        elif key in ("allow", "disallow"):
            last_was_agent = False
            if agents:
                rules.append((key == "allow", val))
        else:
            last_was_agent = False
    if agents:
        groups.append((agents, rules))
    return groups


def robots_allows(groups, agent, path="/"):
    agent = agent.lower()
    best, best_len = None, -1
    for agents, rules in groups:
        for a in agents:
            if a != "*" and agent.startswith(a) and len(a) > best_len:
                best, best_len = rules, len(a)
    if best is None:
        merged = [r for agents, rules in groups if "*" in agents for r in rules]
        best = merged
    verdict, verdict_len = True, -1
    for allow, pattern in best:
        if pattern == "":
            continue
        rx = "^" + re.escape(pattern).replace(r"\*", ".*")
        if rx.endswith(r"\$"):
            rx = rx[:-2] + "$"
        if re.match(rx, path):
            n = len(pattern)
            if n > verdict_len or (n == verdict_len and allow):
                verdict, verdict_len = allow, n
    return verdict


def check_robots(request, site_url, save_path):
    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(site_url))
    result = {"robots_txt": "нет"}
    try:
        r = request.get(origin + "/robots.txt", timeout=20_000)
        ctype = (r.headers.get("content-type") or "text/plain").lower()
        if r.ok and "html" not in ctype:
            text = r.text()
            save_path.write_text(text, encoding="utf-8")
            groups = parse_robots(text)
            result["robots_txt"] = "есть"
            for label, agent in ROBOT_AGENTS.items():
                result[label] = "открыт" if robots_allows(groups, agent) else "закрыт"
        else:
            result["robots_txt"] = f"нет ({r.status})"
    except Exception as e:  # noqa: BLE001
        result["robots_txt"] = f"ошибка: {type(e).__name__}"
    for label in ROBOT_AGENTS:
        result.setdefault(label, "открыт" if result["robots_txt"].startswith("нет") else "?")
    return result


def yesno(v):
    return "" if v is None else ("да" if v else "нет")


# --------------------------------------------------- кадры и движение
def shoot_frames(page, prefix):
    """10 кадров первого экрана ровно по одному в секунду (по часам).
    Возвращает пути к кадрам и реальные моменты съёмки в секундах."""
    paths, times = [], []
    t0 = time.monotonic()
    for i in range(FRAMES):
        wait = t0 + i * FRAME_STEP_S - time.monotonic()
        if wait > 0:
            page.wait_for_timeout(wait * 1000)
        t = time.monotonic() - t0
        path = pathlib.Path(f"{prefix}-f{i + 1:02d}.png")
        page.screenshot(path=str(path))
        paths.append(path)
        times.append(round(t, 2))
    return paths, times


def motion(paths):
    """Что происходит на первом экране за 10 секунд.
    Сравниваем соседние кадры (в уменьшенном виде, по яркости)."""
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return {"тип": "нужны numpy и pillow", "смен кадра": "", "доля экрана в движении, %": ""}
    frames = []
    for pth in paths:
        im = Image.open(pth).convert("L")
        im = im.resize((200, round(im.height * 200 / im.width)))
        frames.append(np.asarray(im, dtype=np.int16))
    changed = [float((np.abs(b - a) > CHANGE_LEVEL).mean()) for a, b in zip(frames, frames[1:])]
    n = sum(c > CHANGE_SHARE for c in changed)
    stack = np.stack(frames)
    moving_area = float(((stack.max(0) - stack.min(0)) > CHANGE_LEVEL).mean())
    if n == 0:
        kind = "неподвижен"
    elif n >= len(changed) - 2:
        kind = "непрерывное движение (видео, анимация)"
    else:
        kind = "смена кадров (слайдшоу)"
    return {"тип": kind, "смен кадра": n,
            "доля экрана в движении, %": round(100 * moving_area, 1)}


def strip(paths, times, out_path, thumb_w, cols):
    """Раскадровка: кадры сеткой с подписью времени."""
    from PIL import Image, ImageDraw
    ims = [Image.open(p).convert("RGB") for p in paths]
    th = round(ims[0].height * thumb_w / ims[0].width)
    rows = (len(ims) + cols - 1) // cols
    pad, lab = 12, 28
    sheet = Image.new("RGB", (cols * (thumb_w + pad) + pad, rows * (th + lab + pad) + pad), (22, 22, 21))
    d = ImageDraw.Draw(sheet)
    for i, (im, t) in enumerate(zip(ims, times)):
        x = pad + (i % cols) * (thumb_w + pad)
        y = pad + (i // cols) * (th + lab + pad)
        d.text((x, y + 6), f"{i + 1:02d} · {t:.1f} c", fill=(200, 198, 190))
        sheet.paste(im.resize((thumb_w, th)), (x, y + lab))
    sheet.save(out_path)


# ------------------------------------------------------------------ съёмка
def shoot_studio(p, browser, num, slug, url, out):
    base = f"{num}-{slug}"
    row = {"№": num, "студия": slug, "адрес": url,
           "дата съёмки": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}
    notes = []

    # ---------- 1. десктоп
    ctx, page, status, n = navigate(browser, {**DESKTOP, **COMMON}, url)
    notes += ["десктоп: " + x for x in n]
    row["итоговый адрес"] = page.url
    row["код ответа"] = status
    d = page.evaluate(PAGE_JS)
    if d.get("empty"):
        notes.append("десктоп: страница пустая — сайт не ответил или прокси не пропустил")
    (out / "html" / f"{base}.html").write_text(page.content(), encoding="utf-8")

    # 10 кадров первого экрана у каждой студии; первый кадр — основной скриншот
    fpaths, ftimes = shoot_frames(page, out / "frames" / f"{base}-1-desktop")
    shutil.copy(fpaths[0], out / "shots" / f"{base}-1-desktop.png")
    dmotion = motion(fpaths)
    strip(fpaths, ftimes, out / "gallery" / f"{base}-1-desktop.png", 480, 5)
    if d["videos"] and d["videos"][0]["poster"]:
        save_remote(ctx.request, d["videos"][0]["poster"],
                    out / "frames" / f"{base}-1-desktop-poster", page.url)

    og_file = save_remote(ctx.request, d["og_image"], out / "shots" / f"{base}-3-og", page.url)
    robots = check_robots(ctx.request, page.url, out / "robots" / f"{base}-robots.txt")
    ctx.close()

    # ---------- 2. телефон
    dev = dict(p.devices[MOBILE_DEVICE])
    dev.pop("default_browser_type", None)
    dev["viewport"] = {"width": 390, "height": 844}   # по протоколу: экран 390 × 844
    ctx, page, _, n = navigate(browser, {**dev, **COMMON}, url)
    notes += ["телефон: " + x for x in n if not x.startswith(("сертификат", "открылся"))]
    m = page.evaluate(PAGE_JS)
    mpaths, mtimes = shoot_frames(page, out / "frames" / f"{base}-2-mobile")
    shutil.copy(mpaths[0], out / "shots" / f"{base}-2-mobile.png")
    mmotion = motion(mpaths)
    strip(mpaths, mtimes, out / "gallery" / f"{base}-2-mobile.png", 180, 10)
    ctx.close()

    # ---------- строка таблицы
    v = d["videos"][0] if d["videos"] else None
    mv = m["videos"][0] if m["videos"] else None
    big, mbig = d["biggest_text"] or {}, m["biggest_text"] or {}
    contact, mcontact = d["contact"], m["contact"]
    ratio = ""
    if big.get("fontSize") and contact and contact.get("fontSize"):
        ratio = round(big["fontSize"] / contact["fontSize"], 1)

    row.update({
        "title": d["title"],
        "description": d["description"],
        "meta robots": d["meta_robots"],
        "H1 штук": d["h1_count"], "H1 текст": " | ".join(d["h1"]),
        "H2 штук": d["h2_count"], "H2 первые": " | ".join(d["h2"]),
        "картинок": d["img_count"], "с alt": d["img_with_alt"],
        "доля с alt, %": round(100 * d["img_with_alt"] / d["img_count"], 1) if d["img_count"] else "",
        "og:title": d["og_title"], "og:description": d["og_description"],
        "og:image": d["og_image"], "og:image файл": og_file,
        "главная картинка (десктоп)": d["main_visual"],
        "её доля экрана, %": d["main_visual_share"],
        "правый клик заблокирован": yesno(d["context_menu_blocked"]),
        "видео на первом экране": "да" if (d["videos"] or d["players"]) else "нет",
        "движение (десктоп)": dmotion["тип"],
        "смен кадра за 10 с (десктоп)": dmotion["смен кадра"],
        "доля экрана в движении, % (десктоп)": dmotion["доля экрана в движении, %"],
        "движение (телефон)": mmotion["тип"],
        "смен кадра за 10 с (телефон)": mmotion["смен кадра"],
        "доля экрана в движении, % (телефон)": mmotion["доля экрана в движении, %"],
        "видео: тип": v["type"] if v else ("встроенный плеер" if d["players"] else ""),
        "видео: адрес": v["src"] if v else (d["players"][0]["src"] if d["players"] else ""),
        "видео: постер": "да" if v and v["poster"] else ("нет" if v else ""),
        "видео: доля экрана, %": v["share"] if v else (d["players"][0]["share"] if d["players"] else ""),
        "видео на телефоне": "нет видео" if not mv else ("играет" if mv["playing"] else "стоит"),
        "Pinterest: запрет nopin": yesno(d["pinterest_nopin"]),
        "Pinterest: картинок можно закрепить": d["pinnable_images"],
        "контакт на 1 экране (десктоп)": f"да: {contact['text'] or contact['href']}" if contact else "нет",
        "контакт на 1 экране (телефон)": f"да: {mcontact['text'] or mcontact['href']}" if mcontact else "нет",
        "крупный текст (десктоп)": big.get("text", ""),
        "его кегль, px": big.get("fontSize", ""),
        "кегль контакта, px": contact.get("fontSize", "") if contact else "",
        "заявление ÷ действие": ratio,
        "шрифт крупного текста (десктоп)": big.get("font", ""),
        "шрифт крупного текста (телефон)": mbig.get("font", ""),
        "шрифт сменился на телефоне": yesno(big.get("font") != mbig.get("font")) if big and mbig else "",
        "горизонтальная прокрутка на телефоне": yesno(m["horizontal_overflow"]),
        "соцсети": ", ".join(d["socials"]),
    })
    row.update(robots)
    row["примечания"] = "; ".join(notes)
    return row, {"desktop": d, "mobile": m, "robots": robots,
                 "frame_times": {"desktop": ftimes, "mobile": mtimes},
                 "motion": {"desktop": dmotion, "mobile": mmotion}}


def write_gallery(out, rows):
    """gallery.html — все студии на одной странице: скриншоты, раскадровки, данные."""
    esc = htmllib.escape
    keys = ["движение (десктоп)", "смен кадра за 10 с (десктоп)", "доля экрана в движении, % (десктоп)",
            "движение (телефон)", "видео на первом экране", "главная картинка (десктоп)",
            "контакт на 1 экране (десктоп)", "контакт на 1 экране (телефон)", "og:image файл",
            "H1 штук", "доля с alt, %", "GPTBot (ChatGPT)", "примечания"]
    parts = []
    for r in rows:
        base = f"{r['№']}-{r['студия']}"
        og = r.get("og:image файл") or ""
        og_html = (f'<img src="shots/{esc(og)}" alt="og:image">' if og and not og.startswith("ошибка")
                   else '<div class="none">нет картинки для карточки ссылки</div>')
        facts = "".join(f"<tr><th>{esc(k)}</th><td>{esc(str(r.get(k, '')))}</td></tr>" for k in keys if r.get(k) not in (None, ""))
        parts.append(f"""
<section id="s{esc(base)}">
  <h2>{esc(r['№'])} · {esc(r['студия'])} <a href="{esc(r['адрес'])}">{esc(r['адрес'])}</a></h2>
  <div class="row">
    <figure class="desk"><img src="shots/{esc(base)}-1-desktop.png" alt=""><figcaption>десктоп 1440 × 900</figcaption></figure>
    <figure class="mob"><img src="shots/{esc(base)}-2-mobile.png" alt=""><figcaption>телефон 390 × 844</figcaption></figure>
    <figure class="og">{og_html}<figcaption>карточка ссылки</figcaption></figure>
  </div>
  <figure><img src="gallery/{esc(base)}-1-desktop.png" alt=""><figcaption>десктоп · 10 кадров по одному в секунду</figcaption></figure>
  <figure><img src="gallery/{esc(base)}-2-mobile.png" alt=""><figcaption>телефон · 10 кадров по одному в секунду</figcaption></figure>
  <table>{facts}</table>
</section>""")
    page = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Галерея съёмки</title>
<style>
body{{margin:0;padding:32px;background:#161615;color:#e9e7e1;font:14px/1.45 ui-monospace,Menlo,monospace}}
section{{border-top:1px solid #444;padding:28px 0}} h2{{font-size:20px;margin:0 0 16px}} h2 a{{color:#8f8c85;font-size:14px;margin-left:12px}}
.row{{display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap}} figure{{margin:0 0 16px}} img{{display:block;max-width:100%;border:1px solid #333}}
.desk img{{width:720px}} .mob img{{width:195px}} .og img{{width:360px}} figcaption{{color:#8f8c85;margin-top:6px}}
.none{{width:360px;height:189px;border:1px dashed #555;display:flex;align-items:center;justify-content:center;color:#8f8c85}}
table{{border-collapse:collapse}} th,td{{text-align:left;padding:4px 16px 4px 0;vertical-align:top}} th{{color:#8f8c85;font-weight:400}}
</style></head><body><h1>Галерея съёмки · {len(rows)} студий</h1>{''.join(parts)}</body></html>"""
    (out / "gallery.html").write_text(page, encoding="utf-8")


def parse_proxy(url):
    """socks5://host:port или http://логин:пароль@host:port → настройки Playwright
    и подпись без пароля для вывода и таблицы."""
    u = urllib.parse.urlparse(url if "://" in url else "http://" + url)
    server = f"{u.scheme}://{u.hostname}:{u.port}" if u.port else f"{u.scheme}://{u.hostname}"
    conf = {"server": server}
    if u.username:
        conf["username"] = urllib.parse.unquote(u.username)
        conf["password"] = urllib.parse.unquote(u.password or "")
    label = server + (" (с логином)" if u.username else "")
    if u.username and u.scheme.startswith("socks"):
        print("Внимание: SOCKS-прокси с логином и паролем Chromium не поддерживает. "
              "Нужен HTTP-прокси или SOCKS без авторизации.")
    return conf, label


def system_proxy():
    """Прокси из настроек macOS (Системные настройки → Сеть → Прокси).
    Сначала SOCKS, потом HTTPS, потом HTTP. Возвращает адрес или None."""
    if sys.platform != "darwin":
        return None
    try:
        txt = subprocess.run(["scutil", "--proxy"], capture_output=True, text=True, timeout=5).stdout
    except Exception:  # noqa: BLE001
        return None
    conf = dict(re.findall(r"^\s*(\w+)\s*:\s*(\S+)\s*$", txt, re.M))
    for kind, scheme in (("SOCKS", "socks5"), ("HTTPS", "http"), ("HTTP", "http")):
        if conf.get(f"{kind}Enable") == "1" and conf.get(f"{kind}Proxy"):
            port = conf.get(f"{kind}Port", "1080" if kind == "SOCKS" else "8080")
            return f"{scheme}://{conf[kind + 'Proxy']}:{port}"
    return None


def save_all(out, rows, raw):
    """Пишет data.csv, data.json и gallery.html. Вызывается после каждой студии,
    чтобы прерванная съёмка не теряла сделанное."""
    cols = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    with open(out / "data.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter=";")
        w.writeheader()
        w.writerows(rows)
    (out / "data.json").write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
    write_gallery(out, rows)


def load_previous(out):
    """Что уже снято в этой папке: строки таблицы и данные по каждой студии."""
    prev_rows, prev_raw = {}, {}
    if (out / "data.json").exists():
        try:
            prev_raw = json.loads((out / "data.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            prev_raw = {}
    if (out / "data.csv").exists():
        with open(out / "data.csv", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f, delimiter=";"):
                prev_rows[f"{r.get('№', '')}-{r.get('студия', '')}"] = r
    done = {k for k, v in prev_raw.items()
            if isinstance(v, dict) and "error" not in v and k in prev_rows
            and (out / "shots" / f"{k}-1-desktop.png").exists()
            and (out / "shots" / f"{k}-2-mobile.png").exists()}
    return prev_rows, prev_raw, done


def main():
    ap = argparse.ArgumentParser(description="Съёмка сайтов студий по протоколу")
    ap.add_argument("--only", help="номера студий через запятую, например 01,08 — "
                                   "эти студии переснимаются, даже если уже сняты")
    ap.add_argument("--out", help="папка для результатов (по умолчанию capture-ГГГГ-ММ-ДД); "
                                  "можно указать папку прошлой съёмки, чтобы доснять её")
    ap.add_argument("--force", action="store_true", help="переснять все студии заново")
    ap.add_argument("--headed", action="store_true", help="показывать окно браузера")
    ap.add_argument("--proxy", default="auto",
                    help="прокси: auto — взять из настроек macOS (по умолчанию); "
                         "none — без прокси; или адрес, например socks5://127.0.0.1:1080")
    args = ap.parse_args()

    if args.proxy == "auto":
        proxy = system_proxy()
    elif args.proxy.lower() in ("none", "no", "off", ""):
        proxy = None
    else:
        proxy = args.proxy
    proxy_conf, proxy_label = (parse_proxy(proxy) if proxy else (None, "нет"))
    if proxy_conf:
        COMMON["proxy"] = proxy_conf

    all_studios = load_studios(None)
    redo = {n.strip().zfill(2) for n in args.only.split(",")} if args.only else set()
    out = pathlib.Path(args.out) if args.out else HERE / f"capture-{dt.date.today():%Y-%m-%d}"
    for sub in ("shots", "frames", "html", "robots", "gallery"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    prev_rows, prev_raw, done = load_previous(out)
    todo = [s for s in all_studios
            if args.force or s[0] in redo or f"{s[0]}-{s[1]}" not in done]
    if args.only:
        todo = [s for s in todo if s[0] in redo]
    print(f"Прокси: {proxy_label if proxy_conf else 'нет, прямое соединение'}")
    print(f"Папка: {out}\nВ списке {len(all_studios)} студий, уже снято {len(done)}, "
          f"снимаем сейчас {len(todo)}.\n")

    rows_by_key = {f"{n}-{s}": prev_rows[f"{n}-{s}"]
                   for n, s, _ in all_studios if f"{n}-{s}" in prev_rows}
    raw = {k: prev_raw[k] for k in rows_by_key if k in prev_raw}

    def ordered_rows():
        return [rows_by_key[f"{n}-{s}"] for n, s, _ in all_studios if f"{n}-{s}" in rows_by_key]

    if todo:
        with sync_playwright() as p:
            launch = {"headless": not args.headed}
            if proxy_conf:
                launch["proxy"] = proxy_conf
            browser = p.chromium.launch(**launch)
            for num, slug, url in todo:
                key = f"{num}-{slug}"
                print(f"{num} {slug:12} {url} … ", end="", flush=True)
                try:
                    row, data = shoot_studio(p, browser, num, slug, url, out)
                    print("готово" + (f" ({row['примечания']})" if row["примечания"] else ""))
                except Exception as e:  # noqa: BLE001
                    row = {"№": num, "студия": slug, "адрес": url,
                           "дата съёмки": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                           "примечания": f"ОШИБКА: {type(e).__name__}: {str(e).splitlines()[0][:200]}"}
                    data = {"error": row["примечания"]}
                    print(row["примечания"])
                row["прокси"] = proxy_label
                rows_by_key[key] = row
                raw[key] = data
                save_all(out, ordered_rows(), raw)
            browser.close()
    else:
        save_all(out, ordered_rows(), raw)

    skipped = [f"{n}-{s}" for n, s, _ in all_studios if f"{n}-{s}" in done and (n, s, _) not in todo]
    if skipped:
        print(f"\nПропущены как уже снятые: {', '.join(skipped)}")
        print("Переснять конкретные: --only 03,07; переснять всё: --force")
    print(f"\nГотово: {out}\n  галерея — gallery.html, скриншоты — shots/, кадры — frames/, таблица — data.csv")


if __name__ == "__main__":
    sys.exit(main())
