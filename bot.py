"""
Ofshore Guardian Bot — @Ofshore_Guardian_bot
AI-powered infrastructure assistant for ofshore.dev
"""
import asyncio, json, os, logging
import httpx

# ── Config ────────────────────────────────────────────────────────
TOKEN         = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
COOLIFY_URL   = os.environ.get("COOLIFY_URL","https://coolify.ofshore.dev")
COOLIFY_TOKEN = os.environ.get("COOLIFY_TOKEN","")
SUPABASE_URL  = os.environ.get("SUPABASE_URL","")
SUPABASE_KEY  = os.environ.get("SUPABASE_KEY","")
ALLOWED       = set(x.strip() for x in os.environ.get("ALLOWED_TELEGRAM_IDS","").split(",") if x.strip())
ADMIN_ID      = os.environ.get("ADMIN_CHAT_ID","")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [BOT] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("guardian")

TG = f"https://api.telegram.org/bot{TOKEN}"
sessions: dict[str,list] = {}

# ── System prompt ─────────────────────────────────────────────────
SYSTEM = """You are the AI Guardian of ofshore.dev — Maciej's personal infrastructure assistant.

You have access to real-time infrastructure status and can:
- Report which apps are running/broken
- Trigger restarts and deployments
- Show smoke test results and Watchdog alerts
- Answer questions about any app on ofshore.dev
- Help debug issues

Apps: agentflow, quiz-manager, omnichannel-inbox, english-teacher, manus-brain, integration-hub, 
ai-control-center, sentinel, n8n, watchdog, autoheal, smoketester, and more.

Style: Natural language, concise. Polish if user writes Polish, English if English.
Never use code blocks unless explicitly asked."""

# ── Supabase RPC ──────────────────────────────────────────────────
async def sb(fn, params={}):
    if not SUPABASE_URL: return None
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.post(f"{SUPABASE_URL}/rest/v1/rpc/{fn}",
                headers={"apikey":SUPABASE_KEY,"Authorization":f"Bearer {SUPABASE_KEY}",
                         "Content-Type":"application/json"}, json=params)
            return r.json() if r.status_code == 200 else None
    except: return None

# ── Coolify ───────────────────────────────────────────────────────
async def coolify(path, method="GET", body=None):
    if not COOLIFY_TOKEN: return {}
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            h = {"Authorization":f"Bearer {COOLIFY_TOKEN}"}
            if method == "GET":
                r = await c.get(f"{COOLIFY_URL}/api/v1{path}", headers=h)
            else:
                r = await c.request(method, f"{COOLIFY_URL}/api/v1{path}", headers=h, json=body or {})
            return r.json() if r.status_code in (200,201) else {}
    except: return {}

async def get_infra():
    apps = await coolify("/applications")
    if not isinstance(apps, list): return "Infrastructure unavailable."
    healthy = sum(1 for a in apps if "running" in a.get("status",""))
    broken  = [(a["name"],a["status"]) for a in apps if "exited" in a.get("status","") or "restarting" in a.get("status","")]
    smoke   = await sb("public_get_smoke_summary") or []
    failed  = [(s["app_name"],s["test_name"]) for s in smoke if not s.get("passed")]
    ctx = f"[{__import__('datetime').datetime.now().strftime('%H:%M')}] {healthy}/{len(apps)} apps OK"
    if broken: ctx += f" | DOWN: {', '.join(n for n,_ in broken)}"
    if failed: ctx += f" | SMOKE FAILS: {', '.join(f'{a}/{t}' for a,t in failed[:4])}"
    return ctx

# ── App map ───────────────────────────────────────────────────────
APPS = {
    "agentflow":"ts0c0wgco8wo8kgocok84cws","quiz":"yssco8cc800ow880w0wo48o0",
    "quiz-manager":"yssco8cc800ow880w0wo48o0","inbox":"tcww08co80wsgwwg8swwgss8",
    "omnichannel":"tcww08co80wsgwwg8swwgss8","english":"d0800oks0g4gws0kw04ck00s",
    "english-teacher":"d0800oks0g4gws0kw04ck00s","manus":"kssk4o48sgosgwwck8s8ws80",
    "brain":"kssk4o48sgosgwwck8s8ws80","hub":"s44sck0k0os0k4w0www00cg4",
    "integration":"s44sck0k0os0k4w0www00cg4","sentinel":"rs488c4ccg48w48gocgog8sg",
    "ai-control":"hokscgg48sowg44wwc044gk8","watchdog":"g8csck0kw8c0sc0cosg0cw84",
    "autoheal":"vcgk0g4sc4sck0kkc8k080gk","smoketester":"qws0sk4gooo4ok8cswc0o0kw",
}

def find_app(text):
    t = text.lower()
    for k,v in APPS.items():
        if k in t: return k,v
    return None,None

# ── Claude ────────────────────────────────────────────────────────
async def ask_claude(chat_id, msg):
    ctx = await get_infra()
    hist = sessions.get(chat_id,[])
    messages = hist[-12:] + [{"role":"user","content":msg}]
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01",
                         "content-type":"application/json"},
                json={"model":"claude-sonnet-4-6","max_tokens":1024,
                      "system":SYSTEM+"\n\n"+ctx,"messages":messages})
            reply = r.json()["content"][0]["text"]
    except Exception as ex:
        reply = f"Error: {ex}"
    hist.append({"role":"user","content":msg})
    hist.append({"role":"assistant","content":reply})
    sessions[chat_id] = hist[-20:]
    return reply

