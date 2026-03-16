"""
Telegram Guardian Bot v3 — Prawa ręka Macieja
Możliwości:
  - Pełna infrastruktura: status, restart, deploy, logi
  - Smoke testy i alerty
  - Pytania do AI (Claude Sonnet) z kontekstem infra
  - Zarządzanie aplikacjami przez komendy
  - Raporty periodyczne
  - Inline klawiatura z przyciskami
  - Markdown formatowanie
"""
import asyncio, json, os, logging, re, time
import httpx
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
TG_TOKEN        = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_KEY   = os.environ["ANTHROPIC_API_KEY"]
COOLIFY_URL     = os.environ.get("COOLIFY_URL", "https://coolify.ofshore.dev")
COOLIFY_TOKEN   = os.environ.get("COOLIFY_TOKEN", "")
SB_URL          = os.environ.get("SUPABASE_URL", "")
SB_KEY          = os.environ.get("SUPABASE_KEY", "")
ALLOWED         = set(x.strip() for x in os.environ.get("ALLOWED_TELEGRAM_IDS","").split(",") if x.strip())
ADMIN_ID        = os.environ.get("ADMIN_CHAT_ID", "")
TG              = f"https://api.telegram.org/bot{TG_TOKEN}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [BOT] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("bot")

sessions: dict[str, list] = {}
app_cache: list = []
cache_ts: float = 0

# ── App map ───────────────────────────────────────────────────────────────────
APP_MAP = {
    "agentflow":     "ts0c0wgco8wo8kgocok84cws",
    "quiz":          "yssco8cc800ow880w0wo48o0",
    "quiz-manager":  "yssco8cc800ow880w0wo48o0",
    "inbox":         "tcww08co80wsgwwg8swwgss8",
    "omnichannel":   "tcww08co80wsgwwg8swwgss8",
    "english":       "d0800oks0g4gws0kw04ck00s",
    "manus":         "kssk4o48sgosgwwck8s8ws80",
    "brain":         "kssk4o48sgosgwwck8s8ws80",
    "integration":   "s44sck0k0os0k4w0www00cg4",
    "hub":           "s44sck0k0os0k4w0www00cg4",
    "sentinel":      "rs488c4ccg48w48gocgog8sg",
    "ai-control":    "hokscgg48sowg44wwc044gk8",
    "security":      "wg0gkco8g0swgccc8www04gg",
    "watchdog":      "g8csck0kw8c0sc0cosg0cw84",
    "autoheal":      "vcgk0g4sc4sck0kkc8k080gk",
    "smoketester":   "qws0sk4gooo4ok8cswc0o0kw",
    "telegram":      "qook8w0sw4o404swcoookg00",
    "wp":            "wp-manager-uuid",
}

DOMAINS = {
    "agentflow":   "agentflow.ofshore.dev",
    "quiz":        "quiz.ofshore.dev",
    "inbox":       "inbox.ofshore.dev",
    "english":     "english-teacher.ofshore.dev",
    "manus":       "brain.ofshore.dev",
    "integration": "hub.ofshore.dev",
    "sentinel":    "sentinel.ofshore.dev",
    "ai-control":  "ai-control-center.ofshore.dev",
    "security":    "security.ofshore.dev",
}

# ── Supabase ──────────────────────────────────────────────────────────────────
async def sb(fn: str, params: dict = {}) -> any:
    if not SB_URL: return None
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{SB_URL}/rest/v1/rpc/{fn}",
                headers={"apikey":SB_KEY,"Authorization":f"Bearer {SB_KEY}","Content-Type":"application/json"},
                json=params)
            return r.json() if r.status_code == 200 else None
    except: return None

# ── Coolify ───────────────────────────────────────────────────────────────────
async def coolify(path: str, method="GET", body=None) -> any:
    if not COOLIFY_TOKEN: return {}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            h = {"Authorization": f"Bearer {COOLIFY_TOKEN}"}
            url = f"{COOLIFY_URL}/api/v1{path}"
            r = await c.request(method, url, headers=h, json=body)
            return r.json() if r.status_code in (200,201) else {}
    except: return {}

async def get_apps(force=False) -> list:
    global app_cache, cache_ts
    now = time.monotonic()
    if not force and app_cache and now - cache_ts < 60:
        return app_cache
    apps = await coolify("/applications")
    if isinstance(apps, list):
        app_cache = apps
        cache_ts = now
    return app_cache

def find_app(text: str) -> tuple[str, str]:
    t = text.lower()
    for k, uuid in APP_MAP.items():
        if k in t:
            return k, uuid
    return None, None

