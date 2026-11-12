"""
SDX Trade Feed bot.
Saves every post from the SDX Trade Ideas channel (and any chart) into Supabase.
"""
import html
import os
import re
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()
BUCKET = os.environ.get("CHARTS_BUCKET", "charts")

TG_API = f"https://api.telegram.org/bot{TOKEN}"
TG_FILES = f"https://api.telegram.org/file/bot{TOKEN}"
SB_HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}

app = FastAPI()

TAGS = {
    "bold": ("<b>", "</b>"),
    "italic": ("<i>", "</i>"),
    "underline": ("<u>", "</u>"),
    "strikethrough": ("<s>", "</s>"),
    "code": ("<code>", "</code>"),
    "pre": ("<pre>", "</pre>"),
}


def to_html(text, entities):
    if not text:
        return ""
    raw = text.encode("utf-16-le")
    length = len(raw) // 2

    def piece(a, b):
        return raw[a * 2 : b * 2].decode("utf-16-le")

    opens, closes = {}, {}
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


def classify(plain):
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


async def save_chart(client, post):
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


async def save_message(post, edited):
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
