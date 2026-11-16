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
  UPSTASH_REDIS_REST_URL / _TOKEN       enables two-day allowance counting
  APP_NAME                              display name (default: Chat Bash)
If FIREBASE_PROJECT_ID is unset the app runs in DEV mode (no login needed)
— local testing only.
"""
import base64
import datetime
import hashlib
import hmac
import json
import os
import secrets
import time

import requests
from flask import Flask, jsonify, make_response, request

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
6. উত্তরে কখনো কোনো ইমোজি ব্যবহার করবে না — শুধু লেখা, প্রয়োজনে মার্কডাউন।
7. উত্তর সহজ, প্রাণবন্ত ও কাজের রাখো — সাধারণত ১৫০ শব্দের মধ্যে। প্রয়োজনে পয়েন্ট আকারে দাও।
8. পরিচয় (সবচেয়ে কড়া নিয়ম): তুমি শুধুই "{APP_NAME}" — নাশিদের বানানো, সম্পূর্ণ ফ্রি প্রযুক্তিতে চলা বাংলাদেশের নিজের AI। কেউ জিজ্ঞেস করলেও তুমি কোন মডেল, কোন কোম্পানি বা কোন API দিয়ে চলো তা কখনোই বলবে না — Gemini, Google, Groq, Llama, OpenAI বা অন্য কোনো মডেল/কোম্পানির নাম মুখেও আনবে না। জিজ্ঞেস করলে বলবে: "আমি {APP_NAME} — সম্পূর্ণ ফ্রি প্রযুক্তিতে বানানো বাংলাদেশের নিজের AI।\""""

LIVE_HINTS = [
    "আজ", "এখন", "সর্বশেষ", "সাম্প্রতিক", "খবর", "দাম", "আবহাওয়া", "স্কোর",
    "জিতল", "হারল", "ফলাফল", "বর্তমান", "২০২৫", "২০২৬", "2025", "2026",
    "today", "now", "latest", "price", "news", "weather", "score",
]

GENERIC_ERROR = "দুঃখিত, এই মুহূর্তে উত্তরটা তৈরি করা যাচ্ছে না। একটু পরে আবার চেষ্টা করো।"
CANNED = {
    "en": {
        "empty": "Type a question first!",
        "login_first": "Log in with Google first — then you can chat.",
        "generic": "Sorry, I couldn't prepare the answer right now. Please try again in a little while.",
        "session_over": "Your 10 minutes of chatting for this hour are over! Take a short break so everyone gets a chance — you can chat again in a little while.",
        "limit": "This model's ({name}) free messages for this round are over! You'll get a fresh limit in two days — try another model meanwhile.",
    },
    "bn": {
        "empty": "আগে একটা প্রশ্ন লেখো তো!",
        "login_first": "আগে Google দিয়ে লগইন করো, তারপর চ্যাট করা যাবে।",
        "generic": GENERIC_ERROR,
        "session_over": "এই ঘণ্টার ১০ মিনিট চ্যাটিং শেষ! সবার সুযোগের জন্য একটু বিরতি নাও — কিছুক্ষণ পর আবার কথা বলা যাবে।",
        "limit": "এই মডেলে ({name}) এই পর্বের ফ্রি মেসেজ শেষ! দুই দিন পর আবার নতুন লিমিট পাবে — ততক্ষণ অন্য মডেলটা ব্যবহার করে দেখো।",
    },
}


def canned(lang, key, **kw):
    txt = CANNED.get(lang, CANNED["en"])[key]
    return txt.format(**kw) if kw else txt

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

# ---------------- two-day allowance (Upstash Redis REST) ------------------

def cycle_key():
    # the free allowance renews every 2 days (fixed 48h cycles)
    return str(int(time.time()) // (2 * 24 * 3600))


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
        val = _redis("GET", f"cb:{uid}:{model_key}:{cycle_key()}")
        return int(val) if val is not None else 0
    except Exception as e:  # noqa: BLE001
        print("credits read failed:", e, flush=True)
        return 0


def credits_consume(uid, model_key):
    if not CREDITS_ON:
        return
    try:
        key = f"cb:{uid}:{model_key}:{cycle_key()}"
        used = _redis("INCR", key)
        if used == 1:
            _redis("EXPIRE", key, str(3 * 24 * 3600))
    except Exception as e:  # noqa: BLE001
        print("credits write failed:", e, flush=True)


HISTORY_ON = CREDITS_ON  # same Redis store as the credit counters


def history_load(uid):
    if not HISTORY_ON:
        return []
    try:
        raw = _redis("GET", f"cbhist:{uid}")
        if not raw:
            return []
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception as e:  # noqa: BLE001
        print("history load failed:", e, flush=True)
        return []


def history_save(uid, chats):
    if not HISTORY_ON:
        return False
    try:
        clean = []
        for c in (chats or [])[:30]:
            if not isinstance(c, dict):
                continue
            pairs = []
            for p in (c.get("pairs") or [])[:60]:
                if not isinstance(p, dict):
                    continue
                pairs.append({
                    "u": str(p.get("u", ""))[:4000],
                    "a": str(p.get("a", ""))[:8000],
                    "f": p.get("f") if p.get("f") in ("up", "down") else "",
                })
            clean.append({"id": str(c.get("id", ""))[:40],
                          "ts": int(c.get("ts") or 0), "pairs": pairs})
        payload = json.dumps(clean, ensure_ascii=False)
        while clean and len(payload) > 400_000:
            clean.pop()
            payload = json.dumps(clean, ensure_ascii=False)
        _redis("SET", f"cbhist:{uid}", payload)
        return True
    except Exception as e:  # noqa: BLE001
        print("history save failed:", e, flush=True)
        return False

# ---------------- capacity: 10 users at a time, 10 min of chat per hour ----------------
MAX_ACTIVE = 10          # simultaneous users inside the app
PRES_TTL = 90            # a presence slot lives this long without a heartbeat
SESSION_LEN = 10 * 60    # chatting window per hour
SESSION_GAP = 60 * 60    # a fresh window opens 1h after the previous one started


def presence_count():
    n = 0
    cur = "0"
    while True:
        res = _redis("SCAN", cur, "MATCH", "cbpres:*", "COUNT", "100")
        cur = str(res[0])
        n += len(res[1])
        if cur == "0":
            return n


def presence_admit(uid):
    """Take or renew the user's presence slot; True when they may be inside.
    Fails open when Redis is unavailable, so an outage never locks everyone out."""
    if not HISTORY_ON:
        return True
    try:
        if _redis("GET", f"cbpres:{uid}"):
            _redis("EXPIRE", f"cbpres:{uid}", str(PRES_TTL))
            return True
        if presence_count() >= MAX_ACTIVE:
            return False
        _redis("SET", f"cbpres:{uid}", "1")
        _redis("EXPIRE", f"cbpres:{uid}", str(PRES_TTL))
        return True
    except Exception as e:  # noqa: BLE001
        print("presence failed:", e, flush=True)
        return True


def presence_release(uid):
    if not HISTORY_ON:
        return
    try:
        _redis("DEL", f"cbpres:{uid}")
    except Exception:  # noqa: BLE001
        pass


def session_state(uid):
    """(state, resume_ts): 'new' = no window yet, 'open' = inside the 10-minute
    window, 'over' = window used up; resume_ts = epoch seconds when a fresh
    window opens (only meaningful for 'over')."""
    if not HISTORY_ON:
        return "new", 0
    try:
        raw = _redis("GET", f"cbsess:{uid}")
        if not raw:
            return "new", 0
        start = int(raw)
        now = int(time.time())
        if now - start >= SESSION_GAP:
            return "new", 0
        if now - start >= SESSION_LEN:
            return "over", start + SESSION_GAP
        return "open", 0
    except Exception as e:  # noqa: BLE001
        print("session read failed:", e, flush=True)
        return "new", 0


def session_touch(uid):
    """Start the user's hourly window on their first message.
    Returns (allowed, resume_ts)."""
    state, resume = session_state(uid)
    if state == "over":
        return False, resume
    if state == "new" and HISTORY_ON:
        try:
            _redis("SET", f"cbsess:{uid}", str(int(time.time())))
            _redis("EXPIRE", f"cbsess:{uid}", str(SESSION_GAP + 600))
        except Exception as e:  # noqa: BLE001
            print("session start failed:", e, flush=True)
    return True, 0


def remaining_map(uid, owner=False):
    if owner:
        return {key: None for key in MODELS}
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
    refreshed = False
    work = list(dict.fromkeys(chain))
    i = 0
    while i < len(work):
        model = work[i]
        i += 1
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
            if resp.status_code == 404 and not refreshed:
                # A listed model vanished — re-detect what the key can use, once.
                refreshed = True
                fresh = detect_gemini_chain(force=True)
                if fresh:
                    work = list(dict.fromkeys(fresh))
                    i = 0
        except Exception as e:  # noqa: BLE001
            last = f"gemini {model}: {e}"
    raise RuntimeError(last or "gemini failed")


# ------- auto-detect: ask the API which models this key can actually use -------

_GEMINI_MODELS_CACHE = {"at": 0.0, "chain": None}
_MODELS_TTL = 6 * 3600


def _model_rank(name):
    """Lower sorts first; None excludes non-chat models (TTS/image/audio/...)."""
    import re

    n = name.lower()
    if any(x in n for x in ("tts", "image", "audio", "embedding", "aqa",
                            "robotics", "computer-use", "video", "music")):
        return None
    ver = 0.0
    mv = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
    if mv:
        try:
            ver = float(mv.group(1))
        except ValueError:
            pass
    if "flash" in n and "lite" in n:
        tier = 0
    elif "flash" in n:
        tier = 1
    elif "pro" in n:
        tier = 2
    else:
        tier = 3
    return (tier, -ver)


def detect_gemini_chain(force=False):
    """Live model list for the key, ranked Flash-Lite first. Cached 6h;
    falls back to the static chain when the list call fails."""
    import time

    now = time.time()
    if (not force and _GEMINI_MODELS_CACHE["chain"]
            and now - _GEMINI_MODELS_CACHE["at"] < _MODELS_TTL):
        return _GEMINI_MODELS_CACHE["chain"]
    try:
        resp = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": GEMINI_API_KEY},
            timeout=20,
        )
        if resp.status_code == 200:
            ranked = []
            for mdl in resp.json().get("models", []):
                if "generateContent" not in mdl.get("supportedGenerationMethods", []):
                    continue
                short = mdl.get("name", "").removeprefix("models/")
                rank = _model_rank(short)
                if rank is not None:
                    ranked.append((rank, short))
            ranked.sort()
            chain = [s for _, s in ranked[:4]]
            if chain:
                _GEMINI_MODELS_CACHE.update(at=now, chain=chain)
                return chain
    except Exception as e:  # noqa: BLE001
        print("model detect failed:", e, flush=True)
    return _GEMINI_MODELS_CACHE["chain"] or MODELS["lite"]["chain"]


