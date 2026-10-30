# OmniTune - Personal Offline Music Server
# Copyright (C) 2026-Present  DarThunder
# SPDX-License-Identifier: AGPL-3.0-or-later.

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import requests
import yt_dlp
from PIL import Image

from core.database import redis_client
from services import media_repo
from services.blobstore import BlobError, blob_client
from services.matcher import FreeMatcher
from services.storage import IMAGE_EXTS, find_media_file, gen_hash

log = logging.getLogger("omnicast.fetcher")

ALLOWED_EXTRACTORS = ["youtube", "youtube:tab", "soundcloud"]
ALLOWED_SCHEMES = {"http", "https"}

MAX_DURATION_SECONDS = 8 * 3600
MAX_FILESIZE_BYTES = 10 * 1024**3

SHM_DIR = Path("/dev/shm")
DISK_TMP_DIR = Path(os.getenv("OMNICAST_TMP_DIR", tempfile.gettempdir()))
WORKDIR_PREFIX = "omnicast-"
WORKDIR_SIZE_FACTOR = 2.5
SHM_RESERVE_BYTES = 256 * 1024**2
MIN_FREE_BYTES = 2 * 1024**3
STALE_WORKDIR_SECONDS = 2 * 3600
FALLBACK_BYTES_PER_SEC = 32 * 1024

PROGRESS_INTERVAL = 1.0
LOCK_TTL = 3600
TASK_TTL_ACTIVE = 3600
TASK_TTL_DONE = 300
TERMINAL_STATUSES = {"completed", "error"}

AUDIO_FORMAT = "bestaudio/best"

metadata_matcher = FreeMatcher()

class FetchError(Exception):
    ""

def set_task(task_id: str | None, **fields) -> None:
    if not task_id:
        return
    key = f"task:{task_id}"
    ttl = TASK_TTL_DONE if fields.get("status") in TERMINAL_STATUSES else TASK_TTL_ACTIVE
    try:
        redis_client.hset(key, mapping={k: str(v) for k, v in fields.items()})
        redis_client.expire(key, ttl)
    except Exception:
        log.warning("No se pudo actualizar la tarea %s en Redis", task_id, exc_info=True)

def _fmt_speed(bytes_per_sec) -> str:
    if not bytes_per_sec:
        return "0KiB/s"
    kib = bytes_per_sec / 1024
    return f"{kib / 1024:.2f}MiB/s" if kib >= 1024 else f"{kib:.0f}KiB/s"

def _fmt_eta(seconds) -> str:
    if seconds is None:
        return "Unknown"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"

def _make_hooks(task_id: str | None):
    last_write = 0.0

    def progress_hook(d):
        nonlocal last_write
        if d.get("status") != "downloading":
            return
        now = time.monotonic()
        if now - last_write < PROGRESS_INTERVAL:
            return
        last_write = now

        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        done = d.get("downloaded_bytes") or 0
        percent = f"{min(done / total * 100, 100):.1f}%" if total else "0%"
        set_task(
            task_id,
            status="downloading",
            percent=percent,
            speed=_fmt_speed(d.get("speed")),
            eta=_fmt_eta(d.get("eta")),
        )

    def postprocessor_hook(d):
        if d.get("status") == "started":
            set_task(task_id, status="processing", percent="100%")

    return progress_hook, postprocessor_hook

def _base_opts() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "noplaylist": True,
        "allowed_extractors": ALLOWED_EXTRACTORS,
        "max_filesize": MAX_FILESIZE_BYTES,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
    }

def _rotate_warp() -> bool:
    log.warning("Bot-check detectado por YouTube. Rotando IP vía Cloudflare WARP...")
    try:
        subprocess.run(["warp-cli", "disconnect"], check=True, stdout=subprocess.DEVNULL)
        time.sleep(2)
        subprocess.run(["warp-cli", "connect"], check=True, stdout=subprocess.DEVNULL)
        time.sleep(3)
        log.info("Túnel WARP reiniciado con éxito.")
        return True
    except Exception:
        log.exception("Error al intentar rotar la IP con warp-cli")
        return False

def _run_with_warp_retry(action, max_retries: int = 2):
    for attempt in range(max_retries + 1):
        try:
            return action()
        except yt_dlp.utils.DownloadError as e:
            err_msg = str(e).lower()
            is_bot_challenge = (
                "sign in to confirm you’re not a bot" in err_msg
                or "confirm you're not a bot" in err_msg
            )
            if is_bot_challenge and attempt < max_retries:
                if _rotate_warp():
                    continue
            raise

