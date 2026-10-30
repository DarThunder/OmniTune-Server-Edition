# OmniTune - Personal Offline Music Server
# Copyright (C) 2026-Present  DarThunder
# SPDX-License-Identifier: AGPL-3.0-or-later.

import logging
import mimetypes
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterator

import httpx

from services.chunking import scan_chunks
from core.configs import HOLDER_URL

log = logging.getLogger("omnicast.blobstore")

TIMEOUT = httpx.Timeout(30.0, read=None, write=None)
UPLOAD_WORKERS = 8
STREAM_BLOCK = 1024 * 1024
READ_BYTES_MAX = 16 * 1024 * 1024

_MIME_OVERRIDES = {
    ".opus": "audio/ogg",
    ".webp": "image/webp",
    ".json": "application/json",
}

class BlobError(Exception):
    def __init__(self, message: str, status_code: int | None = None, headers: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.headers = headers or {}

def _mime_for(path: Path) -> str:
    return (
        _MIME_OVERRIDES.get(path.suffix.lower())
        or mimetypes.guess_type(path.name)[0]
        or "application/octet-stream"
    )

def _check(resp: httpx.Response) -> httpx.Response:
    if resp.is_error:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise BlobError(f"{resp.status_code} {detail}", status_code=resp.status_code, headers=dict(resp.headers))
    return resp

def stream_body(resp: httpx.Response, block: int = STREAM_BLOCK) -> Iterator[bytes]:
    try:
        yield from resp.iter_bytes(block)
    finally:
        resp.close()

class BlobClient:
    def __init__(self, base_url: str = HOLDER_URL):
        self._client = httpx.Client(
            base_url=base_url,
            timeout=TIMEOUT,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=20),
        )

    def close(self) -> None:
        self._client.close()

    def upload_file(self, path: Path, filename: str | None = None) -> int:
        path = Path(path)
        try:
            manifest = scan_chunks(str(path))
            hashes = [h for h, _, _ in manifest]

            missing: set[str] = set()
            for i in range(0, len(hashes), 2000):
                batch_hashes = hashes[i : i + 2000]
                resp_check = _check(self._client.post("/chunks/check", json={"hashes": batch_hashes}))
                missing.update(resp_check.json()["missing"])

            if missing:
                self._upload_missing(path, manifest, missing)

            resp = _check(self._client.post("/files/commit", json={
                "filename": filename or path.name,
                "mimetype": _mime_for(path),
                "total_size": path.stat().st_size,
                "chunk_hashes": hashes,
            }))
            file_id = resp.json()["file_id"]
        except httpx.HTTPError as e:
            raise BlobError(f"Error de red con el blob storage: {e}") from e

        log.info(
            "Subido %s id=%s chunks=%d (nuevos=%d, dedup=%d)",
            path.name, file_id, len(hashes), len(missing), len(hashes) - len(missing),
        )
        return file_id

    def _upload_missing(
        self, path: Path, manifest: list[tuple[str, int, int]], missing: set[str]
    ) -> None:
        def _put(item: tuple[str, int, int]) -> None:
            h, offset, length = item
            with open(path, "rb") as f:
                f.seek(offset)
                data = f.read(length)
            _check(self._client.post(f"/chunks/upload/{h}", files={"file": (h, data)}))

        pending = set(missing)
        to_upload: list[tuple[str, int, int]] = []
        for h, offset, length in manifest:
            if h in pending:
                pending.discard(h)
                to_upload.append((h, offset, length))

        with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as pool:
            for i in range(0, len(to_upload), UPLOAD_WORKERS):
                list(pool.map(_put, to_upload[i : i + UPLOAD_WORKERS]))

    def open(self, file_id: int, range_header: str | None = None) -> httpx.Response:
        headers = {"Accept-Encoding": "identity"}
        if range_header:
            headers["Range"] = range_header
        request = self._client.build_request("GET", f"/download/{file_id}", headers=headers)
        try:
            resp = self._client.send(request, stream=True)
        except httpx.HTTPError as e:
            raise BlobError(f"Error de red con el blob storage: {e}") from e

        if resp.status_code not in (200, 206):
            try:
                resp.read()
                _check(resp)
                raise BlobError(f"Respuesta inesperada {resp.status_code}", status_code=resp.status_code)
            finally:
                resp.close()
        return resp

    def read_bytes(self, file_id: int, max_bytes: int = READ_BYTES_MAX) -> bytes:
        resp = self.open(file_id)
        try:
            buf = bytearray()
            for block in resp.iter_bytes(STREAM_BLOCK):
                buf += block
                if len(buf) > max_bytes:
                    raise BlobError(f"El blob {file_id} supera {max_bytes} bytes; usa open()")
            return bytes(buf)
        except httpx.HTTPError as e:
            raise BlobError(f"Error de red con el blob storage: {e}") from e
        finally:
            resp.close()

    def delete(self, file_id: int) -> None:
        try:
            _check(self._client.delete(f"/files/{file_id}"))
        except (httpx.HTTPError, BlobError):
            log.warning("No se pudo borrar el blob %s", file_id, exc_info=True)

blob_client = BlobClient()