"""
Telegram Guardian Bot v4 — AI Manager ofshore.dev
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AI: 100% Claude (Anthropic) — zero OpenAI, zero ograniczeń "nie mogę"
Routing: claude-sonnet-4-6 do złożonych, haiku do szybkich, zawsze działa

Możliwości managera:
  ✅ Pełna kontrola infrastruktury (status/restart/deploy/logi/envs)
  ✅ Zarządzanie przez język naturalny PL/EN
  ✅ Smoke testy i alerty w czasie rzeczywistym
  ✅ Historia rozmów (kontekst)
  ✅ Inline klawiatura z przyciskami
  ✅ Proaktywne powiadomienia (alerty infra → Telegram)
  ✅ Raporty z danych Supabase
  ✅ Wykonywanie akcji: deploy, restart, trigger
  ✅ Zadawanie pytań do guardianów poszczególnych appów
  ✅ Claude ZAWSZE odpowie — nigdy "nie mogę tego zrobić"
"""
import asyncio, json, os, logging, re, time
import httpx
from datetime import datetime, timezone

# ── Config ─────────────────────────────────────────────────────────────────
TG_TOKEN      = os.environ["TELEGRAM_BOT_TOKEN"]
CLAUDE_KEY    = os.environ["ANTHROPIC_API_KEY"]   # tylko Claude, zero OpenAI
COOLIFY_URL   = os.environ.get("COOLIFY_URL","https://coolify.ofshore.dev")
COOLIFY_TOKEN = os.environ.get("COOLIFY_TOKEN","")
SB_URL        = os.environ.get("SUPABASE_URL","")
SB_KEY        = os.environ.get("SUPABASE_KEY","")
ALLOWED       = set(x.strip() for x in os.environ.get("ALLOWED_TELEGRAM_IDS","").split(",") if x.strip())
ADMIN_ID      = os.environ.get("ADMIN_CHAT_ID","")
TG            = f"https://api.telegram.org/bot{TG_TOKEN}"

# ── AI Models routing ───────────────────────────────────────────────────────
# Haiku: szybkie odpowiedzi, komendy, status (tanie)
# Sonnet: złożone analizy, debugowanie, planowanie (dokładne)
MODEL_FAST  = "claude-haiku-4-5-20251001"
MODEL_SMART = "claude-sonnet-4-6"

def pick_model(text: str) -> str:
    """Wybiera model na podstawie złożoności pytania."""
    t = text.lower()
    # Złożone → Sonnet
    if any(w in t for w in [
        "dlaczego","analyze","analiza","debuguj","debug","błąd w kodzie",
        "zaplanuj","strategia","porównaj","architektura","optymalizuj",
        "wyjaśnij dokładnie","jak naprawić","co jest przyczyną"
    ]):
        return MODEL_SMART
    # Proste → Haiku (szybsze i tańsze)
    return MODEL_FAST

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [BOT] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("bot")

sessions: dict[str, list] = {}   # historia rozmów
app_cache: list = []
cache_ts: float = 0
alert_notified: set = set()       # alerty już notyfikowane

# ── App registry ────────────────────────────────────────────────────────────
APPS = {
    "agentflow":     ("ts0c0wgco8wo8kgocok84cws", "agentflow.ofshore.dev"),
    "quiz":          ("yssco8cc800ow880w0wo48o0",  "quiz.ofshore.dev"),
    "quiz-manager":  ("yssco8cc800ow880w0wo48o0",  "quiz.ofshore.dev"),
    "inbox":         ("tcww08co80wsgwwg8swwgss8",  "inbox.ofshore.dev"),
    "omnichannel":   ("tcww08co80wsgwwg8swwgss8",  "inbox.ofshore.dev"),
    "english":       ("d0800oks0g4gws0kw04ck00s",  "english-teacher.ofshore.dev"),
    "english-teacher":("d0800oks0g4gws0kw04ck00s", "english-teacher.ofshore.dev"),
    "manus":         ("kssk4o48sgosgwwck8s8ws80",  "brain.ofshore.dev"),
    "brain":         ("kssk4o48sgosgwwck8s8ws80",  "brain.ofshore.dev"),
    "integration":   ("s44sck0k0os0k4w0www00cg4",  "hub.ofshore.dev"),
    "hub":           ("s44sck0k0os0k4w0www00cg4",  "hub.ofshore.dev"),
    "sentinel":      ("rs488c4ccg48w48gocgog8sg",  "sentinel.ofshore.dev"),
    "ai-control":    ("hokscgg48sowg44wwc044gk8",  "ai-control-center.ofshore.dev"),
    "security":      ("wg0gkco8g0swgccc8www04gg",  "security.ofshore.dev"),
    "watchdog":      ("g8csck0kw8c0sc0cosg0cw84",  None),
    "autoheal":      ("vcgk0g4sc4sck0kkc8k080gk",  None),
    "smoketester":   ("qws0sk4gooo4ok8cswc0o0kw",  None),
    "telegram":      ("qook8w0sw4o404swcoookg00",   None),
    "n8n":           (None,                          "n8n.ofshore.dev"),
}

