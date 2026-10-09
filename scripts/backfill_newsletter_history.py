#!/usr/bin/env python3
"""
Engangs-script: genskaber optakt/opsamling for de uger i denne sæson der gik
tabt fordi generate_newsletter.py (før 9/10-2026) overskrev newsletter.json
hver uge i stedet for at gemme historik.

For hver uge bruges KUN data der reelt var kendt på det tidspunkt — stilling
og resultater op til og med ugen før, ikke noget fra efterfølgende uger — så
teksterne ikke "snyder" med viden om hvad der skete bagefter. De bliver ikke
ordret identiske med hvad der faktisk ville være blevet skrevet dengang (AI'en
genererer ikke det samme to gange), men indholdsmæssigt tæt på.

Kør én gang via .github/workflows/backfill-newsletter.yml (Actions-fanen ->
"Genskab tidligere nyhedsbreve" -> Run workflow), eller lokalt med
GEMINI_API_KEY sat i miljøet. Kan trygt køres igen — en uge der allerede findes
i newsletter.json bliver erstattet, ikke duplikeret.
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from generate_newsletter import (  # noqa: E402
    LEAGUE_ID, SLEEPER, fetch_json, resolve_manager, load_history,
    build_h2h, title_counts, pick_models, generate_with_fallback,
    NEW_MANAGERS_2026,
)

TONE = (
    "Du er en tør, analytisk sportsskribent for en dansk fantasy football-liga blandt venner og kolleger. "
    "Skriv på dansk i en nøgtern, næsten kommentator-agtig stil — tænk sportsjournalistik, ikke stand-up. "
    "Et diskret glimt i øjet er velkomment (en tør bemærkning, et underspillet ordvalg), men hold det "
    "meget sparsomt — maks én-to steder i teksten, aldrig mere end det. Ingen overdrevne metaforer, "
    "ingen påtaget dramatik, ingen gentagne vittigheder. Brug managernes fornavne. Vær konkret og henvis "
    "præcist til de faktiske tal og den faktiske historik. Ingen overskrifter i markdown, ingen "
    "punktopstilling — skriv i flydende afsnit. Undgå for enhver pris at opfinde spillernavne, kampe "
    "eller resultater der ikke fremgår eksplicit af data nedenfor."
)


def week_games(league_id, roster_mgr, wk):
    try:
        data = fetch_json(f"{SLEEPER}/league/{league_id}/matchups/{wk}")
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


def standings_through(roster_mgr, roster_team, weeks_played_games):
    """Bygger kumulativ stilling ud fra en liste af uge-resultat-lister (uge 1..N)."""
    stats = {m: {"manager": m, "team": roster_team.get(m, m), "wins": 0, "losses": 0, "pf": 0.0, "pa": 0.0}
             for m in set(roster_mgr.values())}
    for games in weeks_played_games:
        for g in games:
            if not g["s1"] and not g["s2"]:
                continue  # ikke spillet endnu
            m1, m2, s1, s2 = g["m1"], g["m2"], g["s1"], g["s2"]
            if m1 not in stats or m2 not in stats:
                continue
            stats[m1]["pf"] += s1; stats[m1]["pa"] += s2
            stats[m2]["pf"] += s2; stats[m2]["pa"] += s1
            if s1 > s2:
                stats[m1]["wins"] += 1; stats[m2]["losses"] += 1
            else:
                stats[m2]["wins"] += 1; stats[m1]["losses"] += 1
    out = list(stats.values())
    for s in out:
        s["pf"] = round(s["pf"], 2)
        s["pa"] = round(s["pa"], 2)
    out.sort(key=lambda x: (-x["wins"], -x["pf"]))
    return out


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY mangler (sæt den som GitHub Secret).")
    api_key = api_key.strip()

    league = fetch_json(f"{SLEEPER}/league/{LEAGUE_ID}")
    season = league.get("season")
    state = fetch_json(f"{SLEEPER}/state/nfl")
    current_week = state.get("week", 1)
    last_played = current_week - 1

    if last_played < 1:
        print("Ingen tidligere uger spillet endnu i denne sæson — intet at genskabe.", flush=True)
        return

    rosters = fetch_json(f"{SLEEPER}/league/{LEAGUE_ID}/rosters")
    users = fetch_json(f"{SLEEPER}/league/{LEAGUE_ID}/users")
    user_map = {u["user_id"]: resolve_manager(u.get("display_name")) for u in users}
    roster_mgr = {r["roster_id"]: user_map.get(r.get("owner_id"), f"Roster{r['roster_id']}") for r in rosters}
    roster_team = {roster_mgr[r["roster_id"]]: (r.get("metadata") or {}).get("team_name") or roster_mgr[r["roster_id"]]
                   for r in rosters}

    raw, playoffs = load_history()
    h2h = build_h2h(raw)
    titles = title_counts(playoffs)

    print("Vælger model...", flush=True)
    models = pick_models(api_key)

    all_weeks_games = {wk: week_games(LEAGUE_ID, roster_mgr, wk) for wk in range(1, current_week)}

    new_issues = []
    for w in range(1, last_played + 1):
        preview_week = w
        recap_week = w - 1 if w - 1 >= 1 else None
        last_results = [g for g in all_weeks_games.get(recap_week, []) if g["s1"] or g["s2"]] if recap_week else []
        upcoming = all_weeks_games.get(preview_week, [])

        # Stillingen som den så ud lige INDEN uge w blev spillet (kun uger 1..w-1 talt med) —
        # det er det en rigtig kørsel ville have haft adgang til dengang.
        weeks_before = [all_weeks_games[i] for i in range(1, w)]
        standings = standings_through(roster_mgr, roster_team, weeks_before)

        ctx = [f"LIGA: Oase NFL Fantasy League — sæson {season}, {len(rosters)} hold."]
        ctx.append("Ligaen har eksisteret siden 2018. Mesterskaber: " +
                   ", ".join(f"{m} {c}x" for m, c in sorted(titles.items(), key=lambda x: -x[1])) + ".")
        if NEW_MANAGERS_2026:
            ctx.append("Debutanter i år: " + ", ".join(sorted(NEW_MANAGERS_2026)) + " (aldrig spillet i ligaen før).")

        if recap_week and last_results:
            ctx.append(f"\nSTILLING EFTER UGE {recap_week}:")
            for i, s in enumerate(standings, 1):
                ctx.append(f"{i}. {s['manager']} ({s['team']}) {s['wins']}-{s['losses']}, PF {s['pf']}, PA {s['pa']}")
            ctx.append(f"\nRESULTATER UGE {recap_week}:")
            for g in last_results:
                win_m, lose_m = (g["m1"], g["m2"]) if g["s1"] > g["s2"] else (g["m2"], g["m1"])
                hi, lo = max(g["s1"], g["s2"]), min(g["s1"], g["s2"])
                ctx.append(f"{win_m} slog {lose_m} {hi:.2f}-{lo:.2f}")
        else:
            ctx.append("\nSÆSONEN ER ENDNU IKKE GÅET I GANG — ingen kampe er spillet, alle hold står 0-0. "
                        "Der findes ingen stilling eller resultater at referere til endnu.")

        if upcoming:
            ctx.append(f"\nKOMMENDE KAMPE UGE {preview_week} (med all-time indbyrdes rekord siden 2018):")
            for g in upcoming:
                a, b = g["m1"], g["m2"]
                rec = h2h.get(a, {}).get(b)
                hist = f" — indbyrdes {rec[0]}-{rec[1]} historisk" if rec else " — mødes for første gang nogensinde"
                ctx.append(f"{a} mod {b}{hist}")

        context = "\n".join(ctx)

        preview_prompt = f"""{TONE}

