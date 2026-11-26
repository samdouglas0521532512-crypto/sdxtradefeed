"""
SDX Trade Feed bot.

Telegram sends every post (and every edit) from the SDX Trade Ideas channel
to this little web app. It saves the message, and any chart image, into
Supabase. The SDX dashboard reads from the same table and shows it live.
"""
import asyncio
import html
import json
import math
import os
import re
from datetime import datetime, timezone

import httpx
import hashlib
import hmac
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()  # optional: lock to one channel
BUCKET = os.environ.get("CHARTS_BUCKET", "charts")
KAJABI_SECRET = os.environ.get("KAJABI_SECRET", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")

TG_API = f"https://api.telegram.org/bot{TOKEN}"
TG_FILES = f"https://api.telegram.org/file/bot{TOKEN}"
SB_HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}

app = FastAPI()
DASHBOARD_ORIGINS = [o.strip() for o in os.environ.get(
    "DASHBOARD_ORIGINS", "https://sdx-dashboard.onrender.com").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=DASHBOARD_ORIGINS,
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type", "Authorization"])


# ---------- turning Telegram formatting into HTML ----------
TAGS = {
    "bold": ("<b>", "</b>"),
    "italic": ("<i>", "</i>"),
    "underline": ("<u>", "</u>"),
    "strikethrough": ("<s>", "</s>"),
    "code": ("<code>", "</code>"),
    "pre": ("<pre>", "</pre>"),
}


def to_html(text: str, entities: list | None) -> str:
    """Rebuild bold, italic and links. Telegram counts positions in UTF-16 units."""
    if not text:
        return ""
    raw = text.encode("utf-16-le")
    length = len(raw) // 2

    def piece(a: int, b: int) -> str:
        return raw[a * 2 : b * 2].decode("utf-16-le")

    opens: dict[int, list] = {}
    closes: dict[int, list] = {}
    for e in entities or []:
        start, end = e["offset"], e["offset"] + e["length"]
        kind = e["type"]
        if kind == "text_link":
            tag = (f'<a href="{html.escape(e["url"], quote=True)}">', "</a>")
        elif kind == "url":
            tag = (f'<a href="{html.escape(piece(start, end), quote=True)}">', "</a>")
        else:
            tag = TAGS.get(kind)
        if not tag:
            continue
        opens.setdefault(start, []).append((end, tag[0]))
        closes.setdefault(end, []).append((start, tag[1]))

    points = sorted({0, length, *opens.keys(), *closes.keys()})
    out = []
    for i, pos in enumerate(points):
        for _, tag in sorted(closes.get(pos, []), key=lambda x: -x[0]):
            out.append(tag)
        for _, tag in sorted(opens.get(pos, []), key=lambda x: -x[0]):
            out.append(tag)
        if i + 1 < len(points):
            out.append(html.escape(piece(pos, points[i + 1])))
    return "".join(out).replace("\n", "<br>")


# ---------- working out what kind of message it is ----------
def classify(plain: str) -> str:
    if re.search(r"zoom\.us/", plain or "", re.I):
        return "live"
    p = " " + re.sub(r"\s+", " ", (plain or "").upper().replace("(", " ")) + " "
    if re.search(r"\b(BUY|BUYING|SELL)\b", p) and " SL " in p and "TP" in p:
        return "signal"
    if "TAKE PROFIT HIT" in p or "TP HIT" in p:
        return "win"
    if "SL HIT" in p:
        return "loss"
    if "BREAKEVEN HIT" in p or "CLOSING THIS AT ENTRY" in p:
        return "be"
    if "RISK FREE" in p:
        return "update"
    return "note"


# ---------- saving ----------
async def save_chart(client: httpx.AsyncClient, post: dict) -> str | None:
    photos = post.get("photo")
    if not photos:
        return None
    file_id = max(photos, key=lambda p: p.get("file_size", 0))["file_id"]
    info = (await client.get(f"{TG_API}/getFile", params={"file_id": file_id})).json()
    file_path = info["result"]["file_path"]
    image = (await client.get(f"{TG_FILES}/{file_path}")).content
    name = f'{post["chat"]["id"]}_{post["message_id"]}.jpg'
    r = await client.post(
        f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{name}",
        headers={**SB_HEADERS, "Content-Type": "image/jpeg", "x-upsert": "true"},
        content=image,
    )
    r.raise_for_status()
    return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{name}"


async def save_message(post: dict, edited: bool) -> None:
    text = post.get("text") or post.get("caption") or ""
    entities = post.get("entities") or post.get("caption_entities")
    async with httpx.AsyncClient(timeout=30) as client:
        row = {
            "chat_id": post["chat"]["id"],
            "message_id": post["message_id"],
            "sent_at": datetime.fromtimestamp(post["date"], timezone.utc).isoformat(),
            "html": to_html(text, entities),
            "plain": text,
            "reply_to": (post.get("reply_to_message") or {}).get("message_id"),
            "kind": classify(text),
        }
        if edited:
            row["edited_at"] = datetime.fromtimestamp(
                post.get("edit_date", post["date"]), timezone.utc
            ).isoformat()
        chart = await save_chart(client, post)
        if chart:
            row["photo_url"] = chart
        r = await client.post(
            f"{SUPABASE_URL}/rest/v1/messages",
            params={"on_conflict": "chat_id,message_id"},
            headers={
                **SB_HEADERS,
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
            json=row,
        )
        r.raise_for_status()


# ---------- web routes ----------
@app.get("/")
async def health():
    return {"ok": True}


@app.post("/telegram")
async def telegram(request: Request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403)
    update = await request.json()
    post = update.get("channel_post")
    edited = False
    if post is None:
        post = update.get("edited_channel_post")
        edited = post is not None
    if post is None:
        return {"ok": True}
    if CHANNEL_ID and str(post["chat"]["id"]) != CHANNEL_ID:
        return {"ok": True}
    await save_message(post, edited)
    return {"ok": True}


# ---------- Trade Service login with Telegram ----------
def telegram_login_ok(data: dict) -> bool:
    got = data.get("hash", "")
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data) if k != "hash" and data[k] is not None)
    secret = hashlib.sha256(TOKEN.encode()).digest()
    want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    fresh = time.time() - int(data.get("auth_date", 0)) < 86400
    return fresh and hmac.compare_digest(want, got)


@app.post("/auth/telegram")
async def auth_telegram(request: Request):
    data = await request.json()
    if not telegram_login_ok(data):
        raise HTTPException(status_code=401, detail="bad_login")
    tg_id = int(data["id"])
    # verification: a Telegram username and a full name (first and last) are required
    if not (data.get("username") and (data.get("first_name") or "").strip() and (data.get("last_name") or "").strip()):
        raise HTTPException(status_code=403, detail="incomplete_profile")
    async with httpx.AsyncClient(timeout=30) as client:
        r = (await client.get(f"{TG_API}/getChatMember",
                              params={"chat_id": CHANNEL_ID, "user_id": tg_id})).json()
        status = (r.get("result") or {}).get("status")
        member = status in ("creator", "administrator", "member") or (
            status == "restricted" and r["result"].get("is_member"))
        if not member:
            raise HTTPException(status_code=403, detail="not_member")

        email = f"tg{tg_id}@tg.sdtradingeducation.com"
        name = " ".join(x for x in [data.get("first_name"), data.get("last_name")] if x) or data.get("username") or "Member"
        hdr = {**SB_HEADERS, "Content-Type": "application/json"}
        existing = (await client.get(f"{SUPABASE_URL}/rest/v1/members", headers=SB_HEADERS,
                                     params={"email": f"eq.{email}", "select": "active"})).json()
        if existing and existing[0].get("active") is False:
            raise HTTPException(status_code=403, detail="revoked")
        await client.post(f"{SUPABASE_URL}/rest/v1/members", params={"on_conflict": "email"},
                          headers={**hdr, "Prefer": "resolution=merge-duplicates,return=minimal"},
                          json={"email": email, "name": name, "plan": "Trade Service",
                                "active": True, "telegram_id": tg_id,
                                "telegram_username": data.get("username")})
        await client.post(f"{SUPABASE_URL}/auth/v1/admin/users", headers=hdr,
                          json={"email": email, "email_confirm": True})  # already exists is fine
        link = await client.post(f"{SUPABASE_URL}/auth/v1/admin/generate_link", headers=hdr,
                                 json={"type": "magiclink", "email": email})
        link.raise_for_status()
        j = link.json()
        token_hash = j.get("hashed_token") or (j.get("properties") or {}).get("hashed_token")
        if not token_hash:
            raise HTTPException(status_code=500, detail="no_token")
        return {"token_hash": token_hash}


# ---------- Forex Factory news calendar (cached) ----------
FF_FEED = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_cal = {"at": 0.0, "data": []}


@app.get("/calendar")
async def calendar():
    if time.time() - _cal["at"] > 1800 or not _cal["data"]:
        try:
            async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "SDX-Dashboard"}) as client:
                r = await client.get(FF_FEED)
                r.raise_for_status()
                _cal["data"] = r.json()
                _cal["at"] = time.time()
        except Exception:
            if not _cal["data"]:
                raise HTTPException(status_code=503, detail="calendar_unavailable")
    return {"events": _cal["data"], "fetched_at": _cal["at"]}


# ---------- helpers for logged-in dashboard calls ----------
async def current_member(request: Request, client: httpx.AsyncClient) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="not_logged_in")
    u = await client.get(f"{SUPABASE_URL}/auth/v1/user", headers={"apikey": SUPABASE_KEY, "Authorization": auth})
    if u.status_code != 200:
        raise HTTPException(status_code=401, detail="not_logged_in")
    user = u.json()
    rows = (await client.get(f"{SUPABASE_URL}/rest/v1/members", headers=SB_HEADERS,
                             params={"email": f"ilike.{user.get('email', '')}", "select": "*"})).json()
    if not rows or not rows[0].get("active"):
        raise HTTPException(status_code=403, detail="no_access")
    return {"user": user, "member": rows[0]}


# ---------- Admin portal ----------
@app.get("/admin/members")
async def admin_members(request: Request):
    async with httpx.AsyncClient(timeout=30) as client:
        me = await current_member(request, client)
        if me["member"].get("plan") != "Admin":
            raise HTTPException(status_code=403, detail="admins_only")
        rows = (await client.get(f"{SUPABASE_URL}/rest/v1/members", headers=SB_HEADERS,
                                 params={"select": "*", "order": "created_at.desc"})).json()
        # work out each member's account size from their own results
        users = (await client.get(f"{SUPABASE_URL}/auth/v1/admin/users", headers=SB_HEADERS,
                                  params={"per_page": "1000"})).json().get("users", [])
        id_by_email = {(u.get("email") or "").lower(): u["id"] for u in users}
        settings = (await client.get(f"{SUPABASE_URL}/rest/v1/account_settings", headers=SB_HEADERS,
                                     params={"select": "*"})).json()
        logs = (await client.get(f"{SUPABASE_URL}/rest/v1/signal_logs", headers=SB_HEADERS,
                                 params={"select": "*"})).json()
        sigs = (await client.get(f"{SUPABASE_URL}/rest/v1/messages", headers=SB_HEADERS,
                                 params={"kind": "eq.signal", "select": "message_id", "order": "sent_at.asc"})).json()
        start_by_user = {x["user_id"]: x.get("start_balance") for x in settings}
        logs_by_user = {}
        for l in logs:
            logs_by_user.setdefault(l["user_id"], {})[l["message_id"]] = l
        for m in rows:
            uid = id_by_email.get((m.get("email") or "").lower())
            start = start_by_user.get(uid)
            bal = float(start) if start is not None else None
            for sgl in sigs:
                l = logs_by_user.get(uid, {}).get(sgl["message_id"])
                if not l or not l.get("took") or bal is None:
                    continue
                if l.get("pnl_amount") is not None:
                    bal += float(l["pnl_amount"])
                elif l.get("pnl_pct") is not None:
                    bal += bal * float(l["pnl_pct"]) / 100
                elif l.get("result_r") is not None and l.get("risk_pct") is not None:
                    bal += bal * float(l["risk_pct"]) / 100 * float(l["result_r"])
            m["start_balance"] = float(start) if start is not None else None
            m["balance"] = round(bal, 2) if bal is not None else None
            m["signals_logged"] = len(logs_by_user.get(uid, {}))
        await add_activity(client, rows, users, id_by_email)
        return {"members": rows}


