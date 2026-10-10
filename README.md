# Fantasy NHL Tracker

A static site tracking a 6-manager fantasy hockey league: live draft tracking during the
draft, then a season-long dashboard once games start. No build step, no server — plain
HTML/CSS/JS served by GitHub Pages, plus a couple of small Python scripts and a GitHub Action
to keep the stats current.

## Pages

| Page | Purpose |
|---|---|
| `index.html` | Live 2026-27 lineup card — every manager's roster, filled slots, draft log |
| `dashboard.html` | Live 2026-27 season dashboard — standings, a cumulative-points race chart, hot/cold skaters |
| `index-2025.html` | Archive: the completed 2025-26 season's final rosters |
| `dashboard-2025.html` | Archive: the completed 2025-26 season's final dashboard |

`dashboard.html?sample` forces the pre-season sample data even after the real season data exists.

## League format

- 6 managers, 12 roster slots each: 8 Forwards, 3 Defense, 1 Team pick (an NHL team, not a player).
- Skaters score **2 × goals + 1 × assist**.
- The Team pick scores **−1 × real goals against + 10 × shutouts** for that NHL team.
  "Real" excludes empty-net goals — those are shown as a separate, non-scoring EN count.
  A goal still counts fully for the *skater* who scored it, empty-net or not.
- No in-season trades, except a season-ending injury: the injured player's points are
  wiped, and the replacement's *full-season* points count from game one — not just from
  when they were added. This is why the data model stores per-game history rather than
  running point totals: a swap has to rewrite the past, and only per-game data lets it.

## Data layout

```
data/
  2025-2026/
    rosters.json         who owns whom, no draft log (season predates this tracker)
    playerdata.json       per-game skater stats
    teamdata.json          per-game Team-pick stats
  2026-2027/
    rosters.json         who owns whom + full pick-by-pick draft log
    playerdata.json       per-game skater stats (born on the season's first nightly run)
    teamdata.json          per-game Team-pick stats (same)
    sample-playerdata.json  pre-season fallback: last season's stats replayed on this year's rosters
    sample-teamdata.json
```

**`rosters.json`** is the source of truth for who's on which roster. Each skater carries a
verified NHL `playerId`; each team's roster has a `replaced: []` array for the injury rule
above. `draftLog` (2026-2027 only) is an ordered list of `{pick, teamId, name, pos}`.

**`playerdata.json`** holds one array of per-game records per player, keyed by playerId.
Each game record is intentionally broad — goals, assists, shots, PIM, TOI, power-play/
shorthanded/game-winning/OT goals, plus-minus, shifts, home/away, opponent — so a future
dashboard view doesn't require going back and re-fetching history.

**`teamdata.json`** holds one array of per-game records per NHL team abbreviation:
`{id, date, ga, en, so}`. `id` is the NHL game ID (also the incremental-fetch cursor —
see below); `ga` is real goals against, already excluding empty-net goals; `en` is the raw
empty-net-goal count for that game (shown on the site, never scored); `so` is 1 if `ga`
was 0. The 2025-26 archive still uses the older `[date, ga, so]` triple, computed by the
schedule-level (pre-empty-net-exclusion) method — see "Known quirks" below for how that's
reconciled.

Both dashboards' JS fetches `rosters.json` plus the two data files and merges them into the
`{season, asOf, players, teams}` shape that `dashboard-calc.js` expects. That file is pure
data-crunching with no DOM access, shared by both dashboards, and testable directly in Node.

## Setup

`scripts/fetch_stats.py` is standard library only. `scripts/draft.py` needs `click` and
`structlog`:

```fish
python3 -m venv .venv
source .venv/bin/activate.fish
pip install -r requirements.txt
```

## Running the fetch script

```fish
python3 scripts/fetch_stats.py --out-dir data/2026-2027
python3 scripts/fetch_stats.py --season 20252026 --out-dir data/2026-2027 --prefix sample-
python3 scripts/fetch_stats.py --out-dir data/2026-2027 --rebuild   # force a full refetch
```

