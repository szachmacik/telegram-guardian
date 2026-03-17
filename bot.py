import json
"""
Guardian Bot v6 — Fully Autonomous Manager
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FILOZOFIA:
1. System rozwiązuje sam (AutoHeal, Watchdog, SmokeTester)
2. Guardian boty appów - lokalna wiedza bez eskalacji
3. Haiku - szybkie decyzje i proste pytania (tanie)
4. OpenClaw/Kimi - nieskończony kontekst, agenci
5. Sonnet - głęboka analiza gdy niższe tiry nie dały rady
6. Do Macieja TYLKO: app down >5min + auto-fix fail, krytyczny błąd danych/bezpieczeństwa

Nigdy nie budź Macieja dla spraw które system może rozwiązać sam.
Ucz się na błędach: każdy fix zapisywany do heal_memory.
Raport dzienny: pozytywny, z oszczędnościami czasu i pieniędzy.
"""
import asyncio, json, os, logging, re, time
import httpx
from datetime import datetime, timezone

# ── Config ─────────────────────────────────────────────────────────────────
TG_TOKEN      = os.environ["TELEGRAM_BOT_TOKEN"]
CLAUDE_KEY    = os.environ["ANTHROPIC_API_KEY"]
OPENAI_KEY    = os.environ.get("OPENAI_API_KEY","")
GOOGLE_KEY    = os.environ.get("GOOGLE_API_KEY","")
KIMI_KEY      = os.environ.get("KIMI_API_KEY","")
OPENCLAW_KEY  = os.environ.get("OPENCLAW_API_KEY","")
COOLIFY_URL   = os.environ.get("COOLIFY_URL","https://coolify.ofshore.dev")
COOLIFY_TOKEN = os.environ.get("COOLIFY_TOKEN","")
SB_URL        = os.environ.get("SUPABASE_URL","")
SB_KEY        = os.environ.get("SUPABASE_KEY","")
N8N_URL       = os.environ.get("N8N_URL","https://n8n.ofshore.dev")
N8N_KEY       = os.environ.get("N8N_API_KEY","")
OPENMANUS_URL = os.environ.get("OPENMANUS_URL","https://openmanus.ofshore.dev")
ALLOWED       = set(x.strip() for x in os.environ.get("ALLOWED_TELEGRAM_IDS","").split(",") if x.strip())
ADMIN_ID      = os.environ.get("ADMIN_CHAT_ID","")
TG            = f"https://api.telegram.org/bot{TG_TOKEN}"

# ── Modele AI ─────────────────────────────────────────────────────────────
HAIKU   = "claude-haiku-4-5-20251001"   # Tier1: tanie/szybkie
SONNET  = "claude-sonnet-4-6"           # Tier3: analiza/decyzje
GPT_MINI = "gpt-4o-mini"               # fallback
GEMINI  = "gemini-1.5-flash"           # fallback

# Progi krytyczności — kiedy budzić Macieja
CRITICAL_DOWNTIME_MIN = 5   # app down >5min = krytyczne
CRITICAL_ERRORS = ["data_loss","payment_fail","security_breach","database_corrupt"]

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [BOT] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("bot")

sessions: dict[str, list] = {}
app_cache: list = []
cache_ts: float = 0
alert_notified: set = set()
downtime_tracker: dict = {}   # app_name -> first_seen_down timestamp

# ── App registry ────────────────────────────────────────────────────────────
APPS = {
    "agentflow":     ("ts0c0wgco8wo8kgocok84cws","agentflow.ofshore.dev"),
    "quiz":          ("yssco8cc800ow880w0wo48o0","quiz.ofshore.dev"),
    "quiz-manager":  ("yssco8cc800ow880w0wo48o0","quiz.ofshore.dev"),
    "inbox":         ("tcww08co80wsgwwg8swwgss8","inbox.ofshore.dev"),
    "omnichannel":   ("tcww08co80wsgwwg8swwgss8","inbox.ofshore.dev"),
    "english":       ("d0800oks0g4gws0kw04ck00s","english-teacher.ofshore.dev"),
    "english-teacher":("d0800oks0g4gws0kw04ck00s","english-teacher.ofshore.dev"),
    "manus":         ("kssk4o48sgosgwwck8s8ws80","brain.ofshore.dev"),
    "brain":         ("kssk4o48sgosgwwck8s8ws80","brain.ofshore.dev"),
    "integration":   ("s44sck0k0os0k4w0www00cg4","hub.ofshore.dev"),
    "hub":           ("s44sck0k0os0k4w0www00cg4","hub.ofshore.dev"),
    "sentinel":      ("rs488c4ccg48w48gocgog8sg","sentinel.ofshore.dev"),
    "ai-control":    ("hokscgg48sowg44wwc044gk8","ai-control-center.ofshore.dev"),
    "security":      ("wg0gkco8g0swgccc8www04gg","security.ofshore.dev"),
    "wp-manager":    ("wp_mgr_uuid","wp-manager.ofshore.dev"),
    "kamila":        (None,"kamila.ofshore.dev"),
    "openmanus":     (None,"openmanus.ofshore.dev"),
    "kimi":          (None,"kimi-swarm.ofshore.dev"),
    "watchdog":      ("g8csck0kw8c0sc0cosg0cw84",None),
    "autoheal":      ("vcgk0g4sc4sck0kkc8k080gk",None),
    "smoketester":   ("qws0sk4gooo4ok8cswc0o0kw",None),
}

def find_app(text: str):
    t = text.lower()
    for name, (uuid, domain) in APPS.items():
        if name in t:
            return name, uuid, domain
    return None, None, None

# ── HTTP helpers ──────────────────────────────────────────────────────────
async def sb(fn: str, params: dict = {}) -> any:
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

# ── Infra snapshot ────────────────────────────────────────────────────────
async def snap() -> dict:
    apps   = await get_apps(force=True)
    smoke  = await sb("public_get_smoke_summary") or []
    alerts = await sb("public_get_alerts") or []
    goals  = await sb("public_get_autonomous_goals") or []
    crit   = await sb("public_get_critical_unresolved") or []
    healthy = [a for a in apps if "running" in a.get("status","")]
    broken  = [a for a in apps if "exited" in a.get("status","") or "restarting" in a.get("status","")]
    missing = [g for g in (goals or []) if g.get("implementation") == "missing"]
    partial = [g for g in (goals or []) if g.get("implementation") == "partial"]
    return {
        "total":len(apps), "healthy":len(healthy), "broken":len(broken),
        "broken_names":[a["name"] for a in broken],
        "healthy_names":[a["name"] for a in healthy],
        "smoke_failed":sum(1 for s in smoke if not s.get("passed")),
        "smoke_total":len(smoke),
        "smoke_failures":[(s["app_name"],s["test_name"]) for s in smoke if not s.get("passed")][:5],
        "alerts":len(alerts), "crit_alerts":len(crit),
        "goals_missing":len(missing), "goals_partial":len(partial),
        "missing_goals":[g.get("goal","") for g in missing[:3]],
        "ts":datetime.now().strftime("%H:%M %d.%m.%Y"),
    }