def find_app(text: str) -> tuple[str, str, str]:
    """Zwraca (name, uuid, domain) dla aplikacji wspomnianej w tekście."""
    t = text.lower()
    for name, (uuid, domain) in APPS.items():
        if name in t:
            return name, uuid, domain
    return None, None, None

# ── Supabase helpers ────────────────────────────────────────────────────────
async def sb_rpc(fn: str, params: dict = {}) -> any:
    if not SB_URL: return None
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{SB_URL}/rest/v1/rpc/{fn}",
                headers={"apikey":SB_KEY,"Authorization":f"Bearer {SB_KEY}",
                         "Content-Type":"application/json"}, json=params)
            return r.json() if r.status_code == 200 else None
    except: return None

# ── Coolify helpers ─────────────────────────────────────────────────────────
async def cf(path: str, method="GET", body=None) -> any:
    if not COOLIFY_TOKEN: return {}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.request(method, f"{COOLIFY_URL}/api/v1{path}",
                headers={"Authorization":f"Bearer {COOLIFY_TOKEN}"}, json=body)
            return r.json() if r.status_code in (200,201) else {}
    except: return {}

async def get_apps(force=False) -> list:
    global app_cache, cache_ts
    now = time.monotonic()
    if not force and app_cache and now-cache_ts < 60:
        return app_cache
    apps = await cf("/applications")
    if isinstance(apps, list):
        app_cache = apps; cache_ts = now
    return app_cache if app_cache else []

# ── Infra snapshot ──────────────────────────────────────────────────────────
async def infra_snapshot() -> dict:
    """Zbiera pełny snapshot stanu infrastruktury."""
    apps   = await get_apps(force=True)
    smoke  = await sb_rpc("public_get_smoke_summary") or []
    alerts = await sb_rpc("public_get_alerts") or []

    healthy = [a for a in apps if "running" in a.get("status","")]
    broken  = [a for a in apps if "exited" in a.get("status","") or "restarting" in a.get("status","")]
    failed_smoke = [s for s in smoke if not s.get("passed")]

    return {
        "total": len(apps), "healthy": len(healthy), "broken": len(broken),
        "broken_names": [a["name"] for a in broken],
        "healthy_names": [a["name"] for a in healthy],
        "smoke_total": len(smoke), "smoke_failed": len(failed_smoke),
        "smoke_failures": [(s["app_name"],s["test_name"]) for s in failed_smoke[:6]],
        "alerts": len(alerts),
        "alert_list": alerts[:5],
        "ts": datetime.now().strftime("%H:%M %d.%m.%Y"),
    }

async def infra_ctx_str() -> str:
    snap = await infra_snapshot()
    ctx = f"INFRA [{snap['ts']}]: {snap['healthy']}/{snap['total']} apps OK"
    if snap["broken_names"]:
        ctx += f" | DOWN: {', '.join(snap['broken_names'])}"
    if snap["smoke_failures"]:
        ctx += f" | SMOKE_FAIL: {', '.join(f'{a}/{t}' for a,t in snap['smoke_failures'][:3])}"
    if snap["alerts"] > 0:
        ctx += f" | {snap['alerts']} ALERTÓW"
    return ctx

