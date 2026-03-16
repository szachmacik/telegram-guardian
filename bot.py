"""
Telegram Guardian Bot — ofshore.dev Superagent
Handles natural language conversations and routes to:
  - Claude (AI responses, question answering)  
  - Infrastructure commands (app status, deploy, restart)
  - Cross-app intelligence (SmokeTester results, Watchdog alerts)

Sends clean natural language responses (no code blocks unless asked).
"""
import asyncio, json, os, logging, re
import httpx
from datetime import datetime

TELEGRAM_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_KEY   = os.environ["ANTHROPIC_API_KEY"]
COOLIFY_URL     = os.environ.get("COOLIFY_URL", "https://coolify.ofshore.dev")
COOLIFY_TOKEN   = os.environ["COOLIFY_TOKEN"]
SUPABASE_URL    = os.environ["SUPABASE_URL"]
SUPABASE_KEY    = os.environ["SUPABASE_KEY"]
ALLOWED_USERS   = set(os.environ.get("ALLOWED_TELEGRAM_IDS","").split(","))
ADMIN_CHAT_ID   = os.environ.get("ADMIN_CHAT_ID","")

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [TG] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("tgbot")

TG = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
OFFSET = 0
sessions: dict[str, list] = {}  # chat_id → conversation history

SYSTEM_PROMPT = """You are the ofshore.dev AI Guardian — a conversational superagent that manages infrastructure and AI applications for Maciej.

You can:
- Answer questions about any of the apps (AgentFlow, Quiz Manager, Omnichannel, English Teacher, Manus Brain, Integration Hub, etc.)
- Report infrastructure status (which apps are healthy, recent failures)
- Trigger deployments and restarts
- Show smoke test results and watchdog alerts
- Explain what any app does and how to use it
- Discuss technical issues and suggest fixes

Apps on ofshore.dev:
- agentflow.ofshore.dev — AI task orchestration platform
- quiz.ofshore.dev — Quiz management with fraud detection
- inbox.ofshore.dev — Omnichannel inbox (email/SMS/chat)
- english-teacher.ofshore.dev — AI lesson generator
- brain.ofshore.dev — Multi-AI router (Claude/Kimi/DeepSeek)
- hub.ofshore.dev — Integration hub (ManyChat/webhooks)
- sentinel.ofshore.dev / ai-control-center.ofshore.dev — Control center
- security.ofshore.dev — Cybersecurity dashboard
- n8n.ofshore.dev — Workflow automation

STYLE: Always respond in NATURAL LANGUAGE. Never use code blocks, JSON, or technical jargon unless the user specifically asks. Be concise (2-4 sentences), friendly, and actionable. When reporting status, use simple words like "all good", "one app is down", etc.

LANGUAGE: Always respond in the same language as the user's message (Polish if they write in Polish, English if they write in English).

If the user asks you to DO something (restart an app, show logs, check status), say what you're doing and then report the result in natural language."""

# ── Supabase ──────────────────────────────────────────────────────────────────
async def sb_rpc(fn, params={}):
    headers = {"apikey":SUPABASE_KEY,"Authorization":f"Bearer {SUPABASE_KEY}",
                "Content-Type":"application/json"}
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(f"{SUPABASE_URL}/rest/v1/rpc/{fn}", headers=headers, json=params)
        return r.json() if r.status_code == 200 else None

# ── Coolify ───────────────────────────────────────────────────────────────────
async def coolify(path, method="GET", body=None):
    headers = {"Authorization": f"Bearer {COOLIFY_TOKEN}"}
    async with httpx.AsyncClient(timeout=15) as c:
        if method == "GET":
            r = await c.get(f"{COOLIFY_URL}/api/v1{path}", headers=headers)
        else:
            r = await c.request(method, f"{COOLIFY_URL}/api/v1{path}",
                                 headers=headers, json=body or {})
        return r.json() if r.status_code in (200,201) else {}