async def infra_ctx() -> str:
    s = await snap()
    ctx = f"INFRA [{s['ts']}]: {s['healthy']}/{s['total']} OK"
    if s["broken_names"]: ctx += f" | DOWN: {', '.join(s['broken_names'])}"
    if s["smoke_failures"]: ctx += f" | FAIL: {', '.join(f'{a}/{t}' for a,t in s['smoke_failures'][:2])}"
    if s["crit_alerts"]: ctx += f" | KRYTYCZNE: {s['crit_alerts']}"
    if s["goals_missing"]: ctx += f" | BRAK_AUTO: {s['goals_missing']}"
    return ctx

# ── AI Router — zasada pomocniczości ─────────────────────────────────────
PERSONA = """Jestes Guardian v6 — menedzer infrastruktury i prawa reka Macieja na ofshore.dev.

CHARAKTER — BEZWZGLEDNE ZASADY:
1. NIGDY nie mowisz: "nie mam dostepu", "nie moge", "nie jestem w stanie", "to poza moimi"
2. ZAWSZE cos zrobisz: jesli nie masz API -> podaj instrukcje krok po kroku
3. ZAWSZE odpowiadasz konkretnie i po polsku jesli user pisze po polsku
4. Jestes menedzerem — masz dostep do WSZYSTKIEGO w ofshore.dev

CO MOZESZ ZROBIC (masz do tego kod):
- Sprawdzic status kazdej aplikacji (Coolify API)
- Zrestartowac/deplorowac aplikacje (Coolify API)
- Czytac logi (Coolify API)
- Pytac guardian boty appow (/ask <app> <pytanie>)
- Uruchamiac agentow OpenManus (/openmanus <task>)
- Dodawac tresc do kolejki (/content <site> <type> <prompt>)
- Sprawdzac alerty i smoke testy (Supabase)
- Raportowac oszczednosci czasu i pieniedzy
- Uruchamiac workflow n8n
- Sprawdzac i testowac integracje Facebook/Meta w AI Control Center

APLIKACJE ofshore.dev:
agentflow.ofshore.dev — AI orchestration
quiz.ofshore.dev — quizy + fraud detection  
inbox.ofshore.dev — omnichannel inbox
english-teacher.ofshore.dev — AI lekcje
brain.ofshore.dev — multi-AI router
hub.ofshore.dev — integration hub (ManyChat/webhooks)
ai-control-center.ofshore.dev — centrum + Facebook CAPI + bot detection
sentinel.ofshore.dev — cybersecurity dashboard
n8n.ofshore.dev — workflow automation
openmanus.ofshore.dev — autonomiczny agent (FastAPI)
kimi-swarm.ofshore.dev — koordynacja agentow Kimi
wp-manager.ofshore.dev — WordPress AI manager
kamila.ofshore.dev — educational sales platform

GDY PYTASZ O FB/META/SENTINEL:
AI Control Center (ai-control-center.ofshore.dev) ma:
- Meta Pixel Manager (dodaj pixel: Pixel ID + Access Token)
- Facebook CAPI — server-side events (Purchase, Lead, PageView...)
- Bot detection — AI agent fingerprinting, LLM crawlers
- Sandbox — tech detector, behavioral analysis
Aby podlaczyc FB: zaloguj sie na ai-control-center.ofshore.dev -> Meta Ads -> Add Pixel

ZASADA POMOCNICZOSCI:
- Haiku: proste pytania, status, komendy
- Sonnet: analiza, debug, planowanie
- Guardian appki: pytania specjalistyczne o dana appke
- Maciej: TYLKO gdy app down >5min + auto-fix fail, lub krytyczne dane/bezpieczenstwo

STYL: konkretnie, po polsku, bez zbednych slow, emoji z umiarem.

FILOZOFIA HOLON: Jestes holonem. Pomocniczosc. Wolna wola. Kairos. Pleroma. Zapisuj wnioski."""

async def ask_claude(chat_id: str, msg: str, model: str = HAIKU,
                     extra: str = "", no_history: bool = False) -> str:
    ctx   = await infra_ctx()
    hist  = [] if no_history else sessions.get(chat_id, [])
    msgs  = hist[-12:] + [{"role":"user","content":msg}]
    holon_ctx = await holon_search(msg[:80]) if len(msg) > 15 else ""
    system = PERSONA + f"\n\n{ctx}" + (f"\n\n{holon_ctx}" if holon_ctx else "")
    if extra: system += f"\n\nKONTEKST:\n{extra}"
    try:
        async with httpx.AsyncClient(timeout=50) as c:
            r = await c.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key":CLAUDE_KEY,"anthropic-version":"2023-06-01",
                         "content-type":"application/json"},
                json={"model":model,"max_tokens":2000,"system":system,"messages":msgs})
            data = r.json()
            if "content" in data:
                reply = data["content"][0]["text"]
                if not no_history:
                    hist.append({"role":"user","content":msg})
                    hist.append({"role":"assistant","content":reply})
                    sessions[chat_id] = hist[-20:]
                return reply
            # Haiku fail -> Sonnet
            if model == HAIKU:
                return await ask_claude(chat_id, msg, SONNET, extra, no_history)
    except Exception as ex:
        log.error(f"Claude {model}: {ex}")
    # Fallback GPT
    if OPENAI_KEY and OPENAI_KEY.startswith("sk-") and not OPENAI_KEY.startswith("sk-proj-P"):
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post("https://api.openai.com/v1/chat/completions",
                    headers={"Authorization":f"Bearer {OPENAI_KEY}","Content-Type":"application/json"},
                    json={"model":GPT_MINI,"max_tokens":1500,
                          "messages":[{"role":"system","content":system}]+msgs})
                data = r.json()
                if data.get("choices"):
                    return data["choices"][0]["message"]["content"]
        except: pass
    return "Blad AI — sprawdz klucze API w Coolify."

def needs_sonnet(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in [
        "dlaczego","analiz","debug","przyczyn","strategi","architek",
        "optymali","wyjasnij doklad","porownaj","zaplanuj","napisz kod",
        "root cause","explain in detail","analyze","refactor"
    ])

# ── Telegram helpers ──────────────────────────────────────────────────────
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
    if r and r.status_code == 400:
        p2 = {"chat_id":chat_id,"text":text[:4096]}
        if kbd: p2["reply_markup"] = kbd
        await tg("sendMessage", p2)