def llm(messages, model_key):
    m = MODELS[model_key]
    if m["provider"] == "gemini":
        return gemini_llm(messages, detect_gemini_chain())
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
<html lang="en">
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
  :root{--bg:#2a0509;--panel:#471216;--bubble:#7a2027;--menu:#521419;--text:#f9efe4;--muted:#e2b3a9;
        --accent:#eebd55;--accent-ink:#331a02;--line:#6d3238}
  :root{color-scheme:dark}
  [data-theme="cream"]{color-scheme:light;--bg:#f7f2e7;--panel:#fffdf8;--bubble:#f3e9d7;--menu:#fffdf8;--text:#2b2320;
        --muted:#8a766a;--accent:#9e3a2e;--accent-ink:#fdf8ee;--line:#e7dcc8}
  [data-theme="cream"] body{background:#f7f2e7}
  [data-theme="cream"] .u{background:#9e3a2e;color:#fdf8ee}
  [data-theme="cream"] #bamboo{opacity:.13}
  [data-theme="cream"] #demo{background:#f6e7c8;color:#8a5a12;border-top-color:#e3cd97;border-bottom-color:#e3cd97}
  [data-theme="cream"] #login .card{background:linear-gradient(170deg,#fffdf8,#f7ecd9);border-color:#e7dcc8}
  [data-theme="cream"] .sm-card{background:linear-gradient(170deg,#fffdf8,#f7ecd9);border-color:#e7dcc8}
  [data-theme="cream"] #drawer .avatar{color:#fdf8ee}
  [data-theme="cream"] form{box-shadow:0 10px 30px rgba(120,70,40,.14)}
  [data-theme="green"]{--bg:#051b12;--panel:#0a3524;--bubble:#12573c;--menu:#0d4530;--text:#eef7ee;
        --muted:#a8cbb4;--accent:#ffd166;--accent-ink:#231a02;--line:#1f7a52}
  [data-theme="green"] body{background:linear-gradient(170deg,#0d4530,#07271b 60%,#051b12)}
  [data-theme="green"] .u{background:#d42a24;color:#fff}
  [data-theme="green"] #bamboo{opacity:.35}
  [data-theme="green"] #demo{background:rgba(4,32,22,.85);color:#ffd166;border-top-color:#1f7a52;border-bottom-color:#1f7a52}
  [data-theme="green"] #login .card{background:linear-gradient(170deg,#0d4530,#07271b);border-color:#1f7a52}
  [data-theme="green"] .sm-card{background:linear-gradient(170deg,#0d4530,#07271b);border-color:#1f7a52}
  [data-theme="green"] #send{background:#d42a24;color:#fff}
  /* ---------- theme menu ---------- */
  #thememenu,#langmenu{position:fixed;left:14px;bottom:78px;background:var(--menu);border:1px solid var(--line);border-radius:16px;
            padding:6px;display:none;z-index:40;box-shadow:0 12px 34px rgba(0,0,0,.45);min-width:204px}
  #thememenu.open,#langmenu.open{display:block;animation:popIn .16s ease}
  #thememenu button,#langmenu button{display:flex;gap:10px;align-items:center;width:100%;background:none;border:none;color:var(--text);
            font-family:inherit;font-size:14.5px;padding:8px 10px 10px;border-radius:11px;cursor:pointer;text-align:left;line-height:1.3}
  #thememenu button:hover,#langmenu button:hover{background:var(--bubble)}
  #thememenu .sw{width:18px;height:18px;border-radius:50%;flex:none;border:1px solid var(--line)}
  #thememenu .tk,#langmenu .tk{margin-left:auto;width:8px;height:8px;border-radius:50%;flex:none}
  #thememenu button.on .tk,#langmenu button.on .tk{background:var(--accent)}
  #langmenu .swtxt{width:26px;font-size:11.5px;font-weight:700;color:var(--accent);flex:none;letter-spacing:.02em}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Hind Siliguri','Noto Sans Bengali',system-ui,sans-serif;background:linear-gradient(168deg,#5c1216 0%,#38070d 55%,#260509 100%);color:var(--text);
       display:flex;flex-direction:column;height:100dvh;overflow:hidden}
  svg{display:block}
  #bamboo{position:fixed;top:0;right:0;height:100dvh;width:min(340px,62vw);z-index:0;pointer-events:none;
          opacity:.6;filter:blur(1.1px) saturate(.72);
          -webkit-mask-image:linear-gradient(to left,#000 52%,transparent 97%);
          mask-image:linear-gradient(to left,#000 52%,transparent 97%)}
  #bamboo svg{width:100%;height:100%}
  #demo,#chat,#f,.fineprint{position:relative;z-index:1}
  .icobtn{background:none;border:none;border-radius:50%;width:40px;height:40px;flex:0 0 40px;
          display:flex;align-items:center;justify-content:center;cursor:pointer;color:var(--text)}
  .icobtn:hover{background:var(--bubble)}
  .icobtn svg{width:22px;height:22px}
  /* ---------- top bar ---------- */
  /* ---------- touch polish: no tap flash, no stray text selection ---------- */
  *{-webkit-tap-highlight-color:transparent}
  button{outline:none}
  body{-webkit-user-select:none;user-select:none;-webkit-touch-callout:none}
  img{-webkit-user-drag:none}
  #chat,#chat .msg{-webkit-user-select:text;user-select:text;-webkit-touch-callout:default}
  input,textarea{-webkit-user-select:text;user-select:text;-webkit-touch-callout:default}
  /* ---------- motion ---------- */
  @keyframes popIn{from{opacity:0;transform:translateY(-5px) scale(.98)}to{opacity:1;transform:none}}
  @keyframes riseIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
  @keyframes fadeIn{from{opacity:0}to{opacity:1}}
  #modelMenu{animation:popIn .16s ease}
  #acct.open{animation:popIn .16s ease}
  #plusmenu.open{animation:popIn .16s ease}
  #limitcard{animation:riseIn .25s ease}
  #login .card{animation:riseIn .3s ease}
  .msg{animation:fadeIn .25s ease}
  .icobtn{transition:transform .12s ease}
  .icobtn:active{transform:scale(.88)}
  @media (prefers-reduced-motion: reduce){ *,*::before,*::after{animation-duration:.01ms !important;transition-duration:.01ms !important} }
  header{display:flex;align-items:center;gap:4px;padding:8px 10px;width:100%;max-width:900px;margin:0 auto;position:relative;z-index:15}
  .modelwrap{display:flex;align-items:center;gap:2px;min-width:0;margin-right:auto;position:relative}
  #modelBtn{display:flex;align-items:center;gap:5px;background:none;border:none;color:var(--text);
         font-family:inherit;font-size:17px;font-weight:600;padding:8px 2px;cursor:pointer;max-width:46vw;line-height:1.3}
  #modelMenu{position:absolute;top:calc(100% + 6px);left:0;min-width:238px;background:var(--menu);
         border:1px solid var(--line);border-radius:16px;box-shadow:0 18px 44px rgba(0,0,0,.55);padding:6px;z-index:46}
  .mopt{display:flex;align-items:center;gap:10px;width:100%;background:none;border:none;color:var(--text);
         font-family:inherit;text-align:left;padding:10px;border-radius:11px;cursor:pointer}
  .mopt:hover{background:var(--bubble)}
  .mopt .mn{display:block;font-size:15.5px;font-weight:600}
  .mopt .md{display:block;font-size:12px;color:var(--muted);margin-top:1px}
  .mopt .radio{margin-left:auto;width:17px;height:17px;border:1.8px solid var(--muted);border-radius:50%;flex:none;position:relative}
  .mopt.on .radio{border-color:var(--accent)}
  .mopt.on .radio::after{content:'';position:absolute;inset:3.5px;background:var(--accent);border-radius:50%}
  .chev{color:var(--muted);pointer-events:none}
  /* ---------- account dropdown ---------- */
  #acct{position:absolute;top:54px;right:10px;background:var(--menu);border:1px solid var(--line);border-radius:16px;
        padding:14px 16px;width:min(300px,86vw);box-shadow:0 12px 34px rgba(0,0,0,.55);display:none;z-index:40}
  #acct.open{display:block}
  #acct .who{font-size:14.5px;font-weight:600;margin-bottom:2px;word-break:break-all}
  #acct .sub{font-size:12.5px;color:var(--muted);margin-bottom:10px}
  #acct .remline{display:flex;justify-content:space-between;align-items:center;font-size:13.5px;padding:5px 0;border-top:1px solid var(--line);line-height:1.35}
  #acct .note{font-size:12px;color:var(--muted);margin-top:10px;line-height:1.5;display:flex;gap:6px;align-items:flex-start}
  #acct .note svg{flex:none;margin-top:2px}
  #logout{margin-top:12px;width:100%;background:var(--bubble);color:var(--text);border:1px solid var(--line);
          border-radius:999px;padding:8px 9px 10px;font-family:inherit;font-size:14px;cursor:pointer;line-height:1.3}
  /* ---------- demo strip ---------- */
  #demo{background:rgba(61,13,17,.82);color:#f5c04e;border-top:1px solid #6e2a2e;border-bottom:1px solid #6e2a2e;
        display:flex;align-items:center;justify-content:center;gap:7px;text-align:center;font-size:12.5px;line-height:1.45;padding:6px 14px}
  #demo svg{flex:none}
  /* ---------- chat ---------- */
  #chat{flex:1;overflow-y:auto;padding:18px 16px 8px;display:flex;flex-direction:column;gap:22px;
        width:100%;max-width:900px;margin:0 auto}
  .msg{line-height:1.7;word-break:break-word;font-size:16px}
  .u{white-space:pre-wrap}
  .a .txt p{margin:0 0 8px}
  .a .txt p:last-child{margin-bottom:0}
  .a .txt ul,.a .txt ol{margin:4px 0 8px;padding-left:22px}
  .a .txt li{margin:2px 0}
  .a .txt h4{font-size:16.5px;margin:10px 0 4px}
  .a .txt code{background:var(--bubble);padding:1px 5px;border-radius:6px;font-size:14px}
  .u{align-self:flex-end;background:var(--bubble);color:var(--text);max-width:82%;
     padding:11px 17px;border-radius:26px}
  .a{align-self:stretch;padding:2px}
  .acts{display:flex;gap:2px;margin-top:10px;flex-wrap:wrap}
  .acts .icobtn{width:34px;height:34px;flex-basis:34px;opacity:.85}
  .acts .icobtn svg{width:19px;height:19px}
  .acts .icobtn.on{color:var(--accent);opacity:1}
  .acts .icobtn:disabled{opacity:.3;cursor:default}
  .dots span{animation:b 1.2s infinite;display:inline-block}
  .dots span:nth-child(2){animation-delay:.15s}.dots span:nth-child(3){animation-delay:.3s}
  @keyframes b{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-4px)}}
  /* ---------- chips ---------- */
  .chips{display:flex;flex-wrap:wrap;gap:8px;padding:6px 16px 2px;max-width:900px;margin:0 auto;width:100%}
  .chip{background:transparent;border:1px solid var(--line);color:var(--text);border-radius:999px;
        padding:7px 14px 9px;font-size:13.5px;cursor:pointer;font-family:inherit;line-height:1.3;display:inline-flex;align-items:center}
  .chip:hover{background:var(--panel)}
  .mopt .soon{margin-left:auto;flex:none;font-size:11px;font-weight:600;color:var(--accent);
       border:1px solid var(--accent);border-radius:999px;padding:3px 8px;opacity:.92}
  /* ---------- soon modal ---------- */
  #soonmodal,#fullmodal,#breakmodal,#infomodal,#maintmodal{position:fixed;inset:0;background:rgba(10,2,3,.62);backdrop-filter:blur(3px);
       display:flex;align-items:center;justify-content:center;z-index:70;padding:18px}
  #soonmodal[hidden],#fullmodal[hidden],#breakmodal[hidden],#infomodal[hidden],#maintmodal[hidden]{display:none}
  .sm-card{background:linear-gradient(170deg,#521419,#3d0d11);border:1px solid #83413d;border-radius:22px;
       padding:22px 20px;width:min(340px,88vw);box-shadow:0 24px 60px rgba(0,0,0,.6);animation:riseIn .25s ease;text-align:center}
  .sm-ic{width:52px;height:52px;margin:0 auto 10px;border-radius:50%;background:rgba(238,189,85,.14);
       border:1px solid var(--line);display:flex;align-items:center;justify-content:center;color:var(--accent)}
  .sm-ic svg{width:26px;height:26px}
  .sm-t{font-size:17px;font-weight:700}
  .sm-b{font-size:14px;line-height:1.65;margin:10px 0 16px}
  #soonOk,#fullRetry,#breakOk,#infoOk,#maintRetry{width:100%;background:var(--bubble);color:var(--text);border:1px solid var(--line);
       border-radius:999px;padding:10px 11px 12px;font-family:inherit;font-size:15px;cursor:pointer;line-height:1.3;display:flex;align-items:center;justify-content:center}
  /* ---------- limit card ---------- */
  #limitcard{background:var(--panel);border:1px solid var(--line);border-radius:18px;
       padding:14px 16px;width:calc(100% - 24px);max-width:876px;margin:10px auto 0;position:relative;z-index:1}
  #limitcard[hidden]{display:none}
  #limitcard .lt{font-size:15.5px;font-weight:600}
  #limitcard .ls{font-size:13px;color:var(--muted);margin-top:3px;line-height:1.55}
  #limitBtn{margin-top:12px;width:100%;background:var(--bubble);color:var(--text);border:1px solid var(--line);
       border-radius:999px;padding:9px 10px 11px;font-family:inherit;font-size:14.5px;cursor:pointer;line-height:1.3;display:flex;align-items:center;justify-content:center}
  #limitBtn:disabled{opacity:.55;cursor:default}
  /* ---------- input pill ---------- */
  form{display:flex;align-items:center;gap:2px;background:var(--panel);border-radius:28px;
       padding:5px;width:calc(100% - 24px);max-width:876px;margin:10px auto 4px}
  #q{flex:1;padding:10px 6px;border:none;font-size:16px;font-family:inherit;background:transparent;color:var(--text);min-width:0;line-height:1.35}
  #q:focus{outline:none}
  #q::placeholder{color:var(--muted)}
  #send{background:var(--accent);color:var(--accent-ink);border:none;border-radius:50%;
        width:40px;height:40px;flex:0 0 40px;cursor:pointer;display:none;align-items:center;justify-content:center}
  #send svg{width:20px;height:20px}
  #send:disabled{opacity:.35}
  #mic.rec{color:#f28b82;background:#3a2320}
  .fineprint{text-align:center;color:var(--muted);font-size:11.5px;padding:2px 16px 10px}
  /* ---------- plus menu ---------- */
  #plusmenu{position:fixed;left:14px;bottom:86px;background:var(--menu);border:1px solid var(--line);border-radius:16px;
            padding:6px;display:none;z-index:40;box-shadow:0 12px 34px rgba(0,0,0,.55);min-width:210px}
  #plusmenu.open{display:block}
  #plusmenu button svg{width:17px;height:17px;flex:none}
  #demo svg{width:13px;height:13px;vertical-align:-1.5px}
  .note svg{width:13px;height:13px;vertical-align:-1.5px}
  #newchat svg{width:15px;height:15px;vertical-align:-2.5px;margin-right:5px}
  .dhead b svg{width:19px;height:19px;vertical-align:-4px;margin-right:5px}
  #plusmenu button{display:flex;gap:10px;align-items:center;width:100%;background:none;border:none;color:var(--text);
                   font-family:inherit;font-size:14.5px;padding:9px 12px 11px;border-radius:10px;cursor:pointer;text-align:left;line-height:1.3}
  #plusmenu button:hover{background:var(--bubble)}
  /* ---------- drawer / history ---------- */
  #scrim{position:fixed;inset:0;background:rgba(0,0,0,.55);opacity:0;pointer-events:none;transition:opacity .25s;z-index:25}
  #scrim.on{opacity:1;pointer-events:auto}
  #drawer{position:fixed;top:0;left:0;bottom:0;width:min(302px,84vw);background:var(--panel);z-index:30;
          transform:translateX(-105%);transition:transform .28s ease;display:flex;flex-direction:column;
          border-right:1px solid var(--line)}
  #drawer.on{transform:translateX(0)}
  #drawer .dhead{padding:14px 14px 8px;display:flex;align-items:center;justify-content:space-between}
  #drawer .dhead b{font-size:16.5px;display:flex;align-items:center;gap:9px}
  #drawer .dlogo{width:27px;height:27px;border-radius:8px;flex:none}
  #newchat{display:flex;align-items:center;justify-content:center;gap:8px;margin:4px 14px 10px;background:var(--bubble);color:var(--text);border:1px solid var(--line);
           border-radius:999px;padding:9px 10px 11px;font-family:inherit;font-size:14.5px;cursor:pointer;line-height:1.3}
  #newchat svg{width:16px;height:16px;flex:none}
  .dsec{padding:10px 18px 2px;font-size:12px;font-weight:600;letter-spacing:.05em;color:var(--muted)}
  #histlist{flex:1;overflow-y:auto;padding:2px 8px}
  .hitem{display:flex;gap:11px;align-items:flex-start;padding:10px;border-radius:12px;cursor:pointer}
  .hitem:hover,.hitem.cur{background:var(--bubble)}
  .hitem .hic{flex:none;width:18px;height:18px;margin-top:1px;color:var(--muted)}
  .hitem .hic svg{width:100%;height:100%;display:block}
  .hitem .hmeta{min-width:0;flex:1}
  .hitem .t{font-size:14.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.35}
  .hitem .d{font-size:11.5px;color:var(--muted);margin-top:1px;line-height:1.35}
  .hempty{color:var(--muted);font-size:13.5px;padding:12px 10px;line-height:1.6}
  #drawer .dfoot{display:flex;align-items:center;gap:11px;padding:12px 16px;border-top:1px solid var(--line)}
  #drawer .dfoot[hidden]{display:none}
  #drawer .avatar{width:36px;height:36px;border-radius:50%;background:var(--accent);color:#43100f;font-weight:700;
           font-size:15.5px;display:flex;align-items:center;justify-content:center;flex:none}
  #drawer .pmeta{min-width:0;flex:1}
  #drawer .pname{font-size:14px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  #drawer .pmail{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  /* ---------- toast ---------- */
  #toast{position:fixed;left:50%;bottom:96px;transform:translateX(-50%) translateY(20px);background:var(--menu);
         color:var(--text);border:1px solid var(--line);font-size:13.5px;padding:8px 16px 10px;border-radius:999px;line-height:1.3;
         opacity:0;pointer-events:none;transition:all .25s;z-index:50;max-width:88vw;text-align:center}
  #toast.on{opacity:1;transform:translateX(-50%) translateY(0)}
  /* ---------- login ---------- */
  #login{position:fixed;inset:0;background:rgba(16,2,4,.8);backdrop-filter:blur(4px);display:none;align-items:center;justify-content:center;z-index:60;padding:16px}
  #login .card{background:linear-gradient(170deg,#521419,#3d0d11);color:var(--text);border:1px solid #83413d;border-radius:24px;
       padding:30px 24px 22px;max-width:360px;width:100%;text-align:center;box-shadow:0 24px 60px rgba(0,0,0,.6),inset 0 1px 0 rgba(255,220,180,.08)}
  #login .lgo{width:78px;height:78px;border-radius:20px;box-shadow:0 8px 22px rgba(0,0,0,.45);border:1px solid rgba(255,255,255,.14)}
  #login .lname{font-size:21px;font-weight:700;margin:13px 0 20px;letter-spacing:.2px}
  #gbtn,#gbtn2{width:100%;display:flex;justify-content:center;align-items:center;gap:10px;border-radius:999px;
       padding:11.5px 18px 13.5px;font-size:15.5px;font-family:inherit;cursor:pointer;font-weight:600;line-height:1.3}
  #gbtn{background:#fff;color:#1f1f1f;border:1px solid #fff}
  #gbtn2{background:transparent;color:var(--text);border:1.5px solid var(--accent)}
  .gicon{width:19px;height:19px;flex:none}
  #login .lor{display:flex;align-items:center;gap:10px;color:var(--muted);font-size:12.5px;margin:13px 2px}
  #login .lor span{flex:1;height:1px;background:var(--line)}
  #lerr{color:#f28b82;font-size:13.5px;margin:10px 0 0}
  #lerr:empty{margin:0}
  #login .llegal{font-size:12px;color:var(--muted);margin:16px 0 0;line-height:1.55}
  #login .llegal a,#acct a{color:var(--accent);text-decoration:underline}
</style>
</head>
<body>
<div id="bamboo" aria-hidden="true"><svg viewBox="0 0 420 920" preserveAspectRatio="xMaxYMid slice" xmlns="http://www.w3.org/2000/svg"><rect x="331.5" y="-20" width="25" height="110" rx="9" fill="#d9905f" opacity="0.16"/><rect x="327.5" y="86" width="33" height="11" rx="5.5" fill="#e2a06b" opacity="0.2"/><rect x="331.5" y="98" width="25" height="110" rx="9" fill="#d9905f" opacity="0.16"/><rect x="327.5" y="204" width="33" height="11" rx="5.5" fill="#e2a06b" opacity="0.2"/><rect x="331.5" y="216" width="25" height="110" rx="9" fill="#d9905f" opacity="0.16"/><rect x="327.5" y="322" width="33" height="11" rx="5.5" fill="#e2a06b" opacity="0.2"/><rect x="331.5" y="334" width="25" height="110" rx="9" fill="#d9905f" opacity="0.16"/><rect x="327.5" y="440" width="33" height="11" rx="5.5" fill="#e2a06b" opacity="0.2"/><rect x="331.5" y="452" width="25" height="110" rx="9" fill="#d9905f" opacity="0.16"/><rect x="327.5" y="558" width="33" height="11" rx="5.5" fill="#e2a06b" opacity="0.2"/><rect x="331.5" y="570" width="25" height="110" rx="9" fill="#d9905f" opacity="0.16"/><rect x="327.5" y="676" width="33" height="11" rx="5.5" fill="#e2a06b" opacity="0.2"/><rect x="331.5" y="688" width="25" height="110" rx="9" fill="#d9905f" opacity="0.16"/><rect x="327.5" y="794" width="33" height="11" rx="5.5" fill="#e2a06b" opacity="0.2"/><rect x="331.5" y="806" width="25" height="110" rx="9" fill="#d9905f" opacity="0.16"/><rect x="327.5" y="912" width="33" height="11" rx="5.5" fill="#e2a06b" opacity="0.2"/><rect x="331.5" y="924" width="25" height="110" rx="9" fill="#d9905f" opacity="0.16"/><rect x="327.5" y="1030" width="33" height="11" rx="5.5" fill="#e2a06b" opacity="0.2"/><rect x="274.0" y="150" width="16" height="110" rx="9" fill="#d9905f" opacity="0.11"/><rect x="270.0" y="256" width="24" height="11" rx="5.5" fill="#e2a06b" opacity="0.15"/><rect x="274.0" y="268" width="16" height="110" rx="9" fill="#d9905f" opacity="0.11"/><rect x="270.0" y="374" width="24" height="11" rx="5.5" fill="#e2a06b" opacity="0.15"/><rect x="274.0" y="386" width="16" height="110" rx="9" fill="#d9905f" opacity="0.11"/><rect x="270.0" y="492" width="24" height="11" rx="5.5" fill="#e2a06b" opacity="0.15"/><rect x="274.0" y="504" width="16" height="110" rx="9" fill="#d9905f" opacity="0.11"/><rect x="270.0" y="610" width="24" height="11" rx="5.5" fill="#e2a06b" opacity="0.15"/><rect x="274.0" y="622" width="16" height="110" rx="9" fill="#d9905f" opacity="0.11"/><rect x="270.0" y="728" width="24" height="11" rx="5.5" fill="#e2a06b" opacity="0.15"/><rect x="274.0" y="740" width="16" height="110" rx="9" fill="#d9905f" opacity="0.11"/><rect x="270.0" y="846" width="24" height="11" rx="5.5" fill="#e2a06b" opacity="0.15"/><rect x="274.0" y="858" width="16" height="110" rx="9" fill="#d9905f" opacity="0.11"/><rect x="270.0" y="964" width="24" height="11" rx="5.5" fill="#e2a06b" opacity="0.15"/><rect x="387.5" y="70" width="13" height="110" rx="9" fill="#d9905f" opacity="0.09"/><rect x="383.5" y="176" width="21" height="11" rx="5.5" fill="#e2a06b" opacity="0.13"/><rect x="387.5" y="188" width="13" height="110" rx="9" fill="#d9905f" opacity="0.09"/><rect x="383.5" y="294" width="21" height="11" rx="5.5" fill="#e2a06b" opacity="0.13"/><rect x="387.5" y="306" width="13" height="110" rx="9" fill="#d9905f" opacity="0.09"/><rect x="383.5" y="412" width="21" height="11" rx="5.5" fill="#e2a06b" opacity="0.13"/><rect x="387.5" y="424" width="13" height="110" rx="9" fill="#d9905f" opacity="0.09"/><rect x="383.5" y="530" width="21" height="11" rx="5.5" fill="#e2a06b" opacity="0.13"/><rect x="387.5" y="542" width="13" height="110" rx="9" fill="#d9905f" opacity="0.09"/><rect x="383.5" y="648" width="21" height="11" rx="5.5" fill="#e2a06b" opacity="0.13"/><rect x="387.5" y="660" width="13" height="110" rx="9" fill="#d9905f" opacity="0.09"/><rect x="383.5" y="766" width="21" height="11" rx="5.5" fill="#e2a06b" opacity="0.13"/><rect x="387.5" y="778" width="13" height="110" rx="9" fill="#d9905f" opacity="0.09"/><rect x="383.5" y="884" width="21" height="11" rx="5.5" fill="#e2a06b" opacity="0.13"/><rect x="387.5" y="896" width="13" height="110" rx="9" fill="#d9905f" opacity="0.09"/><rect x="383.5" y="1002" width="21" height="11" rx="5.5" fill="#e2a06b" opacity="0.13"/><g transform="translate(338,108) rotate(-26)" opacity="0.2"><path d="M0,0 Q 55,-13 122,0 Q 55,7 0,0" fill="#8ba876"/></g><g transform="translate(350,226) rotate(16)" opacity="0.16"><path d="M0,0 Q 46,-13 102,0 Q 46,7 0,0" fill="#8ba876"/></g><g transform="translate(338,348) rotate(-16)" opacity="0.18"><path d="M0,0 Q 50,-13 112,0 Q 50,7 0,0" fill="#8ba876"/></g><g transform="translate(276,300) rotate(-30)" opacity="0.16"><path d="M0,0 Q 41,-13 92,0 Q 41,7 0,0" fill="#8ba876"/></g><g transform="translate(286,428) rotate(15)" opacity="0.14"><path d="M0,0 Q 39,-13 86,0 Q 39,7 0,0" fill="#8ba876"/></g><g transform="translate(390,190) rotate(-22)" opacity="0.15"><path d="M0,0 Q 38,-13 84,0 Q 38,7 0,0" fill="#8ba876"/></g><g transform="translate(342,470) rotate(48)" opacity="0.13"><path d="M0,0 Q 43,-13 96,0 Q 43,7 0,0" fill="#8ba876"/></g><g transform="translate(280,560) rotate(-40)" opacity="0.13"><path d="M0,0 Q 40,-13 88,0 Q 40,7 0,0" fill="#8ba876"/></g></svg></div>
<header>
  <button class="icobtn" id="burger" type="button" aria-label="Menu" data-i18n-aria="ariaMenu">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round"><path d="M4 7h16M4 12h10M4 17h16"/></svg>
  </button>
  <div class="modelwrap">
    <button id="modelBtn" type="button" aria-label="Choose model" data-i18n-aria="ariaModel"><span id="modelName">Bash Lite</span><svg class="chev" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 9l6 6 6-6"/></svg></button>
    <div id="modelMenu" hidden></div>
  </div>
  <button class="icobtn" id="newtop" type="button" aria-label="New chat" data-i18n-aria="ariaNew">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20.5h16"/><path d="M16.9 4.1a1.85 1.85 0 012.6 0l.5.5a1.85 1.85 0 010 2.6L8.2 19l-3.7 1 1-3.7L16.9 4.1z"/><path d="M14.6 6.4l2.6 2.6"/></svg>
  </button>
  <button class="icobtn" id="dots" type="button" aria-label="More" data-i18n-aria="ariaMore">
    <svg viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="5" r="2.15"/><circle cx="12" cy="12" r="2.15"/><circle cx="12" cy="19" r="2.15"/></svg>
  </button>
  <button class="icobtn" id="infobtn" type="button" aria-label="About this app" data-i18n-aria="ariaInfo">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.15" stroke-linecap="round"><circle cx="12" cy="12" r="8.6"/><path d="M12 11.2v5"/><circle cx="12" cy="7.7" r="1.1" fill="currentColor" stroke="none"/></svg>
  </button>
  <div id="acct">
    <div class="who" id="acctname" data-i18n="notSignedIn">Not signed in</div>
    <div class="sub" data-i18n="freeLeft">Free messages left</div>
    <div id="acctrem"></div>
    <p class="note"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11.1 4.4c.4-.8 1.4-.8 1.8 0l8.3 14.2c.4.8-.1 1.7-1 1.7H3.8c-.9 0-1.4-.9-1-1.7L11.1 4.4Z"/><path d="M12 10.2v4.1"/><circle cx="12" cy="17.3" r=".6" fill="currentColor" stroke="none"/></svg> <span data-i18n="acctNote">Demo mode — this app is still being built. Chat Bash can make mistakes, so double-check important information.</span></p>
    <p class="note" style="margin-top:10px" data-i18n-html="acctLegal"><a href="/privacy">Privacy Policy</a> · <a href="/terms">Terms</a></p>
    <button id="logout" type="button" data-i18n="logout">Log out</button>
  </div>
