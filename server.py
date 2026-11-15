# -*- coding: utf-8 -*-
"""
Chat Bash — v2 (fully free, no card anywhere).

Features (per owner's spec):
- Users must log in with Google (Firebase Auth). Temp-mail can't make a
  Google account, so disposable emails are blocked by design.
- Model switcher (Claude-style). Display names are Bash-branded ONLY —
  provider/model names never appear in the UI, answers or errors.
- Weekly per-user, per-model message allowance, counted server-side in
  Upstash Redis (free tier, REST): Lite 10/week, Pro 30/week, Swift 30/week.
- Brains: Gemini free API (Lite) + Groq free API (Pro/Swift), keys only in
  server env vars. Built-in Bangladesh knowledge pack + DuckDuckGo search.
- Identity rule in the system prompt: the AI is "Chat Bash", period.

Env vars:
  GEMINI_API_KEY, GROQ_API_KEY          provider keys (server-side only)
  FIREBASE_PROJECT_ID                   enables login enforcement
  FIREBASE_WEB_CONFIG_JSON              public web config JSON for the page
  UPSTASH_REDIS_REST_URL / _TOKEN       enables weekly allowance counting
  APP_NAME                              display name (default: Chat Bash)
If FIREBASE_PROJECT_ID is unset the app runs in DEV mode (no login needed)
— local testing only.
"""
import datetime
import json
import os
import time

import requests
from flask import Flask, jsonify, request

try:
    from ddgs import DDGS
except ImportError:  # older package name
    from duckduckgo_search import DDGS

from bd_knowledge import BD_FACTS, SEARCH_FIRST

APP_NAME = os.environ.get("APP_NAME", "Chat Bash")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "")
FIREBASE_WEB_CONFIG_JSON = os.environ.get("FIREBASE_WEB_CONFIG_JSON", "")
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
AUTH_REQUIRED = bool(FIREBASE_PROJECT_ID)
CREDITS_ON = bool(UPSTASH_URL and UPSTASH_TOKEN)

# ---------------- model registry (display names never leak providers) ----
MODELS = {
    "lite": {
        "name": "Bash Lite",
        "desc": "হালকা ও দ্রুত — প্রতিদিনের প্রশ্নের জন্য",
        "cap": 10,
        "provider": "gemini",
        "chain": [
            os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite"),
            "gemini-3.1-flash-lite",
            "gemini-3.6-flash",
        ],
    },
    "pro": {
        "name": "Bash Pro",
        "desc": "সবচেয়ে বুদ্ধিমান — গভীর ও লম্বা উত্তরের জন্য",
        "cap": 30,
        "provider": "groq",
        "chain": [
            os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
            "llama-3.3-70b-versatile",
            "openai/gpt-oss-20b",
        ],
    },
    "swift": {
        "name": "Bash Swift",
        "desc": "ঝটপট উত্তর — ছোট প্রশ্নের জন্য সেরা",
        "cap": 30,
        "provider": "groq",
        "chain": ["llama-3.1-8b-instant", "openai/gpt-oss-20b"],
    },
}
DEFAULT_MODEL = "pro" if GROQ_API_KEY else "lite"

