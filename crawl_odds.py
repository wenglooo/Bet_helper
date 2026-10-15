"""Fetch publicly visible pre-match winner odds from the three bookmaker pages.

The crawler uses a real browser because the pages are JavaScript-rendered.
It exports discovered values for review; it does not log in or bypass blocks.
Run: python crawl_odds.py [--sport bundesliga|nfl|all]
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
from datetime import date, datetime
from pathlib import Path

OUT = Path(__file__).resolve().parent / "crawled_odds.csv"
PAGES = {
    "admiral": {
        "bundesliga": "https://www.admiral.at/de/sports/sportwetten/fussball/deutschland/bundesliga",
        "nfl": "https://www.admiral.at/de/sports/sportwetten/us-football",
    },
    "neo.bet": {
        "bundesliga": "https://neo.bet/de/Sportwetten/Fussball/Deutschland/Bundesliga",
        "nfl": "https://neo.bet/de/Sportwetten/American-Football/USA/NFL",
    },
    "tipico": {
        "bundesliga": "https://sports.tipico.at/de/alle/fussball/deutschland/bundesliga",
        "nfl": "https://sports.tipico.at/de/alle/american-football/usa/nfl",
    },
}
ODD = re.compile(r"(?<!\d)([1-9]\d?[,\.]\d{1,3})(?!\d)")
FIXTURE = re.compile(r"([\wÀ-ÿ.'’&() -]{2,50}?)\s+(?:[-–—]|@)\s+([\wÀ-ÿ.'’&() -]{2,50})")
DATE_ISO = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")
DATE_LOCAL = re.compile(r"\b(\d{1,2})\.(\d{1,2})(?:\.(20\d{2}))?\b")


def parse_price(raw: str) -> float | None:
    try:
        price = float(raw.replace(",", "."))
        return price if 1.0 < price < 1000 else None
    except ValueError:
        return None


def parse_match_date(item: dict) -> str | None:
    value = item.get("datetime", "")
    found = DATE_ISO.search(value) or DATE_ISO.search(item.get("text", "")) or DATE_ISO.search(item.get("name", ""))
    if found:
        return found.group(1)
    # Most event cards show a localized day.month date without a year.
    # Infer the nearest plausible season year and reject stale fixtures.
    found = DATE_LOCAL.search(item.get("name", "")) or DATE_LOCAL.search(item.get("text", ""))
    if not found:
        return None
    day, month = int(found.group(1)), int(found.group(2))
    year = int(found.group(3)) if found.group(3) else date.today().year
    try:
        parsed = date(year, month, day)
    except ValueError:
        return None
    if not found.group(3) and parsed < date.today() - __import__("datetime").timedelta(days=3):
        try:
            parsed = date(year + 1, month, day)
        except ValueError:
            return None
    if parsed < date.today() - __import__("datetime").timedelta(days=3) or parsed > date.today() + __import__("datetime").timedelta(days=370):
        return None
    return parsed.isoformat()


async def crawl_page(browser, bookmaker: str, sport: str, url: str):
    page = await browser.new_page(locale="de-AT")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3500)
        # Work from individual visible event links/cards, not arbitrary numbers
        # in the page text (which would mix in unrelated markets and articles).
        records = await page.evaluate("""() => {
          const visible = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
          const odds = s => /^(?:[1-9]\\d?[,\\.]\\d{1,3})$/.test(s.trim());
          const links = [...document.querySelectorAll('a')].filter(visible);
          const out = [];
          for (const a of links) {
            const name = (a.innerText || a.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ');
            if (!name || name.length > 180) continue;
            let box = a;
            for (let i=0; i<7 && box.parentElement; i++) {
              const text = (box.innerText || '').trim().replace(/\\s+/g, ' ');
              const prices = [...box.querySelectorAll('button,[role=button], [class*=odd], [data-testid*=odd]')]
                .filter(visible).map(e => (e.innerText || e.getAttribute('aria-label') || '').trim())
                .filter(odds);
              if (prices.length >= 2 && text.length < 1400) {
                const time = box.querySelector('time[datetime]');
                out.push({name, text, prices: prices.slice(0,3), datetime: time?.getAttribute('datetime') || ''});
                break;
              }
              box = box.parentElement;
            }
          }
          return out;
        }""")
        return records, None
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"
    finally:
        await page.close()


def normalize(records, bookmaker: str, sport: str):
    result = []
    for item in records:
        text = item["text"]
        fixture = FIXTURE.search(item.get("name", "")) or FIXTURE.search(text)
        # Some bookmaker pages put both clubs in a single link without a dash.
        # Do not guess team boundaries when their accessible markup is unclear.
        if not fixture:
            continue
        home, away = fixture.group(1).strip(), fixture.group(2).strip()
        match_date = parse_match_date(item)
        if not match_date:
            continue
        prices = [parse_price(v) for v in item["prices"]]
        prices = [p for p in prices if p is not None]
        needed = 3 if sport == "bundesliga" else 2
        if len(prices) < needed:
            continue
        # NFL event listings commonly write away@home; soccer typically uses home-away.
        if sport == "nfl" and "@" in item.get("name", ""):
            home, away = away, home
        outcomes = [("home", prices[0]), ("draw", prices[1]), ("away", prices[2])] if sport == "bundesliga" else [("home", prices[0]), ("away", prices[1])]
        for outcome, price in outcomes:
            result.append({"sport": sport, "date": match_date, "home": home, "away": away,
                           "bookmaker": bookmaker, "outcome": outcome, "odds": f"{price:.3f}",
                           "source_url": PAGES[bookmaker][sport], "captured_at": datetime.now().astimezone().isoformat(timespec="seconds")})
    return result


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sport", choices=("bundesliga", "nfl", "all"), default="all")
    ap.add_argument("--output", type=Path, default=OUT)
    args = ap.parse_args()
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise SystemExit("Playwright fehlt. Installiere mit: python -m pip install playwright  (danach: python -m playwright install chromium)")
    sports = ("bundesliga", "nfl") if args.sport == "all" else (args.sport,)
    all_rows, errors = [], []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            for bookmaker, pages in PAGES.items():
                for sport in sports:
                    records, error = await crawl_page(browser, bookmaker, sport, pages[sport])
                    rows = normalize(records, bookmaker, sport)
                    all_rows.extend(rows)
                    if error: errors.append(f"{bookmaker} {sport}: {error}")
                    print(f"{bookmaker} / {sport}: {len(rows)} Quotenwerte erkannt")
        finally:
            await browser.close()
    fields = ["sport", "date", "home", "away", "bookmaker", "outcome", "odds", "source_url", "captured_at"]
    with args.output.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"Gespeichert: {args.output} ({len(all_rows)} Werte)")
    for error in errors: print("FEHLER:", error)
    print("Nicht erkannte Karten werden ausgelassen; kontrolliere die Quoten im Browser vor Verwendung.")


if __name__ == "__main__":
    asyncio.run(main())
