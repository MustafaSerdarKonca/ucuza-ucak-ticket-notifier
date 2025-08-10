#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
UcuzUcak.net scraper
- Ana sayfadaki rota kartlarını çeker (kalkış, varış, fiyat, paylaşım zamanı, ilan URL)
- İlan detay sayfasından görünen "uygun tarih aralıkları" listesini toplar
- state.json ile karşılaştırıp sadece yeni ilanları Telegram'a yollar
- İsteklerde rastgele 1–3 sn gecikme, 3 denemeli artan bekleme, basit loglama
- Filtreler ve mesaj şablonu config.yaml'dan okunur
- CSS selektörleri en üstte değişkenlerde toplandı (site yapısı değişirse hızlı değiştirin)

Tamamen ücretsiz:
- Python (requests + bs4)
- GitHub Actions (cron */10 * * * *) ile zamanlayıcı
"""

import os
import re
import json
import time
import random
import logging
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import yaml

from telegram import send_message  # telegram.py içinden

# =========================
#  SITE & SELECTOR AYARLARI
# =========================
BASE_URL = "https://ucuzaucak.net/"

# Ana sayfa – ilan kartı selektörleri
# Not: Site yapısı değişirse sadece bu kısımda güncelleme yapmanız genelde yeterli olur.
CARD_SELECTOR = "a.relative.flex.flex-col"  # İlan kartı link kapsayıcısı (örnek Tailwind sınıfları)
ROUTE_TEXT_SELECTOR = ".flex.items-center.gap-1.text-sm"  # "İstanbul → Paris" vb.
PRICE_SELECTOR = ".text-lg.font-semibold"  # "3.299 TL" vb.
TIME_SELECTOR = "time, .text-xs.text-gray-500"  # Paylaşım/güncelleme zamanı gibi görünen alan
URL_ATTR = "href"  # Kart linki

# Detay sayfası – uygun tarih listesi selektörü
DETAIL_DATES_LIST_SELECTOR = "ul li"  # Detayda listelenen tarih maddeleri (gerekirse özelleştirin)

# =========================
#  İSTEK & UA AYARLARI
# =========================
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.google.com/",
    "Cache-Control": "no-cache",
}

SESSION = requests.Session()
SESSION.headers.update(DEFAULT_HEADERS)

# =========================
#  DOSYA YOLLARI
# =========================
STATE_PATH = os.path.join("data", "state.json")
CONFIG_PATH = "config.yaml"

# =========================
#  LOG AYARLARI
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

# =========================
#  YARDIMCI FONKSİYONLAR
# =========================
def load_config(path=CONFIG_PATH):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

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

def http_get(url: str, max_retries=3, base_sleep=1.5, timeout=20):
    """Basit retry + artan bekleme ile GET isteği."""
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = SESSION.get(url, timeout=timeout)
            if resp.status_code == 200:
                return resp
            logging.warning(f"GET {url} status={resp.status_code}")
        except requests.RequestException as e:
            last_exc = e
            logging.warning(f"GET hata (deneme {attempt}/{max_retries}): {e}")
        # artan bekleme
        sleep_s = base_sleep * attempt + random.uniform(0, 0.5)
        time.sleep(sleep_s)
    if last_exc:
        raise last_exc
    raise RuntimeError(f"{url} için GET başarısız")

def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()

def parse_price_to_int(s: str) -> int:
    # "3.299 TL" -> 3299 ; "12.450₺" -> 12450
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else 0

def extract_route(route_text: str):
    """
    "İstanbul (IST) → Paris (CDG)" gibi bir metinden kalkış/varış çıkarma.
    Site metnine göre bu regex'i sade tuttuk; gerekirse özelleştirin.
    """
    text = route_text.replace("–", "→")
    parts = [p.strip() for p in text.split("→")]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return text, ""  # ayrışmazsa tümünü kalkışa yaz

def make_id_from_url(url: str):
    # URL benzersiz kabul edilir (idempotency için)
    return url

def fetch_detail_dates(detail_url: str) -> list:
    """Detay sayfasındaki görünen tarih maddelerini toplayın (örn. ul>li)."""
    resp = http_get(detail_url)
    soup = BeautifulSoup(resp.text, "lxml")
    items = [clean_text(li.get_text(" ")) for li in soup.select(DETAIL_DATES_LIST_SELECTOR)]
    # Yalnızca tarih gibi görünenleri tutmak isterseniz basit bir filtre uygulayabilirsiniz:
    # items = [it for it in items if re.search(r"\d{1,2}\s+[A-Za-zÇĞİÖŞÜçğıöşü]+\s+\d{4}", it)]
    return items[:50]  # güvenlik için çok uzunsa kes

def parse_homepage_listings(base_url: str) -> list:
    """Ana sayfadaki ilan kartlarını ayrıştırır."""
    resp = http_get(base_url)
    soup = BeautifulSoup(resp.text, "lxml")

    listings = []
    for a in soup.select(CARD_SELECTOR):
        try:
            url_rel = a.get(URL_ATTR) or ""
            url = urljoin(base_url, url_rel)

            # Rota metni
            route_el = a.select_one(ROUTE_TEXT_SELECTOR)
            route_text = clean_text(route_el.get_text(" ")) if route_el else ""
            origin, destination = extract_route(route_text)

            # Fiyat
            price_el = a.select_one(PRICE_SELECTOR)
            price_text = clean_text(price_el.get_text(" ")) if price_el else ""
            price = parse_price_to_int(price_text)

            # Paylaşım zamanı / metni
            time_el = a.select_one(TIME_SELECTOR)
            posted_text = clean_text(time_el.get_text(" ")) if time_el else ""

            # Basit doğrulama
            if not url or (not origin and not destination and price == 0):
                continue

            listings.append({
                "id": make_id_from_url(url),
                "url": url,
                "origin": origin,
                "destination": destination,
                "price_text": price_text,
                "price": price,
                "posted_text": posted_text,
            })
        except Exception as e:
            logging.warning(f"Kart ayrıştırma hatası: {e}")
        # İstek benzetimi/engellenme riskini azaltmak için minik gecikme:
        time.sleep(random.uniform(0.2, 0.5))

    return listings

def apply_filters(listings: list, cfg: dict) -> list:
    """config.yaml filtreleri uygula."""
    filt = cfg.get("filters", {}) or {}

    dep = (filt.get("departure") or "").strip().lower()
    arrivals = [a.strip().lower() for a in (filt.get("arrivals") or [])]
    max_price = int(filt.get("max_price") or 0)  # 0 = sınırsız

    out = []
    for it in listings:
        if dep and dep not in it["origin"].lower():
            continue
        if arrivals and all(a not in it["destination"].lower() for a in arrivals):
            continue
        if max_price and it["price"] and it["price"] > max_price:
            continue
        out.append(it)
    return out

def format_message(item: dict, dates: list, cfg: dict) -> str:
    tmpl = cfg.get("message_template") or (
        "✈️ {origin} → {destination} — {price} TL\n"
        "Tarihler: {dates}\n"
        "Kaynak: {url}"
    )
    date_str = ", ".join(dates) if dates else "—"
    price_num = item.get("price") or 0
    # Eğer metin olarak fiyat daha uygunsa:
    price_disp = item.get("price_text") or str(price_num)

    return tmpl.format(
        origin=item.get("origin", ""),
        destination=item.get("destination", ""),
        price=price_disp,
        dates=date_str,
        url=item.get("url", ""),
    )

def main():
    cfg = load_config()
    state = load_state()
    seen = state.get("seen_ids", {})

    logging.info("Ana sayfa taranıyor...")
    all_listings = parse_homepage_listings(BASE_URL)
    logging.info(f"{len(all_listings)} ilan aday bulundu.")

    filtered = apply_filters(all_listings, cfg)
    logging.info(f"Filtre sonrası {len(filtered)} ilan kaldı.")

    new_items = [it for it in filtered if it["id"] not in seen]
    logging.info(f"Yeni ilan sayısı: {len(new_items)}")

    for idx, item in enumerate(new_items, 1):
        # Detay sayfasındaki tarihleri çek
        try:
            # 1–3 sn rastgele gecikme (daha doğal trafik için)
            time.sleep(random.uniform(1.0, 3.0))
            dates = fetch_detail_dates(item["url"])
        except Exception as e:
            logging.warning(f"Detay tarihleri alınamadı ({item['url']}): {e}")
            dates = []

        # Mesajı formatla ve Telegram'a gönder
        try:
            msg = format_message(item, dates, cfg)
            ok, err = send_message(msg)
            if ok:
                logging.info(f"[{idx}/{len(new_items)}] Telegram'a gönderildi: {item['url']}")
                # idempotency için görüldü olarak işaretle
                seen[item["id"]] = {
                    "first_seen": datetime.now(timezone.utc).isoformat(),
                    "url": item["url"],
                    "price": item.get("price", 0),
                }
                # Her gönderi sonrası state'i yaz (aksama olursa kaybolmasın)
                state["seen_ids"] = seen
                save_state(state)
            else:
                logging.error(f"Telegram gönderim hatası: {err}")
        except Exception as e:
            logging.error(f"Mesaj gönderim hatası: {e}")

    if not new_items:
        logging.info("Yeni ilan yok. İşlem tamam.")

if __name__ == "__main__":
    main()
