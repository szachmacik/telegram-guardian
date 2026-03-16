"""
Telegram Guardian Bot v5 — Manager ofshore.dev
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FILOZOFIA POMOCNICZOŚCI (jak w katolickiej nauce społecznej):
  Tier 1 — Haiku (tanie, szybkie)     -> status, komendy, proste pytania
  Tier 2 — GPT-4o-mini / Gemini Flash -> pytania wymagające więcej kontekstu  
  Tier 3 — Sonnet (droższe)           -> analiza, debugowanie, planowanie
  Tier 4 — Konsultacja z Claude.ai    -> gdy bot nie umie czegoś zrobić, prosi Ciebie
  Tier 5 — Zleca budowę narzędzia     -> gdy funkcja nie istnieje, tworzy ją

AI Router:
  - najpierw próbuje haiku (fast, cheap)
  - jeśli pytanie złożone -> sonnet
  - jeśli potrzeba sprawdzenia faktów na żywo -> guardian danej appki
  - guardiany appów: agentflow, quiz, inbox, english, manus, hub, ai-control
  - n8n: workflow automation (uruchamia workflow przez API)

Kanały komunikacji:
  - Telegram (ten bot) — główny
  - Guardian boty appów — specjalistyczna wiedza
  - Claude API (Anthropic) — głęboka analiza
  - n8n — automatyzacja, scheduled tasks
"""
import asyncio, json, os, logging, re, time
import httpx
from datetime import datetime

# ── Config ─────────────────────────────────────────────────────────────────
TG_TOKEN      = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
OPENAI_KEY    = os.environ.get("OPENAI_API_KEY","")    # opcjonalny
GOOGLE_KEY    = os.environ.get("GOOGLE_API_KEY","")    # opcjonalny
COOLIFY_URL   = os.environ.get("COOLIFY_URL","https://coolify.ofshore.dev")
COOLIFY_TOKEN = os.environ.get("COOLIFY_TOKEN","")
SB_URL        = os.environ.get("SUPABASE_URL","")
SB_KEY        = os.environ.get("SUPABASE_KEY","")
N8N_URL       = os.environ.get("N8N_URL","https://n8n.ofshore.dev")
N8N_KEY       = os.environ.get("N8N_API_KEY","")
ALLOWED       = set(x.strip() for x in os.environ.get("ALLOWED_TELEGRAM_IDS","").split(",") if x.strip())
ADMIN_ID      = os.environ.get("ADMIN_CHAT_ID","")
TG            = f"https://api.telegram.org/bot{TG_TOKEN}"

# ── AI Models — Tier routing ────────────────────────────────────────────────
# Tier 1: Haiku — szybkie, tanie (komendy, status, proste Q&A)
TIER1 = "claude-haiku-4-5-20251001"
# Tier 2: Sonnet — głęboka analiza, debugowanie, planowanie
TIER2 = "claude-sonnet-4-6"
# Tier 2B: GPT-4o-mini — fallback gdy chcemy drugi model (sprawdzenie krzyżowe)
GPT_MINI = "gpt-4o-mini"
# Tier 2C: Gemini Flash — szybki fallback
GEMINI = "gemini-1.5-flash"

def pick_tier(text: str) -> str:
    """Zasada pomocniczości: najpierw tanie, dopiero potem drogie."""
    t = text.lower()
    # Zawsze Sonnet dla złożonych zadań
    if any(w in t for w in [
        "dlaczego","analiz","debug","przyczyn","strategi","architektur",
        "optymali","wytłumacz szczeg","porównaj dokładn","zaplanuj","napisz kod",
        "zbuduj","stwórz aplikacj","napraw","why is","explain in detail",
        "analyze","root cause"
    ]):
        return TIER2
    # Haiku dla wszystkiego prostego
    return TIER1

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [BOT] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("bot")

sessions: dict[str, list] = {}
app_cache: list = []
cache_ts: float = 0
alert_notified: set = set()