# ── Infra context ─────────────────────────────────────────────────────────────
async def get_context() -> str:
    apps = await get_apps()
    if not isinstance(apps, list):
        return "Brak danych o infrastrukturze."
    
    healthy = sum(1 for a in apps if "running" in a.get("status",""))
    broken  = [(a["name"], a["status"]) for a in apps
               if "exited" in a.get("status","") or "restarting" in a.get("status","")]
    
    smoke  = await sb("public_get_smoke_summary") or []
    failed_smoke = [(s["app_name"], s["test_name"]) for s in smoke if not s.get("passed")]
    
    alerts = await sb("public_get_alerts") or []
    
    ctx = f"INFRA [{datetime.now().strftime('%H:%M %d.%m')}]: {healthy}/{len(apps)} apps OK"
    if broken:
        ctx += f" | DOWN: {', '.join(n for n,_ in broken)}"
    if failed_smoke:
        ctx += f" | TESTY FAIL: {', '.join(f'{a}/{t}' for a,t in failed_smoke[:4])}"
    if alerts:
        ctx += f" | {len(alerts)} alertów"
    return ctx

# ── Claude ────────────────────────────────────────────────────────────────────
SYSTEM = """Jesteś AI Guardian ofshore.dev — prawa ręka Macieja, właściciela platformy.

Twoje możliwości:
- Odpowiadasz na pytania o stan infrastruktury (24+ aplikacje na ofshore.dev)
- Możesz zlecić restart lub deploy aplikacji (przez komendy: /restart <app>, /deploy <app>)
- Pokazujesz logi aplikacji (/logs <app>)
- Raportujesz wyniki smoke testów (/smoke)
- Pokazujesz aktywne alerty (/alerts)
- Jesteś ekspertem od każdej z aplikacji

Aplikacje ofshore.dev:
agentflow (AI task orchestration), quiz-manager (quizy + fraud detection),
omnichannel-inbox (chat/email/SMS), english-teacher (generowanie lekcji),
manus-brain (multi-AI: Claude/Kimi/DeepSeek), integration-hub (ManyChat/webhooks),
ai-control-center/sentinel (centrum dowodzenia), security (cybersecurity),
n8n (workflow automation, n8n.ofshore.dev), watchdog (monitoring), 
autoheal (samoleczenie), smoketester (testy funkcjonalne)

Stack: React+tRPC+Drizzle+MySQL (TiDB) lub Postgres, Docker/Coolify, DigitalOcean 178.62.246.169

STYL: Naturalny język, krótko i konkretnie. Używaj emoji. Odpowiadaj po polsku jeśli user pisze po polsku.
Formatowanie Markdown jest OK (bold, italic, code).
Gdy user pyta o status — daj konkretny raport bez owijania w bawełnę."""

async def ask_claude(chat_id: str, user_msg: str, extra_context="") -> str:
    ctx = await get_context()
    history = sessions.get(chat_id, [])
    messages = history[-12:] + [{"role":"user","content":user_msg}]
    
    try:
        async with httpx.AsyncClient(timeout=45) as c:
            r = await c.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01",
                         "content-type":"application/json"},
                json={"model":"claude-sonnet-4-6","max_tokens":1500,
                      "system": SYSTEM + "\n\n" + ctx + (f"\n\n{extra_context}" if extra_context else ""),
                      "messages": messages})
            reply = r.json()["content"][0]["text"]
    except Exception as ex:
        log.error(f"Claude error: {ex}")
        reply = f"⚠️ Błąd AI: {str(ex)[:80]}"
    
    history.append({"role":"user","content":user_msg})
    history.append({"role":"assistant","content":reply})
    sessions[chat_id] = history[-20:]
    return reply

# ── Telegram helpers ──────────────────────────────────────────────────────────
async def send(chat_id, text: str, reply_markup=None, parse_mode="Markdown"):
    payload = {"chat_id":chat_id,"text":text[:4096],"parse_mode":parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"{TG}/sendMessage", json=payload)
    except Exception as ex:
        log.warning(f"send failed: {ex}")

async def send_long(chat_id, text: str):
    """Send text longer than 4096 chars in chunks."""
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        await send(chat_id, chunk)
        if len(chunks) > 1:
            await asyncio.sleep(0.3)

async def typing(chat_id):
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(f"{TG}/sendChatAction", json={"chat_id":chat_id,"action":"typing"})
    except: pass

async def answer_callback(callback_id: str, text=""):
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(f"{TG}/answerCallbackQuery",
                json={"callback_query_id":callback_id,"text":text})
    except: pass

