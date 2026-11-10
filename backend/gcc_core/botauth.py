"""Machine-to-machine authentication for the bot API (``/api/bot/v1``).

* Separate from the administrator login: a bot token never opens admin routes and an admin session
  never opens the bot API. The admin password is not reused.
* Tokens come from ``BOT_API_KEYS`` / ``BOT_API_KEYS_FILE``. Only SHA-256 digests are compared
  (constant time, no early exit), so stored digests do not reveal the token.
* Several keys can be active at once (rotation: add the new one, switch the bot, remove the old one).
* The scope is read-only by construction: the bot router only declares GET routes.
* Per-key rate limit (Redis fixed window, in-process fallback) and a limit on failed attempts per client
  address. Tokens are never logged; only the key *name* is.

Generate a key:  ``python -m gcc_core.botauth generate telegram``
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sys
import time
from dataclasses import dataclass
from functools import lru_cache

from fastapi import Depends, HTTPException, Request, Response, status

from .config import Settings, get_settings
from .log import get_logger

log = get_logger(__name__)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MIN_PLAIN_TOKEN = 32


@dataclass(frozen=True)
class BotKey:
    name: str
    digest: bytes  # sha256 of the token


class BotKeyConfigError(ValueError):
    pass


def parse_keys(raw: str) -> list[BotKey]:
    keys: list[BotKey] = []
    for entry in re.split(r"[,\n]+", raw or ""):
        entry = entry.strip()
        if not entry or entry.startswith("#"):
            continue
        name, _, secret = entry.partition(":")
        name = name.strip().lower()
        if not NAME_RE.match(name):
            raise BotKeyConfigError(f"bot key name '{name[:40]}' must match {NAME_RE.pattern}")
        secret = secret.strip()
        if secret.lower().startswith("sha256:"):
            hexdigest = secret[7:].strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", hexdigest):
                raise BotKeyConfigError(f"bot key '{name}': sha256 digest must be 64 hex characters")
            digest = bytes.fromhex(hexdigest)
        else:
            if len(secret) < MIN_PLAIN_TOKEN:
                raise BotKeyConfigError(f"bot key '{name}': a plain token needs at least {MIN_PLAIN_TOKEN} characters")
            digest = hashlib.sha256(secret.encode()).digest()
        if any(k.name == name for k in keys):
            raise BotKeyConfigError(f"duplicate bot key name '{name}'")
        keys.append(BotKey(name, digest))
    return keys


def _read_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise BotKeyConfigError(f"BOT_API_KEYS_FILE unreadable: {exc.strerror}") from exc


@lru_cache(maxsize=4)
def _load(raw: str, file_path: str) -> tuple[BotKey, ...]:
    text = raw
    if file_path:
        text = f"{text}\n{_read_file(file_path)}"
    return tuple(parse_keys(text))


def load_keys(settings: Settings) -> tuple[BotKey, ...]:
    return _load(settings.bot_api_keys, settings.bot_api_keys_file)


def match_token(token: str, keys: tuple[BotKey, ...]) -> BotKey | None:
    """Constant-time comparison against every configured key (no early exit)."""
    digest = hashlib.sha256(token.encode()).digest()
    found = None
    for k in keys:
        if hmac.compare_digest(digest, k.digest):
            found = k
    return found


# --- rate limiting -----------------------------------------------------------------------------


class RateLimiter:
    """Fixed one-minute window. Redis when reachable (shared by all API workers), else per process."""

    def __init__(self) -> None:
        self._mem: dict[str, tuple[int, int]] = {}
        self._redis = None
        self._redis_url = None

    def _client(self, url: str):
        if self._redis is None or self._redis_url != url:
            import redis

            self._redis = redis.Redis.from_url(url, socket_connect_timeout=0.3, socket_timeout=0.3)
            self._redis_url = url
        return self._redis

    def hit(self, key: str, limit: int, redis_url: str, now: float | None = None) -> tuple[bool, int, int]:
        """Returns (allowed, remaining, seconds until the window resets)."""
        now = time.time() if now is None else now
        window = int(now // 60)
        reset = int(60 - (now % 60)) or 1
        count = None
        try:
            r = self._client(redis_url)
            rk = f"gcc:rl:{key}:{window}"
            pipe = r.pipeline()
            pipe.incr(rk)
            pipe.expire(rk, 70)
            count = int(pipe.execute()[0])
        except Exception:
            count = None
        if count is None:
            w, c = self._mem.get(key, (window, 0))
            if w != window:
                w, c = window, 0
            c += 1
            self._mem[key] = (w, c)
            if len(self._mem) > 2000:
                self._mem = {k: v for k, v in self._mem.items() if v[0] == window}
            count = c
        return count <= limit, max(0, limit - count), reset


limiter = RateLimiter()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:  # Caddy appends the real peer last
        return fwd.split(",")[-1].strip()[:45] or "unknown"
    return request.client.host if request.client else "unknown"


def _reject(code: int, detail: str, headers: dict | None = None) -> HTTPException:
    return HTTPException(code, detail, headers=headers)


def bot_auth(request: Request, response: Response, settings: Settings = Depends(get_settings)) -> BotKey:
    """FastAPI dependency: validates the Bearer token and applies the rate limit."""
    try:
        keys = load_keys(settings)
    except BotKeyConfigError as exc:
        log.error("bot_api_misconfigured", error=str(exc))
        raise _reject(status.HTTP_503_SERVICE_UNAVAILABLE, "Bot API is not configured") from exc
    if not keys:
        raise _reject(status.HTTP_503_SERVICE_UNAVAILABLE, "Bot API is disabled")
    ip = _client_ip(request)
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    key = match_token(token.strip(), keys) if scheme.lower() == "bearer" and token.strip() else None
    if key is None:
        allowed, _, reset = limiter.hit(f"fail:{ip}", settings.bot_auth_fail_limit_per_min, settings.redis_url)
        log.warning("bot_auth_failed", client=ip, path=request.url.path)
        if not allowed:
            raise _reject(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed attempts", {"Retry-After": str(reset)})
        raise _reject(status.HTTP_401_UNAUTHORIZED, "Invalid or missing API token", {"WWW-Authenticate": "Bearer"})
    allowed, remaining, reset = limiter.hit(f"key:{key.name}", settings.bot_rate_limit_per_min, settings.redis_url)
    response.headers["X-RateLimit-Limit"] = str(settings.bot_rate_limit_per_min)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    response.headers["X-RateLimit-Reset"] = str(reset)
    if not allowed:
        log.info("bot_rate_limited", key=key.name)
        raise _reject(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Rate limit exceeded",
            {"Retry-After": str(reset), "X-RateLimit-Limit": str(settings.bot_rate_limit_per_min), "X-RateLimit-Remaining": "0"},
        )
    request.state.bot_key = key.name
    return key


def generate(name: str) -> tuple[str, str]:
    """(token, config line). Show the token once; store only the line."""
    if not NAME_RE.match(name):
        raise SystemExit(f"name must match {NAME_RE.pattern}")
    token = secrets.token_urlsafe(32)
    return token, f"{name}:sha256:{hashlib.sha256(token.encode()).hexdigest()}"


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] != "generate":
        print("usage: python -m gcc_core.botauth generate <name>", file=sys.stderr)
        return 2
    token, line = generate(argv[1])
    print(f"TOKEN (give it to the bot, shown once): {token}")
    print(f"BOT_API_KEYS entry (put into .env / secrets):  {line}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
