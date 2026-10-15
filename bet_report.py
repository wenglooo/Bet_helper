"""Create a transparent 1X2 (Bundesliga) / winner (NFL) value report.

Data inputs:
* Bundesliga: football-data.org v4 (free token required).
* NFL: nflverse public schedules CSV (no key).
* Odds: odds.csv, manually exported/entered from bookmaker pages.

This is an educational statistical baseline, not betting advice.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ODDS_FILE = ROOT / "odds.csv"
REPORT_FILE = ROOT / "report.md"
FD_API = "https://api.football-data.org/v4"
NFL_CSV = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"
NFL_NAMES = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens", "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers", "CHI": "Chicago Bears", "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns",
    "DAL": "Dallas Cowboys", "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars", "KC": "Kansas City Chiefs",
    "LAC": "Los Angeles Chargers", "LAR": "Los Angeles Rams", "LV": "Las Vegas Raiders", "MIA": "Miami Dolphins",
    "MIN": "Minnesota Vikings", "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers", "SEA": "Seattle Seahawks",
    "SF": "San Francisco 49ers", "TB": "Tampa Bay Buccaneers", "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
    "LA": "Los Angeles Rams", "SD": "Los Angeles Chargers", "OAK": "Las Vegas Raiders", "STL": "Los Angeles Rams", "WSH": "Washington Commanders",
}


def get_json(url: str, headers: dict[str, str] | None = None):
    req = urllib.request.Request(url, headers={"User-Agent": "bet-helper/0.1", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def get_csv(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "bet-helper/0.1"})
    with urllib.request.urlopen(req, timeout=45) as response:
        return list(csv.DictReader(line.decode("utf-8-sig") for line in response))


def poisson(lam: float, k: int) -> float:
    return math.exp(-lam) * lam**k / math.factorial(k)


def bundesliga(token: str, season: int | None, lookback: int, days: int):
    today = date.today()
    matches = get_json(f"{FD_API}/competitions/BL1/matches?season={season or today.year}", {"X-Auth-Token": token})["matches"]
    played = [m for m in matches if m.get("status") == "FINISHED" and m.get("score", {}).get("fullTime", {}).get("home") is not None]
    upcoming = [m for m in matches if m.get("status") in ("SCHEDULED", "TIMED") and today <= date.fromisoformat(m["utcDate"][:10]) <= today + timedelta(days=days)]
    if not played:
        raise RuntimeError("Keine abgeschlossenen Bundesliga-Spiele für die Saison erhalten.")
    league_home = sum(m["score"]["fullTime"]["home"] for m in played) / len(played)
    league_away = sum(m["score"]["fullTime"]["away"] for m in played) / len(played)
    by_team = defaultdict(list)
    for m in played:
        h, a = m["homeTeam"]["name"], m["awayTeam"]["name"]
        hg, ag = m["score"]["fullTime"]["home"], m["score"]["fullTime"]["away"]
        by_team[h].append((hg, ag, True))
        by_team[a].append((ag, hg, False))

    forecasts = []
    for m in upcoming:
        home, away = m["homeTeam"]["name"], m["awayTeam"]["name"]
        hf, af = by_team[home][-lookback:], by_team[away][-lookback:]
        if not hf or not af:
            continue
        h_att = (sum(x[0] for x in hf) / len(hf)) / league_home
        h_def = (sum(x[1] for x in hf) / len(hf)) / league_away
        a_att = (sum(x[0] for x in af) / len(af)) / league_away
        a_def = (sum(x[1] for x in af) / len(af)) / league_home
        lam_h = max(.15, league_home * h_att * a_def)
        lam_a = max(.15, league_away * a_att * h_def)
        p = [0.0, 0.0, 0.0]
        for x in range(10):
            for y in range(10):
                q = poisson(lam_h, x) * poisson(lam_a, y)
                p[0 if x > y else 1 if x == y else 2] += q
        total = sum(p)
        forecasts.append({"date": m["utcDate"][:10], "home": home, "away": away,
                          "prob": dict(zip(("home", "draw", "away"), (v / total for v in p))),
                          "market": "1X2"})
    return forecasts


def nfl(lookback: int, days: int):
    games = get_csv(NFL_CSV)
    today = date.today()
    def val(g, key):
        try: return float(g[key])
        except (ValueError, TypeError, KeyError): return None
    played = [g for g in games if val(g, "home_score") is not None and val(g, "away_score") is not None]
    upcoming = [g for g in games if val(g, "home_score") is None and g.get("game_type") in ("REG", "POST") and g.get("gameday") and today <= date.fromisoformat(g["gameday"]) <= today + timedelta(days=days)]
    by_team = defaultdict(list)
    for g in played:
        hs, aws = val(g, "home_score"), val(g, "away_score")
        by_team[g["home_team"]].append((hs, aws, True))
        by_team[g["away_team"]].append((aws, hs, False))
    forecasts = []
    for g in upcoming:
        home_code, away_code = g["home_team"], g["away_team"]
        hf, af = by_team[home_code][-lookback:], by_team[away_code][-lookback:]
        if len(hf) < 3 or len(af) < 3: continue
        home, away = NFL_NAMES.get(home_code, home_code), NFL_NAMES.get(away_code, away_code)
        h_for, h_against = sum(x[0] for x in hf) / len(hf), sum(x[1] for x in hf) / len(hf)
        a_for, a_against = sum(x[0] for x in af) / len(af), sum(x[1] for x in af) / len(af)
        # Expected score margin combines recent scoring and opponent defense.
        margin = ((h_for - a_against) + (h_against - a_for)) / 2 + 1.5
        # NFL score margin has a broad, roughly normal distribution; sd=13 is a baseline.
        p_home = 0.5 * (1 + math.erf(margin / (13 * math.sqrt(2))))
        forecasts.append({"date": g["gameday"], "home": home, "away": away,
                          "prob": {"home": p_home, "away": 1 - p_home}, "market": "Winner"})
    return forecasts


def load_odds(path: Path):
    result = defaultdict(list)
    if not path.exists(): return result
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            try:
                result[(row["sport"].strip().lower(), row["date"].strip(), row["home"].strip().lower(), row["away"].strip().lower())].append(row)
            except KeyError as exc:
                raise RuntimeError(f"Spalte fehlt in {path.name}: {exc}") from exc
    return result


def make_report(forecasts, odds, output: Path):
    lines = ["# Fußball- und NFL-Quotenbericht", "", f"Erstellt: {datetime.now().astimezone().isoformat(timespec='minutes')}",
             "", "> Modellschätzungen sind unsicher und dienen nur zur Information. Ein rechnerischer Vorteil garantiert keinen Gewinn.", ""]
    value_count = 0
    for f in forecasts:
        sport = "bundesliga" if f["market"] == "1X2" else "nfl"
        key = (sport, f["date"], f["home"].lower(), f["away"].lower())
        lines += [f"## {sport.upper()} · {f['date']} · {f['home']} – {f['away']}", "", "Modell: " + " | ".join(f"{k}: {v:.1%}" for k, v in f["prob"].items()), ""]
        rows = odds.get(key, [])
        if not rows:
            lines += ["Keine passende Quote in `odds.csv` gefunden.", ""]
            continue
        lines += ["| Anbieter | Auswahl | Quote | Implizite Wahrscheinlichkeit | Modell | Erwartungswert* |", "|---|---:|---:|---:|---:|---:|"]
        for row in rows:
            outcome = row["outcome"].strip().lower()
            try: price = float(row["odds"])
            except (ValueError, KeyError): continue
            if price <= 1: continue
            if outcome in ("1", "home"): outcome = "home"
            elif outcome in ("x", "draw"): outcome = "draw"
            elif outcome in ("2", "away"): outcome = "away"
            p = f["prob"].get(outcome)
            if p is None: continue
            ev = p * price - 1
            if ev > 0: value_count += 1
            lines.append(f"| {row.get('bookmaker','?')} | {outcome} | {price:.2f} | {1/price:.1%} | {p:.1%} | {ev:+.1%} |")
        lines += ["", "* Erwartungswert pro eingesetzter Einheit: Modellwahrscheinlichkeit × Dezimalquote − 1. Ohne Berücksichtigung von Limits, Steuern oder Modellfehlern.", ""]
    if not forecasts: lines += ["Keine Spiele im gewählten Zeitraum gefunden. Prüfe Liga, Saison und Datenquelle.", ""]
    output.write_text("\n".join(lines), encoding="utf-8")
    print(f"Bericht: {output} · {len(forecasts)} Spiele · {value_count} rechnerische Value-Kandidaten")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sport", choices=("bundesliga", "nfl", "all"), default="all")
    ap.add_argument("--days", type=int, default=14, help="Vorschauzeitraum in Tagen")
    ap.add_argument("--lookback", type=int, default=8, help="Letzte Spiele je Team")
    ap.add_argument("--season", type=int, help="Bundesliga-Saisonstartjahr, z.B. 2026")
    ap.add_argument("--odds", type=Path, default=ODDS_FILE)
    ap.add_argument("--output", type=Path, default=REPORT_FILE)
    args = ap.parse_args()
    forecasts = []
    try:
        if args.sport in ("bundesliga", "all"):
            token = os.getenv("FOOTBALL_DATA_TOKEN")
            if token: forecasts += bundesliga(token, args.season, args.lookback, args.days)
            else: print("Bundesliga übersprungen: FOOTBALL_DATA_TOKEN fehlt.", file=sys.stderr)
        if args.sport in ("nfl", "all"):
            forecasts += nfl(args.lookback, args.days)
        make_report(forecasts, load_odds(args.odds), args.output)
    except (urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Fehler beim Datenabruf: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__": main()
