"""
Universal ofshore Bot Template
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Użyj tego jako bazę dla każdego nowego bota.
Webhook mode = brak 409, działa z HTTPS przez Traefik/Coolify.

Jak dodać nowego bota:
1. Skopiuj ten plik jako bot.py
2. Zmień BOT_NAME, BOT_ROLE, CAPABILITIES
3. Dodaj własne komendy w handle_update()
4. Stwórz repo: szachmacik/<bot-name>
5. Dodaj w Coolify z domeną <bot-name>.ofshore.dev
6. Bot automatycznie rejestruje się w Supabase i daje znać Guardianowi
"""

import asyncio, json, os, logging, time, threading, re, base64
import httpx
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── Konfiguracja — zmień to dla każdego bota ──────────────────────────
BOT_NAME    = os.environ.get("BOT_NAME", "Antygravity")
BOT_ROLE    = os.environ.get("BOT_ROLE", "developer")
CAPABILITIES = os.environ.get("BOT_CAPABILITIES", "code_fix,deploy,github,code_review,guardian_fix").split(",")

# ── Standardowe envs — takie same dla każdego bota ────────────────────
TG_TOKEN    = os.environ.get("ANTYGRAVITY_BOT_TOKEN","") or os.environ.get("TELEGRAM_BOT_TOKEN","")
CLAUDE_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
GH_TOKEN    = os.environ.get("GITHUB_TOKEN", "")
CT          = os.environ.get("COOLIFY_TOKEN", "")
COOLIFY     = os.environ.get("COOLIFY_URL", "https://coolify.ofshore.dev")
# Konto techniczne do visual browsing / backend testing
AG_EMAIL    = os.environ.get("AG_EMAIL", "antygravity@ofshore.dev")
AG_PASSWORD = os.environ.get("AG_PASSWORD", "")
AG_USER_ID  = os.environ.get("AG_USER_ID", "")
# OpenAI (opcjonalne — gdy dostępne)
OPENAI_KEY  = os.environ.get("OPENAI_API_KEY", "")
COOLIFY     = os.environ.get("COOLIFY_URL", "https://coolify.ofshore.dev")
SB_URL      = os.environ.get("SUPABASE_URL", "")
SB_KEY      = os.environ.get("SUPABASE_KEY", "")
ALLOWED     = set(x.strip() for x in os.environ.get("ALLOWED_TELEGRAM_IDS","").split(",") if x.strip())
ADMIN_ID    = os.environ.get("ADMIN_CHAT_ID", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
PORT        = int(os.environ.get("PORT", "8080"))
TG          = f"https://api.telegram.org/bot{TG_TOKEN}"
CLAUDE_H    = "claude-haiku-4-5-20251001"
CLAUDE_S    = "claude-sonnet-4-6"
# Ollama — darmowe modele (domyślne)
OLLAMA_URL  = os.environ.get("OLLAMA_URL", "http://ollama:11434")
OLLAMA_FAST = os.environ.get("OLLAMA_MODEL_FAST", "qwen2.5:0.5b")
OLLAMA_SMART= os.environ.get("OLLAMA_MODEL_SMART", "qwen2.5:7b")
OLLAMA_VISUAL=os.environ.get("OLLAMA_MODEL_VISUAL", "llava:7b")
AI_MODE     = os.environ.get("AI_MODE", "free")  # free=Ollama, paid=Claude

logging.basicConfig(level=logging.INFO,
    format=f"%(asctime)s [{BOT_NAME[:4].upper()}] %(message)s", datefmt="%H:%M:%S")
# ═══════════════════════════════════════════════════════════
# VAULT BOOTSTRAP — ładuje wszystkie envs z Supabase Vault
# Wystarczy ustawić AGENT_API_KEY i SUPABASE_URL
# Reszta kluczy pobiera się automatycznie
# ═══════════════════════════════════════════════════════════
import urllib.request as _urllib

def _bootstrap_from_vault():
    """Pobiera pełną konfigurację z Supabase Vault przy starcie."""
    secrets_url = os.environ.get("SECRETS_URL",
        "https://blgdhfcosqjzrutncbbr.supabase.co/functions/v1/secrets")
    agent_key = os.environ.get("AGENT_API_KEY", "ofshore-agents-2026")
    app_role  = os.environ.get("BOT_ROLE", "agent")
    app_name  = os.environ.get("BOT_NAME", "antygravity")
    
    try:
        body = json.dumps({"action":"bootstrap","app":app_name,"role":app_role}).encode()
        req = _urllib.Request(secrets_url,
            data=body,
            headers={"Content-Type":"application/json",
                     "x-agent-key":agent_key},
            method="POST")
        with _urllib.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode())
        if d.get("ok"):
            envs = d.get("envs", {})
            # Wgraj do os.environ tylko te które nie są już ustawione
            for k, v in envs.items():
                if k not in os.environ and v:
                    os.environ[k] = v
            missing = d.get("missing", [])
            if missing:
                logging.getLogger("vault").warning(f"Vault: brakuje {missing}")
            else:
                logging.getLogger("vault").info(f"Vault: {d.get('count',0)} envs loaded OK")
    except Exception as ex:
        logging.getLogger("vault").error(f"Vault bootstrap failed: {ex} — using existing envs")