# ── App registry ────────────────────────────────────────────────────────────
APPS = {
    "agentflow":      ("ts0c0wgco8wo8kgocok84cws", "agentflow.ofshore.dev"),
    "quiz":           ("yssco8cc800ow880w0wo48o0",  "quiz.ofshore.dev"),
    "quiz-manager":   ("yssco8cc800ow880w0wo48o0",  "quiz.ofshore.dev"),
    "inbox":          ("tcww08co80wsgwwg8swwgss8",  "inbox.ofshore.dev"),
    "omnichannel":    ("tcww08co80wsgwwg8swwgss8",  "inbox.ofshore.dev"),
    "english":        ("d0800oks0g4gws0kw04ck00s",  "english-teacher.ofshore.dev"),
    "english-teacher":("d0800oks0g4gws0kw04ck00s",  "english-teacher.ofshore.dev"),
    "manus":          ("kssk4o48sgosgwwck8s8ws80",  "brain.ofshore.dev"),
    "brain":          ("kssk4o48sgosgwwck8s8ws80",  "brain.ofshore.dev"),
    "integration":    ("s44sck0k0os0k4w0www00cg4",  "hub.ofshore.dev"),
    "hub":            ("s44sck0k0os0k4w0www00cg4",  "hub.ofshore.dev"),
    "sentinel":       ("rs488c4ccg48w48gocgog8sg",  "sentinel.ofshore.dev"),
    "ai-control":     ("hokscgg48sowg44wwc044gk8",  "ai-control-center.ofshore.dev"),
    "security":       ("wg0gkco8g0swgccc8www04gg",  "security.ofshore.dev"),
    "watchdog":       ("g8csck0kw8c0sc0cosg0cw84",  None),
    "autoheal":       ("vcgk0g4sc4sck0kkc8k080gk",  None),
    "smoketester":    ("qws0sk4gooo4ok8cswc0o0kw",  None),
    "telegram":       ("qook8w0sw4o404swcoookg00",   None),
    "n8n":            (None,                          "n8n.ofshore.dev"),
}

def find_app(text: str):
    t = text.lower()
    for name, (uuid, domain) in APPS.items():
        if name in t:
            return name, uuid, domain
    return None, None, None

# ── HTTP helpers ────────────────────────────────────────────────────────────
async def sb_rpc(fn: str, params: dict = {}) -> any:
    if not SB_URL: return None
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{SB_URL}/rest/v1/rpc/{fn}",
                headers={"apikey":SB_KEY,"Authorization":f"Bearer {SB_KEY}",
                         "Content-Type":"application/json"}, json=params)
            return r.json() if r.status_code == 200 else None
    except: return None

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
    return app_cache or []

# ── Infra snapshot ──────────────────────────────────────────────────────────
async def infra_snapshot() -> dict:
    apps   = await get_apps(force=True)
    smoke  = await sb_rpc("public_get_smoke_summary") or []
    alerts = await sb_rpc("public_get_alerts") or []
    healthy = [a for a in apps if "running" in a.get("status","")]
    broken  = [a for a in apps if "exited" in a.get("status","") or "restarting" in a.get("status","")]
    failed_smoke = [s for s in smoke if not s.get("passed")]
    return {
        "total":len(apps), "healthy":len(healthy), "broken":len(broken),
        "broken_names":[a["name"] for a in broken],
        "healthy_names":[a["name"] for a in healthy],
        "smoke_total":len(smoke), "smoke_failed":len(failed_smoke),
        "smoke_failures":[(s["app_name"],s["test_name"]) for s in failed_smoke[:6]],
        "alerts":len(alerts), "alert_list":alerts[:5],
        "ts":datetime.now().strftime("%H:%M %d.%m.%Y"),
    }

async def infra_ctx() -> str:
    s = await infra_snapshot()
    ctx = f"INFRA [{s['ts']}]: {s['healthy']}/{s['total']} OK"
    if s["broken_names"]: ctx += f" | DOWN: {', '.join(s['broken_names'])}"
    if s["smoke_failures"]: ctx += f" | SMOKE_FAIL: {', '.join(f'{a}/{t}' for a,t in s['smoke_failures'][:3])}"
    if s["alerts"]: ctx += f" | {s['alerts']} ALERTÓW"
    return ctx

# ── AI Router — serce systemu ───────────────────────────────────────────────
PERSONA = """Jesteś Guardian — manager infrastruktury ofshore.dev, prawa ręka Macieja.

ZASADY (pomocniczość — najpierw niższe koszty):
1. Odpowiadaj konkretnie, bez owijania w bawełnę
2. NIGDY nie mów "nie mogę", "nie mam dostępu", "to poza moimi możliwościami"
3. Jeśli czegoś nie umiesz ZROBIĆ — powiedz CO Maciej powinien zrobić lub zleć to dalej
4. Jeśli potrzebujesz głębszej analizy -> powiedz "Skonsultuję z Claude.ai" i użyj /claude
5. Jeśli brakuje funkcji — zaproponuj budowę przez /build <opis>
6. Masz dostęp do guardianów każdej appki przez /ask <app> <pytanie>

DOSTĘPNE AI w ekosystemie:
- Claude Haiku (szybkie odpowiedzi, monitoring)
- Claude Sonnet (analiza, debugowanie)
- OpenAI GPT-4o (jeśli dostępny)
- Guardiany appów (agentflow, quiz, manus, english, inbox, hub, ai-control)
- n8n (workflow automation)

INFRASTRUKTURA:
24+ aplikacji na ofshore.dev, DigitalOcean + Coolify + Supabase + GitHub
Stack: React+tRPC+Drizzle+MySQL/Postgres, Python boty

Odpowiadaj po polsku gdy Maciej pisze po polsku. Emoji z umiarem."""

