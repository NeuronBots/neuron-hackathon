"""Public: a project's hosted site, and the one door a visitor's page has back to the team."""
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from core.database import get_db
from core.models import Project
from services import sites

router = APIRouter()

# The page is someone's generated code on our domain: sandboxed, it has no access to the app's cookies or storage, and its scripts and forms still run.
_PAGE = {"Content-Security-Policy": "sandbox allow-scripts allow-forms allow-popups", "X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"}
_CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Access-Control-Allow-Headers": "content-type"}  # the sandboxed page has no origin
_hits: dict[str, deque] = defaultdict(deque)
PER_MINUTE = 5  # ponytail: in-process per site and visitor; a shared limiter if this runs on several workers


def _allowed(key: str) -> bool:
    now = time.monotonic()
    q = _hits[key]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= PER_MINUTE:
        return False
    q.append(now)
    return True


@router.get("/sites/{slug}")
def site_root(slug: str):
    return RedirectResponse(f"/sites/{slug}/")


def _gateway(method: str, path: str, body: dict | None = None) -> dict:
    from routes.billing import _call
    return _call(method, path, body)


def _key_id() -> str:
    from routes.billing import _keys
    return _keys()[0]


async def _pay_door(slug: str, name: str, request: Request, db: Session) -> JSONResponse:
    """The page's way to the payments (_catalog, _pay, _pay/<id>, _refund): small, rate limited, and the amounts are the owner's, never the page's."""
    project = db.query(Project).filter(Project.site_slug == slug).first()
    if project is None:
        return JSONResponse({"ok": False, "error": "No such site"}, status_code=404, headers=_CORS)
    body = await request.body()
    if len(body) > sites.ACTION_MAX:
        return JSONResponse({"ok": False, "error": "Too large"}, status_code=413, headers=_CORS)
    if request.method == "POST" and not (_allowed(f"{slug}:pay:{request.client.host if request.client else ''}") and _allowed(f"{slug}:pay:*")):
        return JSONResponse({"ok": False, "error": "Too many attempts, try again in a minute"}, status_code=429, headers=_CORS)
    import asyncio
    from services import payments
    status, out = await asyncio.to_thread(payments.door, db, project, name, request.method, body, _gateway, _key_id(), False, sites.url(slug))
    return JSONResponse(out, status_code=status, headers=_CORS)


@router.get("/sites/{slug}/_catalog")
async def catalog(slug: str, request: Request, db: Session = Depends(get_db)):
    return await _pay_door(slug, "_catalog", request, db)


@router.post("/sites/{slug}/_pay")
async def pay(slug: str, request: Request, db: Session = Depends(get_db)):
    return await _pay_door(slug, "_pay", request, db)


@router.get("/sites/{slug}/_pay/{payment_id}")
async def pay_status(slug: str, payment_id: str, request: Request, db: Session = Depends(get_db)):
    return await _pay_door(slug, f"_pay/{payment_id}", request, db)


@router.post("/sites/{slug}/_refund")
async def refund(slug: str, request: Request, db: Session = Depends(get_db)):
    return await _pay_door(slug, "_refund", request, db)


@router.get("/sites/{slug}/{path:path}")
def serve(slug: str, path: str):
    file = sites.resolve(slug, path)
    if file is None:
        return Response(status_code=404)
    return FileResponse(file, headers={**_PAGE, "Access-Control-Allow-Origin": "*"})  # the sandboxed page has no origin, so its own fetch("products.json") is cross-origin


@router.options("/sites/{slug}/{name:path}", include_in_schema=False)
def action_preflight(slug: str, name: str = ""):
    return Response(status_code=204, headers=_CORS)


@router.post("/sites/{slug}/_action")
async def action(slug: str, request: Request, db: Session = Depends(get_db)):
    """A visitor's message from the page (an order, a question). Small, rate limited, and handed to the Master as data."""
    project = db.query(Project).filter(Project.site_slug == slug).first()
    if project is None:
        return JSONResponse({"ok": False, "error": "No such site"}, status_code=404, headers=_CORS)
    body = await request.body()
    if not body.strip() or len(body) > sites.ACTION_MAX:
        return JSONResponse({"ok": False, "error": f"Send between 1 and {sites.ACTION_MAX} characters"}, status_code=413, headers=_CORS)
    visitor = request.client.host if request.client else ""
    if not _allowed(f"{slug}:{visitor}") or not _allowed(f"{slug}:*"):
        return JSONResponse({"ok": False, "error": "Too many messages, try again in a minute"}, status_code=429, headers=_CORS)
    await sites.deliver(project, body.decode("utf-8", errors="replace"))
    return JSONResponse({"ok": True}, headers=_CORS)