# ── Telegram ──────────────────────────────────────────────────────
async def send(chat_id, text, parse_mode="Markdown"):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"{TG}/sendMessage",
                json={"chat_id":chat_id,"text":text[:4096],"parse_mode":parse_mode})
    except Exception as ex:
        log.warning(f"Send failed: {ex}")

async def typing(chat_id):
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(f"{TG}/sendChatAction",json={"chat_id":chat_id,"action":"typing"})
    except: pass

async def handle(update):
    msg = update.get("message") or update.get("edited_message")
    if not msg: return
    chat_id = str(msg["chat"]["id"])
    user_id = str(msg["from"]["id"])
    text = msg.get("text","").strip()
    if not text: return

    if ALLOWED and user_id not in ALLOWED and chat_id not in ALLOWED:
        await send(chat_id,"Brak dostępu.")
        return

    log.info(f"[{chat_id}] {text[:60]}")
    await typing(chat_id)
    t = text.lower()

    if t in ["/start","/help"]:
        await send(chat_id,
            "👋 *Ofshore Guardian* jest online!\n\n"
            "Pytaj o co chcesz — status appek, deployment, logi, alerty.\n"
            "Przykłady: _status_, _zrestartuj quiz_, _jakie błędy_, _co się dzieje_")
        return

    if any(w in t for w in ["status","stan","co słychać","co się dzieje","jak działa","zdrowie"]):
        apps = await coolify("/applications")
        if isinstance(apps, list):
            broken = [(a["name"],a["status"]) for a in apps if "exited" in a.get("status","") or "restarting" in a.get("status","")]
            healthy = sum(1 for a in apps if "running" in a.get("status",""))
            if not broken:
                await send(chat_id, f"✅ Wszystko działa — {healthy}/{len(apps)} aplikacji zdrowych.")
            else:
                names = ", ".join(n for n,_ in broken)
                await send(chat_id, f"⚠️ {len(broken)} problem(y): *{names}*\nPozostałe {healthy} appek OK.")
        return

    if any(w in t for w in ["restart","zrestartuj","reboot"]):
        name, uuid = find_app(text)
        if uuid:
            r = await coolify(f"/applications/{uuid}/restart","POST")
            await send(chat_id, f"🔄 Restart *{name}* zlecony. Za ~1 min powinno działać.")
        else:
            await send(chat_id, "Którą aplikację? Np. _zrestartuj quiz-manager_")
        return

    if any(w in t for w in ["deploy","wdróż","update","zaktualizuj"]):
        name, uuid = find_app(text)
        if uuid:
            r = await coolify(f"/deploy?uuid={uuid}&force=true")
            deps = r.get("deployments",[{}])
            dep_id = deps[0].get("deployment_uuid","") if deps else ""
            await send(chat_id, f"🚀 Deploy *{name}* zlecony!\nID: `{dep_id[:12]}...`")
        else:
            await send(chat_id, "Którą aplikację? Np. _deploy quiz-manager_")
        return

    if any(w in t for w in ["smoke","testy","test","wyniki testów"]):
        summary = await sb("public_get_smoke_summary") or []
        if not summary:
            await send(chat_id, "Brak wyników testów smoke.")
            return
        passed = sum(1 for s in summary if s.get("passed"))
        failed = [(s["app_name"],s["test_name"]) for s in summary if not s.get("passed")]
        if not failed:
            await send(chat_id, f"✅ Wszystkie {passed} testy OK!")
        else:
            fail_str = "\n".join(f"• {a}/{t}" for a,t in failed[:6])
            await send(chat_id, f"⚠️ {passed}/{len(summary)} OK\n{fail_str}")
        return

    # Default: Claude
    reply = await ask_claude(chat_id, text)
    await send(chat_id, reply)

# ── Main ──────────────────────────────────────────────────────────
async def main():
    # Verify token
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{TG}/getMe")
        me = r.json()
    if not me.get("ok"):
        log.error(f"Invalid token: {me}")
        raise SystemExit(1)
    
    bot_name = me["result"]["username"]
    log.info(f"✅ @{bot_name} online | allowed={ALLOWED or 'ALL'}")

    # Notify admin
    if ADMIN_ID:
        async with httpx.AsyncClient(timeout=5) as c:
            try:
                await c.post(f"{TG}/sendMessage", json={
                    "chat_id": ADMIN_ID,
                    "parse_mode": "Markdown",
                    "text": f"🤖 *@{bot_name} uruchomiony!*\n\nGotowy. Napisz /start aby zacząć."
                })
            except: pass

    offset = 0
    log.info("Polling updates...")
    while True:
        try:
            async with httpx.AsyncClient(timeout=35) as c:
                r = await c.get(f"{TG}/getUpdates",
                    params={"offset":offset,"timeout":30,"limit":10})
                data = r.json()
            if data.get("ok"):
                for upd in data["result"]:
                    offset = upd["update_id"] + 1
                    asyncio.create_task(handle(upd))
        except asyncio.CancelledError:
            break
        except Exception as ex:
            log.error(f"Poll: {ex}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
