# OmniTune - Personal Offline Music Server
# Copyright (C) 2026-Present  DarThunder
# SPDX-License-Identifier: AGPL-3.0-or-later.

from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    JSON
)
from sqlalchemy.orm import relationship

from core.database import Base

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=True)
    bio = Column(String, nullable=True)
    osiris_sub = Column(String, unique=True, index=True, nullable=True)

    favorites = relationship("Favorite", back_populates="user", cascade="all, delete-orphan")
    playlists = relationship("Playlist", back_populates="user", cascade="all, delete-orphan")

class GlobalMedia(Base):
    __tablename__ = "global_media"

    hash = Column(String, primary_key=True)
    title = Column(String, index=True)
    uploader = Column(String)
    blobs = Column(JSON(none_as_null=True), nullable=True)
    duration = Column(Integer, nullable=False, default=0, server_default="0")
    upload_date = Column(String)
    extractor = Column(String)
    original_id = Column(String)
    source_url = Column(String)
    added_at = Column(DateTime, nullable=False, default=utcnow)
    aliases = Column(String, index=True, nullable=False)

class Favorite(Base):
    __tablename__ = "favorites"
    __table_args__ = (
        UniqueConstraint("user_id", "media_hash", name="uq_favorites_user_media"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    media_hash = Column(
        String, ForeignKey("global_media.hash", ondelete="CASCADE"), nullable=False, index=True
    )

    user = relationship("User", back_populates="favorites")

class Playlist(Base):
    __tablename__ = "playlists"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_playlists_user_name"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    custom_cover = Column(String, nullable=True)

    user = relationship("User", back_populates="playlists")
    items = relationship(
        "PlaylistItem",
        back_populates="playlist",
        cascade="all, delete-orphan",
        order_by="PlaylistItem.position",
    )

class PlaylistItem(Base):
    __tablename__ = "playlist_items"
    __table_args__ = (
        UniqueConstraint("playlist_id", "media_hash", name="uq_playlist_items_playlist_media"),
        Index("ix_playlist_items_playlist_position", "playlist_id", "position"),
    )

    id = Column(Integer, primary_key=True)
    playlist_id = Column(Integer, ForeignKey("playlists.id", ondelete="CASCADE"), nullable=False)
    media_hash = Column(
        String, ForeignKey("global_media.hash", ondelete="CASCADE"), nullable=False
    )
    position = Column(Integer, nullable=False, default=0)

    playlist = relationship("Playlist", back_populates="items")

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action = Column(String, index=True)
    details = Column(String)
    timestamp = Column(DateTime, nullable=False, default=utcnow)

class DownloadTask(Base):
    __tablename__ = "download_tasks"

    id = Column(String, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    url = Column(String)
    status = Column(String, default="pending")
    created_at = Column(DateTime, nullable=False, default=utcnow)