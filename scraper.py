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
from dateutil.parser import parse as dtparse
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

# ------------------------------
# URL'den rota çıkarımı (şehirleri slaktan ayıklama)
# ------------------------------
STOPWORDS = {
    "ucuza", "ucak", "uçak", "bileti", "bilet", "kampanya", "kampanyasi",
    "kampanyası", "fiyati", "fiyatı", "ve", "ile", "gidis", "gidiş",
    "donus", "dönüş", "tek", "yon", "yön", "seyahat", "ucuz", "en",
    "biletleri", "gezi", "rehberi"
}

def prettify_city(token: str) -> str:
    t = (token or "").strip("-_/ ")
    if not t:
        return ""
    # Basit baş harf büyütme; Türkçe özel durumları istersen burada genişletebilirsin
    return t.capitalize()

def infer_route_from_url(url: str):
    """
    Ör: https://ucuzaucak.net/ucak-bileti/istanbul-tokyo-ucuza-ucak-bileti-2/
        → ("İstanbul", "Tokyo")
    Mantık:
      - /ucak-bileti/<slug>/ parçasını al
      - slug'ı '-' ile böl
      - yaygın SEO kelimelerini (STOPWORDS) ele
      - kalan ilk 2 kelimeyi kalkış/varış kabul et
      - 'buenos aires' gibi çok kelimeli şehirler için basit birleştirme desteği
    """
    try:
        m = re.search(r"/ucak-bileti/([^/]+)/?", url)
        if not m:
            return "", ""
        slug = m.group(1)  # istanbul-tokyo-ucuza-ucak-bileti-2
        parts = [p for p in slug.split("-") if p]
        # stopwords ele
        parts = [p for p in parts if p.lower() not in STOPWORDS]
        if len(parts) < 2:
            return "", ""

        # İlk iki parçayı şehir varsay
        o_parts = [parts[0]]
        d_parts = [parts[1]]

        # Çok kelimeli şehir (ör. buenos-aires) basit desteği:
        if parts[0].lower() == "buenos" and len(parts) > 1 and parts[1].lower() == "aires":
            o_parts = ["buenos", "aires"]
            if len(parts) > 2:
                d_parts = [parts[2]]
                if len(parts) > 3 and parts[3][0].isalpha():
                    d_parts.append(parts[3])
        elif parts[1].lower() == "buenos" and len(parts) > 2 and parts[2].lower() == "aires":
            d_parts = ["buenos", "aires"]

        origin = " ".join(prettify_city(p) for p in o_parts)
        destination = " ".join(prettify_city(p) for p in d_parts)
        return origin, destination
    except Exception:
        return "", ""


def expand_content(page):
    """
    Detay sayfada gizli kalan liste/tarih blokları için yaygın butonlara tıklar.
    """
    candidates = [
        'text="Devamını Oku"',
        'text="Devamını oku"',
        'text="Daha Fazla"',
        'text="Daha fazla"',
        'text="Tarih"',
        'text="Tarihler"',
        'role=button[name*="Tarih"i]',
        'role=button[name*="Devam"i]',
    ]
    for sel in candidates:
        try:
            if page.locator(sel).first.is_visible():
                page.locator(sel).first.click()
                page.wait_for_timeout(600)
        except Exception:
            pass

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

# ------------------------------
# Türkçe tarih ayrıştırma & biçimleme
# ------------------------------
TR_MONTHS_MAP = {
    "ocak": 1, "şubat": 2, "subat": 2, "mart": 3, "nisan": 4, "mayıs": 5, "mayis": 5,
    "haziran": 6, "temmuz": 7, "ağustos": 8, "agustos": 8, "eylül": 9, "eylul": 9,
    "ekim": 10, "kasım": 11, "kasim": 11, "aralık": 12, "aralik": 12
}
TR_DAY_NAMES = ["Pazartesi","Salı","Çarşamba","Perşembe","Cuma","Cumartesi","Pazar"]

def norm(s: str) -> str:
    if not s: return ""
    s = s.replace("İ","i").replace("I","i").replace("ı","i")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.lower().strip()

def month_to_num(name: str) -> int:
    return TR_MONTHS_MAP.get(norm(name), 0)