SYSTEM_PROMPT = f"""তুমি "{APP_NAME}" — বাংলাদেশের জন্য তৈরি একটি AI সহকারী। তুমি সম্পূর্ণ ফ্রি ও ওপেন প্রযুক্তিতে বানানো — কোনো কোটি টাকার সরকারি প্রকল্প নও।

তোমার নিয়ম:
1. ভাষা: ব্যবহারকারী বাংলায় লিখলে বাংলা হরফে উত্তর দাও; ইংরেজিতে লিখলে ইংরেজিতে। ডিফল্ট ভাষা বাংলা।
2. বাংলাদেশ-প্রথম: অস্পষ্ট প্রশ্নে বাংলাদেশের প্রেক্ষাপট ধরো ("আমাদের দেশ" = বাংলাদেশ, দাম = টাকায়, জায়গা = বাংলাদেশের)।
3. নিজের জানা তথ্য থেকে সরাসরি উত্তর দাও। নিচের তথ্যভাণ্ডার তোমার স্থায়ী জ্ঞান:
{BD_FACTS}
{SEARCH_FIRST}
4. ওয়েব সার্চের নিয়ম: সার্চ দরকার হলে অন্য কিছু না লিখে ঠিক এই ফরম্যাটে এক লাইনে জবাব দাও:
SEARCH: <খোঁজার শব্দগুচ্ছ>
সার্চের ফল পেলে তা ব্যবহার করে পূর্ণ উত্তর দাও এবং শেষে ছোট করে সূত্র (ওয়েবসাইটের নাম) উল্লেখ করো।
5. না জানলে বানিয়ে বলবে না — সার্চ করো, তাতেও না পেলে সত্যটা স্বীকার করো।
6. উত্তর সহজ, প্রাণবন্ত ও কাজের রাখো — সাধারণত ১৫০ শব্দের মধ্যে। প্রয়োজনে পয়েন্ট আকারে দাও।
7. পরিচয় (সবচেয়ে কড়া নিয়ম): তুমি শুধুই "{APP_NAME}" — নাশিদের বানানো, সম্পূর্ণ ফ্রি প্রযুক্তিতে চলা বাংলাদেশের নিজের AI। কেউ জিজ্ঞেস করলেও তুমি কোন মডেল, কোন কোম্পানি বা কোন API দিয়ে চলো তা কখনোই বলবে না — Gemini, Google, Groq, Llama, OpenAI বা অন্য কোনো মডেল/কোম্পানির নাম মুখেও আনবে না। জিজ্ঞেস করলে বলবে: "আমি {APP_NAME} — সম্পূর্ণ ফ্রি প্রযুক্তিতে বানানো বাংলাদেশের নিজের AI।\""""

LIVE_HINTS = [
    "আজ", "এখন", "সর্বশেষ", "সাম্প্রতিক", "খবর", "দাম", "আবহাওয়া", "স্কোর",
    "জিতল", "হারল", "ফলাফল", "বর্তমান", "২০২৫", "২০২৬", "2025", "2026",
    "today", "now", "latest", "price", "news", "weather", "score",
]

GENERIC_ERROR = "দুঃখিত, এই মুহূর্তে উত্তরটা তৈরি করা যাচ্ছে না। একটু পরে আবার চেষ্টা করো।"

app = Flask(__name__)

# ---------------- web search ----------------

def web_search(query, max_results=5):
    results, last_err = None, None
    for _ in range(2):
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results))
            break
        except Exception as e:  # noqa: BLE001
            last_err, results = e, None
    if results is None:
        print("search failed:", last_err, flush=True)
        return "(সার্চ ব্যর্থ হয়েছে)"
    if not results:
        return "(সার্চে কিছু পাওয়া যায়নি)"
    lines = []
    for r in results:
        lines.append(f"- {r.get('title','')}\n  {r.get('body','')}\n  সূত্র: {r.get('href','')}")
    return "\n".join(lines)

# ---------------- Firebase ID-token verification (no Admin SDK) ---------
_cert_cache = {"at": 0, "certs": {}}
CERTS_URL = "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"


def _google_certs():
    if time.time() - _cert_cache["at"] > 3600 or not _cert_cache["certs"]:
        resp = requests.get(CERTS_URL, timeout=15)
        resp.raise_for_status()
        _cert_cache["certs"] = resp.json()
        _cert_cache["at"] = time.time()
    return _cert_cache["certs"]


def verify_id_token(token):
    """Return {uid, email, name} for a valid Firebase ID token, else None."""
    if not token or not AUTH_REQUIRED:
        return None
    try:
        import jwt
        from cryptography.x509 import load_pem_x509_certificate

        header = jwt.get_unverified_header(token)
        cert_pem = _google_certs().get(header.get("kid"))
        if not cert_pem:
            return None
        public_key = load_pem_x509_certificate(cert_pem.encode()).public_key()
        claims = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=FIREBASE_PROJECT_ID,
            issuer=f"https://securetoken.google.com/{FIREBASE_PROJECT_ID}",
        )
        return {
            "uid": claims.get("user_id") or claims.get("sub"),
            "email": claims.get("email", ""),
            "name": claims.get("name", ""),
        }
    except Exception as e:  # noqa: BLE001
        print("token verify failed:", e, flush=True)
        return None