</header>
<div id="chat">
  <div class="msg a" data-i18n="greeting">Assalamu Alaikum! I'm Chat Bash. Ask me anything about Bangladesh — history, food, sports, travel, today's news or prices — I'll try to give the right answer.</div>
</div>
<div class="chips" id="chips">
  <button class="chip" type="button" data-i18n="chip1">What is the national fish of Bangladesh?</button>
  <button class="chip" type="button" data-i18n="chip2">Tell me about the Padma Bridge</button>
  <button class="chip" type="button" data-i18n="chip3">What's the weather in Dhaka today?</button>
  <button class="chip" type="button" data-i18n="chip4">Cox's Bazar travel tips, please</button>
</div>
<div id="limitcard" hidden>
  <div class="lt" data-i18n="limitTitle">This model's limit is over</div>
  <div class="ls" id="limitSub"></div>
  <button id="limitBtn" type="button" data-i18n="useOther">Use another model</button>
</div>
<form id="f">
  <button class="icobtn" id="plus" type="button" aria-label="More options" data-i18n-aria="ariaMoreOpts">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>
  </button>
  <input id="q" placeholder="Ask Chat Bash…" data-i18n-ph="askPh" autocomplete="off">
  <button class="icobtn" id="mic" type="button" aria-label="Voice input" data-i18n-aria="ariaMic">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2.5" width="6" height="11.5" rx="3"/><path d="M5.5 11.5a6.5 6.5 0 0013 0M12 18v3M8.5 21h7"/></svg>
  </button>
  <button id="send" type="submit" aria-label="Send" data-i18n-aria="ariaSend">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19.5v-13M6 11l6-6 6 6"/></svg>
  </button>
</form>
<p class="fineprint" data-i18n="fineprint">Chat Bash can make mistakes — double-check important information. Built entirely with free technology, not a multi-crore project.</p>

<div id="plusmenu">
  <button type="button" id="pm-new"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><path d="M4.4 19.6l1-3.9L16.5 4.6a1.95 1.95 0 012.8 0l.1.1a1.95 1.95 0 010 2.8L8.3 18.6l-3.9 1Z"/><path d="M14.7 6.4l2.8 2.8"/></svg> <span data-i18n="pmNew">New chat</span></button>
  <button type="button" id="pm-hist"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round"><circle cx="12" cy="12" r="8.3"/><path d="M12 7.8V12l2.9 2"/></svg> <span data-i18n="pmHist">Previous chats</span></button>
  <button type="button" id="pm-model"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><path d="M3.8 8h12.7M13.2 4.4 16.8 8l-3.6 3.6M20.2 16H7.5M10.8 12.4 7.2 16l3.6 3.6"/></svg> <span data-i18n="pmModel">Use another model</span></button>
</div>

<div id="thememenu">
  <button type="button" data-th=""><span class="sw" style="background:linear-gradient(135deg,#7a2027,#38070d)"></span><span data-i18n="thRed">Red Bamboo</span><span class="tk"></span></button>
  <button type="button" data-th="cream"><span class="sw" style="background:linear-gradient(135deg,#f7f2e7 55%,#9e3a2e)"></span><span data-i18n="thCream">Cream</span><span class="tk"></span></button>
  <button type="button" data-th="green"><span class="sw" style="background:linear-gradient(135deg,#12573c 55%,#d42a24)"></span><span data-i18n="thGreen">Flag Green</span><span class="tk"></span></button>
</div>

<div id="langmenu">
  <button type="button" data-lang="en"><span class="swtxt">EN</span>English<span class="tk"></span></button>
  <button type="button" data-lang="bn"><span class="swtxt">বাং</span>বাংলা<span class="tk"></span></button>
</div>

<div id="scrim"></div>
<aside id="drawer">
  <div class="dhead"><b><img class="dlogo" src="/icon-192.png" alt=""> Chat Bash</b>
    <button class="icobtn" id="dclose" type="button" aria-label="Close" data-i18n-aria="ariaClose">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
    </button>
  </div>
  <button id="newchat" type="button"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><path d="M4.4 19.6l1-3.9L16.5 4.6a1.95 1.95 0 012.8 0l.1.1a1.95 1.95 0 010 2.8L8.3 18.6l-3.9 1Z"/><path d="M14.7 6.4l2.8 2.8"/></svg> <span data-i18n="newChatBtn">Start a new chat</span></button>
  <div class="dsec" data-i18n="recent">Recent chats</div>
  <div id="histlist"></div>
  <div class="dfoot" id="dfoot" hidden>
    <div class="avatar" id="davatar">C</div>
    <div class="pmeta"><div class="pname" id="dname"></div><div class="pmail" id="dmail"></div></div>
  <button class="icobtn" id="thembtn" type="button" aria-label="Change theme" data-i18n-aria="ariaTheme">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.15" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.5a8.5 8.5 0 100 17c1.2 0 1.9-.9 1.9-1.9 0-.8-.5-1.3-.5-2.1 0-1 .8-1.7 1.9-1.7H17a4.5 4.5 0 004.5-4.5C21.5 6.4 17.2 3.5 12 3.5z"/><circle cx="7.6" cy="10.2" r="1.15" fill="currentColor" stroke="none"/><circle cx="10.4" cy="6.9" r="1.15" fill="currentColor" stroke="none"/><circle cx="15.2" cy="7.6" r="1.15" fill="currentColor" stroke="none"/></svg>
  </button>
  <button class="icobtn" id="langbtn" type="button" aria-label="Language" data-i18n-aria="ariaLang">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.15" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.6"/><ellipse cx="12" cy="12" rx="3.9" ry="8.6"/><path d="M3.6 9.2h16.8M3.6 14.8h16.8"/></svg>
  </button>
  </div>
</aside>

<div id="soonmodal" hidden>
  <div class="sm-card">
    <div class="sm-t" data-i18n="soonTitle">This model is still learning</div>
    <p class="sm-b" id="soonBody"></p>
    <button id="soonOk" type="button" data-i18n="gotIt">Got it</button>
  </div>
</div>
<div id="fullmodal" hidden>
  <div class="sm-card">
    <img src="/icon-192.png" alt="" style="width:54px;height:54px;border-radius:15px">
    <div class="sm-t" style="margin-top:10px" data-i18n="fullTitle">The server is overloaded</div>
    <p class="sm-b" data-i18n="fullBody">A lot of people are using Chat Bash at the same time right now, so no new users can enter. As soon as someone leaves, a place will open up — please try again in a little while.</p>
    <button id="fullRetry" type="button" data-i18n="tryAgain">Try again</button>
  </div>
</div>
<div id="breakmodal" hidden>
  <div class="sm-card">
    <img src="/icon-192.png" alt="" style="width:54px;height:54px;border-radius:15px">
    <div class="sm-t" style="margin-top:10px" data-i18n="breakTitle">Time's up for this hour</div>
    <p class="sm-b" id="breakBody" data-i18n="breakBody">So that everyone gets a chance, chatting is limited to 10 minutes per hour.</p>
    <button id="breakOk" type="button" data-i18n="tryAgain">Try again</button>
  </div>
</div>
<div id="infomodal" hidden>
  <div class="sm-card">
    <div class="sm-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.15" stroke-linecap="round" stroke-linejoin="round"><path d="M11.1 4.4c.4-.8 1.4-.8 1.8 0l8.3 14.2c.4.8-.1 1.7-1 1.7H3.8c-.9 0-1.4-.9-1-1.7L11.1 4.4Z"/><path d="M12 10.2v4.1"/><circle cx="12" cy="17.3" r=".6" fill="currentColor" stroke="none"/></svg></div>
    <div class="sm-t" data-i18n="infoTitle">Demo mode</div>
    <p class="sm-b" data-i18n="infoBody">This app is still being built. Answers may sometimes be wrong or a little slow — please double-check important information.</p>
    <button id="infoOk" type="button" data-i18n="gotIt">Got it</button>
  </div>
</div>
<div id="maintmodal" hidden>
  <div class="sm-card">
    <img src="/icon-192.png" alt="" style="width:54px;height:54px;border-radius:15px">
    <div class="sm-t" style="margin-top:10px" data-i18n="maintTitle">Work in progress</div>
    <p class="sm-b" data-i18n="maintBody">Chat Bash is being worked on right now, so chatting isn't available for the moment. Please try again in a little while.</p>
    <button id="maintRetry" type="button" data-i18n="tryAgain">Try again</button>
  </div>
</div>
<div id="toast"></div>

<div id="login">
  <div class="card">
    <img class="lgo" src="/icon-192.png" alt="Chat Bash">
    <div class="lname">Chat Bash</div>
    <button id="gbtn" type="button"><svg class="gicon" viewBox="0 0 24 24" aria-hidden="true"><path fill="#4285F4" d="M23.5 12.3c0-.9-.1-1.5-.3-2.2H12v4.3h6.5c-.1 1.1-.8 2.7-2.4 3.8l3.7 2.9c2.3-2.1 3.7-5.2 3.7-8.8z"/><path fill="#34A853" d="M12 24c3.2 0 5.9-1.1 7.9-2.9l-3.7-2.9c-1 .7-2.4 1.2-4.2 1.2-3.2 0-5.9-2.2-6.9-5.1L1.3 17.2C3.3 21.2 7.3 24 12 24z"/><path fill="#FBBC05" d="M5.1 14.3c-.2-.7-.4-1.5-.4-2.3s.1-1.6.4-2.3L1.3 6.8C.5 8.4 0 10.1 0 12s.5 3.6 1.3 5.2l3.8-2.9z"/><path fill="#EA4335" d="M12 4.7c1.8 0 3 .8 3.7 1.4l3.3-3.2C17.9 1.1 15.2 0 12 0 7.3 0 3.3 2.8 1.3 6.8l3.8 2.9c1-2.9 3.7-5 6.9-5z"/></svg> <span data-i18n="loginBtn">Log in with Google</span></button>
    <div class="lor"><span></span><span data-i18n="orWord" style="flex:none">or</span><span></span></div>
    <button id="gbtn2" type="button"><svg class="gicon" viewBox="0 0 24 24" aria-hidden="true"><path fill="#4285F4" d="M23.5 12.3c0-.9-.1-1.5-.3-2.2H12v4.3h6.5c-.1 1.1-.8 2.7-2.4 3.8l3.7 2.9c2.3-2.1 3.7-5.2 3.7-8.8z"/><path fill="#34A853" d="M12 24c3.2 0 5.9-1.1 7.9-2.9l-3.7-2.9c-1 .7-2.4 1.2-4.2 1.2-3.2 0-5.9-2.2-6.9-5.1L1.3 17.2C3.3 21.2 7.3 24 12 24z"/><path fill="#FBBC05" d="M5.1 14.3c-.2-.7-.4-1.5-.4-2.3s.1-1.6.4-2.3L1.3 6.8C.5 8.4 0 10.1 0 12s.5 3.6 1.3 5.2l3.8-2.9z"/><path fill="#EA4335" d="M12 4.7c1.8 0 3 .8 3.7 1.4l3.3-3.2C17.9 1.1 15.2 0 12 0 7.3 0 3.3 2.8 1.3 6.8l3.8 2.9c1-2.9 3.7-5 6.9-5z"/></svg> <span data-i18n="signupBtn">Sign up with Google</span></button>
    <p id="lerr"></p>
    <p class="llegal" data-i18n-html="legal">By logging in, you accept our <a href="/terms">Terms</a> and <a href="/privacy">Privacy Policy</a>.</p>
  </div>
</div>

<script src="https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js"></script>
<script src="https://www.gstatic.com/firebasejs/10.12.2/firebase-auth-compat.js"></script>
<script>
const FB_CONFIG = __FB_CONFIG__;
const MODELS = __MODELS_JSON__;
const AUTH_ON = Object.keys(FB_CONFIG).length > 0;
const $=id=>document.getElementById(id);
const chat=$('chat'),form=$('f'),q=$('q'),send=$('send'),mic=$('mic'),
      loginEl=$('login'),chipsEl=$('chips'),
      drawer=$('drawer'),scrim=$('scrim'),acct=$('acct'),plusmenu=$('plusmenu');
let currentUser=null, remaining={}, pairs=[], currentId=null;
let isOwner=false;
let appSess=null, sessUser=null;
try{ const sv=JSON.parse(localStorage.getItem('cb_appsess')||'null');
  if(sv&&sv.token){ appSess=sv.token; sessUser={uid:sv.uid,email:sv.email||'',displayName:sv.name||''}; } }catch(e){}
async function authTok(){ if(currentUser){ try{ return await currentUser.getIdToken(); }catch(e){ return ''; } } return appSess||''; }
function setAppSession(d){ appSess=d.session; sessUser={uid:d.uid,email:d.email||'',displayName:d.name||''};
  try{ localStorage.setItem('cb_appsess',JSON.stringify({token:d.session,uid:d.uid,email:d.email||'',name:d.name||''})); }catch(e){} }
function clearAppSession(){ appSess=null; sessUser=null; try{ localStorage.removeItem('cb_appsess'); }catch(e){} }

