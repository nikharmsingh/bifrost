"""Saved AI runs: each user's history, read-only share links, and the usage shown in Settings.

Apps call `record` after a successful model call and mount `router(...)` under their own prefix:

    GET    {prefix}/history                  your recent runs (newest first)
    GET    {prefix}/history/{id}             one run, with its input and result
    DELETE {prefix}/history/{id}             erase it (token counts are kept for usage)
    POST   {prefix}/history/{id}/share       create (or return) its public link
    DELETE {prefix}/history/{id}/share       stop sharing
    GET    {prefix}/shared/{share_id}        public, read-only — no sign-in
"""

import json
import secrets
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ._schema import runs_table

HISTORY_LIMIT = 50
USAGE_DAYS = 30


class RunSummary(BaseModel):
    id: str
    title: str
    score: int | None
    created_at: str
    shared: bool


class Run(RunSummary):
    input: dict
    result: dict
    share_path: str | None


class SharedRun(BaseModel):
    title: str
    score: int | None
    created_at: str
    input: dict
    result: dict


class ShareLink(BaseModel):
    share_id: str
    share_path: str


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def token_counts(completion) -> tuple[int | None, int | None]:
    """(prompt, completion) tokens from an OpenAI-style response, if it reports them."""
    usage = getattr(completion, "usage", None)
    return getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None)


def record(
    engine: sa.Engine,
    user_id: str,
    app: str,
    *,
    title: str,
    score: int | None,
    input: dict,
    result: dict,
    tokens: tuple[int | None, int | None] = (None, None),
) -> str:
    run_id = str(uuid4())
    with engine.begin() as conn:
        conn.execute(
            sa.insert(runs_table).values(
                id=run_id,
                user_id=user_id,
                app=app,
                title=title[:200],
                score=score,
                input=json.dumps(input),
                result=json.dumps(result),
                prompt_tokens=tokens[0],
                completion_tokens=tokens[1],
                created_at=now_iso(),
            )
        )
    return run_id


def usage(conn: sa.Connection, user_id: str, *, today: date | None = None, days: int = USAGE_DAYS) -> dict:
    """Requests and tokens per day (zero-filled) and per app, over the last `days` days."""
    today = today or datetime.now(UTC).date()
    start = today - timedelta(days=days - 1)
    day = sa.func.substr(runs_table.c.created_at, 1, 10)
    window = sa.and_(runs_table.c.user_id == user_id, runs_table.c.created_at >= start.isoformat())
    totals = (
        sa.func.count().label("requests"),
        sa.func.coalesce(sa.func.sum(runs_table.c.prompt_tokens), 0).label("prompt_tokens"),
        sa.func.coalesce(sa.func.sum(runs_table.c.completion_tokens), 0).label("completion_tokens"),
    )
    per_day = {
        r["day"]: r for r in conn.execute(sa.select(day.label("day"), *totals).where(window).group_by(day)).mappings()
    }
    per_app = conn.execute(
        sa.select(runs_table.c.app, *totals).where(window).group_by(runs_table.c.app).order_by(runs_table.c.app)
    ).mappings()

    def counts(row) -> dict:
        return {
            "requests": row["requests"] if row else 0,
            "prompt_tokens": int(row["prompt_tokens"]) if row else 0,
            "completion_tokens": int(row["completion_tokens"]) if row else 0,
        }

    daily = [
        {"date": d.isoformat(), **counts(per_day.get(d.isoformat()))}
        for d in (start + timedelta(days=i) for i in range(days))
    ]
    return {
        "days": daily,
        "apps": [{"app": r["app"], **counts(r)} for r in per_app],
        "total": {k: sum(d[k] for d in daily) for k in ("requests", "prompt_tokens", "completion_tokens")},
    }