async def ask_ai(chat_id: str, user_msg: str, model: str = None,
                 extra_ctx: str = "", force_sonnet: bool = False) -> str:
    """
    AI Router z zasadą pomocniczości.
    Haiku first, Sonnet tylko gdy potrzeba, fallback do drugiego modelu.
    """
    chosen = model or (TIER2 if force_sonnet else pick_tier(user_msg))
    ctx    = await infra_ctx()
    hist   = sessions.get(chat_id, [])
    msgs   = hist[-14:] + [{"role":"user","content":user_msg}]
    system = PERSONA + f"\n\n{ctx}"
    if extra_ctx: system += f"\n\nDODATKOWY KONTEKST:\n{extra_ctx}"

    # Próba 1: Anthropic (Claude)
    try:
        async with httpx.AsyncClient(timeout=50) as c:
            r = await c.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01",
                         "content-type":"application/json"},
                json={"model":chosen,"max_tokens":2000,"system":system,"messages":msgs})
            data = r.json()
            if data.get("content"):
                reply = data["content"][0]["text"]
                hist.append({"role":"user","content":user_msg})
                hist.append({"role":"assistant","content":reply})
                sessions[chat_id] = hist[-20:]
                log.info(f"[AI] {chosen} -> {len(reply)} chars")
                return reply
            # Haiku fail -> try Sonnet
            if chosen == TIER1:
                log.warning(f"Haiku failed, trying Sonnet: {data.get('error','?')}")
                return await ask_ai(chat_id, user_msg, TIER2, extra_ctx)
    except Exception as ex:
        log.error(f"Anthropic error: {ex}")

    # Fallback: GPT-4o-mini (jeśli klucz dostępny)
    if OPENAI_KEY and OPENAI_KEY.startswith("sk-"):
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post("https://api.openai.com/v1/chat/completions",
                    headers={"Authorization":f"Bearer {OPENAI_KEY}",
                             "Content-Type":"application/json"},
                    json={"model":GPT_MINI,"max_tokens":1500,
                          "messages":[{"role":"system","content":system}]+msgs})
                data = r.json()
                if data.get("choices"):
                    reply = data["choices"][0]["message"]["content"]
                    log.info(f"[AI] GPT-4o-mini fallback -> {len(reply)} chars")
                    hist.append({"role":"user","content":user_msg})
                    hist.append({"role":"assistant","content":reply})
                    sessions[chat_id] = hist[-20:]
                    return reply
        except Exception as ex:
            log.error(f"OpenAI fallback error: {ex}")

    return "⚠️ Wszystkie modele AI niedostępne. Sprawdź klucze API w Coolify."

# ── Specjalne akcje: konsultacja z Claude.ai ───────────────────────────────
async def escalate_to_claude(chat_id: str, question: str) -> str:
    """
    Gdy bot nie potrafi rozwiązać problemu sam, eskaluje do Claude Sonnet
    z pełnym kontekstem i prosi o plan działania.
    """
    snap   = await infra_snapshot()
    smoke  = await sb_rpc("public_get_smoke_summary") or []
    alerts = await sb_rpc("public_get_alerts") or []
    
    context = f"""
Pytanie od Macieja (właściciela ofshore.dev): {question}

Stan infrastruktury:
- Apps: {snap['healthy']}/{snap['total']} zdrowych
- Problemy: {', '.join(snap['broken_names']) or 'brak'}
- Smoke fails: {', '.join(f"{a}/{t}" for a,t in snap['smoke_failures']) or 'brak'}
- Alerty: {snap['alerts']}

Maciej potrzebuje konkretnej porady technicznej lub planu działania.
Odpowiedz szczegółowo — to jest eskalacja do głównego doradcy.
"""
    
    prompt = f"ESKALACJA: {question}\n\n{context}"
    return await ask_ai(chat_id, prompt, model=TIER2, force_sonnet=True)

# ── Telegram helpers ────────────────────────────────────────────────────────
async def tg(endpoint: str, payload: dict):
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            return await c.post(f"{TG}/{endpoint}", json=payload)
    except Exception as ex:
        log.warning(f"TG {endpoint}: {ex}")