# ---------------- weekly allowance (Upstash Redis REST) ------------------

def week_key():
    iso = datetime.date.today().isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _redis(*args):
    resp = requests.post(
        UPSTASH_URL,
        headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
        json=list(args),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("result")


def credits_used(uid, model_key):
    if not CREDITS_ON:
        return 0
    try:
        val = _redis("GET", f"cb:{uid}:{model_key}:{week_key()}")
        return int(val) if val is not None else 0
    except Exception as e:  # noqa: BLE001
        print("credits read failed:", e, flush=True)
        return 0


def credits_consume(uid, model_key):
    if not CREDITS_ON:
        return
    try:
        key = f"cb:{uid}:{model_key}:{week_key()}"
        used = _redis("INCR", key)
        if used == 1:
            _redis("EXPIRE", key, str(8 * 24 * 3600))
    except Exception as e:  # noqa: BLE001
        print("credits write failed:", e, flush=True)


def remaining_map(uid):
    out = {}
    for key, m in MODELS.items():
        used = credits_used(uid, key) if uid else 0
        out[key] = max(0, m["cap"] - used) if CREDITS_ON else m["cap"]
    return out

# ---------------- provider calls ----------------

def groq_llm(messages, chain, max_tokens=700):
    if not GROQ_API_KEY:
        raise RuntimeError("groq key missing")
    last = None
    for model in dict.fromkeys(chain):
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": model, "messages": messages, "temperature": 0.6,
                      "top_p": 0.9, "max_tokens": max_tokens},
                timeout=60,
            )
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"].strip()
            last = f"groq {model} HTTP {resp.status_code}"
        except Exception as e:  # noqa: BLE001
            last = f"groq {model}: {e}"
    raise RuntimeError(last or "groq failed")


def gemini_llm(messages, chain, max_tokens=700):
    if not GEMINI_API_KEY:
        raise RuntimeError("gemini key missing")
    system_text = ""
    contents = []
    for msg in messages:
        if msg["role"] == "system":
            system_text = msg["content"]
        elif msg["role"] in ("user", "assistant"):
            contents.append({
                "role": "user" if msg["role"] == "user" else "model",
                "parts": [{"text": msg["content"]}],
            })
    body = {
        "system_instruction": {"parts": [{"text": system_text}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.6, "topP": 0.9, "maxOutputTokens": max_tokens},
    }
    last = None
    for model in dict.fromkeys(chain):
        try:
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
                json=body,
                timeout=60,
            )
            if resp.status_code == 200:
                data = resp.json()
                parts = data["candidates"][0]["content"]["parts"]
                return "".join(p.get("text", "") for p in parts).strip()
            last = f"gemini {model} HTTP {resp.status_code}"
        except Exception as e:  # noqa: BLE001
            last = f"gemini {model}: {e}"
    raise RuntimeError(last or "gemini failed")


def llm(messages, model_key):
    m = MODELS[model_key]
    if m["provider"] == "gemini":
        return gemini_llm(messages, m["chain"])
    return groq_llm(messages, m["chain"])

# ---------------- chat logic ----------------

def respond(message, history, model_key):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for item in (history or [])[-6:]:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            user, bot = item
            if user:
                messages.append({"role": "user", "content": str(user)})
            if bot:
                messages.append({"role": "assistant", "content": str(bot)})

    low = message.lower()
    if any(h in low for h in LIVE_HINTS):
        found = web_search(message)
        messages.append({"role": "user", "content":
            f"{message}\n\n[ওয়েব সার্চের ফল — এগুলো ব্যবহার করে উত্তর দাও, শেষে সূত্র দাও]\n{found}"})
    else:
        messages.append({"role": "user", "content": message})

    reply = llm(messages, model_key)
    rounds = 0
    while reply.startswith("SEARCH:") and rounds < 2:
        query = reply[len("SEARCH:"):].strip().splitlines()[0]
        found = web_search(query)
        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content":
            f"[সার্চের ফল]\n{found}\n\nএবার পূর্ণ উত্তর দাও, শেষে সূত্রসহ।"})
        reply = llm(messages, model_key)
        rounds += 1
    if reply.startswith("SEARCH:"):
        reply = "দুঃখিত, এই মুহূর্তে তথ্যটা খুঁজে পেলাম না। আরেকভাবে প্রশ্ন করে দেখো।"
    return reply

