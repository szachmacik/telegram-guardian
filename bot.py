"""
Telegram Guardian Bot — ofshore.dev Superagent
Rozmawia z Claudem przez Telegram, zarządza infrastrukturą.
Token pobierany z env lub Supabase Vault.
"""
import asyncio, json, os, logging
import httpx

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_KEY   = os.environ["ANTHROPIC_API_KEY"]
COOLIFY_URL     = os.environ.get("COOLIFY_URL", "https://coolify.ofshore.dev")
COOLIFY_TOKEN   = os.environ.get("COOLIFY_TOKEN", "")
SUPABASE_URL    = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY    = os.environ.get("SUPABASE_KEY", "")
ALLOWED_USERS   = set(x.strip() for x in os.environ.get("ALLOWED_TELEGRAM_IDS","").split(",") if x.strip())
ADMIN_CHAT_ID   = os.environ.get("ADMIN_CHAT_ID","")
TOKEN_ENV       = os.environ.get("TELEGRAM_BOT_TOKEN","")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [TG] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("tgbot")

# ── Token loading ─────────────────────────────────────────────────────────────
async def load_telegram_token() -> str:
    """Load token from env, or from Supabase Vault if env is placeholder."""
    if TOKEN_ENV and TOKEN_ENV != "PLACEHOLDER_SET_THIS":
        return TOKEN_ENV
    # Try Supabase Vault
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(f"{SUPABASE_URL}/rest/v1/rpc/get_secret_by_name",
                    headers={"apikey":SUPABASE_KEY,"Authorization":f"Bearer {SUPABASE_KEY}",
                             "Content-Type":"application/json"},
                    json={"secret_name":"telegram_bot_token"})
                if r.status_code == 200:
                    data = r.json()
                    token = data if isinstance(data, str) else data.get("decrypted_secret","")
                    if token and len(token) > 20:
                        log.info("✅ Loaded Telegram token from Supabase Vault")
                        return token
        except Exception as ex:
            log.warning(f"Vault load failed: {ex}")
    log.error("❌ No valid TELEGRAM_BOT_TOKEN found!")
    return ""

# ── System prompt ─────────────────────────────────────────────────────────────
SYSTEM = """You are the AI Guardian of ofshore.dev — a conversational assistant for Maciej (the owner).
You have direct access to infrastructure status and can report on all apps.

Apps running on ofshore.dev:
- agentflow.ofshore.dev — AI task orchestration
- quiz.ofshore.dev — Quiz manager with fraud detection  
- inbox.ofshore.dev — Omnichannel inbox
- english-teacher.ofshore.dev — AI lesson generator
- brain.ofshore.dev — Multi-AI router (Claude/Kimi/DeepSeek)
- hub.ofshore.dev — Integration hub (ManyChat/webhooks)
- ai-control-center.ofshore.dev — Control center / Sentinel
- security.ofshore.dev — Cybersecurity dashboard
- n8n.ofshore.dev — Workflow automation (n8n)
- sentinel.ofshore.dev — Security platform

Monitoring stack:
- Watchdog: checks every 60s, reports to Supabase
- AutoHeal: every 5min reads alerts, auto-fixes via GitHub+Coolify
- SmokeTester: every 10min tests real AI endpoints (guardian bots)

STYLE: Natural language, concise (2-4 sentences). No code blocks unless asked.
Polish if user writes Polish, English if English.
When user says "status" or "co słychać" — report infra status."""

# ── Supabase ──────────────────────────────────────────────────────────────────
async def sb(fn: str, params: dict = {}) -> any:
    if not SUPABASE_URL or not SUPABASE_KEY: return None
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{SUPABASE_URL}/rest/v1/rpc/{fn}",
                headers={"apikey":SUPABASE_KEY,"Authorization":f"Bearer {SUPABASE_KEY}",
                         "Content-Type":"application/json"}, json=params)
            return r.json() if r.status_code == 200 else None
    except: return None

# ── Coolify ───────────────────────────────────────────────────────────────────
async def coolify(path: str, method="GET", body=None):
    if not COOLIFY_TOKEN: return {}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            headers = {"Authorization": f"Bearer {COOLIFY_TOKEN}"}
            if method == "GET":
                r = await c.get(f"{COOLIFY_URL}/api/v1{path}", headers=headers)
            else:
                r = await c.request(method, f"{COOLIFY_URL}/api/v1{path}",
                                     headers=headers, json=body or {})
            return r.json() if r.status_code in (200,201) else {}
    except: return {}