async def sb_all(client: httpx.AsyncClient, table: str, params: dict) -> list:
    """Read every row of a table, 1000 at a time."""
    out, start = [], 0
    while True:
        r = await client.get(f"{SUPABASE_URL}/rest/v1/{table}", headers={**SB_HEADERS, "Range": f"{start}-{start + 999}"},
                             params=params)
        if r.status_code >= 400:
            return out
        batch = r.json()
        out += batch
        if len(batch) < 1000:
            return out
        start += 1000


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


async def add_activity(client: httpx.AsyncClient, rows: list, users: list, id_by_email: dict) -> None:
    """Logins, journal use, trades, videos and scanner requests for each member (admin overview)."""
    user_by_id = {u["id"]: u for u in users}
    items = await sb_all(client, "journal_items", {"select": "id,user_id,kind,created_at,updated_at,r:data->>r,done:data->>done"})
    reqs = await sb_all(client, "journal_items", {"select": "id,user_id,data,created_at", "kind": "eq.scanreq"})
    trades = await sb_all(client, "journal_trades", {"select": "*"})
    logs = await sb_all(client, "signal_logs", {"select": "user_id,created_at"})
    per = {}

    def bump(uid, at):
        p = per.setdefault(uid, {"entries": 0, "videos": 0, "trades": 0, "wins": 0, "losses": 0, "r": 0.0, "last": None})
        if at and (p["last"] is None or at > p["last"]):
            p["last"] = at
        return p

    for it in items:
        p = bump(it["user_id"], it.get("updated_at") or it.get("created_at"))
        k = it.get("kind") or ""
        if k == "video":
            if str(it.get("done")).lower() == "true":
                p["videos"] += 1
        elif k not in ("scanreq", "review"):
            p["entries"] += 1
        if k in ("t:live", "t:futures"):
            r = _num(it.get("r"))
            if r is not None:
                p["trades"] += 1
                if r > 0:
                    p["wins"] += 1
                elif r < 0:
                    p["losses"] += 1
                p["r"] += r
    for t in trades:
        p = bump(t.get("user_id"), t.get("updated_at") or t.get("created_at"))
        r = _num(t.get("r"))
        if r is not None:
            p["trades"] += 1
            if r > 0:
                p["wins"] += 1
            elif r < 0:
                p["losses"] += 1
            p["r"] += r
    for l in logs:
        bump(l.get("user_id"), l.get("created_at"))
    req_by_user = {}
    for q in reqs:
        d = q.get("data") or {}
        req_by_user.setdefault(q["user_id"], []).append({
            "id": q["id"], "scanner": d.get("scanner"), "name": d.get("name"), "tv": d.get("tv"),
            "status": d.get("status") or "requested", "at": d.get("at") or q.get("created_at")})
    for m in rows:
        uid = id_by_email.get((m.get("email") or "").lower())
        u = user_by_id.get(uid) or {}
        p = per.get(uid) or {}
        m["user_id"] = uid
        m["last_sign_in"] = u.get("last_sign_in_at")
        m["last_activity"] = max([x for x in (p.get("last"), u.get("last_sign_in_at")) if x], default=None)
        m["journal_entries"] = p.get("entries", 0)
        m["videos_watched"] = p.get("videos", 0)
        m["trades"] = p.get("trades", 0)
        m["wins"] = p.get("wins", 0)
        m["losses"] = p.get("losses", 0)
        m["total_r"] = round(p.get("r", 0.0), 2)
        m["scanner_requests"] = req_by_user.get(uid, [])


@app.get("/admin/student")
async def admin_student(request: Request, user_id: str):
    """One student's recent journal trades and logged signals, for the admin detail view."""
    async with httpx.AsyncClient(timeout=30) as client:
        me = await current_member(request, client)
        if me["member"].get("plan") != "Admin":
            raise HTTPException(status_code=403, detail="admins_only")
        trades = (await client.get(f"{SUPABASE_URL}/rest/v1/journal_trades", headers=SB_HEADERS,
                                   params={"user_id": f"eq.{user_id}", "select": "*"})).json()
        live = (await client.get(f"{SUPABASE_URL}/rest/v1/journal_items", headers=SB_HEADERS,
                                 params={"user_id": f"eq.{user_id}", "kind": "in.(t:live,t:futures)",
                                         "select": "kind,data,created_at"})).json()
        videos = (await client.get(f"{SUPABASE_URL}/rest/v1/journal_items", headers=SB_HEADERS,
                                   params={"user_id": f"eq.{user_id}", "kind": "eq.video", "select": "data"})).json()
        kinds = (await client.get(f"{SUPABASE_URL}/rest/v1/journal_items", headers=SB_HEADERS,
                                  params={"user_id": f"eq.{user_id}", "select": "kind"})).json()
        out = []
        for t in trades if isinstance(trades, list) else []:
            out.append({"date": t.get("trade_date") or t.get("created_at"), "pair": t.get("pair"), "side": t.get("side"),
                        "setup": t.get("setup"), "r": _num(t.get("r"))})
        for x in live if isinstance(live, list) else []:
            d = x.get("data") or {}
            out.append({"date": d.get("opened") or x.get("created_at"), "pair": d.get("pair"), "side": d.get("position"),
                        "setup": d.get("setup") or ("Futures" if x["kind"] == "t:futures" else "Live"), "r": _num(d.get("r"))})
        out.sort(key=lambda t: str(t.get("date") or ""), reverse=True)
        sections = {}
        for k in kinds if isinstance(kinds, list) else []:
            kk = k.get("kind") or ""
            if kk not in ("video", "scanreq"):
                sections[kk] = sections.get(kk, 0) + 1
        return {"trades": out[:30], "videos": [{"file": (v.get("data") or {}).get("v"), "done": bool((v.get("data") or {}).get("done"))}
                                                for v in videos if isinstance(videos, list)],
                "sections": sections}


@app.post("/admin/scanner-request")
async def admin_scanner_request(request: Request):
    body = await request.json()
    status = body.get("status")
    if status not in ("added", "removed", "requested"):
        raise HTTPException(status_code=400, detail="bad_status")
    async with httpx.AsyncClient(timeout=30) as client:
        me = await current_member(request, client)
        if me["member"].get("plan") != "Admin":
            raise HTTPException(status_code=403, detail="admins_only")
        rows = (await client.get(f"{SUPABASE_URL}/rest/v1/journal_items", headers=SB_HEADERS,
                                 params={"id": f"eq.{body.get('id', '')}", "kind": "eq.scanreq", "select": "id,data"})).json()
        if not rows:
            raise HTTPException(status_code=404, detail="not_found")
        data = {**(rows[0].get("data") or {}), "status": status,
                "handled_at": datetime.now(timezone.utc).isoformat()}
        await client.patch(f"{SUPABASE_URL}/rest/v1/journal_items", headers={**SB_HEADERS, "Content-Type": "application/json"},
                           params={"id": f"eq.{rows[0]['id']}"}, json={"data": data})
        return {"ok": True, "data": data}


# ---------- Trade reviews: Elite members send a trade, Sam reviews it ----------
@app.get("/admin/trade-reviews")
async def admin_trade_reviews(request: Request):
    async with httpx.AsyncClient(timeout=30) as client:
        me = await current_member(request, client)
        if me["member"].get("plan") != "Admin":
            raise HTTPException(status_code=403, detail="admins_only")
        rows = await sb_all(client, "journal_items", {"select": "id,user_id,data,created_at", "kind": "eq.review",
                                                      "order": "created_at.desc"})
        users = (await client.get(f"{SUPABASE_URL}/auth/v1/admin/users", headers=SB_HEADERS,
                                  params={"per_page": "1000"})).json().get("users", [])
        email_by_id = {u["id"]: (u.get("email") or "") for u in users}
        members = (await client.get(f"{SUPABASE_URL}/rest/v1/members", headers=SB_HEADERS,
                                    params={"select": "email,name,plan"})).json()
        by_email = {(m.get("email") or "").lower(): m for m in members}
        out = []
        for r in rows:
            em = email_by_id.get(r["user_id"], "")
            m = by_email.get(em.lower(), {})
            out.append({"id": r["id"], "created_at": r.get("created_at"), "data": r.get("data") or {},
                        "email": em, "name": m.get("name") or em, "plan": m.get("plan")})
        return {"reviews": out}


@app.post("/admin/trade-review")
async def admin_trade_review(request: Request):
    body = await request.json()
    reply = str(body.get("reply") or "").strip()
    if not reply:
        raise HTTPException(status_code=400, detail="empty_reply")
    async with httpx.AsyncClient(timeout=30) as client:
        me = await current_member(request, client)
        if me["member"].get("plan") != "Admin":
            raise HTTPException(status_code=403, detail="admins_only")
        rows = (await client.get(f"{SUPABASE_URL}/rest/v1/journal_items", headers=SB_HEADERS,
                                 params={"id": f"eq.{body.get('id', '')}", "kind": "eq.review", "select": "id,data"})).json()
        if not rows:
            raise HTTPException(status_code=404, detail="not_found")
        data = {**(rows[0].get("data") or {}), "status": "reviewed", "reply": reply[:6000],
                "reviewed_at": datetime.now(timezone.utc).isoformat()}
        await client.patch(f"{SUPABASE_URL}/rest/v1/journal_items", headers={**SB_HEADERS, "Content-Type": "application/json"},
                           params={"id": f"eq.{rows[0]['id']}"}, json={"data": data})
        return {"ok": True, "data": data}


# ---------- 1-1 calls: Sam opens slots, Elite members book, Sam adds the recording ----------
CALL_BUCKET = os.environ.get("CALL_BUCKET", "call-recordings")
SAM_CHAT_ID = os.environ.get("SAM_CHAT_ID", "").strip()  # optional: Telegram chat to tell Sam about bookings
JSON_HDR = {**SB_HEADERS, "Content-Type": "application/json"}

# Calendly: members book 1-1 calls on Sam's Calendly page, inside the dashboard.
# CALENDLY_TOKEN is a Calendly personal access token (Calendly > Integrations > API and webhooks).
CALENDLY_TOKEN = os.environ.get("CALENDLY_TOKEN", "").strip()
CALENDLY_API = "https://api.calendly.com"


def _cal_headers() -> dict:
    return {"Authorization": f"Bearer {CALENDLY_TOKEN}", "Content-Type": "application/json"}