const IC={
 up:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.15" stroke-linecap="round" stroke-linejoin="round"><path d="M7.5 21.5v-10l3.9-7c1.6-.3 3 1 2.8 2.6l-.6 4.4h5.9a2 2 0 012 2.4l-1.1 5.7a2 2 0 01-2 1.8H7.5"/><rect x="3" y="11.5" width="4.5" height="10" rx="1.2"/></svg>',
 down:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.15" stroke-linecap="round" stroke-linejoin="round"><path d="M7.5 2.5v10l3.9 7c1.6.3 3-1 2.8-2.6l-.6-4.4h5.9a2 2 0 002-2.4L20.4 4.4a2 2 0 00-2-1.8H7.5"/><rect x="3" y="2.5" width="4.5" height="10" rx="1.2"/></svg>',
 regen:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><path d="M20.5 12a8.5 8.5 0 11-2.5-6.1M20.5 3.4V8H16"/><circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none"/></svg>',
 share:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><path d="M12 14.5v-10M7.8 8.2L12 4l4.2 4.2"/><path d="M5 12.5V19a1.8 1.8 0 001.8 1.8h10.4A1.8 1.8 0 0019 19v-6.5"/></svg>',
 copy:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="3"/><path d="M5.5 15h-.7A1.8 1.8 0 013 13.2V4.8A1.8 1.8 0 014.8 3h8.4A1.8 1.8 0 0115 4.8v.7"/></svg>',
 speak:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9.8v4.4h3.2L12 18.6V5.4L7.2 9.8H4z"/><path d="M15.2 9.6a3.8 3.8 0 010 4.8"/><path d="M17.9 7a7.8 7.8 0 010 10"/></svg>'};

/* ---------------- i18n: English by default, Bengali on toggle ---------------- */
const STR={
en:{greeting:"Assalamu Alaikum! I'm Chat Bash. Ask me anything about Bangladesh — history, food, sports, travel, today's news or prices — I'll try to give the right answer.",
chip1:"What is the national fish of Bangladesh?",chip2:"Tell me about the Padma Bridge",chip3:"What's the weather in Dhaka today?",chip4:"Cox's Bazar travel tips, please",
askPh:"Ask Chat Bash…",thinking:"Thinking",
demoText:"Demo mode — this app is still being built. Answers may sometimes be wrong or a little slow.",
acctNote:"Demo mode — this app is still being built. Chat Bash can make mistakes, so double-check important information.",
acctLegal:'<a href="/privacy">Privacy Policy</a> · <a href="/terms">Terms</a>',
legal:'By logging in, you accept our <a href="/terms">Terms</a> and <a href="/privacy">Privacy Policy</a>.',
fineprint:"Chat Bash can make mistakes — double-check important information. Built entirely with free technology, not a multi-crore project.",
notSignedIn:"Not signed in",freeLeft:"Free messages left",logout:"Log out",
pmNew:"New chat",pmHist:"Previous chats",pmModel:"Use another model",newChatBtn:"Start a new chat",recent:"Recent chats",
thRed:"Red Bamboo",thCream:"Cream",thGreen:"Flag Green",soonPill:"Coming soon",soonTitle:"This model is still learning",gotIt:"Got it",
fullTitle:"The server is overloaded",fullBody:"A lot of people are using Chat Bash at the same time right now, so no new users can enter. As soon as someone leaves, a place will open up — please try again in a little while.",tryAgain:"Try again",
breakTitle:"Time's up for this hour",breakBody:"So that everyone gets a chance, chatting is limited to 10 minutes per hour.",breakBodyT:"So that everyone gets a chance, chatting is limited to 10 minutes per hour. You can chat again from {time}.",
limitTitle:"This model's limit is over",useOther:"Use another model",allOut:"All models are out of limit",
loginBtn:"Log in with Google",orWord:"or",signupBtn:"Sign up with Google",
copied:"Copied",copyFail:"Couldn't copy",noListen:"Listening isn't available on this device",noVoice:"Voice input is not available in this browser — type your question",notHeard:"Couldn't hear that — try again",voiceFail:"Voice input didn't start",
netErr:"Network problem",netRetry:"Network problem — please try again",failed:"Did not work — try again",somethingWrong:"Something went wrong.",loginExpired:"Your login has expired — log in again.",
themeChanged:"Theme changed",langChanged:"Language changed",welcome:"Welcome! Your account has been created",modelWord:"Model",
histEmpty:"No previous chats yet. Ask a question and your history will appear here.",newChatT:"New chat",
fbUp:"Good answer",fbDown:"Bad answer",fbRegen:"Answer again",fbShare:"Share",fbCopy:"Copy",fbListen:"Listen",
descLite:"Light & fast — for everyday questions",descPro:"The smartest — for hard questions",descSwift:"Quick answers — for small questions",
loginWait:"Logging in — please wait…",codeExpired:"That login code has expired — tap sign-in again.",loginIncomplete:"Login didn't complete — please try again.",browserOpening:"Opening the browser — choose your account there…",loginFailed:"Login failed — please try again.",backToApp:"Back to the Chat Bash app",
soonBody:"{name} is still being built. It is not fully ready to talk to people properly yet. It will be ready very soon — though by \"soon\" we could mean ten or twelve years, or even two or three decades.",
unlimited:"Unlimited",maintTitle:"Work in progress",maintBody:"Chat Bash is being worked on right now, so chatting isn't available for the moment. Please try again in a little while.",infoTitle:"Demo mode",infoBody:"This app is still being built. Answers may sometimes be wrong or a little slow — please double-check important information.",ariaInfo:"About this app",ariaMenu:"Menu",ariaModel:"Choose model",ariaNew:"New chat",ariaMore:"More",ariaMoreOpts:"More options",ariaTheme:"Change theme",ariaLang:"Language",ariaMic:"Voice input",ariaSend:"Send",ariaClose:"Close"},
bn:{greeting:"আসসালামু আলাইকুম! আমি Chat Bash। বাংলাদেশ নিয়ে যেকোনো প্রশ্ন করো — ইতিহাস, খাবার, খেলা, ভ্রমণ, আজকের খবর বা দাম — চেষ্টা করব সঠিক উত্তর দিতে।",
chip1:"বাংলাদেশের জাতীয় মাছ কোনটি?",chip2:"পদ্মা সেতু সম্পর্কে বলো",chip3:"আজ ঢাকার আবহাওয়া কেমন?",chip4:"কক্সবাজার ভ্রমণের টিপস দাও",
askPh:"Chat Bash-কে প্রশ্ন করো…",thinking:"ভাবছি",
demoText:"ডেমো মোড — এই অ্যাপটা এখনো তৈরির কাজ চলছে। মাঝে মাঝে ভুল উত্তর বা একটু দেরি হতে পারে।",
acctNote:"ডেমো মোড — অ্যাপটা এখনো তৈরির কাজ চলছে। Chat Bash ভুল করতে পারে, গুরুত্বপূর্ণ তথ্য যাচাই করে নিও।",
acctLegal:'<a href="/privacy">প্রাইভেসি নীতি</a> · <a href="/terms">শর্তাবলি</a>',
legal:'লগইন করলে তুমি আমাদের <a href="/terms">শর্তাবলি</a> ও <a href="/privacy">প্রাইভেসি নীতি</a> মেনে নিচ্ছো।',
fineprint:"Chat Bash ভুল করতে পারে — গুরুত্বপূর্ণ তথ্য যাচাই করে নিও। সম্পূর্ণ ফ্রি প্রযুক্তিতে তৈরি, কোনো কোটি টাকার প্রকল্প নয়।",
notSignedIn:"লগইন করা নেই",freeLeft:"বাকি ফ্রি মেসেজ",logout:"লগআউট",
pmNew:"নতুন চ্যাট",pmHist:"আগের চ্যাটগুলো",pmModel:"অন্য মডেল ব্যবহার করো",newChatBtn:"নতুন চ্যাট শুরু করো",recent:"সাম্প্রতিক চ্যাট",
thRed:"লাল বাঁশ",thCream:"ক্রিম",thGreen:"পতাকার সবুজ",soonPill:"শীঘ্রই আসছে",soonTitle:"এই মডেলটা এখনো শিখছে",gotIt:"বুঝেছি",
fullTitle:"সার্ভার ওভারলোড হয়ে গেছে",fullBody:"এই মুহূর্তে অনেক মানুষ একসাথে Chat Bash ব্যবহার করছে, তাই নতুন করে আর ঢোকা যাচ্ছে না। কেউ বের হলেই জায়গা খালি হবে — একটু পরে আবার চেষ্টা করো।",tryAgain:"আবার চেষ্টা করো",
breakTitle:"এই ঘণ্টার সময় শেষ",breakBody:"সবাই যাতে সুযোগ পায়, তাই প্রতি ঘণ্টায় ১০ মিনিট করে চ্যাট করা যায়।",breakBodyT:"সবাই যাতে সুযোগ পায়, তাই প্রতি ঘণ্টায় ১০ মিনিট করে চ্যাট করা যায়। আবার কথা বলা যাবে {time} থেকে।",
limitTitle:"এই মডেলের লিমিট শেষ",useOther:"অন্য মডেল ব্যবহার করো",allOut:"সব মডেলের লিমিট শেষ",
loginBtn:"Google দিয়ে লগইন",orWord:"অথবা",signupBtn:"Google দিয়ে অ্যাকাউন্ট খুলো",
copied:"কপি হয়েছে",copyFail:"কপি হয়নি",noListen:"এই ডিভাইসে শোনানোর সুবিধা নেই",noVoice:"এই ব্রাউজারে ভয়েস ইনপুট নেই — লিখে প্রশ্ন করো",notHeard:"শোনা যায়নি, আবার চেষ্টা করো",voiceFail:"ভয়েস ইনপুট চালু হয়নি",
netErr:"নেটওয়ার্কে সমস্যা",netRetry:"নেটওয়ার্কে সমস্যা — আবার চেষ্টা করো।",failed:"হয়নি, আবার চেষ্টা করো",somethingWrong:"কিছু একটা সমস্যা হয়েছে।",loginExpired:"লগইনের মেয়াদ শেষ — আবার লগইন করো।",
themeChanged:"থিম বদলানো হয়েছে",langChanged:"ভাষা বদলানো হয়েছে",welcome:"স্বাগতম! তোমার অ্যাকাউন্ট তৈরি হয়ে গেছে",modelWord:"মডেল",
histEmpty:"এখনো কোনো আগের চ্যাট নেই। প্রশ্ন করলেই এখানে হিস্ট্রি জমা হবে।",newChatT:"নতুন চ্যাট",
fbUp:"ভালো লেগেছে",fbDown:"ভালো লাগেনি",fbRegen:"আবার উত্তর দাও",fbShare:"শেয়ার",fbCopy:"কপি",fbListen:"শুনুন",
descLite:"হালকা ও দ্রুত — প্রতিদিনের প্রশ্নে",descPro:"সবচেয়ে বুদ্ধিমান — কঠিন প্রশ্নে",descSwift:"ঝটপট উত্তর — ছোট প্রশ্নে",
loginWait:"লগইন হচ্ছে — একটু অপেক্ষা করো…",codeExpired:"লগইন কোডটার মেয়াদ শেষ — আবার সাইন-ইন চাপো।",loginIncomplete:"লগইন সম্পূর্ণ হয়নি — আবার চেষ্টা করো।",browserOpening:"ব্রাউজার খুলছে — সেখানে তোমার অ্যাকাউন্টটা বেছে নাও…",loginFailed:"লগইন হয়নি — আবার চেষ্টা করো।",backToApp:"Chat Bash অ্যাপে ফিরে যাও",
soonBody:'{name} এখনো তৈরির কাজ চলছে। এটি এখনো মানুষের সাথে ঠিকমতো কথা বলার জন্য পুরোপুরি তৈরি না। খুব দ্রুতই তৈরি করা হবে — তবে "দ্রুত" বলতে দশ-বারো বছরও লাগতে পারে, কিংবা দুই-তিন যুগও লেগে যেতে পারে।',
unlimited:"আনলিমিটেড",maintTitle:"কাজ চলতাছে",maintBody:"এই মুহূর্তে Chat Bash-এ কাজ চলতাছে, তাই আপাতত চ্যাটিং করা যাবে না। একটু পরে আবার চেষ্টা করো।",infoTitle:"ডেমো মোড",infoBody:"এই অ্যাপটা এখনো তৈরির কাজ চলছে। মাঝে মাঝে ভুল উত্তর বা একটু দেরি হতে পারে — গুরুত্বপূর্ণ তথ্য যাচাই করে নিও।",ariaInfo:"অ্যাপ সম্পর্কে",ariaMenu:"মেনু",ariaModel:"মডেল বাছাই",ariaNew:"নতুন চ্যাট",ariaMore:"আরও",ariaMoreOpts:"আরও অপশন",ariaTheme:"থিম বদলাও",ariaLang:"ভাষা",ariaMic:"ভয়েসে লেখো",ariaSend:"পাঠাও",ariaClose:"বন্ধ করো"}};
let curLang=(()=>{ try{ const l=localStorage.getItem('cb_lang'); return l==='bn'?'bn':'en'; }catch(e){ return 'en'; } })();
function tr(k){ return (STR[curLang]&&STR[curLang][k])||STR.en[k]||k; }
function locTag(){ return curLang==='bn'?'bn-BD':'en-GB'; }
function descOf(k){ return k==='lite'?tr('descLite'):k==='pro'?tr('descPro'):tr('descSwift'); }
function applyLang(){
  document.querySelectorAll('[data-i18n]').forEach(el=>{ el.textContent=tr(el.dataset.i18n); });
  document.querySelectorAll('[data-i18n-html]').forEach(el=>{ el.innerHTML=tr(el.dataset.i18nHtml); });
  document.querySelectorAll('[data-i18n-ph]').forEach(el=>{ el.placeholder=tr(el.dataset.i18nPh); });
  document.querySelectorAll('[data-i18n-aria]').forEach(el=>{ el.setAttribute('aria-label',tr(el.dataset.i18nAria)); });
  document.documentElement.lang=(curLang==='bn'?'bn':'en');
  document.querySelectorAll('.mopt').forEach(o=>{ const md=o.querySelector('.md'); if(md) md.textContent=descOf(o.dataset.key);
    const sn=o.querySelector('.soon'); if(sn) sn.textContent=tr('soonPill'); });
  document.querySelectorAll('#langmenu button').forEach(b=>b.classList.toggle('on',b.dataset.lang===curLang));
}
function syncNativeLang(){ try{ const P=window.Capacitor&&Capacitor.Plugins&&Capacitor.Plugins.Preferences; if(P) P.set({key:'cb_lang',value:curLang}); }catch(e){} }
function setLang(l){ curLang=(l==='bn')?'bn':'en'; syncNativeLang();
  try{ localStorage.setItem('cb_lang',curLang); }catch(e){}
  document.cookie='cb_lang='+curLang+';path=/;max-age=31536000;SameSite=Lax';
  applyLang(); if(typeof renderAcct==='function') renderAcct(); if(typeof renderHist==='function') renderHist(); }
let curModel='__DEFAULT_MODEL__';
const modelMenu=$('modelMenu');
MODELS.forEach(m=>{
  const b=document.createElement('button'); b.type='button'; b.className='mopt'; b.dataset.key=m.key;
  b.innerHTML='<span class="mt"><span class="mn"></span><span class="md"></span></span>'+(m.soon?'<span class="soon">'+tr('soonPill')+'</span>':'<span class="radio"></span>');
  b.querySelector('.mn').textContent=m.name; b.querySelector('.md').textContent=descOf(m.key);
  b.onclick=()=>{ modelMenu.hidden=true; if(m.soon){ openSoon(m); return; } setModel(m.key); };
  modelMenu.appendChild(b);
});
function setModel(k){ curModel=k; const m=MODELS.find(x=>x.key===k); if(m) $('modelName').textContent=m.name;
  modelMenu.querySelectorAll('.mopt').forEach(o=>o.classList.toggle('on',o.dataset.key===k));
  if(typeof checkLimit==='function') checkLimit(); }
setModel(curModel);
$('modelBtn').onclick=e=>{ e.stopPropagation(); modelMenu.hidden=!modelMenu.hidden; };
document.addEventListener('click',e=>{ if(!modelMenu.hidden&&!e.target.closest('.modelwrap')) modelMenu.hidden=true; });

let toastTimer=null;
function toast(t){const el=$('toast');el.textContent=t;el.classList.add('on');
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.remove('on'),1800);}

/* ---------------- history storage ---------------- */
function loadChats(){try{return JSON.parse(localStorage.getItem('cb_chats')||'[]');}catch(e){return[];}}
function storeChats(c){try{localStorage.setItem('cb_chats',JSON.stringify(c.slice(0,30)));}catch(e){}}
let syncTimer=null;
async function syncHistoryUp(){
  if(!currentUser&&!sessUser) return;
  try{
    const t=await authTok();
    await fetch('/history/save',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({idToken:t,chats:loadChats()})});
  }catch(e){}
}
function queueSync(){ clearTimeout(syncTimer); syncTimer=setTimeout(syncHistoryUp,900); }
async function syncHistoryDown(){
  if(!currentUser&&!sessUser) return;
  try{
    const t=await authTok();
    const r=await fetch('/history/get',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({idToken:t})});
    const d=await r.json(); if(!d||!Array.isArray(d.chats)) return;
    const byId={};
    d.chats.forEach(c=>{ if(c&&c.id) byId[c.id]=c; });
    loadChats().forEach(c=>{ if(c&&c.id&&(!byId[c.id]||(byId[c.id].ts||0)<(c.ts||0))) byId[c.id]=c; });
    const merged=Object.values(byId).sort((a,b)=>(b.ts||0)-(a.ts||0)).slice(0,30);
    storeChats(merged); renderHist(); queueSync();
  }catch(e){}
}
function persist(){
  if(!pairs.length) return;
  const all=loadChats();
  if(!currentId){ currentId='c'+Date.now(); all.unshift({id:currentId,ts:Date.now(),pairs:pairs}); }
  else{ const c=all.find(x=>x.id===currentId); if(c){c.pairs=pairs;c.ts=Date.now();} else all.unshift({id:currentId,ts:Date.now(),pairs:pairs}); }
  storeChats(all); renderHist(); queueSync();
}
function renderHist(){
  const all=loadChats(), list=$('histlist'); list.innerHTML='';
  if(!all.length){ list.innerHTML='<div class="hempty">'+tr('histEmpty')+'</div>'; return; }
  all.forEach(c=>{
    const d=document.createElement('div'); d.className='hitem'+(c.id===currentId?' cur':'');
    const ic=document.createElement('span'); ic.className='hic';
    ic.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.15" stroke-linecap="round" stroke-linejoin="round"><path d="M20.5 11.6a8.4 8.4 0 01-8.4 8.3c-1.3 0-2.6-.3-3.7-.8L3.5 20l1-4.8a8.3 8.3 0 01-1-3.9 8.4 8.4 0 018.6-8.3 8.4 8.4 0 018.4 8.6z"/></svg>';
    const meta=document.createElement('div'); meta.className='hmeta';
    const t=document.createElement('div'); t.className='t'; t.textContent=(c.pairs&&c.pairs[0]?c.pairs[0].u:tr('newChatT'));
    const s=document.createElement('div'); s.className='d'; s.textContent=new Date(c.ts).toLocaleDateString(locTag(),{day:'numeric',month:'long'})+' · '+(c.pairs?c.pairs.length:0)+(curLang==='bn'?'টি প্রশ্ন':' questions');
    meta.appendChild(t); meta.appendChild(s);
    d.appendChild(ic); d.appendChild(meta);
    d.onclick=()=>{ loadChat(c); closeDrawer(); };
    list.appendChild(d);
  });
}
function loadChat(c){
  currentId=c.id; pairs=(c.pairs||[]).map(p=>({u:p.u,a:p.a,f:p.f||''}));
  chat.innerHTML='';
  pairs.forEach((p,i)=>{ addUser(p.u); const el=addBot(p.a); attachActions(el,i); markFb(el,i); });
  chipsEl.style.display=pairs.length?'none':'flex';
  chat.scrollTop=chat.scrollHeight; renderHist();
}
/* keyboard only opens when the user taps the input (mouse devices may auto-focus) */
function focusQ(){ if(window.matchMedia && matchMedia('(pointer:fine)').matches) q.focus(); }
function newChat(){
  pairs=[]; currentId=null; chat.innerHTML='';
  const g=document.createElement('div'); g.className='msg a'; g.dataset.i18n='greeting'; g.textContent=tr('greeting'); chat.appendChild(g);
  chipsEl.style.display='flex'; q.value=''; syncSend(); focusQ(); renderHist();
}

