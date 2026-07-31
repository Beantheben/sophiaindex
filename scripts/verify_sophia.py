#!/usr/bin/env python3
"""
verify_sophia.py — Cross-check sophiaindex.org course data against Sophia's
public course pages, and (optionally) propose a corrected courses.csv.

Empirically verified 2026-07-31:
  * catalog and course pages are fully server-rendered (no JS needed)
  * the requirements sentence appears exactly once per course page
  * lab courses use "Activities ... Touchstones" and "overall course average"
  * robots.txt does not exist (404); no JSON endpoint exists
  * pages also expose "N.N semester credits" and "N partners accept credit
    transfer" server-side

Design rules:
  * Only HIGH-CONFIDENCE parses (explicit requirements sentence matched) may
    modify the proposed CSV. Weak parses are reported for human review only.
  * A sentence that omits an assessment type means zero of that type — but the
    zero is inferred only within the schema family the sentence belongs to
    (standard: challenges/milestones/touchstones; lab: activities/touchstones).
  * Fail loudly: if the catalog yields fewer than MIN_CATALOG courses or too
    many pages fail to parse, exit 2 so CI alarms.

Outputs (relative to --outdir):
  state/live_snapshot.json   raw parse results per course (verifier state)
  reports/report.md          human-readable report (used as PR/issue body)
  reports/discrepancies.csv  machine-readable diff rows
  data/courses.csv           rewritten IN PLACE only with --propose
  data/changelog.json        appended entries for applied corrections (--propose)

Exit codes: 0 = ran fine (findings or not — see GITHUB_OUTPUT 'outcome')
            2 = pipeline broken (catalog empty/short, mass parse failure)

GITHUB_OUTPUT (when env var set):
  outcome = clean | changes | review_only | broken
  n_confident, n_review

Usage:
    pip install requests beautifulsoup4
    python3 scripts/verify_sophia.py --csv data/courses.csv            # report only
    python3 scripts/verify_sophia.py --csv data/courses.csv --propose  # + rewrite CSV
    python3 scripts/verify_sophia.py --csv data/courses.csv --offline  # re-diff snapshot
"""

import argparse
import csv
import datetime
import json
import os
import re
import sys
import time
from difflib import get_close_matches

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing deps. Run:  pip install requests beautifulsoup4")

CATALOG_URL = "https://www.sophia.org/online-courses/all-courses/"
BASE = "https://www.sophia.org"
UA = "sophiaindex-verifier/1.0 (+https://sophiaindex.org; contact sophiaindexinfo@gmail.com)"
DELAY = 1.5          # seconds between requests — be polite
TIMEOUT = 25
MIN_CATALOG = 70     # fewer catalog URLs than this = template change = broken
MAX_ERROR_FRAC = 0.2 # >20% of pages erroring/unparseable = broken

WORDNUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "no": 0, "zero": 0,
}

# "Touchstone Tasks?" MUST precede "Touchstones?" in the alternation: some
# sentences read "4 Touchstone Tasks and 1 Touchstone", and matching the bare
# word first would steal the task count as the touchstone count (real bug —
# it inverted Ancient Greek Philosophers' 2 into a 1 on first run).
COUNT_RE = re.compile(
    r"\b(\d{1,2}|" + "|".join(WORDNUM) + r")\s+"
    r"(Challenges?|Milestones?|Touchstone\s+Tasks?|Touchstones?|Activities|Activity|Lessons?)\b",
    re.IGNORECASE,
)

# "overall score of 70%", "overall course average of 70%" both occur live.
SENTENCE_RE = re.compile(
    r"[^.]*?must complete[^.]*?(?:overall\s+(?:score|course\s+average)|70%)[^.]*\.",
    re.IGNORECASE | re.DOTALL,
)

CREDITS_RE = re.compile(r"(\d+(?:\.\d+)?)\s+semester credits?", re.IGNORECASE)
PARTNERS_RE = re.compile(r"(\d+)\s+partners accept credit transfer", re.IGNORECASE)

FIELD_MAP = {
    "challenge": "challenges", "challenges": "challenges",
    "milestone": "milestones", "milestones": "milestones",
    "touchstone": "touchstones", "touchstones": "touchstones",
    "touchstone task": "touchstone_tasks", "touchstone tasks": "touchstone_tasks",
    "activity": "activities", "activities": "activities",
    "lesson": "lessons", "lessons": "lessons",
}