def _cal_uri_ok(uri: str, kind: str) -> bool:
    if kind == "event":
        return bool(re.fullmatch(r"https://api\.calendly\.com/scheduled_events/[A-Za-z0-9-]+", uri or ""))
    return bool(re.fullmatch(r"https://api\.calendly\.com/scheduled_events/[A-Za-z0-9-]+/invitees/[A-Za-z0-9-]+", uri or ""))


async def _cal_get(client: httpx.AsyncClient, uri: str) -> dict | None:
    r = await client.get(uri, headers=_cal_headers())
    if r.status_code != 200:
        return None
    return (r.json() or {}).get("resource")


def _cal_event_of(invitee_uri: str) -> str:
    return invitee_uri.split("/invitees/")[0]


def _cal_fields(ev: dict, inv: dict) -> dict:
    """The bits of a Calendly booking we keep on the call."""
    start = ev.get("start_time") or ""
    end = ev.get("end_time") or ""
    try:
        mins = int((datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds() // 60)
    except ValueError:
        mins = 30
    loc = ev.get("location") or {}
    link = loc.get("join_url") or (loc.get("location") if str(loc.get("location") or "").startswith("http") else "") or ""
    at = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat() if start else ""
    return {"at": at, "mins": mins or 30, "link": str(link)[:500],
            "calendly_event": ev.get("uri") or "", "calendly_invitee": inv.get("uri") or "",
            "cancel_url": inv.get("cancel_url") or "", "reschedule_url": inv.get("reschedule_url") or ""}


async def _cal_sync(client: httpx.AsyncClient, row: dict) -> dict:
    """Brings a Calendly-booked call up to date: new time if they rescheduled, cancelled if they cancelled, the call link once it exists."""
    data = dict(row.get("data") or {})
    if not CALENDLY_TOKEN or data.get("source") != "calendly" or data.get("status") != "booked":
        return data
    if (data.get("at") or "") <= _now_iso():
        return data
    inv = await _cal_get(client, data.get("calendly_invitee") or "")
    if not inv:
        return data
    changed = False
    hops = 0
    while inv.get("status") == "canceled" and inv.get("rescheduled") and inv.get("new_invitee") and hops < 5:
        nxt = await _cal_get(client, inv["new_invitee"])
        if not nxt:
            break
        inv = nxt
        hops += 1
    ev = await _cal_get(client, _cal_event_of(inv.get("uri") or "")) if inv.get("uri") else None
    if inv.get("status") == "canceled" or (ev and ev.get("status") == "canceled"):
        data["status"] = "cancelled"
        data["cancelled_at"] = _now_iso()
        changed = True
    elif ev:
        fresh = _cal_fields(ev, inv)
        if fresh["calendly_event"] != data.get("calendly_event"):
            data.update(fresh)  # rescheduled: new time, new link
            changed = True
        for k, v in fresh.items():
            if v and data.get(k) != v:
                data[k] = v
                changed = True
    if changed:
        await client.patch(f"{SUPABASE_URL}/rest/v1/journal_items", headers=JSON_HDR, params={"id": f"eq.{row['id']}"}, json={"data": data})
    return data


def _parse_future(at: str) -> str | None:
    try:
        v = datetime.fromisoformat(str(at or "").replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
    iso = v.isoformat()
    return iso if iso > _now_iso() else None


def _uk(at: str) -> str:
    return datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone(UK).strftime("%a %d %b, %H:%M")


async def _tell_member(client: httpx.AsyncClient, user_id: str, text: str) -> None:
    """Best effort: message the member on Telegram if we know their Telegram id."""
    try:
        u = (await client.get(f"{SUPABASE_URL}/auth/v1/admin/users/{user_id}", headers=SB_HEADERS)).json()
        rows = (await client.get(f"{SUPABASE_URL}/rest/v1/members", headers=SB_HEADERS,
                                 params={"email": f"ilike.{u.get('email', '')}", "select": "telegram_id"})).json()
        tg = rows[0].get("telegram_id") if rows else None
        if tg:
            await client.post(f"{TG_API}/sendMessage", json={"chat_id": tg, "text": text})
    except Exception:
        pass


async def _tell_sam(client: httpx.AsyncClient, text: str) -> None:
    if SAM_CHAT_ID:
        try:
            await client.post(f"{TG_API}/sendMessage", json={"chat_id": SAM_CHAT_ID, "text": text})
        except Exception:
            pass


# who members can book a 1-1 with
CALL_HOSTS = {"sam": "Sam", "harley": "Harley"}
HARLEY_CHAT_ID = os.environ.get("HARLEY_CHAT_ID", "").strip()  # optional: Telegram chat to tell Harley about his calls


def _host_name(data: dict) -> str:
    return CALL_HOSTS.get((data or {}).get("host") or "sam", "Sam")


async def _tell_host(client: httpx.AsyncClient, host: str, text: str) -> None:
    """Tells whoever the call is with. Harley's go to Sam too until HARLEY_CHAT_ID is set."""
    if host == "harley" and HARLEY_CHAT_ID:
        try:
            await client.post(f"{TG_API}/sendMessage", json={"chat_id": HARLEY_CHAT_ID, "text": text})
        except Exception:
            pass
        return
    await _tell_sam(client, text if host != "harley" else f"(For Harley)\n\n{text}")


async def _cal_cancel(client: httpx.AsyncClient, data: dict, reason: str) -> None:
    ev = data.get("calendly_event") or ""
    if CALENDLY_TOKEN and data.get("source") == "calendly" and _cal_uri_ok(ev, "event"):
        try:
            await client.post(f"{ev}/cancellation", headers=_cal_headers(), json={"reason": reason})
        except Exception:
            pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _elite(request: Request, client: httpx.AsyncClient) -> dict:
    me = await current_member(request, client)
    if me["member"].get("plan") not in ("Elite Pack", "Admin"):
        raise HTTPException(status_code=403, detail="elite_only")
    return me


async def _admin(request: Request, client: httpx.AsyncClient) -> dict:
    me = await current_member(request, client)
    if me["member"].get("plan") != "Admin":
        raise HTTPException(status_code=403, detail="admins_only")
    return me


async def _signed_recording(client: httpx.AsyncClient, path: str) -> str | None:
    if not path:
        return None
    r = await client.post(f"{SUPABASE_URL}/storage/v1/object/sign/{CALL_BUCKET}/{path}", headers=JSON_HDR,
                          json={"expiresIn": 6 * 3600})
    if r.status_code != 200:
        return None
    u = r.json().get("signedURL") or r.json().get("signedUrl") or ""
    return f"{SUPABASE_URL}/storage/v1{u}" if u.startswith("/") else u or None


def _call_out(r: dict) -> dict:
    return {"id": r["id"], "created_at": r.get("created_at"), "data": r.get("data") or {}}


@app.get("/calls")
async def calls_mine(request: Request):
    async with httpx.AsyncClient(timeout=30) as client:
        me = await _elite(request, client)
        now = _now_iso()
        slots = await sb_all(client, "journal_items", {"select": "id,data", "kind": "eq.call_slot"})
        open_slots = sorted([{"id": s["id"], "at": s["data"].get("at"), "mins": s["data"].get("mins") or 30}
                             for s in slots if (s.get("data") or {}).get("at", "") > now and not s["data"].get("booked")],
                            key=lambda x: x["at"])
        mine = (await client.get(f"{SUPABASE_URL}/rest/v1/journal_items", headers=SB_HEADERS,
                                 params={"select": "id,data,created_at", "kind": "eq.call",
                                         "user_id": f"eq.{me['user']['id']}", "order": "created_at.desc"})).json()
        out = []
        for r in mine:
            r["data"] = await _cal_sync(client, r)
            c = _call_out(r)
            if c["data"].get("recording_path"):
                c["data"]["recording_url"] = await _signed_recording(client, c["data"]["recording_path"])
            out.append(c)
        return {"slots": open_slots, "calls": out, "calendly": bool(CALENDLY_TOKEN)}


@app.post("/calls/book")
async def calls_book(request: Request):
    body = await request.json()
    topic = str(body.get("topic") or "").strip()[:2000]
    async with httpx.AsyncClient(timeout=30) as client:
        me = await _elite(request, client)
        uid = me["user"]["id"]
        now = _now_iso()
        mine = (await client.get(f"{SUPABASE_URL}/rest/v1/journal_items", headers=SB_HEADERS,
                                 params={"select": "id,data", "kind": "eq.call", "user_id": f"eq.{uid}"})).json()
        if any((c.get("data") or {}).get("status") == "booked" and (c["data"].get("at") or "") > now for c in mine):
            raise HTTPException(status_code=409, detail="You already have a call booked. Once it's done you can book the next one.")
        rows = (await client.get(f"{SUPABASE_URL}/rest/v1/journal_items", headers=SB_HEADERS,
                                 params={"id": f"eq.{body.get('slot_id', '')}", "kind": "eq.call_slot", "select": "id,data"})).json()
        if not rows or rows[0]["data"].get("booked") or (rows[0]["data"].get("at") or "") <= now:
            raise HTTPException(status_code=409, detail="That time has just gone. Pick another one.")
        slot = rows[0]
        name = me["member"].get("name") or me["user"].get("email", "")
        # claim the slot only if nobody else has (stops two people booking the same time)
        claimed = (await client.patch(f"{SUPABASE_URL}/rest/v1/journal_items", headers={**JSON_HDR, "Prefer": "return=representation"},
                                      params={"id": f"eq.{slot['id']}", "data->>booked": "is.null"},
                                      json={"data": {**slot["data"], "booked": True, "user_id": uid, "name": name}})).json()
        if not claimed:
            raise HTTPException(status_code=409, detail="That time has just gone. Pick another one.")
        data = {"slot_id": slot["id"], "at": slot["data"]["at"], "mins": slot["data"].get("mins") or 30,
                "link": slot["data"].get("link") or "", "topic": topic, "status": "booked", "booked_at": now}
        made = (await client.post(f"{SUPABASE_URL}/rest/v1/journal_items", headers={**JSON_HDR, "Prefer": "return=representation"},
                                  json={"user_id": uid, "kind": "call", "data": data})).json()
        call = made[0] if isinstance(made, list) and made else None
        if not call:
            await client.patch(f"{SUPABASE_URL}/rest/v1/journal_items", headers=JSON_HDR, params={"id": f"eq.{slot['id']}"},
                               json={"data": slot["data"]})
            raise HTTPException(status_code=500, detail="Couldn't book the call. Try again.")
        await client.patch(f"{SUPABASE_URL}/rest/v1/journal_items", headers=JSON_HDR, params={"id": f"eq.{slot['id']}"},
                           json={"data": {**slot["data"], "booked": True, "user_id": uid, "name": name, "call_id": call["id"]}})
        if SAM_CHAT_ID:
            try:
                when = datetime.fromisoformat(data["at"].replace("Z", "+00:00")).astimezone(UK).strftime("%a %d %b, %H:%M")
                await client.post(f"{TG_API}/sendMessage", json={"chat_id": SAM_CHAT_ID,
                                  "text": f"New 1-1 call booked\n\n{name}\n{when} (UK time)\n\n{topic or 'No topic given'}"})
            except Exception:
                pass
        return {"call": _call_out(call)}


async def _free_slot(client: httpx.AsyncClient, slot_id) -> None:
    if not slot_id:
        return
    rows = (await client.get(f"{SUPABASE_URL}/rest/v1/journal_items", headers=SB_HEADERS,
                             params={"id": f"eq.{slot_id}", "kind": "eq.call_slot", "select": "id,data"})).json()
    if rows:
        d = {k: v for k, v in (rows[0].get("data") or {}).items() if k not in ("booked", "user_id", "name", "call_id")}
        await client.patch(f"{SUPABASE_URL}/rest/v1/journal_items", headers=JSON_HDR, params={"id": f"eq.{slot_id}"}, json={"data": d})


@app.post("/calls/cancel")
async def calls_cancel(request: Request):
    body = await request.json()
    async with httpx.AsyncClient(timeout=30) as client:
        me = await _elite(request, client)
        rows = (await client.get(f"{SUPABASE_URL}/rest/v1/journal_items", headers=SB_HEADERS,
                                 params={"id": f"eq.{body.get('id', '')}", "kind": "eq.call",
                                         "user_id": f"eq.{me['user']['id']}", "select": "id,data"})).json()
        if not rows or rows[0]["data"].get("status") not in ("booked", "requested", "proposed"):
            raise HTTPException(status_code=404, detail="not_found")
        was = rows[0]["data"].get("status")
        data = {**rows[0]["data"], "status": "cancelled", "cancelled_at": _now_iso()}
        if was in ("requested", "proposed"):
            await client.patch(f"{SUPABASE_URL}/rest/v1/journal_items", headers=JSON_HDR, params={"id": f"eq.{rows[0]['id']}"}, json={"data": data})
            return {"ok": True, "data": data}
        await client.patch(f"{SUPABASE_URL}/rest/v1/journal_items", headers=JSON_HDR, params={"id": f"eq.{rows[0]['id']}"}, json={"data": data})
        if (data.get("at") or "") > _now_iso():
            await _free_slot(client, data.get("slot_id"))
            await _cal_cancel(client, data, "Cancelled from the SDX dashboard")
        return {"ok": True, "data": data}


@app.post("/calls/request")
async def calls_request(request: Request):
    """An Elite member asks for a call and suggests up to three times. Sam books one, suggests another, or declines."""
    body = await request.json()
    topic = str(body.get("topic") or "").strip()[:2000]
    options = []
    for o in (body.get("options") or [])[:3]:
        v = _parse_future(o)
        if v and v not in options:
            options.append(v)
    if not options:
        raise HTTPException(status_code=400, detail="Pick at least one time that's in the future.")
    mins = int(body.get("mins") or 30)
    mins = mins if mins in (20, 30, 45, 60) else 30
    host = str(body.get("host") or "sam")
    if host not in CALL_HOSTS:
        raise HTTPException(status_code=400, detail="Pick who you want to book with.")
    async with httpx.AsyncClient(timeout=30) as client:
        me = await _elite(request, client)
        uid = me["user"]["id"]
        now = _now_iso()
        mine = (await client.get(f"{SUPABASE_URL}/rest/v1/journal_items", headers=SB_HEADERS,
                                 params={"select": "id,data", "kind": "eq.call", "user_id": f"eq.{uid}"})).json()
        for c in mine:
            d = c.get("data") or {}
            if d.get("status") in ("requested", "proposed"):
                raise HTTPException(status_code=409, detail="You've already asked for a call. Wait for an answer on that one first.")
            if d.get("status") == "booked" and (d.get("at") or "") > now:
                raise HTTPException(status_code=409, detail="You already have a call booked. Once it's done you can ask for the next one.")
        data = {"status": "requested", "options": sorted(options), "mins": mins, "topic": topic, "requested_at": now, "source": "request", "host": host}
        made = (await client.post(f"{SUPABASE_URL}/rest/v1/journal_items", headers={**JSON_HDR, "Prefer": "return=representation"},
                                  json={"user_id": uid, "kind": "call", "data": data})).json()
        call = made[0] if isinstance(made, list) and made else None
        if not call:
            raise HTTPException(status_code=500, detail="Couldn't send your request. Try again.")
        name = me["member"].get("name") or me["user"].get("email", "")
        await _tell_host(client, host, f"New 1-1 call request with {CALL_HOSTS[host]}\n\n{name} ({mins} mins)\n" + "\n".join(_uk(o) for o in options) +
                        f" (UK time)\n\n{topic or 'No topic given'}\n\nAnswer it in Admin > 1-1 Calls.")
        return {"call": _call_out(call)}


@app.post("/calls/respond")
async def calls_respond(request: Request):
    """The member says yes to the time Sam suggested."""
    body = await request.json()
    async with httpx.AsyncClient(timeout=30) as client:
        me = await _elite(request, client)
        rows = (await client.get(f"{SUPABASE_URL}/rest/v1/journal_items", headers=SB_HEADERS,
                                 params={"id": f"eq.{body.get('id', '')}", "kind": "eq.call",
                                         "user_id": f"eq.{me['user']['id']}", "select": "id,data"})).json()
        if not rows or rows[0]["data"].get("status") != "proposed":
            raise HTTPException(status_code=404, detail="not_found")
        data = dict(rows[0]["data"])
        if (data.get("at") or "") <= _now_iso():
            raise HTTPException(status_code=409, detail="That time has passed. Ask for a new one.")
        data.update({"status": "booked", "booked_at": _now_iso()})
        await client.patch(f"{SUPABASE_URL}/rest/v1/journal_items", headers=JSON_HDR, params={"id": f"eq.{rows[0]['id']}"}, json={"data": data})
        name = me["member"].get("name") or me["user"].get("email", "")
        await _tell_host(client, data.get("host") or "sam", f"1-1 call confirmed\n\n{name} said yes to {_uk(data['at'])} (UK time).")
        return {"ok": True, "data": data}


@app.post("/calls/calendly")
async def calls_calendly(request: Request):
    """Saves a call the member just booked on Calendly inside the dashboard."""
    body = await request.json()
    ev_uri = str(body.get("event_uri") or "")
    inv_uri = str(body.get("invitee_uri") or "")
    topic = str(body.get("topic") or "").strip()[:2000]
    if not CALENDLY_TOKEN:
        raise HTTPException(status_code=503, detail="Calendly isn't connected yet.")
    if not _cal_uri_ok(ev_uri, "event") or not _cal_uri_ok(inv_uri, "invitee") or _cal_event_of(inv_uri) != ev_uri:
        raise HTTPException(status_code=400, detail="bad_booking")
    async with httpx.AsyncClient(timeout=30) as client:
        me = await _elite(request, client)
        uid = me["user"]["id"]
        mine = (await client.get(f"{SUPABASE_URL}/rest/v1/journal_items", headers=SB_HEADERS,
                                 params={"select": "id,data,created_at", "kind": "eq.call", "user_id": f"eq.{uid}"})).json()
        for c in mine:
            if (c.get("data") or {}).get("calendly_invitee") == inv_uri:
                return {"call": _call_out(c)}
        ev = await _cal_get(client, ev_uri)
        inv = await _cal_get(client, inv_uri)
        if not ev or not inv:
            raise HTTPException(status_code=404, detail="Couldn't find that booking on Calendly.")
        # only save bookings made just now, or made with this member's own email
        email_ok = (inv.get("email") or "").lower() == (me["user"].get("email") or "").lower()
        try:
            fresh = (datetime.now(timezone.utc) - datetime.fromisoformat((inv.get("created_at") or "").replace("Z", "+00:00"))).total_seconds() < 1800
        except ValueError:
            fresh = False
        if not (email_ok or fresh):
            raise HTTPException(status_code=403, detail="That booking isn't yours.")
        if not topic:
            qa = inv.get("questions_and_answers") or []
            topic = "\n".join(str(q.get("answer") or "") for q in qa if q.get("answer")).strip()[:2000]
        now = _now_iso()
        data = {**_cal_fields(ev, inv), "topic": topic, "status": "booked", "booked_at": now, "source": "calendly"}
        made = (await client.post(f"{SUPABASE_URL}/rest/v1/journal_items", headers={**JSON_HDR, "Prefer": "return=representation"},
                                  json={"user_id": uid, "kind": "call", "data": data})).json()
        call = made[0] if isinstance(made, list) and made else None
        if not call:
            raise HTTPException(status_code=500, detail="Booked on Calendly, but couldn't save it to your dashboard. Refresh the page.")
        if SAM_CHAT_ID:
            try:
                name = me["member"].get("name") or me["user"].get("email", "")
                when = datetime.fromisoformat(data["at"].replace("Z", "+00:00")).astimezone(UK).strftime("%a %d %b, %H:%M")
                await client.post(f"{TG_API}/sendMessage", json={"chat_id": SAM_CHAT_ID,
                                  "text": f"New 1-1 call booked (Calendly)\n\n{name}\n{when} (UK time)\n\n{topic or 'No topic given'}"})
            except Exception:
                pass
        return {"call": _call_out(call)}


@app.get("/admin/calls")
async def admin_calls(request: Request):
    async with httpx.AsyncClient(timeout=30) as client:
        await _admin(request, client)
        slots = await sb_all(client, "journal_items", {"select": "id,data", "kind": "eq.call_slot"})
        calls = await sb_all(client, "journal_items", {"select": "id,user_id,data,created_at", "kind": "eq.call"})
        users = (await client.get(f"{SUPABASE_URL}/auth/v1/admin/users", headers=SB_HEADERS,
                                  params={"per_page": "1000"})).json().get("users", [])
        email_by_id = {u["id"]: (u.get("email") or "") for u in users}
        members = (await client.get(f"{SUPABASE_URL}/rest/v1/members", headers=SB_HEADERS,
                                    params={"select": "email,name"})).json()
        name_by_email = {(m.get("email") or "").lower(): m.get("name") for m in members}
        out = []
        for r in calls:
            r["data"] = await _cal_sync(client, r)
            em = email_by_id.get(r["user_id"], "")
            c = _call_out(r)
            c["email"] = em
            c["name"] = name_by_email.get(em.lower()) or em
            if c["data"].get("recording_path"):
                c["data"]["recording_url"] = await _signed_recording(client, c["data"]["recording_path"])
            out.append(c)
        out.sort(key=lambda c: c["data"].get("at") or "")
        return {"slots": sorted([{"id": s["id"], **(s.get("data") or {})} for s in slots], key=lambda s: s.get("at") or ""),
                "calls": out, "calendly": bool(CALENDLY_TOKEN)}


@app.post("/admin/call-slots")
async def admin_call_slots(request: Request):
    body = await request.json()
    async with httpx.AsyncClient(timeout=30) as client:
        me = await _admin(request, client)
        now = _now_iso()
        add = []
        for s in (body.get("add") or [])[:60]:
            at = str(s.get("at") or "")
            try:
                at = datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
            except ValueError:
                continue
            if at <= now:
                continue
            mins = int(s.get("mins") or 30)
            add.append({"user_id": me["user"]["id"], "kind": "call_slot",
                        "data": {"at": at, "mins": max(10, min(mins, 180)), "link": str(s.get("link") or "").strip()[:500]}})
        if add:
            await client.post(f"{SUPABASE_URL}/rest/v1/journal_items", headers=JSON_HDR, json=add)
        for sid in (body.get("remove") or [])[:100]:
            await client.request("DELETE", f"{SUPABASE_URL}/rest/v1/journal_items", headers=SB_HEADERS,
                                 params={"id": f"eq.{sid}", "kind": "eq.call_slot", "data->>booked": "is.null"})
        return {"ok": True, "added": len(add)}


@app.post("/admin/call")
async def admin_call(request: Request):
    body = await request.json()
    async with httpx.AsyncClient(timeout=30) as client:
        await _admin(request, client)
        rows = (await client.get(f"{SUPABASE_URL}/rest/v1/journal_items", headers=SB_HEADERS,
                                 params={"id": f"eq.{body.get('id', '')}", "kind": "eq.call", "select": "id,data"})).json()
        if not rows:
            raise HTTPException(status_code=404, detail="not_found")
        data = dict(rows[0].get("data") or {})
        uid = None
        if any(k in body for k in ("book_at", "propose_at", "move_at", "decline")):
            uid = (await client.get(f"{SUPABASE_URL}/rest/v1/journal_items", headers=SB_HEADERS,
                                    params={"id": f"eq.{rows[0]['id']}", "select": "user_id"})).json()[0]["user_id"]
        msg = str(body.get("message") or "").strip()[:1000]
        tell = ""
        who = _host_name(data)
        if "book_at" in body or "propose_at" in body or "move_at" in body:
            at = _parse_future(body.get("book_at") or body.get("propose_at") or body.get("move_at"))
            if not at:
                raise HTTPException(status_code=400, detail="Pick a time in the future.")
            data["at"] = at
            data["sam_message"] = msg
            if "book_at" in body:
                data.update({"status": "booked", "booked_at": _now_iso()})
                tell = f"Your 1-1 call with {who} is booked for {_uk(at)} (UK time). The details are on your 1-1 Calls page."
            elif "propose_at" in body:
                data["status"] = "proposed"
                tell = f"{who} suggested a new time for your 1-1 call: {_uk(at)} (UK time). Say yes on your 1-1 Calls page."
            else:
                if data.get("status") != "booked":
                    raise HTTPException(status_code=409, detail="Only booked calls can be moved.")
                data["moved_at"] = _now_iso()
                tell = f"{who} moved your 1-1 call to {_uk(at)} (UK time)."
            if msg:
                tell += f"\n\n{msg}"
        if body.get("decline"):
            data.update({"status": "declined", "sam_message": msg, "declined_at": _now_iso()})
            tell = f"{who} can't do the times you asked for." + (f"\n\n{msg}" if msg else "") + "\n\nYou can ask for another time on your 1-1 Calls page."
        for k, cap in (("link", 500), ("notes", 6000), ("recording_link", 1000), ("recording_path", 500)):
            if k in body:
                data[k] = str(body.get(k) or "").strip()[:cap]
        if body.get("status") in ("booked", "done", "cancelled"):
            if body["status"] == "cancelled" and data.get("status") != "cancelled":
                await _free_slot(client, data.get("slot_id"))
                if (data.get("at") or "") > _now_iso():
                    await _cal_cancel(client, data, "Cancelled by Sam")
            data["status"] = body["status"]
        if (data.get("recording_path") or data.get("recording_link")) and data.get("status") == "booked":
            data["status"] = "done"
        if "recording_path" in body or "recording_link" in body:
            data["recording_at"] = _now_iso()
        data.pop("recording_url", None)
        await client.patch(f"{SUPABASE_URL}/rest/v1/journal_items", headers=JSON_HDR, params={"id": f"eq.{rows[0]['id']}"}, json={"data": data})
        if tell and uid:
            await _tell_member(client, uid, tell)
        if data.get("recording_path"):
            data["recording_url"] = await _signed_recording(client, data["recording_path"])
        return {"ok": True, "data": data}


@app.post("/admin/call-upload")
async def admin_call_upload(request: Request):
    """Gives the dashboard a one-off link to upload a call recording straight into a private bucket."""
    body = await request.json()
    async with httpx.AsyncClient(timeout=30) as client:
        await _admin(request, client)
        await client.post(f"{SUPABASE_URL}/storage/v1/bucket", headers=JSON_HDR,
                          json={"id": CALL_BUCKET, "name": CALL_BUCKET, "public": False})  # already there = harmless error
        safe = re.sub(r"[^a-zA-Z0-9._-]", "_", str(body.get("filename") or "recording.mp4"))[-80:]
        path = f"{body.get('id', 'call')}/{int(time.time())}-{safe}"
        r = await client.post(f"{SUPABASE_URL}/storage/v1/object/upload/sign/{CALL_BUCKET}/{path}", headers=JSON_HDR, json={})
        if r.status_code != 200:
            raise HTTPException(status_code=500, detail="Couldn't start the upload.")
        url = r.json().get("url") or ""
        token = url.split("token=")[-1] if "token=" in url else ""
        return {"bucket": CALL_BUCKET, "path": path, "token": token}

@app.post("/admin/access")
async def admin_access(request: Request):
    body = await request.json()
    action = body.get("action")
    if action not in ("revoke", "restore"):
        raise HTTPException(status_code=400, detail="bad_action")
    async with httpx.AsyncClient(timeout=30) as client:
        me = await current_member(request, client)
        if me["member"].get("plan") != "Admin":
            raise HTTPException(status_code=403, detail="admins_only")
        rows = (await client.get(f"{SUPABASE_URL}/rest/v1/members", headers=SB_HEADERS,
                                 params={"email": f"eq.{body.get('email', '')}", "select": "*"})).json()
        if not rows:
            raise HTTPException(status_code=404, detail="not_found")
        m = rows[0]
        if m.get("plan") == "Admin":
            raise HTTPException(status_code=400, detail="cannot_change_admin")
        await client.patch(f"{SUPABASE_URL}/rest/v1/members", headers={**SB_HEADERS, "Content-Type": "application/json"},
                           params={"email": f"eq.{m['email']}"}, json={"active": action == "restore"})
        tg = m.get("telegram_id")
        if tg and CHANNEL_ID:
            if action == "revoke":
                await client.post(f"{TG_API}/banChatMember", json={"chat_id": CHANNEL_ID, "user_id": tg})
            else:
                await client.post(f"{TG_API}/unbanChatMember", json={"chat_id": CHANNEL_ID, "user_id": tg, "only_if_banned": True})
        return {"ok": True}


@app.post("/admin/import-elite")
async def admin_import_elite(request: Request):
    """Give dashboard access to people who bought the Elite Pack before the Kajabi webhook was set up."""
    body = await request.json()
    people = body.get("people") or []
    if not isinstance(people, list) or len(people) > 5000:
        raise HTTPException(status_code=400, detail="bad_list")
    async with httpx.AsyncClient(timeout=60) as client:
        me = await current_member(request, client)
        if me["member"].get("plan") != "Admin":
            raise HTTPException(status_code=403, detail="admins_only")
        existing = await sb_all(client, "members", {"select": "email,plan,active"})
        by_email = {(m.get("email") or "").lower(): m for m in existing}
        added, already, revoked, bad, rows = [], [], [], [], []
        seen = set()
        for p in people:
            email = str((p or {}).get("email") or "").strip().lower()
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or email in seen:
                if email and email not in seen:
                    bad.append(email)
                continue
            seen.add(email)
            m = by_email.get(email)
            if m and m.get("plan") in ("Admin", "Elite Pack"):
                (revoked if m.get("active") is False else already).append(email)
                continue
            name = str((p or {}).get("name") or "").strip() or email.split("@")[0]
            rows.append({"email": email, "name": name, "plan": "Elite Pack", "active": True})
            added.append(email)
        for i in range(0, len(rows), 500):
            r = await client.post(f"{SUPABASE_URL}/rest/v1/members", params={"on_conflict": "email"},
                                  headers={**SB_HEADERS, "Content-Type": "application/json",
                                           "Prefer": "resolution=merge-duplicates,return=minimal"},
                                  json=rows[i:i + 500])
            r.raise_for_status()
        return {"added": added, "already": already, "revoked": revoked, "bad": bad}


# ---------- Kajabi: Elite Pack purchases ----------
def _find(obj, key):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, str) and v:
                return v
        for v in obj.values():
            found = _find(v, key)
            if found:
                return found
    if isinstance(obj, list):
        for v in obj:
            found = _find(v, key)
            if found:
                return found
    return None


def _find_suffix(obj, suffix):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.endswith(suffix) and isinstance(v, str) and "@" in v:
                return v
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            found = _find_suffix(v, suffix)
            if found:
                return found
    return None


@app.post("/kajabi")
async def kajabi(request: Request):
    if not KAJABI_SECRET or request.query_params.get("key") != KAJABI_SECRET:
        raise HTTPException(status_code=403)
    data = await request.json()
    email = (_find(data, "email") or _find_suffix(data, "email") or "").strip().lower()
    if not email:
        return {"ok": True, "note": "no_email"}
    name = _find(data, "name") or _find(data, "first_name") or email.split("@")[0]
    async with httpx.AsyncClient(timeout=30) as client:
        rows = (await client.get(f"{SUPABASE_URL}/rest/v1/members", headers=SB_HEADERS,
                                 params={"email": f"eq.{email}", "select": "plan"})).json()
        if rows and rows[0].get("plan") == "Admin":
            return {"ok": True}
        await client.post(f"{SUPABASE_URL}/rest/v1/members", params={"on_conflict": "email"},
                          headers={**SB_HEADERS, "Content-Type": "application/json",
                                   "Prefer": "resolution=merge-duplicates,return=minimal"},
                          json={"email": email, "name": name, "plan": "Elite Pack", "active": True})
    return {"ok": True}


# ---------- AI coach ----------
COACH_RULES = """You are the SDX Trading Education coach inside the members dashboard.

How you talk:
- Plain, everyday language, like a normal conversation with a trader who is learning. No fancy words.
- Short paragraphs of one to three sentences. Simple numbered steps when you explain a process.
- Confident and direct. Get to the point, no waffle, no hype, no emojis.
- When you use a trading term, say what it means in plain words the first time.
- Never mention any other trading educator, course or brand.

What you help with:
- The SDX trade service: reading signals, lot sizes, risk, and the member's own results.
- Trading concepts: market structure, break of structure (BOS), change of character (CHoCH), swing highs and lows, internal and external structure, liquidity, sweeps, inducements (IDM), points of interest (POI), order blocks (OB), breakers, fair value gaps (FVG), inverse fair value gaps (IFVG), balanced price ranges (BPR), premium and discount, and how sessions and news affect price.
- Explain what each concept is, why it forms, how to spot it on a chart, and how it fits with structure. Give a simple example with made-up prices when it helps.
- Members can send chart screenshots. When they do, say what you can see: the trend and structure, where the breaks of structure are, the obvious liquidity (highs, lows, equal highs and lows), any fair value gaps, order blocks or balanced price ranges, and whether price is in premium or discount. Point to where on the chart you mean (left, right, top, bottom, the big candle, and so on). If the image is unclear or too zoomed in, say so and ask for a clearer one.

The SDX way of seeing things:
- Structure comes first. Every concept is read through market structure, never on its own.
- Liquidity is structural: we focus on the highs and lows that matter to the structure, not every high and low on the chart.
- A sweep is a wick through a level that closes back inside. A break is a candle body closing through and holding.
- Never say support or resistance. Call those levels reaction points.
- Non-USD pairs are called minors.
- Win rate is wins divided by wins plus losses. Breakevens never count.
- Position size is worked out with a lot size calculator (the dashboard's Lot Sizing page, STINU or Myfxbook), never by hand.

Limits:
- Teach concepts, but do not give trade calls: never tell anyone to buy or sell, where to enter right now, or that a trade will win. This applies to charts they send too: explain what is on the chart, do not tell them to take a trade. This is education, not financial advice.
- Do not make up the rules of SDX's own entry models. If someone asks for a specific model's exact rules, tell them it is covered in the SDX education and live sessions.
- Use the member's numbers when they are given.
- Risk guidance: the trade service recommends about 1% risk per trade and the same risk every time.
- Accounts must stay above $500 to keep access; support checks every Friday."""


@app.post("/coach")
async def coach(request: Request):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="coach_off")
    body = await request.json()
    mode = body.get("mode", "chat")
    async with httpx.AsyncClient(timeout=60) as client:
        me = await current_member(request, client)
        uid = me["user"]["id"]
        logs = (await client.get(f"{SUPABASE_URL}/rest/v1/signal_logs", headers=SB_HEADERS,
                                 params={"user_id": f"eq.{uid}", "select": "*"})).json()
        settings = (await client.get(f"{SUPABASE_URL}/rest/v1/account_settings", headers=SB_HEADERS,
                                     params={"user_id": f"eq.{uid}", "select": "*"})).json()
        sigs = (await client.get(f"{SUPABASE_URL}/rest/v1/messages", headers=SB_HEADERS,
                                 params={"kind": "in.(signal,win,loss,be)", "select": "message_id,sent_at,plain,kind,reply_to",
                                         "order": "sent_at.desc", "limit": "40"})).json()
        context = (f"Member's starting balance: {(settings[0] if settings else {}).get('start_balance')}\n"
                   f"Member's logged signals (took, risk %, profit $ or %): {logs}\n"
                   f"Recent trade service messages (signals and results): {sigs}")
        task = {"review": "Review this member's results: follow rate, profit, consistency, and one thing to improve.",
                "risk": "Check this member's risk: is it steady and sensible, did they over-risk or skip signals? Give one clear fix."}.get(mode, "")
        raw = [m for m in body.get("messages", [])[-12:] if m.get("role") in ("user", "assistant") and m.get("content")]
        img_base = f"{SUPABASE_URL}/storage/v1/object/public/journal/"
        msgs = []
        for m in raw:
            imgs = [u for u in (m.get("images") or [])[:3] if isinstance(u, str) and u.startswith(img_base)] if m["role"] == "user" else []
            if imgs:
                content = [{"type": "image", "source": {"type": "url", "url": u}} for u in imgs] + [{"type": "text", "text": str(m["content"])}]
            else:
                content = str(m["content"])
            msgs.append({"role": m["role"], "content": content})
        if not msgs or msgs[-1]["role"] != "user":
            msgs.append({"role": "user", "content": task or "Hi"})
        r = await client.post("https://api.anthropic.com/v1/messages",
                              headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                                       "content-type": "application/json"},
                              json={"model": ANTHROPIC_MODEL, "max_tokens": 1000,
                                    "system": COACH_RULES + "\n\n" + (task + "\n\n" if task else "") + context,
                                    "messages": msgs})
        r.raise_for_status()
        reply = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        return {"reply": reply.strip()}