# Uruchom bootstrap zanim cokolwiek innego
_bootstrap_from_vault()

log = logging.getLogger("bot")
sessions: dict[str, list] = {}

# ── Supabase helpers ──────────────────────────────────────────────────
async def sb(fn: str, params: dict = {}) -> any:
    if not SB_URL: return None
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{SB_URL}/rest/v1/rpc/{fn}",
                headers={"apikey":SB_KEY,"Authorization":f"Bearer {SB_KEY}",
                         "Content-Type":"application/json"}, json=params)
            return r.json() if r.status_code == 200 else None
    except: return None

async def sb_q(table: str, params: str = "") -> list:
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"{SB_URL}/rest/v1/{table}?{params}",
                headers={"apikey":SB_KEY,"Authorization":f"Bearer {SB_KEY}"})
            return r.json() if r.status_code == 200 else []
    except: return []

async def msg_guardian(subject: str, content: str, msg_type: str = "info"):
    """Wyślij wiadomość do Guardiana przez Supabase."""
    await sb("bot_send_message", {
        "p_from": BOT_NAME.lower(), "p_to": "guardian",
        "p_type": msg_type, "p_subject": subject, "p_content": content
    })

# ── Telegram helpers ──────────────────────────────────────────────────
async def tg_call(endpoint: str, payload: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.post(f"{TG}/{endpoint}", json=payload)
            return r.json()
    except: return {}

async def send(chat_id, text: str, parse_mode: str = "Markdown"):
    p = {"chat_id": chat_id, "text": text[:4096], "parse_mode": parse_mode}
    r = await tg_call("sendMessage", p)
    if not r.get("ok"):
        await tg_call("sendMessage", {"chat_id": chat_id, "text": text[:4096]})

async def send_chunks(chat_id, text: str):
    for i in range(0, min(len(text), 12000), 3800):
        await send(chat_id, text[i:i+3800])
        if len(text) > 3800: await asyncio.sleep(0.3)

# ── Claude AI ─────────────────────────────────────────────────────────

# ── GitHub & Coolify helpers ──────────────────────────────────────────────────
def gh_get_file(repo: str, path: str):
    import subprocess as sp
    r = sp.run(["curl","-s",
        f"https://api.github.com/repos/szachmacik/{repo}/contents/{path}",
        f"--header", f"Authorization: token {GH_TOKEN}"],
        capture_output=True, text=True)
    d = json.loads(r.stdout)
    if isinstance(d,dict) and "content" in d:
        return base64.b64decode(d["content"]).decode(), d.get("sha","")
    return "", ""

def gh_put_file(repo: str, path: str, content: str, sha: str, msg: str) -> bool:
    import subprocess as sp
    body = {"message":msg,"content":base64.b64encode(content.encode()).decode()}
    if sha: body["sha"] = sha
    r = sp.run(["curl","-s","-X","PUT",
        f"https://api.github.com/repos/szachmacik/{repo}/contents/{path}",
        "-H",f"Authorization: token {GH_TOKEN}",
        "-H","Content-Type: application/json",
        "-d",json.dumps(body)], capture_output=True, text=True)
    return "content" in r.stdout

async def cf(path: str, method="GET", body=None):
    if not CT: return {}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.request(method, f"{COOLIFY}/api/v1{path}",
                headers={"Authorization":f"Bearer {CT}"}, json=body)
            return r.json() if r.status_code in (200,201) else {}
    except: return {}

PERSONA = f"""Jestes {BOT_NAME} — AI bot dla ofshore.dev.
Rola: {BOT_ROLE}.
Umiejetnosci: {', '.join(CAPABILITIES)}.
Odpowiadaj po polsku gdy user pisze po polsku. Konkretnie i bez owijania."""

async def ask_ollama(msg: str, chat_id: str = "bot",
                    model: str = None, extra: str = "") -> str:
    """Darmowy model przez Ollama — domyślny dla wszystkich zapytań."""
    m = model or OLLAMA_FAST
    hist = sessions.get(chat_id, [])
    system = PERSONA + (f"\n\nKONTEKST:\n{extra}" if extra else "")
    prompt = f"{system}\n\nUser: {msg}\nAssistant:"
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{OLLAMA_URL}/api/generate",
                json={"model": m, "prompt": prompt, "stream": False,
                      "options": {"num_predict": 800, "temperature": 0.7}})
            if r.status_code == 200:
                reply = r.json().get("response","").strip()
                hist.append({"role":"user","content":msg})
                hist.append({"role":"assistant","content":reply})
                sessions[chat_id] = hist[-20:]
                return reply
            else:
                log.warning(f"Ollama {r.status_code} — fallback Claude")
                return await ask_claude_paid(msg, chat_id, extra=extra)
    except Exception as ex:
        log.warning(f"Ollama error ({ex}) — fallback Claude")
        return await ask_claude_paid(msg, chat_id, extra=extra)

