#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, date
from zoneinfo import ZoneInfo
from typing import List, Tuple, Optional, Dict, Set

import requests
from bs4 import BeautifulSoup, Tag, NavigableString

# --- Config ---
BASE = "https://ucuzaucak.net"
LIST_PAGES = [
    BASE + "/",
    BASE + "/ucak-bileti/",
    BASE + "/kategori/ucak-bileti/",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) ucuz-ucak-watcher/1.3 (+github actions)"
}
STATE_FILE = "state.json"
TZ = ZoneInfo("Europe/Istanbul")

PRICE_RE = re.compile(r"((?:\d{1,3}(?:\.\d{3})+|\d+))\s*TL", re.IGNORECASE)

TR_MONTHS = {
    "Ocak": 1, "Şubat": 2, "Mart": 3, "Nisan": 4, "Mayıs": 5, "Haziran": 6,
    "Temmuz": 7, "Ağustos": 8, "Eylül": 9, "Ekim": 10, "Kasım": 11, "Aralık": 12
}
TR_MONTHS_RE = "|".join(TR_MONTHS.keys())
TR_WEEKDAYS_RE = r"Pazartesi|Salı|Çarşamba|Perşembe|Cuma|Cumartesi|Pazar"

# 24 Kasım Pazartesi – 01 Aralık Pazartesi  (hafta içi opsiyonel, (X Gün) opsiyonel)
DATE_RANGE_RE = re.compile(
    rf"\b(\d{{1,2}})\s+({TR_MONTHS_RE})(?:\s+(?:{TR_WEEKDAYS_RE}))?\s*[–\-]\s*(\d{{1,2}})\s+({TR_MONTHS_RE})(?:\s+(?:{TR_WEEKDAYS_RE}))?(?:\s*\(\d+\s*Gün\))?",
    re.IGNORECASE,
)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


def load_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return {"posts": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"posts": {}}


def save_state(state: Dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


def http_get(url: str, use_playwright_fallback: bool = True) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        html = r.text
        if use_playwright_fallback and _looks_empty(html):
            logging.info("HTML looked minimal; invoking Playwright fallback for %s", url)
            html = render_with_playwright(url)
        return html
    except Exception as e:
        logging.warning("requests get failed for %s: %s", url, e)
        if use_playwright_fallback:
            return render_with_playwright(url)
        raise


def _looks_empty(html: str) -> bool:
    return len(html) < 3000


def render_with_playwright(url: str) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logging.error("Playwright not installed; cannot render %s", url)
        return ""
    html = ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=HEADERS["User-Agent"], locale="tr-TR")
            page = context.new_page()
            page.set_default_navigation_timeout(30000)
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            html = page.content()
            context.close()
            browser.close()
    except Exception as e:
        logging.error("Playwright failed for %s: %s", url, e)
    return html