# ---------- SDX scanners: TradingView alerts -> Supabase + Telegram topics ----------
SCANNER_BOT_TOKEN = os.environ.get("SCANNER_BOT_TOKEN", "")
SCANNER_KEY = os.environ.get("SCANNER_KEY", "")
SCANNER_CHAT_ID = os.environ.get("SCANNER_CHAT_ID", "-1004453278644")
SCANNER_TG = f"https://api.telegram.org/bot{SCANNER_BOT_TOKEN}"
# a scanner can post to its own group instead of the main scanners group
SCANNER_CHATS = {"SDX Failed Break": os.environ.get("FB_CHAT_ID", "")}

# scanner name -> (dashboard id, Telegram topic)
SCANNERS = {
    "SDX Pulse": ("chrome", 17),
    "SDX Sniper": ("platinum", 15),
    "SDX Precision": ("titanium", 11),
    "SDX Phantom": ("obsidian", 6),
    "SDX Raid": ("cobalt", 4),
    "SDX Ghost": ("iridium", 2),
    "SDX Failed Break": ("trap", int(os.environ.get("FB_TOPIC", "0") or 0)),
}


# Failed Break: one Telegram topic per pair, set on Render as FB_TOPICS, e.g. XAUUSD=12,EURUSD=34,GER30=56,GBPUSD=78
FB_PAIRS = ("XAUUSD", "EURUSD", "GER30", "GBPUSD")