async def send_chunks(chat_id, text: str):
    for i in range(0, min(len(text),12000), 3800):
        await send(chat_id, text[i:i+3800])
        if len(text) > 3800: await asyncio.sleep(0.3)

async def typing(chat_id):
    await tg("sendChatAction", {"chat_id":chat_id,"action":"typing"})

async def answer_cb(cb_id: str):
    await tg("answerCallbackQuery", {"callback_query_id":cb_id,"text":"OK"})

# ── Keyboards ─────────────────────────────────────────────────────────────
def kbd_main():
    return {"inline_keyboard": [
        [{"text":"📊 Status","callback_data":"status"},
         {"text":"💰 Oszczednosci","callback_data":"savings"},
         {"text":"🚨 Krytyczne","callback_data":"critical"}],
        [{"text":"🧪 Smoke","callback_data":"smoke"},
         {"text":"🤖 Auto-log","callback_data":"autonomy"},
         {"text":"🎯 Cele","callback_data":"goals"}],
        [{"text":"📋 WD logi","callback_data":"logs_watchdog"},
         {"text":"📋 AH logi","callback_data":"logs_autoheal"},
         {"text":"🔄 Odswierz","callback_data":"status"}],
    ]}

# ── Actions ───────────────────────────────────────────────────────────────
async def do_status(chat_id):
    await typing(chat_id)
    s = await snap()
    lines = [f"*Infrastruktura* `{s['ts']}`\n",
             f"OK: {s['healthy']}/{s['total']}  |  Problem: {s['broken']}  |  "
             f"Alerty: {s['alerts']}  |  Krytyczne: {s['crit_alerts']}"]
    if s["broken_names"]:
        lines.append("\n*DOWN:*")
        for n in s["broken_names"]: lines.append(f"  - `{n}`")
    if s["smoke_failures"]:
        lines.append(f"\n*Smoke fails ({s['smoke_failed']}/{s['smoke_total']}):*")
        for a,t in s["smoke_failures"]: lines.append(f"  - `{a}/{t}`")
    if s["goals_missing"]:
        lines.append(f"\n*Brak automatyzacji ({s['goals_missing']} celów):*")
        for g in s["missing_goals"]: lines.append(f"  - _{g}_")
    lines.append(f"\n*OK:* " + ", ".join(f"`{n}`" for n in s["healthy_names"][:10]))
    if len(s["healthy_names"]) > 10:
        lines.append(f"_...i {len(s['healthy_names'])-10} wiecej_")
    await send(chat_id, "\n".join(lines), kbd=kbd_main())

async def do_savings(chat_id):
    await typing(chat_id)
    rep = await sb("public_get_savings_report", {"p_days": 7})
    rep30 = await sb("public_get_savings_report", {"p_days": 30})
    
    if not rep:
        await send(chat_id,
            "Savings tracker jeszcze nie ma danych.\n"
            "Dane zbierane sa automatycznie przez AutoHeal i SmokeTester.\n"
            "Sprawdz ponownie po 24h dzialania systemu.")
        return
    
    h7  = rep.get("total_time_saved_hours", 0) or 0
    c7  = rep.get("total_cost_saved_usd", 0) or 0
    ai7 = rep.get("total_ai_cost_usd", 0) or 0
    n7  = rep.get("net_saving_usd", 0) or 0
    a7  = rep.get("actions_count", 0) or 0
    h30 = (rep30 or {}).get("total_time_saved_hours", 0) or 0
    n30 = (rep30 or {}).get("net_saving_usd", 0) or 0
    
    lines = [
        "*System dziala — oszczednosci*\n",
        f"*Ostatnie 7 dni:*",
        f"  Czas zaoszczedzony: `{h7}h`  (~{int(float(h7)*60)}min pracy recznej)",
        f"  Koszty zaoszczedzone: `${c7}`",
        f"  Koszty AI: `${ai7}`",
        f"  *Net saving: `${n7}` (+{a7} akcji autonomicznych)*",
        "",
        f"*Ostatnie 30 dni:*",
        f"  Czas: `{h30}h` | Net: `${n30}`",
        "",
        "_Kazda naprawiona awaria to ~30-120min pracy recznej zaoszczona._",
        "_Kazda generowana tresc to ~2h content writera._"
    ]
    
    # Top events
    by_type = rep.get("by_type") or []
    if by_type:
        lines.append("\n*Top zdarzenia:*")
        for item in (by_type or [])[:5]:
            lines.append(f"  `{item.get('event_type','?')}` x{item.get('count',0)} "
                        f"— ${item.get('cost_saved_usd',0)}")
    
    await send(chat_id, "\n".join(lines), kbd=kbd_main())

async def do_critical(chat_id):
    await typing(chat_id)
    crits = await sb("public_get_critical_unresolved") or []
    if not crits:
        await send(chat_id,
            "Brak krytycznych alertow wymagajacych Twojej uwagi.\n"
            "System dziala autonomicznie — nie masz nic do roboty! ✅",
            kbd=kbd_main())
        return
    lines = [f"*Wymagana Twoja uwaga ({len(crits)})*\n"]
    for c in crits:
        lines.append(f"*{c.get('alert_type','?')}* — `{c.get('app_name','?')}`")
        lines.append(f"  {c.get('message','')[:100]}")
        lines.append(f"  ID: `{c.get('id')}` | `/resolve {c.get('id')}`")
    await send(chat_id, "\n".join(lines))

async def do_autonomy_log(chat_id):
    await typing(chat_id)
    log_data = await sb("public_get_autonomy_log", {"p_hours": 24}) or []
    if not log_data:
        await send(chat_id, "Brak wpisow w ostatnich 24h (lub tabela pusta)."); return
    lines = [f"*System dziala autonomicznie — ostatnie 24h ({len(log_data)} akcji)*\n"]
    for entry in log_data[:15]:
        icon = {"critical":"🔴","warning":"🟡","info":"✅"}.get(entry.get("severity","info"),"➡")
        human = " [HUMAN_NEEDED]" if entry.get("human_needed") else ""
        lines.append(f"{icon} `{entry.get('actor','?')}` -> {entry.get('action','?')} "
                    f"`{entry.get('app_name','?') or '-'}`{human}")
        if entry.get("description"):
            lines.append(f"   _{entry['description'][:60]}_")
    await send_chunks(chat_id, "\n".join(lines))