def main_keyboard():
    """Main inline keyboard with quick actions."""
    return {"inline_keyboard": [
        [{"text":"📊 Status","callback_data":"status"},
         {"text":"🧪 Smoke testy","callback_data":"smoke"},
         {"text":"🚨 Alerty","callback_data":"alerts"}],
        [{"text":"📋 Logi watchdog","callback_data":"logs_watchdog"},
         {"text":"📋 Logi autoheal","callback_data":"logs_autoheal"}],
        [{"text":"🔄 Odśwież status","callback_data":"refresh"}],
    ]}

# ── Command handlers ──────────────────────────────────────────────────────────
async def cmd_start(chat_id):
    await send(chat_id,
        "👋 *Jestem Twoim AI Guardian ofshore.dev!*\n\n"
        "Co mogę zrobić:\n"
        "• Odpowiadać na pytania o infrastrukturę\n"
        "• `/status` — stan wszystkich aplikacji\n"
        "• `/smoke` — wyniki smoke testów\n"
        "• `/alerts` — aktywne alerty\n"
        "• `/restart quiz-manager` — restart aplikacji\n"
        "• `/deploy english-teacher` — wdróż aplikację\n"
        "• `/logs watchdog` — ostatnie logi\n"
        "• `/apps` — lista wszystkich aplikacji\n"
        "• `/help` — pełna pomoc\n\n"
        "Albo po prostu napisz co chcesz zrobić! 🚀",
        reply_markup=main_keyboard()
    )

async def cmd_status(chat_id):
    await typing(chat_id)
    apps = await get_apps(force=True)
    if not isinstance(apps, list):
        await send(chat_id, "❌ Nie mogę pobrać statusu z Coolify.")
        return
    
    healthy = [a for a in apps if "running" in a.get("status","")]
    broken  = [a for a in apps if "exited" in a.get("status","") or "restarting" in a.get("status","")]
    unknown = [a for a in apps if "unknown" in a.get("status","")]
    
    lines = [f"*📊 Status infrastruktury* `{datetime.now().strftime('%H:%M %d.%m')}`\n"]
    lines.append(f"✅ {len(healthy)} zdrowych | ❌ {len(broken)} problem | ⚠️ {len(unknown)} nieznany")
    
    if broken:
        lines.append("\n*🔴 Problemy:*")
        for a in broken:
            lines.append(f"  • `{a['name']}` — {a['status']}")
    
    if unknown:
        lines.append("\n*🟡 Nieznany status:*")
        for a in unknown[:5]:
            lines.append(f"  • `{a['name']}`")
    
    lines.append(f"\n*🟢 Zdrowe ({len(healthy)}):*")
    healthy_names = [a['name'] for a in healthy]
    lines.append("  " + ", ".join(f"`{n}`" for n in healthy_names[:12]))
    if len(healthy_names) > 12:
        lines.append(f"  _(+{len(healthy_names)-12} więcej)_")
    
    await send(chat_id, "\n".join(lines), reply_markup=main_keyboard())

async def cmd_smoke(chat_id):
    await typing(chat_id)
    summary = await sb("public_get_smoke_summary") or []
    if not summary:
        await send(chat_id, "⚠️ Brak wyników smoke testów. SmokeTester może być w trakcie cyklu.")
        return
    
    passed = [s for s in summary if s.get("passed")]
    failed = [s for s in summary if not s.get("passed")]
    
    lines = [f"*🧪 Smoke testy* `{datetime.now().strftime('%H:%M')}`\n"]
    lines.append(f"✅ {len(passed)}/{len(summary)} testów OK")
    
    if failed:
        lines.append("\n*❌ Nieprzechodzące:*")
        for s in failed[:10]:
            app = s.get("app_name","?")
            test = s.get("test_name","?")
            det = s.get("details","")[:50]
            lines.append(f"  • `{app}/{test}` — {det}")
    else:
        lines.append("\n🎉 Wszystkie testy przechodzą!")
    
    await send(chat_id, "\n".join(lines))

async def cmd_alerts(chat_id):
    await typing(chat_id)
    alerts = await sb("public_get_alerts") or []
    if not alerts:
        await send(chat_id, "✅ Brak aktywnych alertów! System działa poprawnie.")
        return
    
    lines = [f"*🚨 Aktywne alerty ({len(alerts)})*\n"]
    for a in alerts[:8]:
        sev = a.get("severity","?")
        icon = "🔴" if sev == "critical" else "🟡"
        lines.append(f"{icon} `{a.get('app_name')}` [{sev}]")
        lines.append(f"   {a.get('message','')[:80]}")
        lines.append(f"   _{a.get('source','?')} — {str(a.get('created_at',''))[:16]}_")
    
    await send(chat_id, "\n".join(lines))

