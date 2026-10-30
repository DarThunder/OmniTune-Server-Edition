# OmniTune - Personal Offline Music Server
# Copyright (C) 2026-Present  DarThunder
# SPDX-License-Identifier: AGPL-3.0-or-later.

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

logger = logging.getLogger("osiris.client")

_ALGORITHM = "EdDSA"
_REQUIRED = ["exp", "iat", "sub", "aud", "iss"]

class AuthError(Exception):
    ""

class AuthUnavailable(AuthError):
    ""

@dataclass(frozen=True)
class Claims:
    sub: str
    tid: Optional[str]
    roles: tuple
    jti: Optional[str]
    aud: str
    exp: int
    raw: dict

    def has_role(self, role: str) -> bool:
        return role in self.roles

def _b64u_decode(data: str) -> bytes:
    import base64
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))

class OsirisVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: Optional[str] = None,
        jwks_fetch: Optional[Callable[[], dict]] = None,
        cache_ttl: float = 300.0,
        min_refetch_interval: float = 10.0,
        leeway: int = 5,
        timeout: float = 3.0,
    ):
        if bool(jwks_url) == bool(jwks_fetch):
            raise ValueError("indica exactamente uno: jwks_url o jwks_fetch")
        self.issuer = issuer
        self.audience = audience
        self.jwks_url = jwks_url
        self._fetch_fn = jwks_fetch
        self.cache_ttl = cache_ttl
        self.min_refetch_interval = min_refetch_interval
        self.leeway = leeway
        self.timeout = timeout

        self._keys: dict[str, Ed25519PublicKey] = {}
        self._fetched_at = 0.0
        self._last_attempt = -1e12
        self._lock = threading.Lock()

    def _fetch(self) -> dict:
        if self._fetch_fn is not None:
            return self._fetch_fn()
        import httpx
        r = httpx.get(self.jwks_url, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _refresh(self) -> None:
        now = time.monotonic()
        self._last_attempt = now
        try:
            data = self._fetch()
            keys: dict[str, Ed25519PublicKey] = {}
            for k in data.get("keys", []):
                if k.get("kty") == "OKP" and k.get("crv") == "Ed25519" and k.get("kid") and k.get("x"):
                    keys[k["kid"]] = Ed25519PublicKey.from_public_bytes(_b64u_decode(k["x"]))
            if not keys:
                raise ValueError("el JWKS no contiene llaves Ed25519")
            self._keys = keys
            self._fetched_at = now
        except Exception as exc:
            if not self._keys:
                raise AuthUnavailable("no se pudo obtener el JWKS de OSIRIS") from exc
            logger.warning("No se pudo refrescar el JWKS (%s); se usa la caché", exc)
            self._fetched_at = now - self.cache_ttl + 5

    def _key_for(self, kid: str) -> Ed25519PublicKey:
        with self._lock:
            now = time.monotonic()
            if not self._keys or now - self._fetched_at > self.cache_ttl:
                self._refresh()
            key = self._keys.get(kid)
            if key is None and now - self._last_attempt >= self.min_refetch_interval:
                self._refresh()
                key = self._keys.get(kid)
            if key is None:
                raise AuthError("kid desconocido")
            return key

    def verify(self, token: str) -> Claims:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise AuthError("token malformado")
        if header.get("alg") != _ALGORITHM:
            raise AuthError("algoritmo no permitido")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise AuthError("falta el kid")

        key = self._key_for(kid)
        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=[_ALGORITHM],
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway,
                options={"require": _REQUIRED},
            )
        except jwt.ExpiredSignatureError:
            raise AuthError("token expirado")
        except jwt.PyJWTError as exc:
            raise AuthError(f"token inválido ({type(exc).__name__})")

        roles = payload.get("roles") or []
        if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
            raise AuthError("claim roles inválido")
        if not isinstance(payload["sub"], str):
            raise AuthError("claim sub inválido")
        return Claims(
            sub=payload["sub"],
            tid=payload.get("tid"),
            roles=tuple(roles),
            jti=payload.get("jti"),
            aud=payload["aud"],
            exp=int(payload["exp"]),
            raw=payload,
        )

def bearer_dependency(verifier: OsirisVerifier):
    from fastapi import HTTPException, Request

    def _dependency(request: Request) -> Claims:
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise HTTPException(401, "Falta el token Bearer", headers={"WWW-Authenticate": "Bearer"})
        try:
            return verifier.verify(token.strip())
        except AuthUnavailable:
            raise HTTPException(503, "Servicio de autenticación no disponible")
        except AuthError as exc:
            raise HTTPException(
                401, str(exc), headers={"WWW-Authenticate": 'Bearer error="invalid_token"'}
            )

    return _dependency