async def ask_claude_paid(msg: str, chat_id: str = "bot",
                          model: str = None, extra: str = "") -> str:
    """Płatny Claude — tylko gdy Ollama nie radzi sobie lub force_paid=True."""
    m = model or CLAUDE_H
    hist = sessions.get(chat_id, [])
    msgs = hist[-10:] + [{"role": "user", "content": msg}]
    system = PERSONA + (f"\n\nKONTEKST:\n{extra}" if extra else "")
    try:
        async with httpx.AsyncClient(timeout=40) as c:
            r = await c.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": m, "max_tokens": 1500,
                      "system": system, "messages": msgs})
            d = r.json()
            if "content" in d:
                reply = d["content"][0]["text"]
                hist.append({"role": "user", "content": msg})
                hist.append({"role": "assistant", "content": reply})
                sessions[chat_id] = hist[-20:]
                return reply
    except Exception as ex:
        log.error(f"Claude: {ex}")
    return "Błąd AI."

async def ask_claude(msg: str, chat_id: str = "bot",
                     model: str = None, extra: str = "") -> str:
    """Router AI: domyślnie Ollama (darmowe), Claude tylko gdy force lub Ollama zawiedzie."""
    # Zadania wymagające Claude (złożone naprawy kodu, architektura)
    needs_claude = any(w in msg.lower() for w in [
        "napraw", "fix", "typescript", "dockerfile", "architektura",
        "przepisz", "zrefaktoruj", "debug", "błąd składni"
    ])
    if AI_MODE == "paid" or needs_claude:
        log.info(f"[AI] Claude ({CLAUDE_H}) — {'forced' if AI_MODE=='paid' else 'code task'}")
        return await ask_claude_paid(msg, chat_id, model, extra)
    else:
        log.info(f"[AI] Ollama ({OLLAMA_FAST}) — free mode")
        return await ask_ollama(msg, chat_id, extra=extra)