async def cmd_restart(chat_id, text):
    name, uuid = find_app(text)
    if not uuid:
        await send(chat_id, "❓ Którą aplikację? Np. `/restart quiz-manager`\n\nDostępne: " + 
                   ", ".join(f"`{k}`" for k in list(APP_MAP.keys())[:10]))
        return
    await typing(chat_id)
    r = await coolify(f"/applications/{uuid}/restart","POST")
    if r.get("message"):
        await send(chat_id, f"🔄 Restart `{name}` zlecony!\nSprawdź za ~1min: `/status`")
    else:
        await send(chat_id, f"❌ Nie udało się zrestartować `{name}`.\nOdpowiedź: {str(r)[:100]}")

async def cmd_deploy(chat_id, text):
    name, uuid = find_app(text)
    if not uuid:
        await send(chat_id, "❓ Którą aplikację deploować? Np. `/deploy quiz-manager`")
        return
    await typing(chat_id)
    r = await coolify(f"/deploy?uuid={uuid}&force=true","GET")
    deps = r.get("deployments",[{}]) if isinstance(r,dict) else []
    dep_id = deps[0].get("deployment_uuid","?")[:12] if deps else "?"
    await send(chat_id, f"🚀 Deploy `{name}` zlecony!\nID: `{dep_id}...`\nSprawdź za ~3min: `/status`")

async def cmd_logs(chat_id, text):
    name, uuid = find_app(text)
    if not uuid:
        await send(chat_id, "❓ Np. `/logs watchdog` lub `/logs quiz-manager`")
        return
    await typing(chat_id)
    r = await coolify(f"/applications/{uuid}/logs?lines=30")
    logs = r.get("logs","") if isinstance(r,dict) else ""
    if not logs:
        await send(chat_id, f"❌ Brak logów dla `{name}` (może nie działa?)")
        return
    lines = [l for l in logs.split("\n") if l.strip()][-20:]
    output = f"*📋 Logi `{name}` (ostatnie 20 linii):*\n```\n"
    output += "\n".join(lines[-15:])
    output += "\n```"
    await send_long(chat_id, output)

async def cmd_apps(chat_id):
    await typing(chat_id)
    apps = await get_apps()
    if not isinstance(apps, list):
        await send(chat_id, "❌ Błąd pobierania listy aplikacji.")
        return
    lines = ["*📱 Wszystkie aplikacje:*\n"]
    for a in sorted(apps, key=lambda x: x.get("name","")):
        s = a.get("status","?")
        icon = "✅" if "running:healthy" in s else "🟡" if "running" in s else "❌"
        fqdn = a.get("fqdn","").replace("https://","").replace("http://","")[:30]
        lines.append(f"{icon} `{a['name'][:25]}` {f'→ {fqdn}' if fqdn and 'sslip' not in fqdn else ''}")
    await send_long(chat_id, "\n".join(lines))

async def cmd_help(chat_id):
    await send(chat_id,
        "*🤖 Guardian Bot — Pełna pomoc*\n\n"
        "*Komendy:*\n"
        "• `/status` — stan wszystkich aplikacji\n"
        "• `/smoke` — wyniki testów funkcjonalnych\n"
        "• `/alerts` — aktywne alerty z watchdog/autoheal\n"
        "• `/apps` — pełna lista aplikacji\n"
        "• `/restart <app>` — zrestartuj aplikację\n"
        "• `/deploy <app>` — wdróż najnowszy kod\n"
        "• `/logs <app>` — ostatnie logi\n"
        "• `/clear` — wyczyść historię rozmowy\n\n"
        "*Przykłady aplikacji:*\n"
        "`quiz`, `agentflow`, `manus`, `english`, `inbox`, `hub`, `sentinel`, `watchdog`, `autoheal`\n\n"
        "*AI:*\nMożesz pisać normalnie — Guardian rozumie polecenia w języku naturalnym i odpowiada z kontekstem infrastruktury."
    )