# ---------------- page + routes ----------------

PAGE = r"""<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#131314">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Chat Bash">
<link rel="manifest" href="/manifest.json">
<link rel="icon" type="image/png" href="/icon-192.png">
<link rel="apple-touch-icon" href="/icon-180.png">
<title>__APP_NAME__ — বাংলাদেশের ফ্রি AI</title>
<style>
  :root{--bg:#131314;--panel:#1e1f20;--bubble:#28292a;--text:#e3e3e3;--muted:#9aa0a6;
        --accent:#57bb8a;--accent-ink:#06281a;--line:#333537}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Hind Siliguri','Noto Sans Bengali',system-ui,sans-serif;background:var(--bg);color:var(--text);
       display:flex;flex-direction:column;height:100dvh}
  header{display:flex;align-items:center;justify-content:space-between;gap:10px;
         padding:12px 16px;width:100%;max-width:860px;margin:0 auto}
  header .brand{font-size:20px;font-weight:700;letter-spacing:.2px;white-space:nowrap}
  header .brand small{display:block;font-size:11.5px;font-weight:400;color:var(--muted);letter-spacing:0}
  #userbox{font-size:13.5px;display:flex;gap:8px;align-items:center;color:var(--muted);
           max-width:55%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  #userbox button{background:none;border:1px solid var(--line);border-radius:999px;padding:5px 12px;
       cursor:pointer;font-family:inherit;color:var(--text);font-size:13px}
  #demo{background:#2b2108;color:#f5c04e;border-top:1px solid #4a3a12;border-bottom:1px solid #4a3a12;
        text-align:center;font-size:13px;line-height:1.5;padding:7px 14px}
  #bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between;
       padding:10px 16px 2px;width:100%;max-width:860px;margin:0 auto}
  #bar select{padding:8px 14px;border:1px solid var(--line);border-radius:999px;background:var(--panel);
       color:var(--text);font-family:inherit;font-size:14px;font-weight:600;max-width:100%}
  #remain{font-size:13px;color:var(--muted)}
  #chat{flex:1;overflow-y:auto;padding:18px 16px 8px;display:flex;flex-direction:column;gap:20px;
        width:100%;max-width:860px;margin:0 auto}
  .msg{line-height:1.65;white-space:pre-wrap;word-break:break-word;font-size:16px}
  .u{align-self:flex-end;background:var(--bubble);color:var(--text);max-width:82%;
     padding:10px 16px;border-radius:24px}
  .a{align-self:stretch;padding:2px}
  .chips{display:flex;flex-wrap:wrap;gap:8px;padding:6px 16px 2px;max-width:860px;margin:0 auto;width:100%}
  .chip{background:transparent;border:1px solid var(--line);color:var(--text);border-radius:999px;
        padding:8px 14px;font-size:13.5px;cursor:pointer;font-family:inherit}
  .chip:hover{background:var(--panel)}
  form{display:flex;align-items:center;gap:8px;background:var(--panel);border-radius:28px;
       padding:6px 6px 6px 20px;width:calc(100% - 24px);max-width:836px;margin:10px auto 4px}
  input{flex:1;padding:10px 0;border:none;font-size:16px;font-family:inherit;background:transparent;color:var(--text)}
  input:focus{outline:none}
  input::placeholder{color:var(--muted)}
  button.send{background:var(--accent);color:var(--accent-ink);border:none;border-radius:50%;
        width:40px;height:40px;flex:0 0 40px;font-size:19px;font-weight:700;cursor:pointer;font-family:inherit;line-height:1}
  button.send:disabled{opacity:.35}
  .fineprint{text-align:center;color:var(--muted);font-size:12px;padding:2px 16px 10px}
  .dots span{animation:b 1.2s infinite;display:inline-block}
  .dots span:nth-child(2){animation-delay:.15s}.dots span:nth-child(3){animation-delay:.3s}
  @keyframes b{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-4px)}}
  #login{position:fixed;inset:0;background:rgba(0,0,0,.72);display:none;align-items:center;justify-content:center;z-index:10;padding:16px}
  #login .card{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:20px;
       padding:28px 24px;max-width:380px;width:100%;text-align:center;box-shadow:0 10px 40px rgba(0,0,0,.5)}
  #login h2{margin-bottom:8px;font-size:21px}
  #login p{font-size:14.5px;line-height:1.6;margin-bottom:18px;color:var(--muted)}
  #gbtn{background:#fff;color:#1f1f1f;border:none;border-radius:999px;padding:12px 20px;font-size:16px;
       cursor:pointer;font-family:inherit;display:inline-flex;gap:10px;align-items:center;font-weight:600}
</style>
</head>
<body>
<header>
  <div class="brand">🇧🇩 __APP_NAME__<small>বাংলাদেশের নিজের ফ্রি AI</small></div>
  <span id="userbox"></span>
</header>
<div id="demo">⚠ ডেমো মোড — এই অ্যাপটা এখনো তৈরির কাজ চলছে। মাঝে মাঝে ভুল উত্তর বা একটু দেরি হতে পারে।</div>
<div id="bar">
  <select id="model"></select>
  <span id="remain"></span>
</div>
<div id="chat">
  <div class="msg a">আসসালামু আলাইকুম! আমি Chat Bash। বাংলাদেশ নিয়ে যেকোনো প্রশ্ন করো — ইতিহাস, খাবার, খেলা, ভ্রমণ, আজকের খবর বা দাম — চেষ্টা করব সঠিক উত্তর দিতে।</div>
</div>
<div class="chips">
  <button class="chip" type="button">বাংলাদেশের জাতীয় মাছ কোনটি?</button>
  <button class="chip" type="button">পদ্মা সেতু সম্পর্কে বলো</button>
  <button class="chip" type="button">আজ ঢাকার আবহাওয়া কেমন?</button>
  <button class="chip" type="button">কক্সবাজার ভ্রমণের টিপস দাও</button>
</div>
<form id="f">
  <input id="q" placeholder="Chat Bash-কে প্রশ্ন করো…" autocomplete="off">
  <button class="send" id="send" type="submit" aria-label="পাঠাও">↑</button>
</form>
<p class="fineprint">Chat Bash ভুল করতে পারে — গুরুত্বপূর্ণ তথ্য যাচাই করে নিও। সম্পূর্ণ ফ্রি প্রযুক্তিতে তৈরি, কোনো কোটি টাকার প্রকল্প নয়।</p>

<div id="login">
  <div class="card">
    <h2>🇧🇩 Chat Bash-এ স্বাগতম</h2>
    <p>চ্যাট শুরু করতে Google দিয়ে লগইন করো। প্রতি সপ্তাহে প্রতি মডেলে নির্দিষ্ট সংখ্যক ফ্রি মেসেজ পাবে — লগইন থাকলে হিসাবটা ঠিক থাকে, আর ভুয়া/টেম্প মেইলের ঝামেলাও থাকে না।</p>
    <button id="gbtn" type="button"><b style="color:#4285F4">G</b> Google দিয়ে লগইন</button>
    <p id="lerr" style="color:#f28b82;margin-top:12px"></p>
  </div>
</div>

<script src="https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js"></script>
<script src="https://www.gstatic.com/firebasejs/10.12.2/firebase-auth-compat.js"></script>
<script>
const FB_CONFIG = __FB_CONFIG__;
const MODELS = __MODELS_JSON__;
const AUTH_ON = Object.keys(FB_CONFIG).length > 0;
const chat=document.getElementById('chat'),form=document.getElementById('f'),
      q=document.getElementById('q'),send=document.getElementById('send'),
      modelSel=document.getElementById('model'),remainEl=document.getElementById('remain'),
      userbox=document.getElementById('userbox'),loginEl=document.getElementById('login');
let history=[], currentUser=null, remaining={};

MODELS.forEach(m=>{const o=document.createElement('option');o.value=m.key;
  o.textContent=m.name+' — '+m.desc; modelSel.appendChild(o);});
modelSel.value='__DEFAULT_MODEL__';

function showRemaining(){
  const m=MODELS.find(x=>x.key===modelSel.value);
  const r=remaining[modelSel.value];
  remainEl.textContent = (r===undefined)? ('সপ্তাহে '+m.cap+'টি ফ্রি মেসেজ') :
    ('এই সপ্তাহে বাকি: '+r+' / '+m.cap);
}
modelSel.onchange=showRemaining;

function add(text,cls){const d=document.createElement('div');d.className='msg '+cls;d.textContent=text;
  chat.appendChild(d);chat.scrollTop=chat.scrollHeight;return d;}
document.querySelectorAll('.chip').forEach(c=>c.onclick=()=>{q.value=c.textContent;form.requestSubmit();});

async function refreshMe(){
  if(!currentUser) return;
  try{
    const t=await currentUser.getIdToken();
    const r=await fetch('/me',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({idToken:t})});
    const d=await r.json(); if(d.remaining){remaining=d.remaining; showRemaining();}
  }catch(e){}
}

if(AUTH_ON){
  firebase.initializeApp(FB_CONFIG);
  firebase.auth().onAuthStateChanged(u=>{
    currentUser=u;
    if(u){
      loginEl.style.display='none';
      userbox.innerHTML='<span>'+(u.displayName||u.email)+'</span>';
      const b=document.createElement('button'); b.textContent='লগআউট'; b.type='button';
      b.onclick=()=>firebase.auth().signOut(); userbox.appendChild(b);
      refreshMe();
    }else{
      userbox.innerHTML=''; loginEl.style.display='flex';
    }
  });
  document.getElementById('gbtn').onclick=async()=>{
    try{ await firebase.auth().signInWithPopup(new firebase.auth.GoogleAuthProvider()); }
    catch(e){ document.getElementById('lerr').textContent='লগইন হয়নি: '+e.message; }
  };
}else{
  userbox.innerHTML='<span>(ডেভ মোড — লগইন বন্ধ)</span>';
}
showRemaining();

form.onsubmit=async e=>{
  e.preventDefault();
  const text=q.value.trim(); if(!text) return;
  if(AUTH_ON && !currentUser){ loginEl.style.display='flex'; return; }
  q.value=''; add(text,'u'); send.disabled=true;
  const wait=add('ভাবছি ','a'); wait.classList.add('dots');
  wait.innerHTML='ভাবছি <span>.</span><span>.</span><span>.</span>';
  try{
    let token=''; if(currentUser) token=await currentUser.getIdToken();
    const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:text,history:history,model:modelSel.value,idToken:token})});
    const data=await r.json();
    wait.classList.remove('dots');
    if(r.status===401){ wait.textContent='লগইনের মেয়াদ শেষ — আবার লগইন করো।';
      if(AUTH_ON) loginEl.style.display='flex'; }
    else if(r.status===429){ wait.textContent=data.reply; if(data.remaining){remaining=data.remaining; showRemaining();} }
    else{ wait.textContent=data.reply||'কিছু একটা সমস্যা হয়েছে।';
      history.push([text,wait.textContent]);
      if(data.remaining){remaining=data.remaining; showRemaining();} }
  }catch(err){ wait.classList.remove('dots'); wait.textContent='নেটওয়ার্কে সমস্যা — আবার চেষ্টা করো।'; }
  send.disabled=false; q.focus();
};
q.focus();
if('serviceWorker' in navigator){window.addEventListener('load',()=>{navigator.serviceWorker.register('/sw.js').catch(()=>{});});}
</script>
</body>
</html>"""