def _extract_info(url: str) -> dict:
    if urlparse(url).scheme.lower() not in ALLOWED_SCHEMES:
        raise FetchError("La URL debe ser http o https")

    opts = {**_base_opts(), "format": AUDIO_FORMAT}

    def _extract():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = _run_with_warp_retry(_extract)
    except yt_dlp.utils.DownloadError as e:
        raise FetchError(f"Error extrayendo información: {e}") from e

    if not info:
        raise FetchError("No se pudo extraer la información del contenido")
    if info.get("_type") == "playlist":
        raise FetchError("Las playlists no están soportadas")
    if info.get("is_live"):
        raise FetchError("Las transmisiones en vivo no están soportadas")
    duration = info.get("duration")
    if duration and duration > MAX_DURATION_SECONDS:
        raise FetchError("El contenido supera la duración máxima permitida")
    return info

def _estimate_bytes(info: dict) -> int:
    formats = info.get("requested_formats") or [info]
    total = sum((f.get("filesize") or f.get("filesize_approx") or 0) for f in formats)
    if total:
        return int(total)
    duration = info.get("duration") or 0
    return int(duration * FALLBACK_BYTES_PER_SEC)

def _make_workdir(info: dict) -> Path:
    needed = int(_estimate_bytes(info) * WORKDIR_SIZE_FACTOR)

    if SHM_DIR.is_dir():
        try:
            free = shutil.disk_usage(SHM_DIR).free
            if free - SHM_RESERVE_BYTES >= needed:
                return Path(tempfile.mkdtemp(prefix=WORKDIR_PREFIX, dir=SHM_DIR))
            log.info(
                "/dev/shm insuficiente (libre=%d MiB, necesito ~%d MiB); uso disco",
                free // 2**20, needed // 2**20,
            )
        except OSError:
            log.warning("No se pudo usar /dev/shm; uso disco", exc_info=True)

    DISK_TMP_DIR.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(DISK_TMP_DIR).free - needed < MIN_FREE_BYTES:
        raise FetchError("No hay espacio suficiente en el servidor")
    return Path(tempfile.mkdtemp(prefix=WORKDIR_PREFIX, dir=DISK_TMP_DIR))

def cleanup_stale_workdirs() -> None:
    cutoff = time.time() - STALE_WORKDIR_SECONDS
    for base in (SHM_DIR, DISK_TMP_DIR):
        if not base.is_dir():
            continue
        for p in base.glob(f"{WORKDIR_PREFIX}*"):
            try:
                if p.is_dir() and p.stat().st_mtime < cutoff:
                    shutil.rmtree(p, ignore_errors=True)
                    log.info("Workdir huérfano eliminado: %s", p)
            except OSError:
                pass

def _metadata_lookup(info: dict) -> dict | None:
    try:
        return metadata_matcher.find_metadata(
            yt_title=info.get("title") or "",
            yt_uploader=info.get("uploader") or info.get("channel") or "",
            duration=info.get("duration"),
            artist=info.get("artist") or info.get("creator"),
            track=info.get("track"),
            album=info.get("album"),
            description=info.get("description"),
        )
    except Exception:
        log.warning("Falló la búsqueda de metadatos; se usan los de YouTube", exc_info=True)
        return None

def _save_cover(cover_url: str, dest_dir: Path) -> bool:
    try:
        resp = requests.get(cover_url, timeout=10)
        resp.raise_for_status()
        img = Image.open(BytesIO(resp.content)).convert("RGB")
        img.save(dest_dir / "media.webp", "WEBP")
        return True
    except Exception:
        log.warning("No se pudo descargar la portada", exc_info=True)
        return False

def _find_thumbnail(workdir: Path, media_file: Path) -> Path | None:
    for p in sorted(workdir.glob("media.*")):
        if p != media_file and p.suffix.lower() in IMAGE_EXTS:
            return p
    return None

def _discard_blobs(blob_ids: dict) -> None:
    for file_id in blob_ids.values():
        if file_id is not None:
            blob_client.delete(file_id)