# ── Message router ────────────────────────────────────────────────────────────
async def handle_message(chat_id: str, user_id: str, text: str):
    # Auth
    if ALLOWED and user_id not in ALLOWED and chat_id not in ALLOWED:
        await send(chat_id, "🔒 Brak dostępu.")
        return
    
    log.info(f"[{chat_id}] {text[:80]}")
    t = text.strip()
    t_lower = t.lower()
    
    # Commands
    if t_lower in ["/start", "/help", "help", "pomoc"]:
        await cmd_start(chat_id); return
    if t_lower.startswith("/help"):
        await cmd_help(chat_id); return
    if t_lower in ["/status","status","stan","co slychac","co słychać","status?"]:
        await cmd_status(chat_id); return
    if t_lower in ["/smoke","smoke","testy"]:
        await cmd_smoke(chat_id); return
    if t_lower in ["/alerts","alerty","alarmy"]:
        await cmd_alerts(chat_id); return
    if t_lower in ["/apps","apps","aplikacje"]:
        await cmd_apps(chat_id); return
    if t_lower.startswith("/restart") or re.search(r'\b(zrestartuj|restart)\b', t_lower):
        await cmd_restart(chat_id, t); return
    if t_lower.startswith("/deploy") or re.search(r'\b(deploy|wdroz|wdróż)\b', t_lower):
        await cmd_deploy(chat_id, t); return
    if t_lower.startswith("/logs") or re.search(r'\b(logi|logs)\b', t_lower):
        await cmd_logs(chat_id, t); return
    if t_lower in ["/clear","clear","wyczyść","wyczysc","zapomnij"]:
        sessions.pop(chat_id, None)
        await send(chat_id, "🧹 Historia wyczyszczona!"); return
    
    # Natural language → Claude with context
    await typing(chat_id)
    
    # Detect if user wants action
    extra_ctx = ""
    if any(w in t_lower for w in ["nie działa","błąd","error","crash","problem","awaria"]):
        apps = await get_apps(force=True)
        broken = [a for a in apps if "exited" in a.get("status","") or "restarting" in a.get("status","")]
        if broken:
            extra_ctx = "UWAGA: Broken apps: " + ", ".join(a["name"] for a in broken)
    
    reply = await ask_claude(chat_id, t, extra_ctx)
    await send_long(chat_id, reply)

async def handle_callback(callback_id: str, chat_id: str, user_id: str, data: str):
    """Handle inline keyboard button presses."""
    await answer_callback(callback_id)
    
    if data == "status" or data == "refresh":
        await cmd_status(chat_id)
    elif data == "smoke":
        await cmd_smoke(chat_id)
    elif data == "alerts":
        await cmd_alerts(chat_id)
    elif data.startswith("logs_"):
        app_name = data[5:]
        await cmd_logs(chat_id, app_name)
    elif data.startswith("restart_"):
        app_name = data[8:]
        await cmd_restart(chat_id, app_name)

# ── Main poll loop ────────────────────────────────────────────────────────────
async def main():
    log.info("🤖 Guardian Bot v3 starting")
    
    # Verify token
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{TG}/getMe")
        me = r.json()
    if not me.get("ok"):
        log.error(f"Invalid token: {me}")
        return
    
    bot_name = me["result"]["username"]
    log.info(f"✅ @{bot_name} ready | allowed: {ALLOWED or 'ALL'}")
    
    # Notify admin on startup
    if ADMIN_ID:
        apps = await get_apps()
        broken = [a["name"] for a in apps if "exited" in a.get("status","") or "restarting" in a.get("status","")]
        healthy = sum(1 for a in apps if "running" in a.get("status",""))
        msg = f"🤖 *Guardian v3 uruchomiony!*\n\n📊 Infrastruktura: {healthy}/{len(apps)} OK"
        if broken:
            msg += f"\n⚠️ Problemy: {', '.join(broken)}"
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                await c.post(f"{TG}/sendMessage",
                    json={"chat_id":ADMIN_ID,"text":msg,"parse_mode":"Markdown"})
        except: pass
    
    offset = 0
    log.info("Polling updates...")
    
    while True:
        try:
            async with httpx.AsyncClient(timeout=35) as c:
                r = await c.get(f"{TG}/getUpdates",
                    params={"offset":offset,"timeout":30,"limit":10})
                data = r.json()
            
            if not data.get("ok"):
                await asyncio.sleep(5)
                continue
            
            for upd in data["result"]:
                offset = upd["update_id"] + 1
                
                if "message" in upd or "edited_message" in upd:
                    msg = upd.get("message") or upd.get("edited_message")
                    chat_id = str(msg["chat"]["id"])
                    user_id = str(msg["from"]["id"])
                    text = msg.get("text","").strip()
                    if text:
                        asyncio.create_task(handle_message(chat_id, user_id, text))
                
                elif "callback_query" in upd:
                    cb = upd["callback_query"]
                    asyncio.create_task(handle_callback(
                        cb["id"],
                        str(cb["message"]["chat"]["id"]),
                        str(cb["from"]["id"]),
                        cb.get("data","")
                    ))
        
        except asyncio.CancelledError:
            break
        except Exception as ex:
            log.error(f"Poll error: {ex}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
