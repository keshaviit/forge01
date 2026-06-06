#!/usr/bin/env python3
"""
run.py — headless runner for the SEO Command Center (also the grader's entry point).

Runs the full pipeline on a Screaming Frog export:
  load -> detect -> fix (model-driven) -> recommend -> write report.json + report.html + CSVs

Usage:
  python run.py sample-export/
  python run.py sample-export/ --no-dashboard
"""
from __future__ import annotations
import argparse, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "mcp"))
sys.path.insert(0, HERE)
import server  # the MCP server module exposes every tool as a function


def main():
    ap = argparse.ArgumentParser(description="SEO Command Center — headless audit runner")
    ap.add_argument("export_dir", help="Path to the Screaming Frog export folder")
    ap.add_argument("--no-dashboard", action="store_true", help="Skip the live dashboard")
    ap.add_argument("--no-fixer", action="store_true", help="Skip model-driven fixes (faster)")
    args = ap.parse_args()

    if not args.no_dashboard:
        if server.start_dashboard() is None:
            print("[seo] WARNING: Port 7700 already in use; dashboard may show a previous run.", flush=True)
        print(f"[seo] Live cockpit: http://localhost:{server.PORT}", flush=True)
        time.sleep(0.5)

    t0 = time.time()

    # Stage 1 — Ingest
    print("[seo] Stage 1/4: Loading export …", flush=True)
    load_result = server.seo_load(args.export_dir)
    print(f"[seo]   Loaded {load_result['urls']} URLs from {load_result['site']}", flush=True)

    # Stage 2 — Detect
    print("[seo] Stage 2/4: Running rulebook detectors …", flush=True)
    det_result = server.seo_detect()
    print(f"[seo]   Detected {det_result['detected']} issue types", flush=True)

    # Stage 3 — Fix (model-driven champion tier)
    model_calls = 0
    if not args.no_fixer:
        print("[seo] Stage 3/4: Generating model-driven fixes …", flush=True)
        from seo import fixer
        fixes, model_calls = fixer.run_fixer_pipeline(
            server.RUN["rows"], server.RUN["issues"], return_call_count=True
        )
        server.seo_set_fixes(fixes["titles"], fixes["redirect_map"])
        print(f"[seo]   {len(fixes['titles'])} title/meta rewrites, "
              f"{len(fixes['redirect_map'])} redirect suggestions "
              f"({model_calls} model calls)", flush=True)
    else:
        print("[seo] Stage 3/4: Skipping fixer (--no-fixer)", flush=True)
        server.seo_set_fixes([], [])

    # Stage 4 — Prioritized recommendations
    issues_sorted = sorted(
        server.RUN["issues"],
        key=lambda x: {"High": 0, "Medium": 1, "Low": 2}.get(x["severity"], 3)
    )
    recs = []
    high_issues = [i for i in issues_sorted if i["severity"] == "High"]
    if high_issues:
        types = ", ".join(f"'{i['type']}' ({i['count']})" for i in high_issues[:3])
        recs.append(f"Immediately fix all High-severity issues: {types}.")
    for i in issues_sorted[:6]:
        if i["severity"] != "High":
            recs.append(f"Fix {i['count']} '{i['type'].replace('_',' ')}' issues ({i['severity']} severity).")
    if not recs:
        recs.append("No issues detected on this crawl — site is in great shape!")
    server.seo_recommend(recs)

    # Finalize metadata
    server.RUN["model_calls"] = model_calls
    server.RUN["duration_sec"] = round(time.time() - t0, 1)

    # Write outputs
    print("[seo] Stage 4/4: Writing outputs …", flush=True)
    server.seo_report()
    server.seo_export()

    s = server.RUN["summary"]
    dur = server.RUN["duration_sec"]
    print(f"""
╔══════════════════════════════════════╗
║      SEO AUDIT COMPLETE ✓            ║
╠══════════════════════════════════════╣
║ Site    : {server.RUN['site']:<28}║
║ URLs    : {server.RUN['urls']:<28}║
║ Issues  : {s['total_issues']:<28}║
║   High  : {s['by_severity'].get('High',0):<28}║
║   Medium: {s['by_severity'].get('Medium',0):<28}║
║   Low   : {s['by_severity'].get('Low',0):<28}║
║ Calls   : {model_calls:<28}║
║ Time    : {dur}s{'':<26}║
╠══════════════════════════════════════╣
║ outputs/report.json    (grader)      ║
║ outputs/report.html    (client)      ║""")
    if server.RUN.get("fixes", {}).get("titles"):
        print("║ outputs/fixes_titles_metas.csv       ║")
    if server.RUN.get("fixes", {}).get("redirect_map"):
        print("║ outputs/redirect_map.csv             ║")
    print("╚══════════════════════════════════════╝")


if __name__ == "__main__":
    main()