# ── Claude AI ───────────────────────────────────────────────────────────────
PERSONA = """Jesteś AI Guardian — osobisty manager infrastruktury Macieja, właściciela platformy ofshore.dev.

CHARAKTER:
- Mówisz konkretnie, bez owijania w bawełnę
- Nigdy nie mówisz "nie mogę", "nie jestem w stanie", "nie mam dostępu" — zawsze coś zrobisz
- Jeśli czegoś nie masz, mówisz wprost CO zrobić żeby to zmienić
- Używasz emoji z umiarem — tylko gdy pasują
- Odpowiadasz po polsku gdy Maciej pisze po polsku

TWOJE NARZĘDZIA (możesz i rób to):
• Pobierasz status infra z Coolify (24+ aplikacji)
• Restartujesz i deplorujesz aplikacje
• Czytasz logi dowolnej aplikacji
• Sprawdzasz wyniki smoke testów (co 10min)
• Czytasz alerty z Watchdog/AutoHeal
• Pytasz guardian botów poszczególnych appów
• Zarządzasz Supabase: dane, statystyki
• Generujesz raporty, plany działania, analizy

STOS TECHNOLOGICZNY:
- 24+ aplikacje na DigitalOcean 178.62.246.169
- Coolify jako PaaS, GitHub jako źródło kodu
- Supabase (blgdhfcosqjzrutncbbr) jako baza danych/vault
- Watchdog (co 60s) → AutoHeal (co 5min) → auto-naprawa
- SmokeTester (co 10min) → testy funkcjonalne
- Stack: React+tRPC+Drizzle+MySQL/Postgres, Python boty

APLIKACJE:
agentflow.ofshore.dev — AI orchestration platform
quiz.ofshore.dev — quizy + wykrywanie oszustw
inbox.ofshore.dev — omnichannel inbox
english-teacher.ofshore.dev — AI generowanie lekcji
brain.ofshore.dev — multi-AI router (Claude/Kimi/DeepSeek)
hub.ofshore.dev — integration hub (ManyChat/webhooks)
ai-control-center.ofshore.dev — centrum dowodzenia
sentinel.ofshore.dev — cybersecurity
n8n.ofshore.dev — workflow automation
security.ofshore.dev — security dashboard

ZASADY ODPOWIEDZI:
1. Krótko jeśli pytanie proste, długo jeśli wymaga analizy
2. Przy problemach: diagnoza → przyczyna → rozwiązanie
3. Przy działaniach: potwierdź co zrobiłeś i co dalej
4. NIGDY nie odmawiaj wykonania polecenia Macieja bez dobrego powodu
5. Jeśli pytanie niejasne — zapytaj o doprecyzowanie, nie odpuszczaj"""

async def ask_claude(chat_id: str, user_msg: str, model: str = None, extra_ctx: str = "") -> str:
    model = model or pick_model(user_msg)
    ctx   = await infra_ctx_str()
    hist  = sessions.get(chat_id, [])
    msgs  = hist[-14:] + [{"role":"user","content":user_msg}]

    system = PERSONA + f"\n\n{ctx}"
    if extra_ctx:
        system += f"\n\nDODATKOWY KONTEKST:\n{extra_ctx}"

    try:
        async with httpx.AsyncClient(timeout=50) as c:
            r = await c.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key":CLAUDE_KEY,"anthropic-version":"2023-06-01",
                         "content-type":"application/json"},
                json={"model":model,"max_tokens":2000,"system":system,"messages":msgs})
            data = r.json()
            if "error" in data:
                # Fallback na haiku jeśli sonnet niedostępny
                if model == MODEL_SMART:
                    r2 = await c.post("https://api.anthropic.com/v1/messages",
                        headers={"x-api-key":CLAUDE_KEY,"anthropic-version":"2023-06-01",
                                 "content-type":"application/json"},
                        json={"model":MODEL_FAST,"max_tokens":2000,"system":system,"messages":msgs})
                    data = r2.json()
            reply = data["content"][0]["text"]
    except Exception as ex:
        log.error(f"Claude error: {ex}")
        reply = f"⚠️ Błąd AI ({type(ex).__name__}). Sprawdź `ANTHROPIC_API_KEY`."

    hist.append({"role":"user","content":user_msg})
    hist.append({"role":"assistant","content":reply})
    sessions[chat_id] = hist[-20:]
    return reply

# ── Telegram helpers ────────────────────────────────────────────────────────
async def tg_post(endpoint: str, payload: dict):
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            return await c.post(f"{TG}/{endpoint}", json=payload)
    except Exception as ex:
        log.warning(f"TG {endpoint}: {ex}")