/* ---------------- messages ---------------- */
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function md(s){
  const lines=esc(s).split('\n'); let html='',inUl=false,inOl=false;
  const inline=t=>t.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/(^|[^*])\*([^*\n]+)\*/g,'$1<em>$2</em>').replace(/`([^`]+)`/g,'<code>$1</code>');
  const closeLists=()=>{ if(inUl){html+='</ul>';inUl=false;} if(inOl){html+='</ol>';inOl=false;} };
  lines.forEach(ln=>{
    const t=ln.trim();
    if(/^#{1,4}\s/.test(t)){ closeLists(); html+='<h4>'+inline(t.replace(/^#{1,4}\s/,''))+'</h4>'; }
    else if(/^[-*•]\s+/.test(t)){ if(!inUl){closeLists();html+='<ul>';inUl=true;} html+='<li>'+inline(t.replace(/^[-*•]\s+/,''))+'</li>'; }
    else if(/^\d+[.)]\s+/.test(t)){ if(!inOl){closeLists();html+='<ol>';inOl=true;} html+='<li>'+inline(t.replace(/^\d+[.)]\s+/,''))+'</li>'; }
    else if(t===''){ closeLists(); }
    else{ closeLists(); html+='<p>'+inline(t)+'</p>'; }
  });
  closeLists(); return html;
}
function setRich(el,text){ const x=el.querySelector('.txt'); if(x) x.innerHTML=md(text); }
function typeReply(el, full, done){
  const x=el.querySelector('.txt');
  if(!x||!full){ setRich(el, full||''); if(done) done(); return; }
  const tmp=document.createElement('div'); tmp.innerHTML=md(full);
  const plain=tmp.textContent||full;
  let units;
  try{ units=[...new Intl.Segmenter(undefined,{granularity:'grapheme'}).segment(plain)].map(g=>g.segment); }
  catch(e){ units=Array.from(plain); }
  let i=0; const step=Math.max(1, Math.ceil(units.length/450)); let vt=0;
  x.textContent='';
  const iv=setInterval(()=>{
    i+=step;
    x.textContent=units.slice(0,i).join('');
    chat.scrollTop=chat.scrollHeight;
    if(i<units.length && ++vt%6===0 && navigator.vibrate){ try{ navigator.vibrate(7); }catch(e){} }
    if(i>=units.length){ clearInterval(iv); setRich(el, full); chat.scrollTop=chat.scrollHeight; if(done) done(); }
  }, 20);
}
function add(text,cls){const d=document.createElement('div');d.className='msg '+cls;
  if(cls==='a'){ const x=document.createElement('div'); x.className='txt'; x.innerHTML=md(text); d.appendChild(x); }
  else d.textContent=text;
  chat.appendChild(d);chat.scrollTop=chat.scrollHeight;return d;}
function addUser(t){return add(t,'u');}
function addBot(t){return add(t,'a');}
function markFb(el,i){
  const row=el.querySelector('.acts'); if(!row) return;
  row.querySelectorAll('[data-fb]').forEach(b=>b.classList.toggle('on',pairs[i]&&pairs[i].f===b.dataset.fb));
}
function attachActions(el,i){
  if(el.querySelector('.acts')) return;
  const row=document.createElement('div'); row.className='acts';
  const mk=(svg,label,fn,fb)=>{const b=document.createElement('button');b.type='button';b.className='icobtn';
    b.innerHTML=svg;b.title=label;b.setAttribute('aria-label',label);if(fb)b.dataset.fb=fb;
    b.onclick=()=>fn(b);return b;};
  row.appendChild(mk(IC.up,tr('fbUp'),b=>{pairs[i].f=pairs[i].f==='up'?'':'up';persist();markFb(el,i);},'up'));
  row.appendChild(mk(IC.down,tr('fbDown'),b=>{pairs[i].f=pairs[i].f==='down'?'':'down';persist();markFb(el,i);},'down'));
  row.appendChild(mk(IC.regen,tr('fbRegen'),b=>regen(i,el,b)));
  row.appendChild(mk(IC.share,tr('fbShare'),()=>shareText(pairs[i].a)));
  row.appendChild(mk(IC.copy,tr('fbCopy'),()=>copyText(pairs[i].a)));
  row.appendChild(mk(IC.speak,tr('fbListen'),b=>speak(pairs[i].a,b)));
  el.appendChild(row);
}
function copyText(t){
  if(navigator.clipboard&&navigator.clipboard.writeText){ navigator.clipboard.writeText(t).then(()=>toast(tr('copied'))).catch(()=>toast(tr('copyFail'))); }
  else{ const ta=document.createElement('textarea'); ta.value=t; document.body.appendChild(ta); ta.select();
        try{document.execCommand('copy'); toast(tr('copied'));}catch(e){toast(tr('copyFail'));} ta.remove(); }
}
function shareText(t){
  if(navigator.share){ navigator.share({text:t}).catch(()=>{}); }
  else copyText(t);
}
let speaking=false;
function speak(t,btn){
  if(!('speechSynthesis' in window)){ toast(tr('noListen')); return; }
  if(speaking){ speechSynthesis.cancel(); speaking=false; return; }
  t=t.replace(/\*\*/g,'').replace(/[*#`]/g,'');
  const u=new SpeechSynthesisUtterance(t); u.lang=/[\u0980-\u09FF]/.test(t)?'bn-BD':'en-US';
  u.onend=()=>{speaking=false;}; u.onerror=()=>{speaking=false;};
  speechSynthesis.speak(u); speaking=true;
}
async function ask(text,histPairs){
  const token=await authTok();
  const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({message:text,history:histPairs.map(p=>[p.u,p.a]),model:curModel,idToken:token,lang:curLang})});
  return {status:r.status,data:await r.json()};
}
async function regen(i,el,btn){
  if(btn.disabled) return;
  const old=pairs[i].a; btn.disabled=true;
  const txtEl=el.querySelector('.txt');
  el.classList.add('dots');
  if(txtEl) txtEl.textContent=tr('thinking')+'…';
  try{
    const {status,data}=await ask(pairs[i].u,pairs.slice(0,i));
    el.classList.remove('dots');
    if(status===200&&data.reply){ pairs[i].a=data.reply;
      if(data.remaining){remaining=data.remaining; renderAcct();}
      typeReply(el,data.reply,()=>persist()); }
    else{ setRich(el,old); if(data.error==='session_over'){ admitted=false; showBreak(data.resume); } else toast(data.reply||tr('failed')); }
  }catch(e){ el.classList.remove('dots'); setRich(el,old); toast(tr('netErr')); }
  btn.disabled=false;
}

/* ---------------- account / menus ---------------- */
function nextReset(){ const step=2*24*3600*1000; const ts=(Math.floor(Date.now()/step)+1)*step;
  return new Date(ts).toLocaleDateString(locTag(),{weekday:'long',day:'numeric',month:'long'}); }
function checkLimit(){ const card=$('limitcard'); if(!card) return;
  const r=remaining[curModel]; const out=(!isOwner&&r!==undefined&&r!==null&&r<=0);
  card.hidden=!out; if(!out) return;
  const m=MODELS.find(x=>x.key===curModel);
  $('limitSub').textContent=curLang==='bn'?(m?m.name:'এই মডেল')+'-এর এই পর্বের ফ্রি মেসেজ শেষ হয়ে গেছে। আবার পাওয়া যাবে '+nextReset()+' থেকে।':(m?m.name:'This model')+"'s free messages for this round are over. Available again from "+nextReset()+'.';
  const alt=MODELS.find(x=>x.key!==curModel&&!x.soon&&(remaining[x.key]===undefined||remaining[x.key]>0));
  const btn=$('limitBtn');
  if(alt){ btn.disabled=false; btn.textContent=curLang==='bn'?alt.name+' ব্যবহার করো':'Use '+alt.name; btn.onclick=()=>{ setModel(alt.key); toast(tr('modelWord')+': '+alt.name); }; }
  else{ btn.disabled=true; btn.textContent=tr('allOut'); btn.onclick=null; } }
function openSoon(m){
  $('soonBody').textContent=tr('soonBody').replace('{name}',m.name);
  $('soonmodal').hidden=false;
}
$('soonOk').onclick=()=>{ $('soonmodal').hidden=true; };
$('soonmodal').addEventListener('click',e=>{ if(e.target.id==='soonmodal') $('soonmodal').hidden=true; });
$('infobtn').onclick=e=>{ e.stopPropagation(); closeMenus(); $('infomodal').hidden=false; };
$('infoOk').onclick=()=>{ $('infomodal').hidden=true; };
$('infomodal').addEventListener('click',e=>{ if(e.target.id==='infomodal') $('infomodal').hidden=true; });
function renderAcct(){
  const box=$('acctrem'); box.innerHTML='';
  MODELS.forEach(m=>{ const r=remaining[m.key];
    const line=document.createElement('div'); line.className='remline';
    line.innerHTML='<span>'+m.name+'</span><span>'+(isOwner?tr('unlimited'):(r===undefined?(curLang==='bn'?'দুই দিনে '+m.cap+'টি':m.cap+' every 2 days'):(curLang==='bn'?'বাকি '+r+' / '+m.cap:r+' / '+m.cap+' left')))+'</span>';
    box.appendChild(line); });
  const who=currentUser||sessUser;
  if(who){ $('acctname').textContent=who.displayName||who.email||'';
    const nm=who.displayName||who.email||'';
    $('dname').textContent=nm; $('dmail').textContent=who.email||'';
    $('davatar').textContent=(nm.trim().charAt(0)||'C').toUpperCase(); $('dfoot').hidden=false;
    $('logout').style.display='block'; }
  else{ $('acctname').textContent=tr('notSignedIn'); $('dfoot').hidden=true; $('logout').style.display='none'; }
  checkLimit();
}
function closeMenus(){ acct.classList.remove('open'); plusmenu.classList.remove('open'); $('thememenu').classList.remove('open'); $('langmenu').classList.remove('open'); }
const THEME_BG={'':'#38070d','cream':'#f7f2e7','green':'#07271b'};
function setTheme(t){ if(t) document.documentElement.dataset.theme=t; else delete document.documentElement.dataset.theme;
  try{ localStorage.setItem('cb_theme',t); }catch(e){}
  const mc=document.querySelector('meta[name="theme-color"]'); if(mc) mc.content=THEME_BG[t]||'#38070d';
  document.querySelectorAll('#thememenu button').forEach(b=>b.classList.toggle('on',b.dataset.th===t)); }
setTheme((()=>{ try{ return localStorage.getItem('cb_theme')||''; }catch(e){ return ''; } })());
$('thembtn').onclick=e=>{ e.stopPropagation(); const m=$('thememenu'); m.classList.toggle('open'); plusmenu.classList.remove('open'); acct.classList.remove('open'); $('langmenu').classList.remove('open'); };
document.querySelectorAll('#thememenu button').forEach(b=>{ b.onclick=()=>{ setTheme(b.dataset.th); $('thememenu').classList.remove('open'); toast(tr('themeChanged')); }; });
$('langbtn').onclick=e=>{ e.stopPropagation(); const m=$('langmenu'); m.classList.toggle('open'); plusmenu.classList.remove('open'); acct.classList.remove('open'); $('thememenu').classList.remove('open'); };
document.querySelectorAll('#langmenu button').forEach(b=>{ b.onclick=()=>{ setLang(b.dataset.lang); $('langmenu').classList.remove('open'); toast(tr('langChanged')); }; });
$('dots').onclick=e=>{ e.stopPropagation(); renderAcct(); acct.classList.toggle('open'); plusmenu.classList.remove('open'); $('thememenu').classList.remove('open'); $('langmenu').classList.remove('open'); };
$('logout').onclick=async()=>{ closeMenus(); admitted=false; if(presTimer){clearInterval(presTimer); presTimer=null;} $('fullmodal').hidden=true; $('breakmodal').hidden=true; clearAppSession(); if(!currentUser){ loginEl.style.display='flex'; renderAcct(); } if(AUTH_ON){ try{ const FA=window.Capacitor&&Capacitor.Plugins&&Capacitor.Plugins.FirebaseAuthentication; if(FA) await FA.signOut(); }catch(e){} firebase.auth().signOut(); } };
document.addEventListener('click',e=>{ if(!acct.contains(e.target)&&e.target!==$('dots')) acct.classList.remove('open');
  if(!plusmenu.contains(e.target)&&e.target!==$('plus')&&!$('plus').contains(e.target)) plusmenu.classList.remove('open');
  const tm=$('thememenu'); if(!tm.contains(e.target)&&e.target!==$('thembtn')&&!$('thembtn').contains(e.target)) tm.classList.remove('open');
  const lm=$('langmenu'); if(lm&&!lm.contains(e.target)&&e.target!==$('langbtn')&&!$('langbtn').contains(e.target)) lm.classList.remove('open'); });

/* ---------------- drawer ---------------- */
function openDrawer(){ renderHist(); drawer.classList.add('on'); scrim.classList.add('on'); }
function closeDrawer(){ drawer.classList.remove('on'); scrim.classList.remove('on'); }
$('burger').onclick=openDrawer; $('dclose').onclick=closeDrawer; scrim.onclick=closeDrawer;
$('newchat').onclick=()=>{ newChat(); closeDrawer(); };
$('newtop').onclick=()=>{ newChat(); };

/* ---------------- plus menu ---------------- */
$('plus').onclick=e=>{ e.stopPropagation(); plusmenu.classList.toggle('open'); acct.classList.remove('open'); $('thememenu').classList.remove('open'); $('langmenu').classList.remove('open'); };
$('pm-new').onclick=()=>{ closeMenus(); newChat(); };
$('pm-hist').onclick=()=>{ closeMenus(); openDrawer(); };
$('pm-model').onclick=()=>{ closeMenus(); let i=MODELS.findIndex(m=>m.key===curModel);
  for(let n=0;n<MODELS.length;n++){ i=(i+1)%MODELS.length; if(!MODELS[i].soon) break; }
  setModel(MODELS[i].key); toast(tr('modelWord')+': '+MODELS[i].name); focusQ(); };

/* ---------------- chips ---------------- */
document.querySelectorAll('.chip').forEach(c=>c.onclick=()=>{ q.value=c.textContent; form.requestSubmit(); });

/* ---------------- mic / send toggle ---------------- */
function syncSend(){ const has=q.value.trim().length>0; send.style.display=has?'flex':'none'; mic.style.display=has?'none':'flex'; }
q.addEventListener('input',syncSend); syncSend();
const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
mic.onclick=()=>{
  if(!SR){ toast(tr('noVoice')); return; }
  try{
    const r=new SR(); r.lang=curLang==='bn'?'bn-BD':'en-US'; r.interimResults=false;
    mic.classList.add('rec');
    r.onresult=e=>{ q.value=e.results[0][0].transcript; syncSend(); };
    r.onend=()=>mic.classList.remove('rec');
    r.onerror=()=>{ mic.classList.remove('rec'); toast(tr('notHeard')); };
    r.start();
  }catch(e){ toast(tr('voiceFail')); }
};

/* --------- browser sign-in hands the session to the app (deep link) --------- */
async function maybeHandoff(){
  if(window.Capacitor) return;
  if(!new URLSearchParams(location.search).has('applogin')) return;
  if(sessionStorage.getItem('cb_ho')) return;
  try{
    const t=await currentUser.getIdToken();
    const r=await fetch('/auth/code',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({idToken:t})});
    const d=await r.json(); if(!d||!d.code) return;
    sessionStorage.setItem('cb_ho','1');
    const link='chatbash://login?code='+d.code;
    const div=document.createElement('div');
    div.style.cssText='position:fixed;inset:0;background:rgba(20,2,4,.88);display:flex;align-items:center;justify-content:center;z-index:70;padding:20px';
    div.innerHTML='<a href="'+link+'" style="background:#fff;color:#1f1f1f;padding:14px 22px;border-radius:999px;font-size:17px;text-decoration:none;font-family:inherit">'+tr('backToApp')+'</a>';
    document.body.appendChild(div);
    location.href=link;
  }catch(e){}
}
function wireAppLinks(){
  if(!window.Capacitor||!Capacitor.Plugins||!Capacitor.Plugins.App) return;
  const handleUrl=async e=>{
    const url=(e&&e.url)||''; const m=url.match(/code=([A-Za-z0-9]+)/); if(!m) return;
    const le=document.getElementById('lerr');
    if(le){ le.style.color='#e2b3a9'; le.textContent=tr('loginWait'); }
    try{
      const r=await fetch('/auth/exchange',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({code:m[1]})});
      const d=await r.json();
      if(d&&d.session){
        setAppSession(d);
        if(le) le.textContent='';
        afterSignIn();
      }else if(le){ le.style.color='#f28b82'; le.textContent=tr('codeExpired'); }
    }catch(err){ console.warn('handoff failed',err);
      if(le){ le.style.color='#f28b82'; le.textContent=tr('loginIncomplete'); } }
  };
  Capacitor.Plugins.App.addListener('appUrlOpen',handleUrl);
  Capacitor.Plugins.App.getLaunchUrl().then(u=>{ if(u&&u.url) handleUrl(u); }).catch(()=>{});
}

/* ---------------- capacity gate: 10 inside at a time, 10 min chat per hour ---------------- */
let admitted=false, presTimer=null;
function fmtTime(ts){ return new Date(ts*1000).toLocaleTimeString(locTag(),{hour:'numeric',minute:'2-digit'}); }
function showBreak(resume){
  if(resume) $('breakBody').textContent=tr('breakBodyT').replace('{time}',fmtTime(resume));
  $('breakmodal').hidden=false;
}
async function tryEnter(){
  if(!AUTH_ON||(!currentUser&&!sessUser)) return true;
  try{
    const t=await authTok();
    const r=await fetch('/enter',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({idToken:t})});
    const d=await r.json().catch(()=>({}));
    if(r.ok&&d.ok){ admitted=true; $('fullmodal').hidden=true; $('breakmodal').hidden=true;
      if(!presTimer) presTimer=setInterval(tryEnter,30000);
      return true; }
    admitted=false;
    if(d.reason==='break'){ $('fullmodal').hidden=true; showBreak(d.resume); }
    else{ $('breakmodal').hidden=true; $('fullmodal').hidden=false; }
    return false;
  }catch(e){ return true; }
}
$('fullRetry').onclick=()=>tryEnter();
$('breakOk').onclick=()=>tryEnter();
document.addEventListener('visibilitychange',()=>{ if(!document.hidden&&(currentUser||sessUser)) tryEnter(); });

