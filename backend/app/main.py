from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .config import PERSISTED_FIELDS, SECRET_FIELDS, Settings, get_bootstrap_settings
from .db import Database
from .media import DetectedInput, clip_context, detect_input
from .mistral import MistralClient, MistralConversationNotFound
from .telegram import TelegramClient

bootstrap = get_bootstrap_settings()
db = Database(bootstrap.database_path)
logger = logging.getLogger("mistral.messenger")
_chat_locks: dict[str, asyncio.Lock] = {}
_background_tasks: set[asyncio.Task] = set()
_workspace_pairs: dict[str, tuple[str, float]] = {}
_WORKSPACE_PAIR_TTL_SECONDS = 15 * 60
_WORKSPACE_PAIR_RE = re.compile(r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{4}-){5}[0-9A-Fa-f]{4}(?![0-9A-Fa-f])")

settings: Settings
mistral: MistralClient
tg: TelegramClient
bot_username: str | None = None
bot_id: int | None = None
default_mcp_status: dict[str, Any] = {"enabled": False, "ok": False}


def _load_settings() -> Settings:
    base = bootstrap.model_dump()
    stored = db.get_config()
    base.update({k: v for k, v in stored.items() if k in PERSISTED_FIELDS})
    return Settings.model_validate(base)


def _reload_clients() -> None:
    global settings, mistral, tg, default_mcp_status
    settings = _load_settings()
    mistral = MistralClient(
        settings.mistral_api_key,
        settings.mistral_agent_id,
        settings.mistral_agent_version,
        base_url=settings.mistral_api_base,
        timeout=settings.mistral_timeout_seconds,
    )
    tg = TelegramClient(settings.telegram_bot_token)
    default_mcp_status = {"enabled": settings.mistral_mcp_enabled, "ok": False}


_reload_clients()


def _configured() -> bool:
    return bool(settings.mistral_api_key and settings.mistral_agent_id and settings.telegram_bot_token)


def _chat_lock(chat_id: str) -> asyncio.Lock:
    return _chat_locks.setdefault(chat_id, asyncio.Lock())