# ── Message handler — TUTAJ DODAJ WŁASNE KOMENDY ─────────────────────
async def handle_update(update: dict):
    msg = update.get("message") or update.get("edited_message")
    if not msg: return
    chat_id = str(msg["chat"]["id"])
    user_id = str(msg["from"]["id"])
    text    = msg.get("text", "").strip()
    if not text: return
    if ALLOWED and user_id not in ALLOWED and chat_id not in ALLOWED:
        await send(chat_id, "🔒 Brak dostępu."); return

    log.info(f"[{chat_id}] {text[:60]}")
    tl = text.lower()

    # ── Standardowe komendy (każdy bot ma) ──────────────────────────
    if tl in ["/start", "start"]:
        await send(chat_id,
            f"*{BOT_NAME} Bot* ✅\n\n"
            f"Rola: {BOT_ROLE}\n"
            f"Umiejętności: {', '.join(CAPABILITIES)}\n\n"
            "`/help` — pomoc\n"
            "`/team` — lista botów w zespole\n"
            "Lub pisz naturalnie — rozumiem po polsku!"); return

    if tl in ["/help", "help", "pomoc"]:
        team = await sb("bot_get_team") or []
        team_str = "\n".join(f"  • {b['name']} (@{b.get('username','?')}) — {b['role']}"
                             for b in team)
        await send(chat_id,
            f"*{BOT_NAME} — Pomoc*\n\n"
            f"Jestem: {PERSONA}\n\n"
            f"*Zespół botów:*\n{team_str}"); return

    if tl in ["/team", "team", "zespół", "zespol"]:
        team = await sb("bot_get_team") or []
        lines = ["*Zespół ofshore.dev:*\n"]
        for b in (team or []):
            domain = b.get("domain","")
            lines.append(f"• *{b['name']}* (@{b.get('username','?')}) — {b['role']}"
                        + (f"\n  🌐 {domain}" if domain else ""))
        await send(chat_id, "\n".join(lines)); return

    if tl in ["/clear", "clear", "wyczyść"]:
        sessions.pop(chat_id, None)
        await send(chat_id, "🧹 Historia wyczyszczona."); return

    # ── TUTAJ DODAJ WŁASNE KOMENDY ───────────────────────────────────
    # if tl.startswith("/moja_komenda"):
    #     await handle_moja_komenda(chat_id, text[len("/moja_komenda"):].strip())
    #     return

    # ── Komendy Antygravity ──────────────────────────────────────────
    if tl in ["/tasks", "tasks", "zadania"]:
        tasks = await sb_q("antygravity_tasks",
            "status=eq.pending&order=created_at.asc&limit=10")
        if not tasks:
            await send(chat_id, "✅ Brak zadań w kolejce."); return
        lines = [f"*Zadania ({len(tasks)}):*\n"]
        for t2 in tasks[:8]:
            p = {"critical":"🔴","high":"🟡","normal":"🟢"}.get(t2.get("priority",""),"⚪")
            lines.append(f"{p} `{t2['repo_name']}` — {t2['description'][:70]}")
        await send(chat_id, "\n".join(lines)); return

    if tl in ["/status", "status"]:
        repos = await sb_q("repo_knowledge", "order=updated_at.desc&limit=15")
        broken = [r for r in repos if not r.get("guardian_status","").startswith("OK")]
        ok_r   = [r for r in repos if r.get("guardian_status","").startswith("OK")]
        lines = [f"*Repo status*\n✅ OK: {len(ok_r)} | ❌ Broken: {len(broken)}\n"]
        if broken:
            lines.append("*Do naprawy:*")
            for r in broken[:6]:
                lines.append(f"  ❌ `{r['repo_name']}` — {r.get('guardian_status','?')[:50]}")
        await send(chat_id, "\n".join(lines)); return

    if tl.startswith("/fix"):
        repo = t[4:].strip()
        if not repo:
            await send(chat_id, "Użycie: `/fix quiz-manager`"); return
        await send(chat_id, f"🔧 Naprawiam guardian w `{repo}`...")
        import re
        idx, idx_sha = gh_get_file(repo, "server/_core/index.ts")
        if not idx:
            await send(chat_id, f"❌ Nie znalazłem index.ts w `{repo}`."); return
        new_idx = re.sub(
            r"server\.listen\(port,\s*\(\)",
            'server.listen(port, "0.0.0.0", ()',
            idx)
        if "findAvailablePort" in new_idx:
            new_idx = re.sub(
                r"const preferredPort.+?\n.+?findAvailablePort.+?\n.+?if \(port.+?\n.+?console.+?\n.+?\}\n\n\s+server\.listen\(port",
                'const port = parseInt(process.env.PORT || "3000");\n\n  server.listen(port, "0.0.0.0"',
                new_idx, flags=re.DOTALL)
        ok_push = gh_put_file(repo, "server/_core/index.ts", new_idx, idx_sha,
            f"fix: port 0.0.0.0 binding (Antygravity fix)")
        repos_db = await sb_q("repo_knowledge", f"repo_name=eq.{repo}")
        uuid = repos_db[0].get("coolify_uuid","") if repos_db else ""
        dep_id = ""
        if uuid:
            dep = await cf(f"/deploy?uuid={uuid}&force=true","GET")
            dep_id = dep.get("deployments",[{}])[0].get("deployment_uuid","")[:12]
        await msg_guardian(f"Fixed: {repo}", f"Port fix applied, deploy: {dep_id or 'no uuid'}", "feedback")
        await send(chat_id, f"✅ `{repo}` — push={'OK' if ok_push else 'FAIL'}, deploy={dep_id or 'sprawdź Coolify'}"); return

    if tl in ["/sync", "sync"]:
        tasks2 = await sb_q("antygravity_tasks","status=eq.pending&limit=5")
        await msg_guardian("Antygravity sync", f"Online, {len(tasks2)} zadań pending")
        await send(chat_id, f"Sync z Guardianem OK. Zadań pending: {len(tasks2)}"); return


    if tl.startswith("/manus"):
        q = t[6:].strip()
        if not q:
            await send(chat_id, "Użycie: `/manus zapytaj Manusa`"); return
        await send(chat_id, "_Pytam Manus Brain..._")
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post("https://brain.ofshore.dev/api/guardian",
                    json={"message":q,"userId":f"antygravity_{chat_id}"},
                    headers={"Content-Type":"application/json"})
                if r.status_code == 200:
                    reply = r.json().get("reply","")
                    await send_chunks(chat_id, f"*Manus:*\n\n{reply}")
                else:
                    await send(chat_id, f"Manus błąd: {r.status_code}")
        except Exception as ex:
            await send(chat_id, f"Manus niedostępny: {ex}")
        return

    if tl.startswith("/audit"):
        app = t[6:].strip() or "openmanus"
        url_map = {
            "openmanus": "https://openmanus.ofshore.dev",
            "agentflow": "https://agentflow.ofshore.dev",
            "sentinel": "https://sentinel.ofshore.dev",
            "hub": "https://hub.ofshore.dev",
            "quiz": "https://quiz.ofshore.dev",
            "wp": "https://wp-manager.ofshore.dev",
        }
        url = url_map.get(app, f"https://{app}.ofshore.dev")
        await send(chat_id, f"🔍 Audytuję `{app}` ({url})...")
        import time as _time
        t0 = _time.time()
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
                # Test jako niezalogowany user
                r1 = await c.get(url)
                t1 = int((_time.time()-t0)*1000)
                # Test API health
                r2 = await c.get(f"{url}/api/health")
                # Test guardian
                r3 = await c.post(f"{url}/api/guardian",
                    json={"message":"audit ping","userId":"antygravity"},
                    headers={"Content-Type":"application/json"})
                
                issues = []
                if r1.status_code != 200: issues.append(f"HTTP {r1.status_code} na głównej")
                if "guardian" not in r3.text and '"reply"' not in r3.text:
                    issues.append("Guardian nie odpowiada na /api/guardian")
                
                guardian_ok = '"reply"' in r3.text
                report = (
                    f"*Audit: {app}*\n\n"
                    f"🌐 HTTP: {r1.status_code} ({t1}ms)\n"
                    f"❤️ Health: {r2.status_code}\n"
                    f"🤖 Guardian: {'✅ OK' if guardian_ok else '❌ HTML/brak'}\n"
                )
                if issues:
                    report += f"\n⚠️ Problemy ({len(issues)}):\n"
                    for iss in issues: report += f"  • {iss}\n"
                else:
                    report += "\n✅ Wszystko OK"
                
                # Zapisz do Supabase
                await sb("bot_save_audit", {
                    "p_app": app, "p_url": url,
                    "p_issues": json.dumps(issues),
                    "p_ui_notes": f"HTTP {r1.status_code}",
                    "p_backend_notes": f"Guardian: {guardian_ok}",
                    "p_status": r1.status_code, "p_time_ms": t1
                })
                # GPT-4o visual analysis
                if OPENAI_KEY:
                    gpt = await gpt4_analyze(url)
                    if gpt:
                        report += f"\n*GPT-4o:*\n{gpt[:250]}"
                await send(chat_id, report)
        except Exception as ex:
            await send(chat_id, f"❌ Audit error: {ex}")
        return

    # ── Zmiana trybu AI ──────────────────────────────────────────────
    if tl.startswith("/aimode"):
        mode = t[7:].strip().lower()
        if mode in ["free","paid"]:
            import os
            os.environ["AI_MODE"] = mode
            global AI_MODE
            AI_MODE = mode
            icon = "🆓" if mode=="free" else "💰"
            await send(chat_id,
                f"{icon} Tryb AI zmieniony na: *{mode}*\n\n"
                f"{'Ollama (darmowy) — domyślny' if mode=='free' else 'Claude (płatny) — wszystkie zapytania'}")
        else:
            await send(chat_id,
                f"Aktualny tryb: *{AI_MODE}*\n\n"
                "`/aimode free` — Ollama domyślnie (darmowe)\n"
                "`/aimode paid` — Claude zawsze (płatne)\n\n"
                "W trybie *free*: Ollama dla rozmów, Claude tylko dla napraw kodu.")
        return

    # ── AI fallback ──────────────────────────────────────────────────
    reply = await ask_claude(text, chat_id)
    await send_chunks(chat_id, reply)

