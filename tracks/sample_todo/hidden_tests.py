"""Hidden contract tests for the sample to-do track (never published)."""

import uuid

from hackeval.livetest.framework import contract_test


@contract_test(category="basics", weight=1)
async def health(ctx):
    r = await ctx.get("/health")
    ctx.expect_status(r, 200)
    ctx.expect(ctx.expect_json(r).get("status") == "ok", 'GET /health should return {"status": "ok"}')


@contract_test(category="crud", weight=2)
async def create(ctx):
    title = f"eval-{uuid.uuid4().hex[:8]}"
    r = await ctx.post("/todos", json={"title": title})
    ctx.expect_status(r, 201)
    body = ctx.expect_json(r)
    ctx.expect_keys(body, "id", "title", "done")
    ctx.expect(body["title"] == title, "created todo should echo the title")
    ctx.expect(body["done"] is False, "new todos should not be done")
    ctx.state["id"], ctx.state["title"] = body["id"], title


@contract_test(category="crud", weight=2)
async def get_one(ctx):
    ctx.expect("id" in ctx.state, "depends on create")
    r = await ctx.get(f"/todos/{ctx.state['id']}")
    ctx.expect_status(r, 200)
    ctx.expect(ctx.expect_json(r).get("title") == ctx.state["title"], "GET /todos/{id} should return the todo")


@contract_test(category="crud", weight=1)
async def list_contains(ctx):
    r = await ctx.get("/todos")
    ctx.expect_status(r, 200)
    items = ctx.expect_json(r)
    ctx.expect(isinstance(items, list), "GET /todos should return a list")
    ctx.expect(any(i.get("id") == ctx.state.get("id") for i in items), "list should contain the created todo")


@contract_test(category="crud", weight=2)
async def mark_done(ctx):
    ctx.expect("id" in ctx.state, "depends on create")
    r = await ctx.patch(f"/todos/{ctx.state['id']}", json={"done": True})
    ctx.expect_status(r, 200)
    ctx.expect(ctx.expect_json(r).get("done") is True, "PATCH should set done")


@contract_test(category="validation", weight=1)
async def rejects_empty_title(ctx):
    r = await ctx.post("/todos", json={"title": ""})
    ctx.expect_status(r, 400, 422)


@contract_test(category="validation", weight=1)
async def missing_is_404(ctx):
    r = await ctx.get(f"/todos/does-not-exist-{uuid.uuid4().hex[:6]}")
    ctx.expect_status(r, 404)


@contract_test(category="crud", weight=2)
async def delete(ctx):
    ctx.expect("id" in ctx.state, "depends on create")
    r = await ctx.delete(f"/todos/{ctx.state['id']}")
    ctx.expect_status(r, 200, 204)
    r = await ctx.get(f"/todos/{ctx.state['id']}")
    ctx.expect_status(r, 404)