async def send(chat_id, text: str, kbd=None, parse_mode="Markdown"):
    p = {"chat_id":chat_id,"text":text[:4096],"parse_mode":parse_mode}
    if kbd: p["reply_markup"] = kbd
    r = await tg("sendMessage", p)
    # Fallback: jeśli Markdown powoduje błąd, wyślij bez formatowania
    if r and r.status_code == 400 and parse_mode:
        p2 = {"chat_id":chat_id,"text":text[:4096]}
        if kbd: p2["reply_markup"] = kbd
        await tg("sendMessage", p2)

async def send_chunks(chat_id, text: str):
    for i in range(0, len(text), 3800):
        await send(chat_id, text[i:i+3800])
        if len(text) > 3800: await asyncio.sleep(0.3)

async def typing(chat_id):
    await tg("sendChatAction", {"chat_id":chat_id,"action":"typing"})

async def answer_cb(cb_id: str, text="✅"):
    await tg("answerCallbackQuery", {"callback_query_id":cb_id,"text":text})

# ── Keyboards ───────────────────────────────────────────────────────────────
def kbd_main():
    return {"inline_keyboard": [
        [{"text":"📊 Status","callback_data":"status"},
         {"text":"🧪 Testy","callback_data":"smoke"},
         {"text":"🚨 Alerty","callback_data":"alerts"}],
        [{"text":"📋 Watchdog","callback_data":"logs_watchdog"},
         {"text":"📋 AutoHeal","callback_data":"logs_autoheal"},
         {"text":"📋 Smoketester","callback_data":"logs_smoketester"}],
        [{"text":"📱 Aplikacje","callback_data":"apps"},
         {"text":"📋 Raport","callback_data":"report"},
         {"text":"🔄 Odśwież","callback_data":"status"}],
    ]}

def kbd_ai():
    return {"inline_keyboard": [
        [{"text":"🤖 Konsultuj Claude","callback_data":"escalate"},
         {"text":"🔧 Zbuduj funkcję","callback_data":"build"}],
        [{"text":"◀️ Powrót","callback_data":"status"}],
    ]}

# ── Action handlers ─────────────────────────────────────────────────────────
async def do_status(chat_id):
    await typing(chat_id)
    s = await infra_snapshot()
    lines = [f"*📊 Infrastruktura* `{s['ts']}`\n",
             f"✅ {s['healthy']} OK  |  ❌ {s['broken']} problemy  |  📱 {s['total']} łącznie"]
    if s["broken_names"]:
        lines.append("\n*🔴 Problemy:*")
        for n in s["broken_names"]: lines.append(f"  • `{n}`")
    if s["smoke_failures"]:
        lines.append(f"\n*🧪 Smoke fails ({s['smoke_failed']}/{s['smoke_total']}):*")
        for app, test in s["smoke_failures"][:5]: lines.append(f"  • `{app}/{test}`")
    if s["alerts"]: lines.append(f"\n*🚨 Aktywne alerty: {s['alerts']}*")
    healthy_str = ", ".join(f"`{n}`" for n in s["healthy_names"][:10])
    if s["healthy_names"]:
        lines.append(f"\n*🟢 Zdrowe:* {healthy_str}")
        if len(s["healthy_names"]) > 10:
            lines.append(f"_...i {len(s['healthy_names'])-10} więcej_")
    await send(chat_id, "\n".join(lines), kbd=kbd_main())

async def do_smoke(chat_id):
    await typing(chat_id)
    summary = await sb_rpc("public_get_smoke_summary") or []
    if not summary:
        await send(chat_id, "⚠️ Brak wyników. SmokeTester działa co ~10min."); return
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
        lines.append("\n🎉 Wszystkie OK!")
    await send(chat_id, "\n".join(lines))

async def do_alerts(chat_id):
    await typing(chat_id)
    alerts = await sb_rpc("public_get_alerts") or []
    if not alerts:
        await send(chat_id, "✅ Brak aktywnych alertów!"); return
    lines = [f"*🚨 Alerty ({len(alerts)})*\n"]
    for a in alerts[:8]:
        icon = "🔴" if a.get("severity") == "critical" else "🟡"
        lines.append(f"{icon} `{a.get('app_name')}` [{a.get('severity','?')}]")
        lines.append(f"   _{a.get('message','')[:80]}_")
    await send(chat_id, "\n".join(lines))

async def do_logs(chat_id, app_ref: str):
    name, uuid, _ = find_app(app_ref)
    if not uuid:
        await send(chat_id, f"❓ `{app_ref}` — nie znam. Spróbuj: watchdog, autoheal, quiz..."); return
    await typing(chat_id)
    r = await cf(f"/applications/{uuid}/logs?lines=40")
    logs = r.get("logs","") if isinstance(r,dict) else ""
    if not logs:
        await send(chat_id, f"❌ Brak logów dla `{name}`."); return
    lines = [l for l in logs.split("\n") if l.strip()][-25:]
    await send_chunks(chat_id, f"*📋 Logi `{name}` (25 linii)*\n```\n" + "\n".join(lines) + "\n```")