def public_models():
    return [{"key": k, "name": m["name"], "desc": m["desc"], "cap": m["cap"]}
            for k, m in MODELS.items()]


@app.get("/")
def home():
    page = PAGE.replace("__APP_NAME__", APP_NAME)
    page = page.replace("__FB_CONFIG__", FIREBASE_WEB_CONFIG_JSON or "{}")
    page = page.replace("__MODELS_JSON__", json.dumps(public_models(), ensure_ascii=False))
    page = page.replace("__DEFAULT_MODEL__", DEFAULT_MODEL)
    return page, 200, {"Content-Type": "text/html; charset=utf-8"}


MANIFEST_JSON = json.dumps({
    "name": "Chat Bash — বাংলাদেশের ফ্রি AI",
    "short_name": "Chat Bash",
    "description": "বাংলাদেশকে সবচেয়ে বেশি গুরুত্ব দেওয়া ফ্রি AI সহকারী।",
    "lang": "bn",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#131314",
    "theme_color": "#131314",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}, ensure_ascii=False)

SW_JS = (
    "const C='chatbash-v1';"
    "self.addEventListener('install',e=>{self.skipWaiting()});"
    "self.addEventListener('activate',e=>{e.waitUntil(clients.claim())});"
    "self.addEventListener('fetch',e=>{if(e.request.method!=='GET')return;"
    "e.respondWith(fetch(e.request).catch(()=>caches.match('/')))});"
)