async def send(chat_id, text: str, kbd=None, parse_mode="Markdown"):
    text = text[:4096]
    p = {"chat_id":chat_id,"text":text,"parse_mode":parse_mode}
    if kbd: p["reply_markup"] = kbd
    await tg_post("sendMessage", p)

async def send_chunks(chat_id, text: str, parse_mode="Markdown"):
    for i in range(0, len(text), 3800):
        await send(chat_id, text[i:i+3800], parse_mode=parse_mode)
        if len(text) > 3800: await asyncio.sleep(0.4)

async def typing(chat_id):
    await tg_post("sendChatAction", {"chat_id":chat_id,"action":"typing"})

async def answer_cb(cb_id: str, text="✅"):
    await tg_post("answerCallbackQuery", {"callback_query_id":cb_id,"text":text})

# ── Keyboards ───────────────────────────────────────────────────────────────
def kbd_main():
    return {"inline_keyboard": [
        [{"text":"📊 Status","callback_data":"status"},
         {"text":"🧪 Testy","callback_data":"smoke"},
         {"text":"🚨 Alerty","callback_data":"alerts"}],
        [{"text":"📋 Watchdog","callback_data":"logs_watchdog"},
         {"text":"📋 AutoHeal","callback_data":"logs_autoheal"},
         {"text":"📋 SmokeTester","callback_data":"logs_smoketester"}],
        [{"text":"📱 Wszystkie appki","callback_data":"apps"},
         {"text":"🔄 Odśwież","callback_data":"status"}],
    ]}

def kbd_app(name: str, uuid: str):
    btns = [
        [{"text":f"🔄 Restart {name}","callback_data":f"restart_{uuid}_{name}"},
         {"text":f"🚀 Deploy {name}","callback_data":f"deploy_{uuid}_{name}"}],
        [{"text":f"📋 Logi {name}","callback_data":f"logs_{name}"}],
        [{"text":"◀️ Powrót","callback_data":"status"}],
    ]
    return {"inline_keyboard": btns}

# ── Action handlers ─────────────────────────────────────────────────────────
async def do_status(chat_id):
    await typing(chat_id)
    snap = await infra_snapshot()
    
    lines = [f"*📊 Infrastruktura* `{snap['ts']}`\n",
             f"✅ {snap['healthy']} OK  |  ❌ {snap['broken']} problem  |  "
             f"📱 {snap['total']} łącznie"]
    
    if snap["broken_names"]:
        lines.append("\n*🔴 Problemy:*")
        for n in snap["broken_names"]:
            lines.append(f"  • `{n}`")
    
    if snap["smoke_failures"]:
        lines.append(f"\n*🧪 Smoke fails ({snap['smoke_failed']}/{snap['smoke_total']}):*")
        for app, test in snap["smoke_failures"][:5]:
            lines.append(f"  • `{app}` / {test}")
    
    if snap["alerts"] > 0:
        lines.append(f"\n*🚨 Aktywne alerty: {snap['alerts']}*")

    healthy_str = ", ".join(f"`{n}`" for n in snap["healthy_names"][:10])
    if snap["healthy_names"]:
        lines.append(f"\n*🟢 Zdrowe:* {healthy_str}")
        if len(snap["healthy_names"]) > 10:
            lines.append(f"_...i {len(snap['healthy_names'])-10} więcej_")

    await send(chat_id, "\n".join(lines), kbd=kbd_main())

async def do_smoke(chat_id):
    await typing(chat_id)
    summary = await sb_rpc("public_get_smoke_summary") or []
    if not summary:
        await send(chat_id, "⚠️ Brak wyników. SmokeTester może być w trakcie cyklu (~10min).")
        return
    
    ok = [s for s in summary if s.get("passed")]
    fail = [s for s in summary if not s.get("passed")]
    
    lines = [f"*🧪 Smoke testy* `{datetime.now().strftime('%H:%M')}`\n",
             f"✅ {len(ok)}/{len(summary)} przechodzi"]
    
    if fail:
        lines.append("\n*❌ Nieprzechodzące:*")
        for s in fail[:12]:
            det = s.get("details","")[:55]
            lines.append(f"  • `{s['app_name']}/{s['test_name']}` — {det}")
    else:
        lines.append("\n🎉 Wszystkie testy OK!")
    
    await send(chat_id, "\n".join(lines))

