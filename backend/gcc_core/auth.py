"""Single-administrator authentication with a signed, HttpOnly session cookie.

Unsafe requests (POST/PUT/PATCH/DELETE) additionally require the header
``X-Requested-With: gcc-ui``. Together with ``SameSite=Strict`` this blocks
cross-site request forgery.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from .config import Settings, get_settings

COOKIE = "gcc_session"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "gcc-ui"
ALGORITHM = "HS256"

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def _issue(settings: Settings, username: str) -> str:
    now = dt.datetime.now(dt.UTC)
    payload = {"sub": username, "iat": now, "exp": now + dt.timedelta(hours=settings.session_ttl_hours)}
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def current_user(request: Request, settings: Settings = Depends(get_settings)) -> str:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired") from exc
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing X-Requested-With header")
    return payload["sub"]


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response, settings: Settings = Depends(get_settings)):
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing X-Requested-With header")
    ok_user = hmac.compare_digest(_digest(body.username), _digest(settings.admin_username))
    ok_pass = hmac.compare_digest(_digest(body.password), _digest(settings.admin_password))
    if not (ok_user and ok_pass and settings.admin_password):
        await asyncio.sleep(0.5)  # slow down guessing
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    response.set_cookie(
        COOKIE,
        _issue(settings, body.username),
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    return {"username": body.username}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: str = Depends(current_user)):
    return {"username": user}