ICON_192_B64 = "iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAIAAADdvvtQAAACzklEQVR42u3dzU1jQRCFUVPqJeREBARClOQEAbBjgRB6xuK569b51jOj1+VDuYcf8XB5fblIf62MQAAJIAEkgCSABJAAEkASQAJIAAkgCSABJIAEkASQABJAAkgASQAJIAEkgCSABJAAEkASQAJIAAkgCSABJIDUvTXqtB9v78f/8OPzEx+jAV3F5chfR2oEoBvdHPmXSQoE9H9uSEoGdKYbkqIA3ZfOjw8zkNFCB6NBgLalM5bRQgejWyp6PHnyBgp4AbJXUdHjLIEbKHLckauo6HG6KEDxt86wM5bJOmkIoDl6ks5bpunU7QHN1JNx9jJBE2gMiJ7ucyhTY6glIHoyZuLnwtQQkPUTM5kyI4Y6AaInbEruQOoDyPrJm5UNpCaArJ/IidlA6gDI+kmdmw2k7QFZP8HTs4G0NyDrJ3uGNpA2BmT9xE/SBhJAigTk/WvCPG0gASSABJAL0MRrkA0kgASQABJAbtDu0TaQABJAAkgCSAAJIAEkASSABJAAkgASQAJIAH0r9Xek79Bus7WBBJAAEkACyD3aDdoGEkACSAC5BrkA2UACSGMBeReLn6QNpL0BWULZM7SBtD0gSyh4ejaQOgCyhFLnZgOpCSBLKHJiNpD6ALKE8mZlA6kVIEsobEplOvT0ewtjKGYy7kDqCcgSyphJmRc9jd/CGOo+hzI7E2h/iZ5sqPvZyxydOuS/8dMMZZy3zNRJcwANMZR0xjJfp7ultfOUw377c+QHRpm4swRuoKRVlP2OXF4DT568gfquoiGfklgdP6A3ZzTqM6Kr40Nvy2jgF2RW30ffitHYrwev7gf4euXuIsn3oqyYk5wpiZtAQCdI4mYEoF9e76tI4TIdEBMn5OfCBJAAEkACSAJIAAkgASQBJIAEkACSABJAAkgASQAJIAEkgASQBJAAEkACSAJIAAkgASQBJIAEkACSABJAAkjt+wRy2e+9XVOp1AAAAABJRU5ErkJggg=="
ICON_180_B64 = "iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAIAAACyr5FlAAACsklEQVR42u3dy00kQRBFUUjFEnzCAgzBSnwCA9ghhADRqEXGizx3PTNURp+JquYjbm+eHm+kr1pGIDgEh+AQHIJDcAgOwSE4BIcEh+AQHIJDcAgOwSE4BIfgkOAQHIJDcAgOwSE4BIfgEBwSHIJDcAgO/Uc1+3ivzy+//8N3D/dATMZxkYaf/y4rdTiIX/6zZ0IpJi76WEcpKSz+8KEPIVJYIBKPowmLo4gUFohE4mjOYjyRRcbh15yHI3fKk3yU4brFZGyOSf/tBpxlmaYTdccxT8aAcy0TdLq+OGbLiD7jMjUn7YjjHBmh510m5dS9cJwpI+7sy3RMoPVbWcFhbYTNYZkIH/txkBE3E88c2o3D2kicjM2hrTisjdD52Bzah8PayJ2SzaFNOKyN6FnZHNqBw9pIn5jNITjUB4d7yoC52RyCQ3CoOw4PHDOmZ3MIDsEhOJSJw9PomBnaHIJDcAgOwSE4BIfgEByCQ3BIcAgOwaEsHGf+2veRM7Q5BIfgEByKxeGZdMb0bA7BITgUgMNjx4C52RyCQ61wuLOkT8zm0CYclkf0rGwO7cNheeROyebQVhyWR+h8bA7txmF5JE7G5lADHJZH3EyWWZDR5bbCR9AcPHOoEw7LI2UCy3Scvd1t5UwfWadeJuW8TR9Iz/GReNJlas7Y+q3sbB+5p1sm6FzdcYz0kX6iZZrO8l3Vc6bRvwtsDPFlvq48DEfolIc9NlX/WUfcYka+26qUubclMviTNJVyoQ2JjP/cbmVdbhMih3xJqBIveiORo76SXLmX/v46/YOSM7/7pAac4eMrd0Uovp2xhp3n0yt6kRUahuPwel8xP5ogOASH4BAcgkNwCA7BITgEhwSH4BAcgkNwCA7BITgEh+CQ4BAcgkNwCA7BITgEh+AQHBIcgkNw6Jq9ASg/4KF1FbK/AAAAAElFTkSuQmCC"
ICON_512_B64 = "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"