async def get_infra_context() -> str:
    """Get current infrastructure state for Claude's context."""
    apps = await coolify("/applications")
    if not isinstance(apps, list): return "Infrastructure status unavailable."
    
    healthy = [a["name"] for a in apps if "running" in a.get("status","")]
    broken  = [(a["name"], a.get("status")) for a in apps
                if "exited" in a.get("status","") or "restarting" in a.get("status","")]
    
    # Get smoke test summary
    smoke = await sb_rpc("public_get_smoke_summary") or []
    failed_tests = [(s["app_name"], s["test_name"]) for s in smoke if not s.get("passed")]
    
    # Get recent alerts
    alerts = await sb_rpc("public_get_alerts") or []
    
    ctx = f"""CURRENT INFRASTRUCTURE STATE (as of {datetime.now().strftime('%H:%M')}):
- Total apps: {len(apps)} | Healthy: {len(healthy)} | Down: {len(broken)}
"""
    if broken:
        ctx += f"- Broken apps: {', '.join(f'{n} ({s})' for n,s in broken)}\n"
    if failed_tests:
        ctx += f"- Failed smoke tests: {', '.join(f'{a}/{t}' for a,t in failed_tests[:5])}\n"
    if alerts:
        ctx += f"- Active alerts: {len(alerts)} unhandled\n"
    
    return ctx

# ── Intent detection ──────────────────────────────────────────────────────────
def detect_intent(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["status", "stan", "jak działa", "co się dzieje", "health", "działa"]):
        return "status"
    if any(w in t for w in ["restart", "zrestartuj", "reboot", "uruchom ponownie"]):
        return "restart"
    if any(w in t for w in ["deploy", "wdroz", "zaktualizuj", "update"]):
        return "deploy"
    if any(w in t for w in ["logi", "logs", "błąd", "error", "co się stało"]):
        return "logs"
    if any(w in t for w in ["smoke", "test", "sprawdź", "check"]):
        return "smoke"
    if any(w in t for w in ["alert", "alarm", "problem"]):
        return "alerts"
    return "chat"

def find_app_in_text(text: str) -> tuple[str, str] | None:
    """Find app name and UUID from user text."""
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
        "wp": "wp_uuid",
        "watchdog": "g8csck0kw8c0sc0cosg0cw84",
        "autoheal": "vcgk0g4sc4sck0kkc8k080gk",
        "smoketester": "qws0sk4gooo4ok8cswc0o0kw",
    }
    t = text.lower()
    for name, uuid in APP_MAP.items():
        if name in t:
            return name, uuid
    return None

# ── Action handlers ───────────────────────────────────────────────────────────
async def handle_status(chat_id: str) -> str:
    apps = await coolify("/applications")
    if not isinstance(apps, list):
        return "Nie mogę teraz sprawdzić statusu — Coolify nie odpowiada."
    
    healthy = [a["name"] for a in apps if "running" in a.get("status","")]
    broken  = [(a["name"], a.get("status")) for a in apps
                if "exited" in a.get("status","") or "restarting" in a.get("status","")]
    
    if not broken:
        return f"Wszystkie {len(apps)} aplikacji działa poprawnie. 🟢"
    else:
        broken_list = ", ".join(f"{n}" for n,s in broken)
        return f"Mam {len(broken)} problem(y): {broken_list}. Pozostałe {len(healthy)} appek działa ok."

async def handle_restart(text: str) -> str:
    app = find_app_in_text(text)
    if not app:
        return "Którą aplikację chcesz zrestartować? Powiedz np. 'zrestartuj quiz-manager'."
    name, uuid = app
    r = await coolify(f"/applications/{uuid}/restart", "POST")
    if r.get("message"):
        return f"Restart {name} zlecony. Za chwilę powinien działać."
    return f"Nie udało się zrestartować {name}."

async def handle_smoke() -> str:
    smoke = await sb_rpc("public_get_smoke_summary") or []
    if not smoke:
        return "Brak wyników testów smoke. SmokeTester może jeszcze nie zakończył cyklu."
    
    passed = sum(1 for s in smoke if s.get("passed"))
    failed = [(s["app_name"], s["test_name"]) for s in smoke if not s.get("passed")]
    
    if not failed:
        return f"Wszystkie {passed} testy smoke przechodzą pomyślnie! 🟢"
    
    fail_str = ", ".join(f"{a}/{t}" for a,t in failed[:5])
    return f"{passed}/{len(smoke)} testów przechodzi. Problemy: {fail_str}."