STANDARD_FIELDS = ("challenges", "milestones", "touchstones")
LAB_FIELDS = ("activities", "lessons")


def norm(s):
    """Normalise a course name for fuzzy matching."""
    s = s.lower().strip()
    s = s.replace("&", "and").replace("’", "'")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\biii\b", "3", s)
    s = re.sub(r"\bii\b", "2", s)
    s = re.sub(r"\bi\b", "1", s)
    return s.strip()


def to_int(tok):
    tok = tok.lower()
    return int(tok) if tok.isdigit() else WORDNUM.get(tok)


def parse_page(html):
    """Extract counts + extras from a course page. Returns a snapshot entry."""
    soup = BeautifulSoup(html, "html.parser")
    text = " ".join(soup.get_text(" ").split())

    m = SENTENCE_RE.search(text)
    scope = m.group(0) if m else None
    if not scope:
        # weak fallback: any sentence naming an assessment count
        for cand in re.split(r"(?<=\.)\s+", text):
            if COUNT_RE.search(cand) and re.search(
                r"challenge|touchstone|milestone|activit|lesson", cand, re.I
            ):
                scope = cand
                break

    counts, confident = {}, bool(m)
    if scope:
        for num, word in COUNT_RE.findall(scope):
            field = FIELD_MAP.get(re.sub(r"\s+", " ", word.lower()))
            val = to_int(num)
            if field and val is not None and field not in counts:
                counts[field] = val

    # "N Touchstone Tasks and M Touchstones": whether tasks count as
    # touchstones is a human convention call — never auto-apply that field
    ambiguous = ["touchstones"] if counts.get("touchstone_tasks") else []

    # Zero-inference: an explicit requirements sentence that omits a type means
    # zero of that type — but only within the schema family the sentence uses.
    schema = None
    if confident:
        if "challenges" in counts or "milestones" in counts:
            schema = "standard"
            for f in STANDARD_FIELDS:
                counts.setdefault(f, 0)
        elif "activities" in counts or "lessons" in counts:
            schema = "lab"
            # labs genuinely have zero challenges/milestones; zero-filling here
            # lets stale pixel-derived values in those columns get caught
            # (blank-vs-0 still counts as agreement downstream)
            for f in STANDARD_FIELDS:
                counts.setdefault(f, 0)
        else:
            schema = "ambiguous"   # touchstones-only sentence: don't infer zeros

    extras = {}
    cm = CREDITS_RE.search(text)
    if cm:
        extras["credits"] = float(cm.group(1))
    pm = PARTNERS_RE.search(text)
    if pm:
        extras["transfer_partners"] = int(pm.group(1))

    return {
        "counts": counts,
        "extras": extras,
        "confident": confident,
        "ambiguous_fields": ambiguous,
        "schema": schema,
        "evidence": (scope or "")[:200],
    }


def scrape_catalog(session):
    """Return {slug: {"url":..., "name":...}} from the public catalog."""
    r = session.get(CATALOG_URL, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    found = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].split("?")[0]
        parts = [p for p in href.strip("/").split("/") if p]
        # want /online-courses/<category>/<course>/ (tolerate /content/sophia/en prefix)
        if "online-courses" not in parts:
            continue
        parts = parts[parts.index("online-courses"):]
        if len(parts) != 3 or parts[1] in ("all-courses", "course-pathways"):
            continue
        slug = parts[2]
        label = " ".join(a.get_text(" ").split())
        if not label or len(label) < 3:
            label = slug.replace("-", " ").title()
        url = href if href.startswith("http") else BASE + "/" + "/".join(parts) + "/"
        found.setdefault(slug, {"url": url, "name": label})
    return found


