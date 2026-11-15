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
            os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite"),
            "gemini-2.0-flash-lite",
            "gemini-2.5-flash",
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
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__APP_NAME__ — বাংলাদেশের ফ্রি AI</title>
<style>
  :root{--cream:#f7f2e7;--brick:#9e3a2e;--brick2:#7c2c22;--ink:#38352f;--card:#fffdf8;--line:#e5dcc8}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Hind Siliguri','Noto Sans Bengali',system-ui,sans-serif;background:var(--cream);color:var(--ink);
       display:flex;flex-direction:column;height:100dvh}
  header{background:var(--card);border-bottom:2px solid var(--line);padding:12px 18px;text-align:center}
  header h1{color:var(--brick);font-size:clamp(22px,4.5vw,32px)}
  header p{font-size:clamp(12px,2.6vw,14.5px);opacity:.85;margin-top:2px}
  #bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between;
       padding:8px 14px;background:var(--card);border-bottom:1px solid var(--line);
       width:100%;max-width:860px;margin:0 auto}
  #bar select{padding:8px 10px;border:1.5px solid var(--brick);border-radius:10px;background:#fff;
       color:var(--brick);font-family:inherit;font-size:14.5px;font-weight:600}
  #remain{font-size:13px;opacity:.85}
  #userbox{font-size:13.5px;display:flex;gap:8px;align-items:center}
  #userbox button{background:none;border:1.5px solid var(--line);border-radius:8px;padding:5px 10px;
       cursor:pointer;font-family:inherit;color:var(--ink)}
  #chat{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px;
        width:100%;max-width:860px;margin:0 auto}
  .msg{max-width:85%;padding:10px 14px;border-radius:16px;line-height:1.55;white-space:pre-wrap;word-break:break-word;
       box-shadow:0 1px 3px rgba(60,40,20,.08);font-size:15.5px}
  .u{align-self:flex-end;background:var(--brick);color:#fff;border-bottom-right-radius:4px}
  .a{align-self:flex-start;background:var(--card);border:1px solid var(--line);border-bottom-left-radius:4px}
  .chips{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;padding:4px 16px 8px;max-width:860px;margin:0 auto;width:100%}
  .chip{background:var(--card);border:1.5px solid var(--brick);color:var(--brick);border-radius:999px;
        padding:6px 12px;font-size:13.5px;cursor:pointer;font-family:inherit}
  .chip:hover{background:var(--brick);color:#fff}
  form{display:flex;gap:8px;padding:12px 16px;background:var(--card);border-top:2px solid var(--line);
       width:100%;max-width:860px;margin:0 auto}
  input{flex:1;padding:12px 14px;border:1.5px solid var(--line);border-radius:12px;font-size:16px;font-family:inherit;background:#fff}
  input:focus{outline:none;border-color:var(--brick)}
  button.send{background:var(--brick);color:#fff;border:none;border-radius:12px;padding:12px 20px;
        font-size:16px;cursor:pointer;font-family:inherit}
  button.send:hover{background:var(--brick2)}
  button.send:disabled{opacity:.5}
  .dots span{animation:b 1.2s infinite;display:inline-block}
  .dots span:nth-child(2){animation-delay:.15s}.dots span:nth-child(3){animation-delay:.3s}
  @keyframes b{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-4px)}}
  #login{position:fixed;inset:0;background:rgba(56,53,47,.55);display:none;align-items:center;justify-content:center;z-index:10;padding:16px}
  #login .card{background:var(--card);border-radius:18px;padding:28px 24px;max-width:380px;width:100%;text-align:center;
       box-shadow:0 10px 40px rgba(0,0,0,.25)}
  #login h2{color:var(--brick);margin-bottom:8px}
  #login p{font-size:14.5px;line-height:1.6;margin-bottom:18px}
  #gbtn{background:#fff;border:1.5px solid var(--line);border-radius:12px;padding:12px 18px;font-size:16px;
       cursor:pointer;font-family:inherit;display:inline-flex;gap:10px;align-items:center}
  #gbtn:hover{border-color:var(--brick)}
</style>
</head>
<body>
<header>
  <h1>🇧🇩 __APP_NAME__</h1>
  <p>বাংলাদেশকে সবচেয়ে বেশি গুরুত্ব দেওয়া AI — যা জানে নিজে বলে, যা জানে না অনলাইন থেকে খুঁজে এনে উত্তর দেয়।<br>
  <i>সম্পূর্ণ ফ্রি প্রযুক্তিতে তৈরি — কোনো কোটি টাকার প্রকল্প নয়।</i> 😉</p>
</header>
<div id="bar">
  <select id="model"></select>
  <span id="remain"></span>
  <span id="userbox"></span>
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
  <input id="q" placeholder="এখানে প্রশ্ন লেখো…" autocomplete="off">
  <button class="send" id="send" type="submit">পাঠাও</button>
</form>

<div id="login">
  <div class="card">
    <h2>🇧🇩 Chat Bash-এ স্বাগতম</h2>
    <p>চ্যাট শুরু করতে Google দিয়ে লগইন করো। প্রতি সপ্তাহে প্রতি মডেলে নির্দিষ্ট সংখ্যক ফ্রি মেসেজ পাবে — লগইন থাকলে হিসাবটা ঠিক থাকে, আর ভুয়া/টেম্প মেইলের ঝামেলাও থাকে না।</p>
    <button id="gbtn" type="button"><b style="color:#4285F4">G</b> Google দিয়ে লগইন</button>
    <p id="lerr" style="color:#9e3a2e;margin-top:12px"></p>
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
