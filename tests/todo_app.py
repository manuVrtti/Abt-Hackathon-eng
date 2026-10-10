"""Reference implementation of the sample track, used as a team's live link in tests."""

from __future__ import annotations

import uuid

from fastapi import FastAPI, HTTPException, Request, Response


def make_app(broken: bool = False, seen_headers: list | None = None) -> FastAPI:
    app = FastAPI()
    todos: dict[str, dict] = {}

    @app.middleware("http")
    async def record(request: Request, call_next):
        if seen_headers is not None:
            seen_headers.append(dict(request.headers))
        if broken:
            return Response(status_code=503)
        return await call_next(request)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/todos", status_code=201)
    async def create(request: Request):
        body = await request.json()
        title = (body or {}).get("title")
        if not title:
            raise HTTPException(422, "title required")
        t = {"id": uuid.uuid4().hex, "title": title, "done": False}
        todos[t["id"]] = t
        return t

    @app.get("/todos")
    def list_todos():
        return list(todos.values())

    @app.get("/todos/{tid}")
    def get(tid: str):
        if tid not in todos:
            raise HTTPException(404)
        return todos[tid]

    @app.patch("/todos/{tid}")
    async def patch(tid: str, request: Request):
        if tid not in todos:
            raise HTTPException(404)
        todos[tid].update({k: v for k, v in (await request.json()).items() if k in ("done", "title")})
        return todos[tid]

    @app.delete("/todos/{tid}", status_code=204)
    def delete(tid: str):
        if tid not in todos:
            raise HTTPException(404)
        del todos[tid]
        return Response(status_code=204)

    return app