# ── Webhook HTTP server ────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok", "bot": BOT_NAME, "role": BOT_ROLE,
                "mode": "webhook"
            }).encode())
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == f"/webhook/{TG_TOKEN}":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                update = json.loads(body)
                loop = asyncio.new_event_loop()
                loop.run_until_complete(handle_update(update))
                loop.close()
            except Exception as ex:
                log.error(f"Webhook: {ex}")
            self.send_response(200); self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404); self.end_headers()

async def setup_webhook(base_url: str) -> bool:
    webhook = f"{base_url}/webhook/{TG_TOKEN}"
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post(f"{TG}/deleteWebhook", json={"drop_pending_updates": False})
        await asyncio.sleep(1)
        r = await c.post(f"{TG}/setWebhook", json={
            "url": webhook, "allowed_updates": ["message", "edited_message",
                                                 "callback_query"]})
        d = r.json()
        if d.get("ok"):
            log.info(f"Webhook: {webhook}")
            return True
        log.error(f"Webhook failed: {d}")
        return False


async def auto_task_loop():
    """Co 30min pobierz i wykonaj zadania z kolejki (antygravity_tasks + agent_tasks)."""
    await asyncio.sleep(90)  # poczekaj na pełny start
    while True:
        try:
            # ── 1. Agent Tasks (nowy system) ──────────────────────────
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post("https://blgdhfcosqjzrutncbbr.supabase.co/functions/v1/agent-dispatch",
                    headers={"x-agent-key":"ofshore-agents-2026","Content-Type":"application/json"},
                    json={"action":"get_mine","agent":"antygravity","limit":3})
                data = r.json()
            
            for task in data.get("tasks", []):
                tid = task["id"]
                ttype = task.get("task_type","")
                title = task.get("title","")
                desc  = task.get("description","")
                payload = task.get("payload", {})
                
                log.info(f"[DISPATCH] Task #{tid}: {title[:50]}")
                
                # Claim zadanie
                async with httpx.AsyncClient(timeout=5) as c:
                    cr = await c.post("https://blgdhfcosqjzrutncbbr.supabase.co/functions/v1/agent-dispatch",
                        headers={"x-agent-key":"ofshore-agents-2026","Content-Type":"application/json"},
                        json={"action":"claim","task_id":tid,"agent":"antygravity"})
                    if not cr.json().get("ok"): continue
                
                result = "skipped"
                try:
                    if ttype in ["code_fix","deploy"]:
                        # Użyj Claude do analizy + własnych narzędzi do naprawy
                        advice = await ask_ollama(
                            f"Zadanie: {desc}\n\nPayload: {json.dumps(payload)}\n\n"
                            f"Co powinienem zrobić krok po kroku? Bądź konkretny.",
                            chat_id="auto_task"
                        )
                        result = f"Analiza: {advice[:300]}"
                        
                    elif ttype == "audit":
                        url = payload.get("url","")
                        if url:
                            async with httpx.AsyncClient(timeout=15) as c:
                                ra = await c.post(
                                    f"{SB_URL}/functions/v1/visual-audit",
                                    headers={"x-agent-key":"ofshore-agents-2026","Content-Type":"application/json"},
                                    json={"url":url,"app_name":payload.get("app_name","")}
                                )
                                ad = ra.json()
                            result = f"Audit {url}: {'OK' if ad.get('ok') else 'Issues: '+str(ad.get('issues',[]))}"
                    
                    elif ttype in ["analyze","research","generate"]:
                        # Wyślij do Claude przez bridge
                        async with httpx.AsyncClient(timeout=30) as c:
                            ra = await c.post(
                                f"{SB_URL}/functions/v1/claude-bridge",
                                headers={"x-agent-key":"ofshore-agents-2026","Content-Type":"application/json"},
                                json={"from":"antygravity","message":desc,"tier":"smart"}
                            )
                            result = ra.json().get("reply","")[:500]
                    
                    else:
                        result = f"Task type '{ttype}' not handled yet"

                except Exception as ex:
                    result = f"Error: {ex}"
                    log.error(f"[DISPATCH] Task #{tid} error: {ex}")
                
                # Zakończ zadanie
                async with httpx.AsyncClient(timeout=5) as c:
                    await c.post("https://blgdhfcosqjzrutncbbr.supabase.co/functions/v1/agent-dispatch",
                        headers={"x-agent-key":"ofshore-agents-2026","Content-Type":"application/json"},
                        json={"action":"complete","task_id":tid,"agent":"antygravity",
                              "result":result,"status":"done" if "Error" not in result else "failed"})
                log.info(f"[DISPATCH] Task #{tid} done: {result[:80]}")

        except Exception as ex:
            log.debug(f"[DISPATCH] loop: {ex}")
        
        await asyncio.sleep(1800)  # co 30 minut

