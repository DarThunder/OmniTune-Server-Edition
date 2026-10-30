# OmniTune - Personal Offline Music Server
# Copyright (C) 2026-Present  DarThunder
# SPDX-License-Identifier: AGPL-3.0-or-later.

import asyncio
import json
import logging
import re
import uuid
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.database import get_db, redis_client
from core.audit import log_action
from services import media_repo
from services.blobstore import BlobError, blob_client, stream_body
from services.fetcher import download_music, search_youtube
from models.db import User
from routers.dependencies import get_current_user

log = logging.getLogger("omnicast.media")

DISCOVER_CACHE_TTL = 600
DISCOVER_CHUNK = 50
DISCOVER_MAX_TOTAL = 100

_PASSTHROUGH = ("content-length", "content-range", "accept-ranges")
_THUMB_CACHE = "public, max-age=86400"
_CONTROL_CHARS = re.compile(r'[\\/*?:"<>|\x00-\x1f\x7f]')

router = APIRouter(tags=["Media"])

class _BlobStreamingResponse(StreamingResponse):
    def __init__(self, upstream, **kwargs):
        super().__init__(stream_body(upstream), **kwargs)
        self._upstream = upstream

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._upstream.close()

def _blob_failure(e: BlobError, what: str) -> Response:
    if e.status_code == 416:
        headers = {}
        if cr := e.headers.get("content-range"):
            headers["Content-Range"] = cr
        return Response(status_code=416, headers=headers)
    if e.status_code == 404:
        log.warning("%s: el holder no tiene el blob referenciado en la BD (%s)", what, e)
        raise HTTPException(status_code=404, detail=f"{what} no encontrado")
    log.error("%s: fallo del blob storage: %s", what, e)
    raise HTTPException(status_code=502, detail="Almacenamiento no disponible")

def _proxy_blob(
    file_id: int,
    request: Request,
    mime: str,
    what: str,
    extra_headers: dict | None = None,
) -> Response:
    try:
        upstream = blob_client.open(file_id, request.headers.get("range"))
    except BlobError as e:
        return _blob_failure(e, what)

    headers = {k: v for k in _PASSTHROUGH if (v := upstream.headers.get(k))}
    headers.setdefault("accept-ranges", "bytes")
    if upstream.status_code == 206:
        headers["cache-control"] = "no-store"
    if extra_headers:
        headers.update(extra_headers)

    return _BlobStreamingResponse(
        upstream,
        status_code=upstream.status_code,
        media_type=mime,
        headers=headers,
    )

def _clean_result(r) -> dict | None:
    if not r or not r.get("url"):
        return None

    thumb_url = r.get("thumbnail")
    if not thumb_url and r.get("thumbnails"):
        thumb_url = r["thumbnails"][0].get("url")

    return {
        "title": r.get("title"),
        "uploader": r.get("uploader") or r.get("channel"),
        "url": r.get("url"),
        "duration": r.get("duration"),
        "thumbnail": thumb_url,
    }

def _cache_get(key: str) -> dict | None:
    try:
        raw = redis_client.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None

def _cache_set(key: str, value: dict) -> None:
    try:
        redis_client.set(key, json.dumps(value), ex=DISCOVER_CACHE_TTL)
    except Exception:
        pass

@router.get("/thumbnail/{hash}")
def get_thumbnail_route(hash: str, request: Request):
    file_id = media_repo.get_thumbnail_blob_id(hash)
    if file_id is None:
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return _proxy_blob(
        file_id, request, "image/webp", "Thumbnail", extra_headers={"cache-control": _THUMB_CACHE}
    )

