"""
detector.py — deterministic SEO issue detection from a Screaming Frog internal_all.csv.

Implements the FULL rulebook from rulebook.md:
  missing_title, duplicate_title, title_too_long, title_too_short,
  missing_meta_description, duplicate_meta_description, meta_description_too_long,
  missing_h1, duplicate_h1, broken_link, server_error, redirect, redirect_chain,
  missing_image_alt_text, thin_content, orphan_page, non_indexable_but_linked, slow_page

Pre-filters (per rulebook):
  - Only text/html rows for title/meta/H1 checks.
  - Duplicate checks only among indexable pages.
  - thin_content only on indexable HTML pages (not images/JS/CSS).

Standard library only (csv). Detection is plain Python — the model is for
judgment (rewriting titles, choosing redirect targets), not for counting rows.
"""

from __future__ import annotations
import csv
import os
from collections import defaultdict


def load_rows(export_dir: str) -> list[dict]:
    path = os.path.join(export_dir, "internal_all.csv")
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_image_alt_issues(export_dir: str) -> list[str]:
    """
    Load the image missing alt text URLs from the Screaming Frog issue CSV.
    The internal_all.csv does not carry alt text per-image; the dedicated issue
    CSV (images_missing_alt_text.csv) is the authoritative source.
    Falls back to empty list if file is missing.
    """
    path = os.path.join(export_dir, "issues_reports", "images_missing_alt_text.csv")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    return [r.get("Address", "").strip() for r in rows if r.get("Address", "").strip()]


def _int(v, default=0):
    try:
        return int(float(str(v).strip()))
    except Exception:
        return default


def _float(v, default=0.0):
    try:
        return float(str(v).strip())
    except Exception:
        return default


def is_html(r):    return "text/html" in (r.get("Content Type", "") or "").lower()
def is_200(r):     return _int(r.get("Status Code")) == 200
def indexable(r):  return (r.get("Indexability", "") or "").strip().lower() == "indexable"