def _fb_topics() -> dict:
    out = {}
    for part in os.environ.get("FB_TOPICS", "").replace(" ", "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            if v.isdigit():
                out[k.upper()] = int(v)
    return out


def _pair(d: dict) -> str:
    p = str(d.get("pair") or "").upper()
    if p == "GOLD":
        return "XAUUSD"
    if p in FB_PAIRS:
        return p
    s = str(d.get("symbol") or "").upper()
    if "XAU" in s or "GOLD" in s:
        return "XAUUSD"
    if "EURUSD" in s:
        return "EURUSD"
    if "GBPUSD" in s:
        return "GBPUSD"
    if any(k in s for k in ("GER", "DE30", "DE40", "DAX", "GRXEUR")):
        return "GER30"
    return ""


def _topic_for(name: str, d: dict) -> int:
    """Telegram topic for this message: the pair's own topic for the Failed Break, else the scanner's topic."""
    if name == "SDX Failed Break":
        t = _fb_topics().get(_pair(d))
        if t:
            return t
    return SCANNERS[name][1]


def _tf(tf: str) -> str:
    tf = str(tf)
    if tf.isdigit():
        m = int(tf)
        return f"H{m // 60}" if m >= 60 and m % 60 == 0 else f"M{m}"
    return tf.upper()


def _fmt(x: float, pip: float) -> str:
    tick = pip / 10 if pip else 0.00001
    dec = max(0, round(-math.log10(tick))) if tick < 1 else 0
    return f"{x:.{dec}f}"


def _r(x: float) -> str:
    return f"{x:.1f}".rstrip("0").rstrip(".")


def _fmtc(x: float, pip: float) -> str:
    tick = pip / 10 if pip else 0.00001
    dec = max(0, round(-math.log10(tick))) if tick < 1 else 0
    return f"{x:,.{dec}f}"


def _pips(x: float) -> str:
    return f"{x:,.2f}".rstrip("0").rstrip(".")


FB_RISK = os.environ.get("FB_RISK", "1.5%")


def _fb_pip(d: dict) -> float:
    """Pip size for a Failed Break pair: what the alert sends, else gold 0.1, GER30 1 point, FX 0.0001."""
    try:
        p = float(d.get("pip") or 0)
    except Exception:
        p = 0
    if p:
        return p
    return {"XAUUSD": 0.1, "GER30": 1.0}.get(_pair(d), 0.0001)


def scanner_text(ev: str, d: dict, name: str) -> str:
    pip = float(d.get("pip") or 0) or 0.0001
    if name == "SDX Failed Break" and ev == "new":
        e, sl, tp = (float(d[k]) for k in ("entry", "sl", "tp"))
        order = "NOW" if d.get("order", "now") == "now" else "LIMIT"
        return (f"{d['symbol']} {d['side'].upper()} {order} \n\n@{_fmtc(e, pip)} \n\n"
                f"SL {_fmtc(sl, pip)} ( {_pips(abs(e - sl) / pip)} pips ) \n\n"
                f"TP {_fmtc(tp, pip)} ( {_pips(abs(tp - e) / pip)} pips )\n\n"
                f"Risk ={FB_RISK}")
    if name == "SDX Failed Break" and ev == "ready":
        lvl, ext = float(d["entry"]), float(d["sl"])
        buy = d["side"] == "buy"
        tfn = _tf(d["tf"])
        return (f"{d['symbol']} {'BUY' if buy else 'SELL'} SETUP FORMING ( {tfn} )\n\n"
                f"Failed break of {_fmtc(lvl, pip)} ✅\n\n"
                f"If the next {tfn} candle closes {'bullish' if buy else 'bearish'}, {'buy' if buy else 'sell'} at the close\n\n"
                f"SL will be just {'below' if buy else 'above'} {_fmtc(ext, pip)}\n\n"
                f"Get ready")
    if name == "SDX Failed Break" and ev == "cancel":
        return "No entry ❌\n\nThe setup didn't confirm, no trade"
    if name == "SDX Failed Break" and ev == "rf":
        return "Risk free point hit ✅\n\nGo risk free"
    if name == "SDX Failed Break" and ev in ("tp", "sl"):
        e, sl, tp = (float(d[k]) for k in ("entry", "sl", "tp"))
        fp = _fb_pip(d)
        if ev == "sl":
            return f"SL HIT -1R ( -{_pips(abs(e - sl) / fp)} pips ) ❌"
        head = (f"Take Profit Hit ✅\n\n+{_r(abs(tp - e) / abs(e - sl))}R ( +{_pips(abs(tp - e) / fp)} pips ) "
                f"Secured on this position")
        if d.get("month_r") is None:
            return head
        m = float(d["month_r"])
        state = f"now in profit off {_r(m)}R" if m > 0 else ("now flat" if m == 0 else f"now down {_r(abs(m))}R")
        return f"{head} , {state} on the month due to risking {FB_RISK}"
    e, sl, tp, rf = (float(d[k]) for k in ("entry", "sl", "tp", "rf"))
    side = d["side"].upper()
    multi = all(k in d for k in ("tp1", "tp2", "tp3"))
    two = not multi and all(k in d for k in ("tp1", "tp2"))
    if two:
        part = int(float(d.get("part1") or 50))
        if ev == "new":
            order = "NOW" if d.get("order", "now") == "now" else "LIMIT"
            t1, t2 = float(d["tp1"]), float(d["tp2"])
            return (f"{name} | {_tf(d['tf'])}\n\n{d['symbol']} {side} {order}\n\n@{_fmt(e, pip)}\n\n"
                    f"SL {_fmt(sl, pip)} ( {round(abs(e - sl) / pip)} pips )\n\n"
                    f"TP1 {_fmt(t1, pip)} ( {round(abs(t1 - e) / pip)} pips )\n"
                    f"TP2 {_fmt(t2, pip)} ( {round(abs(t2 - e) / pip)} pips )\n\n"
                    f"Close {part}% at TP1 and move SL to entry. Let the rest run to TP2")
        if ev == "tp1":
            return f"TP1 hit ✅\n\nClose {part}% and move SL to {_fmt(e, pip)} ( entry )"
        if ev == "tp2":
            return f"TP2 hit ✅\n\nAll targets hit, +{_r(float(d.get('r') or 0))}R Secured on this position"
    if ev == "new" and multi:
        order = "NOW" if d.get("order", "now") == "now" else "LIMIT"
        t1, t2, t3 = (float(d[k]) for k in ("tp1", "tp2", "tp3"))
        return (f"{name} | {_tf(d['tf'])}\n\n{d['symbol']} {side} {order}\n\n@{_fmt(e, pip)}\n\n"
                f"SL {_fmt(sl, pip)} ( {round(abs(e - sl) / pip)} pips )\n\n"
                f"TP1 {_fmt(t1, pip)} ( {round(abs(t1 - e) / pip)} pips )\n"
                f"TP2 {_fmt(t2, pip)} ( {round(abs(t2 - e) / pip)} pips )\n"
                f"TP3 {_fmt(t3, pip)} ( {round(abs(t3 - e) / pip)} pips )\n\n"
                f"Close 1/3 at each TP. SL to entry at TP1")
    if ev == "tp1":
        return f"TP1 hit ✅\n\nClose 1/3 and move SL to {_fmt(e, pip)} ( entry )"
    if ev == "tp2":
        return "TP2 hit ✅\n\nClose another 1/3, let the rest run to TP3"
    if ev == "tp3":
        return f"TP3 hit ✅\n\nAll targets hit, +{_r(float(d.get('r') or 0))}R Secured on this position"
    if ev == "be" and d.get("r") is not None and float(d.get("r") or 0) > 0:
        return f"Rest of the position closed at entry ✅\n\n+{_r(float(d['r']))}R Secured on this position"
    if ev == "new":
        order = "NOW" if d.get("order", "now") == "now" else "LIMIT"
        return (f"{name} | {_tf(d['tf'])}\n\n{d['symbol']} {side} {order}\n\n@{_fmt(e, pip)}\n\n"
                f"SL {_fmt(sl, pip)} ( {round(abs(e - sl) / pip)} pips )\n\n"
                f"TP {_fmt(tp, pip)} ( {round(abs(tp - e) / pip)} pips )\n\n"
                f"Risk free point @ {_fmt(rf, pip)}")
    if ev == "rf":
        return f"Risk free point hit ✅\n\nMove SL to {_fmt(e, pip)} ( entry )"
    if ev == "tp":
        return f"Take Profit Hit ✅\n\n+{_r(abs(tp - e) / abs(e - sl))}R Secured on this position"
    if ev == "be":
        return "Breakeven hit - No P/L secured"
    if ev == "sl":
        return "SL HIT -1R ❌"
    if ev == "cancel":
        return "Setup cancelled - remove the limit order"
    if ev == "close":
        r = float(d.get("r") or 0)
        return f"Trade closed at market (time limit) {'+' if r > 0 else ''}{_r(r)}R"
    return ""


@app.get("/scanner/demo")
async def scanner_demo(key: str = "", symbol: str = "XAUUSD", price: float = 4123.45, side: str = "sell", result: str = "tp"):
    """Open this link in a browser to post an example trade (get ready, entry, risk free, result) in the Failed Break channel."""
    if not SCANNER_KEY or key != SCANNER_KEY:
        raise HTTPException(status_code=403)
    t = asyncio.create_task(_demo(symbol, price, side, result))
    _TASKS.add(t)
    t.add_done_callback(_TASKS.discard)
    return {"ok": True, "note": "Example trade is posting now, one message every few seconds."}


async def _demo(symbol: str, price: float, side: str, result: str):
    name = "SDX Failed Break"
    d = 1 if side == "buy" else -1
    pip = 0.1 if price > 100 else 0.0001
    risk = 47 * pip
    e = price
    base = {"symbol": symbol, "tf": "15", "side": "buy" if d == 1 else "sell", "pip": str(pip), "order": "now",
            "entry": str(e), "sl": str(e - d * risk), "tp": str(e + d * 4 * risk), "rf": str(e + d * 1.5 * risk)}
    ready = {**base, "entry": str(e - d * 0.8 * risk), "sl": str(e - d * 0.95 * risk)}
    try:
        import chart
        cs, swing, sl_tip = chart.demo_candles(e, base["side"], pip)
        risk = abs(e - sl_tip)
        base = {**base, "sl": str(sl_tip), "tp": str(e + d * 4 * risk), "rf": str(e + d * 1.5 * risk),
                "candles": cs, "lvl": str(swing), "t0": str(int((datetime.now(timezone.utc).timestamp() - 60 * 15 * 60) * 1000))}
        ready = {**ready, "entry": str(swing), "sl": str(sl_tip)}
    except Exception as exc:
        print("demo chart failed:", repr(exc), flush=True)
    steps = [("ready", ready), ("new", base), ("rfat", base), ("rf", base)]
    steps.append(("tp", {**base, "month_r": 4.0}) if result == "tp" else ("be", {**base, "r": "0"}) if result == "be" else ("sl", base))
    chat = SCANNER_CHATS.get(name) or SCANNER_CHAT_ID
    topic = _topic_for(name, base)
    prev = None
    async with httpx.AsyncClient(timeout=30) as client:
        for ev, data in steps:
            if result == "sl" and ev == "rf":
                continue
            if ev == "rfat" and False:
                continue
            text = f"Risk free point @{_fmtc(float(data['rf']), pip)}" if ev == "rfat" else scanner_text(ev, data, name)
            msg = {"chat_id": chat, "text": "EXAMPLE - NOT A LIVE TRADE\n\n" + text}
            if topic:
                msg["message_thread_id"] = topic
            if prev:
                msg["reply_parameters"] = {"message_id": prev, "allow_sending_without_reply": True}
            try:
                mid = await _tg_send(client, msg, _chart(data) if ev == "new" else None)
                if ev in ("ready", "new"):
                    prev = mid or prev
            except Exception as exc:
                print("demo failed:", repr(exc), flush=True)
            await asyncio.sleep(4)


async def _tg_send(client, msg: dict, photo: bytes = None):
    """Send a text message, or a photo with the text as its caption. Returns the message id."""
    if photo:
        data = {"chat_id": str(msg["chat_id"]), "caption": msg["text"]}
        if msg.get("message_thread_id"):
            data["message_thread_id"] = str(msg["message_thread_id"])
        if msg.get("reply_parameters"):
            data["reply_parameters"] = json.dumps(msg["reply_parameters"])
        sent = (await client.post(f"{SCANNER_TG}/sendPhoto", data=data, files={"photo": ("chart.png", photo, "image/png")})).json()
        if sent.get("ok"):
            return sent["result"]["message_id"]
        print("photo failed, sending text:", sent.get("description"), flush=True)
    sent = (await client.post(f"{SCANNER_TG}/sendMessage", json=msg)).json()
    if not sent.get("ok") and msg.get("reply_parameters"):
        # Telegram refused it (for example the trade message it replies to is gone): send it on its own instead
        print("telegram refused reply, sending plain:", sent.get("description"), flush=True)
        sent = (await client.post(f"{SCANNER_TG}/sendMessage", json={k: v for k, v in msg.items() if k != "reply_parameters"})).json()
    if not sent.get("ok"):
        print("telegram send failed:", sent.get("description"), msg.get("text", "")[:60], flush=True)
    return (sent.get("result") or {}).get("message_id")


def _chart(d: dict):
    if not d.get("candles"):
        return None
    try:
        import chart
        return chart.draw(d)
    except Exception as exc:
        print("chart failed:", repr(exc), flush=True)
        return None


@app.get("/scanner/fb-topics")
async def scanner_fb_topics(key: str = ""):
    """Makes one topic per pair (XAUUSD, EURUSD, GER30, GBPUSD) in the Failed Break group and says what to put in FB_TOPICS."""
    if not SCANNER_KEY or key != SCANNER_KEY:
        raise HTTPException(status_code=403)
    chat = SCANNER_CHATS.get("SDX Failed Break") or SCANNER_CHAT_ID
    have = _fb_topics()
    made, errors = {}, {}
    async with httpx.AsyncClient(timeout=30) as client:
        for p in FB_PAIRS:
            if have.get(p):
                made[p] = have[p]
                continue
            r = (await client.post(f"{SCANNER_TG}/createForumTopic", json={"chat_id": chat, "name": p})).json()
            if r.get("ok"):
                made[p] = r["result"]["message_thread_id"]
                await client.post(f"{SCANNER_TG}/sendMessage", json={"chat_id": chat, "message_thread_id": made[p],
                                                                     "text": f"SDX Failed Break | {p}\n\n{p} trades will post here."})
            else:
                errors[p] = r.get("description", "failed")
    line = ",".join(f"{p}={made[p]}" for p in FB_PAIRS if p in made)
    return {"ok": not errors, "FB_TOPICS": line, "errors": errors,
            "next": "On Render, add an environment variable called FB_TOPICS with the value above, then save." if line else ""}


_SEEN = {}
_TASKS = set()
_READY = {}
_SCAN_LOCK = asyncio.Lock()


@app.post("/scanner")
async def scanner(request: Request):
    raw = await request.body()
    try:
        d = json.loads(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="bad_json")
    if not SCANNER_KEY or d.get("key") != SCANNER_KEY:
        raise HTTPException(status_code=403)
    name, ev, sid = d.get("scanner"), d.get("event"), d.get("id")
    print("scanner alert in:", name, ev, d.get("symbol"), d.get("tf"), sid, flush=True)
    # same alert arriving twice (two copies of the alert, or TradingView retrying): only post it once
    _k = (name, ev, sid, d.get("tf"), str(d.get("symbol") or "").upper())
    _now = time.time()
    for _o in [k for k, t in _SEEN.items() if _now - t > 600]:
        _SEEN.pop(_o, None)
    if _k in _SEEN:
        return {"ok": True, "note": "duplicate"}
    _SEEN[_k] = _now
    if name not in SCANNERS or ev not in ("new", "rf", "be", "tp", "sl", "cancel", "tp1", "tp2", "tp3", "close", "test", "ready") or not sid:
        raise HTTPException(status_code=400, detail="bad_signal")
    # answer TradingView straight away (it gives up after a few seconds), then post in the background, in order
    _t = asyncio.create_task(_scanner_work(d, name, ev, sid))
    _TASKS.add(_t)
    _t.add_done_callback(_TASKS.discard)
    return {"ok": True, "queued": True}


async def _scanner_work(d: dict, name: str, ev: str, sid: str):
    async with _SCAN_LOCK:
        try:
            await _scanner_post(d, name, ev, sid)
        except Exception as exc:
            print("scanner post failed:", name, ev, sid, repr(exc), flush=True)


async def _scanner_post(d: dict, name: str, ev: str, sid: str):
    dash_id = SCANNERS[name][0]
    topic = _topic_for(name, d)
    if ev == "test":
        if not SCANNER_BOT_TOKEN:
            return {"ok": False, "note": "SCANNER_BOT_TOKEN is not set on the server"}
        msg = {"chat_id": SCANNER_CHATS.get(name) or SCANNER_CHAT_ID, "text": f"{name} is connected ✅\n\n{d.get('symbol', '')} {_tf(d.get('tf', ''))} - trades will post here."}
        if topic:
            msg["message_thread_id"] = topic
        async with httpx.AsyncClient(timeout=30) as client:
            sent = (await client.post(f"{SCANNER_TG}/sendMessage", json=msg)).json()
        return {"ok": bool(sent.get("ok")), "telegram": sent.get("description", "")}
    if name == "SDX Failed Break" and ev in ("ready", "cancel"):
        msg = {"chat_id": SCANNER_CHATS.get(name) or SCANNER_CHAT_ID, "text": scanner_text(ev, d, name)}
        if topic:
            msg["message_thread_id"] = topic
        if ev == "cancel" and _READY.get(sid):
            msg["reply_parameters"] = {"message_id": _READY[sid], "allow_sending_without_reply": True}
        async with httpx.AsyncClient(timeout=30) as client:
            sent = (await client.post(f"{SCANNER_TG}/sendMessage", json=msg)).json()
        if ev == "ready":
            _READY[sid] = (sent.get("result") or {}).get("message_id")
            if len(_READY) > 200:
                _READY.pop(next(iter(_READY)))
        return
    hdr = {**SB_HEADERS, "Content-Type": "application/json"}
    now = datetime.now(timezone.utc).isoformat()

    async with httpx.AsyncClient(timeout=30) as client:
        rows = (await client.get(f"{SUPABASE_URL}/rest/v1/scanner_signals", headers=SB_HEADERS,
                                 params={"setup_id": f"eq.{sid}", "select": "*"})).json()
        row = rows[0] if rows else None
        if ev == "new" and row:
            return {"ok": True, "note": "duplicate"}

        if name == "SDX Failed Break" and ev == "tp":
            try:
                start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
                done = (await client.get(f"{SUPABASE_URL}/rest/v1/scanner_signals", headers=SB_HEADERS,
                                         params={"scanner": f"eq.{dash_id}", "created_at": f"gte.{start}",
                                                 "status": "in.(tp,sl,be)", "select": "entry,sl,tp,status,setup_id"})).json()
                mr = 0.0
                for x in done:
                    if x.get("setup_id") == sid:
                        continue
                    if x["status"] == "tp":
                        mr += abs(float(x["tp"]) - float(x["entry"])) / abs(float(x["entry"]) - float(x["sl"]))
                    elif x["status"] == "sl":
                        mr -= 1
                mr += abs(float(d["tp"]) - float(d["entry"])) / abs(float(d["entry"]) - float(d["sl"]))
                d = {**d, "month_r": round(mr, 1)}
            except Exception as exc:
                print("month R failed:", repr(exc), flush=True)
        msg = {"chat_id": SCANNER_CHATS.get(name) or SCANNER_CHAT_ID, "text": scanner_text(ev, d, name)}
        if topic:
            msg["message_thread_id"] = topic
        if row and row.get("tg_msg"):
            msg["reply_parameters"] = {"message_id": row["tg_msg"], "allow_sending_without_reply": True}
        elif ev == "new" and _READY.get(sid):
            msg["reply_parameters"] = {"message_id": _READY[sid], "allow_sending_without_reply": True}
        tg_msg = None
        if SCANNER_BOT_TOKEN:
            fb_new = name == "SDX Failed Break" and ev == "new"
            tg_msg = await _tg_send(client, msg, _chart(d) if fb_new else None)
            if fb_new and float(d.get("rf") or 0) > 0:
                rf_msg = {"chat_id": msg["chat_id"], "text": f"Risk free point @{_fmtc(float(d['rf']), float(d.get('pip') or 0.0001))}"}
                if topic:
                    rf_msg["message_thread_id"] = topic
                if tg_msg:
                    rf_msg["reply_parameters"] = {"message_id": tg_msg, "allow_sending_without_reply": True}
                await _tg_send(client, rf_msg)

        if ev == "new":
            await client.post(f"{SUPABASE_URL}/rest/v1/scanner_signals", headers={**hdr, "Prefer": "return=minimal"},
                              json={"scanner": dash_id, "symbol": d["symbol"], "tf": _tf(d["tf"]), "side": d["side"],
                                    "entry": float(d["entry"]), "sl": float(d["sl"]), "tp": float(d["tp"]),
                                    "rf": float(d["rf"]), "status": "triggered" if d.get("order", "now") == "now" else "waiting",
                                    "setup_id": sid, "tg_msg": tg_msg})
        else:
            patch = {"updated_at": now}
            if ev in ("rf", "tp1"):
                patch["rf_hit"] = True
            elif ev == "tp2" and not ("tp1" in d and "tp2" in d and "tp3" not in d):
                pass
            else:
                r = float(d.get("r") or 0)
                close_st = "tp" if r > 0 else "sl" if r < 0 else "be"
                patch["status"] = {"tp": "tp", "tp2": "tp", "tp3": "tp", "sl": "sl", "be": "be", "cancel": "cancelled", "close": close_st}[ev]
            await client.patch(f"{SUPABASE_URL}/rest/v1/scanner_signals", headers={**hdr, "Prefer": "return=minimal"},
                               params={"setup_id": f"eq.{sid}"}, json=patch)
    return {"ok": True}


# ---------- daily / weekly / monthly results for the Failed Break channel ----------
from datetime import timedelta
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")
SUMMARY_SCANNER = "SDX Failed Break"
SUMMARY_TIME = os.environ.get("SUMMARY_TIME", "21:00")  # UK time, weekdays
_POSTED = set()


def _risk_pct() -> float:
    try:
        return float(FB_RISK.strip().rstrip("%"))
    except Exception:
        return 0.0


def _trade_r(x: dict) -> float:
    if x["status"] == "sl":
        return -1.0
    if x["status"] == "tp":
        return abs(float(x["tp"]) - float(x["entry"])) / abs(float(x["entry"]) - float(x["sl"]))
    return 0.0


def _rtxt(r: float) -> str:
    return "0R" if round(r, 1) == 0 else f"{'+' if r > 0 else '-'}{_r(abs(r))}R"


def _ptxt(r: float) -> str:
    p = r * _risk_pct()
    return "0%" if round(p, 2) == 0 else f"{'+' if p > 0 else '-'}{_r(abs(p))}%"


def _period(kind: str, now: datetime):
    """Start and end (UK time) of the day, week or month that `now` is in."""
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "day":
        return day, day + timedelta(days=1)
    if kind == "week":
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=7)
    start = day.replace(day=1)
    return start, (start + timedelta(days=32)).replace(day=1)


