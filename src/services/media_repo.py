# OmniTune - Personal Offline Music Server
# Copyright (C) 2026-Present  DarThunder
# SPDX-License-Identifier: AGPL-3.0-or-later.

import logging
from contextlib import contextmanager
from datetime import timezone

from sqlalchemy import desc
from sqlalchemy.exc import IntegrityError

from core.database import SessionLocal
from models.db import GlobalMedia
from services.blobstore import blob_client
from services.storage import generate_search_aliases, is_valid_hash

log = logging.getLogger("omnicast.media_repo")

@contextmanager
def _session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def _row(db, media_hash):
    if not is_valid_hash(media_hash):
        return None
    return db.query(GlobalMedia).filter(GlobalMedia.hash == media_hash).first()

def to_metadata(row) -> dict:
    added = row.added_at
    if added is not None and added.tzinfo is None:
        added = added.replace(tzinfo=timezone.utc)
    return {
        "id": row.hash,
        "original_id": row.original_id,
        "extractor": row.extractor,
        "title": row.title,
        "uploader": row.uploader or "Charlie Kirk",
        "duration": row.duration or 0,
        "upload_date": row.upload_date,
        "url": row.source_url,
        "downloaded_at": added.isoformat() if added else None,
    }

def get_blobs(media_hash: str) -> dict | None:
    with _session() as db:
        row = _row(db, media_hash)
        if not row or not row.blobs:
            return None

        return dict(row.blobs)

def get_blob_id(media_hash: str, role: str) -> int | None:
    blobs = get_blobs(media_hash)
    return blobs.get(role) if blobs else None

def get_media_blob_id(media_hash: str) -> int | None:
    return get_blob_id(media_hash, "media")

def get_thumbnail_blob_id(media_hash: str) -> int | None:
    return get_blob_id(media_hash, "thumb")

def get_metadata(media_hash: str) -> dict | None:
    with _session() as db:
        row = _row(db, media_hash)
        return to_metadata(row) if row and row.blobs else None

def get_metadata_many(media_hashes) -> dict[str, dict]:
    hashes = [h for h in dict.fromkeys(media_hashes) if is_valid_hash(h)]
    found: dict[str, dict] = {}
    with _session() as db:
        for i in range(0, len(hashes), 500):
            rows = db.query(GlobalMedia).filter(
                GlobalMedia.hash.in_(hashes[i:i + 500]), GlobalMedia.blobs.isnot(None)
            ).all()
            found.update({r.hash: to_metadata(r) for r in rows})
    return found

def list_metadata() -> list[dict]:
    with _session() as db:
        query = db.query(GlobalMedia).filter(GlobalMedia.blobs.isnot(None))
        rows = query.order_by(GlobalMedia.added_at.desc()).all()
        return [to_metadata(r) for r in rows if r.blobs]

def feed_page(skip: int, limit: int) -> tuple[list[dict], bool]:
    with _session() as db:
        query = db.query(GlobalMedia).filter(GlobalMedia.blobs.isnot(None))
        rows = (
            query.order_by(desc(GlobalMedia.added_at), GlobalMedia.hash)
            .offset(skip).limit(limit + 1).all()
        )
        return [to_metadata(r) for r in rows[:limit]], len(rows) > limit

def register_media(media_hash: str, meta: dict, blob_ids: dict) -> bool:
    title = meta.get("title") or "Sin título"
    uploader = meta.get("uploader")
    fields = {
        "title": title,
        "uploader": uploader,
        "duration": int(meta.get("duration") or 0),
        "upload_date": meta.get("upload_date"),
        "extractor": meta.get("extractor"),
        "original_id": meta.get("original_id"),
        "source_url": meta.get("url"),
        "blobs": blob_ids,
    }
    with _session() as db:
        try:
            row = _row(db, media_hash)
            if row:
                if row.blobs:
                    return False
                for key, value in fields.items():
                    setattr(row, key, value)
                if not row.aliases:
                    row.aliases = generate_search_aliases(title, uploader or "", "")
            else:
                db.add(GlobalMedia(
                    hash=media_hash,
                    aliases=generate_search_aliases(title, uploader or "", ""),
                    **fields,
                ))
            db.commit()
            return True
        except IntegrityError:
            db.rollback()
            return False

def delete_media(media_hash: str) -> bool:
    with _session() as db:
        row = _row(db, media_hash)
        if not row:
            return False
        blob_ids = [i for i in (row.blobs or {}).values() if i is not None]
        db.delete(row)
        db.commit()
    for file_id in blob_ids:
        blob_client.delete(file_id)
    return True