def fetch_all(limit=0):
    session = requests.Session()
    session.headers["User-Agent"] = UA

    print("scraping catalog…")
    catalog = scrape_catalog(session)
    print(f"catalog URLs found: {len(catalog)}")
    if len(catalog) < MIN_CATALOG:
        return None, (f"Catalog scrape returned only {len(catalog)} courses "
                      f"(expected ~82). Sophia's template likely changed.")
    time.sleep(DELAY)

    live, errors = {}, 0
    slugs = list(catalog)[:limit] if limit else list(catalog)
    for i, slug in enumerate(slugs, 1):
        url = catalog[slug]["url"]
        entry = {"url": url, "name": catalog[slug]["name"]}
        try:
            r = session.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            entry.update(parse_page(r.text))
            flag = "" if entry["confident"] else "  [weak parse]"
            print(f"  [{i:>2}/{len(slugs)}] {slug[:40]:42} {entry['counts']}{flag}")
        except Exception as e:
            entry["error"] = str(e)
            errors += 1
            print(f"  [{i:>2}/{len(slugs)}] {slug[:40]:42} ERROR {e}")
        live[slug] = entry
        time.sleep(DELAY)

    unparsed = sum(1 for e in live.values()
                   if "error" in e or not e.get("counts"))
    if slugs and (unparsed / len(slugs)) > MAX_ERROR_FRAC:
        return live, (f"{unparsed}/{len(slugs)} pages failed to fetch or parse. "
                      f"Sophia's page template likely changed.")
    return live, None


def resolve_col(fieldnames, *candidates):
    lower = {f.lower(): f for f in fieldnames}
    for c in candidates:
        if c in lower:
            return lower[c]
    return None


def diff(local_rows, fieldnames, live):
    """Match CSV rows to live data; return (findings, matches, new, removed)."""
    slug_col = resolve_col(fieldnames, "sophia_slug")
    name_col = resolve_col(fieldnames, "course_name", "name", "course")
    if not name_col:
        sys.exit("CSV has no course_name/name column — can't match rows.")

    cols = {}
    for f in STANDARD_FIELDS + LAB_FIELDS:
        cols[f] = resolve_col(fieldnames, f)
    cols["credits"] = resolve_col(fieldnames, "credits", "semester_credits")
    cols["transfer_partners"] = resolve_col(
        fieldnames, "transfer_partners", "partners", "transfer_partner_count")

    by_norm = {norm(e["name"]): slug for slug, e in live.items()}
    by_norm.update({norm(slug.replace("-", " ")): slug for slug in live})

    findings, matches, removed = [], {}, []
    for idx, row in enumerate(local_rows):
        slug = (row.get(slug_col) or "").strip() if slug_col else ""
        if not slug or slug not in live:
            key = norm(row[name_col])
            slug = by_norm.get(key)
            if not slug:
                hit = get_close_matches(key, list(by_norm), n=1, cutoff=0.86)
                slug = by_norm[hit[0]] if hit else None
        if not slug:
            removed.append(row[name_col])
            continue
        matches[idx] = slug
        entry = live[slug]
        if "error" in entry:
            continue

        live_vals = dict(entry["counts"])
        live_vals.update(entry.get("extras", {}))
        for field, col in cols.items():
            if not col or field not in live_vals:
                continue
            raw = (row.get(col) or "").strip()
            try:
                local_val = float(raw) if raw != "" else None
            except ValueError:
                continue
            live_val = float(live_vals[field])
            # blank local + live zero = agreement (blank means "n/a" in the CSV)
            if local_val is None and live_val == 0:
                continue
            if local_val != live_val:
                findings.append({
                    "row_index": idx,
                    "course_name": row[name_col],
                    "slug": slug,
                    "field": field,
                    "column": col,
                    "csv_value": raw,
                    "sophia_value": live_vals[field],
                    # extras come from their own explicit regexes, so a hit
                    # there is confident even without the requirements sentence
                    "confident": ((bool(entry.get("confident")) and field in entry["counts"])
                                  or field in entry.get("extras", {}))
                                 and field not in entry.get("ambiguous_fields", []),
                    "url": entry["url"],
                    "evidence": entry.get("evidence", ""),
                })

    matched_slugs = set(matches.values())
    new = [s for s in live if s not in matched_slugs]
    return findings, matches, new, removed, slug_col, name_col


