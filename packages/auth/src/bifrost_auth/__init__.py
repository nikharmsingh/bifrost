"""Shared GitHub sign-in for every Bifrost app.

hello-api mounts `Auth.router()` at /auth/*. It sets one `bifrost_session` cookie for the whole site
(Path=/), so any app on the domain can resolve the signed-in user with `Auth.current_user` or
`Auth.optional_user`. Users live in the shared Postgres `users` table.
"""

import hashlib
import hmac
import os
import re
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlencode
from uuid import uuid4

import httpx
import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.security import APIKeyCookie
from pydantic import BaseModel

SESSION_COOKIE = "bifrost_session"
STATE_COOKIE = "bifrost_oauth_state"
SESSION_TTL_SECONDS = 30 * 24 * 3600
STATE_TTL_SECONDS = 600
# Same-site paths only, so ?next= can't send someone to another site after sign-in
_SAFE_NEXT = re.compile(r"^/(?!/)[A-Za-z0-9/_\-.?=&%]*$")

metadata = sa.MetaData()
users_table = sa.Table(
    "users",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("github_id", sa.BigInteger, nullable=False, unique=True),
    sa.Column("login", sa.String, nullable=False),
    sa.Column("name", sa.String),
    sa.Column("avatar_url", sa.String),
    sa.Column("created_at", sa.String, nullable=False),
)

session_scheme = APIKeyCookie(name=SESSION_COOKIE, auto_error=False, description="Bifrost session from GitHub sign-in")


class User(BaseModel):
    id: str
    login: str
    name: str | None
    avatar_url: str | None


# Settings are read per call so tests (and env changes) don't need a reimport
def _secret() -> str:
    return os.getenv("SESSION_SECRET", "")


def _github_credentials() -> tuple[str, str]:
    return os.getenv("GITHUB_CLIENT_ID", ""), os.getenv("GITHUB_CLIENT_SECRET", "")


# ── signed cookies ─────────────────────────────────────────────────────────────


def sign(payload: str) -> str:
    """Sign `payload` with an issue time; verify with `unsign`."""
    if not _secret():
        raise HTTPException(status_code=503, detail="Sign-in is not configured (SESSION_SECRET missing)")
    value = f"{payload}:{int(time.time())}"
    mac = hmac.new(_secret().encode(), value.encode(), hashlib.sha256).hexdigest()
    return f"{value}.{mac}"


def unsign(signed: str, max_age: int) -> str | None:
    """Return the payload if the signature is valid and younger than `max_age` seconds."""
    if not _secret() or "." not in signed:
        return None
    value, mac = signed.rsplit(".", 1)
    expected = hmac.new(_secret().encode(), value.encode(), hashlib.sha256).hexdigest()
    payload, _, issued_at = value.rpartition(":")
    if not hmac.compare_digest(mac, expected) or not issued_at.isdigit():
        return None
    return payload if time.time() - int(issued_at) <= max_age else None


def _set_cookie(response: Response, name: str, value: str, max_age: int, path: str) -> None:
    response.set_cookie(name, value, max_age=max_age, path=path, httponly=True, secure=True, samesite="lax")


def safe_next(next_path: str | None) -> str:
    return next_path if next_path and _SAFE_NEXT.match(next_path) else "/"


# ── GitHub OAuth ───────────────────────────────────────────────────────────────


def exchange_code_for_github_user(code: str, redirect_uri: str) -> dict:
    client_id, client_secret = _github_credentials()
    with httpx.Client(timeout=10) as client:
        token_response = client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={"client_id": client_id, "client_secret": client_secret, "code": code, "redirect_uri": redirect_uri},
        )
        try:
            access_token = token_response.json().get("access_token")
        except ValueError:
            access_token = None
        if not access_token:
            raise HTTPException(status_code=400, detail="GitHub sign-in failed — please try again")
        user_response = client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
        )
    if user_response.status_code != 200:
        raise HTTPException(status_code=502, detail="Could not read your GitHub profile")
    return user_response.json()