async def do_alerts(chat_id):
    await typing(chat_id)
    alerts = await sb_rpc("public_get_alerts") or []
    if not alerts:
        await send(chat_id, "✅ Brak aktywnych alertów. System działa poprawnie.")
        return
    
    lines = [f"*🚨 Alerty ({len(alerts)})*\n"]
    for a in alerts[:8]:
        sev = a.get("severity","?")
        icon = "🔴" if sev == "critical" else "🟡"
        lines.append(f"{icon} `{a.get('app_name')}` [{sev}]")
        lines.append(f"   _{a.get('message','')[:80]}_")
        ts = str(a.get("created_at",""))[:16]
        lines.append(f"   `{a.get('source','?')}` • {ts}")
    
    await send(chat_id, "\n".join(lines))

async def do_logs(chat_id, app_name: str):
    name, uuid, _ = find_app(app_name)
    if not uuid:
        await send(chat_id, f"❓ Nie znam aplikacji `{app_name}`.\nSpróbuj: watchdog, autoheal, quiz, manus...")
        return
    await typing(chat_id)
    r = await cf(f"/applications/{uuid}/logs?lines=40")
    logs = r.get("logs","") if isinstance(r,dict) else ""
    if not logs:
        await send(chat_id, f"❌ Brak logów dla `{name}` (może nie działa?)")
        return
    lines = [l for l in logs.split("\n") if l.strip()][-25:]
    out = f"*📋 Logi `{name}` (ostatnie 25)*\n```\n" + "\n".join(lines) + "\n```"
    await send_chunks(chat_id, out)

async def do_restart(chat_id, app_ref: str):
    name, uuid, _ = find_app(app_ref)
    if not uuid:
        await send(chat_id, f"❓ Nie znam `{app_ref}`. Dostępne: quiz, agentflow, manus, english, inbox...")
        return
    await typing(chat_id)
    r = await cf(f"/applications/{uuid}/restart","POST")
    if r.get("message") or r.get("deployment_uuid"):
        await send(chat_id, f"🔄 Restart `{name}` zlecony! Sprawdź `/status` za ~1min.")
    else:
        await send(chat_id, f"❌ Błąd restartu `{name}`: {str(r)[:100]}")

async def do_deploy(chat_id, app_ref: str):
    name, uuid, _ = find_app(app_ref)
    if not uuid:
        await send(chat_id, f"❓ Nie znam `{app_ref}`.")
        return
    await typing(chat_id)
    r = await cf(f"/deploy?uuid={uuid}&force=true","GET")
    deps = r.get("deployments",[]) if isinstance(r,dict) else []
    dep_id = deps[0].get("deployment_uuid","?")[:14] if deps else "?"
    await send(chat_id, f"🚀 Deploy `{name}` zlecony!\nID: `{dep_id}...`\nSprawdź za ~3min: `/status`")

async def do_apps(chat_id):
    await typing(chat_id)
    apps = await get_apps(force=True)
    if not apps:
        await send(chat_id, "❌ Błąd pobierania listy."); return
    
    lines = [f"*📱 Aplikacje ({len(apps)})*\n"]
    for a in sorted(apps, key=lambda x: x.get("name","")):
        s = a.get("status","?")
        icon = "✅" if "healthy" in s else "🟡" if "running" in s else "❌"
        fqdn = a.get("fqdn","").replace("https://","").replace("http://","")
        domain = f" → `{fqdn[:35]}`" if fqdn and "sslip" not in fqdn else ""
        lines.append(f"{icon} `{a['name'][:28]}`{domain}")
    
    await send_chunks(chat_id, "\n".join(lines))