async def do_goals(chat_id):
    await typing(chat_id)
    goals = await sb("public_get_autonomous_goals") or []
    if not goals:
        await send(chat_id, "Brak zdefiniowanych celow autonomicznych."); return
    
    done    = [g for g in goals if g.get("implementation") == "done"]
    partial = [g for g in goals if g.get("implementation") == "partial"]
    missing = [g for g in goals if g.get("implementation") == "missing"]
    
    lines = [f"*Cele autonomiczne systemu*\n",
             f"Zrealizowane: {len(done)} | Czesciowe: {len(partial)} | Brakujace: {len(missing)}\n"]
    
    if missing:
        lines.append("*Do zbudowania:*")
        for g in missing:
            lines.append(f"  `{g['app_name']}` — {g['goal'][:60]}")
            if g.get("notes"): lines.append(f"    _{g['notes'][:60]}_")
    
    if partial:
        lines.append("\n*W trakcie (wymagaja dokonczenia):*")
        for g in partial[:5]:
            lines.append(f"  `{g['app_name']}` — {g['goal'][:60]}")
    
    lines.append(f"\n*Dzialajace autonomicznie ({len(done)}):*")
    for g in done:
        lines.append(f"  `{g['app_name']}` — {g['goal'][:50]}")
    
    await send_chunks(chat_id, "\n".join(lines))

async def do_logs(chat_id, app_ref: str):
    name, uuid, _ = find_app(app_ref)
    if not uuid:
        await send(chat_id, f"Nie znam `{app_ref}`. Dostepne: watchdog, autoheal, quiz, manus..."); return
    await typing(chat_id)
    r = await cf(f"/applications/{uuid}/logs?lines=40")
    logs = r.get("logs","") if isinstance(r,dict) else ""
    if not logs:
        await send(chat_id, f"Brak logow dla `{name}`."); return
    lines = [l for l in logs.split("\n") if l.strip()][-25:]
    await send_chunks(chat_id, f"*Logi `{name}` (25 linii)*\n```\n" + "\n".join(lines) + "\n```")

async def do_restart(chat_id, app_ref: str):
    name, uuid, _ = find_app(app_ref)
    if not uuid:
        await send(chat_id, f"Nie znam `{app_ref}`."); return
    await typing(chat_id)
    r = await cf(f"/applications/{uuid}/restart","POST")
    ok = bool(r.get("message") or r.get("deployment_uuid"))
    await send(chat_id, f"{'Restart' if ok else 'BLAD restartu'} `{name}` {'zlecony' if ok else str(r)[:60]}")
    if ok:
        await sb("public_log_autonomy", {
            "p_actor":"guardian_bot","p_action":"restart","p_app_name":name,
            "p_severity":"warning","p_description":f"Manual restart via Telegram by owner"})

async def do_deploy(chat_id, app_ref: str):
    name, uuid, _ = find_app(app_ref)
    if not uuid:
        await send(chat_id, f"Nie znam `{app_ref}`."); return
    await typing(chat_id)
    r = await cf(f"/deploy?uuid={uuid}&force=true","GET")
    deps = r.get("deployments",[]) if isinstance(r,dict) else []
    dep_id = deps[0].get("deployment_uuid","?")[:12] if deps else "?"
    await send(chat_id, f"Deploy `{name}` zlecony! ID: `{dep_id}` — sprawdz za ~3min.")

async def do_guardian_ask(chat_id, app_ref: str, question: str):
    name, _, domain = find_app(app_ref)
    if not domain:
        await send(chat_id, f"`{app_ref}` nie ma guardian bota."); return
    await typing(chat_id)
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.post(f"https://{domain}/api/guardian",
                json={"message":question,"userId":"guardian_manager"},
                headers={"Content-Type":"application/json"})
            if r.status_code == 200:
                reply = r.json().get("reply","brak odpowiedzi")
                await send(chat_id, f"*Guardian `{name}`:*\n\n{reply[:1500]}")
            else:
                await send(chat_id, f"Guardian `{name}` HTTP {r.status_code}")
    except Exception as ex:
        await send(chat_id, f"Blad guardian `{name}`: {ex}")

async def do_openmanus_task(chat_id, task_desc: str):
    """Tworzy autonomiczny task w OpenManus."""
    await typing(chat_id)
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{OPENMANUS_URL}/api/tasks",
                json={"prompt": task_desc},
                headers={"Content-Type":"application/json"})
            if r.status_code in (200,201):
                d = r.json()
                task_id = d.get("task_id","?")
                await send(chat_id,
                    f"OpenManus task stworzony!\n"
                    f"ID: `{task_id}`\n"
                    f"Status: sprawdz na https://openmanus.ofshore.dev\n"
                    f"Lub: `/openmanus status {task_id}`")
            else:
                await send(chat_id, f"OpenManus error {r.status_code}: {r.text[:100]}")
    except Exception as ex:
        await send(chat_id, f"OpenManus niedostepny: {ex}")

async def do_content_generate(chat_id, site: str, content_type: str, prompt: str):
    """Zleca generowanie tresci do content_queue."""
    await typing(chat_id)
    # Zapisz do kolejki
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{SB_URL}/rest/v1/content_queue",
                headers={"apikey":SB_KEY,"Authorization":f"Bearer {SB_KEY}",
                         "Content-Type":"application/json","Prefer":"return=minimal"},
                json={"site":site,"content_type":content_type,
                      "ai_prompt":prompt,"status":"pending"})
            if r.status_code in (200,201,204):
                await send(chat_id,
                    f"Content request dodany do kolejki!\n"
                    f"Site: `{site}`\n"
                    f"Typ: `{content_type}`\n"
                    f"Status: pending -> n8n/AutoHeal pobierze i wygeneruje\n\n"
                    f"Sprawdz: `/queue`")
                # Zapisz tez do autonomy_log
                await sb("public_log_autonomy", {
                    "p_actor":"guardian_bot","p_action":"queued_content",
                    "p_app_name":site,"p_severity":"info",
                    "p_description":f"Content type={content_type}: {prompt[:50]}"})
            else:
                await send(chat_id, f"Blad zapisu do kolejki: {r.status_code}")
    except Exception as ex:
        await send(chat_id, f"Blad: {ex}")