def _upload_all(workdir: Path, media_hash: str) -> dict:
    media_file = find_media_file(workdir)
    if media_file is None:
        raise FetchError("La descarga terminó pero no se generó el archivo")
    media_file = Path(media_file)

    to_upload = {"media": media_file}
    thumb = _find_thumbnail(workdir, media_file)
    if thumb:
        to_upload["thumb"] = thumb
    to_upload["metadata"] = workdir / "metadata.json"

    uploaded: dict[str, int] = {}
    try:
        for role, path in to_upload.items():
            uploaded[role] = blob_client.upload_file(path, filename=f"{media_hash}{path.suffix}")
    except BlobError as e:
        _discard_blobs(uploaded)
        raise FetchError("No se pudo guardar el contenido en el almacenamiento") from e

    uploaded.setdefault("thumb", None)
    return uploaded

def _download(url: str, task_id: str | None, provider_meta: dict | None = None) -> bool:
    lock_key = None
    workdir = None

    try:
        info = _extract_info(url)
        try:
            media_hash = gen_hash(info)
        except ValueError as e:
            raise FetchError("No se pudo identificar el contenido") from e

        lock_key = f"lock:media:{media_hash}"
        if not redis_client.set(lock_key, task_id or "1", nx=True, ex=LOCK_TTL):
            lock_key = None
            raise FetchError("Este contenido ya se está descargando")

        if media_repo.get_blobs(media_hash):
            log.info("Ya existe: %s", media_hash)
            set_task(task_id, status="completed", percent="100%")
            return True

        workdir = _make_workdir(info)

        if not provider_meta:
            provider_meta = _metadata_lookup(info)

        title = (provider_meta or {}).get("title") or info.get("title") or "Sin título"
        uploader = (provider_meta or {}).get("uploader") or info.get("uploader")

        metadata = {
            "id": media_hash,
            "original_id": info.get("id"),
            "extractor": (info.get("extractor_key") or info.get("extractor") or "").lower(),
            "title": title,
            "uploader": uploader,
            "duration": info.get("duration") or 0,
            "upload_date": info.get("upload_date"),
            "url": url,
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(workdir / "metadata.json", "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=4)

        progress_hook, postprocessor_hook = _make_hooks(task_id)
        dl_opts = {
            **_base_opts(),
            "outtmpl": str(workdir / "media.%(ext)s"),
            "progress_hooks": [progress_hook],
            "postprocessor_hooks": [postprocessor_hook],
            "format": AUDIO_FORMAT,
        }

        cover_url = (provider_meta or {}).get("cover_url")
        has_cover = bool(cover_url) and _save_cover(cover_url, workdir)
        postprocessors = [{"key": "FFmpegExtractAudio", "preferredcodec": "opus"}]
        if not has_cover:
            postprocessors += [
                {"key": "FFmpegThumbnailsConvertor", "format": "webp"},
                {"key": "EmbedThumbnail", "already_have_thumbnail": True},
            ]
        dl_opts.update({
            "writethumbnail": not has_cover,
            "postprocessors": postprocessors,
        })

        set_task(task_id, status="downloading", percent="0%")

        def _exec_download():
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                ydl.download([url])

        _run_with_warp_retry(_exec_download)

        set_task(task_id, status="uploading", percent="100%")
        blob_ids = _upload_all(workdir, media_hash)

        try:
            registered = media_repo.register_media(media_hash, metadata, blob_ids)
        except Exception:
            _discard_blobs(blob_ids)
            raise
        if not registered:
            _discard_blobs(blob_ids)
            if not media_repo.get_blobs(media_hash):
                raise FetchError("No se pudo registrar el contenido")

        set_task(task_id, status="completed", percent="100%")
        log.info("Descargado (%s): %s", "music", title[:70])
        return True

    except FetchError as e:
        log.warning("Descarga rechazada (%s): %s", url, e)
        set_task(task_id, status="error", percent="-1%", detail=str(e))
        return False
    except Exception:
        log.exception("Error descargando %s", url)
        set_task(task_id, status="error", percent="-1%", detail="Error al descargar el contenido")
        return False
    finally:
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)
        if lock_key:
            try:
                redis_client.delete(lock_key)
            except Exception:
                log.warning("No se pudo liberar el lock %s", lock_key, exc_info=True)

def search_youtube(query: str, max_results: int = 5) -> list:
    search_opts = {
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "extract_flat": True,
    }
    with yt_dlp.YoutubeDL(search_opts) as ydl:
        info = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
    return (info or {}).get("entries") or []

def download_music(url: str, task_id: str | None = None, provider_meta: dict | None = None) -> bool:
    return _download(url, task_id, provider_meta)