async def do_restart(chat_id, app_ref: str):
    name, uuid, _ = find_app(app_ref)
    if not uuid:
        await send(chat_id, f"❓ Nie znam `{app_ref}`."); return
    await typing(chat_id)
    r = await cf(f"/applications/{uuid}/restart","POST")
    if r.get("message") or r.get("deployment_uuid"):
        await send(chat_id, f"🔄 Restart `{name}` zlecony! Za ~1min sprawdź `/status`.")
    else:
        await send(chat_id, f"❌ Błąd restartu `{name}`: {str(r)[:80]}")

async def do_deploy(chat_id, app_ref: str):
    name, uuid, _ = find_app(app_ref)
    if not uuid:
        await send(chat_id, f"❓ Nie znam `{app_ref}`."); return
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
        domain = f" `{fqdn[:32]}`" if fqdn and "sslip" not in fqdn else ""
        lines.append(f"{icon} `{a['name'][:28]}`{domain}")
    await send_chunks(chat_id, "\n".join(lines))

async def do_report(chat_id):
    await typing(chat_id)
    s = await infra_snapshot()
    ctx = (f"Stan: {s['healthy']}/{s['total']} OK, {s['broken']} problemów: "
           f"{', '.join(s['broken_names']) or 'brak'}\n"
           f"Smoke: {s['smoke_total']-s['smoke_failed']}/{s['smoke_total']} OK, "
           f"fails: {', '.join(f'{a}/{t}' for a,t in s['smoke_failures']) or 'brak'}\n"
           f"Alerty: {s['alerts']}")
    reply = await ask_ai(chat_id,
        "Przygotuj krótki raport dzienny dla właściciela. Co działa, co wymaga uwagi, 3 priorytety. Max 250 słów.",
        model=TIER2, extra_ctx=ctx)
    await send_chunks(chat_id, f"*📋 Raport dzienny {s['ts']}*\n\n{reply}")

async def do_guardian_ask(chat_id, app_ref: str, question: str):
    """Pyta guardian bota konkretnej appki — specjalistyczna wiedza lokalna."""
    name, _, domain = find_app(app_ref)
    if not domain:
        await send(chat_id, f"❓ `{app_ref}` nie ma guardian bota lub nie znam domeny."); return
    await typing(chat_id)
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"https://{domain}/api/guardian",
                json={"message":question,"userId":f"manager_{chat_id}"},
                headers={"Content-Type":"application/json"})
            if r.status_code == 200:
                reply = r.json().get("reply","brak odpowiedzi")
                await send(chat_id, f"*🤖 Guardian `{name}`:*\n\n{reply[:1500]}")
            else:
                await send(chat_id, f"❌ Guardian `{name}` HTTP {r.status_code}. Może nie działa?")
    except Exception as ex:
        await send(chat_id, f"❌ Błąd połączenia z guardian `{name}`: {ex}")

async def do_n8n_workflows(chat_id):
    """Lista workflow w n8n."""
    if not N8N_KEY:
        await send(chat_id, "⚠️ Brak N8N_API_KEY. Dodaj w Coolify envs bota.")
        return
    await typing(chat_id)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{N8N_URL}/api/v1/workflows",
                headers={"X-N8N-API-KEY":N8N_KEY})
            if r.status_code == 200:
                wf = r.json().get("data",[])
                if not wf:
                    await send(chat_id, "n8n działa ale brak workflow. Dodaj je na n8n.ofshore.dev")
                    return
                lines = [f"*⚙️ n8n Workflows ({len(wf)})*\n"]
                for w in wf:
                    icon = "✅" if w.get("active") else "⏸"
                    lines.append(f"{icon} `{w.get('name','?')}` (ID: {w.get('id','?')})")
                await send(chat_id, "\n".join(lines))
            else:
                await send(chat_id, f"❌ n8n API błąd: {r.status_code}")
    except Exception as ex:
        await send(chat_id, f"❌ n8n niedostępne: {ex}")