Standard library only, no dependencies. `--season` defaults to the current season
(2026-27). Team data is fetched incrementally: each completed game's play-by-play is only
requested once, ever — a normal run only fetches whatever's finished since the last one,
by comparing against the game IDs already in the existing `teamdata.json`. `--rebuild`
ignores what's on disk and refetches every game from scratch (use it if a past game's data
ever needs correcting; the NHL amending a "FINAL" boxscore after the fact is rare, but a
previously-recorded game is never automatically re-verified otherwise). Skaters (via the
game-log endpoint) are always a full refetch — one API call returns a player's whole
season regardless, so there's no per-game cost to save, and it means a stat correction is
picked up automatically. The script also refuses to overwrite a file with fewer games than
it already has for any player or team (a defense against a partial/failed fetch silently
erasing history), and writes atomically (temp file + rename) so a crash mid-run can't
corrupt the output.

## Starting a new draft

`scripts/draft.py` is a `draft` CLI group — `init` creates a new line card, and more
subcommands (recording a pick, etc.) will join it as the same tool rather than as separate
scripts, since they all read and write the same `rosters.json`.

```fish
python3 scripts/draft.py init Drew Mike Matt Travis James Mac --season 2027-2028
python3 scripts/draft.py init Drew Mike Matt Travis James Mac --season 27-28   # same result
python3 scripts/draft.py init Drew Mike Matt Travis James Mac --season 2027-2028 --force
```