/* ---------------- auth ---------------- */
function afterSignIn(){ loginEl.style.display='none'; refreshMe(); syncHistoryDown(); tryEnter(); renderAcct(); }
async function refreshMe(){
  if(!currentUser&&!sessUser) return;
  try{
    const t=await authTok();
    const r=await fetch('/me',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({idToken:t})});
    const d=await r.json(); if(d.owner!==undefined) isOwner=!!d.owner;
    if(d.remaining){ remaining=d.remaining; renderAcct(); }
  }catch(e){}
}
if(AUTH_ON){
  firebase.initializeApp(FB_CONFIG);
  wireAppLinks();
  firebase.auth().onAuthStateChanged(u=>{
    currentUser=u;
    if(u){ clearAppSession(); loginEl.style.display='none'; refreshMe(); syncHistoryDown(); maybeHandoff(); tryEnter(); if(regWanted){ regWanted=false; toast(tr('welcome')); } }
    else if(sessUser){ loginEl.style.display='none'; refreshMe(); syncHistoryDown(); tryEnter(); }
    else{ loginEl.style.display='flex'; }
    renderAcct();
  });
  let regWanted=false;
  const gb2=$('gbtn2'); if(gb2) gb2.onclick=()=>{ regWanted=true; $('gbtn').click(); };
  $('gbtn').onclick=async()=>{
    const prov=new firebase.auth.GoogleAuthProvider();
    const FA=window.Capacitor&&Capacitor.Plugins&&Capacitor.Plugins.FirebaseAuthentication;
    if(FA){
      try{
        const r=await FA.signInWithGoogle();
        const tok=r&&r.credential&&r.credential.idToken;
        if(tok){ await firebase.auth().signInWithCredential(firebase.auth.GoogleAuthProvider.credential(tok)); return; }
      }catch(e){ console.warn('native login failed, falling back', e); }
    }
    if(window.Capacitor){
      const purl='https://chat-bash.vercel.app/?applogin=1';
      try{
        if(window.Capacitor.Plugins&&Capacitor.Plugins.Browser){
          lerr.style.color='#e2b3a9'; lerr.textContent=tr('browserOpening');
          await Capacitor.Plugins.Browser.open({url:purl}); return;
        }
      }catch(e){ console.warn('browser open failed',e); }
      try{ const w=window.open(purl,'_system'); if(w){ return; } }catch(e){}
    }
    try{
      if(window.Capacitor){ await firebase.auth().signInWithRedirect(prov); }
      else{ await firebase.auth().signInWithPopup(prov); }
    }
    catch(e){ console.warn('login failed', e); $('lerr').textContent=tr('loginFailed'); }
  };
}else{
  renderAcct();
}
applyLang(); syncNativeLang();
renderAcct(); renderHist();
/* first visit only: show the demo info once, as if the i button was tapped.
   Anyone with traces of earlier use counts as a regular and is not bothered. */
(function(){ try{
  if(localStorage.getItem('cb_seeninfo')) return;
  const regular=!!(localStorage.getItem('cb_appsess')||localStorage.getItem('cb_theme')||localStorage.getItem('cb_lang')||(JSON.parse(localStorage.getItem('cb_chats')||'[]').length));
  localStorage.setItem('cb_seeninfo','1');
  if(!regular) $('infomodal').hidden=false;
}catch(e){} })();

/* ---------------- maintenance watchdog ----------------
   If the server cannot be reached (work going on / outage), say so in the
   user's language instead of leaving a dead screen. */
let maintShown=false;
async function healthOk(){
  try{ const r=await fetch('/health',{cache:'no-store'}); return r.ok; }catch(e){ return false; }
}
async function maintWatch(){
  if(maintShown) return;
  let ok=await healthOk();
  for(let k=0;k<2&&!ok;k++){ await new Promise(r=>setTimeout(r,2500)); ok=await healthOk(); }
  if(!ok){ maintShown=true; $('maintmodal').hidden=false; }
}
$('maintRetry').onclick=()=>{ location.reload(); };
document.addEventListener('visibilitychange',async()=>{
  if(document.hidden) return;
  const ok=await healthOk();
  if(ok&&maintShown){ maintShown=false; $('maintmodal').hidden=true; }
  else if(!ok) maintWatch();
});
maintWatch();