def detect(rows: list[dict], export_dir: str = None) -> list[dict]:
    """
    Return a list of issue dicts: {type, severity, affected_urls, count, explanation}.
    Implements the full rulebook — all 18 issue types.
    """
    issues = []

    def add(t, sev, urls, explanation):
        urls = sorted(set(u for u in urls if u))
        if urls:
            issues.append({
                "type": t,
                "severity": sev,
                "affected_urls": urls,
                "count": len(urls),
                "explanation": explanation,
            })

    # Pre-computed subsets (per rulebook pre-filters)
    html_rows  = [r for r in rows if is_html(r)]
    idx200     = [r for r in html_rows if is_200(r) and indexable(r)]

    # ------------------------------------------------------------------ #
    # TITLE CHECKS (indexable HTML 200 pages only)
    # ------------------------------------------------------------------ #
    add("missing_title", "High",
        [r["Address"] for r in idx200 if not (r.get("Title 1", "") or "").strip()],
        "Indexable pages with no title tag.")

    # Duplicate titles — only among indexable pages with a non-empty title
    by_title = defaultdict(list)
    for r in idx200:
        t = (r.get("Title 1", "") or "").strip()
        if t:
            by_title[t].append(r["Address"])
    dup_t = [u for urls in by_title.values() if len(urls) > 1 for u in urls]
    add("duplicate_title", "High", dup_t, "Pages sharing an identical title tag.")

    add("title_too_long", "Medium",
        [r["Address"] for r in idx200
         if (r.get("Title 1", "") or "").strip()
         and (_int(r.get("Title 1 Pixel Width")) > 561 or _int(r.get("Title 1 Length")) > 60)],
        "Title pixel width > 561 or character length > 60 — likely truncated in SERPs.")

    add("title_too_short", "Low",
        [r["Address"] for r in idx200
         if 0 < _int(r.get("Title 1 Length")) < 30],
        "Title under 30 characters — too short for optimal SEO.")

    # ------------------------------------------------------------------ #
    # META DESCRIPTION CHECKS (indexable HTML 200 pages only)
    # ------------------------------------------------------------------ #
    add("missing_meta_description", "Medium",
        [r["Address"] for r in idx200 if not (r.get("Meta Description 1", "") or "").strip()],
        "Indexable pages with no meta description.")

    by_meta = defaultdict(list)
    for r in idx200:
        m = (r.get("Meta Description 1", "") or "").strip()
        if m:
            by_meta[m].append(r["Address"])
    dup_m = [u for urls in by_meta.values() if len(urls) > 1 for u in urls]
    add("duplicate_meta_description", "Medium", dup_m, "Pages sharing an identical meta description.")

    add("meta_description_too_long", "Low",
        [r["Address"] for r in idx200
         if (r.get("Meta Description 1", "") or "").strip()
         and _int(r.get("Meta Description 1 Length")) > 155],
        "Meta description over 155 characters — likely truncated in SERPs.")

    # ------------------------------------------------------------------ #
    # H1 CHECKS
    # ------------------------------------------------------------------ #
    # missing_h1: all 200 HTML pages (not only indexable — per rulebook)
    add("missing_h1", "Medium",
        [r["Address"] for r in html_rows if is_200(r) and not (r.get("H1-1", "") or "").strip()],
        "200 pages with no H1 tag.")

    # duplicate_h1: indexable pages only
    by_h1 = defaultdict(list)
    for r in idx200:
        h = (r.get("H1-1", "") or "").strip()
        if h:
            by_h1[h].append(r["Address"])
    dup_h1 = [u for urls in by_h1.values() if len(urls) > 1 for u in urls]
    add("duplicate_h1", "Low", dup_h1, "Pages sharing an identical H1.")

    # ------------------------------------------------------------------ #
    # RESPONSE CODE CHECKS (all rows)
    # ------------------------------------------------------------------ #
    add("broken_link", "High",
        [r["Address"] for r in rows if 400 <= _int(r.get("Status Code")) <= 499],
        "URLs returning a 4xx client error.")

    add("server_error", "High",
        [r["Address"] for r in rows if 500 <= _int(r.get("Status Code")) <= 599],
        "URLs returning a 5xx server error.")

    add("redirect", "Medium",
        [r["Address"] for r in rows if 300 <= _int(r.get("Status Code")) <= 399],
        "URLs that redirect (3xx).")

    # ------------------------------------------------------------------ #
    # REDIRECT CHAIN / LOOP
    # Rulebook: a URL is in a chain when its Redirect URL is ALSO a
    # redirecting URL (i.e. also a key in the redirect map).
    # A loop is a chain that returns to an earlier URL.
    # ------------------------------------------------------------------ #
    redirect_map = {}
    for r in rows:
        code = _int(r.get("Status Code"))
        if 300 <= code <= 399:
            target = (r.get("Redirect URL", "") or "").strip()
            if target:
                redirect_map[r["Address"]] = target

    chain_urls = set()
    for url, target in redirect_map.items():
        # chain: target is itself a redirect
        if target in redirect_map:
            chain_urls.add(url)
            chain_urls.add(target)
        # loop: url redirects to itself
        if target == url:
            chain_urls.add(url)

    add("redirect_chain", "High", list(chain_urls),
        "URLs in a redirect chain (target is also a redirect) or redirect loop.")

    # ------------------------------------------------------------------ #
    # MISSING IMAGE ALT TEXT
    # Source: issues_reports/images_missing_alt_text.csv (the authoritative
    # Screaming Frog issue file). Falls back to empty if not present.
    # ------------------------------------------------------------------ #
    if export_dir:
        alt_urls = load_image_alt_issues(export_dir)
    else:
        # Try to infer export_dir from a common parent if not given
        alt_urls = []
    add("missing_image_alt_text", "Medium", alt_urls,
        "Images with no alt text attribute.")

    # ------------------------------------------------------------------ #
    # CONTENT & PERFORMANCE
    # ------------------------------------------------------------------ #
    # thin_content: indexable HTML pages only (not images, JS, CSS)
    add("thin_content", "Low",
        [r["Address"] for r in html_rows if indexable(r) and _int(r.get("Word Count")) < 200],
        "Indexable HTML pages with fewer than 200 words.")

    # orphan_page: indexable HTML 200 pages with zero inlinks
    add("orphan_page", "Medium",
        [r["Address"] for r in idx200 if _int(r.get("Inlinks")) == 0],
        "Indexable 200 pages with no internal links pointing to them.")

    # non_indexable_but_linked: non-indexable pages that have inlinks
    add("non_indexable_but_linked", "Medium",
        [r["Address"] for r in rows
         if (r.get("Indexability", "") or "").strip().lower() == "non-indexable"
         and _int(r.get("Inlinks")) > 0],
        "Non-indexable pages still linked internally.")

    # slow_page: all rows with response time > 1.0s
    add("slow_page", "Low",
        [r["Address"] for r in rows if _float(r.get("Response Time")) > 1.0],
        "Pages with response time over 1.0 second.")

    return issues


def summarize(issues: list[dict]) -> dict:
    by_sev = defaultdict(int)
    for i in issues:
        by_sev[i["severity"]] += 1
    return {
        "total_issues": len(issues),
        "by_severity": {
            "High":   by_sev["High"],
            "Medium": by_sev["Medium"],
            "Low":    by_sev["Low"],
        },
    }


if __name__ == "__main__":
    import sys, json
    d = sys.argv[1] if len(sys.argv) > 1 else "../sample-export"
    rows = load_rows(d)
    iss = detect(rows, export_dir=d)
    print(f"Loaded {len(rows)} rows, detected {len(iss)} issue types.")
    print(json.dumps(summarize(iss), indent=2))
    for i in iss:
        print(f"  [{i['severity']:<6}] {i['type']:<35} x{i['count']}")
