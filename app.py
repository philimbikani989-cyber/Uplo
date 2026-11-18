"""Uploader Ultra Plus v0.1: local-first editorial queue + guarded Facebook publishing."""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from publisher import FacebookPagePublisher, PublishingError

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("UUP_DATA_DIR", ROOT / "local_data")).resolve()
MEDIA_DIR = DATA_DIR / "media"
DB = DATA_DIR / "queue.sqlite3"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Uploader Ultra Plus", version="0.1.0", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

BRANDS = {"bigdawg": "BIGDAWG", "curiosity": "The Curiosity Hub"}
MAX_BYTES = 100 * 1024 * 1024
EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".mp4"}

@contextmanager
def connection():
    db = sqlite3.connect(DB, timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=15000")
    db.execute("PRAGMA journal_mode=WAL")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def init_db():
    with connection() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS posts (
          id TEXT PRIMARY KEY, brand TEXT NOT NULL, platform TEXT NOT NULL DEFAULT 'facebook',
          text TEXT NOT NULL, schedule_at TEXT NOT NULL,
          media_name TEXT, media_type TEXT NOT NULL DEFAULT 'text',
          duration REAL, has_audio INTEGER NOT NULL DEFAULT 0,
          narration_verified INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'DRAFT',
          remote_id TEXT, error TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_due ON posts(status,schedule_at);
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, post_id TEXT NOT NULL,
          kind TEXT NOT NULL, detail TEXT, at TEXT NOT NULL
        );
        """)

def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def parse_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, "Enter a valid ISO 8601 date with timezone")
    if parsed.tzinfo is None:
        raise HTTPException(422, "Scheduled time must include a timezone offset")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")

def check_auth(authorization: str | None = Header(None)):
    expected = os.getenv("UUP_ADMIN_KEY", "")
    if len(expected) < 24:
        raise HTTPException(503, "Set UUP_ADMIN_KEY (24+ characters) before starting")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Admin key required")
    if not secrets.compare_digest(authorization[7:], expected):
        raise HTTPException(403, "Invalid admin key")

class PostNew(BaseModel):
    brand: Literal["bigdawg", "curiosity"]
    text: str = Field(min_length=3, max_length=5000)
    schedule_at: str
    media_name: str | None = None

class Approval(BaseModel):
    narration_verified: bool = False

def require_post(db, pid):
    post = db.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    if not post:
        raise HTTPException(404, "Post not found")
    return post

def record(db, pid, kind, detail=""):
    db.execute("INSERT INTO events(post_id,kind,detail,at) VALUES(?,?,?,?)", (pid, kind, detail, now_utc()))

def probe_media(path: Path):
    if path.suffix.lower() == ".mp4":
        try:
            r = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                               capture_output=True, text=True, timeout=25, check=True)
            obj = json.loads(r.stdout)
            d = float(obj.get("format", {}).get("duration") or 0)
            streams = obj.get("streams", [])
            if d <= 0 or not any(s.get("codec_type") == "video" for s in streams):
                raise ValueError("Invalid video")
            return "video", d, int(any(s.get("codec_type") == "audio" for s in streams))
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
            raise HTTPException(422, "Invalid MP4 file or ffprobe unavailable")
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.verify()
    except Exception:
        raise HTTPException(422, "Invalid image file")
    return "image", None, 0

@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")

@app.get("/api/health")
def health():
    return {"app": "Uploader Ultra Plus", "mode": "LOCAL-FIRST", "live_publishing_enabled": os.getenv("UUP_ENABLE_LIVE", "false").lower() == "true"}

@app.get("/api/brands", dependencies=[Depends(check_auth)])
def brands():
    return [{"id": k, "name": name, "facebook_configured": bool(os.getenv(f"META_PAGE_ID_{k.upper()}") and os.getenv(f"META_PAGE_TOKEN_{k.upper()}"))} for k, name in BRANDS.items()]

@app.get("/api/posts", dependencies=[Depends(check_auth)])
def list_posts():
    with connection() as db:
        return [dict(row) for row in db.execute("SELECT * FROM posts ORDER BY schedule_at ASC, created_at ASC").fetchall()]

@app.post("/api/posts", dependencies=[Depends(check_auth)])
def new_post(item: PostNew):
    schedule = parse_time(item.schedule_at)
    media_type, duration, has_audio = "text", None, 0
    if item.media_name:
        name = Path(item.media_name).name
        if name != item.media_name or not (MEDIA_DIR / name).is_file():
            raise HTTPException(400, "First upload the media file; media_name must match returned filename")
        media_type, duration, has_audio = probe_media(MEDIA_DIR / name)
    else:
        name = None
    pid, current = uuid.uuid4().hex[:16], now_utc()
    with connection() as db:
        db.execute("INSERT INTO posts(id,brand,platform,text,schedule_at,media_name,media_type,duration,has_audio,status,created_at,updated_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?,'DRAFT',?,?)",
                   (pid, item.brand, "facebook", item.text.strip(), schedule, name, media_type, duration, has_audio, current, current))
        record(db, pid, "CREATED", "Draft created")
    return {"id": pid, "status": "DRAFT"}

@app.post("/api/media", dependencies=[Depends(check_auth)])
async def upload_media(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in EXTENSIONS:
        raise HTTPException(422, "Only PNG, JPG, WEBP and MP4 files are supported")
    target = MEDIA_DIR / f"{uuid.uuid4().hex}{ext}"
    size = 0
    try:
        with target.open("wb") as dest:
            while True:
                chunk = await file.read(512 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    raise HTTPException(413, "Maximum media size is 100 MB")
                dest.write(chunk)
        mediatype, duration, has_audio = probe_media(target)
        return {"media_name": target.name, "media_type": mediatype, "duration": duration, "has_audio": bool(has_audio)}
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

@app.get("/api/media/{media_name}", dependencies=[Depends(check_auth)])
def get_media(media_name: str):
    if media_name != Path(media_name).name or not media_name.endswith(tuple(EXTENSIONS)):
        raise HTTPException(404)
    target = MEDIA_DIR / media_name
    if not target.is_file():
        raise HTTPException(404)
    return FileResponse(target)

@app.post("/api/posts/{pid}/approve", dependencies=[Depends(check_auth)])
def approve(pid: str, choice: Approval):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = require_post(db, pid)
        if row["status"] not in {"DRAFT", "ERROR"}:
            raise HTTPException(409, "Only draft or errored posts can be approved")
        if row["media_type"] == "video":
            if (row["duration"] or 0) < 60:
                raise HTTPException(422, "Story video must be at least 60 seconds")
            if not row["has_audio"]:
                raise HTTPException(422, "Video has no audio stream")
            if not choice.narration_verified:
                raise HTTPException(422, "A reviewer must verify actual audible narration, not only an audio track")
        db.execute("UPDATE posts SET status='READY',narration_verified=?,error=NULL,updated_at=? WHERE id=?",
                   (int(choice.narration_verified), now_utc(), pid))
        record(db, pid, "APPROVED", "Editorial checks complete")
    return {"id": pid, "status": "READY"}

@app.post("/api/posts/{pid}/hold", dependencies=[Depends(check_auth)])
def hold(pid: str):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = require_post(db, pid)
        if row["status"] in {"PUBLISHED", "PUBLISHING"}:
            raise HTTPException(409, "Cannot hold sent or in-progress posts")
        db.execute("UPDATE posts SET status='DRAFT',updated_at=? WHERE id=?", (now_utc(), pid))
        record(db, pid, "HELD", "Returned to draft")
    return {"id": pid, "status": "DRAFT"}

@app.get("/api/events", dependencies=[Depends(check_auth)])
def events():
    with connection() as db:
        return [dict(row) for row in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 150").fetchall()]

@app.post("/api/run", dependencies=[Depends(check_auth)])
def run_due():
    live = os.getenv("UUP_ENABLE_LIVE", "false").lower() == "true"
    outcomes = []
    with connection() as db:
        posts = [dict(r) for r in db.execute("SELECT * FROM posts WHERE status='READY' AND schedule_at<=? ORDER BY schedule_at LIMIT 5", (now_utc(),)).fetchall()]
    for post in posts:
        pid = post["id"]
        if not live:
            with connection() as db:
                record(db, pid, "SIMULATED", "Due and approved; live publishing is disabled")
            outcomes.append({"id": pid, "result": "SIMULATED", "message": "No upload attempted"})
            continue
        if post["media_type"] == "video":
            outcomes.append({"id": pid, "result": "HELD", "message": "Video publishing integration not yet implemented"})
            continue
        with connection() as db:
            db.execute("BEGIN IMMEDIATE")
            cur = db.execute("UPDATE posts SET status='PUBLISHING',updated_at=? WHERE id=? AND status='READY'", (now_utc(), pid))
            if cur.rowcount != 1:
                continue
            record(db, pid, "SENDING", "Facebook Page request started")
        try:
            media = MEDIA_DIR / post["media_name"] if post["media_name"] else None
            remote_id = FacebookPagePublisher(post["brand"]).publish(post["text"], media)
            with connection() as db:
                db.execute("UPDATE posts SET status='SUBMITTED',remote_id=?,updated_at=? WHERE id=?",
                           (remote_id, now_utc(), pid))
                record(db, pid, "SUBMITTED", f"Facebook returned ID {remote_id}; verify it is publicly visible")
            outcomes.append({"id": pid, "result": "SUBMITTED", "remote_id": remote_id})
        except PublishingError as exc:
            with connection() as db:
                db.execute("UPDATE posts SET status='ERROR',error=?,updated_at=? WHERE id=?", (str(exc), now_utc(), pid))
                record(db, pid, "ERROR", str(exc))
            outcomes.append({"id": pid, "result": "ERROR", "message": str(exc)})
    return {"live_mode": live, "outcomes": outcomes, "note": "SUBMITTED means provider returned an ID; manually verify visibility before calling it PUBLISHED"}

@app.post("/api/posts/{pid}/confirm", dependencies=[Depends(check_auth)])
def confirm_post(pid: str):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = require_post(db, pid)
        if row["status"] != "SUBMITTED" or not row["remote_id"]:
            raise HTTPException(409, "Only submitted posts with a remote ID can be marked verified")
        db.execute("UPDATE posts SET status='PUBLISHED',updated_at=? WHERE id=?", (now_utc(), pid))
        record(db, pid, "VERIFIED_LIVE", "Reviewer confirmed Page visibility")
    return {"id": pid, "status": "PUBLISHED"}

init_db()