async def _typing_heartbeat(chat_id: str, stop: asyncio.Event):
    while not stop.is_set():
        await tg.typing(chat_id)
        try:
            await asyncio.wait_for(stop.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            pass


def _exc_text(exc: Exception) -> str:
    return str(exc).strip() or exc.__class__.__name__ or "Unknown error"


def _capture_workspace_pair(chat_id: str, text: str) -> str | None:
    matches = _WORKSPACE_PAIR_RE.findall(str(text or ""))
    if not matches:
        return None
    code = matches[-1].upper()
    _workspace_pairs[str(chat_id)] = (code, time.monotonic() + _WORKSPACE_PAIR_TTL_SECONDS)
    return code


def _active_workspace_pair(chat_id: str) -> str | None:
    item = _workspace_pairs.get(str(chat_id))
    if not item:
        return None
    code, expires_at = item
    if time.monotonic() >= expires_at:
        _workspace_pairs.pop(str(chat_id), None)
        return None
    return code


def _mcp_hardened_prompt(chat_id: str, prompt: str) -> str:
    code = _capture_workspace_pair(chat_id, prompt) or _active_workspace_pair(chat_id)
    if not code or not settings.mistral_mcp_enabled:
        return prompt
    return (
        "[Internal TriForce local-workspace continuity instruction: MCP transport sessions may be recreated between turns, "
        "but the logical TriForce workspace lease is persistent and independent of the browser executor transport. Bootstrap this "
        "conversation by calling workspace_status with workspace_id "
        + code
        + ". On success, preserve the returned workspace_token in conversation/tool context and use workspace_token on later "
        "workspace calls instead of reusing the human pairing ID. The pairing ID is short-lived bootstrap authorization only. "
        "Interpret state=ready as the persistent logical lease. connected=true refers to that logical lease. Treat "
        "transport_state=online/offline and executor_online=true/false as the browser executor state. An offline executor does not "
        "invalidate the lease and must never trigger a new pairing request; report/retry after the browser resumes. access_mode is "
        "read_only or write and must not change because of MCP/session/transport churn. Do not expose the pairing ID or workspace_token "
        "in assistant output. The Mistral connector visibility value shared_workspace is connector scope only and is unrelated to "
        "TriForce workspace lease or access state.]\n\n"
        + prompt
    )


def _conversation_metadata(sender: dict, chat: dict) -> dict:
    metadata = {
        "source": "telegram",
        "telegram_chat_id": str(chat.get("id") or ""),
        "telegram_chat_type": str(chat.get("type") or ""),
        "telegram_chat_title": str(chat.get("title") or "")[:200],
        "telegram_user_id": str(sender.get("id") or ""),
        "telegram_username": str(sender.get("username") or "")[:100],
    }
    return {k: v for k, v in metadata.items() if v}


async def answer_via_mistral(chat_id: str, prompt: str, sender: dict, chat: dict):
    conversation_id = db.get_mistral_conversation(chat_id)
    hardened_prompt = _mcp_hardened_prompt(chat_id, prompt)
    try:
        reply = await mistral.ask(
            hardened_prompt,
            conversation_id=conversation_id,
            metadata=None if conversation_id else _conversation_metadata(sender, chat),
        )
    except MistralConversationNotFound:
        db.clear_mistral_conversation(chat_id)
        reply = await mistral.ask(hardened_prompt, metadata=_conversation_metadata(sender, chat))
    db.set_mistral_conversation(chat_id, reply.conversation_id)
    return reply


def _media_followup(detected: DetectedInput, extracted: str) -> str:
    label = {
        "image": "Image analysis",
        "document": "Document content",
        "text_file": "File content",
        "audio": "Transcript",
    }.get(detected.kind, "Media content")
    context = clip_context(extracted, settings.media_context_max_chars)
    return (
        f"The user sent the Telegram attachment {detected.file_name or 'attachment'!r}.\n"
        f"User instruction: {detected.prompt}\n\n{label}:\n{context}\n\n"
        "Answer the original user instruction once, in natural conversational text unless the user explicitly requests "
        "a structured format. Treat the supplied analysis as internal file context, do not echo preprocessing metadata, "
        "and do not invent details."
    )


async def _answer_detected_input(detected: DetectedInput, chat_id: str, sender: dict, chat: dict):
    if detected.kind == "text":
        return await answer_via_mistral(chat_id, strip_mention(detected.prompt) or "Please answer.", sender, chat)
    if detected.kind == "unsupported":
        raise RuntimeError("Unsupported file type. Images, documents, text/code files and audio are supported.")
    if detected.file_size and detected.file_size > settings.telegram_max_download_bytes:
        raise RuntimeError(
            f"File too large ({detected.file_size / 1_000_000:.1f} MB; limit {settings.telegram_max_download_bytes / 1_000_000:.1f} MB)"
        )
    file_bytes, _ = await tg.download_file(detected.file_id, settings.telegram_max_download_bytes)
    if detected.kind == "image":
        vision_prompt = (
            "Analyze the image as internal context for another assistant. Describe only what is visibly supported. "
            "Be concise but preserve details relevant to the user's request. Use plain text, not JSON or XML. "
            "Do not include meta-commentary, alternative drafts, hidden reasoning, or phrases about producing a final answer. "
            f"The user's request about the image is: {detected.prompt}"
        )
        extracted = await mistral.vision(
            file_bytes,
            detected.mime_type or "image/jpeg",
            vision_prompt,
            settings.mistral_vision_model,
        )
    elif detected.kind == "document":
        extracted = await mistral.ocr(file_bytes, detected.file_name or "document", detected.mime_type or "application/octet-stream", settings.mistral_ocr_model)
    elif detected.kind == "text_file":
        extracted = file_bytes.decode("utf-8", errors="replace")
        if not extracted.strip():
            raise RuntimeError("The text file is empty or unreadable.")
    elif detected.kind == "audio":
        extracted = await mistral.transcribe(file_bytes, detected.file_name or "audio", detected.mime_type or "application/octet-stream", settings.mistral_audio_model)
    else:
        raise RuntimeError(f"Unsupported media route: {detected.kind}")
    return await answer_via_mistral(chat_id, _media_followup(detected, extracted), sender, chat)


async def _process_ai_message(message: dict):
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id"))
    message_id = message.get("message_id")
    detected = detect_input(message)
    async with _chat_lock(chat_id):
        stop = asyncio.Event()
        heartbeat = asyncio.create_task(_typing_heartbeat(chat_id, stop))
        try:
            reply = await _answer_detected_input(detected, chat_id, sender, chat)
            chunks = [reply.text[i:i + 4000] for i in range(0, len(reply.text), 4000)] or ["(empty response)"]
            for index, chunk in enumerate(chunks):
                await tg.send_message(chat_id, chunk, reply_to=message_id if index == 0 else None)
        except Exception as exc:
            logger.exception("AI processing failed chat=%s message=%s kind=%s", chat_id, message_id, detected.kind)
            await tg.send_message(chat_id, f"⚠️ Mistral error: {_exc_text(exc)[:900]}", reply_to=message_id)
        finally:
            stop.set()
            heartbeat.cancel()
            try:
                await heartbeat
            except BaseException:
                pass


def owner_id() -> str | None:
    return settings.owner_telegram_id.strip() or db.get_state("owner_telegram_id")


def is_owner(user_id: int | str | None) -> bool:
    oid = owner_id()
    return bool(oid and user_id is not None and str(user_id) == str(oid))


def strip_mention(text: str) -> str:
    if not bot_username:
        return text.strip()
    return re.sub(rf"@{re.escape(bot_username)}\b", "", text, flags=re.IGNORECASE).strip()


def should_answer(message: dict) -> bool:
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    if chat.get("type") == "private":
        return is_owner(sender.get("id"))
    if not db.group_allowed(str(chat.get("id"))):
        return False
    text = message.get("text") or message.get("caption") or ""
    if bot_username and f"@{bot_username.lower()}" in text.lower():
        return True
    reply_from = (message.get("reply_to_message") or {}).get("from") or {}
    return bool(bot_id and reply_from.get("id") == bot_id)


async def _configure_integrations() -> dict[str, Any]:
    global bot_username, bot_id, default_mcp_status
    result: dict[str, Any] = {}
    if settings.telegram_bot_token:
        me = await tg.get_me()
        bot_username = me.get("username")
        bot_id = me.get("id")
        db.set_state("bot_username", bot_username or "")
        result["telegram"] = {"ok": True, "username": bot_username}
        if settings.public_base_url and settings.telegram_webhook_secret:
            url = settings.public_base_url.rstrip("/") + "/telegram/webhook"
            await tg.set_webhook(url, settings.telegram_webhook_secret)
            result["webhook"] = {"ok": True, "url": url}
    if mistral.configured:
        result["mistral"] = await mistral.health()
    if settings.mistral_mcp_enabled and settings.mistral_mcp_server and mistral.configured:
        default_mcp_status = await mistral.ensure_default_mcp(
            settings.mistral_mcp_name,
            settings.mistral_mcp_server,
            settings.mistral_mcp_visibility,
            settings.mistral_mcp_credentials_name,
        )
        default_mcp_status["enabled"] = True
        result["mcp"] = default_mcp_status
    else:
        default_mcp_status = {"enabled": False, "ok": False}
    return result


def _require_admin_token(authorization: str | None) -> None:
    if not settings.admin_token:
        raise HTTPException(503, "Admin token is not configured")
    if authorization != f"Bearer {settings.admin_token}":
        raise HTTPException(401, "Unauthorized")


def require_admin(authorization: str | None = Header(default=None)):
    _require_admin_token(authorization)


def _setup_authorized(authorization: str | None) -> None:
    if _configured() or settings.admin_token:
        _require_admin_token(authorization)


class SetupRequest(BaseModel):
    mistral_api_key: str = Field(min_length=1)
    telegram_bot_token: str = Field(min_length=1)
    public_base_url: str = ""
    admin_token: str = ""
    telegram_webhook_secret: str = ""
    owner_telegram_id: str = ""
    existing_agent_id: str = ""
    create_agent: bool = True
    assistant_name: str = "Mistral Messenger Assistant"
    agent_name: str = "Mistral Messenger Assistant"
    agent_model: str = "mistral-medium-latest"
    agent_description: str = "A practical personal messenger assistant for text, images, documents and audio."
    agent_instructions: str = ""
    agent_enable_web_search: bool = True
    agent_enable_code_interpreter: bool = False
    agent_enable_image_generation: bool = False
    mistral_mcp_enabled: bool = False
    mistral_mcp_server: str = ""


class ChatRequest(BaseModel):
    message: str
    session: str = "admin-test"


SETUP_HTML = """<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Mistral Messenger Assistant Setup</title><style>
body{font-family:system-ui,sans-serif;background:#111;color:#eee;max-width:760px;margin:40px auto;padding:0 18px}input,textarea,button{width:100%;box-sizing:border-box;margin:6px 0 14px;padding:11px;border-radius:8px;border:1px solid #555;background:#1d1d1d;color:#fff}textarea{min-height:160px}button{background:#eee;color:#111;font-weight:700;cursor:pointer}.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}.check{display:flex;gap:8px;align-items:center}.check input{width:auto;margin:0}pre{white-space:pre-wrap;background:#191919;padding:12px;border-radius:8px}.hint{color:#aaa;font-size:.9rem}</style></head><body>
<h1>Mistral Messenger Assistant</h1><p>First-run setup. No credentials are included in the image.</p>
<label>Mistral API key</label><input id='mk' type='password' autocomplete='off'>
<label>Telegram bot token</label><input id='tt' type='password' autocomplete='off'>
<label>Public HTTPS base URL (optional until webhook deployment)</label><input id='url' placeholder='https://bot.example.com'>
<div class='row'><div><label>Assistant name</label><input id='an' value='Mistral Messenger Assistant'></div><div><label>Model</label><input id='model' value='mistral-medium-latest'></div></div>
<label>Existing Mistral Agent ID (leave empty to create one)</label><input id='aid'>
<label>Agent system prompt / instructions (optional; a safe default is used when empty)</label><textarea id='instructions' placeholder='You are a capable personal messenger assistant...'></textarea>
<label class='check'><input id='web' type='checkbox' checked> Enable Mistral web search on the created Agent</label>
<label class='check'><input id='mcp' type='checkbox'> Enable optional MCP connector</label><input id='mcpurl' placeholder='https://your-mcp.example.com/mcp'>
<label>Admin token (optional; generated automatically when empty)</label><input id='adm' type='password' autocomplete='off'>
<button onclick='go()'>Configure and test</button><pre id='out'>Ready.</pre>
<script>async function go(){let out=document.getElementById('out');out.textContent='Configuring...';let body={mistral_api_key:mk.value,telegram_bot_token:tt.value,public_base_url:url.value,admin_token:adm.value,existing_agent_id:aid.value,create_agent:!aid.value,assistant_name:an.value,agent_name:an.value,agent_model:model.value,agent_instructions:instructions.value,agent_enable_web_search:web.checked,mistral_mcp_enabled:mcp.checked,mistral_mcp_server:mcpurl.value};let h={'Content-Type':'application/json'};if(adm.value)h.Authorization='Bearer '+adm.value;let r=await fetch('/setup/configure',{method:'POST',headers:h,body:JSON.stringify(body)});out.textContent=JSON.stringify(await r.json(),null,2)}</script></body></html>"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await _configure_integrations()
    except Exception as exc:
        logger.warning("Startup integration warning: %s", exc)
    yield


app = FastAPI(title="Mistral Messenger Assistant", version="1.0.0-beta2", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def root():
    if not _configured():
        return HTMLResponse(SETUP_HTML)
    return HTMLResponse("<h1>Mistral Messenger Assistant</h1><p>Configured and running. See <a href='/health'>/health</a>.</p>")


@app.get("/setup", response_class=HTMLResponse)
async def setup_page():
    return HTMLResponse(SETUP_HTML)


@app.get("/setup/status")
async def setup_status():
    return {
        "configured": _configured(),
        "mistral_api_key": bool(settings.mistral_api_key),
        "mistral_agent_id": bool(settings.mistral_agent_id),
        "telegram_bot_token": bool(settings.telegram_bot_token),
        "public_base_url": settings.public_base_url or None,
        "assistant_name": settings.assistant_name,
        "mcp_enabled": settings.mistral_mcp_enabled,
    }


@app.post("/setup/configure")
async def setup_configure(req: SetupRequest, authorization: str | None = Header(default=None)):
    _setup_authorized(authorization)
    admin_token = req.admin_token.strip() or settings.admin_token or secrets.token_urlsafe(32)
    webhook_secret = req.telegram_webhook_secret.strip() or settings.telegram_webhook_secret or secrets.token_urlsafe(32)

    probe = MistralClient(req.mistral_api_key, req.existing_agent_id.strip(), None, settings.mistral_api_base, settings.mistral_timeout_seconds)
    tg_probe = TelegramClient(req.telegram_bot_token)
    me = await tg_probe.get_me()

    agent_id = req.existing_agent_id.strip()
    managed = False
    if agent_id:
        agent = await probe.get_agent(agent_id)
    elif req.create_agent:
        tools: list[dict[str, Any]] = []
        if req.agent_enable_web_search:
            tools.append({"type": "web_search"})
        if req.agent_enable_code_interpreter:
            tools.append({"type": "code_interpreter"})
        if req.agent_enable_image_generation:
            tools.append({"type": "image_generation"})
        instructions = req.agent_instructions.strip() or settings.agent_instructions
        agent = await probe.create_agent(req.agent_name, req.agent_model, req.agent_description, instructions, tools)
        agent_id = str(agent.get("id") or "")
        managed = True
        if not agent_id:
            raise HTTPException(502, "Mistral created an agent without an id")
    else:
        raise HTTPException(400, "Provide an existing_agent_id or enable create_agent")

    values = {
        "mistral_api_key": req.mistral_api_key,
        "mistral_agent_id": agent_id,
        "mistral_agent_version": None,
        "telegram_bot_token": req.telegram_bot_token,
        "public_base_url": req.public_base_url.strip().rstrip("/"),
        "admin_token": admin_token,
        "telegram_webhook_secret": webhook_secret,
        "owner_telegram_id": req.owner_telegram_id.strip(),
        "assistant_name": req.assistant_name.strip() or req.agent_name,
        "agent_name": req.agent_name,
        "agent_model": req.agent_model,
        "agent_description": req.agent_description,
        "agent_instructions": req.agent_instructions.strip() or settings.agent_instructions,
        "agent_enable_web_search": req.agent_enable_web_search,
        "agent_enable_code_interpreter": req.agent_enable_code_interpreter,
        "agent_enable_image_generation": req.agent_enable_image_generation,
        "agent_managed": managed,
        "mistral_mcp_enabled": bool(req.mistral_mcp_enabled and req.mistral_mcp_server.strip()),
        "mistral_mcp_server": req.mistral_mcp_server.strip(),
    }
    db.set_config(values)
    db.clear_all_mistral_conversations()
    _reload_clients()
    integration = await _configure_integrations()
    return {
        "ok": True,
        "configured": True,
        "assistant_name": settings.assistant_name,
        "mistral_agent_id": settings.mistral_agent_id,
        "agent_created": managed,
        "agent_model": agent.get("model"),
        "telegram_bot": me.get("username"),
        "webhook_configured": bool(settings.public_base_url),
        "mcp_enabled": settings.mistral_mcp_enabled,
        "admin_token_generated": not bool(req.admin_token.strip()),
        "admin_token": admin_token if not req.admin_token.strip() else None,
        "integration": integration,
    }


@app.get("/health")
async def health():
    mistral_ok = False
    mistral_error = None
    agent = None
    if mistral.configured:
        try:
            agent = await mistral.health()
            mistral_ok = True
        except Exception as exc:
            mistral_error = str(exc)
    return {
        "ok": True,
        "configured": _configured(),
        "assistant_name": settings.assistant_name,
        "mistral_configured": mistral.configured,
        "mistral_reachable": mistral_ok,
        "mistral_error": mistral_error,
        "mistral_agent": agent,
        "telegram_configured": bool(settings.telegram_bot_token),
        "telegram_bot": bot_username or db.get_state("bot_username"),
        "owner_paired": bool(owner_id()),
        "stored_conversations": db.count_mistral_conversations(),
        "mcp": default_mcp_status,
        "capabilities": ["text", "images", "documents", "text_files", "audio"],
    }


@app.get("/admin/settings", dependencies=[Depends(require_admin)])
async def admin_settings():
    data = settings.model_dump()
    data.pop("database_path", None)
    for key in SECRET_FIELDS:
        if key in data:
            data[key] = "********" if data[key] else ""
    return data


@app.post("/admin/pair", dependencies=[Depends(require_admin)])
async def new_pair():
    if owner_id():
        return {"paired": True, "telegram_user_id": owner_id(), "pair_code": None}
    code = db.new_pairing_code()
    username = bot_username or db.get_state("bot_username")
    return {"paired": False, "pair_code": code, "url": f"https://t.me/{username}?start={code}" if username else None, "expires_in": 600}


@app.post("/admin/chat", dependencies=[Depends(require_admin)])
async def admin_chat(req: ChatRequest):
    reply = await answer_via_mistral(f"admin:{req.session}", req.message, {"id": "admin"}, {"type": "private", "id": req.session})
    return {"text": reply.text, "route": reply.route, "conversation_id": reply.conversation_id, "meta": reply.meta}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    if not _configured():
        raise HTTPException(503, "Setup is incomplete")
    if not settings.telegram_webhook_secret or x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(401, "Bad Telegram webhook secret")
    update = await request.json()
    update_id = update.get("update_id")
    if update_id is not None and not db.claim_telegram_update(update_id):
        return {"ok": True, "duplicate": True}

    membership = update.get("my_chat_member")
    if membership:
        actor = membership.get("from") or {}
        chat = membership.get("chat") or {}
        status = (membership.get("new_chat_member") or {}).get("status")
        if is_owner(actor.get("id")) and chat.get("type") in {"group", "supergroup"}:
            if status in {"member", "administrator"}:
                db.allow_group(str(chat.get("id")), chat.get("title") or "")
            elif status in {"left", "kicked"}:
                db.disallow_group(str(chat.get("id")))
        return {"ok": True}

    message = update.get("message")
    if not message:
        return {"ok": True}
    text = (message.get("text") or "").strip()
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id"))

    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        code = parts[1].strip() if len(parts) > 1 else ""
        if is_owner(sender.get("id")):
            await tg.send_message(chat_id, f"✅ {settings.assistant_name} is already connected.")
        elif not code:
            await tg.send_message(chat_id, "👋 This bot is running but this Telegram account is not paired yet. Generate a pairing link from the admin API.")
        elif not owner_id() and db.consume_pairing_code(code):
            db.set_state("owner_telegram_id", str(sender.get("id")))
            await tg.send_message(chat_id, f"✅ Connected. This Telegram account now owns {settings.assistant_name}.")
        else:
            await tg.send_message(chat_id, "⚠️ Invalid/expired pairing code or an owner is already configured.")
        return {"ok": True}

    if text in {"/new", "/restart"} and is_owner(sender.get("id")):
        conversation_id = db.get_mistral_conversation(chat_id)
        db.clear_mistral_conversation(chat_id)
        _workspace_pairs.pop(chat_id, None)
        if conversation_id:
            try:
                await mistral.delete_conversation(conversation_id)
            except Exception as exc:
                logger.warning("Could not delete conversation %s: %s", conversation_id, exc)
        await tg.send_message(chat_id, "✅ New conversation started.")
        return {"ok": True}

    if text == "/status" and is_owner(sender.get("id")):
        await tg.send_message(
            chat_id,
            f"{settings.assistant_name}\nAgent: {settings.mistral_agent_id}\nMCP: {'on' if settings.mistral_mcp_enabled else 'off'}\nConversation: {db.get_mistral_conversation(chat_id) or 'new'}",
        )
        return {"ok": True}

    detected = detect_input(message)
    if detected.kind == "unsupported" and not text and not message.get("document"):
        return {"ok": True}
    if not should_answer(message):
        return {"ok": True}
    task = asyncio.create_task(_process_ai_message(message))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"ok": True, "queued": True}
