"""FastAPI service wrapping scrape_directory.

Endpoints (match the n8n workflow in directory-scraper.n8n.json):
  GET    /                    health check
  GET    /ui                  single-page form: paste a URL, download a CSV
  POST   /scrape              one-shot: url in, auto-detected CSV out
  POST   /inspect             fetch a URL, return detected selectors + a sample
  GET    /profiles            list saved profiles
  GET    /profiles/{name}     get one saved profile
  POST   /profiles/{name}     save/update a profile (body: same shape as /inspect)
  DELETE /profiles/{name}     delete a profile
  POST   /run/{name}          run a saved profile, sync or async (body: overrides)
  GET    /jobs/{job_id}       poll an async job
  GET    /jobs                list recent jobs

Profile + job state lives in SQLite (SCRAPER_DB) so it survives container
restarts. Async jobs run via FastAPI BackgroundTasks in-process — fine at
--workers 1, but a job in flight when the worker restarts is lost. Move to
RQ/Redis before scaling workers.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

import scrape_directory as sd

DB_PATH = os.environ.get("SCRAPER_DB", "/data/scraper.db")

app = FastAPI(title="Directory Scraper")


class ScrapeRequest(BaseModel):
    url: str
    format: str = "json"
    max_pages: int = 25
    delay: float = 1.0
    min_repeat: int = 5
    render: bool = False
    wait: Optional[str] = None
    row_selector: Optional[str] = None
    col_selectors: Optional[dict] = None
    next_selector: Optional[str] = None


class QuickScrapeRequest(BaseModel):
    url: str
    max_pages: int = 25
    delay: float = 1.0
    render: bool = False


class RunOverrides(BaseModel):
    max_pages: Optional[int] = None
    format: Optional[str] = None
    async_job: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS profiles (
                name TEXT PRIMARY KEY,
                config TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                profile_name TEXT NOT NULL,
                status TEXT NOT NULL,
                params TEXT,
                result TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            )"""
        )


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/scrape")
def quick_scrape(req: QuickScrapeRequest):
    """One-shot scrape: give it a URL, get a CSV back. No profile, no
    manual selectors - uses the same auto-detection as /inspect."""
    config = sd.ScrapeConfig(url=req.url, max_pages=req.max_pages, delay=req.delay, render=req.render)
    try:
        result = sd.scrape(config)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return PlainTextResponse(
        sd.records_to_csv(result["records"]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="contacts.csv"'},
    )


UI_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Directory Scraper</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 640px; margin: 3rem auto; padding: 0 1rem; color: #222; }
  h1 { font-size: 1.4rem; }
  input[type=url] { width: 100%; padding: 0.6rem; font-size: 1rem; box-sizing: border-box; }
  button { margin-top: 0.75rem; padding: 0.6rem 1.2rem; font-size: 1rem; cursor: pointer; }
  #status { margin-top: 1rem; color: #555; white-space: pre-wrap; }
  #status.error { color: #b00020; }
  details { margin-top: 1rem; color: #555; }
</style>
</head>
<body>
<h1>Directory Scraper</h1>
<p>Paste the URL of a directory-style listing page. It'll auto-detect the rows,
follow pagination, and download a CSV of everything it finds.</p>
<form id="f">
  <input type="url" id="url" placeholder="https://example.com/directory" required>
  <details>
    <summary>Advanced</summary>
    <label>Max pages <input type="number" id="max_pages" value="25" min="1" style="width:5rem"></label>
    <label style="margin-left:1rem">Delay (sec) <input type="number" id="delay" value="1" min="0" step="0.5" style="width:5rem"></label>
  </details>
  <button type="submit">Scrape</button>
</form>
<div id="status"></div>
<script>
const form = document.getElementById('f');
const statusEl = document.getElementById('status');
form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const url = document.getElementById('url').value;
  const max_pages = Number(document.getElementById('max_pages').value) || 25;
  const delay = Number(document.getElementById('delay').value) || 1;
  statusEl.className = '';
  statusEl.textContent = 'Scraping... this can take a while for many pages.';
  try {
    const resp = await fetch('/scrape', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url, max_pages, delay}),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || ('HTTP ' + resp.status));
    }
    const blob = await resp.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'contacts.csv';
    document.body.appendChild(a);
    a.click();
    a.remove();
    statusEl.textContent = 'Done - check your downloads.';
  } catch (err) {
    statusEl.className = 'error';
    statusEl.textContent = 'Failed: ' + err.message;
  }
});
</script>
</body>
</html>
"""


@app.get("/ui", response_class=HTMLResponse)
def ui():
    return HTMLResponse(UI_HTML, headers={"Cache-Control": "no-store"})


@app.post("/inspect")
def inspect_site(req: ScrapeRequest):
    config = sd.ScrapeConfig(**req.dict())
    try:
        return sd.inspect(config)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/profiles")
def list_profiles():
    with db() as conn:
        rows = conn.execute(
            "SELECT name, config, created_at, updated_at FROM profiles ORDER BY name"
        ).fetchall()
    return [
        {
            "name": r["name"],
            "config": json.loads(r["config"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


@app.get("/profiles/{name}")
def get_profile(name: str):
    with db() as conn:
        row = conn.execute(
            "SELECT name, config, created_at, updated_at FROM profiles WHERE name = ?", (name,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    return {
        "name": row["name"],
        "config": json.loads(row["config"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@app.post("/profiles/{name}")
def save_profile(name: str, req: ScrapeRequest):
    now = _now()
    config_json = json.dumps(req.dict())
    with db() as conn:
        existing = conn.execute("SELECT created_at FROM profiles WHERE name = ?", (name,)).fetchone()
        created_at = existing["created_at"] if existing else now
        conn.execute(
            """INSERT INTO profiles (name, config, created_at, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET config = excluded.config, updated_at = excluded.updated_at""",
            (name, config_json, created_at, now),
        )
    return {"name": name, "config": req.dict(), "created_at": created_at, "updated_at": now}


@app.delete("/profiles/{name}")
def delete_profile(name: str):
    with db() as conn:
        cur = conn.execute("DELETE FROM profiles WHERE name = ?", (name,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    return {"deleted": name}


def _load_profile_config(name: str) -> dict:
    with db() as conn:
        row = conn.execute("SELECT config FROM profiles WHERE name = ?", (name,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    return json.loads(row["config"])


def _run_scrape(name: str, overrides: RunOverrides) -> dict:
    config_data = _load_profile_config(name)
    if overrides.max_pages is not None:
        config_data["max_pages"] = overrides.max_pages
    if overrides.format is not None:
        config_data["format"] = overrides.format
    return sd.scrape(sd.ScrapeConfig(**config_data))


def _run_job(job_id: str, name: str, overrides: RunOverrides) -> None:
    with db() as conn:
        conn.execute("UPDATE jobs SET status = ?, started_at = ? WHERE id = ?", ("running", _now(), job_id))
    try:
        result = _run_scrape(name, overrides)
        with db() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, result = ?, finished_at = ? WHERE id = ?",
                ("done", json.dumps(result), _now(), job_id),
            )
    except Exception as exc:
        with db() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                ("failed", str(exc), _now(), job_id),
            )


@app.post("/run/{name}")
def run_profile(name: str, overrides: RunOverrides, background_tasks: BackgroundTasks):
    config_data = _load_profile_config(name)  # 404s early if the profile doesn't exist
    fmt = overrides.format or config_data.get("format", "json")

    if overrides.async_job:
        job_id = str(uuid.uuid4())
        with db() as conn:
            conn.execute(
                "INSERT INTO jobs (id, profile_name, status, params, created_at) VALUES (?, ?, ?, ?, ?)",
                (job_id, name, "queued", json.dumps(overrides.dict()), _now()),
            )
        background_tasks.add_task(_run_job, job_id, name, overrides)
        return {"job_id": job_id, "status": "queued"}

    try:
        result = _run_scrape(name, overrides)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if fmt == "csv":
        return PlainTextResponse(sd.records_to_csv(result["records"]), media_type="text/csv")
    return result


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    with db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return {
        "id": row["id"],
        "profile_name": row["profile_name"],
        "status": row["status"],
        "params": json.loads(row["params"]) if row["params"] else None,
        "result": json.loads(row["result"]) if row["result"] else None,
        "error": row["error"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


@app.get("/jobs")
def list_jobs(profile_name: Optional[str] = None):
    query = "SELECT id, profile_name, status, created_at, started_at, finished_at FROM jobs"
    params: tuple = ()
    if profile_name:
        query += " WHERE profile_name = ?"
        params = (profile_name,)
    query += " ORDER BY created_at DESC LIMIT 100"
    with db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]