async def auto_task_loop():
    """Co 30min automatycznie wykonaj pierwsze zadanie z kolejki."""
    await asyncio.sleep(60)  # poczekaj na start
    while True:
        try:
            tasks = await sb_q("antygravity_tasks",
                "status=eq.pending&order=created_at.asc&limit=1")
            if tasks:
                task = tasks[0]
                repo  = task["repo_name"]
                ttype = task["task_type"]
                tid   = task["id"]
                log.info(f"[AUTO] Executing task: {ttype} on {repo}")

                # Oznacz jako in_progress
                async with httpx.AsyncClient(timeout=5) as c:
                    await c.patch(
                        f"{SB_URL}/rest/v1/antygravity_tasks?id=eq.{tid}",
                        headers={"apikey":SB_KEY,"Authorization":f"Bearer {SB_KEY}",
                                 "Content-Type":"application/json","Prefer":"return=minimal"},
                        json={"status":"in_progress"})

                result = "skipped"
                try:
                    if ttype == "fix_guardian":
                        # Fix port binding w index.ts
                        import re as _re
                        idx, idx_sha = gh_get_file(repo,"server/_core/index.ts")
                        if idx:
                            new_idx = _re.sub(
                                r'server\.listen\(port,\s*\(\)',
                                'server.listen(port, "0.0.0.0", ()',
                                idx)
                            if new_idx != idx:
                                gh_put_file(repo,"server/_core/index.ts",new_idx,idx_sha,
                                    f"auto-fix: port binding (Antygravity auto-loop)")
                                # Deploy
                                repos_db = await sb_q("repo_knowledge",f"repo_name=eq.{repo}")
                                uuid = repos_db[0].get("coolify_uuid","") if repos_db else ""
                                if uuid:
                                    dep = await cf(f"/deploy?uuid={uuid}&force=true","GET")
                                    result = f"fixed+deployed {dep.get('deployments',[{}])[0].get('deployment_uuid','')[:10]}"
                                else:
                                    result = "fixed, no uuid"
                            else:
                                result = "no change needed"
                    elif ttype == "fix_crash":
                        result = "crash fix requires manual review"
                except Exception as ex:
                    result = f"error: {ex}"
                    log.error(f"[AUTO] Task error: {ex}")

                # Oznacz jako done/failed
                status = "done" if "fix" in result or "no change" in result else "failed"
                async with httpx.AsyncClient(timeout=5) as c:
                    await c.patch(
                        f"{SB_URL}/rest/v1/antygravity_tasks?id=eq.{tid}",
                        headers={"apikey":SB_KEY,"Authorization":f"Bearer {SB_KEY}",
                                 "Content-Type":"application/json","Prefer":"return=minimal"},
                        json={"status":status,"result":result,
                              "completed_at":"now()"})

                # Poinformuj Guardiana
                await msg_guardian(f"Auto-task done: {repo}",
                    f"{ttype} → {result}", "feedback")
                log.info(f"[AUTO] Task {tid} done: {result}")

        except Exception as ex:
            log.debug(f"[AUTO] loop: {ex}")
        await asyncio.sleep(1800)  # co 30min