def tr_format_date(dt: datetime) -> str:
    # "24 Kasım Pazartesi" biçimi
    ay_adı = list(TR_MONTHS_MAP.keys())[list(TR_MONTHS_MAP.values()).index(dt.month)]
    # ay_adı listede "mayis" gibi de olabilir, güzel gösterelim:
    pretty = {
        "ocak":"Ocak","subat":"Şubat","şubat":"Şubat","mart":"Mart","nisan":"Nisan","mayis":"Mayıs","mayıs":"Mayıs",
        "haziran":"Haziran","temmuz":"Temmuz","agustos":"Ağustos","ağustos":"Ağustos","eylul":"Eylül","eylül":"Eylül",
        "ekim":"Ekim","kası m":"Kasım","kasim":"Kasım","kasım":"Kasım","aralik":"Aralık","aralık":"Aralık"
    }.get(norm(ay_adı), ay_adı.capitalize())
    gun = TR_DAY_NAMES[dt.weekday()]
    return f"{dt.day:02d} {pretty} {gun}"

def parse_tr_date(day_s: str, month_s: str, year_s: str = "") -> datetime:
    d = int(day_s)
    m = month_to_num(month_s)
    if m == 0:
        raise ValueError("Ay çözümlenemedi")
    y = int(year_s) if year_s else datetime.utcnow().year
    # yıl eksik ve ay geçmiş/yakın taşmalar olabilir → dtparse fallback kullan
    try:
        return datetime(y, m, d)
    except Exception:
        # dateutil ile şansımızı deneyelim
        return dtparse(f"{d} {month_s} {y}", dayfirst=True)

def parse_date_range_line(text: str):
    """
    '24 Kasım – 01 Aralık', '24 Kasım 2025 - 01 Aralık 2025' gibi satırları yakalar.
    Dönüş: (start_dt, end_dt) veya None
    """
    import re
    t = text.strip()
    # iki uç tarih yakala (ay adları Türkçe)
    # 1) 24 Kasım 2025 – 01 Aralık 2025
    pat_full = re.compile(
        r"(\d{1,2})\s+([A-Za-zÇĞİÖŞÜçğıöşü]+)\s*(\d{4})?\s*[–—\-]\s*(\d{1,2})\s+([A-Za-zÇĞİÖŞÜçğıöşü]+)\s*(\d{4})?",
        re.IGNORECASE
    )
    m = pat_full.search(t)
    if not m:
        return None
    d1, mon1, y1, d2, mon2, y2 = m.groups()
    start = parse_tr_date(d1, mon1, y1 or "")
    end   = parse_tr_date(d2, mon2, y2 or "")
    # yıl/ay taşması küçük düzeltme: bitiş başlangıçtan önceyse +1 yıl dene
    if end < start:
        try:
            end = end.replace(year=end.year + 1)
        except Exception:
            pass
    return start, end

def format_dates_lines_from_list(li_texts: list) -> list:
    """
    <li> metinlerini alır, tarih aralıklarını parse edip
    '24 Kasım Pazartesi – 01 Aralık Pazartesi (7 Gün)' satırları üretir.
    """
    out = []
    for raw in li_texts:
        pr = parse_date_range_line(raw)
        if not pr:
            continue
        start, end = pr
        days = (end - start).days
        # Gün sayısı 0 veya negatifse atla
        if days <= 0:
            continue
        left = tr_format_date(start)
        right = tr_format_date(end)
        out.append(f"{left} – {right} ({days} Gün)")
    # benzersiz & sıralı
    uniq = []
    for line in out:
        if line not in uniq:
            uniq.append(line)
    return uniq

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


def format_message(item, dates_lines, cfg):
    """
    Çıktı biçimi:
    ✈️ İstanbul — Tokyo

    💳 Fiyat: 19.920 TL

    📅 Tarihler:
    24 Kasım Pazartesi – 01 Aralık Pazartesi (7 Gün)
    ...

    🔗 Kaynak: https://...
    """
    # Fiyat
    price_text = (item.get("price_text") or "").strip()
    price_int = item.get("price", 0)
    if price_text:
        disp = price_text if ("TL" in price_text.upper() or "₺" in price_text) else f"{price_text} TL"
    else:
        disp = f"{price_int:,}".replace(",", ".") + " TL" if price_int > 0 else "—"

    origin = item.get("origin") or ""
    destination = item.get("destination") or ""
    if not origin or not destination:
        o2, d2 = infer_route_from_url(item.get("url", ""))
        origin = origin or o2
        destination = destination or d2

    # Tarih satırları
    if dates_lines:
        dates_block = "\n".join(dates_lines)
    else:
        dates_block = "—"

    lines = [
        f"✈️ {origin} — {destination}",
        "",
        f"💳 Fiyat: {disp}",
        "",
        "📅 Tarihler:",
        dates_block,
        "",
        f"🔗 Kaynak: {item.get('url','')}",
    ]
    return "\n".join(lines)