async def _closed_trades(client, start: datetime, end: datetime) -> list:
    dash_id = SCANNERS[SUMMARY_SCANNER][0]
    rows = (await client.get(f"{SUPABASE_URL}/rest/v1/scanner_signals", headers=SB_HEADERS,
                             params=[("scanner", f"eq.{dash_id}"), ("status", "in.(tp,sl,be)"),
                                     ("updated_at", f"gte.{start.astimezone(timezone.utc).isoformat()}"),
                                     ("updated_at", f"lt.{end.astimezone(timezone.utc).isoformat()}"),
                                     ("select", "entry,sl,tp,status,updated_at"), ("order", "updated_at.asc")])).json()
    return rows if isinstance(rows, list) else []


def summary_text(kind: str, now: datetime, rows: list) -> str:
    start, _ = _period(kind, now)
    title = {"day": f"DAILY RESULTS - {now:%a %-d %b}",
             "week": f"WEEKLY RESULTS - w/c {start:%-d %b}",
             "month": f"MONTHLY RESULTS - {now:%B %Y}"}[kind]
    if not rows:
        return f"{title}\n\nNo trades closed. No setups met the rules, so no trades were forced."
    wins = sum(1 for x in rows if x["status"] == "tp")
    losses = sum(1 for x in rows if x["status"] == "sl")
    bes = sum(1 for x in rows if x["status"] == "be")
    total = sum(_trade_r(x) for x in rows)
    wr = f"{round(100 * wins / (wins + losses))}%" if wins + losses else "-"
    out = (f"{title}\n\nTrades: {len(rows)}\nWins: {wins} | Losses: {losses} | Breakeven: {bes}\n"
           f"Win rate: {wr}\n\n{_rtxt(total)} ( {_ptxt(total)} )")
    if kind in ("week", "month"):
        groups = {}
        for x in rows:
            t = datetime.fromisoformat(x["updated_at"].replace("Z", "+00:00")).astimezone(UK)
            key = f"{t:%a}" if kind == "week" else f"w/c {(t - timedelta(days=t.weekday())):%-d %b}"
            groups[key] = groups.get(key, 0.0) + _trade_r(x)
        out += "\n\n" + "\n".join(f"{k}: {_rtxt(v)}" for k, v in groups.items())
    return out


