#!/usr/bin/env python3
"""Fetch per-game stats for every rostered player and Team pick.

Writes two files per season into --out-dir (e.g. data/2026-2027/): playerdata.json and
teamdata.json (or sample-playerdata.json / sample-teamdata.json with --prefix sample-).
Reads rosters.json from --out-dir by default (--rosters to override).

Skaters: one record per regular-season game (goals, assists, shots, PIM, TOI, power-play/
shorthanded/game-winning/OT goals, plus/minus, shifts, home/away, opponent) -- kept broad
so a future dashboard view doesn't need a backfill.

Teams: one record per regular-season game -- {id, date, ga, en, so}. ga is the opponent's
real goals against (EXCLUDING empty-net goals -- these don't count against the Team
pick's score, per league rule), en is the raw empty-net-goal count (shown, not scored),
so is 1 if ga == 0. Sourced from each game's /gamecenter/{id}/landing goal list, which
tags every goal with goalModifier "empty-net" or "none" (shootout-winning goals appear
here too, under a periodType "SO" entry, and are never empty-net -- no special-casing
needed).

Team data is fetched INCREMENTALLY: team_log() reads whatever's already in the existing
output file and only requests landing data for games not already recorded there (matched
by game ID, one schedule call still covers the whole season -- that part's cheap). A full
backfill is ~500 landing requests; a normal nightly run is only the handful of games that
finished since the last one. Pass --rebuild to ignore existing data and refetch everything
from scratch (e.g. if a past game's data needs correcting). A previously recorded game is
never re-verified once written -- if the NHL amends a "FINAL" boxscore after the fact
(rare), --rebuild is how you'd pick that up.

Skaters (player_log) are NOT incremental -- the game-log endpoint returns a player's whole
season in one call regardless, so there's no per-game cost to amortize, and a full refetch
every run means a late correction to a skater's stat line is picked up automatically.

Any failed request aborts the run without touching either output file.
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
    rows = [{
        "date": g["gameDate"],
        "g": g["goals"],
        "a": g["assists"],
        "shots": g.get("shots", 0),
        "pim": g.get("pim", 0),
        "toi": g.get("toi"),
        "ppg": g.get("powerPlayGoals", 0),
        "ppp": g.get("powerPlayPoints", 0),
        "shg": g.get("shorthandedGoals", 0),
        "shp": g.get("shorthandedPoints", 0),
        "gwg": g.get("gameWinningGoals", 0),
        "otg": g.get("otGoals", 0),
        "pm": g.get("plusMinus", 0),
        "shifts": g.get("shifts", 0),
        "home": g.get("homeRoadFlag") == "H",
        "opp": g.get("opponentAbbrev"),
    } for g in (j or {}).get("gameLog", [])]
    return sorted(rows, key=lambda r: r["date"])


_game_goals_cache = {}


def game_goals(game_id):
    """List of {"team": abbrev, "empty_net": bool} for every goal in a game, cached by ID
    since two tracked teams occasionally share a game."""
    if game_id not in _game_goals_cache:
        j = get("/gamecenter/%d/landing" % game_id)
        goals = []
        for period in (j or {}).get("summary", {}).get("scoring", []):
            for g in period.get("goals", []):
                goals.append({
                    "team": g["teamAbbrev"]["default"],
                    "empty_net": g.get("goalModifier") == "empty-net",
                })
        _game_goals_cache[game_id] = goals
    return _game_goals_cache[game_id]


def team_log(abbr, season, existing=None):
    """existing: this team's rows already on disk (from the prior run's output). Games
    whose ID is already present are kept as-is and never re-fetched."""
    known_ids = {r["id"] for r in (existing or [])}
    j = get("/club-schedule-season/%s/%d" % (abbr, season))
    rows = list(existing or [])
    for g in (j or {}).get("games", []):
        if g["gameType"] != 2 or g["gameState"] not in DONE_STATES:
            continue
        if g["id"] in known_ids:
            continue
        opp = g["awayTeam"] if g["homeTeam"]["abbrev"] == abbr else g["homeTeam"]
        opp_goals = [x for x in game_goals(g["id"]) if x["team"] == opp["abbrev"]]
        en = sum(1 for x in opp_goals if x["empty_net"])
        ga = len(opp_goals) - en
        rows.append({"id": g["id"], "date": g["gameDate"], "ga": ga, "en": en, "so": 1 if ga == 0 else 0})
    return sorted(rows, key=lambda r: r["date"])


def write_json(path, obj, old_rows_by_key=None):
    """Write obj (a {"...": ..., <key>: {id: [rows]}} dict) atomically. If old_rows_by_key
    is given, refuse to write when any id's row count shrank versus the prior file."""
    if old_rows_by_key and os.path.exists(path):
        with open(path) as f:
            old = json.load(f)
        key = old_rows_by_key
        for k, rows in obj.get(key, {}).items():
            if len(rows) < len(old.get(key, {}).get(k, [])):
                sys.exit("refusing to write %s: %s %s shrank (%d -> %d rows)"
                         % (path, key, k, len(old[key][k]), len(rows)))
    text = json.dumps(obj, separators=(",", ":"), sort_keys=True) + "\n"
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def load_existing(path, key):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f).get(key, {})


def stamp_readme():
    """Rewrite README's last-run line to now (UTC), so every real run leaves a
    visible trace even when no game data changed -- proves the trigger fired."""
    path = os.path.join(ROOT, "README.md")
    marker = "**Last successful script run:**"
    with open(path) as f:
        lines = f.read().splitlines()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    stamped = marker + " " + ts + " (UTC) -- rewritten by fetch_stats.py at the " \
        "end of every run, independent of whether any game data changed."
    for i, line in enumerate(lines):
        if line.startswith(marker):
            lines[i] = stamped
            break
    else:
        raise RuntimeError("README.md is missing the %r marker line" % marker)
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=20262027)
    ap.add_argument("--out-dir", required=True,
                     help="season directory to write into, e.g. data/2026-2027")
    ap.add_argument("--prefix", default="",
                     help="filename prefix, e.g. 'sample-' for sample-playerdata.json / sample-teamdata.json")
    ap.add_argument("--rosters", help="defaults to <out-dir>/rosters.json")
    ap.add_argument("--rebuild", action="store_true",
                     help="ignore existing team data and refetch every completed game from scratch")
    args = ap.parse_args()
    rosters_path = args.rosters or os.path.join(args.out_dir, "rosters.json")

    with open(rosters_path) as f:
        rosters = json.load(f)
    pids, abbrs = roster_targets(rosters)

    team_path = os.path.join(args.out_dir, args.prefix + "teamdata.json")
    existing_teams = {} if args.rebuild else load_existing(team_path, "teams")

    players = {str(p): player_log(p, args.season) for p in pids}
    teams = {a: team_log(a, args.season, existing=existing_teams.get(a, [])) for a in abbrs}

    dates = [r["date"] for rows in players.values() for r in rows]
    dates += [r["date"] for rows in teams.values() for r in rows]
    as_of = max(dates) if dates else None

    player_path = os.path.join(args.out_dir, args.prefix + "playerdata.json")
    write_json(player_path, {"season": args.season, "asOf": as_of, "players": players}, old_rows_by_key="players")
    write_json(team_path, {"season": args.season, "asOf": as_of, "teams": teams}, old_rows_by_key="teams")
    print("season %d: %d players -> %s, %d teams -> %s, asOf %s"
          % (args.season, len(players), player_path, len(teams), team_path, as_of))

    if not args.prefix:
        stamp_readme()


if __name__ == "__main__":
    main()