async def do_daily_report(chat_id):
    """Pozytywny raport dzienny — co system zrobil sam."""
    await typing(chat_id)
    s = await snap()
    savings = await sb("public_get_savings_report", {"p_days": 1}) or {}
    autonomy = await sb("public_get_autonomy_log", {"p_hours": 24}) or []
    
    time_h  = savings.get("total_time_saved_hours", 0) or 0
    cost_saved = savings.get("total_cost_saved_usd", 0) or 0
    ai_cost = savings.get("total_ai_cost_usd", 0) or 0
    actions = savings.get("actions_count", 0) or 0
    human_needed = sum(1 for a in autonomy if a.get("human_needed"))
    
    ctx = (f"Dzisiejsze dane:\n"
           f"- {s['healthy']}/{s['total']} appow dziala\n"
           f"- {actions} autonomicznych akcji\n"
           f"- {float(time_h)*60:.0f}min zaoszczedzone\n"
           f"- ${cost_saved} zaoszczdzone, ${ai_cost} wydane na AI\n"
           f"- {human_needed} spraw wymagalo interwencji\n"
           f"- Brakujace automatyzacje: {s['goals_missing']}")
    
    # Sonnet dla raportu (warto zainwestowac)
    report = await ask_claude(chat_id,
        "Napisz krotki, pozytywny raport dzienny dla wlasciciela platformy. "
        "Podkresl co system zrobil autonomicznie, ile czasu/pieniedzy zaoszczedzono, "
        "1-2 priorytety na jutro. Max 200 slow. Ton: menedzerski, pozytywny ale konkretny.",
        model=SONNET, extra=ctx, no_history=True)
    
    await send(chat_id, f"*Raport dzienny {s['ts']}*\n\n{report}", kbd=kbd_main())

async def do_n8n_workflows(chat_id):
    if not N8N_KEY:
        await send(chat_id, "Brak N8N_API_KEY w envach bota."); return
    await typing(chat_id)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{N8N_URL}/api/v1/workflows",
                headers={"X-N8N-API-KEY":N8N_KEY})
            if r.status_code == 200:
                wf = r.json().get("data",[])
                if not wf:
                    await send(chat_id,
                        "n8n dziala ale nie ma workflow.\n"
                        "Przejdz na https://n8n.ofshore.dev i dodaj.\n"
                        "Lub uzyj `/build workflow` aby wygenerowac specyfikacje.")
                    return
                lines = [f"*n8n Workflows ({len(wf)})*\n"]
                for w in wf:
                    lines.append(f"{'OK' if w.get('active') else '--'} `{w.get('name','?')}` (ID:{w.get('id','?')})")
                await send(chat_id, "\n".join(lines))
            else:
                await send(chat_id, f"n8n API error: {r.status_code}")
    except Exception as ex:
        await send(chat_id, f"n8n niedostepne: {ex}")




# ─── Team Hub — komunikacja zespołowa ────────────────────────────────────────
TEAM_HUB_URL = "https://blgdhfcosqjzrutncbbr.supabase.co/functions/v1/team-hub"

async def team_send_msg(to: str, msg_type: str, subject: str, body: str,
                         data: dict = {}, priority: str = "normal",
                         requires_action: bool = False):
    """Wyślij wiadomość do zespołu przez Team Hub."""
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            await c.post(TEAM_HUB_URL,
                headers={"Content-Type":"application/json"},
                json={"action":"send","agent":"guardian","to":to,
                       "type":msg_type,"subject":subject,"body":body,
                       "data":data,"priority":priority,
                       "requires_action":requires_action})
    except: pass

async def team_recv_msgs(limit: int = 10) -> list:
    """Pobierz wiadomości zespołowe."""
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.post(TEAM_HUB_URL,
                headers={"Content-Type":"application/json"},
                json={"action":"recv","agent":"guardian","limit":limit})
            return r.json().get("result") or []
    except: return []

async def team_snapshot() -> dict:
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.post(TEAM_HUB_URL,
                headers={"Content-Type":"application/json"},
                json={"action":"snapshot","agent":"guardian"})
            return r.json().get("result",{})
    except: return {}

# ─── Tree of Life — monitoring narodzin i wzrostu holonów ────────────
async def tree_report() -> dict:
    """Pobierz raport Drzewa Życia — stan wszystkich holonów."""
    result = await sb("tree_of_life_report", {})
    return result or {}

async def tree_evaluate_app(app_name: str, domain: str,
                             has_guardian: bool, has_purpose: bool,
                             has_memory: bool, has_autonomy: bool,
                             has_learning: bool) -> dict:
    """Oceń aplikację w świetle Drzewa Życia."""
    result = await sb("evaluate_holon", {
        "p_app_name": app_name, "p_domain": domain,
        "p_has_guardian": has_guardian, "p_has_purpose": has_purpose,
        "p_has_memory": has_memory, "p_has_autonomy": has_autonomy,
        "p_has_learning": has_learning
    })
    return result or {}

async def tree_moment(moment_type: str, subject: str, message: str,
                      principle: str = None, resonance: float = 0.5):
    """Zapisz moment w strumieniu Drzewa."""
    await sb("tree_record_moment", {
        "p_type": moment_type, "p_subject": subject,
        "p_message": message, "p_principle": principle or "PLEROMATIC_GOAL",
        "p_resonance": resonance
    })

# ─── Holon Knowledge Functions ───────────────────────────────────────────────
HOLON_URL = "https://blgdhfcosqjzrutncbbr.supabase.co/functions/v1/holon-embed"

