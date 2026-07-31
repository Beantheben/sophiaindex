# sophiaindex verification pipeline — runbook

## The decision (Option C-lite)

`data/courses.csv` in the **repo** becomes the source of truth for course facts.
Google Sheets keeps doing what it's good at: collecting Form responses and
serving `ratings_agg` (and `labs`, if you prefer). A daily GitHub Action scrapes
Sophia's public pages, and when a **high-confidence** discrepancy appears it
opens a **pull request** you approve from the GitHub Mobile app. Nothing touches
live data without your tap — the PR *is* the human-review gate.

Why not A/B: auto-writing Sheets (B) has no review gate and needs a stored
credential; alert-only (A) makes every correction a fiddly manual Sheets-app
edit with no diff and no history. C-lite is a one-time ~1-hour migration for a
permanent 4-tap correction flow, and your `snapshot.json` fallback architecture
already proves the site can read data from the repo.

Empirical facts this design rests on (verified 2026-07-31): catalog and course
pages are fully server-rendered; the requirements sentence appears exactly once
per page; labs say "Activities … Touchstones" and "overall course **average**";
there is no JSON endpoint; robots.txt does not exist. Pages also expose
"3.0 semester credits" and "71 partners accept credit transfer" server-side —
the script verifies those too when your CSV has matching columns.

## Repo layout after migration

```
index.html                      # site (one fetch URL changed, see below)
data/courses.csv                # ← SOURCE OF TRUTH (exported from Sheets once)
data/changelog.json             # auto-appended on every applied correction
state/live_snapshot.json        # verifier's last full parse of Sophia
state/heartbeat                 # keeps the scheduled workflow alive
scripts/verify_sophia.py
.github/workflows/verify.yml
reports/                        # generated each run; gitignore if you like
```

## One-time setup (~1 hour, laptop)

1. **Export the courses tab** of your Sheet as CSV → commit as
   `data/courses.csv`. Keep your existing columns; the script matches on
   `course_name` and will add a `sophia_slug` column on its first PR so all
   later runs join exactly, not fuzzily.
2. **Re-plumb the site**: in `index.html`, change the courses fetch URL from the
   Sheets-published CSV to `data/courses.csv` (relative path — same origin on
   GitHub Pages, PapaParse unchanged). Ratings keep loading from Sheets.
   The old `snapshot.json` fallback can stay or be retired.
3. Copy `scripts/`, `.github/workflows/verify.yml` into the repo; commit.
4. Repo **Settings → Actions → General → Workflow permissions**: select
   "Read and write permissions" and check **"Allow GitHub Actions to create and
   approve pull requests"** (the PR step fails without this).
5. **Phone**: install GitHub Mobile, sign in, allow push notifications, and
   Watch the repo (All activity, or Custom → Issues + Pull requests).
6. Optional but recommended: create a free check at healthchecks.io
   (schedule: 1 day, grace: 12 h) and save its ping URL as repo secret
   `HEALTHCHECK_URL`. It emails you if the daily run ever stops happening —
   this covers GitHub's 60-day scheduled-workflow disable AND silent cron drift.
   (The workflow also self-mitigates with a monthly heartbeat commit.)
7. **Mark the Sheet's courses tab read-only for yourself** (rename it
   `courses_LEGACY`). From now on, course facts change only via git.

## First backlog cleanup (the 61 pixel-derived rows)

0. Optional smoke test (any machine with Python, or a codespace):
   `python scripts/verify_sophia.py --csv data/courses.csv --limit 5` — checks
   5 pages, writes a report, refuses to touch the CSV. Ignore the "removed
   courses" section on limited runs; unfetched courses land there.
1. Actions tab → **Verify course data** → *Run workflow* (works from mobile
   web, but do this first one on a laptop).
2. Runtime ≈ 4 min (82 pages × 1.5 s + overhead). The run opens **one PR**
   containing every high-confidence correction, the new `sophia_slug` column,
   `changelog.json` entries, and the parse snapshot. Expect a big diff — this
   is the one PR worth reviewing on a real screen. Spot-check a handful of
   evidence sentences against the linked pages.
3. Anything the parser wasn't sure about (weak parses, unmatched names, the 5
   lab courses if their columns don't line up) lands in a separate **issue**,
   never in the PR. Resolve those by hand-editing `data/courses.csv` in the
   GitHub web editor, citing the course page.
4. Merge. GitHub Pages redeploys automatically; the site is correct ~30 s later.

## What happens each time it fires (steady state)

Daily at ~09:17 UTC the Action rescrapes all 82 pages. Outcomes:

- **All agree** → no PR, no noise. Nothing to do.
- **Confident change** (Sophia edited a course) → PR opens, changelog entry
  included. You get a push notification. See mobile flow below.
- **Weak/structural finding** (new course, retired course, template drift on
  one page) → issue opens or gets a comment. Read it, edit the CSV in the
  GitHub mobile/web editor if needed, close it.
- **Scraper broken** (catalog < 70 courses, >20 % parse failures, any crash)
  → 🚨 issue + failed workflow (GitHub also emails you on scheduled-run
  failure). The site keeps serving the last-good CSV — breakage never
  degrades live data.

## The mobile path, tap by tap

1. 📳 Push notification: *"sophiaindex #47 — Course corrections: 2 field(s)
   differ from Sophia"*. **Tap it.**
2. PR body shows a table — course, field, old → new — with the quoted evidence
   sentence and a link to Sophia's page. *Files changed* shows the exact CSV
   cells. Optionally **tap the course link** to eyeball Sophia's page yourself.
3. **Tap Merge** → **Confirm merge.**
4. Done. Pages redeploys in ~30 s; the changelog entry is already in the merge.

Four taps, no laptop. If a correction looks wrong (parser fooled by a page
change), close the PR instead — nothing has touched live data — and comment on
the auto-opened issue trail so you remember why.

Rejecting one line of a multi-line PR: edit `data/courses.csv` directly on the
PR branch in the GitHub mobile file editor (revert that cell), then merge.

## Guardrails built in

- Only parses backed by the explicit requirements sentence can modify the CSV;
  the zero-inference rule applies only within the schema family the sentence
  uses (so labs never get spurious `challenges = 0` diffs).
- Blank CSV cell vs live 0 counts as agreement — blanks mean "n/a", not drift.
- Same branch (`auto/course-corrections`) is force-updated each run, so you
  never accumulate stacked PRs; the body always reflects the latest scrape and
  refreshes are silent — one push notification per new finding, not per day.
- Review issues are deduped by a committed report hash
  (`state/report_hash.txt`): a persistent weak parse alarms once, not daily.
  Pipeline *breakage* alarms every run on purpose.
- 1.5 s crawl delay, descriptive User-Agent with contact email, daily cadence,
  facts only — no course content is downloaded or stored beyond one ≤200-char
  evidence sentence per course.

## Phase 2 (when you feel like it)

- **Changelog feed on the site**: `data/changelog.json` is already accumulating
  dated entries. Fetch it in `index.html` and render a "recently updated
  courses" box — your repeat-visit hook.
- **ACE National Guide watcher**: a second small scraper on the SOPHIA
  Learning, LLC org page; alert (issue only, no auto-edit) when a new
  sequential ACE ID or a `Version:` bump appears. Highest-signal source for
  *new/revised* courses before Sophia markets them.
- **Degree Forum cross-check**: report-only third opinion. Remember their
  milestone convention differs (final milestone excluded unless it's the only
  one) — map through your `standalone_final` column before diffing, and never
  auto-apply from this source.