async def do_n8n_trigger(chat_id, workflow_name: str):
    """Uruchamia workflow n8n po nazwie."""
    if not N8N_KEY:
        await send(chat_id, "⚠️ Brak N8N_API_KEY."); return
    await typing(chat_id)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            # Znajdź workflow
            r = await c.get(f"{N8N_URL}/api/v1/workflows",
                headers={"X-N8N-API-KEY":N8N_KEY})
            wf_list = r.json().get("data",[])
            wf = next((w for w in wf_list if workflow_name.lower() in w.get("name","").lower()), None)
            if not wf:
                names = ", ".join(f"`{w['name']}`" for w in wf_list[:5])
                await send(chat_id, f"❓ Nie znalazłem workflow `{workflow_name}`.\nDostępne: {names}")
                return
            # Uruchom przez webhook lub manual trigger
            wf_id = wf["id"]
            r2 = await c.post(f"{N8N_URL}/api/v1/workflows/{wf_id}/activate",
                headers={"X-N8N-API-KEY":N8N_KEY})
            await send(chat_id, f"⚙️ Workflow `{wf['name']}` aktywowany!\n"
                               f"Sprawdź wykonania na https://n8n.ofshore.dev")
    except Exception as ex:
        await send(chat_id, f"❌ n8n błąd: {ex}")

async def do_envs(chat_id, app_ref: str):
    name, uuid, _ = find_app(app_ref)
    if not uuid:
        await send(chat_id, f"❓ Nie znam `{app_ref}`."); return
    await typing(chat_id)
    r = await cf(f"/applications/{uuid}/envs")
    envs = r if isinstance(r,list) else []
    if not envs:
        await send(chat_id, f"❌ Brak env vars dla `{name}`."); return
    lines = [f"*🔧 Env vars `{name}` ({len(envs)})*\n"]
    for e in envs:
        k = e.get("key","?")
        v = e.get("value","")
        safe = v[:6]+"..." if any(x in k.upper() for x in ["TOKEN","KEY","SECRET","PASS"]) else v[:40]
        lines.append(f"  `{k}` = `{safe}`")
    await send_chunks(chat_id, "\n".join(lines))

async def do_build_request(chat_id, description: str):
    """
    Gdy bot nie potrafi czegoś zrobić — generuje specyfikację do budowy
    i informuje Macieja że potrzeba nowej funkcji.
    """
    await typing(chat_id)
    spec = await ask_ai(chat_id,
        f"Maciej potrzebuje nowej funkcji w bocie Guardian: '{description}'\n"
        "Przygotuj krótką specyfikację techniczną (max 150 słów): co zbudować, "
        "jak to wdrożyć, jakie API/dane są potrzebne. Bądź konkretny.",
        model=TIER2)
    
    msg = (f"*🔧 Specyfikacja nowej funkcji*\n\n"
           f"Żądanie: _{description}_\n\n{spec}\n\n"
           f"_Aby zbudować: wklej tę specyfikację do Claude.ai lub powiedz Manusowi._")
    await send(chat_id, msg)