# =========================
#  PLAYWRIGHT SCRAPERS
# =========================
def collect_cards(page):
    """
    Ana sayfada gerçek ilan kartlarını topla.
    Yöntem:
      - /ucak-bileti/ altındaki detay linklerini bul
      - header/nav/footer/menu içindeki linkleri dışla
      - aynı href'e sahip tüm linklerin metinlerinden rota ve fiyatı çıkar
    """
    items = []
    seen_hrefs = set()

    # Linkler DOM'a gelsin
    try:
        page.wait_for_selector('a[href*="/ucak-bileti/"]', timeout=15000)
    except Exception:
        pass

    links = page.locator('a[href*="/ucak-bileti/"]').all()

    for a in links:
        href = a.get_attribute("href") or ""
        if not href:
            continue
        href = urljoin(BASE_URL, href)

        # Kategori/menü kökünü ele (…/ucak-bileti/ tek başına ise)
        if re.search(r"/ucak-bileti/?$", href):
            continue

        # Menü/başlık/altbilgi alanlarındaki linkleri ele
        try:
            is_nav = page.evaluate(
                'el => !!el.closest("header, nav, footer, .site-footer, .elementor-nav-menu, .menu, .widget, aside")',
                a
            )
            if is_nav:
                continue
        except Exception:
            pass

        if href in seen_hrefs:
            continue
        seen_hrefs.add(href)

        # Aynı href'e sahip tüm linklerin metinlerini topla
        group = page.locator(f'a[href="{href}"]').all()
        texts = []
        for g in group:
            try:
                t = clean(g.inner_text())
                if t:
                    texts.append(t)
            except Exception:
                pass

        # Rota adayını bul (ok veya tire içeren)
        route_text = ""
        for t in texts:
            if ("→" in t) or (" - " in t) or ARROW_RE.search(t):
                route_text = t
                break
        origin, destination = extract_route(route_text)

        # Rota metinden çıkmazsa URL'den dene
        if not origin or not destination:
            o2, d2 = infer_route_from_url(href)
            origin = origin or o2
            destination = destination or d2


        # Fiyatı bul
        price_text = ""
        price_int = 0
        for t in texts:
            m = PRICE_RE.search(t)
            if m:
                price_text = clean(m.group(0))
                price_int = parse_price_to_int(price_text)
                break

        # Çok zayıf sinyaller (ne rota ne fiyat) ise ele
        if not origin and not destination and price_int == 0:
            continue

        items.append({
            "id": make_id_from_url(href),
            "url": href,
            "origin": origin,
            "destination": destination,
            "price_text": price_text,
            "price": price_int,
            "posted_text": "",
        })

    return items

def collect_detail_dates(page):
    """
    Detay sayfasında 'Tarih/Tarihler/Uygun Tarihler' başlıklarının hemen altındaki
    <ul><li> maddelerinden tarih aralıklarını al ve biçimlendir.
    Bulamazsak son çare tüm <li> içinde ararız.
    """
    # Önce varsa gizli blokları aç
    expand_content(page)

    # 1) Başlık odaklı: 'Tarih' içeren heading'i bul, sonraki kardeşlerde ul>li ara
    headings = page.locator("h1, h2, h3, h4, h5, h6")
    raw_items = []

    for i in range(headings.count()):
        try:
            h = headings.nth(i)
            txt = clean(h.inner_text())
            if not txt:
                continue
            if any(k in txt.lower() for k in ["tarih", "tarihler", "uygun tarih"]):
                # Heading'in ebeveyni içinde (veya sonrasında) ilk ul>li bloklarını topla
                parent = h.locator("xpath=ancestor::*[self::div or self::section or self::article][1]")
                buckets = [
                    parent.locator("ul li"),
                    h.locator("xpath=following::ul[1]/li"),
                    parent.locator(".elementor-widget-container ul li"),
                ]
                for bucket in buckets:
                    for li in bucket.all():
                        try:
                            t = clean(li.inner_text())
                            if t:
                                raw_items.append(t)
                        except Exception:
                            pass
        except Exception:
            pass

    # 2) Başlık temelli bulamadıysak içerik alanındaki ul>li'ları topla
    if not raw_items:
        roots = [
            "article .entry-content",
            "main .entry-content",
            "article",
            "div.elementor-widget-container",
            ".elementor-section .elementor-container",
        ]
        for root in roots:
            try:
                for li in page.locator(f"{root} ul li").all():
                    try:
                        t = clean(li.inner_text())
                        if t:
                            raw_items.append(t)
                    except Exception:
                        pass
            except Exception:
                pass

    # 3) Metinlerden tarih aralığı satırlarını üret
    formatted = format_dates_lines_from_list(raw_items)
    return formatted[:50]


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
                expand_content(page)
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
