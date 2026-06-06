"""
server.py — local MCP server + live dashboard host (one process, two faces).

  1. MCP tools over stdio  -> Claude Code calls: load, detect_issues, set_fixes, recommend, write_report, export_report
  2. HTTP + SSE on localhost:7700 -> the live cockpit that fills as issues are found.

Needs the MCP SDK to expose tools to Claude (`pip install mcp`).
Standard library otherwise.
"""
from __future__ import annotations
import json, os, queue, threading, time, html, csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DASH_DIR = os.path.join(ROOT, "dashboard")
OUT_DIR  = os.path.join(ROOT, "outputs")
PORT  = int(os.environ.get("SEO_PORT", "7700"))
MODEL = os.environ.get("RADAR_MODEL", "qwen3.5:9b")

import sys
sys.path.insert(0, ROOT)
from seo import detector  # noqa: E402

RUN = {"site": None, "urls": 0, "issues": [], "summary": None, "status": "idle",
       "export_dir": None}
_subs: list[queue.Queue] = []
_lock = threading.Lock()


def _emit(event, data):
    payload = json.dumps({"event": event, "data": data})
    with _lock:
        for q in list(_subs):
            try: q.put_nowait(payload)
            except Exception: pass


# ----- pipeline tools (importable by run.py without MCP) -----
def seo_load(export_dir: str) -> dict:
    rows = detector.load_rows(export_dir)
    RUN.update({"rows": rows, "urls": len(rows), "issues": [], "summary": None,
                "site": _guess_site(rows), "status": "running", "export_dir": export_dir})
    _emit("loaded", {"site": RUN["site"], "urls": len(rows)})
    return {"urls": len(rows), "site": RUN["site"]}


def _guess_site(rows):
    if not rows: return "unknown"
    addr = rows[0].get("Address", "")
    try:
        from urllib.parse import urlparse
        return urlparse(addr).netloc or "unknown"
    except Exception:
        return "unknown"


def seo_detect() -> dict:
    export_dir = RUN.get("export_dir")
    issues = detector.detect(RUN.get("rows", []), export_dir=export_dir)
    RUN["issues"] = issues
    RUN["summary"] = detector.summarize(issues)
    for i in issues:
        _emit("issue", i)
    _emit("summary", RUN["summary"])
    return {"detected": len(issues), "summary": RUN["summary"]}


def _report_obj() -> dict:
    return {
        "site": RUN["site"],
        "urls_crawled": RUN["urls"],
        "summary": RUN["summary"] or {"total_issues": 0, "by_severity": {}},
        "issues": RUN["issues"],
        "fixes": RUN.get("fixes", {"titles": [], "redirect_map": []}),
        "recommendations": RUN.get("recommendations", []),
        "run_meta": {
            "model": MODEL,
            "model_calls": RUN.get("model_calls", 0),
            "duration_sec": RUN.get("duration_sec", 0),
        },
    }


def seo_set_fixes(titles=None, redirect_map=None) -> dict:
    RUN["fixes"] = {"titles": titles or [], "redirect_map": redirect_map or []}
    _emit("fixes", RUN["fixes"])
    return {"titles": len(titles or []), "redirects": len(redirect_map or [])}


def seo_recommend(recommendations: list) -> dict:
    RUN["recommendations"] = recommendations
    _emit("recommendations", {"recommendations": recommendations})
    return {"count": len(recommendations)}


def seo_report() -> dict:
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, "report.json")
    json.dump(_report_obj(), open(p, "w", encoding="utf-8"), indent=2)
    RUN["status"] = "done"
    _emit("saved", {"path": p})
    return {"path": p}


def seo_export() -> dict:
    os.makedirs(OUT_DIR, exist_ok=True)
    obj = _report_obj()

    # Write report.html
    p = os.path.join(OUT_DIR, "report.html")
    open(p, "w", encoding="utf-8").write(_render_html(obj))

    # Write champion CSV fix files
    _write_fix_csvs(obj)

    _emit("exported", {"path": p})
    return {"path": p}