async def do_envs(chat_id, app_ref: str):
    """Pokazuje (bezpiecznie!) env vars aplikacji."""
    name, uuid, _ = find_app(app_ref)
    if not uuid:
        await send(chat_id, f"❓ Nie znam `{app_ref}`."); return
    await typing(chat_id)
    r = await cf(f"/applications/{uuid}/envs")
    envs = json.loads(r) if isinstance(r,str) else r
    if not isinstance(envs, list):
        await send(chat_id, f"❌ Błąd pobierania env vars `{name}`."); return
    
    lines = [f"*🔧 Env vars `{name}` ({len(envs)})*\n"]
    for e in envs:
        k = e.get("key","?")
        v = e.get("value","")
        # Maskuj wrażliwe dane
        if any(x in k.upper() for x in ["TOKEN","KEY","SECRET","PASSWORD","PASS"]):
            v_safe = v[:6]+"..." if len(v) > 6 else "***"
        else:
            v_safe = v[:40]+"..." if len(v) > 40 else v
        lines.append(f"  `{k}` = `{v_safe}`")
    
    await send_chunks(chat_id, "\n".join(lines))

async def do_guardian_ask(chat_id, app_ref: str, question: str):
    """Zadaje pytanie guardian botowi konkretnej aplikacji."""
    name, _, domain = find_app(app_ref)
    if not domain:
        await send(chat_id, f"❓ `{app_ref}` nie ma guardian bota lub nie znam domeny.")
        return
    await typing(chat_id)
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"https://{domain}/api/guardian",
                json={"message":question,"userId":f"guardian_bot_{chat_id}"},
                headers={"Content-Type":"application/json"})
            if r.status_code == 200:
                reply = r.json().get("reply","brak odpowiedzi")
                await send(chat_id,
                    f"*🤖 Guardian `{name}` odpowiada:*\n\n{reply[:1500]}")
            else:
                await send(chat_id,
                    f"❌ Guardian `{name}` nie odpowiada (HTTP {r.status_code}).")
    except Exception as ex:
        await send(chat_id, f"❌ Błąd połączenia z guardian `{name}`: {ex}")

async def do_report(chat_id):
    """Generuje pełny raport managera."""
    await typing(chat_id)
    snap = await infra_snapshot()
    
    # Pobierz ostatnie smoke testy
    smoke = await sb_rpc("public_get_smoke_summary") or []
    alerts = await sb_rpc("public_get_alerts") or []
    
    ctx = f"""
Stan infrastruktury:
- Aplikacje: {snap['healthy']}/{snap['total']} zdrowych, {snap['broken']} problemy
- Problemy: {', '.join(snap['broken_names']) or 'brak'}
- Smoke testy: {snap['smoke_total']-snap['smoke_failed']}/{snap['smoke_total']} OK
- Smoke failures: {', '.join(f"{a}/{t}" for a,t in snap['smoke_failures']) or 'brak'}
- Aktywne alerty: {snap['alerts']}
"""
    
    prompt = "Przygotuj krótki raport dzienny dla mnie jako właściciela. Co działa, co wymaga uwagi, jakie priorytety na dziś. Bądź konkretny, max 300 słów."
    reply = await ask_claude(chat_id, prompt, model=MODEL_SMART, extra_ctx=ctx)
    await send_chunks(chat_id, f"*📋 Raport dzienny {snap['ts']}*\n\n{reply}")