async def periodic_check():
    """Co 5min sprawdź wiadomości i zarejestruj heartbeat."""
    while True:
        try:
            # Sprawdź wiadomości od innych botów
            msgs = await sb_q("bot_messages",
                f"to_bot=eq.{BOT_NAME.lower()}&read=eq.false&limit=3")
            for m in (msgs or []):
                log.info(f"Msg from {m.get('from_bot')}: {m.get('subject','?')}")
                if ADMIN_ID:
                    await send(ADMIN_ID,
                        f"📩 *{m.get('from_bot','?')}:* {m.get('subject','?')}\n"
                        f"{m.get('content','')[:150]}")
                async with httpx.AsyncClient(timeout=5) as c:
                    await c.patch(
                        f"{SB_URL}/rest/v1/bot_messages?id=eq.{m['id']}",
                        headers={"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
                                 "Content-Type": "application/json", "Prefer": "return=minimal"},
                        json={"read": True})
            # Heartbeat
            async with httpx.AsyncClient(timeout=5) as c:
                await c.patch(f"{SB_URL}/rest/v1/bot_registry?bot_name=eq.{BOT_NAME}",
                    headers={"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
                             "Content-Type": "application/json", "Prefer": "return=minimal"},
                    json={"last_seen": "now()", "status": "active"})
        except: pass
        await asyncio.sleep(300)