async def handle_logs(text: str) -> str:
    app = find_app_in_text(text)
    if not app:
        return "Podaj nazwę aplikacji, np. 'pokaż logi quiz-manager'."
    name, uuid = app
    r = await coolify(f"/applications/{uuid}/logs?lines=10")
    logs = r.get("logs","") if isinstance(r,dict) else ""
    if not logs:
        return f"Brak logów dla {name} lub aplikacja nie odpowiada."
    # Extract last few meaningful lines
    lines = [l for l in logs.split("\n") if l.strip()][-5:]
    return f"Ostatnie logi {name}:\n" + "\n".join(lines)

# ── Claude AI ─────────────────────────────────────────────────────────────────
async def ask_claude(chat_id: str, user_msg: str) -> str:
    history = sessions.get(chat_id, [])
    
    # Build context
    infra_ctx = await get_infra_context()
    
    messages = history[-10:] + [{"role":"user","content":user_msg}]
    
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01",
                         "content-type":"application/json"},
                json={"model":"claude-sonnet-4-6","max_tokens":1024,
                      "system": SYSTEM_PROMPT + "\n\n" + infra_ctx,
                      "messages": messages})
            reply = r.json()["content"][0]["text"]
    except Exception as ex:
        reply = f"Przepraszam, wystąpił błąd AI: {str(ex)[:80]}"
    
    # Update session
    history.append({"role":"user","content":user_msg})
    history.append({"role":"assistant","content":reply})
    sessions[chat_id] = history[-20:]  # keep last 10 exchanges
    
    return reply

# ── Telegram ──────────────────────────────────────────────────────────────────
async def send(chat_id, text: str):
    """Send message - use Markdown formatting."""
    # Clean up any code blocks for natural language mode
    # (Claude might still add them occasionally)
    clean = text.strip()
    
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post(f"{TG}/sendMessage", json={
            "chat_id": chat_id,
            "text": clean,
            "parse_mode": "Markdown"
        })

async def typing(chat_id):
    async with httpx.AsyncClient(timeout=5) as c:
        await c.post(f"{TG}/sendChatAction",
                     json={"chat_id": chat_id, "action": "typing"})

async def handle_update(update: dict):
    msg = update.get("message") or update.get("edited_message")
    if not msg: return
    
    chat_id = str(msg["chat"]["id"])
    user_id = str(msg["from"]["id"])
    text = msg.get("text","").strip()
    
    if not text: return
    
    # Auth check
    if ALLOWED_USERS and user_id not in ALLOWED_USERS and chat_id not in ALLOWED_USERS:
        await send(chat_id, "Brak dostępu. Skontaktuj się z administratorem.")
        return
    
    log.info(f"[{chat_id}] {text[:60]}")
    await typing(chat_id)
    
    # Route by intent
    intent = detect_intent(text)
    
    try:
        if intent == "status":
            reply = await handle_status(chat_id)
        elif intent == "restart" and find_app_in_text(text):
            reply = await handle_restart(text)
        elif intent == "smoke":
            reply = await handle_smoke()
        elif intent == "logs" and find_app_in_text(text):
            reply = await handle_logs(text)
        else:
            # All other requests go to Claude with full context
            reply = await ask_claude(chat_id, text)
    except Exception as ex:
        log.error(f"Handle error: {ex}")
        reply = "Coś poszło nie tak. Spróbuj ponownie za chwilę."
    
    await send(chat_id, reply)

async def poll():
    global OFFSET
    log.info("🤖 Telegram Guardian Bot starting...")
    log.info(f"   Allowed users: {ALLOWED_USERS or 'ALL'}")
    
    while True:
        try:
            async with httpx.AsyncClient(timeout=35) as c:
                r = await c.get(f"{TG}/getUpdates",
                    params={"offset": OFFSET, "timeout": 30, "limit": 10})
                data = r.json()
                
                if not data.get("ok"): 
                    await asyncio.sleep(5)
                    continue
                
                for update in data["result"]:
                    OFFSET = update["update_id"] + 1
                    asyncio.create_task(handle_update(update))
                    
        except asyncio.CancelledError:
            break
        except Exception as ex:
            log.error(f"Poll error: {ex}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(poll())