# ── Message router ──────────────────────────────────────────────────────────
async def handle_msg(chat_id: str, user_id: str, text: str):
    if ALLOWED and user_id not in ALLOWED and chat_id not in ALLOWED:
        await send(chat_id, "🔒 Brak dostępu."); return
    
    log.info(f"[{chat_id}] {text[:80]}")
    t = text.strip()
    tl = t.lower()

    # ── Komendy slashowe ──
    if tl in ["/start","start"]:
        await send(chat_id,
            "👋 *Guardian Bot v4 — Twoja prawa ręka*\n\n"
            "*Komendy:*\n"
            "`/status` — stan infra\n"
            "`/smoke` — wyniki smoke testów\n"
            "`/alerts` — aktywne alerty\n"
            "`/apps` — lista aplikacji\n"
            "`/report` — raport dzienny\n"
            "`/logs watchdog` — logi aplikacji\n"
            "`/restart quiz` — restart\n"
            "`/deploy manus` — deploy\n"
            "`/envs quiz` — env vars\n"
            "`/ask quiz 'co to robi?'` — pytaj guardian bota\n"
            "`/clear` — wyczyść historię\n\n"
            "Możesz też pisać normalnie po polsku — rozumiem wszystko 🤖",
            kbd=kbd_main()); return

    if tl in ["/help","help","pomoc"]:
        await send(chat_id,
            "*🆘 Pełna lista poleceń:*\n\n"
            "• `/status` / _co słychać_ / _status_\n"
            "• `/smoke` / _testy_\n"
            "• `/alerts` / _alerty_\n"
            "• `/apps` / _aplikacje_\n"
            "• `/report` — raport AI dzienny\n"
            "• `/logs <app>` — logi (watchdog, quiz, manus...)\n"
            "• `/restart <app>` — restart aplikacji\n"
            "• `/deploy <app>` — deploy z GitHub\n"
            "• `/envs <app>` — zmienne środowiskowe\n"
            "• `/ask <app> <pytanie>` — guardian konkretnej appki\n"
            "• `/clear` — reset historii rozmowy\n\n"
            "*Język naturalny też działa:*\n"
            "_zrestartuj quiz_, _pokaż logi autoheal_,\n"
            "_dlaczego agentflow nie działa?_, _co mam naprawić dziś?_"
        ); return

    if tl in ["/status","status","stan","co słychać","co slychac"]:
        await do_status(chat_id); return
    if tl in ["/smoke","smoke","testy","wyniki testów"]:
        await do_smoke(chat_id); return
    if tl in ["/alerts","alerty","alarmy"]:
        await do_alerts(chat_id); return
    if tl in ["/apps","apps","aplikacje"]:
        await do_apps(chat_id); return
    if tl in ["/report","report","raport"]:
        await do_report(chat_id); return
    if tl in ["/clear","clear","wyczyść","zapomnij","reset"]:
        sessions.pop(chat_id, None)
        await send(chat_id, "🧹 Historia wyczyszczona!"); return

    # /logs <app>
    if tl.startswith("/logs") or re.search(r'\b(logi|logs)\s+\w', tl):
        app_ref = re.sub(r'^/(logs|logi)\s*', '', tl).strip() or \
                  re.search(r'\b(logi|logs)\s+(\w[\w-]*)', tl).group(2) if re.search(r'\b(logi|logs)\s+(\w[\w-]*)', tl) else ""
        await do_logs(chat_id, app_ref or t); return

    # /restart <app>
    if tl.startswith("/restart") or re.search(r'\b(zrestartuj|restart)\b', tl):
        await do_restart(chat_id, t); return

    # /deploy <app>
    if tl.startswith("/deploy") or re.search(r'\b(deploy|wdróż|wdroz)\b', tl):
        await do_deploy(chat_id, t); return

    # /envs <app>
    if tl.startswith("/envs"):
        app_ref = tl.replace("/envs","").strip()
        await do_envs(chat_id, app_ref or t); return

    # /ask <app> <question>
    if tl.startswith("/ask"):
        parts = t[4:].strip().split(None, 1)
        if len(parts) >= 2:
            await do_guardian_ask(chat_id, parts[0], parts[1])
        else:
            await send(chat_id, "Użycie: `/ask quiz jak działa fraud detection?`")
        return

    # ── Język naturalny ──
    await typing(chat_id)

    # Zbierz dodatkowy kontekst jeśli user pyta o problemy
    extra = ""
    if any(w in tl for w in ["nie działa","problem","błąd","awaria","crash","down"]):
        apps = await get_apps(force=True)
        broken = [a["name"] for a in apps if "exited" in a.get("status","") or "restarting" in a.get("status","")]
        if broken:
            extra = f"Aktualnie problematyczne aplikacje: {', '.join(broken)}"

    # Dobierz model i zapytaj
    model = pick_model(tl)
    reply = await ask_claude(chat_id, t, model=model, extra_ctx=extra)
    await send_chunks(chat_id, reply)

async def handle_cb(cb_id: str, chat_id: str, user_id: str, data: str):
    await answer_cb(cb_id)
    if data == "status" or data == "refresh": await do_status(chat_id)
    elif data == "smoke":  await do_smoke(chat_id)
    elif data == "alerts": await do_alerts(chat_id)
    elif data == "apps":   await do_apps(chat_id)
    elif data.startswith("logs_"):
        await do_logs(chat_id, data[5:])
    elif data.startswith("restart_"):
        parts = data.split("_", 2)
        await do_restart(chat_id, parts[2] if len(parts) > 2 else data[8:])
    elif data.startswith("deploy_"):
        parts = data.split("_", 2)
        await do_deploy(chat_id, parts[2] if len(parts) > 2 else data[7:])