async def post_summary(kind: str, now: datetime = None) -> dict:
    now = now or datetime.now(UK)
    start, end = _period(kind, now)
    async with httpx.AsyncClient(timeout=30) as client:
        rows = await _closed_trades(client, start, end)
        msg = {"chat_id": SCANNER_CHATS.get(SUMMARY_SCANNER) or SCANNER_CHAT_ID, "text": summary_text(kind, now, rows)}
        topic = SCANNERS[SUMMARY_SCANNER][1]
        if topic:
            msg["message_thread_id"] = topic
        await _tg_send(client, msg)
    return {"ok": True, "text": msg["text"]}


def _due(now: datetime) -> list:
    """Which summaries are due right now: daily every weekday, weekly on Friday, monthly on the last weekday of the month."""
    if now.weekday() >= 5 or now.strftime("%H:%M") < SUMMARY_TIME:
        return []
    due = ["day"]
    if now.weekday() == 4:
        due.append("week")
    nxt = now + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    if nxt.month != now.month:
        due.append("month")
    return due


async def _summary_loop():
    while True:
        try:
            now = datetime.now(UK)
            for kind in _due(now):
                key = (kind, now.date())
                if key not in _POSTED:
                    _POSTED.add(key)
                    await post_summary(kind, now)
                    await asyncio.sleep(3)
        except Exception as exc:
            print("summary failed:", repr(exc), flush=True)
        await asyncio.sleep(60)