@router.get("/discover")
async def discover_media(
    q: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=50),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    query = q.strip()
    start = (page - 1) * limit
    end = start + limit

    if not query or start >= DISCOVER_MAX_TOTAL:
        return {"page": page, "limit": limit, "has_more": False, "results": []}

    cache_key = f"discover:{query.lower()}"
    entry = await asyncio.to_thread(_cache_get, cache_key)
    results = entry["results"] if entry else []
    exhausted = entry["exhausted"] if entry else False

    if len(results) < end and not exhausted:
        fetch_n = min(-(-end // DISCOVER_CHUNK) * DISCOVER_CHUNK, DISCOVER_MAX_TOTAL)
        raw = await asyncio.to_thread(search_youtube, query, fetch_n)

        results = [c for c in (_clean_result(r) for r in raw) if c]
        exhausted = len(raw) < fetch_n
        await asyncio.to_thread(
            _cache_set, cache_key, {"results": results, "exhausted": exhausted}
        )

    page_results = results[start:end]
    has_more = len(results) > end or (not exhausted and end < DISCOVER_MAX_TOTAL)

    if page == 1:
        log_action(db, user.id, "SEARCH", f"Query: '{query}' Limit: {limit}")

    return {
        "page": page,
        "limit": limit,
        "has_more": has_more,
        "results": page_results
    }

@router.get("/search")
def search_media(q: str = Query(..., min_length=1), db: Session = Depends(get_db)):
    clean_query = "".join(c for c in q if c.isalnum() or c.isspace()).strip()
    if not clean_query:
        return []

    fts_query = " ".join(f'"{word}"*' for word in clean_query.split())

    query = text("""
        SELECT gm.hash, gm.title, gm.uploader, gm.added_at, gm.aliases,
               gm.duration, gm.hash AS id
        FROM global_media gm
        JOIN global_media_fts fts ON gm.hash = fts.hash
        WHERE global_media_fts MATCH :query
          AND gm.blobs IS NOT NULL
        ORDER BY bm25(global_media_fts)
        LIMIT 25;
    """)
    return db.execute(query, {"query": fts_query}).mappings().all()

@router.get("/feed", dependencies=[Depends(get_current_user)])
def get_feed(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    feed, has_more = media_repo.feed_page(skip, limit)
    return {
        "feed": feed,
        "next_skip": skip + len(feed) if has_more else None,
    }

@router.get("/music/{hash}", dependencies=[Depends(get_current_user)])
def get_music_stream_route(hash: str, request: Request):
    file_id = media_repo.get_media_blob_id(hash)
    if file_id is None:
        raise HTTPException(status_code=404, detail="Música no encontrada")
    return _proxy_blob(file_id, request, "audio/ogg", "Música")

@router.get("/music/{hash}/download")
def download_music_file(hash: str, request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    file_id = media_repo.get_media_blob_id(hash)
    if file_id is None:
        raise HTTPException(status_code=404, detail="Música no encontrada")

    meta = media_repo.get_metadata(hash) or {}
    title = meta.get("title") or "track"
    uploader = meta.get("uploader") or ""

    raw_name = f"{uploader} - {title}" if uploader else title
    clean_name = _CONTROL_CHARS.sub("", raw_name).strip() or "track"
    filename = f"{clean_name}.opus"
    ascii_name = filename.encode("ascii", "ignore").decode() or "track.opus"

    log_action(db, current_user.id, "EXPORT_MEDIA", f"Exportó archivo de audio Hash: {hash}")
    return _proxy_blob(
        file_id, request, "audio/ogg", "Música",
        extra_headers={
            "content-disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
        },
    )

@router.get("/music/progress/{task_id}", dependencies=[Depends(get_current_user)])
def get_download_progress(task_id: str):
    task_data = redis_client.hgetall(f"task:{task_id}")

    if not task_data:
        raise HTTPException(status_code=404, detail="Download task not found or expired")

    return task_data

@router.post("/music")
def set_music_route(url: str, background_tasks: BackgroundTasks, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    task_id = str(uuid.uuid4())

    initial_data = {
        "status": "starting",
        "percent": "0%",
        "speed": "0KiB/s",
        "eta": "Unknown"
    }
    redis_client.hset(f"task:{task_id}", mapping=initial_data)
    redis_client.expire(f"task:{task_id}", 3600)

    background_tasks.add_task(asyncio.to_thread, download_music, url, task_id)

    log_action(db, current_user.id, "DOWNLOAD_MUSIC_START", f"URL: {url}")
    return {
        "Message": "¡Descarga de música iniciada en segundo plano!",
        "task_id": task_id
    }