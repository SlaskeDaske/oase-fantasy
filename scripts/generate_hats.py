#!/usr/bin/env python3
"""
Genererer hats.json til Oase NFL Fantasy League.

Begreber (sæson 2026 og frem):
  Hat    = en manager har mindst én starter, der er Out/IR/inaktiv inden kampstart
  Turban = en manager møder en modstander, der er en Hat
  Kyse   = en manager taber mod en modstander, der er en Hat

Kilder:
  - Sleeper: hvem der startede hver uge (matchups/{uge}.starters) + aktuel skadesstatus
  - nflverse roster_weekly: status pr. spiller pr. uge. INA = inaktiv til kampen,
    RES = reserve/IR. Valideret mod faktiske stats: 0 % af INA/RES-spillere spillede.
  - nflverse injuries: officiel Out/Doubtful-rapport (bruges til live-advarslen
    inden inaktiv-listen er udkommet)

Output (hats.json):
  weeks: færdigspillede uger, hver med kampe og de Hat-spillere der blev startet
  live:  indeværende uge — hvilke starters der lige nu ser ud til at score 0

Kører via GitHub Actions (.github/workflows/hats.yml). Uger der er færdigspillede
og allerede gemt bliver ikke genberegnet, undtagen den seneste (hvor nflverse-data
kan have været ufuldstændig første gang).
"""

import csv
import io
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from generate_newsletter import LEAGUE_ID, SLEEPER, fetch_json, resolve_manager  # noqa: E402

NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download"
OUT_FILE = "hats.json"

# nflverse roster_weekly: INA = inaktiv til kampen, RES = reserve/IR
INACTIVE_ROSTER_STATUSES = {"INA", "RES"}
# Sleeper-statusser der betyder "scorer reelt 0" (bruges kun til live-advarslen)
SLEEPER_ZERO_STATUSES = {"Out", "IR", "PUP", "Sus", "COV", "NA", "DNR"}
SLEEPER_DOUBTFUL = {"Doubtful"}