def _write_fix_csvs(obj: dict):
    """Write champion-tier fix CSV files alongside report.json."""
    fixes = obj.get("fixes", {})

    titles = fixes.get("titles", [])
    if titles:
        p = os.path.join(OUT_DIR, "fixes_titles_metas.csv")
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["url", "old", "new"])
            w.writeheader()
            w.writerows(titles)

    redirects = fixes.get("redirect_map", [])
    if redirects:
        p = os.path.join(OUT_DIR, "redirect_map.csv")
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["from", "to", "reason"])
            w.writeheader()
            w.writerows(redirects)


def _render_html(o) -> str:
    sev    = (o["summary"] or {}).get("by_severity", {})
    issues = sorted(o["issues"],
                    key=lambda x: {"High": 0, "Medium": 1, "Low": 2}.get(x["severity"], 3))

    def sev_badge(s):
        colors = {"High": "#e53e3e", "Medium": "#d69e2e", "Low": "#718096"}
        return (f'<span style="background:{colors.get(s,"#718096")};color:#fff;'
                f'font-size:11px;font-weight:700;padding:2px 10px;border-radius:999px">{s}</span>')

    # Per-issue detail rows with affected URL list
    issue_rows_html = ""
    for i in issues:
        urls_preview = "".join(
            f'<li style="font-size:12px;color:#a0aec0;margin:2px 0;word-break:break-all">{html.escape(u)}</li>'
            for u in i["affected_urls"][:10]
        )
        more = f'<li style="font-size:12px;color:#718096">…and {i["count"]-10} more</li>' if i["count"] > 10 else ""
        issue_rows_html += f"""
        <div style="border:1px solid #2d3748;border-radius:10px;padding:16px;margin-bottom:12px;background:#1a202c">
          <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">
            {sev_badge(i['severity'])}
            <span style="font-weight:600;font-size:14px">{i['type'].replace('_',' ').title()}</span>
            <span style="margin-left:auto;font-size:13px;color:#a0aec0">{i['count']} URL{'s' if i['count']!=1 else ''} affected</span>
          </div>
          <p style="margin:0 0 8px;font-size:13px;color:#a0aec0">{html.escape(i.get('explanation',''))}</p>
          <ul style="margin:0;padding-left:18px">{urls_preview}{more}</ul>
        </div>"""

    # Fixes section
    fixes_html = ""
    titles_fixes   = o.get("fixes", {}).get("titles", [])
    redirect_fixes = o.get("fixes", {}).get("redirect_map", [])
    if titles_fixes or redirect_fixes:
        t_rows = "".join(
            f'<tr><td style="word-break:break-all;font-size:11px;color:#a0aec0">{html.escape(f["url"])}</td>'
            f'<td style="font-size:12px;color:#fc8181">{html.escape(str(f.get("old",""))[:80])}</td>'
            f'<td style="font-size:12px;color:#68d391">{html.escape(str(f.get("new",""))[:80])}</td></tr>'
            for f in titles_fixes[:20]
        ) if titles_fixes else ""
        r_rows = "".join(
            f'<tr><td style="font-size:11px;color:#a0aec0;word-break:break-all">{html.escape(f["from"])}</td>'
            f'<td style="font-size:11px;color:#68d391;word-break:break-all">{html.escape(f["to"])}</td>'
            f'<td style="font-size:12px;color:#a0aec0">{html.escape(f.get("reason",""))}</td></tr>'
            for f in redirect_fixes[:10]
        ) if redirect_fixes else ""

        t_section = f"""<h3 style="color:#e2e8f0;margin:20px 0 8px">Title & Meta Rewrites</h3>
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <thead><tr>
            <th style="text-align:left;padding:8px;background:#2d3748;color:#a0aec0;font-size:11px;text-transform:uppercase">URL</th>
            <th style="text-align:left;padding:8px;background:#2d3748;color:#fc8181;font-size:11px;text-transform:uppercase">Old</th>
            <th style="text-align:left;padding:8px;background:#2d3748;color:#68d391;font-size:11px;text-transform:uppercase">New</th>
          </tr></thead><tbody>{t_rows}</tbody></table>""" if t_rows else ""

        r_section = f"""<h3 style="color:#e2e8f0;margin:20px 0 8px">Redirect Map</h3>
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <thead><tr>
            <th style="text-align:left;padding:8px;background:#2d3748;color:#a0aec0;font-size:11px;text-transform:uppercase">From (404)</th>
            <th style="text-align:left;padding:8px;background:#2d3748;color:#68d391;font-size:11px;text-transform:uppercase">To (live)</th>
            <th style="text-align:left;padding:8px;background:#2d3748;color:#a0aec0;font-size:11px;text-transform:uppercase">Reason</th>
          </tr></thead><tbody>{r_rows}</tbody></table>""" if r_rows else ""

        fixes_html = f'<div style="background:#1a202c;border:1px solid #2d3748;border-radius:12px;padding:24px;margin-top:24px">{t_section}{r_section}</div>'

    # Recommendations
    recs_html = "".join(
        f'<li style="margin:8px 0;font-size:14px;color:#e2e8f0">{html.escape(r)}</li>'
        for r in o.get("recommendations", [])
    ) or '<li style="color:#718096">None generated.</li>'

    total  = (o["summary"] or {}).get("total_issues", 0)
    h_cnt  = sev.get("High", 0)
    m_cnt  = sev.get("Medium", 0)
    l_cnt  = sev.get("Low", 0)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>SEO Audit Report — {html.escape(o['site'])}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:'Inter',system-ui,sans-serif;background:#0f1117;color:#e2e8f0;line-height:1.6}}
    .hero{{background:linear-gradient(135deg,#1a365d 0%,#2d3748 100%);padding:48px 40px 40px;border-bottom:1px solid #2d3748}}
    .hero h1{{font-size:32px;font-weight:700;letter-spacing:-0.5px}}
    .hero .sub{{color:#a0aec0;font-size:15px;margin-top:6px}}
    .kpi-bar{{display:flex;gap:20px;flex-wrap:wrap;margin-top:28px}}
    .kpi{{background:rgba(255,255,255,0.07);border:1px solid rgba(255,255,255,0.1);border-radius:12px;padding:16px 24px;min-width:120px}}
    .kpi .val{{font-size:36px;font-weight:700;line-height:1}}
    .kpi .lbl{{font-size:12px;color:#a0aec0;margin-top:4px;text-transform:uppercase;letter-spacing:.08em}}
    .wrap{{max-width:960px;margin:0 auto;padding:40px}}
    .section-title{{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.1em;color:#718096;margin-bottom:16px}}
    .card{{background:#171923;border:1px solid #2d3748;border-radius:14px;padding:24px;margin-bottom:24px}}
    ul{{padding-left:20px}}
    table{{width:100%;border-collapse:collapse}}
    td,th{{text-align:left;padding:8px 10px;border-bottom:1px solid #2d3748;vertical-align:top}}
    .footer{{text-align:center;color:#4a5568;font-size:12px;padding:40px 0}}
  </style>
</head>
<body>
<div class="hero">
  <h1>SEO Audit Report</h1>
  <div class="sub">{html.escape(o['site'])} &nbsp;·&nbsp; {o['urls_crawled']:,} URLs crawled &nbsp;·&nbsp; model: {html.escape(o.get('run_meta',{{}}).get('model',''))}</div>
  <div class="kpi-bar">
    <div class="kpi"><div class="val">{total}</div><div class="lbl">Total Issues</div></div>
    <div class="kpi"><div class="val" style="color:#fc8181">{h_cnt}</div><div class="lbl">High</div></div>
    <div class="kpi"><div class="val" style="color:#f6e05e">{m_cnt}</div><div class="lbl">Medium</div></div>
    <div class="kpi"><div class="val" style="color:#68d391">{l_cnt}</div><div class="lbl">Low</div></div>
    <div class="kpi"><div class="val">{o.get('run_meta',{{}}).get('duration_sec',0)}s</div><div class="lbl">Audit Time</div></div>
  </div>
</div>

<div class="wrap">
  <div class="card">
    <div class="section-title">Prioritized Recommendations</div>
    <ul>{recs_html}</ul>
  </div>

  <div class="section-title">All Issues (highest priority first)</div>
  {issue_rows_html or '<p style="color:#718096">No issues detected.</p>'}

  {fixes_html}
</div>
<div class="footer">Generated by SEO Command Center · {html.escape(o['site'])}</div>
</body>
</html>"""


# ----- dashboard HTTP host -----
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body.encode() if isinstance(body, str) else body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            p = os.path.join(DASH_DIR, "index.html")
            self._send(200, open(p, encoding="utf-8").read() if os.path.exists(p) else "no dashboard")
        elif self.path == "/app.js":
            p = os.path.join(DASH_DIR, "app.js")
            self._send(200, open(p, encoding="utf-8").read() if os.path.exists(p) else "", "application/javascript")
        elif self.path == "/state":
            self._send(200, json.dumps({k: v for k, v in RUN.items() if k != "rows"}),
                       "application/json")
        elif self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            q = queue.Queue()
            with _lock: _subs.append(q)
            try:
                snap = {k: v for k, v in RUN.items() if k != "rows"}
                self.wfile.write(f"data: {json.dumps({'event':'snapshot','data':snap})}\n\n".encode())
                self.wfile.flush()
                while True:
                    try:
                        self.wfile.write(f"data: {q.get(timeout=15)}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                with _lock:
                    if q in _subs: _subs.remove(q)
        else:
            self._send(404, "not found")


class DashboardServer(ThreadingHTTPServer):
    allow_reuse_address = True


def start_dashboard(port=PORT):
    try:
        httpd = DashboardServer(("127.0.0.1", port), H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        print(f"[seo] Dashboard started on http://localhost:{port}", flush=True)
        return httpd
    except OSError as e:
        if e.errno in (98, 48):
            print(f"[seo] Port {port} already in use; skipping.", flush=True)
        else:
            print(f"[seo] Error starting dashboard: {e}", flush=True)
        return None


def _run_mcp():
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception:
        print(f"[seo] MCP SDK not found. Dashboard only at http://localhost:{PORT}", flush=True)
        while True: time.sleep(3600)
    mcp = FastMCP("seo-command-center")

    @mcp.tool()
    def load(export_dir: str) -> dict:
        """Load a Screaming Frog export directory (expects internal_all.csv)."""
        return seo_load(export_dir)

    @mcp.tool()
    def detect_issues() -> dict:
        """Run the SEO rulebook detectors over the loaded crawl."""
        return seo_detect()

    @mcp.tool()
    def set_fixes(titles: list = None, redirect_map: list = None) -> dict:
        """Attach the model-written title rewrites and the redirect map."""
        return seo_set_fixes(titles, redirect_map)

    @mcp.tool()
    def recommend(recommendations: list) -> dict:
        """Attach the prioritized recommendations."""
        return seo_recommend(recommendations)

    @mcp.tool()
    def write_report() -> dict:
        """Write outputs/report.json."""
        return seo_report()

    @mcp.tool()
    def export_report() -> dict:
        """Write outputs/report.html (the client deliverable)."""
        return seo_export()

    mcp.run()


if __name__ == "__main__":
    start_dashboard()
    print(f"[seo] dashboard live at http://localhost:{PORT}", flush=True)
    _run_mcp()