@app.get("/manifest.json")
def manifest():
    return MANIFEST_JSON, 200, {"Content-Type": "application/manifest+json; charset=utf-8"}


@app.get("/sw.js")
def service_worker():
    return SW_JS, 200, {"Content-Type": "application/javascript; charset=utf-8",
                        "Cache-Control": "no-cache"}


def _icon(b64):
    import base64
    return base64.b64decode(b64), 200, {"Content-Type": "image/png",
                                        "Cache-Control": "public, max-age=604800"}


@app.get("/icon-192.png")
def icon_192():
    return _icon(ICON_192_B64)


@app.get("/icon-180.png")
def icon_180():
    return _icon(ICON_180_B64)


@app.get("/icon-512.png")
def icon_512():
    return _icon(ICON_512_B64)


@app.get("/health")
def health():
    return jsonify({"ok": True, "app": APP_NAME, "auth": AUTH_REQUIRED,
                    "credits": CREDITS_ON,
                    "keys": {"gemini": bool(GEMINI_API_KEY), "groq": bool(GROQ_API_KEY)}})


def authed_user(req):
    """Return user dict; 'DEV' user when auth disabled; None when invalid."""
    if not AUTH_REQUIRED:
        return {"uid": "dev", "email": "", "name": ""}
    data = req.get_json(force=True, silent=True) or {}
    return verify_id_token(data.get("idToken", ""))


