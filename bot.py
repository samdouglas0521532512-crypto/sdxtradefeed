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
        elif k != "scanreq":
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
Talk in plain, everyday language, like a normal conversation. No jargon, no fancy words, no emojis.
You help members with the SDX trade service: reading signals, lot sizes, risk, and their own results.
Do not teach trading strategies and do not give financial advice or tell anyone to buy or sell.
Keep answers short and practical. Use the member's numbers when they are given.
Risk guidance: the trade service recommends about 1% risk per trade and the same risk every time.
Accounts must stay above $500 to keep access; support checks every Friday."""


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
        msgs = [m for m in body.get("messages", [])[-12:] if m.get("role") in ("user", "assistant") and m.get("content")]
        if not msgs or msgs[-1]["role"] != "user":
            msgs.append({"role": "user", "content": task or "Hi"})
        r = await client.post("https://api.anthropic.com/v1/messages",
                              headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                                       "content-type": "application/json"},
                              json={"model": ANTHROPIC_MODEL, "max_tokens": 700,
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


def scanner_text(ev: str, d: dict, name: str) -> str:
    pip = float(d.get("pip") or 0) or 0.0001
    if name == "SDX Failed Break" and ev == "new":
        e, sl, tp = (float(d[k]) for k in ("entry", "sl", "tp"))
        order = "NOW" if d.get("order", "now") == "now" else "LIMIT"
        return (f"{d['symbol']} {d['side'].upper()} {order} \n\n@{_fmtc(e, pip)} \n\n"
                f"SL {_fmtc(sl, pip)} ( {_pips(abs(e - sl) / pip)} pips ) \n\n"
                f"TP {_fmtc(tp, pip)} ( {_pips(abs(tp - e) / pip)} pips )\n\n"
                f"Risk ={FB_RISK}\n\n"
                f"Risk free point @{_fmtc(float(d['rf']), pip)}")
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
        return "Risk free point hit ✅"
    if name == "SDX Failed Break" and ev == "tp" and d.get("month_r") is not None:
        e, sl, tp = (float(d[k]) for k in ("entry", "sl", "tp"))
        m = float(d["month_r"])
        state = f"now in profit off {_r(m)}R" if m > 0 else ("now flat" if m == 0 else f"now down {_r(abs(m))}R")
        return (f"Take Profit Hit ✅\n\n+{_r(abs(tp - e) / abs(e - sl))}R Secured on this position , "
                f"{state} on the month due to risking {FB_RISK}")
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
    steps = [("ready", ready), ("new", base), ("rf", base)]
    steps.append(("tp", {**base, "month_r": 4.0}) if result == "tp" else ("be", {**base, "r": "0"}) if result == "be" else ("sl", base))
    chat = SCANNER_CHATS.get(name) or SCANNER_CHAT_ID
    topic = SCANNERS[name][1]
    prev = None
    async with httpx.AsyncClient(timeout=30) as client:
        for ev, data in steps:
            if result == "sl" and ev == "rf":
                continue
            msg = {"chat_id": chat, "text": "EXAMPLE - NOT A LIVE TRADE\n\n" + scanner_text(ev, data, name)}
            if topic:
                msg["message_thread_id"] = topic
            if prev:
                msg["reply_parameters"] = {"message_id": prev, "allow_sending_without_reply": True}
            try:
                sent = (await client.post(f"{SCANNER_TG}/sendMessage", json=msg)).json()
                prev = (sent.get("result") or {}).get("message_id") or prev
            except Exception as exc:
                print("demo failed:", repr(exc), flush=True)
            await asyncio.sleep(4)


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
    # same alert arriving twice (two copies of the alert, or TradingView retrying): only post it once
    _k = (name, ev, sid, d.get("tf"))
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
    dash_id, topic = SCANNERS[name]
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
            sent = (await client.post(f"{SCANNER_TG}/sendMessage", json=msg)).json()
            tg_msg = (sent.get("result") or {}).get("message_id")

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
