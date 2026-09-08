#!/usr/bin/env python3
"""
Genererer ugentlig AI-optakt og -opsamling til Oase NFL Fantasy League.

Kører via GitHub Actions. Henter live data fra Sleeper API, kombinerer med
historisk liga-data udtrukket fra index.html, og beder Gemini skrive to korte
tekster på dansk. Resultatet gemmes som newsletter.json, som hjemmesiden læser.

API-nøglen kommer fra miljøvariablen GEMINI_API_KEY (sat via GitHub Secrets)
og forlader aldrig serveren.
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

LEAGUE_ID = "1387759346238128128"
SLEEPER = "https://api.sleeper.app/v1"
GEMINI_MODEL = "gemini-flash-latest"  # alias: peger altid på nyeste Flash-model
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# Sleeper display_name -> manager (samme mapping som på hjemmesiden)
SLEEPER_TO_MANAGER = {
    "limkilde": "Roal",
    "jonassangild": "Jonas",
    "plebmaster3000": "Tobias",
    "rehhoff": "Thomas",
    "shyllested": "Simon",
    "jesperon": "Jesper",
    "ramsloeg": "Lasse",
    "pavenpape": "Kasper",
    "christian1964reh": "Christian",
    "alexanderkravn": "Alexander",
}

NEW_MANAGERS_2026 = {"Christian", "Alexander"}


def resolve_manager(name):
    if not name:
        return name
    return SLEEPER_TO_MANAGER.get(str(name).lower().strip(), name)


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "oase-fantasy-newsletter"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def load_history(path="index.html"):
    """Udtrækker RAW og PLAYOFFS fra hjemmesidens indlejrede JSON."""
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        return {}, {}

    def extract(varname):
        m = re.search(rf"(?:let|const)\s+{varname}\s*=\s*(\{{.*?\}});\n", content, re.DOTALL)
        if not m:
            return {}
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return {}

    return extract("RAW"), extract("PLAYOFFS")


def build_h2h(raw):
    """Bygger all-time head-to-head rekord (kun regular season)."""
    h2h = {}
    for yr, season in raw.items():
        if not yr.isdigit():
            continue
        max_wk = 15 if int(yr) >= 2021 else 14
        for m in season.get("matchups", []):
            if m.get("week", 99) > max_wk:
                continue
            a, b = m.get("m1"), m.get("m2")
            if not a or not b:
                continue
            h2h.setdefault(a, {}).setdefault(b, [0, 0])
            h2h.setdefault(b, {}).setdefault(a, [0, 0])
            if m.get("winner") == a:
                h2h[a][b][0] += 1
                h2h[b][a][1] += 1
            else:
                h2h[b][a][0] += 1
                h2h[a][b][1] += 1
    return h2h


def title_counts(playoffs):
    counts = {}
    for po in playoffs.values():
        champ = po.get("champion")
        if champ:
            counts[champ] = counts.get(champ, 0) + 1
    return counts


def call_gemini(api_key, prompt):
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.9, "maxOutputTokens": 1200},
    }).encode()
    req = urllib.request.Request(
        GEMINI_URL,
        data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:400]
        raise SystemExit(f"Gemini API fejl {e.code}: {detail}")

    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError):
        raise SystemExit(f"Uventet Gemini-svar: {json.dumps(data)[:400]}")


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY mangler (sæt den som GitHub Secret).")

    league = fetch_json(f"{SLEEPER}/league/{LEAGUE_ID}")
    season = league.get("season")
    status = league.get("status")

    if status == "pre_draft":
        print("Sæsonen er ikke startet endnu — intet at generere.")
        return

    state = fetch_json(f"{SLEEPER}/state/nfl")
    current_week = state.get("week", 1)
    last_played = max(1, current_week - 1)

    rosters = fetch_json(f"{SLEEPER}/league/{LEAGUE_ID}/rosters")
    users = fetch_json(f"{SLEEPER}/league/{LEAGUE_ID}/users")

    user_map = {u["user_id"]: resolve_manager(u.get("display_name")) for u in users}
    roster_mgr = {r["roster_id"]: user_map.get(r.get("owner_id"), f"Roster{r['roster_id']}") for r in rosters}
    roster_team = {r["roster_id"]: (r.get("metadata") or {}).get("team_name", "") for r in rosters}

    standings = []
    for r in rosters:
        s = r.get("settings") or {}
        pf = (s.get("fpts", 0) or 0) + (s.get("fpts_decimal", 0) or 0) / 100
        pa = (s.get("fpts_against", 0) or 0) + (s.get("fpts_against_decimal", 0) or 0) / 100
        standings.append({
            "manager": roster_mgr[r["roster_id"]],
            "team": roster_team.get(r["roster_id"]) or roster_mgr[r["roster_id"]],
            "wins": s.get("wins", 0), "losses": s.get("losses", 0),
            "pf": round(pf, 2), "pa": round(pa, 2),
        })
    standings.sort(key=lambda x: (-x["wins"], -x["pf"]))

    def week_games(wk):
        try:
            data = fetch_json(f"{SLEEPER}/league/{LEAGUE_ID}/matchups/{wk}")
        except Exception:
            return []
        groups = {}
        for e in data or []:
            mid = e.get("matchup_id")
            if mid is None:
                continue
            groups.setdefault(mid, []).append(e)
        out = []
        for pair in groups.values():
            if len(pair) != 2:
                continue
            a, b = pair
            out.append({
                "m1": roster_mgr.get(a["roster_id"], "?"), "s1": a.get("points") or 0,
                "m2": roster_mgr.get(b["roster_id"], "?"), "s2": b.get("points") or 0,
            })
        return out

    last_results = [g for g in week_games(last_played) if g["s1"] or g["s2"]]
    upcoming = week_games(current_week)

    raw, playoffs = load_history()
    h2h = build_h2h(raw)
    titles = title_counts(playoffs)

    # ── Byg kontekst til AI'en ──
    ctx = [f"LIGA: Oase NFL Fantasy League — sæson {season}, {len(rosters)} hold."]
    ctx.append(f"Ligaen har eksisteret siden 2018. Mesterskaber: " +
               ", ".join(f"{m} {c}x" for m, c in sorted(titles.items(), key=lambda x: -x[1])) + ".")
    if NEW_MANAGERS_2026:
        ctx.append("Debutanter i år: " + ", ".join(sorted(NEW_MANAGERS_2026)) + " (aldrig spillet i ligaen før).")

    ctx.append(f"\nSTILLING EFTER UGE {last_played}:")
    for i, s in enumerate(standings, 1):
        ctx.append(f"{i}. {s['manager']} ({s['team']}) {s['wins']}-{s['losses']}, PF {s['pf']}, PA {s['pa']}")

    if last_results:
        ctx.append(f"\nRESULTATER UGE {last_played}:")
        for g in last_results:
            w, l = (g["m1"], g["m2"]) if g["s1"] > g["s2"] else (g["m2"], g["m1"])
            hi, lo = max(g["s1"], g["s2"]), min(g["s1"], g["s2"])
            ctx.append(f"{w} slog {l} {hi:.2f}-{lo:.2f}")

    if upcoming:
        ctx.append(f"\nKOMMENDE KAMPE UGE {current_week} (med all-time indbyrdes rekord siden 2018):")
        for g in upcoming:
            a, b = g["m1"], g["m2"]
            rec = h2h.get(a, {}).get(b)
            hist = f" — indbyrdes {rec[0]}-{rec[1]} historisk" if rec else " — mødes for første gang nogensinde"
            ctx.append(f"{a} mod {b}{hist}")

    context = "\n".join(ctx)

    tone = (
        "Du er kommissær og fast skribent for en dansk fantasy football-liga blandt venner og kolleger. "
        "Skriv på dansk, i en humoristisk og let hånlig tone med kærlig drilleri — som en ven der driller, "
        "ikke som en der mobber. Brug managernes fornavne. Vær konkret og henvis til de faktiske tal. "
        "Ingen overskrifter i markdown, ingen punktopstilling med bindestreger — skriv i flydende afsnit. "
        "Undgå at opfinde spillernavne eller kampe der ikke står i data."
    )

    preview_prompt = f"""{tone}

{context}

Skriv en OPTAKT til uge {current_week} på cirka 150-200 ord. Fremhæv det mest spændende opgør
(brug den indbyrdes historik hvor det er sjovt), nævn hvis en debutant står over for en veteran,
og slut med en kort forudsigelse. Skriv kun selve teksten."""

    recap_prompt = f"""{tone}

{context}

Skriv en OPSAMLING på uge {last_played} på cirka 150-200 ord. Fremhæv ugens bedste præstation,
den mest pinlige, og eventuelle bad beats (høj score der alligevel tabte). Kommentér kort på stillingen.
Skriv kun selve teksten."""

    print("Genererer optakt...")
    preview = call_gemini(api_key, preview_prompt)
    print("Genererer opsamling...")
    recap = call_gemini(api_key, recap_prompt)

    out = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "season": season,
        "previewWeek": current_week,
        "recapWeek": last_played,
        "preview": preview,
        "recap": recap,
    }
    with open("newsletter.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"newsletter.json skrevet ({len(preview)} + {len(recap)} tegn).")


if __name__ == "__main__":
    main()