# ── Message router ──────────────────────────────────────────────────────────
async def handle_msg(chat_id: str, user_id: str, text: str):
    if ALLOWED and user_id not in ALLOWED and chat_id not in ALLOWED:
        await send(chat_id, "🔒 Brak dostępu."); return

    log.info(f"[{chat_id}] {text[:80]}")
    t  = text.strip()
    tl = t.lower()

    # ── /start i /help ──
    if tl in ["/start","start"]:
        await send(chat_id,
            "👋 *Guardian v5 — Manager ofshore.dev*\n\n"
            "*Szybkie komendy:*\n"
            "`/status` — stan infrastruktury\n"
            "`/smoke` — wyniki smoke testów\n"
            "`/alerts` — aktywne alerty\n"
            "`/report` — raport AI dzienny\n"
            "`/apps` — lista aplikacji\n"
            "`/logs watchdog` — logi aplikacji\n"
            "`/restart quiz` — restart\n"
            "`/deploy manus` — wdróż\n"
            "`/envs quiz` — env vars\n"
            "`/ask manus jakie modele masz?` — pytaj guardian\n"
            "`/n8n` — lista workflow n8n\n"
            "`/claude <pytanie>` — konsultacja z Claude Sonnet\n"
            "`/build <opis>` — wygeneruj specyfikację nowej funkcji\n"
            "`/clear` — wyczyść historię\n\n"
            "💬 Pisz też normalnie po polsku — rozumiem wszystko!",
            kbd=kbd_main()); return

    if tl in ["/help","help","pomoc"]:
        help_text = (
            "*Pelna pomoc*\n\n"
            "Pisz naturalnie po polsku:\n"
            "- status / co slychac -> stan infra\n"
            "- zrestartuj quiz -> restart\n"
            "- logi watchdog -> logi\n"
            "- raport -> raport dzienny\n\n"
            "*AI routing (zasada pomocniczosci):*\n"
            "Proste pytania -> Haiku (szybkie/tanie)\n"
            "Analiza/debug -> Sonnet (dokladne)\n"
            "/claude <pytanie> -> wymuszony Sonnet\n"
            "/ask <app> <pytanie> -> guardian lokalny\n"
            "/build <opis> -> spec nowej funkcji"
        )
        await send(chat_id, help_text); return

    # ── Komendy slash ──
    if tl in ["/status","status","stan","co słychać","co slychac","jak idzie","health"]:
        await do_status(chat_id); return
    if tl in ["/smoke","smoke","testy","wyniki testów","wyniki testow"]:
        await do_smoke(chat_id); return
    if tl in ["/alerts","alerty","alarmy"]:
        await do_alerts(chat_id); return
    if tl in ["/apps","apps","aplikacje"]:
        await do_apps(chat_id); return
    if tl in ["/report","report","raport"]:
        await do_report(chat_id); return
    if tl in ["/n8n","n8n","workflow","workflows"]:
        await do_n8n_workflows(chat_id); return
    if tl in ["/clear","clear","wyczyść","wyczysc","zapomnij","reset"]:
        sessions.pop(chat_id, None)
        await send(chat_id, "🧹 Historia wyczyszczona!"); return

    # /logs <app>
    m = re.match(r'^/logs\s+(.+)$', tl) or re.search(r'\b(logi|logs)\s+([\w-]+)', tl)
    if m:
        app_ref = m.group(2) if len(m.groups()) > 1 else m.group(1)
        await do_logs(chat_id, app_ref); return
    if tl.startswith("/logs"):
        await send(chat_id, "Użycie: `/logs watchdog` lub `/logs quiz`"); return

    # /restart <app>
    if tl.startswith("/restart") or re.search(r'\b(zrestartuj|restart)\b', tl):
        await do_restart(chat_id, t); return

    # /deploy <app>
    if tl.startswith("/deploy") or re.search(r'\b(deploy|wdróż|wdroz)\b', tl):
        await do_deploy(chat_id, t); return

    # /envs <app>
    if tl.startswith("/envs"):
        await do_envs(chat_id, tl.replace("/envs","").strip() or t); return

    # /ask <app> <question>
    if tl.startswith("/ask"):
        parts = t[4:].strip().split(None, 1)
        if len(parts) >= 2:
            await do_guardian_ask(chat_id, parts[0], parts[1])
        else:
            await send(chat_id, "Użycie: `/ask quiz jak działa fraud detection?`")
        return

    # /claude <pytanie> — wymuszony Sonnet (konsultacja z głównym doradcą)
    if tl.startswith("/claude") or tl.startswith("/consult") or tl.startswith("/expert"):
        question = re.sub(r'^/(claude|consult|expert)\s*', '', t, flags=re.IGNORECASE).strip()
        if not question: question = "Pomóż z analizą infrastruktury"
        await typing(chat_id)
        await send(chat_id, "🤔 _Konsultuję z Claude Sonnet (Tier 2)..._")
        reply = await escalate_to_claude(chat_id, question)
        await send_chunks(chat_id, f"*🧠 Claude Sonnet odpowiada:*\n\n{reply}")
        return

    # /build <opis> — generuj spec nowej funkcji
    if tl.startswith("/build"):
        desc = tl.replace("/build","").strip()
        if not desc:
            await send(chat_id, "Użycie: `/build chcę żeby bot wysyłał mi raport codziennie o 9:00`")
            return
        await do_build_request(chat_id, desc)
        return

    # /n8n trigger <workflow>
    if tl.startswith("/n8n trigger") or tl.startswith("/trigger"):
        wf_name = re.sub(r'^/(n8n trigger|trigger)\s*', '', tl).strip()
        await do_n8n_trigger(chat_id, wf_name); return

    # ── Język naturalny ──
    await typing(chat_id)

    # Detekcja intencji: jeśli wyraźnie potrzeba konsultacji
    needs_escalation = any(w in tl for w in [
        "nie rozumiem dlaczego","zupełnie nie wiem","to skomplikowane",
        "potrzebuję planu","jak to architektować","zaprojektuj mi",
        "co powinienem","poradź mi","doradzaj"
    ])
    
    # Dodatkowy kontekst przy problemach
    extra = ""
    if any(w in tl for w in ["nie działa","błąd","awaria","crash","down","problem"]):
        apps = await get_apps(force=True)
        broken = [a["name"] for a in apps if "exited" in a.get("status","") or "restarting" in a.get("status","")]
        if broken:
            extra = f"Aktualnie problematyczne: {', '.join(broken)}"
    
    if needs_escalation:
        await send(chat_id, "🤔 _Przekazuję do Claude Sonnet (zaawansowana analiza)..._")
        reply = await escalate_to_claude(chat_id, t)
    else:
        reply = await ask_ai(chat_id, t, extra_ctx=extra)
    
    # Wykryj czy bot mówi że czegoś nie może i zaproponuj eskalację
    cant_phrases = ["nie mam dostępu","nie mogę","poza moimi możliwościami",
                    "nie jestem w stanie","nie potrafię","nie mam narzędzi"]
    if any(phrase in reply.lower() for phrase in cant_phrases):
        reply += ("\n\n_Jeśli chcesz żebym to zbudował/wdrożył, napisz:_\n"
                  "`/build <opis tego co potrzebujesz>`\n"
                  "_Albo skonsultuj bezpośrednio:_ `/claude <pytanie>`")
    
    await send_chunks(chat_id, reply)

