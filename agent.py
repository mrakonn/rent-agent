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

# как выглядят ссылки на объявления на каждой площадке
LINK_PATTERNS = {
    "avito.ru": re.compile(r"^https://www\.avito\.ru/sankt-peterburg/[a-z_]+/[^?#]+_(\d+)"),
    "cian.ru": re.compile(r"^https://(?:spb\.)?cian\.ru/rent/flat/(\d+)"),
    "realty.yandex.ru": re.compile(r"^https://realty\.yandex\.ru/offer/(\d+)"),
}

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


CENTRAL = ["центральн", "адмиралтейск", "петроградск", "василеостровск", "невский проспект",
           "гостиный двор", "маяковская", "площадь восстания", "владимирская", "достоевская",
           "пушкинская", "садовая", "сенная", "спасская", "чернышевская", "чкаловская",
           "горьковская", "спортивная", "звенигородская", "технологический институт",
           "лиговский", "площадь александра невского", "театральная", "василеостровская",
           "старая деревня", "крестовский", "кирочная", "литейный", "фонтанка", "моховая"]
NO_PETS = ["без животных", "без питомцев", "животных не", "питомцев не", "нельзя с животными",
           "с животными нельзя", "с животными не", "животные не", "без домашних животных"]
YES_PETS = ["можно с животными", "с животными можно", "с животными разреш", "животные разреш",
            "с питомцами можно", "можно с питомцами", "можно с кошк", "можно с собак",
            "допускается проживание с животными", "животные допускаются", "с животными: можно",
            "с животными — можно"]
NO_COMM = ["без комиссии", "комиссия 0", "комиссия: нет", "комиссия: 0", "комиссия отсутствует",
           "без комисс", "без агент", "от собственника"]


def analyze(text: str) -> dict:
    """Бесплатный разбор по ключевым словам (без нейросети)."""
    t = text.lower()
    head = text[:1500]
    d = {"summary": next((l.strip() for l in text.splitlines() if len(l.strip()) > 10), "")[:120]}

    d["is_rental_long_term"] = not ("посуточно" in t and "длительн" not in t)

    if "студи" in t[:800] or "апарт" in t[:800]:
        d["rooms_type"] = "studio"
    else:
        m = re.search(r"(\d)[\s\-]*(?:к\b|комн|-к\.)", t[:800])
        n = m.group(1) if m else ("1" if "однокомн" in t[:800] else "2" if "двухкомн" in t[:800] else None)
        d["rooms_type"] = {"1": "1k", "2": "2k"}.get(n, "other")

    d["rent"] = None
    for m in re.finditer(r"(\d[\d\s\u00a0\u202f]{3,8})\s*(?:₽|руб|р\.|р/)", head):
        v = int(re.sub(r"\D", "", m.group(1)))
        if 5000 <= v <= 500000:
            d["rent"] = v
            break

    if any(k in t for k in NO_PETS):
        d["pets"] = "no"
    elif any(k in t for k in YES_PETS):
        d["pets"] = "yes"
    else:
        d["pets"] = "unknown"

    if any(k in t for k in NO_COMM):
        d["commission"] = "none"
    elif re.search(r"комисси\w*\W{0,3}[1-9]\d?\s*%", t) or re.search(r"комисси\w*\W{0,3}\d{4,}", t):
        d["commission"] = "yes"
    else:
        d["commission"] = "unknown"
    d["commission_note"] = None

    m = re.search(r"залог[^\n.]{0,40}", t)
    d["deposit"] = m.group(0).strip() if m else None

    d["metro"], d["metro_minutes"] = None, None
    m = re.search(r"(?:м\.|метро)\s*([А-ЯЁ][А-Яа-яё\-]+(?: [А-ЯЁа-яё\-]+)?)([^\n]{0,60})", text)
    if m:
        d["metro"] = m.group(1).strip()
        tail = m.group(2)
        mm = re.search(r"(\d{1,2})\s*мин", tail)
        if mm and "транспорт" not in tail:
            d["metro_minutes"] = int(mm.group(1))

    m = re.search(r"Санкт-Петербург,\s*([^\n]{5,80})", text)
    d["address"] = m.group(1).strip() if m else None

    m = re.search(r"(\d{2,3}(?:[.,]\d)?)\s*м²", text)
    d["area_m2"] = float(m.group(1).replace(",", ".")) if m else None

    d["is_central"] = any(k in t for k in CENTRAL)
    return d


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
    once = os.getenv("RUN_ONCE", "false").lower() == "true"
    if not once:
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
            if once:
                break
            time.sleep(INTERVAL_MIN * 60 + random.uniform(0, 120))


if __name__ == "__main__":
    main()