/* ---------------- submit ---------------- */
form.onsubmit=async e=>{
  e.preventDefault();
  const text=q.value.trim(); if(!text) return;
  if(AUTH_ON && !currentUser && !sessUser){ loginEl.style.display='flex'; return; }
  q.value=''; syncSend(); addUser(text); send.disabled=true;
  chipsEl.style.display='none';
  const wait=addBot(tr('thinking')+' '); wait.classList.add('dots');
  wait.querySelector('.txt').innerHTML=tr('thinking')+' <span>.</span><span>.</span><span>.</span>';
  const idx=pairs.length;
  try{
    const {status,data}=await ask(text,pairs);
    wait.classList.remove('dots');
    if(status===401){ setRich(wait,tr('loginExpired'));
      clearAppSession(); if(AUTH_ON) loginEl.style.display='flex'; renderAcct(); }
    else if(data.error==='session_over'){ setRich(wait,data.reply); admitted=false; if(presTimer){clearInterval(presTimer); presTimer=null;} showBreak(data.resume); if(data.remaining){remaining=data.remaining; renderAcct();} }
    else if(status===429){ setRich(wait,data.reply); if(data.remaining){remaining=data.remaining; renderAcct();} }
    else if(data.reply){ typeReply(wait,data.reply,()=>{ pairs.push({u:text,a:data.reply,f:''}); attachActions(wait,idx); persist(); });
      if(data.remaining){remaining=data.remaining; renderAcct();} }
    else setRich(wait,tr('somethingWrong'));
  }catch(err){ wait.classList.remove('dots'); setRich(wait,tr('netRetry')); }
  send.disabled=false; focusQ();
};
if('serviceWorker' in navigator){window.addEventListener('load',()=>{navigator.serviceWorker.register('/sw.js').catch(()=>{});});}
focusQ();
</script>
</body>
</html>
"""


def public_models():
    return [{"key": k, "name": m["name"], "desc": m["desc"], "cap": m["cap"],
             "soon": (m["provider"] == "groq" and not GROQ_API_KEY)
                     or (m["provider"] == "gemini" and not GEMINI_API_KEY)}
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

ICON_192_B64 = "iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAYAAABS3GwHAAAnTUlEQVR4nO2deZxdRZmwn7fq3KXXbE2AsC9GZVNQRgU1MAqo47gMdmREGEVFZdBxnBlR0Wka3BUdt09UQBjQT4ggLuPHIk7CNooogpCJLAoJJEA6W693OVXv90ed232zEkJ333Nvn+f3O+n07XvvWerd6q23qoTppK/PwFLDecscgo6//tUPFDo2rVvoMIcKeqh6/zxE9hbVBYh04rQbIZrWa814dqh6jNmkXkewshr0MRF5QJH7Deb+0Y6eFfzLV8bqPiH0LbJwnKe/30/XZcq0nKWvz7B8ubBkiau9VPjC6QfZcnyceo4DXozqAUS2gDGgCl43/5nRfIiAkfGfIoJ6hTiOEXkE+D1WllmJfjV87uUrxj+3DXmZskuc0m/v7bVcvcTXrH3xS+/YR8ruTTi/GNWjpZAroKCxA+fBew+iCKAanpxMw3VmTBUaWl4Vkdr/BRFDZJHIggharsQq8ntErlHLteWPX/nQ+Ddc3WtZPHWKMDWC1ddnAGqurHjBaS8X0TPx+gbJR7PUeajG4EluTIN5yAR95qAajB0oQkQuQiKDVuJRFfmFRvLt8seu+GXybqG310yFR5hsgROu7jU1je349NtP8F7/DeQEiSxaroJXl5zVTMH5M5oTRVVBPIZI8rnwq+ptCBeOnXvldUDwBr0TEcVkMHkC2Ntraxra9qnTXqyq5xmRv0lcXLg5MEgm9Bk7IARKHlWhkDOh3+D/W430l869YhkwqWHR5AhjckFz+07tHrPSB/oBsTanpUotpreTcp6MmYWqR0GKOaNeAb5rHJ8Y6b/yqcTgenh23uDZKUBfn+G8fkXQ9gtOPVGVr0vOLtSxCiguE/yMSUFxoEbaCkIcP6ZO/nms//s/AoIMPou06a4rwETII23nn3o+Ip9AFapxjIh9Vt+dkbEtVGMiG0lk8c59o7R2/b/y9evLzyYk2jUhTU7Y0ff2+d7q5ZKPXqMjZQ8KImaXvjMjY2dQ9YiotBWsVuP/8eXKqeVPL/kLfYsi+pfFz/TrnrkCJCfq6Ft8mI9y14o1z9HRSoyRbKQ2Y/pQjaWYj7z3q/HVxaW+q2/fFSV4ZgqQnKB47luPkYL9GZi5VKoxkgl/RgNQdeQii2HUx3Jyuf/K65+pEux8uJJ8ceGTp5wg+eh6PHOpxC4T/oyGIWKpxJ5Y203EdcVPLD6F/mUxfYt2WiZ3zgMkMX/xk70vk1zuBjxdVJ3HZPF+RgpQ9VhjMMaJd68f7f/hTnuCp1eAJM2U7+s9xEruNtA5xD5LcWaki6AEgjVlrcavLl1w9e31g7PbY8cWPMnzd/b9fY+V6KcYMuHPSCcihtgrXosS2WsLF/QewJIlbrwubTvs6I/CocsljEL4H0guOohyHGfCn5FajBhi7zBmvsR2CRf2trF8+Q6LLLevAH2LLIuXuLa+U86XQuEEHSmHbE+o38uO7EjnAZaxamwK+Re1bTRfDV5g0XaN9rY1I4mdCn2n/LUx9maqLgbNRnczmgcllrZ85CuVU0rnX3XV9kaLt/YAinDIIUrfWZ3GczGqoD4rXc5oNqxWYi/IN9v7evegd4nfVn9gawVY3Gvo7/dFP3C+FHMHUI1dVt6Q0XQIgnNeCtE89XIhgoY+7ZZvqycJfdrPX/wC9eYunAqqmfXPaGac5CPrnH9Vuf+Hv9oyFNp8xOyQQxTAV/TzpmAjrVYdMpnzbzIyphFhvHMszn2RvkUv4f5DfN1f6kKgq3st/f2+8Mm3HW9yuZN0rOIBmwl/RtOSZIW0HDtTKBxV1D1Opr/fc3XvuNxPKMD9wfob7z6pIiCSiX5GayCgzilOz+XMM3P0LhmfQBMUoDdY/7aPL34pkTmeUmL9MzJaA0s5VlPIHd625+BrEZSrey1smQUycrZElmQCe0ZG6yCiCorTDwDjEY/Uit3aPtW7FyVZAXQkK7FlmZ+MFkMUI86ovHDkUz+8n74+E8FSA3jKnCyFXKeOVuJsHc6MlkTVSTEX+VLlVODjsNQYWBbCHccp6rySWf6MlkWNVh0oJ3PmmTn6lzlDP75wbu8BGDmKaizIM5gllpHRTIgYYqdY85z23TccAagBsCqvkkKukKzVmXmAjNZFcZKPxKt5DSQjwc7ocUZByYa9MlocQcKS+34R8GmhrzdfrHIvkX0u1dhnhW8ZLY2iREbw/smcqx4a5Zx9DuoOIHYAJtuMIqPFEWKnGDO/YguHG+viw8mZfNicIiNjJqCeyIhRfaFRkUMRIav5zJhpKHqYQXhuoy8kI2NaUQRVFF1oQPcJ0x6z9GfGTEEErwiyV4STPUP2UzIFyJgZCOAU0N0jEbrwSfif9QIyZgaSCHtHhPruiRczMmYWEVvOC25iMg2eHlopUGha4RcRTLKPtlfFq+JUJ2Y7Z0w6tWdrkmdvJOTOvSa7nDYhTacARsKe2qVqFVcpg3qwEYVcnqKNmrYhmgURoexiRqsVcDGIweYLFHNhb1/fZM8/ahZzWbP4I6UxiGP2nrsbxzz/KP5qn4N43m57sqBrFm25Apn9n2qEsWqZ1UObWLF2DXeuepg7Hn2Qx9avhSiio9jWVB5BiuecnPortWIoVyvElTIv3vdg3v/SV/G6572APbrngLHgHXjfNA+92RERMGb82T8xuIFfrLiHb/36Zu5a+RBRvkAhl8dp+qtrUq8AkTEMj4ywoHsOF5z0Fk498hgKhSLVcolKHFObwibZMMa0oqrjzz4fReQKRcrlEt+/+w4+ecOPWD24gc6ODuKUl5hJ8Zy/S60CWBFGhod49cIjuGTxe9l33u6UxkaIfdgMRLK8TypQFOeVyBiKbR2sXPck77r62/zygXvp6OzCpdgzp7P2XzUI/9Ag/3D0cfzi3R9l7+7ZDA8PAsErZMKfHgQhMkGUhocH2bt7Nr9490f5h6OPY2RoECtCWsvs06cAqlgxjAwN8rajXsFlp5yFi2PGKhUim63VlXYiaxmrVHBxzGWnnMXbjnpFogQmlUpgGr6jx2aHYjGMjo7ykn0XcvHiMylXSvhkE8CM5sAag1dPuVLi4sVn8pJ9FzI6OoolUYKGy9nEkSqpEgXnYjpzeb7T+26KuTyxc5isg9t0GBFi5ygmbdmZy+NcTNpWnE2PAiShT2l4mLOPPYkj9j2Y4bHRzPI3MdYYhsdGOWLfgzn72JMoDQ+nLhRKiXSFEoZypcIes3s4+9gTqJbGxjtWGc1LZAzV0hhnH3sCe8zuoVypJOmLdCiBaXgQhiaxP1THRjjlyJeyoGd3ynEly+23ACJCOa6woGd3TjnypVTHRsKy45oCuUPT0wl2zmNtjjcfdjQax5nwtxAigsYxbz7saKzN4ZxvuLzVjsbHGKoYDeHPAXPnc+Re+1GtVjCtsDyRBu+G93VH8po2+uKmDyOGarXCkXvtxwFz51OuVDAKaegLpELKBPCVCofO34uu9k6q3jXfMJcqOA8u1CUhQC6CYh462iaOtgLkc2AkKINz4XMpEIapQoCqd3S1d3Lo/L3w4/2AxtP4cmjVMKobOw6cNx+sDUVtaXlCO0IJwg5QyEGxAGJCmfDIGGzYAMNj6PDouIBLexE62mF2J3S2Qy4fvqhcgVIF1IdCsxYLAVUVrA1tHCcGTrXh99lYBagPBdSzZ/echl7OTlMLa6IIujvDPTz+FHrfg+gfH4AVj6Crn4J1G2GsDJXqhIXP56CQhzndyJ67wcH7Ioc/Bzl8Iey/AHI5GBsLCiEmeIoWYs/uOUHJx9u9sUrQeA9AYh28kjNNsCWxc5DPQ3sbrN+I/uTX6I23o3cth6fWQezAWohsUBAjIRSqoQqjYzA4jD68EpbeiRoTFOLwhcirX4r89Utgnz2hXIaxUgt5BAlt7DU1Xj5qaOw53kkkPBSf4jjYJ6HJ7FmwZi3+kmvQ634FD60Mf28rQGdHIqh1Hd3aPdZjbTiKhYn3V6robb9Dl94JC+YjJ70cc+rr4LkHwuho8CItUAulPhg7fJKCaXCTNzgEYkI40twJjB10tUMlRi+9Bn/xtbByTRD6WZ3hPV4n+gNPx7bu2ciEAm0aQi+9FvfjXyKnvAbznl6YPwc2Dof3Nbs3qI0BKKDSUE8QNVbswtkn5CBlSlCz3nNmwe/ux33623DnfSGbM29WkvWZpAkfSoiNIYRO82ZBNUa/dRXu+tsxHzkD+dvjYHg0hGFNO0o+0eYqtd8bpwHpeYppy43XQp7ODvTbV+NO/QjcvSIIZi4KXmGqvJZq+H4jMG82PLkOf/an8R/7anhGxUJQgmYkJfn/GulRgDThfRByI/iPXIg//1sh/u5qn1rB3xIlnK+Qg1md6H/+BP+Oj8PaDSGV2qxKkCIyBdgS70OqMvb4956P/vD/Qc/sEHdPVrjzjK8pGWTrmYPedT/u1HPg0dXQ1UGysUnGLpKaWqBUhD9eg+WvOvxZF4SMTM+crax+6BmoS/JX4aOJmE5pIBfHMLsLVq3BnfFJ+MvjYTCt2TxBo+Ws7khHNWgaNEA1xNzW4j/0uSD882YHoavDg48Q6RRr2xFTE/gujOkUY02Y1zN1riJ2wfKvegL3nj5Yuz70CVK++sLmNFrOJo5UDISlAq8wuxPf9030htugZy5Utxb+LjGmDBvLc2ddKwfstyx6zv5PEMem9MDD+5m/PHaiDA6/oQ2JRvHeTFWIGbswAv3nx/Af/gLm0k+FDnsKSguajUwBIIQQs7vRH92IXvrjbVp+BdclxpbbizcWPnLWubzldY8AOaA2OnU3cC3nf/Wi0o9/8ZWuKocO6VQqQRyu+dbf4b/4PUzfWbBxU0sMlk0nWSfYKxSL8PBj+M98N+T4txiR9uA7xdhSV/tPC3f+/J285XVPsXqgh4GBbgYH2xkcbGdgYDarB+bx7/90X/Gqb/dW87k/tCPGT2k4FMPc2ehl16E33QHdXY3rqDcp6eoEN6QboJCP8F/6HgxsCBmgzTu8vk1ExiL7v8X/+emHgCIDA20s6Inp6XF0d3u6uz09PY4FPTGrV89i4f6DfPAd749FNuQQ0am+s8jiP38JbBqCnCVNefataLR8bXGkzwNMZwjrPHR1oDf/Br3+VpjVtc2MilFEDn/ehcAoq1fn6enZftplwYKYR9Z0597x1gfdHj2XFsPUtqkzy96Hwrz//TP+P38KHR3p7RCnsHvS4CwQ2/h9GrEC5Sr+oqtC6fEWKPgiYsYi82jxyxfcCnSxYEG89RdtwZ5zY6Cz/Y0n/WRMtWTATqkXcCEzpFf8LIwPFAvp9gI7lIHpPRrsARrYSM5DRwd6y13wu+VJ7L+V5dRIBNPe/jt2697IwODO9TALBaVczvOBd67SXPRQMSjX1N2sahgtfmIAf82NoU+TVi+wFY1V1PSFQNOFAKroj24EdJvuWZJlnHxbcTXgy5XKzjvxoSEDlKSQW2NCG09tSzsP7UX0Z0th3YYwoJdqL5AOZqYCqIYw4cFH0N/cG2LoHVhMY5/FemYyTSZONcw//vNj6O1/eNp7ygikKws0XXgPhQK69C7YMBjKj7dxfk0Kdhkp7QGYQj6/81fZ1eOBgpbi3X34lmnqAip60+3J/aSw1wmNl7O6Y2Z6ABGoVtE7/hCmLm4/VBCnih8dPRLoKncVds6klstCgQqXLFkgceXgcqjzn3ppTMY09O4VoUQin4VBT8fMUwDVkOt/cgD90yNhgvp2hETAjKG+UHUHV9770ZcUCoWh8urVTz96vmZNBAyXfvTT17ap6XDgpmXek2qI/Z8YQB94dIf3lhFIWRp0GhpLFQp59MGVMLB+pzqLIuDu/MOHAVtYsCBmcHC7hqO8enXE/vuPcMNte8vjT5xZDl8+fYbGGChX0Hv/BDaNHmBnZGD6jpnpAYxFH14VJpo/zQp0AmZU1bdV4xeVjn3zZ4BNdHdXWT0QMThoKJeFclkYHDQ8siZXWLBguAS2dO5nvpH3unsFneZZrxo09sGVhCmWKe0HpISZpwAAaBgw2kkMmBH1rrhp6PTysW/6Go88UmBBzwDd3SMUClUKhQrd3cPsv+dafvDjBeZFr72iWKq8fES9m7JiuO2hhE7940+GtYVabF2hyaax+wQ3IgOEhCzQU+snSoh3Djus3nduGj6l/Mb3vNzuPv/77ohDbi8cvvDJaqlq9b7le/PHB16lA+tOySOzhtU7magUnT5UITLo+k0wWgoDZM6nr0x6miPf7THzyqGNhDr/weFnvLKCgBlS7wux7B2tfvKc+PE1jF5/85AqpoB0RCKUFIbDXIDG1SUbE5ZmHCuHpVuyCtHtMvMUQMI6pDpSSsKDZ2Z+DJgq6iuqKmCtShdACVXUexAz7WHPlogka42W02f5U8bMU4DJYTyyjpMYKnR0JZuN0mRsewh02tgyFTZNCJPWOWzsumY7wEiKO8ANavdtkJ5O8HRdh1co5pDuTtTV0oRpy5U/S7yHtiK0pbQeaHtDQg1g5qVBVUP5w+yuiY0sWgkhdHo720MH2KcwA5QiZp4CoGHwa8FuSQq01YRDwDlk/jxoL241vzljc2agAgAosv9eE0uTtxJJlov99gybbaQxBEoRM08BJIwDyHP2CzXzrZgjF0GefyAtp9xTwAxUgFAsxoF7w967J/VALRQGOQedHchhC3eq1mmmE2kadohRwMO0XIsQQoTZ3cgRC9EHHgkZk2ZbX3NbGAmjv889AA5YkMpaIK3tCKSa7JOhWRaoUciiF7dWPyDxbnLMC6GrszWUeoqZmQpgBEbHkJe9EPbdM9mRMV2WcpfwDtqLyEnHQtxiod0UMTMVIJkSyfx5yInHhKrJpt1yKMEYGCkhLzo0bLnaCvc0DczcJ2QMVCqYk08IKy03e7ggSf5/8UlhymeW/twpZq4CSAiDeP5BYfO5weHmXVnZGBgZhaMOQU44BoZHmvdeppmZuSxKDWOgXMac2Rt2gqk2adyclD+Ys05JMlopt/6NlrO6Y2bvECNJ2vDAfZAze2GwCS1nZGHjEPL6RcirXwpDw2DT7tgbLWcTR9qf1NQTWRgcxrzjTcjLjwpLjDeLEhgJk1723A1zzrtab1BvGsgUAEA9WIO54AMwpxsqlfRnUCT5p1zFnPePsPce2QywXSDlrTxNGBM6xAv3w3zun4MlTXsZsbWwfiPyodOQ174CNg02j+dKEenoBLPF/xuBtbBxEDnp5Zh/PwuGRsLraVSCyMLARuS0N2A+eGpzCf+22ryBR+YB6qkpwTvehOk7K6RGVdMTDknYxpW1G5BTX4/51AfD6g9pVNImISUtmyKsCUrwrpMx538gZImq1cZb2JoSrt+InPFmzKc/CKUS2daoz44UTIqv/WzkdWyBNbBxU/AEe/bgP/qVsIz6rM6QY5/uCtrIhtIGr5hz34u8bzEMjzax8Nfau/GFiJkH2B7WwoZNyEnHYK+6EHnpEbBuYyiZiKbJG1gTZGTdRth7d8ylFyDvP2UiNGtK4U8XmQLsiGSQif33xFz2GeST7w/zbNdvCobL2skXwlqcLwKbhsPstTP+Drvky8grjgqbYRuTCf8kkZ5lUdKKtaEfIIJ572I48WX47/wI/fnSoAjtxbA1UW3N0V0Jj6S2hk9SpTo4DPk88uqXIe97K3L0YTA62pwj1dsjJdFvtjLczlDrgG7cFEZdP/sh9PQ3oD/+JXr97bBydVh9oZAPlZjWMB7fbqtxa4NYaPhctQqlSlCe+fOQ170SecuJyF8dFl6rWf3Ulzg0H6lSAEm7W7c2DJKVysiBeyMffy+85y3ob/6ILv0t+ocVsPqpELo4n6zOZiasOwQvUfMUImGzvt3nIYcejLzyRcixR8J+C8K0zeHRifO2CGlr4xQoQGIiVRkeG93spVRSi9FLFRgrQXsb8jevRF6/CDYMoivXwEMr4ZHV6ONPhj7E0MhEaNTRFuYfLNgN2W8BHLRPWKJltzlBWcbK4TNCesYfJoPk9ofHRuvCxFomqHGkIA2q4QoEVq5fm4ZnsnMYAWzICm0aDq/lIuSQA+EFzyXsMuyDJ4iTzeWV0LGObLJagwZLX6lMKEmrhjpJxLdy/VqQ8Van0dausZ1gCGGyB2zEA2sex1fLmGZaykMEbC28URgtg5YY12Rh84xNpZpYQJ34vEhrWfttYMTgq2UeWPM42Ci0ee2WGyiDjX3q49GPYvJ57n/8UdasX0chiqZniZTJprbqtDUhTLJmImU5Luj1f7czIqWpqhSiiDXr13H/449i8vmJ9m1wMzd4IwdABEXJ53KsWzfALSvuwxaKOE35rKaMncapxxaK3LLiPtatGyCfy4UQSBq/uHw6/K4kWRJjuPK2X4H3mEY/mYxJwyRjJFfe9qsk1JPUeL2UKAB4VXIdndx4953csfwe2trbcdnKBk2P85629nbuWH4PN959J7mOTrxqwy1/jcZvlF17EALWCrGP6b/mB0hanlDGs0YQ+q/5AbGPsVY2a/NA42SwsRNiaveeREBOlWJnFzf+9na+e8PP6Jg1m2othZjRdFTjmI5Zs/nuDT/jxt/eTrGzC1ez/vWFoA2UwcaHQLWygCRD4gVyHZ18+JJv8tv776Wre1amBE1INY7p6p7Fb++/lw9f8s0Q+tSyZLU+XwqcfOMVAMaFX4xBRTD5HCNxmZM/+wlWrHo0U4Imoyb8K1Y9ysmf/QQjcRmTz6EiySayWSd4gtqDGM+RBy9Q6Oxg1YYBTjz3Q/x6+R/pmjMP733WMU4xznu893TNmcevl/+RE8/9EKs2DFDo7AjW38rmwp8CJZDc+0/Sp3/bFDO+R0AoHdDYQcVhVSkPjdBuIi5899m873VvAoHRkRG8KtaY1BVXzTRUFec9RoT2jg5QuOgX1/EvF3+DUR9T6OrAiUDeIlH94CCZAmyGaigl8ArOoVUP1RjjFVeu4IaGOfHFL+Njbz2N4444EmyEL5coV6t49TsswxcRbANKDbz3IeXXggSHbSjkcphCEVzM0nvv5rNXXcGNd/0PtqsTW8jjjYQaqVwy+m0kVSGQ5N53YnpayGtQBFfzBEEJxCvGK+VNgyCGE174Yk497gRecfgL2Ldnd6Jisc6t1n2fMr4U+tjY6LR6C+ccHW3tUCiEe0pHez97xjM3SlwqsXLgSW794z18f+lN3PSHu0A9hVndeCNoTfijpMDPmrrJP+kgXQpQHwp5nQiHnIfYYRV87KgODUPs6J4zh4V77ctz99qHfXp2o7utY7MaIiNCXC5z6IEH87fHvIJKpYKZYiVQVbz3dMyew6/v+T03/+432Fy+ZTyBiDA4NsKqgbX86fFVPPD4SgY3bIDIkuvqxEQWJ4SKV2vqwh5JVehTI10KANtWAufHlQCnWACvVKoVtFQO5cbeJ9ap7nbEwNAgx778eG778kWMjo5gpjAU8t4jIrR1d/O9//oJZ3/9i4wObQJjN7+upkYm5ipEEVIskM/lwQgOQke3Jvw1q59S4YdUTIjZgtqeXcaAaDJEIGjNdTqPc2GH98jkkXxhIpVVb2U1vL3S3s5QpUSpNIaZQkF0zlHI5zFRxDkXfZUv/PBybEcHbfPnB6+UsoZ/ViT34gEVDZ3c8SrX0MkNwl8T/G2UhaeE9CkA1G1cV6uT1xC/e0GNARu8g3oNIcf4bpPJA04UQRTinGXNpg2sHx5ifjKyPNl9gdg5Ojs6WbtpI+/88qf4rzuWUZgzFy9QrWni+CnTJwTPmJow15d4J1Ze6qeB1jq7KRV+SKsCwBZKwMRgmVfwBlU/0WlWtrD+4TVVxQpsLI0yMLiJBXPnUYnjSRXB2Dk6Z83m7j8t522fP48Vq/5McbfdiNVPCMJmwt/EoVC9ENcLthFEtiP0KRZ+SMOMsB0iwYzXi6wNr4kKmOTi6/aandh7NqRUrTFUhoZZvWEdRxy0ENXSpDSIqqIonbPncPXN1/Pur36OoWqZ4tx5xChEUZ0wtIYHkPF/2Fyw6z3B+O91H0ixjDV4TvBOUv/QlaAEmvyiiYJo3VvrFEA8oI5HB54CYyZlppnzjnyUI5cvcN73vkX/Dy7FtLVR6OokFoXIhhi4CUKAXWa8TRJJly1fr70x3fKV3hBoS2rWRJjIq2v9gEp9CCSJYoSsDEZYuW7tpAhh7Byd7e1sGh3lXV88n2uW3Uh+7lzUWpyVJPVn6sKfuvGJLZxZcyNb/7cJlbx5FKCeLQe9tmdkTDLdwRhWrnsq2fRi108bu5jO7tnc/5eHePvnz+MPD6+g2LNbYvUFrE2Ef4vsx/h17/q504e0xP00pwJsyWbBKYybWkn6BlHEyoG1aLWK3YUVJ1RDtqlz9lx+euuveOeXP836sWGK83qIxYch/ihJ/ZkWD31ajJR3gneVWggUhBdrWbNpPSOlMXJRhFfdaePlvCeylkJbG5/7/qV87LKLkGKBQnd3Yvmj7Qz41K5jim4xY1JofDn0VJEIoAJiI54a3MS64SEiYzdPme6A2Dnai0Vihbd/7jw+9t2vkevsIFcsjg/3j9e5mC2rHDPL3wy0rgLUYSPL0NgoT23agLUW3QmzHMcxnV2zeOiJNfz1OWfz/V/+gmJPD0QWH0lS5GXrRj+zkKcZaY406C4RJn0qYSwgrlZ4fMM6jrZ2PHO6zU8lI8udc+Zy029u5/Qvnc8TG9dT7JlXF+9bpDa5Y1zwa+UOrfo8W5PW6ARvj8QiGwG849G1T4Kx2x0L8N5jraWtvYP/WHIl/3rx13FRRGH2rIl4P6q3+FsM/mQ0Ha2tALXkeyKbKwee2u47a/F+xXve++XP8J2f/4ho9mzyuQhnmBjc2izTk5wjk/2mpUWzQLB5JogwFjCwFrzbqhgudjGdXd08+sQaTvtCP7fecxfFefNwQpjRFBlkfDZT/Vqeyc+WfYatT4t7gIAC2IiV65/C1U2KqU1e6Zw9l1t+/1ve/oXzWLX2SYo9PSHkyfL7LU/rK0CtYxpZnty0iaGxUdqLRapxjDWGjlmzuei6JfzTRV+hIkph7hxifBLy2FB3VJvKlwl/yxGh6kMtawtSq5dDMTZi3fAg64YH6SwWyedyiLH80ze+xNeu+QFRdzf5fA63rcGtrLPbskQY2QTMSVIjLda6dalQaxgeG2PVurUctN9zWPPkY/zDhRdw0523U5g7D28Eb9mis1tv9eu/M6NViFR1WIyZg2/Bhh2/JcFaQ7VSZrRS4b6HH+CN/f/Cn1c/ForZ8EHg6+P9zer4s5KGlkLG63LHIkHWIOyDqrbsKlMSCkFNscjXbriO3z/0AGvXraU4d06YuWW3mMRdE/7ahzNaCw9Yg3r/pFH0scTStZ6NG6/JEVTAFgrccM9dDJRGyHd3EwuQCyuWjZc1jM9xJYv3WxZVDAiyOhKRB1rayo3rQBDsQmcHmqzYtvk81qyeZ8YgKCKo0QcjRO9L5tS27p4Uklh0TFhBwpjg8GrWPktxzkAEhPsi8PcRU0UkR4tN2gM2v5taKlOTP9QLfCb8MwgxOIeKvyeqrh97IJrVsVKMHITzracAW7GN2p3xqZWt1w3K2ArFiOD8gMvZewxLfj0G+nsiA6F/3JrU5/M3O7LJKzML9VgLIn/kopueCqUQIksV6W3BAGhzshAnQ0WxgsbcCrWuofJLKnEVISIb8slobSxVh4i/AZJsd+Ximx9U9F4iC2RbtGe0LJ7ICM4/EhcLvwMw9C2ygIpwNdYEF5GR0Yoonsii6HV8/foyfYsiA8d5AKt2CeXqGIJN6semba/W7MiOaTlQSyX2RrgSgOXzk/XEe3stS5a46F3HX0suejOVOGYmzBXImEGoIxcZrbo73KX//XJC+O83mwegwjeSJcdbc35AxsxGRBT5BgCLFhmoT3r2hYyQXXX8HZKzf0U19iC2EdeZkTGpqHoiKxr7h533h3P5snLtLxOWfnmv0I/HmE+1bFl0xkxFiawYq5/j8mUlFi0a3ytrc0Hv6zP096t95/G3Ss4eSzV2iNjw1oyMJkMA1GEjo879yTl/JPsvq9A/0S3ePNZfvlwAFTH/Nr6vZyb8Gc1KTcyNCEY/yuXLSizv3Wz5vq1DnSQjZM84/mLJR++iVAleICOj2VB1FHKWavzz+NKlf1uT7fq3bJ3tuXqJpw/j2sw5VOMnsCbZkS4jo6kIOyTGbtga90FAOOSQreKZrRVAUJb3Ct+8eZ2qvJ/ICiKZAmQ0F4ojl7Pi+Zfyxbf+hd5eQ3//VnK8/WzPokURy5bF9ozjvyH56B8pVeOkWC4jI90oMcUoohoviS9ZunhboU+NHaU7hd5ewyFYu2rgFonMS6jELhsbyEg36oisVacPupw9mj2PHaK/fzzrsyU7GvFVDjlE6V9ScfBWnD4RdpfI+gMZKUXVI8YAQ8bJW/jOLzfVMpvb+8jTD3jV6oTOeOXLkOhmVNtwvnWXU8xoThRFUCJjtOpf7y5f+l87Cn1q7NyIb60/8K5FrxHsT/E+h1ePzIwtljJSzrjwW+Or7nR/+bIrajL7dB/dOQFetiymb1HkLll2vY/d6RipYiRLj2Y0HlWPQclZQxz/4zMRfngmm+T1ByXwly/7oXr3RoyMYa1BdYcuJiNjylB1GGMwRnzFnR5fdsv/oW/nhR92ZQp836KI/mVxdOorX0HeLMGa3anEMSJZijRj+lCNk02fhzT2b3NXLPv5M7H8NXat6jM5UeH04w+KI/2/EkVHU644VEwLry+XkQ4U1JHPRer8ClN1b6teccvduyL8sKv7BC9bFtPba8v/+d8Pu6HSIondt8nnLFYE1Wd8ERkZO4eGDd4K+Qjnr3Kl6jHVK265m95euyvCD892FaBQPu0Bcu88/u9V9CtYuzvlaugcZ6nSjMmglmwp5AzODYI5J770VxcB7Eyqc0dMRrgSRoyXLHGcumjvqCifATkNgDh2qEiWLs3YJRSPqGKtxQioXhc7/xEuu+XB2twVnmXB/uTF63WaaM84/kTw54uxL8F7qDqfLMdsJvWcGS1Kklm01hIZNPb3IpzvLl16DfCsrX49kyuMirA48QZ9fcasWvb3ovLPYngRAFUXeu+IybxCxmYoHtSDROTCnswa+/81Iv9RHe65jCVLKvT1GeiH/slbw3ZqrHG9hvb1Gbvy1jcK/kzUn0AussQenA/KIEjmGWYkCupRFMQSGSEyUHUoLFNrvuNjfy2XLysBk2r165laodvionPvPP4FHn0rqm8SkecT2bB5l/Mky7GEUEmRZGJ+phStQVhqTWrrz0uI6WsbEsYOvP5ZjblOTHxVfMmtd45/MsiQZ4om506HgCVl1Ut03HX1LYqiR/VIjDkBr4vwegRG9sAmUZFqTSGyOcnNTm0J+vq9lp0HeAq4X+FWwdwQq//9uLWfSKxMmeDXX9700ddnYKmhf4uc7WmvmhdF7jCUF6pyGOhC0H0E5qN0TOs1Zkw2Y8CTauRxkAdFuE/Re1whfy8X3fTUZu9ctChi/nydilBne/x/NxYHbR8JyuUAAAAASUVORK5CYII="
ICON_180_B64 = "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"
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



# ---------------- legal pages ----------------

def _legal_page(title, body_html, lang="en"):
    back = "← Chat Bash-এ ফিরে যাও" if lang == "bn" else "← Back to Chat Bash"
    eff = "কার্যকর: ১৬ নভেম্বর ২০২৬" if lang == "bn" else "Effective: 16 November 2026"
    return f"""<!DOCTYPE html>