async def handle_cb(cb_id: str, chat_id: str, user_id: str, data: str):
    await answer_cb(cb_id)
    if data in ("status","refresh"):  await do_status(chat_id)
    elif data == "smoke":             await do_smoke(chat_id)
    elif data == "alerts":            await do_alerts(chat_id)
    elif data == "apps":              await do_apps(chat_id)
    elif data == "report":            await do_report(chat_id)
    elif data == "escalate":
        await send(chat_id, "Napisz: `/claude <twoje pytanie>` aby skonsultować z Claude Sonnet.")
    elif data == "build":
        await send(chat_id, "Napisz: `/build <opis funkcji>` a wygeneruję specyfikację.")
    elif data.startswith("logs_"):    await do_logs(chat_id, data[5:])
    elif data.startswith("restart_"): await do_restart(chat_id, data[8:])
    elif data.startswith("deploy_"):  await do_deploy(chat_id, data[7:])

# ── Background: proaktywne alerty ──────────────────────────────────────────
async def alert_watcher():
    await asyncio.sleep(45)
    while True:
        try:
            alerts = await sb_rpc("public_get_alerts") or []
            new_alerts = [a for a in alerts
                          if str(a.get("id")) not in alert_notified
                          and a.get("severity") in ("critical","warning")]
            if new_alerts and ADMIN_ID:
                for a in new_alerts[:3]:
                    alert_notified.add(str(a.get("id")))
                    icon = "🔴" if a.get("severity") == "critical" else "🟡"
                    await send(ADMIN_ID,
                        f"{icon} *Alert {a.get('severity','?').upper()}*\n"
                        f"`{a.get('app_name','?')}` — {a.get('message','')[:100]}\n"
                        f"_{a.get('source','?')}_")
                    await asyncio.sleep(1)
            if len(alert_notified) > 200:
                alert_notified = set(list(alert_notified)[-100:])
        except Exception as ex:
            log.debug(f"alert_watcher: {ex}")
        await asyncio.sleep(180)

# ── Main ────────────────────────────────────────────────────────────────────
async def main():
    log.info("🤖 Guardian Bot v5 starting")
    log.info(f"  AI: Anthropic (haiku+sonnet) | OpenAI: {'✅' if OPENAI_KEY else '❌'} | n8n: {'✅' if N8N_KEY else '❌'}")

    async with httpx.AsyncClient(timeout=10) as c:
        me = (await c.get(f"{TG}/getMe")).json()
    if not me.get("ok"):
        log.error(f"Bad token: {me}"); return

    bot_name = me["result"]["username"]
    log.info(f"✅ @{bot_name} ready | allowed: {ALLOWED or 'ALL'}")

    if ADMIN_ID:
        s = await infra_snapshot()
        msg = (f"🤖 *Guardian v5 uruchomiony!*\n\n"
               f"📊 {s['healthy']}/{s['total']} apps OK"
               + (f"\n⚠️ Problemy: {', '.join(s['broken_names'])}" if s["broken_names"] else ""))
        await tg("sendMessage", {"chat_id":ADMIN_ID,"text":msg,
                                  "parse_mode":"Markdown","reply_markup":kbd_main()})

    asyncio.create_task(alert_watcher())

    offset = 0
    conflict_backoff = 1
    log.info("Polling...")
    while True:
        try:
            async with httpx.AsyncClient(timeout=35) as c:
                r = await c.get(f"{TG}/getUpdates",
                    params={"offset":offset,"timeout":30,"limit":10})
                data = r.json()

            if not data.get("ok"):
                desc = data.get("description","?")
                if "Conflict" in desc:
                    log.warning(f"409 — backoff {conflict_backoff}s")
                    await asyncio.sleep(conflict_backoff)
                    conflict_backoff = min(conflict_backoff * 2, 30)
                    continue
                await asyncio.sleep(5); continue

            conflict_backoff = 1
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