{context}

Skriv en OPTAKT til uge {preview_week} på cirka 150-200 ord. Fremhæv det mest interessante opgør
(brug den indbyrdes historik hvor det er relevant), nævn hvis en debutant står over for en veteran,
og slut med en kort, analytisk vurdering. Skriv kun selve teksten."""

        print(f"Genererer optakt til uge {preview_week}...", flush=True)
        preview, used_model = generate_with_fallback(api_key, models, preview_prompt, f"  Uge {preview_week} optakt")

        if recap_week and last_results:
            recap_prompt = f"""{TONE}

{context}

Skriv en OPSAMLING på uge {recap_week} på cirka 150-200 ord. Fremhæv ugens bedste præstation,
den svageste indsats, og eventuelle bad beats (høj score der alligevel tabte). Kommentér kort på stillingen.
Skriv kun selve teksten."""
            recap, _ = generate_with_fallback(api_key, models, recap_prompt, f"  Uge {recap_week} opsamling")
        else:
            recap = ("Sæsonen er lige gået i gang, og der er endnu ikke spillet nogen kampe. "
                     "Den første opsamling kommer, så snart uge 1 er overstået.")

        new_issues.append({
            "generated": datetime.now(timezone.utc).isoformat(),
            "season": season,
            "previewWeek": preview_week,
            "recapWeek": recap_week,
            "preview": preview,
            "recap": recap,
            "model": used_model,
            "backfilled": True,
        })

    try:
        with open("newsletter.json", "r", encoding="utf-8") as f:
            existing = json.load(f)
        issues = existing.get("issues", [existing] if "preview" in existing else [])
    except (FileNotFoundError, json.JSONDecodeError):
        issues = []

    for issue in new_issues:
        issues = [i for i in issues
                  if not (i.get("season") == issue["season"] and i.get("previewWeek") == issue["previewWeek"])]
        issues.append(issue)

    issues.sort(key=lambda i: (str(i.get("season")), i.get("previewWeek") or 0), reverse=True)
    issues = issues[:30]

    with open("newsletter.json", "w", encoding="utf-8") as f:
        json.dump({"issues": issues}, f, ensure_ascii=False, indent=2)
    print(f"Færdig — {len(new_issues)} genskabte udgave(r) tilføjet, {len(issues)} udgaver i alt.", flush=True)


if __name__ == "__main__":
    main()