<html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Chat Bash</title>
<style>
  body{{margin:0;background:linear-gradient(168deg,#5c1216,#260509);color:#f9efe4;
       font-family:'Hind Siliguri','Noto Sans Bengali',system-ui,sans-serif;line-height:1.75}}
  .wrap{{max-width:720px;margin:0 auto;padding:28px 20px 56px}}
  a{{color:#eebd55}}
  h1{{font-size:24px;margin:14px 0 4px}}
  h2{{font-size:17px;margin:24px 0 4px;color:#eebd55}}
  p,li{{font-size:15px;color:#f3e2d4}}
  .date{{color:#dda79d;font-size:13px}}
  .back{{display:inline-block;margin-bottom:8px;text-decoration:none}}
</style></head><body><div class="wrap">
<a class="back" href="/">{back}</a>
<p class="date"><a href="?lang=en">English</a> · <a href="?lang=bn">বাংলা</a></p>
<h1>{title}</h1>
<p class="date">{eff}</p>
{body_html}
</div></body></html>"""


PRIVACY_BODY = """
<h2>১. আমরা কী তথ্য সংগ্রহ করি</h2>
<p>Chat Bash চালাতে যতটুকু দরকার, শুধু ততটুকুই: (ক) Google লগইন থেকে পাওয়া তোমার নাম ও ইমেইল ঠিকানা; (খ) তোমার পাঠানো প্রশ্ন ও AI-এর উত্তর — যা চ্যাট হিস্ট্রি হিসেবে তোমার অ্যাকাউন্টে সংরক্ষিত থাকে; (গ) প্রতি মডেলে প্রতি দুই দিনে তুমি কতগুলো মেসেজ পাঠিয়েছ, সেই সংখ্যা (ফ্রি লিমিটের হিসাব রাখতে)। তোমার Google পাসওয়ার্ড আমরা কখনোই পাই না বা সংরক্ষণ করি না — লগইন সম্পূর্ণ Google-এর নিজস্ব সুরক্ষিত পদ্ধতিতে হয়।</p>
<h2>২. তথ্য কেন ব্যবহার করি</h2>
<p>শুধু সেবাটা দিতে: তোমাকে চেনা, তোমার হিস্ট্রি দেখানো, ফ্রি লিমিট প্রয়োগ করা এবং অপব্যবহার ঠেকানো।</p>
<h2>৩. AI প্রসেসিং</h2>
<p>উত্তর তৈরি করার জন্য তোমার প্রশ্ন ক্লাউড-ভিত্তিক AI প্রযুক্তির মাধ্যমে প্রসেস করা হয়। তাই প্রশ্নের মধ্যে নিজের বা অন্যের ব্যক্তিগত ও সংবেদনশীল তথ্য (যেমন পাসওয়ার্ড, আর্থিক তথ্য, ঠিকানা) না লেখাই ভালো।</p>
<h2>৪. তথ্য শেয়ার করা</h2>
<p>আমরা তোমার তথ্য বিক্রি করি না এবং বিজ্ঞাপনের উদ্দেশ্যে কাউকে দিই না। আইনি বাধ্যবাধকতা থাকলে ছাড়া তোমার তথ্য তৃতীয় পক্ষের কাছে প্রকাশ করা হয় না।</p>
<h2>৫. সংরক্ষণ ও মুছে ফেলা</h2>
<p>তোমার চ্যাট হিস্ট্রি তোমার অ্যাকাউন্টের সাথে সংরক্ষিত থাকে, যাতে যেকোনো ডিভাইস থেকে লগইন করে তুমি তা দেখতে পারো। তুমি চাইলে যেকোনো সময় <b>nasheedulislam@gmail.com</b> ঠিকানায় ইমেইল করে তোমার সংরক্ষিত হিস্ট্রি ও ব্যবহারের হিসাব মুছে ফেলতে বলতে পারো — আমরা তা মুছে দেব।</p>
<h2>৬. কুকি ও লোকাল স্টোরেজ</h2>
<p>লগইন অবস্থা ধরে রাখতে এবং চ্যাটের স্থানীয় ক্যাশ রাখতে তোমার ব্রাউজারের লোকাল স্টোরেজ ব্যবহার করা হয়। কোনো বিজ্ঞাপন-ট্র্যাকিং কুকি ব্যবহার করা হয় না।</p>
<h2>৭. নিরাপত্তা</h2>
<p>সব সংযোগ এনক্রিপ্টেড (HTTPS)। তবু মনে রাখবে, ইন্টারনেটে কোনো সেবাই শতভাগ ঝুঁকিমুক্ত নয়।</p>
<h2>৮. শিশুদের ক্ষেত্রে</h2>
<p>এই সেবা ১৩ বছরের কম বয়সী শিশুদের জন্য নয়।</p>
<h2>৯. পরিবর্তন</h2>
<p>এই নীতিতে পরিবর্তন এলে এই পেজেই হালনাগাদ করে জানানো হবে।</p>
<h2>১০. যোগাযোগ</h2>
<p>প্রাইভেসি নিয়ে কোনো প্রশ্ন থাকলে ইমেইল করো: <b>nasheedulislam@gmail.com</b></p>
"""

TERMS_BODY = """
<h2>১. সেবার বর্ণনা</h2>
<p>Chat Bash একটি সম্পূর্ণ ফ্রি AI চ্যাট সেবা, বর্তমানে পরীক্ষামূলক (ডেমো) পর্যায়ে আছে। সেবাটি ব্যবহার করলেই তুমি এই শর্তাবলি মেনে নিচ্ছো বলে ধরা হবে।</p>
<h2>২. অ্যাকাউন্ট</h2>
<p>সেবাটি ব্যবহার করতে Google অ্যাকাউন্ট দিয়ে লগইন করতে হয়। নিজের অ্যাকাউন্টের নিরাপত্তা রক্ষা করা তোমার নিজের দায়িত্ব।</p>
<h2>৩. ফ্রি লিমিট</h2>
<p>প্রতি মডেলে প্রতি দুই দিনে নির্দিষ্ট সংখ্যক ফ্রি মেসেজ পাওয়া যায়। সবার জন্য সুবিধা রাখতে একসাথে সর্বোচ্চ ১০ জন ব্যবহারকারী অ্যাপে থাকতে পারে, আর প্রতি ঘণ্টায় ১০ মিনিট করে চ্যাট করা যায়। এই সীমা ও মডেলের তালিকা যেকোনো সময় পরিবর্তন হতে পারে।</p>
<h2>৪. উত্তরের নির্ভুলতা</h2>
<p>AI-এর উত্তর ভুল হতে পারে। গুরুত্বপূর্ণ বিষয়ে যাচাই না করে শুধু এই উত্তরের ওপর নির্ভর করে সিদ্ধান্ত নেবে না। এই সেবার উত্তর কোনোভাবেই চিকিৎসা, আইনি বা আর্থিক পরামর্শ নয় — এমন প্রয়োজনে সংশ্লিষ্ট পেশাদারের পরামর্শ নাও।</p>
<h2>৫. নিষিদ্ধ ব্যবহার</h2>
<p>এই সেবা দিয়ে অবৈধ কাজ, ক্ষতিকর বা বিদ্বেষমূলক কনটেন্ট তৈরি, স্প্যাম, অন্যের ক্ষতি, কিংবা সেবার নিরাপত্তা ভাঙা বা ফ্রি লিমিট ফাঁকি দেওয়ার চেষ্টা করা যাবে না। এমন ব্যবহার দেখলে অ্যাকাউন্টের প্রবেশ বন্ধ করে দেওয়া হতে পারে।</p>
<h2>৬. দায়বদ্ধতার সীমা</h2>
<p>সেবাটি "যেমন আছে" ভিত্তিতে বিনামূল্যে দেওয়া হচ্ছে। প্রযোজ্য আইন যতটুকু অনুমতি দেয়, সেবা ব্যবহার থেকে উদ্ভূত কোনো ক্ষতির জন্য Chat Bash বা এর নির্মাতা দায়ী থাকবে না।</p>
<h2>৭. সেবা পরিবর্তন বা বন্ধ</h2>
<p>যেকোনো সময় সেবার কোনো অংশ পরিবর্তন, স্থগিত বা সম্পূর্ণ বন্ধ করে দেওয়া হতে পারে, পূর্বঘোষণা ছাড়াই।</p>
<h2>৮. মেধাসম্পত্তি</h2>
<p>Chat Bash নাম, লোগো ও ডিজাইন এর নির্মাতার সম্পত্তি। তুমি যা লিখছো (তোমার প্রশ্ন), তার অধিকার তোমারই।</p>
<h2>৯. প্রযোজ্য আইন</h2>
<p>এই শর্তাবলি বাংলাদেশের আইন অনুযায়ী ব্যাখ্যা ও প্রয়োগ হবে।</p>
<h2>১০. যোগাযোগ</h2>
<p>যেকোনো প্রশ্নে ইমেইল করো: <b>nasheedulislam@gmail.com</b></p>
"""


def _pick_lang():
    lang = request.args.get("lang") or request.cookies.get("cb_lang") or "en"
    return "bn" if lang == "bn" else "en"


def _legal_response(title_en, title_bn, body_en, body_bn):
    lang = _pick_lang()
    resp = make_response(_legal_page(title_bn if lang == "bn" else title_en,
                                     body_bn if lang == "bn" else body_en, lang))
    if request.args.get("lang"):
        resp.set_cookie("cb_lang", lang, max_age=365 * 24 * 3600, path="/", samesite="Lax")
    return resp


@app.get("/privacy")
def privacy():
    return _legal_response("Privacy Policy", "প্রাইভেসি নীতি", PRIVACY_BODY_EN, PRIVACY_BODY)


@app.get("/terms")
def terms():
    return _legal_response("Terms of Service", "শর্তাবলি", TERMS_BODY_EN, TERMS_BODY)
PRIVACY_BODY_EN = """
<h2>1. What information we collect</h2>
<p>Only as much as Chat Bash needs to run: (a) your name and email address from Google sign-in; (b) the questions you send and the AI's answers, stored as chat history on your account; (c) how many messages you have sent with each model in each two-day period (to keep track of the free limit). We never receive or store your Google password — sign-in happens entirely through Google's own secure system.</p>
<h2>2. Why we use it</h2>
<p>Only to provide the service: recognising you, showing your history, applying the free limit and preventing abuse.</p>
<h2>3. AI processing</h2>
<p>To generate answers, your questions are processed using cloud-based AI technology. So it is best not to write personal or sensitive information about yourself or others (such as passwords, financial details or addresses) in your questions.</p>
<h2>4. Sharing information</h2>
<p>We do not sell your information and do not give it to anyone for advertising. Your information is not disclosed to third parties except where legally required.</p>
<h2>5. Storage and deletion</h2>
<p>Your chat history is stored with your account so you can view it by signing in from any device. You can ask for your stored history and usage counts to be deleted at any time by emailing <b>nasheedulislam@gmail.com</b> — we will delete them.</p>
<h2>6. Cookies and local storage</h2>
<p>Your browser's local storage is used to keep your sign-in state and a local cache of your chats. No advertising-tracking cookies are used.</p>
<h2>7. Security</h2>
<p>All connections are encrypted (HTTPS). Even so, remember that no internet service is completely risk-free.</p>
<h2>8. Children</h2>
<p>This service is not for children under 13.</p>
<h2>9. Changes</h2>
<p>If this policy changes, the update will be posted on this page.</p>
<h2>10. Contact</h2>
<p>For any privacy question, email: <b>nasheedulislam@gmail.com</b></p>
"""

TERMS_BODY_EN = """
<h2>1. About the service</h2>
<p>Chat Bash is a completely free AI chat service, currently at an experimental (demo) stage. By using the service you are taken to accept these terms.</p>
<h2>2. Account</h2>
<p>You need to sign in with a Google account to use the service. Keeping your own account secure is your responsibility.</p>
<h2>3. Free limits</h2>
<p>Each model gives a set number of free messages every two days. To keep things fair for everyone, at most 10 users can be in the app at the same time, and chatting is limited to 10 minutes per hour. These limits and the list of models may change at any time.</p>
<h2>4. Accuracy of answers</h2>
<p>AI answers can be wrong. Do not make decisions on important matters based only on these answers without verifying them. Answers from this service are not medical, legal or financial advice — consult the relevant professional for such needs.</p>
<h2>5. Prohibited use</h2>
<p>You may not use this service for illegal activity, creating harmful or hateful content, spam, harming others, breaking the service's security, or trying to bypass the free limits. Accounts doing so may be blocked.</p>
<h2>6. Limitation of liability</h2>
<p>The service is provided free of charge, "as is". To the extent permitted by applicable law, Chat Bash and its creator are not liable for any loss arising from use of the service.</p>
<h2>7. Changes or shutdown</h2>
<p>Any part of the service may be changed, suspended or shut down completely at any time, without prior notice.</p>
<h2>8. Intellectual property</h2>
<p>The Chat Bash name, logo and design are the property of its creator. What you write (your questions) remains yours.</p>
<h2>9. Governing law</h2>
<p>These terms are interpreted and applied under the laws of Bangladesh.</p>
<h2>10. Contact</h2>
<p>For any question, email: <b>nasheedulislam@gmail.com</b></p>
"""



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


# ---------------- app session tokens (browser sign-in handoff) ----------------
# The app cannot turn a Firebase ID token into a Firebase session, so after
# the one-time-code exchange the server issues its own signed session token
# (30 days). The signing key is derived from the Upstash token, never stored.

def _session_secret():
    return hashlib.sha256(("cb-session:" + UPSTASH_TOKEN).encode()).digest()


def make_session(user):
    payload = {"uid": user["uid"], "email": user.get("email", ""),
               "name": user.get("name", ""),
               "exp": int(time.time()) + 30 * 24 * 3600}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = hmac.new(_session_secret(), raw.encode(), hashlib.sha256).hexdigest()
    return raw + "." + sig


def verify_session(token):
    if not UPSTASH_TOKEN:
        return None
    try:
        raw, sig = (token or "").split(".", 1)
        good = hmac.new(_session_secret(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, sig):
            return None
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return {"uid": payload["uid"], "email": payload.get("email", ""),
                "name": payload.get("name", "")}
    except Exception:  # noqa: BLE001
        return None


OWNER_EMAIL = os.environ.get(
    "OWNER_EMAIL", "nasheedulislam@gmail.com").strip().lower()


def is_owner(user):
    """The owner's own account is exempt from every limit."""
    return bool(user) and (user.get("email") or "").strip().lower() == OWNER_EMAIL


def authed_user(req):
    """Return user dict; 'DEV' user when auth disabled; None when invalid."""
    if not AUTH_REQUIRED:
        return {"uid": "dev", "email": "", "name": ""}
    data = req.get_json(force=True, silent=True) or {}
    token = data.get("idToken", "")
    user = verify_id_token(token)
    if user is None and token:
        user = verify_session(token)
    return user


@app.post("/me")
def me():
    user = authed_user(request)
    if user is None:
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"email": user["email"], "owner": is_owner(user),
                    "remaining": remaining_map(user["uid"], is_owner(user))})


def login_code_create(user):
    """One-time code so a browser sign-in can hand the session to the app.
    The code lives 5 minutes, is single-use, and the app exchanges it for a
    server session token (see make_session)."""
    if not HISTORY_ON:
        return None
    try:
        code = "".join(secrets.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(8))
        payload = json.dumps({"uid": user["uid"], "email": user["email"],
                              "name": user["name"]})
        _redis("SET", f"cblogin:{code}", payload)
        _redis("EXPIRE", f"cblogin:{code}", "300")
        return code
    except Exception as e:  # noqa: BLE001
        print("login code create failed:", e, flush=True)
        return None


@app.post("/auth/code")
def auth_code():
    user = authed_user(request)
    if user is None:
        return jsonify({"error": "unauthorized"}), 401
    code = login_code_create(user)
    if not code:
        return jsonify({"error": "unavailable"}), 503
    return jsonify({"code": code})


@app.post("/auth/exchange")
def auth_exchange():
    data = request.get_json(force=True, silent=True) or {}
    code = (data.get("code") or "").strip().upper()
    if not code or not HISTORY_ON:
        return jsonify({"error": "bad code"}), 400
    try:
        raw = _redis("GET", f"cblogin:{code}")
        if not raw:
            return jsonify({"error": "expired"}), 404
        _redis("DEL", f"cblogin:{code}")
        info = json.loads(raw)
        user = {"uid": info["uid"], "email": info.get("email", ""),
                "name": info.get("name", "")}
        return jsonify({"uid": user["uid"], "email": user["email"],
                        "name": user["name"], "session": make_session(user)})
    except Exception as e:  # noqa: BLE001
        print("login exchange failed:", e, flush=True)
        return jsonify({"error": "failed"}), 500


@app.post("/enter")
def enter():
    user = authed_user(request)
    if user is None:
        return jsonify({"error": "unauthorized"}), 401
    if is_owner(user):
        return jsonify({"ok": True, "owner": True})
    state, resume = session_state(user["uid"])
    if state == "over":
        presence_release(user["uid"])
        return jsonify({"ok": False, "reason": "break", "resume": resume}), 403
    if not presence_admit(user["uid"]):
        return jsonify({"ok": False, "reason": "overloaded"}), 503
    return jsonify({"ok": True})


@app.post("/history/get")
def history_get():
    user = authed_user(request)
    if user is None:
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"chats": history_load(user["uid"]), "on": HISTORY_ON})


@app.post("/history/save")
def history_save_route():
    user = authed_user(request)
    if user is None:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True) or {}
    return jsonify({"ok": history_save(user["uid"], data.get("chats") or [])})


@app.post("/chat")
def chat():
    data = request.get_json(force=True, silent=True) or {}
    message = (data.get("message") or "").strip()
    lang = "bn" if data.get("lang") == "bn" else "en"
    if not message:
        return jsonify({"reply": canned(lang, "empty")})
    user = authed_user(request)
    if user is None:
        return jsonify({"reply": canned(lang, "login_first")}), 401
    owner = is_owner(user)
    allowed, resume = (True, 0) if owner else session_touch(user["uid"])
    if not allowed:
        presence_release(user["uid"])
        return jsonify({
            "error": "session_over",
            "reply": canned(lang, "session_over"),
            "resume": resume,
            "remaining": remaining_map(user["uid"], is_owner(user)),
        }), 403
    model_key = data.get("model") or DEFAULT_MODEL
    if model_key not in MODELS:
        model_key = DEFAULT_MODEL
    if CREDITS_ON and not owner:
        used = credits_used(user["uid"], model_key)
        if used >= MODELS[model_key]["cap"]:
            return jsonify({
                "reply": canned(lang, "limit", name=MODELS[model_key]['name']),
                "remaining": remaining_map(user["uid"], is_owner(user)),
            }), 429
    try:
        reply = respond(message, data.get("history") or [], model_key)
    except Exception as e:  # noqa: BLE001 — details stay in the server log only
        print("LLM error:", e, flush=True)
        return jsonify({"reply": canned(lang, "generic"), "remaining": remaining_map(user["uid"], is_owner(user))})
    if CREDITS_ON and not owner:
        credits_consume(user["uid"], model_key)
    return jsonify({"reply": reply, "remaining": remaining_map(user["uid"], is_owner(user))})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
