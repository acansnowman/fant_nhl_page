#!/usr/bin/env python3
"""Fetch per-game stats for every rostered player and Team pick into data/stats.json.

Reads data/rosters.json. Skaters: [date, goals, assists] per regular-season game.
Teams: [date, goals_against, shutout] per regular-season game (GA = opponent's final
score, matching league standings; shutout = opponent scored 0).

Any failed request aborts the run without touching the output file.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api-web.nhle.com/v1"
UA = "fant-nhl-page/1.0 (personal fantasy league tracker)"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DONE_STATES = ("FINAL", "OFF")


def get(path):
    """GET JSON with retries. Returns None on 404 (e.g. no games yet), raises otherwise."""
    for attempt in range(3):
        try:
            req = urllib.request.Request(API + path, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                time.sleep(0.15)
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            err = e
        except (urllib.error.URLError, TimeoutError) as e:
            err = e
        time.sleep(2 ** attempt)
    raise RuntimeError("GET %s failed: %s" % (path, err))


def roster_targets(rosters):
    players, teams = [], []
    for t in rosters["teams"]:
        for pos in ("F", "D"):
            players += [p["playerId"] for p in t["roster"][pos] if p]
        teams += [p["teamAbbrev"] for p in t["roster"]["T"] if p]
    return sorted(set(players)), sorted(set(teams))


def player_log(pid, season):
    j = get("/player/%d/game-log/%d/2" % (pid, season))
    rows = [[g["gameDate"], g["goals"], g["assists"]] for g in (j or {}).get("gameLog", [])]
    return sorted(rows)


def team_log(abbr, season):
    j = get("/club-schedule-season/%s/%d" % (abbr, season))
    rows = []
    for g in (j or {}).get("games", []):
        if g["gameType"] != 2 or g["gameState"] not in DONE_STATES:
            continue
        opp = g["awayTeam"] if g["homeTeam"]["abbrev"] == abbr else g["homeTeam"]
        rows.append([g["gameDate"], opp["score"], 1 if opp["score"] == 0 else 0])
    return sorted(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=20262027)
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "stats.json"))
    ap.add_argument("--rosters", default=os.path.join(ROOT, "data", "rosters.json"))
    args = ap.parse_args()

    with open(args.rosters) as f:
        rosters = json.load(f)
    pids, abbrs = roster_targets(rosters)

    players = {str(p): player_log(p, args.season) for p in pids}
    teams = {a: team_log(a, args.season) for a in abbrs}

    old = {}
    if os.path.exists(args.out):
        with open(args.out) as f:
            old = json.load(f)
    if old.get("season") == args.season:
        for key, new in (("players", players), ("teams", teams)):
            for k, rows in new.items():
                if len(rows) < len(old.get(key, {}).get(k, [])):
                    sys.exit("refusing to write: %s %s shrank (%d -> %d rows)"
                             % (key, k, len(old[key][k]), len(rows)))

    dates = [r[0] for rows in list(players.values()) + list(teams.values()) for r in rows]
    out = {
        "season": args.season,
        "asOf": max(dates) if dates else None,
        "players": players,
        "teams": teams,
    }
    text = json.dumps(out, separators=(",", ":"), sort_keys=True) + "\n"
    tmp = args.out + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, args.out)
    print("season %d: %d players, %d teams, asOf %s, %d bytes"
          % (args.season, len(players), len(teams), out["asOf"], len(text)))


if __name__ == "__main__":
    main()
