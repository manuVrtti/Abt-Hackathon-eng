"""Playwright UI check for the top teams: does the live page load, what does it
show, are there console errors. Evidence for reviewers and judges; it does
not change the score."""

from __future__ import annotations

import time

from sqlalchemy import select

from . import db, jobs
from .config import get_settings
from .models import Submission, UICheck


@jobs.handler("uicheck")
async def uicheck_job(p: dict) -> dict:
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise jobs.Permanent("playwright not installed (pip install 'hackeval[ui]')") from e
    import os

    cfg = get_settings()
    sub_id = p["submission_id"]
    with db.session() as s:
        sub = s.get(Submission, sub_id)
        url = sub.live_url
    shot = cfg.path("screenshots") / f"{sub_id}.png"
    result = {"ok": False, "status_code": None, "title": None, "load_ms": None, "console_errors": [], "error": None}
    if not url:
        result["error"] = "no live link"
    else:
        exe = os.environ.get("HACKEVAL_CHROMIUM") or (
            "/opt/pw-browsers/chromium" if os.path.exists("/opt/pw-browsers/chromium") else None)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(**({"executable_path": exe} if exe else {}))
            try:
                page = await browser.new_page(user_agent=cfg.tester.user_agent,
                                              extra_http_headers={cfg.tester.request_id_header: f"ui-{sub_id}"},
                                              viewport={"width": 1366, "height": 850})
                page.on("console", lambda m: m.type == "error" and result["console_errors"].append(m.text[:500]))
                page.on("pageerror", lambda e: result["console_errors"].append(str(e)[:500]))
                t0 = time.perf_counter()
                resp = await page.goto(url, timeout=cfg.tester.warmup_timeout_s * 1000, wait_until="load")
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                result["load_ms"] = (time.perf_counter() - t0) * 1000
                result["status_code"] = resp.status if resp else None
                result["title"] = (await page.title())[:500]
                await page.screenshot(path=str(shot), full_page=False)
                result["ok"] = bool(resp and resp.status < 400)
            except Exception as e:
                result["error"] = f"{type(e).__name__}: {e}"[:1000]
            finally:
                await browser.close()

    with db.session() as s:
        u = s.scalars(select(UICheck).where(UICheck.submission_id == sub_id)).first() or UICheck(submission_id=sub_id)
        for k, v in result.items():
            setattr(u, k, v)
        u.screenshot_path = str(shot) if shot.exists() else None
        s.merge(u)
    return {k: result[k] for k in ("ok", "status_code", "error")}


def enqueue_ui_checks(top_n: int | None = None) -> int:
    from .ranking import preliminary

    n = top_n or get_settings().llm.top_n
    rows = preliminary()[:n]
    with db.session() as s:
        for r in rows:
            jobs.enqueue(s, "uicheck", str(r.sub_id), {"submission_id": r.sub_id}, max_attempts=2)
    return len(rows)