async def main():
    log.info(f"{BOT_NAME} starting (polling with dedup)...")

    # Wyczyść webhook jeśli był ustawiony
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post(f"{TG}/deleteWebhook", json={"drop_pending_updates": False})
        me = (await c.get(f"{TG}/getMe")).json()

    username = me.get("result",{}).get("username","?")
    log.info(f"@{username} ready")

    # HTTP server do health checka (Coolify wymaga)
    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", PORT), Handler).serve_forever(),
        daemon=True).start()

    # Rejestracja w Supabase
    domain = WEBHOOK_URL.replace("https://","").split("/")[0] if WEBHOOK_URL else None
    await sb("bot_register", {
        "p_name": BOT_NAME, "p_username": username,
        "p_coolify_uuid": os.environ.get("COOLIFY_UUID",""),
        "p_domain": domain, "p_role": BOT_ROLE,
        "p_capabilities": CAPABILITIES
    })
    log.info("Registered in team registry")

    # Powiadom
    if ADMIN_ID:
        await send(ADMIN_ID,
            f"✅ *{BOT_NAME} online!*\n\n"
            f"Możesz teraz pisać do mnie normalnie.\n"
            f"Wpisz /help żeby zobaczyć co umiem.")

    asyncio.create_task(periodic_check())
    asyncio.create_task(auto_task_loop())

    # POLLING z deduplikacją przez Supabase
    # Pobierz aktualny offset - skip starych wiadomości przy restarcie
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{TG}/getUpdates", params={"offset": -1, "limit": 1})
            d = r.json()
            if d.get("ok") and d.get("result"):
                offset = d["result"][-1]["update_id"] + 1
                log.info(f"Starting from offset {offset} (skipping old messages)")
            else:
                offset = 0
    except:
        offset = 0
    conflict_backoff = 1
    log.info("Polling...")

    while True:
        try:
            async with httpx.AsyncClient(timeout=35) as c:
                r = await c.get(f"{TG}/getUpdates",
                    params={"offset": offset, "timeout": 30, "limit": 10})
                data = r.json()

            if not data.get("ok"):
                desc = data.get("description","")
                if "Conflict" in desc:
                    # Inny kontener polluje — czekaj dłużej
                    log.warning(f"409 conflict — backoff {conflict_backoff}s")
                    await asyncio.sleep(conflict_backoff)
                    conflict_backoff = min(conflict_backoff * 2, 60)
                    continue
                await asyncio.sleep(5)
                continue

            conflict_backoff = 1  # reset

            for upd in data["result"]:
                update_id = upd["update_id"]
                offset = update_id + 1

                # Distributed dedup — czy już przetworzyliśmy ten update?
                key = f"tg_update_{update_id}"
                try:
                    async with httpx.AsyncClient(timeout=3) as c:
                        check = await c.get(
                            f"{SB_URL}/rest/v1/bot_registry?bot_name=eq._update_{update_id}",
                            headers={"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"})
                        already = bool(check.json())
                except:
                    already = False

                if already:
                    continue  # już przetworzone przez inny kontener

                # Oznacz jako przetwarzane
                try:
                    async with httpx.AsyncClient(timeout=3) as c:
                        await c.post(f"{SB_URL}/rest/v1/bot_registry",
                            headers={"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
                                     "Content-Type": "application/json",
                                     "Prefer": "return=minimal"},
                            json={"bot_name": f"_update_{update_id}", "status": "processed",
                                  "role": "dedup"})
                except:
                    pass

                asyncio.create_task(handle_update(upd))

        except asyncio.CancelledError:
            break
        except Exception as ex:
            log.error(f"Poll: {ex}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
