"""
fixer.py — model-driven SEO fixes (titles, metas, redirects).
Uses local Ollama API for generation and difflib for candidate search.

Returns accurate model_calls count for run_meta tracking.
"""
from __future__ import annotations
import json, os
import urllib.request
from difflib import SequenceMatcher

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = os.environ.get("RADAR_MODEL", "qwen3.5:9b")

_call_count = 0


def call_ollama(prompt: str) -> str:
    """Simple wrapper for Ollama generate API. Increments global call counter."""
    global _call_count
    try:
        data = json.dumps({
            "model": MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": 80},   # keep responses short & fast
        }).encode("utf-8")
        req = urllib.request.Request(
            OLLAMA_URL, data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            _call_count += 1
            return json.loads(resp.read().decode("utf-8")).get("response", "").strip()
    except Exception as e:
        return ""   # fail silently — never crash the pipeline


def get_similarity_candidates(broken_url: str, rows: list[dict], limit: int = 5) -> list[str]:
    """Find the most path-similar live (200, indexable) URLs for a broken link."""
    candidates = []
    for r in rows:
        if (str(r.get("Status Code", "")).strip() == "200"
                and (r.get("Indexability", "") or "").strip().lower() == "indexable"):
            addr = r.get("Address", "")
            if addr:
                score = SequenceMatcher(None, broken_url, addr).ratio()
                candidates.append((score, addr))
    candidates.sort(reverse=True)
    return [addr for _, addr in candidates[:limit]]


def generate_text(url: str, context: dict, text_type: str) -> str:
    """
    Generate an optimized title or meta description.
    Validates length and retries once if over limit.
    Hard-crops as last resort.
    """
    limit = 60 if text_type == "title" else 155
    h1      = (context.get("h1") or "").strip() or "N/A"
    current = (context.get("current") or "").strip() or "N/A"

    prompt = (
        f"You are an SEO expert. Rewrite the {text_type} for this page.\n"
        f"URL: {url}\n"
        f"H1: {h1}\n"
        f"Current {text_type}: {current}\n"
        f"Rules: max {limit} characters, no quotes, return ONLY the {text_type} text."
    )
    res = call_ollama(prompt)

    # Validation + one retry
    if len(res) > limit:
        retry = (
            f"Shorten this {text_type} to under {limit} characters. "
            f"Keep SEO value. Return ONLY the text:\n{res}"
        )
        res = call_ollama(retry)

    return res[:limit] if res else ""   # hard crop as absolute last resort


def suggest_redirect(broken_url: str, candidates: list[str]) -> str | None:
    """Use the model to pick the best redirect target from candidates."""
    if not candidates:
        return None
    cand_list = "\n".join(f"- {c}" for c in candidates)
    prompt = (
        f"URL {broken_url} returns 404. Which candidate is the best redirect target?\n"
        f"{cand_list}\n"
        f"Return ONLY the best URL from the list, or 'None' if no match makes sense."
    )
    res = call_ollama(prompt).strip()
    return res if res in candidates else None


def run_fixer_pipeline(
    rows: list[dict],
    issues: list[dict],
    return_call_count: bool = False,
) -> dict | tuple[dict, int]:
    """
    Process detected issues and generate model-driven fixes.
    Returns fixes dict, or (fixes, model_calls) if return_call_count=True.
    """
    global _call_count
    _call_count = 0                       # reset for this run

    fixes    = {"titles": [], "redirect_map": []}
    row_map  = {r["Address"]: r for r in rows if "Address" in r}

    title_issues  = [i for i in issues if i["type"] in ("missing_title", "title_too_long")]
    meta_issues   = [i for i in issues if i["type"] in ("missing_meta_description", "meta_description_too_long")]
    broken_issues = [i for i in issues if i["type"] == "broken_link"]

    # --- Fix Titles (cap at 10 rewrites) ---
    for issue in title_issues:
        for url in issue["affected_urls"]:
            if len(fixes["titles"]) >= 10:
                break
            r = row_map.get(url, {})
            ctx = {"h1": r.get("H1-1", ""), "current": r.get("Title 1", "")}
            new_val = generate_text(url, ctx, "title")
            if new_val:
                fixes["titles"].append({"url": url, "old": ctx["current"], "new": new_val})
        if len(fixes["titles"]) >= 10:
            break

    # --- Fix Meta Descriptions (cap at 10 rewrites) ---
    meta_fixes = []
    for issue in meta_issues:
        for url in issue["affected_urls"]:
            if len(meta_fixes) >= 10:
                break
            r = row_map.get(url, {})
            ctx = {"h1": r.get("H1-1", ""), "current": r.get("Meta Description 1", "")}
            new_val = generate_text(url, ctx, "meta description")
            if new_val:
                meta_fixes.append({"url": url, "old": ctx["current"], "new": new_val})
        if len(meta_fixes) >= 10:
            break
    # Append meta fixes into titles list (schema: fixes.titles holds all metadata rewrites)
    fixes["titles"].extend(meta_fixes)

    # --- Redirect Map (cap at 10 suggestions) ---
    for issue in broken_issues:
        for url in issue["affected_urls"]:
            if len(fixes["redirect_map"]) >= 10:
                break
            candidates = get_similarity_candidates(url, rows)
            best = suggest_redirect(url, candidates)
            if best:
                fixes["redirect_map"].append({
                    "from": url,
                    "to": best,
                    "reason": "404 — path-similarity redirect suggestion",
                })
        if len(fixes["redirect_map"]) >= 10:
            break

    if return_call_count:
        return fixes, _call_count
    return fixes