class Auth:
    """Session lookup and the /auth routes, bound to an app's database engine."""

    def __init__(self, get_engine: Callable[[], sa.Engine]) -> None:
        self._get_engine = get_engine

    def create_tables(self) -> None:
        metadata.create_all(self._get_engine())

    def _user(self, user_id: str | None) -> dict | None:
        if not user_id:
            return None
        with self._get_engine().connect() as conn:
            row = conn.execute(sa.select(users_table).where(users_table.c.id == user_id)).mappings().first()
        return dict(row) if row else None

    # Dependencies — use as Depends(auth.current_user) / Depends(auth.optional_user)
    def optional_user(self, session: str | None = Depends(session_scheme)) -> dict | None:
        return self._user(unsign(session, SESSION_TTL_SECONDS) if session else None)

    def current_user(self, session: str | None = Depends(session_scheme)) -> dict:
        user = self.optional_user(session)
        if not user:
            raise HTTPException(status_code=401, detail="Sign in with GitHub to continue")
        return user

    def upsert_github_user(self, profile: dict) -> dict:
        fields = {"login": profile["login"], "name": profile.get("name"), "avatar_url": profile.get("avatar_url")}
        with self._get_engine().begin() as conn:
            existing = (
                conn.execute(sa.select(users_table).where(users_table.c.github_id == profile["id"])).mappings().first()
            )
            if existing:
                conn.execute(sa.update(users_table).where(users_table.c.id == existing["id"]).values(**fields))
                return {**existing, **fields}
            user = {
                "id": str(uuid4()),
                "github_id": profile["id"],
                "created_at": datetime.now(UTC).isoformat(),
                **fields,
            }
            conn.execute(sa.insert(users_table).values(**user))
            return user

    def router(self) -> APIRouter:
        router = APIRouter(prefix="/auth", tags=["auth"])

        def callback_url(request: Request) -> str:
            # Vercel terminates TLS at the edge, so trust its forwarded scheme
            scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
            return f"{scheme}://{request.url.netloc}/auth/callback"

        def require_config() -> None:
            if not (all(_github_credentials()) and _secret()):
                raise HTTPException(status_code=503, detail="GitHub sign-in is not configured")

        @router.get("/login", include_in_schema=False)
        def login(request: Request, next_path: str = Query("/", alias="next")) -> RedirectResponse:
            require_config()
            state = secrets.token_urlsafe(24)
            query = urlencode(
                {"client_id": _github_credentials()[0], "redirect_uri": callback_url(request), "state": state}
            )
            response = RedirectResponse(f"https://github.com/login/oauth/authorize?{query}", status_code=302)
            # The destination rides in the signed state cookie, so it can't be swapped on the way back
            _set_cookie(response, STATE_COOKIE, sign(f"{state}|{safe_next(next_path)}"), STATE_TTL_SECONDS, "/auth")
            return response

        @router.get("/callback", include_in_schema=False)
        def callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
            require_config()
            stored = unsign(request.cookies.get(STATE_COOKIE, ""), STATE_TTL_SECONDS) or ""
            expected_state, _, next_path = stored.partition("|")
            if not code or not expected_state or not hmac.compare_digest(state, expected_state):
                raise HTTPException(status_code=400, detail="Sign-in expired or was tampered with — please try again")
            user = self.upsert_github_user(exchange_code_for_github_user(code, callback_url(request)))
            response = RedirectResponse(safe_next(next_path), status_code=302)
            response.delete_cookie(STATE_COOKIE, path="/auth")
            _set_cookie(response, SESSION_COOKIE, sign(user["id"]), SESSION_TTL_SECONDS, "/")
            return response

        @router.post("/logout", status_code=204, summary="Sign out of every Bifrost app")
        def logout() -> Response:
            response = Response(status_code=204)
            response.delete_cookie(SESSION_COOKIE, path="/")
            return response

        @router.get("/me", response_model=User, summary="The signed-in user")
        def me(user: dict = Depends(self.current_user)) -> dict:
            return user

        return router