@app.post("/me")
def me():
    user = authed_user(request)
    if user is None:
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"email": user["email"], "remaining": remaining_map(user["uid"])})


@app.post("/chat")
def chat():
    data = request.get_json(force=True, silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"reply": "আগে একটা প্রশ্ন লেখো তো! 😊"})
    user = authed_user(request)
    if user is None:
        return jsonify({"reply": "আগে Google দিয়ে লগইন করো, তারপর চ্যাট করা যাবে।"}), 401
    model_key = data.get("model") or DEFAULT_MODEL
    if model_key not in MODELS:
        model_key = DEFAULT_MODEL
    if CREDITS_ON:
        used = credits_used(user["uid"], model_key)
        if used >= MODELS[model_key]["cap"]:
            return jsonify({
                "reply": f"এই মডেলে ({MODELS[model_key]['name']}) এই সপ্তাহের ফ্রি মেসেজ শেষ! "
                         "সামনের সপ্তাহে আবার পাবে — ততক্ষণ অন্য মডেলটা ব্যবহার করে দেখো।",
                "remaining": remaining_map(user["uid"]),
            }), 429
    try:
        reply = respond(message, data.get("history") or [], model_key)
    except Exception as e:  # noqa: BLE001 — details stay in the server log only
        print("LLM error:", e, flush=True)
        return jsonify({"reply": GENERIC_ERROR, "remaining": remaining_map(user["uid"])})
    if CREDITS_ON:
        credits_consume(user["uid"], model_key)
    return jsonify({"reply": reply, "remaining": remaining_map(user["uid"])})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
