# OmniTune - Personal Offline Music Server
# Copyright (C) 2026-Present  DarThunder
# SPDX-License-Identifier: AGPL-3.0-or-later.

import hashlib
import re
from pathlib import Path

from unidecode import unidecode

_HASH_RE = re.compile(r"[0-9a-f]{64}")

IMAGE_EXTS = {".webp", ".jpg", ".jpeg", ".png"}
_NON_MEDIA_EXTS = IMAGE_EXTS | {".json", ".part", ".ytdl", ".temp"}


def is_valid_hash(media_hash) -> bool:
    return isinstance(media_hash, str) and _HASH_RE.fullmatch(media_hash) is not None


def make_hash(extractor: str, media_id) -> str:
    key = f"{str(extractor).lower()}:{media_id}:music"
    return hashlib.sha3_256(key.encode("utf-8")).hexdigest()


def gen_hash(info: dict) -> str:
    extractor = info.get("extractor_key") or info.get("extractor")
    media_id = info.get("id")
    if not extractor or not media_id:
        raise ValueError("La información extraída no trae extractor o id")
    return make_hash(extractor, media_id)


def find_media_file(dir_path: Path):
    dir_path = Path(dir_path)
    if not dir_path.is_dir():
        return None
    for file in sorted(dir_path.iterdir()):
        if (
            file.is_file()
            and file.stem == "media"
            and file.suffix.lower() not in _NON_MEDIA_EXTS
        ):
            return file
    return None


def generate_search_aliases(title: str, caster: str, extra_aliases: str = "") -> str:
    parts = []

    ascii_title = unidecode(title)
    if ascii_title.lower() != title.lower():
        parts.append(ascii_title)

    ascii_caster = unidecode(caster)
    if ascii_caster.lower() != caster.lower():
        parts.append(ascii_caster)

    if extra_aliases:
        parts.append(extra_aliases)

    return " ".join(parts)