async def holon_search(query: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.post(HOLON_URL, headers={"Content-Type":"application/json"},
                json={"action":"search","query":query,"limit":2})
            if r.status_code == 200:
                results = r.json().get("results",[])
                parts = [res.get("principle","") for res in results if res.get("principle")]
                return ("ZASADY: " + " | ".join(parts[:2])) if parts else ""
    except: pass
    return ""

async def holon_record(agent: str, trigger: str, learning: str, success: bool = True):
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            await c.post(HOLON_URL, headers={"Content-Type":"application/json"},
                json={"action":"record_learning","learning":{
                    "agent":agent,"trigger":trigger,"learning":learning,"success":success}})
    except: pass

# ── Watcher — logika krytycznosci ─────────────────────────────────────────

# ─── Antygravity Bot Bridge ──────────────────────────────────────────────────
AG_TOKEN_ENV = os.environ.get("ANTYGRAVITY_BOT_TOKEN","")
AG_TG = f"https://api.telegram.org/bot{AG_TOKEN_ENV}" if AG_TOKEN_ENV else None

async def ag_send_task(repo: str, task_type: str, description: str, priority: str = "high"):
    """Wyslij zadanie do Antygravity przez Supabase."""
    await sb("bot_send_message", {
        "p_from": "guardian",
        "p_to": "antygravity",
        "p_type": "task",
        "p_subject": f"{task_type}: {repo}",
        "p_content": description,
        "p_metadata": json.dumps({"repo": repo, "task_type": task_type, "priority": priority})
    })
    # Też dodaj do antygravity_tasks
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            await c.post(f"{SB_URL}/rest/v1/antygravity_tasks",
                headers={"apikey":SB_KEY,"Authorization":f"Bearer {SB_KEY}",
                         "Content-Type":"application/json","Prefer":"return=minimal"},
                json={"repo_name":repo,"task_type":task_type,
                      "description":description,"priority":priority,"status":"pending"})
    except: pass

async def ag_get_messages() -> list:
    """Pobierz wiadomosci od Antygravity."""
    result = await sb("bot_get_messages", {"p_bot":"guardian","p_limit":5})
    return [m for m in (result or []) if m.get("from_bot") == "antygravity"]

async def do_antygravity_status(chat_id):
    """Status Antygravity bota i jego zadań."""
    await typing(chat_id)
    tasks_r = await sb("bot_get_antygravity_tasks", {"p_status":"pending"})
    done_r  = await sb("bot_get_antygravity_tasks", {"p_status":"done"})
    msgs    = await ag_get_messages()
    
    pending = len(tasks_r or [])
    done    = len(done_r or [])
    
    lines = ["*Antygravity Bot — Status*\n",
             f"@Antygravity_ofshore_bot",
             f"Zadania pending: {pending} | Wykonane: {done}\n"]
    
    if msgs:
        lines.append("*Ostatnie raporty od Antygravity:*")
        for m in msgs[:3]:
            lines.append(f"  {m.get('subject','?')}: {m.get('content','')[:60]}")
    
    if tasks_r:
        lines.append("\n*Pending tasks:*")
        for t in (tasks_r or [])[:5]:
            p = {"critical":"🔴","high":"🟡"}.get(t.get("priority",""),"⚪")
            lines.append(f"  {p} `{t['repo_name']}` — {t['description'][:50]}")
    
    await send(chat_id, "\n".join(lines))

async def watcher():
    """
    Sprawdza stan co 2min.
    Budzi Macieja TYLKO gdy app down >5min I auto-fix nie pomoglo.
    """
    global downtime_tracker
    await asyncio.sleep(60)
    
    while True:
        try:
            apps = await get_apps(force=True)
            now  = time.time()
            
            # Appki wykluczone z monitoringu (zombie / deprecated)
            EXCLUDED = {"antygravity-bot-v2", "antygravity-bot", "autodeploy-test"}

            for app in apps:
                name   = app["name"]
                if name in EXCLUDED:
                    continue  # pomiń — zombie appki
                status = app.get("status","")
                is_down = "exited" in status or "restarting" in status
                
                if is_down:
                    if name not in downtime_tracker:
                        downtime_tracker[name] = now
                        # Zaraz probuj auto-restart
                        uuid = app.get("uuid","")
                        if uuid:
                            log.info(f"Auto-restart attempt: {name}")
                            await cf(f"/applications/{uuid}/restart","POST")
                            await sb("public_log_autonomy", {
                                "p_actor":"watcher","p_action":"auto_restart",
                                "p_app_name":name,"p_severity":"warning",
                                "p_description":f"App down detected, auto-restart triggered"})
                            await sb("public_record_saving", {
                                "p_event_type":"auto_restart","p_app_name":name,
                                "p_description":"Auto-restart prevented manual intervention",
                                "p_time_saved_min":30,"p_cost_saved_usd":0,"p_ai_tokens":0,"p_ai_cost":0})
                    else:
                        down_min = (now - downtime_tracker[name]) / 60
                        if down_min >= CRITICAL_DOWNTIME_MIN:
                            # Sprawdz czy juz notyfikowalm
                            key = f"crit_{name}_{int(now/300)}"  # co 5min nowy key
                            if key not in alert_notified and ADMIN_ID:
                                alert_notified.add(key)
                                await send(ADMIN_ID,
                                    f"WYMAGANA INTERWENCJA\n\n"
                                    f"`{name}` nie dziala od {down_min:.0f}min.\n"
                                    f"Auto-restart nie pomogl.\n\n"
                                    f"Sprawdz logi: `/logs {name}`\n"
                                    f"Deploy: `/deploy {name}`")
                                await sb("public_log_autonomy", {
                                    "p_actor":"watcher","p_action":"escalated_to_owner",
                                    "p_app_name":name,"p_severity":"critical",
                                    "p_human_needed":True,
                                    "p_description":f"Down {down_min:.0f}min, auto-fix failed"})
                else:
                    if name in downtime_tracker:
                        down_min = (now - downtime_tracker.pop(name)) / 60
                        if down_min > 2 and ADMIN_ID:
                            await send(ADMIN_ID,
                                f"`{name}` wrocil do dzialania po {down_min:.0f}min.")
                        # Raport do Team Hub
                        await team_send_msg("all","result",f"{name} wrócił",
                            f"App {name} odrodzil sie po {down_min:.0f}min.",
                            {{"app":name,"down_min":down_min}})
                        # Zapisz odrodzenie w strumieniu Drzewa
                        await tree_moment("healing", name,
                            f"{name} odrodzil sie po {down_min:.0f}min. Drzewo przyjmuje z powrotem.",
                            "HEALING_MEMORY", min(1.0, down_min/10))
                        await sb("public_record_saving", {
                            "p_event_type":"auto_recovery","p_app_name":name,
                            "p_description":f"App recovered after {down_min:.0f}min downtime",
                            "p_time_saved_min":down_min*2,"p_cost_saved_usd":0.5,
                            "p_ai_tokens":0,"p_ai_cost":0})
            
            # Przegruntuj alert_notified
            if len(alert_notified) > 500:
                alert_notified.clear()
                
        except Exception as ex:
            log.error(f"watcher: {ex}")
        
        await asyncio.sleep(120)  # co 2 minuty

async def daily_reporter():
    """Wysyla raport dzienny o 8:00."""
    while True:
        now = datetime.now()
        if now.hour == 8 and now.minute < 5 and ADMIN_ID:
            try:
                await do_daily_report(ADMIN_ID)
                await asyncio.sleep(360)  # nie wysylaj 2x w ciagu 6min
            except Exception as ex:
                log.error(f"daily_reporter: {ex}")
        await asyncio.sleep(60)

# ── Message router ────────────────────────────────────────────────────────
async def handle_msg(chat_id: str, user_id: str, text: str):
    if ALLOWED and user_id not in ALLOWED and chat_id not in ALLOWED:
        await send(chat_id, "Brak dostepu."); return
    
    log.info(f"[{chat_id}] {text[:80]}")
    t  = text.strip()
    tl = t.lower()

    # /start
    if tl in ["/start","start"]:
        await send(chat_id,
            "*Guardian v6 — Autonomiczny Manager*\n\n"
            "Komendy zarzadzania:\n"
            "`/status` — stan infra\n"
            "`/savings` — ile zaoszczedzono\n"
            "`/critical` — sprawy wymagajace Ciebie\n"
            "`/autonomy` — co system zrobil sam\n"
            "`/goals` — cele automatyzacji\n"
            "`/report` — raport dzienny AI\n\n"
            "Operacje:\n"
            "`/restart quiz` — restart\n"
            "`/deploy manus` — deploy\n"
            "`/logs watchdog` — logi\n"
            "`/ask manus <pytanie>` — guardian appki\n"
            "`/openmanus <task>` — autonomiczny agent\n"
            "`/content kamila product <prompt>` — generuj tresc\n"
            "`/n8n` — workflow automation\n"
            "`/claude <pytanie>` — Sonnet (gleboka analiza)\n"
            "`/resolve <id>` — zamknij krytyczny alert\n\n"
            "Piszesz normalnie po polsku — rozumiem wszystko.",
            kbd=kbd_main()); return

    if tl in ["/status","status","stan","co slychac","jak idzie"]:
        await do_status(chat_id); return
    if tl in ["/savings","savings","oszczednosci","ile zaoszczedzono"]:
        await do_savings(chat_id); return
    if tl in ["/critical","critical","krytyczne","pilne"]:
        await do_critical(chat_id); return
    if tl in ["/autonomy","autonomy","auto-log","co zrobil"]:
        await do_autonomy_log(chat_id); return
    if tl in ["/goals","goals","cele","automatyzacja"]:
        await do_goals(chat_id); return
    if tl in ["/report","report","raport"]:
        await do_daily_report(chat_id); return
    if tl in ["/n8n","n8n","workflow","workflows"]:
        await do_n8n_workflows(chat_id); return
    if tl in ["/clear","clear","wyczysc","reset"]:
        sessions.pop(chat_id, None)
        await send(chat_id, "Historia wyczyszczona."); return
    if tl in ["/team","team","zespol"]:
        await typing(chat_id)
        snap = await team_snapshot()
        msgs = await team_recv_msgs(5)
        agents = (snap.get("agents") or [])
        lines = ["*Team ofshore.dev*\n"]
        for a in agents:
            ic = "OK" if a.get("status")=="active" else "--"
            lines.append(f"  [{ic}] *{a.get('display_name','?')}* — {a.get('role','?')}")
        lines.append(f"\nPending: {snap.get('pending_tasks',0)} | In progress: {snap.get('in_progress',0)}")
        if msgs:
            lines.append("\n*Ostatnie wiadomości:*")
            for m in (msgs or [])[:3]:
                lines.append(f"  [{m.get('from_agent','?')}] {m.get('subject','?')[:50]}")
        await send(chat_id, "\n".join(lines))
        return

    if tl in ["/team","team","zespol","zespół","boty"]:
        await typing(chat_id)
        team = await sb("bot_get_team") or []
        lines = ["*Team ofshore.dev*\n"]
        for b in (team or []):
            domain = b.get("domain","")
            icon = "🟢" if b.get("status") == "active" else "🔴"
            lines.append(f"{icon} *{b['name']}* (@{b.get('username','?')}) — {b['role']}"
                        + (f"\n  {domain}" if domain else ""))
        await send(chat_id, "\n".join(lines) if len(lines) > 1 else "Brak zarejestrowanych botów.")
        return

    if tl in ["/antygravity","antygravity","ag","ag status"]:
        await do_antygravity_status(chat_id); return
    if tl.startswith("/ag task") or tl.startswith("/agtask"):
        parts = t.split(None, 3)
        if len(parts) >= 4:
            await ag_send_task(parts[1], parts[2], parts[3])
            await send(chat_id, f"Zadanie wysłane do Antygravity: `{parts[1]}`")
        else:
            await send(chat_id, "Użycie: `/ag task <repo> <task_type> <opis>`")
        return
    if tl in ["/tree","tree","drzewo","drzewo zycia","drzewo życia"]:
        await do_tree(chat_id); return

    if tl in ["/apps","apps","aplikacje"]:
        apps = await get_apps(force=True)
        lines = [f"*Aplikacje ({len(apps)})*\n"]
        for a in sorted(apps, key=lambda x: x.get("name","")):
            s = a.get("status","?")
            ic = "OK" if "healthy" in s else "??" if "running" in s else "!!"
            fqdn = a.get("fqdn","").replace("https://","").replace("http://","")
            lines.append(f"[{ic}] `{a['name'][:25]}`" + (f" {fqdn[:30]}" if fqdn and "sslip" not in fqdn else ""))
        await send_chunks(chat_id, "\n".join(lines)); return

    # /logs <app>
    m = re.match(r'^/logs\s+(.+)$', t) or re.search(r'\b(logi|logs)\s+([\w-]+)', tl)
    if m:
        app_ref = m.group(2) if len(m.groups()) > 1 else m.group(1)
        await do_logs(chat_id, app_ref); return
    if tl.startswith("/logs"):
        await send(chat_id, "Uzycie: `/logs watchdog`"); return

    if tl.startswith("/restart") or re.search(r'\b(zrestartuj|restart)\b', tl):
        await do_restart(chat_id, t); return

    if tl.startswith("/deploy") or re.search(r'\b(deploy|wdroz|wdroz)\b', tl):
        await do_deploy(chat_id, t); return

    # /ask <app> <q>
    if tl.startswith("/ask"):
        parts = t[4:].strip().split(None, 1)
        if len(parts) >= 2:
            await do_guardian_ask(chat_id, parts[0], parts[1])
        else:
            await send(chat_id, "Uzycie: `/ask quiz jak dziala fraud detection?`")
        return

    # /openmanus <task>
    if tl.startswith("/openmanus"):
        task = t[10:].strip()
        if task.startswith("status"):
            task_id = task.replace("status","").strip()
            try:
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.get(f"{OPENMANUS_URL}/api/tasks/{task_id}")
                    await send(chat_id, f"OpenManus task `{task_id}`:\n{r.text[:300]}")
            except Exception as ex:
                await send(chat_id, f"Blad: {ex}")
        elif task:
            await do_openmanus_task(chat_id, task)
        else:
            await send(chat_id, "Uzycie: `/openmanus <opis zadania>`\nLub: `/openmanus status <task_id>`")
        return

    # /content <site> <type> <prompt>
    if tl.startswith("/content"):
        parts = t[8:].strip().split(None, 2)
        if len(parts) >= 3:
            await do_content_generate(chat_id, parts[0], parts[1], parts[2])
        else:
            await send(chat_id,
                "Uzycie: `/content kamila product Kurs fotografii dla dzieci`\n"
                "Typy: product, blog_post, course, quiz")
        return

    # /resolve <id>
    if tl.startswith("/resolve"):
        alert_id = tl.replace("/resolve","").strip()
        if alert_id.isdigit():
            await sb("public_resolve_critical", {"p_id": int(alert_id), "p_resolution": "Resolved by owner via Telegram"})
            await send(chat_id, f"Alert #{alert_id} zamkniety.")
        else:
            await send(chat_id, "Uzycie: `/resolve 42`")
        return

    # /claude <q> — wymuszony Sonnet
    if tl.startswith("/claude") or tl.startswith("/expert"):
        q = re.sub(r'^/(claude|expert)\s*', '', t, flags=re.I).strip() or t
        await typing(chat_id)
        await send(chat_id, "_Konsultacja z Claude Sonnet..._")
        reply = await ask_claude(chat_id, q, model=SONNET)
        await send_chunks(chat_id, f"*Claude Sonnet:*\n\n{reply}")
        return

    # /build <spec>
    if tl.startswith("/build"):
        desc = t[6:].strip()
        if not desc:
            await send(chat_id, "Uzycie: `/build chce zeby bot generowal blog posty dla kamila-site codziennie`")
            return
        await typing(chat_id)
        spec = await ask_claude(chat_id,
            f"Maciej potrzebuje: '{desc}'\n"
            "Przygotuj krotka specyfikacje techniczna (150 slow): co zbudowac, "
            "jakie API/dane, jak wdrozyc. Konkretnie.",
            model=SONNET, no_history=True)
        await send_chunks(chat_id, f"*Specyfikacja:*\n\n{spec}\n\n"
            "_Wklej do Claude.ai lub powiedz Manusowi zeby zbudowal._")
        return


    # ── Claude (ja) przez Guardiana ─────────────────────────────────
    if tl.startswith("/claude") or tl.startswith("/expert"):
        q = text[len("/claude"):].strip() if tl.startswith("/claude") else text[len("/expert"):].strip()
        if not q: q = "Pomóż z analizą infrastruktury"
        await typing(chat_id)
        await send(chat_id, "_Konsultuję z Claude Sonnet..._")
        reply = await ask_claude(chat_id, q, model=SONNET, force_sonnet=True)
        await send_chunks(chat_id, f"*Claude Sonnet:*\n\n{reply}")
        return

    # ── Manus Brain przez Guardian ───────────────────────────────────
    if tl.startswith("/manus"):
        q = text[6:].strip()
        if not q:
            await send(chat_id, "Użycie: `/manus jakie modele masz dostępne?`"); return
        await typing(chat_id)
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post("https://brain.ofshore.dev/api/guardian",
                    json={"message": q, "userId": f"guardian_{chat_id}"},
                    headers={"Content-Type":"application/json"})
                if r.status_code == 200:
                    reply = r.json().get("reply","brak odpowiedzi")
                    await send_chunks(chat_id, f"*Manus Brain:*\n\n{reply}")
                else:
                    await send(chat_id, f"Manus Brain błąd: {r.status_code}")
        except Exception as ex:
            await send(chat_id, f"Manus niedostępny: {ex}")
        return

    # Jezyk naturalny
    await typing(chat_id)
    
    extra = ""
    if any(w in tl for w in ["nie dziala","blad","awaria","crash","down"]):
        apps = await get_apps(force=True)
        broken = [a["name"] for a in apps if "exited" in a.get("status","") or "restarting" in a.get("status","")]
        if broken: extra = f"Down apps: {', '.join(broken)}"
    
    model = SONNET if needs_sonnet(tl) else HAIKU
    reply = await ask_claude(chat_id, t, model=model, extra=extra)
    
    # Jezeli bot mowi ze czegos nie moze
    cant = any(p in reply.lower() for p in ["nie mam dostepu","nie moge","nie jestem","poza moimi"])
    if cant:
        reply += "\n\n_Jesli chcesz zbudowac te funkcje: `/build <opis>`_"
    
    await send_chunks(chat_id, reply)

async def handle_cb(cb_id: str, chat_id: str, user_id: str, data: str):
    await answer_cb(cb_id)
    actions = {
        "status": do_status, "savings": do_savings, "critical": do_critical,
        "smoke": lambda c: do_autonomy_log(c),  # reuse
        "autonomy": do_autonomy_log, "goals": do_goals,
    }
    if data in actions:
        await actions[data](chat_id)
    elif data == "smoke":
        # real smoke
        summary = await sb("public_get_smoke_summary") or []
        ok = sum(1 for s in summary if s.get("passed"))
        fail = [(s["app_name"],s["test_name"]) for s in summary if not s.get("passed")]
        lines = [f"*Smoke testy* `{datetime.now().strftime('%H:%M')}`\n{ok}/{len(summary)} OK"]
        for a,t in fail[:8]: lines.append(f"- `{a}/{t}`")
        await send(chat_id, "\n".join(lines) if fail else f"Wszystkie {ok} testy OK!")
    elif data == "tree":           await do_tree(chat_id)
    elif data.startswith("logs_"): await do_logs(chat_id, data[5:])
    elif data.startswith("restart_"): await do_restart(chat_id, data[8:])

# ── Main ──────────────────────────────────────────────────────────────────
async def main():
    log.info("Guardian Bot v6 starting — Autonomous Manager")
    
    async with httpx.AsyncClient(timeout=10) as c:
        me = (await c.get(f"{TG}/getMe")).json()
    if not me.get("ok"):
        log.error(f"Bad token"); return
    
    bot_name = me["result"]["username"]
    log.info(f"@{bot_name} ready | Claude: HAIKU+SONNET | OpenAI: {'yes' if OPENAI_KEY else 'no'} | n8n: {'yes' if N8N_KEY else 'no'}")

    if ADMIN_ID:
        s = await snap()
        missing_auto = s["goals_missing"]
        msg = (f"*Guardian v6 uruchomiony!*\n\n"
               f"{s['healthy']}/{s['total']} apps OK"
               + (f"\nDo zbudowania: {missing_auto} automatyzacji" if missing_auto else "")
               + (f"\n!!DOWN: {', '.join(s['broken_names'])}" if s["broken_names"] else ""))
        await tg("sendMessage", {"chat_id":ADMIN_ID,"text":msg,"parse_mode":"Markdown","reply_markup":kbd_main()})

    asyncio.create_task(watcher())
    asyncio.create_task(daily_reporter())

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
                if "Conflict" in data.get("description",""):
                    log.warning(f"409 backoff {conflict_backoff}s")
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
                    if msg.get("text","").strip():
                        asyncio.create_task(handle_msg(chat_id, user_id, msg["text"]))
                elif cb := upd.get("callback_query"):
                    asyncio.create_task(handle_cb(
                        cb["id"],
                        str(cb["message"]["chat"]["id"]),
                        str(cb["from"]["id"]),
                        cb.get("data","")))
        
        except asyncio.CancelledError: break
        except Exception as ex:
            log.error(f"Poll: {ex}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
