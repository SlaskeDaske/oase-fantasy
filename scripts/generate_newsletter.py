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
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

LEAGUE_ID = "1387759346238128128"
SLEEPER = "https://api.sleeper.app/v1"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Modelnavne hos Google skifter ofte (aliaser repointes, versioner udfases).
# I stedet for at hardkode ét navn spørger vi API'et hvad der faktisk findes,
# og vælger den bedste tilgængelige Flash-model. Rækkefølgen er præference.
MODEL_PREFERENCE = [
    "gemini-flash-latest",
    "gemini-3-flash",
    "gemini-2.5-flash",
    "gemini-flash",
]

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


def pick_models(api_key):
    """Returnerer en prioriteret liste af brugbare modeller (til fallback)."""
    req = urllib.request.Request(
        f"{GEMINI_BASE}/models",
        headers={"x-goog-api-key": api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:500]
        raise SystemExit(
            f"Kunne ikke hente modelliste (HTTP {e.code}).\n"
            f"Tjek at GEMINI_API_KEY-secreten indeholder selve nøglen (ikke projekt-ID).\n"
            f"Svar fra Google: {detail}"
        )

    usable = []
    for m in data.get("models", []):
        name = m.get("name", "").replace("models/", "")
        if "generateContent" in (m.get("supportedGenerationMethods") or []):
            usable.append(name)

    if not usable:
        raise SystemExit("Ingen modeller med generateContent tilgængelige for denne nøgle.")

    ordered = [p for p in MODEL_PREFERENCE if p in usable]
    flashes = sorted(m for m in usable
                     if "flash" in m and "image" not in m and "tts" not in m and m not in ordered)
    ordered.extend(flashes)
    if not ordered:
        ordered = usable[:3]

    print(f"Tilgængelige modeller (prioriteret): {', '.join(ordered[:4])}")
    return ordered[:4]


def generate_with_fallback(api_key, models, prompt, label):
    """Prøver hver model i rækkefølge — skifter kun hvis en model er permanent utilgængelig."""
    last = None
    for i, model in enumerate(models):
        try:
            print(f"{label} med {model}...")
            return call_gemini(api_key, model, prompt), model
        except SystemExit as e:
            last = str(e)
            if i < len(models) - 1:
                print(f"  {model} kunne ikke bruges — skifter til næste model.")
                continue
    raise SystemExit(f"Alle modeller fejlede. Sidste fejl: {last}")


def call_gemini(api_key, model, prompt, attempts=5):
    """Kalder Gemini med automatiske genforsøg ved midlertidige fejl (503/429/500)."""
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.9, "maxOutputTokens": 1200},
    }).encode()

    last_error = None
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
            f"{GEMINI_BASE}/models/{model}:generateContent",
            data=body,
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read().decode())
            try:
                parts = data["candidates"][0]["content"]["parts"]
                return "".join(p.get("text", "") for p in parts).strip()
            except (KeyError, IndexError):
                raise SystemExit(f"Uventet Gemini-svar: {json.dumps(data)[:500]}")

        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            last_error = f"HTTP {e.code}: {detail}"
            # 503 = overbelastet, 429 = rate limit, 500 = intern fejl. Alle kan lykkes ved genforsøg.
            if e.code in (429, 500, 503) and attempt < attempts:
                wait = min(2 ** attempt * 5, 90)  # 10s, 20s, 40s, 80s
                print(f"  Forsøg {attempt}/{attempts} fejlede ({e.code}) — venter {wait}s og prøver igen...")
                time.sleep(wait)
                continue
            raise SystemExit(f"Gemini API fejl — {last_error}")

        except urllib.error.URLError as e:
            last_error = str(e)
            if attempt < attempts:
                wait = min(2 ** attempt * 5, 90)
                print(f"  Forsøg {attempt}/{attempts} fejlede (netværk) — venter {wait}s...")
                time.sleep(wait)
                continue
            raise SystemExit(f"Netværksfejl mod Gemini: {last_error}")

    raise SystemExit(f"Gav op efter {attempts} forsøg. Sidste fejl: {last_error}")


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY mangler (sæt den som GitHub Secret).")
    api_key = api_key.strip()
    if api_key.startswith("gen-lang-client") or api_key.startswith("projects/"):
        raise SystemExit(
            "GEMINI_API_KEY ser ud til at indeholde et projekt-ID, ikke en API-nøgle.\n"
            "Hent den rigtige nøgle i Google AI Studio (Projects -> klik på 'x key' -> Copy key)\n"
            "og opdatér GitHub-secreten."
        )

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

    print("Vælger model...")
    models = pick_models(api_key)
    preview, used_model = generate_with_fallback(api_key, models, preview_prompt, "Genererer optakt")
    recap, _ = generate_with_fallback(api_key, models, recap_prompt, "Genererer opsamling")

    out = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "season": season,
        "previewWeek": current_week,
        "recapWeek": last_played,
        "preview": preview,
        "recap": recap,
        "model": used_model,
    }
    with open("newsletter.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"newsletter.json skrevet ({len(preview)} + {len(recap)} tegn).")


if __name__ == "__main__":
    main()