Writes a blank line card to `data/<start-year>-<start-year + 1>/rosters.json` — one team per
name, in the order given (that's the draft order), each with empty `F`/`D`/`T` rosters and an
empty `draftLog`, ready to fill in as the draft happens. `--season` accepts `2027`, `27-28`, or
`2027-2028`; all three resolve to the same directory. Refuses to overwrite an existing file
unless `--force` is passed, and never touches another season's directory, so it's safe to run
before an old season's data is finalized. `--out-file` and `--data-dir` override the filename
and the root data directory if needed.

Each team's `id` is a plain lowercase slug of the name passed in (`"Drew"` → `"drew"`), not the
`"you"` id the current `index.html`/`dashboard.html` hardcode for the active-tab default and
per-manager colors — those color maps are hand-written for this season's six names and won't
pick up a new draft's names automatically.

## Nightly pipeline

`.github/workflows/nightly-stats.yml` runs the fetch script every night at two offsets
(`9 9 * * *` and `21 9 * * *` UTC — late enough that West Coast games are long finished,
deliberately off the top of the hour, and doubled up as a safety net — see "Known quirks"
below) and commits `data/2026-2027/playerdata.json`/`teamdata.json` plus this README's
timestamp line below. `fetch_stats.py` rewrites that line to the current UTC time at the
end of every real run (not a `--prefix sample-` one), so the commit happens every night
regardless of whether any game data actually changed — a stale timestamp here means the
scheduled trigger never fired, not that there was nothing new to fetch (see "Known
quirks"). It also supports a manual "Run workflow" trigger from the Actions tab, or
`gh workflow run nightly-stats.yml`. Runs on `ubuntu-26.04` (pinned explicitly, ahead of
the `ubuntu-latest` migration, so a future runner-image bump doesn't happen mid-season
without anyone deciding it).

**Last successful script run:** 2026-10-10T15:15:20Z (UTC) -- rewritten by fetch_stats.py at the end of every run, independent of whether any game data changed.

## Testing locally

Pages fetch JSON with `fetch()`, which doesn't work against a `file://` URL — serve the
directory instead:

```fish
python3 -m http.server 8000
```

## Known quirks worth knowing before touching this

- **A scheduled run can simply not fire, with no error anywhere — confirmed twice in one
  day.** On 2026-10-01 the 05:00 EDT (`09:00 UTC`) nightly cron silently didn't run: no
  failed run, no entry in the Actions log at all, confirmed by diffing `asOf` across the
  only two `github-actions[bot]` commits that exist (`git log --author=github-actions`) —
  both came from manual `workflow_dispatch` runs, none from that day's scheduled slot.
  GitHub's own docs flag the top of the hour as the highest-risk time for a scheduled run
  to be delayed or dropped (every repo scheduling at `:00` collides at once), so the cron
  was moved to `9 9 * * *`. As a same-day test, a one-off trigger was also scheduled for
  17:10 UTC — deliberately not on the hour — and *that* didn't fire either (confirmed by
  the Actions run list itself showing no new run at all, not just no new commit). Two
  misses in one day means `schedule:` is unreliable for this repo beyond just the `:00`
  risk, so the workflow now fires at two offsets 12 minutes apart (`9 9 * * *` and
  `21 9 * * *`) — the commit step's "nothing to commit" skip makes a double-fire harmless,
  so this only costs a few extra seconds of runner time most nights in exchange for one
  firing surviving if the other is dropped. If a nightly update ever looks stale again,
  check `git log --author=github-actions -- data/2026-2027/` for a gap in `asOf` before
  assuming the fetch script itself is broken — the fix is `gh workflow run
  nightly-stats.yml` (or the Actions tab's "Run workflow").
- **NHL API is unofficial.** `api-web.nhle.com` has no SLA, no versioning guarantee, and
  its predecessor (`statsapi.web.nhle.com`) was killed without notice in 2023. Keep the
  fetch logic in one place (`scripts/fetch_stats.py`), fail loudly rather than silently,
  and don't hammer it — one polite request at a time is enough for a 6-person league.
- **A team roster mismatch is real, not a glitch.** Verify a player's current team by
  lookup every time rather than assuming an API discrepancy is a data error — players get
  traded in the offseason (this bit us with Brady Tkachuk, who'd moved OTT → FLA).
- **The 2025-26 archive's Team-pick GA is a hybrid.** The per-game numbers behind its
  chart come from the same schedule-level method the 2026-27 season uses (fast, but
  includes empty-net goals). The *season totals* shown in the standings table and on the
  lineup card come instead from that season's own tracking spreadsheet, which excluded
  empty-net goals per that year's rule. `dashboard-2025.html` reconciles the two: it shifts
  each team's cumulative chart line by a constant so the final total is exact, while the
  day-to-day shape is an approximation. This is disclosed in that page's own footer.
- **2026-27's empty-net exclusion needed real per-game data**, since the NHL doesn't expose
  it at the schedule level. Each game's `/gamecenter/{id}/landing` response tags every goal
  with `goalModifier: "empty-net"` or `"none"` — including shootout-winning goals, which
  appear in the same list under a `periodType: "SO"` entry and are never empty-net, so no
  special-casing was needed there. `team_log()` fetches this per completed game, cached
  across teams that happen to share a game, and merges incrementally (see above) rather
  than re-fetching a full season every run.
- **Git is the site owner's, not an assistant's, to drive.** Nothing here should ever be
  committed, pushed, or merged by an automated assistant without the repo owner running
  those commands themselves.
- **The nightly workflow's season is hardcoded, not read from anywhere.**
  `.github/workflows/nightly-stats.yml` hardcodes `data/2026-2027` in three places (the
  `--out-dir` flag and both the `git diff`/`git add` paths) and never looks at `rosters.json`.
  `scripts/draft.py init` can create a new season's directory (e.g. `data/2027-2028/`) at any
  time without affecting the nightly job at all — but when that new season actually goes
  live, those three hardcoded references need a manual bump to point at it, or nightly stats
  keep silently fetching the old season.

## License

[PolyForm Noncommercial License 1.0.0](LICENSE) — free to use, study, and modify for any
noncommercial purpose (personal, hobby, research, education); commercial use requires a
separate license from the copyright holder.