def fetch_csv(url):
    """Henter en CSV fra nflverse. Returnerer [] hvis filen ikke findes endnu."""
    req = urllib.request.Request(url, headers={"User-Agent": "oase-fantasy-hats"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            text = r.read().decode("utf-8", errors="replace")
    except Exception as e:  # 404 tidligt på sæsonen, netværksfejl, osv.
        print(f"  Kunne ikke hente {url}: {e}", flush=True)
        return []
    return list(csv.DictReader(io.StringIO(text)))


def build_lookup(players):
    """gsis_id -> sleeper_id, til spillere uden sleeper_id i nflverse."""
    out = {}
    for sid, p in players.items():
        gsis = p.get("gsis_id")
        if gsis:
            out[gsis] = sid
    return out


def inactive_by_week(roster_rows, injury_rows, gsis_to_sleeper):
    """week -> {sleeper_id: status}. Status er INA, RES eller OUT."""
    result = {}

    def add(week, sid, status):
        if sid:
            result.setdefault(week, {}).setdefault(sid, status)

    for r in roster_rows:
        if r.get("status") not in INACTIVE_ROSTER_STATUSES or not r.get("week"):
            continue
        sid = r.get("sleeper_id") or gsis_to_sleeper.get(r.get("gsis_id", ""))
        add(int(r["week"]), sid, r["status"])

    for r in injury_rows:
        if r.get("report_status") == "Out" and r.get("week"):
            sid = gsis_to_sleeper.get(r.get("gsis_id", ""))
            add(int(r["week"]), sid, "OUT")
    return result


def doubtful_by_week(injury_rows, gsis_to_sleeper):
    result = {}
    for r in injury_rows:
        if r.get("report_status") == "Doubtful" and r.get("week"):
            sid = gsis_to_sleeper.get(r.get("gsis_id", ""))
            if sid:
                result.setdefault(int(r["week"]), set()).add(sid)
    return result


def player_info(players, sid):
    p = players.get(sid) or {}
    name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or sid
    return {"player": name, "pos": p.get("position") or "?", "nfl": p.get("team") or ""}


def load_week(week, roster_mgr):
    """Returnerer (kampe, {manager: (starters, players_points)}) for en uge."""
    try:
        data = fetch_json(f"{SLEEPER}/league/{LEAGUE_ID}/matchups/{week}")
    except Exception as e:
        print(f"  Uge {week}: kunne ikke hente matchups ({e})", flush=True)
        return [], {}
    groups, lineups = {}, {}
    for e in data or []:
        mgr = roster_mgr.get(e.get("roster_id"), "?")
        lineups[mgr] = (e.get("starters") or [], e.get("players_points") or {}, e.get("points") or 0)
        if e.get("matchup_id") is not None:
            groups.setdefault(e["matchup_id"], []).append((mgr, e.get("points") or 0))
    games = []
    for pair in groups.values():
        if len(pair) == 2:
            (m1, s1), (m2, s2) = pair
            games.append({"m1": m1, "s1": round(s1, 2), "m2": m2, "s2": round(s2, 2)})
    return games, lineups


def hats_for_week(week, lineups, inactive, players):
    hats = []
    flagged = inactive.get(week, {})
    for mgr, (starters, pts, _total) in lineups.items():
        for sid in starters:
            if sid in flagged:
                hats.append({
                    "manager": mgr, **player_info(players, sid),
                    "status": flagged[sid], "points": round(pts.get(sid, 0) or 0, 2),
                })
    return hats


def live_flags(week, lineups, inactive, doubtful, players):
    """Starters i indeværende uge der ser ud til at score 0 (eller er tvivlsomme)."""
    flagged, doubt = [], []
    inactive_now = inactive.get(week, {})
    doubtful_now = doubtful.get(week, set())
    for mgr, (starters, _pts, _total) in lineups.items():
        for sid in starters:
            if not sid or sid == "0":
                continue
            p = players.get(sid) or {}
            sleeper_status = p.get("injury_status")
            if sid in inactive_now:
                flagged.append({"manager": mgr, **player_info(players, sid), "status": inactive_now[sid]})
            elif sleeper_status in SLEEPER_ZERO_STATUSES:
                flagged.append({"manager": mgr, **player_info(players, sid), "status": sleeper_status})
            elif sid in doubtful_now or sleeper_status in SLEEPER_DOUBTFUL:
                doubt.append({"manager": mgr, **player_info(players, sid), "status": "Doubtful"})
    return flagged, doubt


def main():
    league = fetch_json(f"{SLEEPER}/league/{LEAGUE_ID}")
    season = league.get("season")
    if league.get("status") == "pre_draft":
        print("Sæsonen er ikke startet — intet at generere.", flush=True)
        return

    state = fetch_json(f"{SLEEPER}/state/nfl")
    current_week = state.get("week", 1)
    last_played = current_week - 1

    rosters = fetch_json(f"{SLEEPER}/league/{LEAGUE_ID}/rosters")
    users = fetch_json(f"{SLEEPER}/league/{LEAGUE_ID}/users")
    user_map = {u["user_id"]: resolve_manager(u.get("display_name")) for u in users}
    roster_mgr = {r["roster_id"]: user_map.get(r.get("owner_id"), f"Roster{r['roster_id']}") for r in rosters}

    print("Henter spillerdatabase fra Sleeper...", flush=True)
    players = fetch_json(f"{SLEEPER}/players/nfl")
    gsis_to_sleeper = build_lookup(players)

    print("Henter nflverse-data...", flush=True)
    roster_rows = fetch_csv(f"{NFLVERSE}/weekly_rosters/roster_weekly_{season}.csv")
    injury_rows = fetch_csv(f"{NFLVERSE}/injuries/injuries_{season}.csv")
    inactive = inactive_by_week(roster_rows, injury_rows, gsis_to_sleeper)
    doubtful = doubtful_by_week(injury_rows, gsis_to_sleeper)

    existing = {}
    try:
        with open(OUT_FILE, "r", encoding="utf-8") as f:
            old = json.load(f)
        if str(old.get("season")) == str(season):
            existing = old.get("weeks", {})
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    weeks = {}
    for wk in range(1, last_played + 1):
        key = str(wk)
        # Færdige uger der allerede er gemt, røres ikke — undtagen den seneste.
        if key in existing and wk < last_played:
            weeks[key] = existing[key]
            continue
        if wk not in inactive and key in existing:
            weeks[key] = existing[key]  # nflverse mangler data; behold det vi har
            continue
        games, lineups = load_week(wk, roster_mgr)
        if not games:
            continue
        weeks[key] = {"games": games, "hats": hats_for_week(wk, lineups, inactive, players)}
        print(f"  Uge {wk}: {len(weeks[key]['hats'])} Hat-spiller(e) i {len(games)} kampe", flush=True)

    live = None
    if state.get("season_type", "regular") == "regular" and 1 <= current_week <= 18:
        _games, lineups = load_week(current_week, roster_mgr)
        if lineups:
            flagged, doubt = live_flags(current_week, lineups, inactive, doubtful, players)
            live = {"week": current_week, "flagged": flagged, "doubtful": doubt}
            print(f"  Live uge {current_week}: {len(flagged)} flagget, {len(doubt)} doubtful", flush=True)

    out = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "season": season,
        "weeks": weeks,
        "live": live,
    }
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"{OUT_FILE} skrevet: {len(weeks)} uger.", flush=True)


if __name__ == "__main__":
    main()
