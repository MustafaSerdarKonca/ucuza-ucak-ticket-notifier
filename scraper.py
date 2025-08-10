#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ucuzaucak.net DOM scraping (Playwright)
- Ana sayfa: ilan kartlarını DOM yüklendikten sonra bulur
- Detay sayfası: görünen tarih maddelerini toplar
- state.json ile idempotent
- config.yaml ile filtreleme + mesaj şablonu
- Telegram’a gönderim: telegram.py
"""

import os
import re
import json
import time
import yaml
import unicodedata
import random
import logging
from datetime import datetime, timezone
from urllib.parse import urljoin

from telegram import send_message  # telegram.py
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

# =========================
#  AYARLAR
# =========================
BASE_URL = "https://ucuzaucak.net/"

# ---- CSS/XPath/Heuristik Seçiciler ----
# Site yapısı değişirse burada oynayacağız.
# 1) Kart kapsayıcı adayları (esnek tutuyoruz)
CARD_LOCATORS = [
    "a:has-text('→')",             # içinde yön oku olan linkler
    "article a",                   # WP tema: yazı linki
    "a.entry-title",               # başlık linki
    "a.relative",                  # önceki tahmin
]

# 2) Kart içinden route, price, time çekmeye yardımcı regexler
PRICE_RE = re.compile(r"(\d[\d\.\s]{1,12})\s?(?:TL|₺)", re.IGNORECASE)
ARROW_RE = re.compile(r"(.+?)\s*(?:→|->|›|▶|–|-)\s*(.+)", re.UNICODE)

# 3) Detay sayfasındaki tarih listesi için seçiciler
DETAIL_DATE_LOCATORS = [
    "ul li",          # klasik liste
    "div:has-text('Tarih') >> .. li",
    "div:has-text('Uygun') >> .. li",
]

# Playwright zaman aşımı (ms)
NAV_TIMEOUT = 25_000
WAIT_DOM_MS = 6_000

# Dosya yolları
STATE_PATH = os.path.join("data", "state.json")
CONFIG_PATH = "config.yaml"

# Log
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


# =========================
#  YARDIMCI FONKSİYONLAR
# =========================
def ensure_dirs():
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)

def load_state():
    ensure_dirs()
    if not os.path.exists(STATE_PATH):
        return {"seen_ids": {}}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"seen_ids": {}}

def save_state(state: dict):
    ensure_dirs()
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def load_config(path=CONFIG_PATH):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def parse_price_to_int(s: str) -> int:
    # "3.299 TL" -> 3299; "12 450₺" -> 12450
    digits = re.sub(r"[^\d]", "", s or "")
    return int(digits) if digits else 0

def extract_route(text: str):
    """Metinden kalkış/varış ayıkla; oku (→, -, ›) baz alıyoruz."""
    m = ARROW_RE.search(text or "")
    if not m:
        return clean(text), ""
    return clean(m.group(1)), clean(m.group(2))

def make_id_from_url(url: str):
    return url  # URL benzersiz kabul

def normalize_tr(s: str) -> str:
    """
    Türkçe karakter ve i/ı/İ normalizasyonu + aksan kaldırma + lower.
    'İstanbul', 'ISTANBUL', 'ıstanbul' -> 'istanbul'
    """
    if not s:
        return ""
    s = s.replace("İ", "i").replace("I", "i").replace("ı", "i")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.lower().strip()

def apply_filters(listings, cfg):
    filt = (cfg.get("filters") or {})
    dep = normalize_tr(filt.get("departure") or "")
    arrivals = [normalize_tr(a) for a in (filt.get("arrivals") or [])]
    max_price = int(filt.get("max_price") or 0)

    out = []
    for it in listings:
        origin_n = normalize_tr(it.get("origin", ""))
        dest_n   = normalize_tr(it.get("destination", ""))

        if dep and dep not in origin_n:
            continue
        if arrivals and all(a not in dest_n for a in arrivals):
            continue
        if max_price and (it.get("price") or 0) > max_price:
            continue
        out.append(it)
    return out


def format_message(item, dates, cfg):
    tmpl = cfg.get("message_template") or (
        "✈️ {origin} → {destination} — {price} TL\nTarihler: {dates}\nKaynak: {url}"
    )
    date_str = ", ".join(dates) if dates else "—"
    price_disp = item.get("price_text") or str(item.get("price", "—"))
    return tmpl.format(
        origin=item.get("origin", ""),
        destination=item.get("destination", ""),
        price=price_disp,
        dates=date_str,
        url=item.get("url", ""),
    )

# =========================
#  PLAYWRIGHT SCRAPERS
# =========================
def collect_cards(page):
    """
    Ana sayfada Elementor kolonları içindeki uçuş kartlarını topla.
    Mantık:
      - /ucak-bileti/ içeren linkleri al
      - Her benzersiz href için en yakın elementor-column atasının tüm metnini oku
      - Metinden rota (origin, destination) ve fiyatı çıkar
    """
    items = []
    seen = set()

    # Sayfada linkler gerçekten görününceye kadar bekle
    try:
        page.wait_for_selector('a[href*="/ucak-bileti/"]', timeout=15000)
    except Exception:
        pass

    links = page.locator('a[href*="/ucak-bileti/"]').all()
    for a in links:
        href = a.get_attribute("href") or ""
        if not href or href in seen:
            continue
        seen.add(href)

        # En yakın Elementor kolon sarmalayıcısı
        try:
            col = a.locator("xpath=ancestor::div[contains(@class,'elementor-column')][1]")
            block_text = col.inner_text()
        except Exception:
            block_text = a.inner_text()

        text = clean(block_text)

        # Rota (ok veya tire ile ayrılmış)
        # Ör: "İstanbul - Tokyo" / "İstanbul → Tokyo"
        origin, destination = extract_route(text)

        # Fiyat
        m = PRICE_RE.search(text)
        price_text = clean(m.group(0)) if m else ""
        price_int = parse_price_to_int(price_text)

        # Çok zayıf adayları ele (hem rota hem fiyat yoksa)
        if not origin and not destination and price_int == 0:
            continue

        items.append({
            "id": make_id_from_url(href),
            "url": href,
            "origin": origin,
            "destination": destination,
            "price_text": price_text,
            "price": price_int,
            "posted_text": "",  # istersen ayrıca "paylaşıldı" metni için regex ekleyebiliriz
        })

    return items

def collect_detail_dates(page):
    """Detay sayfasındaki görünen tarih maddeleri."""
    dates = []
    for loc in DETAIL_DATE_LOCATORS:
        try:
            for li in page.locator(loc).all():
                t = clean(li.inner_text())
                if t and len(t) < 120:
                    dates.append(t)
        except Exception:
            pass
    # Benzersiz sırayı koru
    uniq = []
    for d in dates:
        if d not in uniq:
            uniq.append(d)
    return uniq[:50]


def run_scrape():
    cfg = load_config()
    state = load_state()
    seen = state.get("seen_ids", {})

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0 Safari/537.36"
            ),
            locale="tr-TR",
        )
        page = context.new_page()

        logging.info("Ana sayfa açılıyor...")
        page.goto(BASE_URL, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
        page.wait_for_selector('a[href*="/ucak-bileti/"]', timeout=15000)
        page.wait_for_timeout(1000)  # minik tampon
        # Biraz bekle ki JS listeyi doldursun
        page.wait_for_timeout(WAIT_DOM_MS)

        # Bazı siteler scroll sonrası yükler
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1500)
        except Exception:
            pass

        listings = collect_cards(page)
        # Örnek ilk 5 kartı logla (rota, fiyat, url)
        for i, it in enumerate(listings[:5], 1):
            logging.info(f"[Örnek {i}] {it.get('origin')} -> {it.get('destination')} | {it.get('price_text')} | {it.get('url')}")

        logging.info(f"Ana sayfada bulunan kart sayısı: {len(listings)}")

        filtered = apply_filters(listings, cfg)
        logging.info(f"Filtre sonrası {len(filtered)} ilan kaldı.")

        new_items = [it for it in filtered if it["id"] not in seen]
        logging.info(f"Yeni ilan sayısı: {len(new_items)}")

        for idx, item in enumerate(new_items, 1):
            try:
                # Nazik olun: 1–3 sn bekle
                time.sleep(random.uniform(1.0, 3.0))
                logging.info(f"Detay sayfasına gidiliyor: {item['url']}")
                page.goto(item["url"], timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
                page.wait_for_timeout(WAIT_DOM_MS)
                # Bazı sayfalar “devamını oku” tarzı gizleme kullanabilir
                dates = collect_detail_dates(page)
            except PwTimeout:
                logging.warning("Detay sayfası zaman aşımı.")
                dates = []
            except Exception as e:
                logging.warning(f"Detay sayfası hata: {e}")
                dates = []

            msg = format_message(item, dates, cfg)
            ok, err = send_message(msg)
            if ok:
                logging.info(f"[{idx}/{len(new_items)}] Telegram'a gönderildi.")
                seen[item["id"]] = {
                    "first_seen": datetime.now(timezone.utc).isoformat(),
                    "url": item["url"],
                    "price": item.get("price", 0),
                }
                state["seen_ids"] = seen
                save_state(state)
            else:
                logging.error(f"Telegram gönderim hatası: {err}")

        context.close()
        browser.close()

    if not new_items:
        logging.info("Yeni ilan yok veya selektörler eşleşmedi. İşlem tamam.")


if __name__ == "__main__":
    run_scrape()