def router(
    current_user: Callable[..., dict],
    get_engine: Callable[[], sa.Engine],
    app: str,
    prefix: str,
    public_input: set[str] | None = None,
) -> APIRouter:
    """History and share routes for one app's runs, mounted under `prefix` (e.g. /code-review).

    `public_input` limits which input fields a share link reveals (None = all of them).
    """
    routes = APIRouter(prefix=prefix, tags=["history"])

    def share_path(share_id: str | None) -> str | None:
        return f"{prefix}?shared={share_id}" if share_id else None

    def owned(run_id: str, user: dict):
        return sa.and_(
            runs_table.c.id == run_id,
            runs_table.c.user_id == user["id"],
            runs_table.c.app == app,
            runs_table.c.deleted_at.is_(None),
        )

    def summary(row) -> dict:
        return {
            "id": row["id"],
            "title": row["title"],
            "score": row["score"],
            "created_at": row["created_at"],
            "shared": row["share_id"] is not None,
        }

    def find(conn: sa.Connection, run_id: str, user: dict) -> dict:
        row = conn.execute(sa.select(runs_table).where(owned(run_id, user))).mappings().first()
        # Another user's run is a 404, so ids can't be probed
        if not row:
            raise HTTPException(status_code=404, detail="Not found in your history")
        return dict(row)

    @routes.get("/history", response_model=list[RunSummary], summary="Your recent runs")
    def list_history(user: dict = Depends(current_user)) -> list[dict]:
        with get_engine().connect() as conn:
            rows = conn.execute(
                sa.select(runs_table)
                .where(runs_table.c.user_id == user["id"], runs_table.c.app == app, runs_table.c.deleted_at.is_(None))
                .order_by(runs_table.c.created_at.desc())
                .limit(HISTORY_LIMIT)
            ).mappings()
            return [summary(r) for r in rows]

    @routes.get("/history/{run_id}", response_model=Run, summary="One run from your history")
    def get_run(run_id: str, user: dict = Depends(current_user)) -> dict:
        with get_engine().connect() as conn:
            row = find(conn, run_id, user)
        return {
            **summary(row),
            "input": json.loads(row["input"]),
            "result": json.loads(row["result"]),
            "share_path": share_path(row["share_id"]),
        }

    @routes.delete("/history/{run_id}", status_code=204, summary="Delete a run from your history")
    def delete_run(run_id: str, user: dict = Depends(current_user)) -> None:
        with get_engine().begin() as conn:
            find(conn, run_id, user)
            conn.execute(
                sa.update(runs_table)
                .where(runs_table.c.id == run_id)
                .values(title="", input="{}", result="{}", share_id=None, deleted_at=now_iso())
            )

    @routes.post("/history/{run_id}/share", response_model=ShareLink, summary="Create a public read-only link")
    def share_run(run_id: str, user: dict = Depends(current_user)) -> dict:
        with get_engine().begin() as conn:
            share_id = find(conn, run_id, user)["share_id"]
            if not share_id:
                share_id = secrets.token_urlsafe(16)
                conn.execute(sa.update(runs_table).where(runs_table.c.id == run_id).values(share_id=share_id))
        return {"share_id": share_id, "share_path": share_path(share_id)}

    @routes.delete("/history/{run_id}/share", status_code=204, summary="Stop sharing a run")
    def unshare_run(run_id: str, user: dict = Depends(current_user)) -> None:
        with get_engine().begin() as conn:
            find(conn, run_id, user)
            conn.execute(sa.update(runs_table).where(runs_table.c.id == run_id).values(share_id=None))

    @routes.get("/shared/{share_id}", response_model=SharedRun, summary="A shared run (public, read-only)")
    def get_shared(share_id: str) -> dict:
        with get_engine().connect() as conn:
            row = (
                conn.execute(
                    sa.select(runs_table).where(
                        runs_table.c.share_id == share_id, runs_table.c.app == app, runs_table.c.deleted_at.is_(None)
                    )
                )
                .mappings()
                .first()
            )
        if not row:
            raise HTTPException(status_code=404, detail="This link was removed or never existed")
        return {
            "title": row["title"],
            "score": row["score"],
            "created_at": row["created_at"],
            "input": {k: v for k, v in json.loads(row["input"]).items() if public_input is None or k in public_input},
            "result": json.loads(row["result"]),
        }

    return routes