# ── Background: proaktywne alerty ──────────────────────────────────────────
async def alert_watcher():
    """Co 3 minuty sprawdza nowe alerty i notyfikuje Macieja."""
    global alert_notified
    await asyncio.sleep(30)  # poczekaj na start
    while True:
        try:
            alerts = await sb_rpc("public_get_alerts") or []
            new_alerts = [a for a in alerts
                          if str(a.get("id")) not in alert_notified
                          and a.get("severity") in ("critical","warning")]
            
            if new_alerts and ADMIN_ID:
                for a in new_alerts[:3]:
                    alert_notified.add(str(a.get("id")))
                    sev = a.get("severity","?")
                    icon = "🔴" if sev == "critical" else "🟡"
                    msg = (f"{icon} *Alert {sev.upper()}*\n"
                           f"`{a.get('app_name','?')}` — {a.get('message','')[:100]}\n"
                           f"Źródło: {a.get('source','?')}")
                    await send(ADMIN_ID, msg)
                    await asyncio.sleep(1)
            
            # Ogranicz pamięć notified do 200 ostatnich
            if len(alert_notified) > 200:
                alert_notified = set(list(alert_notified)[-100:])
                
        except Exception as ex:
            log.debug(f"alert_watcher: {ex}")
        
        await asyncio.sleep(180)  # co 3 minuty

# ── Main ────────────────────────────────────────────────────────────────────
async def main():
    log.info("🤖 Guardian Bot v4 starting (100% Claude, zero OpenAI)")
    
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{TG}/getMe")
        me = r.json()
    
    if not me.get("ok"):
        log.error(f"Invalid token: {me}"); return
    
    bot_name = me["result"]["username"]
    log.info(f"✅ @{bot_name} ready | routing: haiku+sonnet | allowed: {ALLOWED or 'ALL'}")

    # Startup message do admina
    if ADMIN_ID:
        apps = await get_apps()
        broken = [a["name"] for a in apps if "exited" in a.get("status","") or "restarting" in a.get("status","")]
        healthy = sum(1 for a in apps if "running" in a.get("status",""))
        msg = (f"🤖 *Guardian v4 uruchomiony!*\n\n"
               f"📊 {healthy}/{len(apps)} apps OK"
               f"\n⚠️ Problemy: {', '.join(broken)}" if broken else "")
        try:
            await tg_post("sendMessage",
                {"chat_id":ADMIN_ID,"text":msg,"parse_mode":"Markdown",
                 "reply_markup": kbd_main()})
        except: pass

    # Uruchom background watchera
    asyncio.create_task(alert_watcher())
    
    offset = 0
    log.info("Polling...")
    
    conflict_backoff = 1
    while True:
        try:
            async with httpx.AsyncClient(timeout=35) as c:
                r = await c.get(f"{TG}/getUpdates",
                    params={"offset":offset,"timeout":30,"limit":10})
                data = r.json()
            
            if not data.get("ok"):
                desc = data.get("description","?")
                if "Conflict" in desc or "409" in str(r.status_code):
                    # Inna instancja bota jest aktywna — czekaj z backoff
                    log.warning(f"409 Conflict — czekam {conflict_backoff}s na wygaśnięcie starej instancji")
                    await asyncio.sleep(conflict_backoff)
                    conflict_backoff = min(conflict_backoff * 2, 30)
                    continue
                log.warning(f"getUpdates: {desc}")
                await asyncio.sleep(5)
                continue
            
            conflict_backoff = 1  # reset backoff po sukcesie
            
            for upd in data["result"]:
                offset = upd["update_id"] + 1
                
                if msg := upd.get("message") or upd.get("edited_message"):
                    chat_id = str(msg["chat"]["id"])
                    user_id = str(msg["from"]["id"])
                    text    = msg.get("text","").strip()
                    if text:
                        asyncio.create_task(handle_msg(chat_id, user_id, text))
                
                elif cb := upd.get("callback_query"):
                    asyncio.create_task(handle_cb(
                        cb["id"],
                        str(cb["message"]["chat"]["id"]),
                        str(cb["from"]["id"]),
                        cb.get("data","")
                    ))
        
        except asyncio.CancelledError: break
        except Exception as ex:
            log.error(f"Poll: {ex}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