def apply_corrections(local_rows, fieldnames, findings, matches, live, slug_col):
    """Rewrite rows in memory. Returns (fieldnames, applied) — confident only."""
    if not slug_col:
        slug_col = "sophia_slug"
        fieldnames = list(fieldnames) + [slug_col]
    applied = []
    for idx, slug in matches.items():
        local_rows[idx].setdefault(slug_col, "")
        if local_rows[idx][slug_col] != slug:
            local_rows[idx][slug_col] = slug
            applied.append({"type": "slug", "row": idx})
    lv_col = resolve_col(fieldnames, "last_verified")
    today = datetime.date.today().isoformat()
    for f in findings:
        if not f["confident"]:
            continue
        row = local_rows[f["row_index"]]
        val = f["sophia_value"]
        row[f["column"]] = str(int(val)) if float(val) == int(val) else str(val)
        if lv_col:
            row[lv_col] = today   # this row was just verified against the page
        applied.append({"type": "count", **{k: f[k] for k in
                        ("course_name", "field", "csv_value", "sophia_value", "url")}})
    return fieldnames, applied


def write_report(path, findings, new, removed, live, n_weak_pages, proposed):
    today = datetime.date.today().isoformat()
    conf = [f for f in findings if f["confident"]]
    weak = [f for f in findings if not f["confident"]]
    lines = [f"## Sophia verification — {today}", ""]
    if not (findings or new or removed or n_weak_pages):
        lines.append("All checked fields agree with Sophia's public pages. ✅")

    if conf:
        verb = "Corrections applied in this PR" if proposed else "High-confidence mismatches"
        lines += [f"### {verb} ({len(conf)})", "",
                  "| Course | Field | CSV | Sophia | Source |", "|---|---|---|---|---|"]
        for f in conf:
            lines.append(f"| {f['course_name']} | {f['field']} | {f['csv_value'] or '—'} "
                         f"| **{f['sophia_value']}** | [page]({f['url']}) |")
        lines += ["", "<details><summary>Evidence sentences</summary>", ""]
        for f in conf:
            lines.append(f"- **{f['course_name']}**: “{f['evidence']}”")
        lines += ["", "</details>", ""]

    if weak:
        lines += [f"### ⚠️ Needs human review — weak parses, NOT auto-applied ({len(weak)})", ""]
        for f in weak:
            lines.append(f"- **{f['course_name']}** `{f['field']}`: CSV={f['csv_value'] or '—'} "
                         f"vs parsed={f['sophia_value']} — [check the page]({f['url']})")
        lines.append("")
    if new:
        lines += [f"### 🆕 On Sophia but not in the CSV ({len(new)})", ""]
        for s in new:
            e = live[s]
            lines.append(f"- [{e.get('name', s)}]({e['url']}) — parsed: {e.get('counts', {})}")
        lines.append("")
    if removed:
        lines += [f"### 🗑️ In the CSV but not found in Sophia's catalog ({len(removed)})", ""]
        lines += [f"- {n}" for n in removed] + [""]
    errs = {s: e for s, e in live.items() if "error" in e}
    if errs:
        lines += [f"### Fetch errors ({len(errs)})", ""]
        lines += [f"- {s}: `{e['error']}`" for s, e in errs.items()] + [""]

    lines += ["---",
              f"_Checked {len(live)} live course pages; "
              f"{n_weak_pages} page(s) lacked an explicit requirements sentence. "
              "Weak parses and catalog membership changes are never auto-applied._"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/courses.csv")
    ap.add_argument("--labs-csv", default="",
                    help="optional second CSV for lab courses (lessons/activities schema)")
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--propose", action="store_true",
                    help="rewrite --csv with confident corrections + slugs")
    ap.add_argument("--offline", action="store_true",
                    help="reuse saved snapshot instead of hitting the network")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    if args.limit and args.propose:
        sys.exit("--limit is for smoke tests; refusing to --propose from a "
                 "partial scrape (unfetched courses would look 'removed').")

    snap_path = os.path.join(args.outdir, "state", "live_snapshot.json")
    report_path = os.path.join(args.outdir, "reports", "report.md")
    disc_path = os.path.join(args.outdir, "reports", "discrepancies.csv")
    changelog_path = os.path.join(args.outdir, "data", "changelog.json")

    hash_path = os.path.join(args.outdir, "state", "report_hash.txt")

    def emit(outcome, n_conf=0, n_review=0, report_changed=True):
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a", encoding="utf-8") as fh:
                fh.write(f"outcome={outcome}\nn_confident={n_conf}\n"
                         f"n_review={n_review}\n"
                         f"report_changed={'true' if report_changed else 'false'}\n")
        print(f"\noutcome={outcome} confident={n_conf} review={n_review} "
              f"report_changed={report_changed}")

    def report_hash_changed():
        """True if the report body (minus the dated header) differs from last
        run's. Lets CI skip re-alarming daily about the same persistent finding.
        The hash file must be committed for this to work across CI runs."""
        import hashlib
        with open(report_path, encoding="utf-8") as fh:
            body = "\n".join(fh.read().splitlines()[1:])
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        prev = None
        if os.path.exists(hash_path):
            with open(hash_path, encoding="utf-8") as fh:
                prev = fh.read().strip()
        with open(hash_path, "w", encoding="utf-8") as fh:
            fh.write(digest + "\n")
        return digest != prev

    datasets = []
    for path in [args.csv] + ([args.labs_csv] if args.labs_csv else []):
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            datasets.append({"path": path,
                             "fieldnames": list(reader.fieldnames or []),
                             "rows": list(reader)})
        print(f"local rows ({path}): {len(datasets[-1]['rows'])}")

    if args.offline:
        with open(snap_path, encoding="utf-8") as fh:
            live = json.load(fh)
        print(f"loaded {len(live)} courses from snapshot")
    else:
        live, fatal = fetch_all(args.limit)
        if live:
            os.makedirs(os.path.dirname(snap_path), exist_ok=True)
            with open(snap_path, "w", encoding="utf-8") as fh:
                json.dump(live, fh, indent=1)
        if fatal:
            os.makedirs(os.path.dirname(report_path), exist_ok=True)
            with open(report_path, "w", encoding="utf-8") as fh:
                fh.write(f"## 🚨 Verification pipeline broken\n\n{fatal}\n")
            emit("broken")
            sys.exit(2)

    findings, removed, all_matched = [], [], set()
    for ds in datasets:
        f, matches, _, rem, slug_col, _ = diff(ds["rows"], ds["fieldnames"], live)
        ds["findings"], ds["matches"], ds["slug_col"] = f, matches, slug_col
        findings += f
        removed += rem
        all_matched |= set(matches.values())
    # "new on Sophia" only counts courses missing from EVERY local file
    new = [s for s in live if s not in all_matched]
    n_weak_pages = sum(1 for e in live.values()
                       if "error" not in e and not e.get("confident"))

    os.makedirs(os.path.dirname(disc_path), exist_ok=True)
    with open(disc_path, "w", newline="", encoding="utf-8") as fh:
        cols = ["course_name", "slug", "field", "csv_value", "sophia_value",
                "confident", "url", "evidence"]
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore",
                           lineterminator="\n")
        w.writeheader()
        w.writerows(findings)

    proposed = False
    if args.propose:
        count_changes = []
        for ds in datasets:
            fieldnames, applied = apply_corrections(
                ds["rows"], ds["fieldnames"], ds["findings"], ds["matches"],
                live, ds["slug_col"])
            if not applied:
                continue
            proposed = True
            with open(ds["path"], "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=fieldnames, lineterminator="\n")
                w.writeheader()
                w.writerows(ds["rows"])
            count_changes += [a for a in applied if a["type"] == "count"]
        if count_changes:
            log = []
            if os.path.exists(changelog_path):
                with open(changelog_path, encoding="utf-8") as fh:
                    log = json.load(fh)
            today = datetime.date.today().isoformat()
            for a in count_changes:
                log.append({"date": today, "course": a["course_name"],
                            "field": a["field"], "old": a["csv_value"],
                            "new": a["sophia_value"], "source": a["url"]})
            os.makedirs(os.path.dirname(changelog_path), exist_ok=True)
            with open(changelog_path, "w", encoding="utf-8") as fh:
                json.dump(log, fh, indent=1)

    write_report(report_path, findings, new, removed, live, n_weak_pages, proposed)

    n_conf = sum(1 for f in findings if f["confident"])
    n_review = (len(findings) - n_conf) + len(new) + len(removed) + n_weak_pages
    changed = report_hash_changed()
    if proposed or (n_conf and not args.propose):
        emit("changes", n_conf, n_review, changed)
    elif n_review:
        emit("review_only", n_conf, n_review, changed)
    else:
        emit("clean", report_changed=changed)

    print(f"report -> {report_path}")


if __name__ == "__main__":
    main()
