# OmniTune - Personal Offline Music Server
# Copyright (C) 2026-Present  DarThunder
# SPDX-License-Identifier: AGPL-3.0-or-later.

import redis
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import declarative_base, sessionmaker

from core.configs import DATABASE_URL, REDIS_PASSWD

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 15},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_fts(engine):
    with engine.begin() as conn:
        conn.execute(text("DROP TRIGGER IF EXISTS media_ai;"))
        conn.execute(text("DROP TRIGGER IF EXISTS media_ad;"))
        conn.execute(text("DROP TRIGGER IF EXISTS media_au;"))
        conn.execute(text("DROP TABLE IF EXISTS media_fts;"))

        conn.execute(text("""
            CREATE VIRTUAL TABLE IF NOT EXISTS global_media_fts USING fts5(
                hash UNINDEXED,
                title,
                uploader,
                aliases,
                tokenize = 'unicode61 remove_diacritics 2'
            );
        """))

        conn.execute(text("""
            CREATE TRIGGER IF NOT EXISTS global_media_ai AFTER INSERT ON global_media BEGIN
                INSERT INTO global_media_fts(hash, title, uploader, aliases)
                VALUES (new.hash, coalesce(new.title, ''), coalesce(new.uploader, ''), coalesce(new.aliases, ''));
            END;
        """))

        conn.execute(text("""
            CREATE TRIGGER IF NOT EXISTS global_media_ad AFTER DELETE ON global_media BEGIN
                DELETE FROM global_media_fts WHERE hash = old.hash;
            END;
        """))

        conn.execute(text("""
            CREATE TRIGGER IF NOT EXISTS global_media_au AFTER UPDATE ON global_media BEGIN
                DELETE FROM global_media_fts WHERE hash = old.hash;
                INSERT INTO global_media_fts(hash, title, uploader, aliases)
                VALUES (new.hash, coalesce(new.title, ''), coalesce(new.uploader, ''), coalesce(new.aliases, ''));
            END;
        """))

        conn.execute(text("""
            INSERT INTO global_media_fts(hash, title, uploader, aliases)
            SELECT gm.hash, coalesce(gm.title, ''), coalesce(gm.uploader, ''), coalesce(gm.aliases, '')
            FROM global_media gm
            WHERE NOT EXISTS (
                SELECT 1 FROM global_media_fts fts WHERE fts.hash = gm.hash
            );
        """))

redis_client = redis.Redis(
    host="localhost",
    port=int("6379"),
    password=None,
    db=1,
    decode_responses=True,
    socket_timeout=5,
    socket_connect_timeout=3,
)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()