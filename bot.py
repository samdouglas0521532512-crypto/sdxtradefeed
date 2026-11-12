"""
SDX Trade Feed bot.

Telegram sends every post (and every edit) from the SDX Trade Ideas channel
to this little web app. It saves the message, and any chart image, into
Supabase. The SDX dashboard reads from the same table and shows it live.
"""
import html
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
        return {"members": rows}


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
