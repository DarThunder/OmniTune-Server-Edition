# OmniTune - Personal Offline Music Server
# Copyright (C) 2026-Present  DarThunder
# SPDX-License-Identifier: AGPL-3.0-or-later.

from pydantic import BaseModel, Field
from typing import Optional

class UserCreate(BaseModel):
    username: str = Field(
        ..., 
        min_length=3, 
        max_length=30,
        pattern=r"^[a-zA-Z0-9_-]+$",
        description="Solo letras, números, guiones bajos o medios."
    )
    password: str = Field(..., min_length=8)
    public_key: Optional[str] = Field(
        None,
        pattern=r"^[0-9a-fA-F]{64}$",
        description="Clave pública Ed25519 en formato hexadecimal (64 caracteres)."
    )

class RefreshTokenRequest(BaseModel):
    refresh_token: str
    nonce: Optional[str] = Field(None, min_length=16)
    signature: Optional[str] = Field(
        None,
        pattern=r"^[0-9a-fA-F]{128}$",
        description="Firma Ed25519 del nonce en formato hexadecimal (128 caracteres)."
    )

class UserProfileUpdate(BaseModel):
    bio: Optional[str] = None

class PlaylistCreate(BaseModel):
    name: str
    description: Optional[str] = None

class PlaylistUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None

class PlaylistOrder(BaseModel):
    hashes: list[str]