# ── Get infra context ─────────────────────────────────────────────────────────
async def infra_context() -> str:
    apps = await coolify("/applications")
    if not isinstance(apps, list):
        return "Infrastructure status: unavailable."
    
    healthy = sum(1 for a in apps if "running" in a.get("status",""))
    broken  = [(a["name"], a["status"]) for a in apps
               if "exited" in a.get("status","") or "restarting" in a.get("status","")]
    
    smoke = await sb("public_get_smoke_summary") or []
    failed_smoke = [(s["app_name"],s["test_name"]) for s in smoke if not s.get("passed")]
    
    ctx = f"INFRA [{__import__('datetime').datetime.now().strftime('%H:%M')}]: "
    ctx += f"{healthy}/{len(apps)} apps healthy"
    if broken:
        ctx += f" | DOWN: {', '.join(n for n,_ in broken)}"
    if failed_smoke:
        ctx += f" | SMOKE FAILS: {', '.join(f'{a}/{t}' for a,t in failed_smoke[:4])}"
    return ctx

# ── Actions ───────────────────────────────────────────────────────────────────
APP_MAP = {
    "agentflow": "ts0c0wgco8wo8kgocok84cws",
    "quiz": "yssco8cc800ow880w0wo48o0",
    "quiz-manager": "yssco8cc800ow880w0wo48o0",
    "inbox": "tcww08co80wsgwwg8swwgss8",
    "omnichannel": "tcww08co80wsgwwg8swwgss8",
    "english": "d0800oks0g4gws0kw04ck00s",
    "english-teacher": "d0800oks0g4gws0kw04ck00s",
    "manus": "kssk4o48sgosgwwck8s8ws80",
    "brain": "kssk4o48sgosgwwck8s8ws80",
    "integration": "s44sck0k0os0k4w0www00cg4",
    "hub": "s44sck0k0os0k4w0www00cg4",
    "sentinel": "rs488c4ccg48w48gocgog8sg",
    "ai-control": "hokscgg48sowg44wwc044gk8",
    "watchdog": "g8csck0kw8c0sc0cosg0cw84",
    "autoheal": "vcgk0g4sc4sck0kkc8k080gk",
    "smoketester": "qws0sk4gooo4ok8cswc0o0kw",
}

def find_app(text: str):
    t = text.lower()
    for k, uuid in APP_MAP.items():
        if k in t:
            return k, uuid
    return None, None

# ── Claude ────────────────────────────────────────────────────────────────────
sessions: dict[str, list] = {}

async def claude(chat_id: str, user_msg: str) -> str:
    ctx = await infra_context()
    history = sessions.get(chat_id, [])
    messages = history[-12:] + [{"role":"user","content":user_msg}]
    
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01",
                         "content-type":"application/json"},
                json={"model":"claude-sonnet-4-6","max_tokens":1024,
                      "system": SYSTEM + "\n\n" + ctx,
                      "messages": messages})
            reply = r.json()["content"][0]["text"]
    except Exception as ex:
        reply = f"Error: {ex}"
    
    history.append({"role":"user","content":user_msg})
    history.append({"role":"assistant","content":reply})
    sessions[chat_id] = history[-20:]
    return reply

# ── Telegram ──────────────────────────────────────────────────────────────────
TG_URL = ""  # set after token load

async def send(chat_id, text: str, parse_mode="Markdown"):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"{TG_URL}/sendMessage",
                json={"chat_id": chat_id, "text": text[:4096], "parse_mode": parse_mode})
    except Exception as ex:
        log.warning(f"Send failed: {ex}")

async def typing(chat_id):
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(f"{TG_URL}/sendChatAction",
                         json={"chat_id": chat_id, "action": "typing"})
    except: pass

