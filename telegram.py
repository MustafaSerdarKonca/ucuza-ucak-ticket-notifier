# -*- coding: utf-8 -*-
"""
Telegram yardımcıları
- BOT_TOKEN ve CHAT_ID ortam değişkenlerinden okunur
- Basit POST ile mesaj gönderir (sendMessage)
"""

import os
import json
import time
import random
import logging
import requests

TELEGRAM_API_BASE = "https://api.telegram.org"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

def send_message(text: str, parse_mode: str = None):
    """
    Telegram'a mesaj gönderir.
    Dönüş: (ok: bool, error: str|None)
    """
    if not BOT_TOKEN or not CHAT_ID:
        return False, "TELEGRAM_BOT_TOKEN veya TELEGRAM_CHAT_ID tanımlı değil."

    url = f"{TELEGRAM_API_BASE}/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    # Küçük bir rasgele gecikme (rate-limit ve bot davranışı açısından nazik olmak için)
    time.sleep(random.uniform(0.2, 0.6))

    try:
        resp = requests.post(url, json=payload, timeout=20)
        if resp.status_code != 200:
            logging.warning(f"Telegram status={resp.status_code} body={resp.text[:200]}")
            return False, f"HTTP {resp.status_code}"
        data = resp.json()
        if not data.get("ok"):
            return False, json.dumps(data)
        return True, None
    except requests.RequestException as e:
        return False, str(e)
