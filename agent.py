"""Агент мониторинга аренды в СПб: Авито, Циан, Яндекс Недвижимость -> Telegram."""
import hashlib
import html
import json
import os
import random
import re
import sqlite3
import time
from pathlib import Path

import requests
from anthropic import Anthropic
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv(Path(__file__).parent / "config.env")
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5")
MAX_PRICE = int(os.getenv("MAX_PRICE", "55000"))
MAX_METRO_MIN = int(os.getenv("MAX_METRO_MIN", "15"))
INTERVAL_MIN = int(os.getenv("INTERVAL_MIN", "25"))
MAX_NEW_PER_RUN = int(os.getenv("MAX_NEW_PER_RUN", "25"))
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"

BASE = Path(__file__).parent
DB = sqlite3.connect(BASE / "seen.db")
DB.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, url TEXT)")
DB.execute("CREATE TABLE IF NOT EXISTS sent (fp TEXT PRIMARY KEY)")
client = Anthropic()

# как выглядят ссылки на объявления на каждой площадке
LINK_PATTERNS = {
    "avito.ru": re.compile(r"^https://www\.avito\.ru/sankt-peterburg/[a-z_]+/[^?#]+_(\d+)"),
    "cian.ru": re.compile(r"^https://(?:spb\.)?cian\.ru/rent/flat/(\d+)"),
    "realty.yandex.ru": re.compile(r"^https://realty\.yandex\.ru/offer/(\d+)"),
}

PROMPT = """Ты помогаешь искать квартиру в аренду в Санкт-Петербурге. Ниже текст страницы объявления.
Верни ТОЛЬКО JSON без пояснений:
{{
 "is_rental_long_term": true/false,   // долгосрочная аренда (не посуточно)
 "rooms_type": "studio" | "1k" | "2k" | "other",  // студия/апарт-студия, 1-комн, 2-комн
 "rent": число в рублях в месяц или null,
 "deposit": "текст про залог" или null,
 "commission": "none" | "yes" | "unknown",  // комиссия агенту/собственнику
 "commission_note": "кратко" или null,
 "pets": "yes" | "no" | "unknown",
 "metro": "название станции" или null,
 "metro_minutes": число минут (пешком) или null,
 "address": "улица и дом" или null,
 "area_m2": число или null,
 "is_central": true/false,  // центр СПб: Центральный, Адмиралтейский, Петроградский, Василеостровский р-ны и близкие к ним
 "summary": "1 короткая фраза"
}}
Если данных нет, ставь null или "unknown", ничего не выдумывай.

ТЕКСТ:
{text}"""


def tg(text: str):
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": False},
        timeout=30,
    )
    r.raise_for_status()


def listing_id(url: str):
    for domain, pat in LINK_PATTERNS.items():
        m = pat.match(url)
        if m:
            return f"{domain}:{m.group(1)}"
    return None


def collect_links(page, search_url: str):
    page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)
    for _ in range(4):  # подгрузка ленивого списка
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(800)
    hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
    found = {}
    for h in hrefs:
        lid = listing_id(h)
        if lid:
            found[lid] = h.split("?")[0]
    return found


def page_text(page, url: str) -> str:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)
    return page.inner_text("body")[:7000]


def analyze(text: str) -> dict:
    resp = client.messages.create(
        model=MODEL, max_tokens=600,
        messages=[{"role": "user", "content": PROMPT.format(text=text)}],
    )
    raw = resp.content[0].text
    return json.loads(raw[raw.index("{"): raw.rindex("}") + 1])


def is_match(d: dict) -> bool:
    if not d.get("is_rental_long_term"):
        return False
    if d.get("rooms_type") not in ("studio", "1k", "2k"):
        return False
    if d.get("rent") is None or d["rent"] > MAX_PRICE:
        return False
    if d.get("pets") == "no":
        return False
    if not d.get("is_central"):
        return False
    mm = d.get("metro_minutes")
    if mm is not None and mm > MAX_METRO_MIN:
        return False
    return True


def fingerprint(d: dict):
    if not d.get("address"):
        return None
    key = f"{d['address'].lower()}|{d.get('rooms_type')}|{d.get('area_m2')}"
    return hashlib.md5(key.encode()).hexdigest()


def format_msg(d: dict, url: str) -> str:
    pets = {"yes": "🐾 с животными можно", "unknown": "🐾 животные: не указано"}[d.get("pets", "unknown") if d.get("pets") != "no" else "unknown"]
    comm = {"none": "✅ без комиссии", "yes": "⚠️ комиссия", "unknown": "❓ комиссия не указана"}[d.get("commission", "unknown")]
    metro = d.get("metro") or "метро не указано"
    if d.get("metro_minutes"):
        metro += f", {d['metro_minutes']} мин"
    lines = [
        f"<b>{d['rent']:,} ₽/мес</b> · {d['rooms_type']} · {d.get('area_m2') or '?'} м²".replace(",", " "),
        f"📍 {html.escape(str(d.get('address') or 'адрес не указан'))}",
        f"🚇 {html.escape(metro)}",
        pets,
        f"{comm} {html.escape(d.get('commission_note') or '')}".strip(),
        f"💰 Залог: {html.escape(str(d.get('deposit') or 'не указан'))}",
        html.escape(d.get("summary") or ""),
        url,
    ]
    return "\n".join(l for l in lines if l)


def run_once(ctx):
    urls = [u.strip() for u in (BASE / "urls.txt").read_text(encoding="utf-8").splitlines()
            if u.strip() and not u.startswith("#")]
    page = ctx.new_page()
    new = {}
    for u in urls:
        try:
            for lid, link in collect_links(page, u).items():
                if not DB.execute("SELECT 1 FROM seen WHERE id=?", (lid,)).fetchone():
                    new[lid] = link
        except Exception as e:
            print(f"[!] не удалось открыть {u[:60]}: {e}")
        time.sleep(random.uniform(3, 7))

    print(f"Новых объявлений: {len(new)} (обработаю до {MAX_NEW_PER_RUN})")
    for lid, link in list(new.items())[:MAX_NEW_PER_RUN]:
        try:
            d = analyze(page_text(page, link))
        except Exception as e:
            print(f"[!] ошибка анализа {link}: {e}")
            continue
        DB.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (lid, link))
        DB.commit()
        if is_match(d):
            fp = fingerprint(d)
            if fp and DB.execute("SELECT 1 FROM sent WHERE fp=?", (fp,)).fetchone():
                print(f"дубль на другой площадке: {link}")
                continue
            tg(format_msg(d, link))
            if fp:
                DB.execute("INSERT OR IGNORE INTO sent VALUES (?)", (fp,))
                DB.commit()
            print(f"отправлено: {link}")
        time.sleep(random.uniform(3, 8))
    page.close()


def main():
    tg("🏠 Агент поиска квартир запущен")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(BASE / "browser_profile"), headless=HEADLESS, locale="ru-RU",
            viewport={"width": 1366, "height": 900},
        )
        while True:
            try:
                run_once(ctx)
            except Exception as e:
                print(f"[!] ошибка цикла: {e}")
            time.sleep(INTERVAL_MIN * 60 + random.uniform(0, 120))


if __name__ == "__main__":
    main()