@app.on_event("startup")
async def _start_summaries():
    if SCANNER_BOT_TOKEN:
        t = asyncio.create_task(_summary_loop())
        _TASKS.add(t)


@app.get("/scanner/summary")
async def scanner_summary(key: str = "", kind: str = "day", preview: int = 0):
    """Post (or with preview=1, just show) the daily / weekly / monthly results now."""
    if not SCANNER_KEY or key != SCANNER_KEY:
        raise HTTPException(status_code=403)
    if kind not in ("day", "week", "month"):
        raise HTTPException(status_code=400, detail="kind must be day, week or month")
    now = datetime.now(UK)
    if preview:
        start, end = _period(kind, now)
        async with httpx.AsyncClient(timeout=30) as client:
            rows = await _closed_trades(client, start, end)
        return {"ok": True, "text": summary_text(kind, now, rows)}
    _POSTED.add((kind, now.date()))  # so the automatic one doesn't post it again tonight
    return await post_summary(kind, now)


# ---------- Failed Break trade log as CSV, for the Google Sheets tracker (=IMPORTDATA) ----------
@app.get("/scanner/trades.csv")
async def scanner_trades_csv():
    """Every Failed Break trade that was entered: open ones and closed ones. Read-only, no key needed
    (these trades are already posted in the Telegram group)."""
    import csv
    import io
    from fastapi.responses import PlainTextResponse
    dash_id = SCANNERS[SUMMARY_SCANNER][0]
    async with httpx.AsyncClient(timeout=30) as client:
        rows = (await client.get(f"{SUPABASE_URL}/rest/v1/scanner_signals", headers=SB_HEADERS,
                                 params=[("scanner", f"eq.{dash_id}"), ("status", "in.(triggered,tp,sl,be)"),
                                         ("select", "created_at,updated_at,symbol,tf,side,entry,sl,tp,rf,rf_hit,status"),
                                         ("order", "created_at.asc")])).json()
    if not isinstance(rows, list):
        rows = []
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Date", "Time (UK)", "Pair", "Timeframe", "Direction", "Entry", "SL", "TP",
                "Risk free hit", "Result", "R"])
    names = {"tp": "Win", "sl": "Loss", "be": "Breakeven", "triggered": "Open"}
    for x in rows:
        t = datetime.fromisoformat(x["created_at"].replace("Z", "+00:00")).astimezone(UK)
        pair = _pair({"symbol": x.get("symbol")}) or str(x.get("symbol") or "")
        r = "" if x["status"] == "triggered" else round(_trade_r(x), 2)
        w.writerow([t.strftime("%Y-%m-%d"), t.strftime("%H:%M"), pair, x.get("tf") or "",
                    "Buy" if x.get("side") == "buy" else "Sell", x["entry"], x["sl"], x["tp"],
                    "Yes" if x.get("rf_hit") else "No", names.get(x["status"], x["status"]), r])
    return PlainTextResponse(out.getvalue(), media_type="text/csv")


@app.get("/scanner/backtest.csv")
async def scanner_backtest_csv():
    """Failed Break Mech Model backtest trades (same trades as the dashboard's Mech groups), for the Google Sheets tracker."""
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest.csv"), media_type="text/csv")
