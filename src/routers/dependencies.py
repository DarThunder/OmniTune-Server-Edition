# OmniTune - Personal Offline Music Server
# Copyright (C) 2026-Present  DarThunder
# SPDX-License-Identifier: AGPL-3.0-or-later.

import logging

from fastapi import Depends
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.audit import log_action
from core.configs import OSIRIS_AUDIENCE, OSIRIS_ISSUER, OSIRIS_JWKS_URL
from core.database import get_db
from models.db import User
from services.osiris_client import OsirisVerifier, bearer_dependency

log = logging.getLogger("omnicast.auth")

verifier = OsirisVerifier(
    issuer=OSIRIS_ISSUER,
    audience=OSIRIS_AUDIENCE,
    jwks_url=OSIRIS_JWKS_URL,
)

current_claims = bearer_dependency(verifier)

def _get_or_provision(db: Session, sub: str) -> User:
    user = db.query(User).filter(User.osiris_sub == sub).first()
    if user is not None:
        return user

    user = User(osiris_sub=sub, username=f"osiris-{sub}")
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        user = db.query(User).filter(User.osiris_sub == sub).first()
        if user is None:
            raise
        return user
    log_action(db, user.id, "PROVISION", f"Usuario aprovisionado desde OSIRIS: {sub}")
    return user

def get_current_user(claims = Depends(current_claims), db: Session = Depends(get_db)) -> User:
    return _get_or_provision(db, claims.sub)