async def handle(update: dict):
    msg = update.get("message") or update.get("edited_message")
    if not msg: return
    
    chat_id = str(msg["chat"]["id"])
    user_id = str(msg["from"]["id"])
    text = msg.get("text","").strip()
    if not text: return
    
    # Auth
    if ALLOWED_USERS and user_id not in ALLOWED_USERS and chat_id not in ALLOWED_USERS:
        log.warning(f"Unauthorized: {user_id} {chat_id}")
        await send(chat_id, "Brak dostępu.")
        return
    
    log.info(f"[{chat_id}:{user_id}] {text[:60]}")
    await typing(chat_id)
    
    # Commands
    t = text.lower()
    if t in ["/start", "/help"]:
        await send(chat_id,
            "👋 Jestem AI Guardian ofshore.dev!\n\n"
            "Mogę:\n• Odpowiadać na pytania o aplikacje\n"
            "• Raportować status infrastruktury\n"
            "• Restartować aplikacje\n"
            "• Pokazywać wyniki testów smoke\n\n"
            "Napisz cokolwiek, albo spróbuj: _status_, _zrestartuj quiz_, _wyniki testów_")
        return
    
    # Quick status
    if any(w in t for w in ["status","stan infra","co słychać","co sie dzieje","jak dziala"]):
        apps = await coolify("/applications")
        if isinstance(apps, list):
            broken = [(a["name"],a["status"]) for a in apps
                      if "exited" in a.get("status","") or "restarting" in a.get("status","")]
            healthy = sum(1 for a in apps if "running" in a.get("status",""))
            if not broken:
                reply = f"✅ Wszystko działa! {healthy}/{len(apps)} aplikacji zdrowych."
            else:
                names = ", ".join(n for n,_ in broken)
                reply = f"⚠️ {len(broken)} problem(y): {names}\nPozostałe {healthy} appek OK."
        else:
            reply = "Nie mogę sprawdzić statusu."
        await send(chat_id, reply)
        return
    
    # Restart
    if any(w in t for w in ["restart","zrestartuj","reboot","uruchom ponownie"]):
        name, uuid = find_app(text)
        if uuid:
            r = await coolify(f"/applications/{uuid}/restart","POST")
            if r.get("message"):
                await send(chat_id, f"🔄 Restart {name} zlecony. Za ~1min powinien działać.")
            else:
                await send(chat_id, f"❌ Nie udało się zrestartować {name}.")
        else:
            await send(chat_id, "Którą aplikację? Np. _zrestartuj quiz-manager_")
        return
    
    # Deploy
    if any(w in t for w in ["deploy","wdróż","zaktualizuj"]):
        name, uuid = find_app(text)
        if uuid:
            r = await coolify(f"/deploy?uuid={uuid}&force=true","GET")
            deps = r.get("deployments",[{}])
            dep_id = deps[0].get("deployment_uuid","") if deps else ""
            await send(chat_id, f"🚀 Deploy {name} zlecony! ID: `{dep_id[:12]}...`")
        else:
            await send(chat_id, "Którą aplikację? Np. _deploy quiz-manager_")
        return
    
    # Smoke tests
    if any(w in t for w in ["smoke","testy","test results","wyniki"]):
        summary = await sb("public_get_smoke_summary") or []
        if not summary:
            await send(chat_id, "Brak wyników testów smoke.")
            return
        passed = sum(1 for s in summary if s.get("passed"))
        failed = [(s["app_name"],s["test_name"]) for s in summary if not s.get("passed")]
        if not failed:
            await send(chat_id, f"✅ Wszystkie {passed} testy przechodzą!")
        else:
            fail_str = "\n".join(f"• {a}/{t}" for a,t in failed[:6])
            await send(chat_id, f"⚠️ {passed}/{len(summary)} testów OK\nProblemy:\n{fail_str}")
        return
    
    # Everything else → Claude
    reply = await claude(chat_id, text)
    await send(chat_id, reply)

# ── Main polling loop ─────────────────────────────────────────────────────────
async def main():
    global TG_URL
    
    token = await load_telegram_token()
    if not token:
        log.error("No token available. Set TELEGRAM_BOT_TOKEN in Coolify env.")
        log.error("Get token from @BotFather on Telegram.")
        # Keep running so container stays up (easier to fix via Coolify)
        while True:
            await asyncio.sleep(60)
            log.info("Waiting for TELEGRAM_BOT_TOKEN...")
    
    TG_URL = f"https://api.telegram.org/bot{token}"
    
    # Verify token
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{TG_URL}/getMe")
        me = r.json()
    
    if not me.get("ok"):
        log.error(f"Invalid token: {me.get('description')}")
        while True:
            await asyncio.sleep(60)
    
    bot_name = me["result"]["username"]
    log.info(f"🤖 Bot started: @{bot_name}")
    
    # Notify admin
    if ADMIN_CHAT_ID:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                await c.post(f"{TG_URL}/sendMessage",
                    json={"chat_id": ADMIN_CHAT_ID,
                          "text": f"🤖 @{bot_name} uruchomiony! Napisz /start aby zacząć."})
        except: pass
    
    offset = 0
    log.info("Polling for updates...")
    
    while True:
        try:
            async with httpx.AsyncClient(timeout=35) as c:
                r = await c.get(f"{TG_URL}/getUpdates",
                    params={"offset": offset, "timeout": 30, "limit": 10})
                data = r.json()
            
            if not data.get("ok"):
                log.warning(f"getUpdates: {data}")
                await asyncio.sleep(5)
                continue
            
            for update in data["result"]:
                offset = update["update_id"] + 1
                asyncio.create_task(handle(update))
                
        except asyncio.CancelledError:
            break
        except Exception as ex:
            log.error(f"Poll error: {ex}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