def normalize_space(s: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", s or "").strip()


def pick_text(el: Optional[Tag]) -> str:
    return normalize_space(el.get_text(" ", strip=True)) if el else ""


# ---------- Discovery ----------

def discover_post_links(max_posts: int) -> List[str]:
    seen: Set[str] = set()
    links: List[str] = []
    for lp in LIST_PAGES:
        logging.info("Discovering links from: %s", lp)
        html = http_get(lp)
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")

        for a in soup.select("a[href]"):
            href = a["href"]
            if "/ucak-bileti/" in href:
                url = href if href.startswith("http") else BASE + href
                if url.startswith(BASE) and url not in seen:
                    seen.add(url)
                    links.append(url)

        if len(links) >= max_posts:
            break

    links = links[:max_posts]
    logging.info("Discovered %d post link(s).", len(links))
    for u in links:
        logging.info("  - %s", u)
    return links


# ---------- Price from listing (stronger) ----------

def _nodes_after(node: Tag, max_steps: int = 60):
    """Yield up to max_steps of next elements (siblings/descendants in doc order)."""
    steps = 0
    for el in node.next_elements:
        if steps >= max_steps:
            break
        steps += 1
        yield el


def _nearest_price_around_anchor(soup: BeautifulSoup, anchor: Tag) -> str:
    """Search anchor's ancestors, descendants, siblings, and nearby nodes for a TL price."""
    # 1) Within the anchor subtree
    txt = pick_text(anchor)
    m = PRICE_RE.search(txt)
    if m:
        return m.group(0)

    # 2) Up to 6 ancestors (likely card container)
    ancestors: List[Tag] = []
    node: Optional[Tag] = anchor
    for _ in range(6):
        if isinstance(node, Tag):
            ancestors.append(node)
            node = node.parent  # type: ignore[attr-defined]

    for anc in ancestors:
        t = pick_text(anc)
        m = PRICE_RE.search(t)
        if m:
            return m.group(0)

    # 3) Following nodes near this anchor (stay local)
    for el in _nodes_after(anchor, max_steps=80):
        if isinstance(el, Tag):
            t = pick_text(el)
            m = PRICE_RE.search(t)
            if m:
                return m.group(0)

    # 4) As a last resort, pick the **closest** text node with TL in full doc
    best = ""
    best_dist = 1e9
    tl_nodes = soup.find_all(string=PRICE_RE)
    for s in tl_nodes:
        try:
            # rough distance heuristic: DOM depth difference
            d = abs(len(list(s.parents)) - len(list(anchor.parents)))
            if d < best_dist:
                best = PRICE_RE.search(str(s)).group(0)  # type: ignore
                best_dist = d
        except Exception:
            continue
    return best


def _price_from_listing(url: str, route_hint: str) -> str:
    """Find price on listing/home near the same link; use route as a hint if needed."""
    try:
        for lp in LIST_PAGES:
            html = http_get(lp, use_playwright_fallback=False)
            if not html:
                continue
            soup = BeautifulSoup(html, "lxml")

            # try exact & relative href
            a = soup.find("a", href=re.compile(re.escape(url))) or \
                soup.find("a", href=re.compile(re.escape(url.replace(BASE, ""))))
            # also try by route text near anchors
            if not a and route_hint:
                for cand in soup.select("a[href]"):
                    if route_hint.lower() in pick_text(cand).lower():
                        a = cand
                        break
            if not a:
                continue

            price = _nearest_price_around_anchor(soup, a)
            if price:
                return price
    except Exception as e:
        logging.debug("price_from_listing failed: %s", e)
    return ""


# ---------- Dates (wider & complete) ----------

def _collect_blocks_after(node: Tag, max_blocks: int = 500) -> List[str]:
    """Collect text chunks after node until next heading or hard cap."""
    out: List[str] = []
    for el in node.next_elements:
        if isinstance(el, Tag) and el.name and re.match(r"^h[1-6]$", el.name, re.I):
            break
        if isinstance(el, Tag) and el.name in ("li", "p", "a", "div", "span"):
            t = el.get_text(" ", strip=True)
            if t:
                out.append(t)
        if len(out) >= max_blocks:
            break
    return out


def _iter_date_ranges_from_text(text: str) -> List[str]:
    return [m.group(0) for m in DATE_RANGE_RE.finditer(text)]


def _extract_date_lines(soup: BeautifulSoup) -> List[str]:
    # Prefer the section after the “Uçak Bileti Tarihleri” anchor
    collected: List[str] = []
    anchor = soup.find(string=re.compile("Uçak Bileti Tarihleri", re.IGNORECASE))
    if isinstance(anchor, (NavigableString,)) and anchor.parent:
        for block in _collect_blocks_after(anchor.parent):
            for rng in _iter_date_ranges_from_text(block):
                collected.append(normalize_space(rng))

    # Also scan the whole page to catch dates split across multiple small blocks
    full_text = soup.get_text(" ", strip=True)
    for rng in _iter_date_ranges_from_text(full_text):
        collected.append(normalize_space(rng))

    # De-dupe preserving order; keep a generous amount (e.g., first 60)
    seen: Set[str] = set()
    uniq = []
    for x in collected:
        if x not in seen:
            uniq.append(x)
            seen.add(x)
        if len(uniq) >= 60:
            break
    return uniq


def _compute_days(line: str) -> Optional[int]:
    m = DATE_RANGE_RE.search(line)
    if not m:
        return None
    d1, m1, d2, m2 = int(m.group(1)), m.group(2), int(m.group(3)), m.group(4)
    mon1 = TR_MONTHS.get(m1.capitalize())
    mon2 = TR_MONTHS.get(m2.capitalize())
    if not mon1 or not mon2:
        return None
    year = datetime.now(TZ).year
    y1 = year
    y2 = year + (1 if mon2 < mon1 else 0)
    try:
        dt1 = date(y1, mon1, d1)
        dt2 = date(y2, mon2, d2)
        return (dt2 - dt1).days
    except Exception:
        return None


def _decorate_with_days(lines: List[str]) -> List[str]:
    out = []
    for ln in lines:
        days = _compute_days(ln)
        base = re.sub(r"\(\d+\s*Gün\)\s*$", "", ln).strip()
        if days is not None and days >= 1:
            out.append(f"{base} ({days} Gün)")
        else:
            out.append(base)
    return out


# ---------- Parse detail ----------

def parse_detail(url: str) -> Tuple[str, str, List[str], Dict[str, str]]:
    html = http_get(url)
    if not html:
        raise RuntimeError(f"Empty HTML for {url}")

    soup = BeautifulSoup(html, "lxml")

    # ROUTE
    route = ""
    for sel in ["h1.entry-title", "header h1", "h1", ".post-title", ".hero-title", "title"]:
        el = soup.select_one(sel)
        if el:
            route = pick_text(el)
            break

    # PRICE from detail, else listing/home (route hint helps)
    full_text = soup.get_text(" ", strip=True)
    m = PRICE_RE.search(full_text or "")
    price = m.group(0) if m else ""
    if not price:
        price = _price_from_listing(url, route_hint=route)

    # DATE LINES (complete + day counts)
    date_lines_raw = _extract_date_lines(soup)
    date_lines = _decorate_with_days(date_lines_raw)

    # Extras
    extra: Dict[str, str] = {}
    for airline in ["THY", "Turkish Airlines", "Pegasus", "SunExpress", "Lufthansa", "Qatar Airways", "Emirates"]:
        if re.search(rf"\b{re.escape(airline)}\b", full_text, re.IGNORECASE):
            extra["airline"] = airline
            break

    if not price:
        price = "(fiyat sitede bulunamadı)"
    return route, price, date_lines, extra


# ---------- Hash / Render / Telegram ----------

def build_hash(route: str, price: str, dates: List[str]) -> str:
    h = hashlib.sha256()
    h.update((route or "").encode("utf-8")); h.update(b"|")
    h.update((price or "").encode("utf-8")); h.update(b"|")
    h.update("\n".join(dates or []).encode("utf-8"))
    return h.hexdigest()


def render_message(route: str, price: str, dates: List[str], url: str, extra: Dict[str, str]) -> str:
    lines = []
    lines.append(f"✈️ {route or '(rota belirtilmemiş)'}")
    lines.append("")
    lines.append(f"💳 Fiyat: {price}")
    lines.append("")
    lines.append("📅 Tarihler:")
    if dates:
        lines.extend(dates)
    else:
        lines.append("(sitede belirtilmemiş)")
    if extra.get("airline"):
        lines.append("")
        lines.append(f"🛫 Havayolu: {extra['airline']}")
    now_tr = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    lines.append("")
    lines.append(f"🕒 Son kontrol: {now_tr} (TR)")
    lines.append("")
    lines.append(f"🔗 Kaynak: {url}")
    return "\n".join(lines)


def telegram_send(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set; skipping Telegram send.")
        return False
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    for attempt in range(1, 4):
        try:
            r = requests.post(api, json=payload, timeout=15)
            if r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", "1"))
                time.sleep(min(2 ** attempt, 8) + retry_after)
                continue
            r.raise_for_status()
            logging.info("Telegram message sent.")
            return True
        except Exception as e:
            logging.warning("Telegram send attempt %d failed: %s", attempt, e)
            time.sleep(min(2 ** attempt, 8))
    logging.error("All Telegram attempts failed.")
    return False


# ---------- Main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-posts", type=int, default=5)
    args = ap.parse_args()

    state = load_state()
    posts_state = state.setdefault("posts", {})

    links = discover_post_links(args.max_posts)

    processed: Set[str] = set()
    for url in links:
        if url in processed:
            continue
        processed.add(url)
        try:
            route, price, dates, extra = parse_detail(url)
            price_hash = build_hash(route, price, dates)
            last = posts_state.get(url)
            changed = (last is None) or (last.get("price_hash") != price_hash)
            logging.info("[%s] route='%s' price='%s' dates=%d changed=%s",
                         url, route, price, len(dates), changed)
            if changed:
                msg = render_message(route, price, dates, url, extra)
                if telegram_send(msg):
                    posts_state[url] = {
                        "price_hash": price_hash,
                        "last_seen": datetime.now(TZ).isoformat(timespec="seconds"),
                        "last_route": route,
                        "last_price": price,
                        "last_dates_preview": dates[:30],
                    }
        except Exception as e:
            logging.error("Failed to process %s: %s", url, e)

    save_state(state)
    logging.info("Run complete.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
