# OmniTune - Personal Offline Music Server
# Copyright (C) 2026-Present  DarThunder
# SPDX-License-Identifier: AGPL-3.0-or-later.

import os
from typing import Iterator, Tuple
import blake3
from fastcdc import fastcdc

MIN_SIZE = 1 * 1024 * 1024
AVG_SIZE = 2 * 1024 * 1024
MAX_SIZE = 4 * 1024 * 1024

def iter_chunks(path: str) -> Iterator[Tuple[int, str, bytes]]:
    if os.path.getsize(path) == 0:
        return
    for index, chunk in enumerate(fastcdc(path, MIN_SIZE, AVG_SIZE, MAX_SIZE, fat=True)):
        data = bytes(chunk.data)
        yield index, blake3.blake3(data).hexdigest(), data

def scan_chunks(path: str) -> list[Tuple[str, int, int]]:
    if os.path.getsize(path) == 0:
        return []
    manifest = []
    for chunk in fastcdc(path, MIN_SIZE, AVG_SIZE, MAX_SIZE, fat=True):
        h = blake3.blake3(chunk.data).hexdigest()
        manifest.append((h, chunk.offset, chunk.length))
    return manifest