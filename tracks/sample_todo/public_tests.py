"""Public sample tests for the sample to-do track (published to participants)."""

from hackeval.livetest.framework import contract_test


@contract_test(category="basics", weight=1)
async def health(ctx):
    r = await ctx.get("/health")
    ctx.expect_status(r, 200)
    ctx.expect(ctx.expect_json(r).get("status") == "ok", 'GET /health should return {"status": "ok"}')


@contract_test(category="crud", weight=2)
async def create_todo(ctx):
    r = await ctx.post("/todos", json={"title": "sample"})
    ctx.expect_status(r, 201)
    ctx.expect_keys(ctx.expect_json(r), "id", "title", "done")
