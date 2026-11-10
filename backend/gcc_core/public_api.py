"""Unauthenticated endpoints: only what a website owner needs to know about the crawler.

Nothing here reads credentials, database state or infrastructure settings; the payload is
exactly what ``gcc_core.botinfo.describe`` returns (the User-Agent is sent to every site anyway).
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from .botinfo import describe

router = APIRouter(prefix="/api/public", tags=["public"])


@router.get("/bot", summary="Public information about the crawler (for website owners)")
def bot_info(response: Response) -> dict:
    response.headers["Cache-Control"] = "public, max-age=300"
    return describe()
