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
  :root{--bg:#131314;--panel:#1e1f20;--bubble:#28292a;--menu:#2b2c2d;--text:#e3e3e3;--muted:#9aa0a6;
        --accent:#57bb8a;--accent-ink:#06281a;--line:#333537}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Hind Siliguri','Noto Sans Bengali',system-ui,sans-serif;background:var(--bg);color:var(--text);
       display:flex;flex-direction:column;height:100dvh;overflow:hidden}
  svg{display:block}
  .icobtn{background:none;border:none;border-radius:50%;width:40px;height:40px;flex:0 0 40px;
          display:flex;align-items:center;justify-content:center;cursor:pointer;color:var(--text)}
  .icobtn:hover{background:var(--bubble)}
  .icobtn svg{width:22px;height:22px}
  /* ---------- top bar ---------- */
  header{display:flex;align-items:center;gap:4px;padding:8px 10px;width:100%;max-width:900px;margin:0 auto;position:relative;z-index:15}
  .modelwrap{display:flex;align-items:center;gap:2px;min-width:0;margin-right:auto}
  #model{appearance:none;-webkit-appearance:none;background:none;border:none;color:var(--text);
         font-family:inherit;font-size:17px;font-weight:600;padding:8px 2px;cursor:pointer;max-width:46vw}
  #model option{background:var(--panel);color:var(--text);font-size:15px}
  .chev{color:var(--muted);pointer-events:none}
  /* ---------- account dropdown ---------- */
  #acct{position:absolute;top:54px;right:10px;background:var(--menu);border:1px solid var(--line);border-radius:16px;
        padding:14px 16px;width:min(300px,86vw);box-shadow:0 12px 34px rgba(0,0,0,.55);display:none;z-index:40}
  #acct.open{display:block}
  #acct .who{font-size:14.5px;font-weight:600;margin-bottom:2px;word-break:break-all}
  #acct .sub{font-size:12.5px;color:var(--muted);margin-bottom:10px}
  #acct .remline{display:flex;justify-content:space-between;font-size:13.5px;padding:5px 0;border-top:1px solid var(--line)}
  #acct .note{font-size:12px;color:var(--muted);margin-top:10px;line-height:1.5}
  #logout{margin-top:12px;width:100%;background:var(--bubble);color:var(--text);border:1px solid var(--line);
          border-radius:999px;padding:9px;font-family:inherit;font-size:14px;cursor:pointer}
  /* ---------- demo strip ---------- */
  #demo{background:#2b2108;color:#f5c04e;border-top:1px solid #4a3a12;border-bottom:1px solid #4a3a12;
        text-align:center;font-size:12.5px;line-height:1.45;padding:6px 14px}
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
        padding:8px 14px;font-size:13.5px;cursor:pointer;font-family:inherit}
  .chip:hover{background:var(--panel)}
  /* ---------- input pill ---------- */
  form{display:flex;align-items:center;gap:2px;background:var(--panel);border-radius:28px;
       padding:5px;width:calc(100% - 24px);max-width:876px;margin:10px auto 4px}
  #q{flex:1;padding:10px 6px;border:none;font-size:16px;font-family:inherit;background:transparent;color:var(--text);min-width:0}
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
  #plusmenu button{display:flex;gap:10px;align-items:center;width:100%;background:none;border:none;color:var(--text);
                   font-family:inherit;font-size:14.5px;padding:10px 12px;border-radius:10px;cursor:pointer;text-align:left}
  #plusmenu button:hover{background:var(--bubble)}
  /* ---------- drawer / history ---------- */
  #scrim{position:fixed;inset:0;background:rgba(0,0,0,.55);opacity:0;pointer-events:none;transition:opacity .25s;z-index:25}
  #scrim.on{opacity:1;pointer-events:auto}
  #drawer{position:fixed;top:0;left:0;bottom:0;width:min(302px,84vw);background:var(--panel);z-index:30;
          transform:translateX(-105%);transition:transform .28s ease;display:flex;flex-direction:column;
          border-right:1px solid var(--line)}
  #drawer.on{transform:translateX(0)}
  #drawer .dhead{padding:14px 14px 8px;display:flex;align-items:center;justify-content:space-between}
  #drawer .dhead b{font-size:16.5px}
  #newchat{margin:4px 14px 10px;background:var(--bubble);color:var(--text);border:1px solid var(--line);
           border-radius:999px;padding:10px;font-family:inherit;font-size:14.5px;cursor:pointer}
  #histlist{flex:1;overflow-y:auto;padding:2px 8px}
  .hitem{padding:10px 10px;border-radius:12px;cursor:pointer}
  .hitem:hover,.hitem.cur{background:var(--bubble)}
  .hitem .t{font-size:14.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .hitem .d{font-size:11.5px;color:var(--muted);margin-top:1px}
  .hempty{color:var(--muted);font-size:13.5px;padding:12px 10px;line-height:1.6}
  #drawer .dfoot{padding:12px 16px;border-top:1px solid var(--line);font-size:13px;color:var(--muted);word-break:break-all}
  /* ---------- toast ---------- */
  #toast{position:fixed;left:50%;bottom:96px;transform:translateX(-50%) translateY(20px);background:var(--menu);
         color:var(--text);border:1px solid var(--line);font-size:13.5px;padding:9px 16px;border-radius:999px;
         opacity:0;pointer-events:none;transition:all .25s;z-index:50;max-width:88vw;text-align:center}
  #toast.on{opacity:1;transform:translateX(-50%) translateY(0)}
  /* ---------- login ---------- */
  #login{position:fixed;inset:0;background:rgba(0,0,0,.72);display:none;align-items:center;justify-content:center;z-index:60;padding:16px}
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
  <button class="icobtn" id="burger" type="button" aria-label="মেনু">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h16"/></svg>
  </button>
  <div class="modelwrap">
    <select id="model" aria-label="মডেল"></select>
    <svg class="chev" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 9l6 6 6-6"/></svg>
  </div>
  <button class="icobtn" id="newtop" type="button" aria-label="নতুন চ্যাট">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
  </button>
  <button class="icobtn" id="dots" type="button" aria-label="আরও">
    <svg viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="5" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="12" cy="19" r="1.7"/></svg>
  </button>
  <div id="acct">
    <div class="who" id="acctname">লগইন করা নেই</div>
    <div class="sub">এই সপ্তাহে বাকি ফ্রি মেসেজ</div>
    <div id="acctrem"></div>
    <p class="note">⚠ ডেমো মোড — অ্যাপটা এখনো তৈরির কাজ চলছে। Chat Bash ভুল করতে পারে, গুরুত্বপূর্ণ তথ্য যাচাই করে নিও।</p>
    <button id="logout" type="button">লগআউট</button>
  </div>
</header>
<div id="demo">⚠ ডেমো মোড — এই অ্যাপটা এখনো তৈরির কাজ চলছে। মাঝে মাঝে ভুল উত্তর বা একটু দেরি হতে পারে।</div>
<div id="chat">
  <div class="msg a">আসসালামু আলাইকুম! আমি Chat Bash। বাংলাদেশ নিয়ে যেকোনো প্রশ্ন করো — ইতিহাস, খাবার, খেলা, ভ্রমণ, আজকের খবর বা দাম — চেষ্টা করব সঠিক উত্তর দিতে।</div>
</div>
<div class="chips" id="chips">
  <button class="chip" type="button">বাংলাদেশের জাতীয় মাছ কোনটি?</button>
  <button class="chip" type="button">পদ্মা সেতু সম্পর্কে বলো</button>
  <button class="chip" type="button">আজ ঢাকার আবহাওয়া কেমন?</button>
  <button class="chip" type="button">কক্সবাজার ভ্রমণের টিপস দাও</button>
</div>
<form id="f">
  <button class="icobtn" id="plus" type="button" aria-label="আরও অপশন">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>
  </button>
  <input id="q" placeholder="Chat Bash-কে প্রশ্ন করো…" autocomplete="off">
  <button class="icobtn" id="mic" type="button" aria-label="ভয়েসে লেখো">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0014 0M12 18v3"/></svg>
  </button>
  <button id="send" type="submit" aria-label="পাঠাও">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>
  </button>
</form>
<p class="fineprint">Chat Bash ভুল করতে পারে — গুরুত্বপূর্ণ তথ্য যাচাই করে নিও। সম্পূর্ণ ফ্রি প্রযুক্তিতে তৈরি, কোনো কোটি টাকার প্রকল্প নয়।</p>

<div id="plusmenu">
  <button type="button" id="pm-new">✎&nbsp; নতুন চ্যাট</button>
  <button type="button" id="pm-hist">🕘&nbsp; আগের চ্যাটগুলো</button>
  <button type="button" id="pm-model">🔀&nbsp; অন্য মডেল ব্যবহার করো</button>
</div>

<div id="scrim"></div>
<aside id="drawer">
  <div class="dhead"><b>🇧🇩 Chat Bash</b>
    <button class="icobtn" id="dclose" type="button" aria-label="বন্ধ করো">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
    </button>
  </div>
  <button id="newchat" type="button">✎ নতুন চ্যাট শুরু করো</button>
  <div id="histlist"></div>
  <div class="dfoot" id="dfoot"></div>
</aside>

<div id="toast"></div>

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
const $=id=>document.getElementById(id);
const chat=$('chat'),form=$('f'),q=$('q'),send=$('send'),mic=$('mic'),
      modelSel=$('model'),loginEl=$('login'),chipsEl=$('chips'),
      drawer=$('drawer'),scrim=$('scrim'),acct=$('acct'),plusmenu=$('plusmenu');
const GREETING='আসসালামু আলাইকুম! আমি Chat Bash। বাংলাদেশ নিয়ে যেকোনো প্রশ্ন করো — ইতিহাস, খাবার, খেলা, ভ্রমণ, আজকের খবর বা দাম — চেষ্টা করব সঠিক উত্তর দিতে।';
let currentUser=null, remaining={}, pairs=[], currentId=null;

const IC={
 up:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M7 10v11H4a1 1 0 01-1-1v-9a1 1 0 011-1h3zm0 0l4.2-7.1c1.6 0 2.8 1.3 2.6 2.9L13.2 10H20a2 2 0 012 2.4l-1.2 6.8A2 2 0 0118.8 21H7"/></svg>',
 down:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M17 14V3h3a1 1 0 011 1v9a1 1 0 01-1 1h-3zm0 0l-4.2 7.1c-1.6 0-2.8-1.3-2.6-2.9L10.8 14H4a2 2 0 01-2-2.4l1.2-6.8A2 2 0 015.2 3H17"/></svg>',
 regen:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 11-2.6-6.4M21 4v5h-5"/></svg>',
 share:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="12" r="2.5"/><circle cx="17.5" cy="5.5" r="2.5"/><circle cx="17.5" cy="18.5" r="2.5"/><path d="M8.2 10.8l7-4M8.2 13.2l7 4"/></svg>',
 copy:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>',
 speak:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5L6 9H3v6h3l5 4V5z"/><path d="M15.5 8.5a5 5 0 010 7M18.4 5.6a9 9 0 010 12.8"/></svg>'};

MODELS.forEach(m=>{const o=document.createElement('option');o.value=m.key;o.textContent=m.name;modelSel.appendChild(o);});
modelSel.value='__DEFAULT_MODEL__';

let toastTimer=null;
function toast(t){const el=$('toast');el.textContent=t;el.classList.add('on');
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.remove('on'),1800);}

/* ---------------- history storage ---------------- */
function loadChats(){try{return JSON.parse(localStorage.getItem('cb_chats')||'[]');}catch(e){return[];}}
function storeChats(c){try{localStorage.setItem('cb_chats',JSON.stringify(c.slice(0,30)));}catch(e){}}
function persist(){
  if(!pairs.length) return;
  const all=loadChats();
  if(!currentId){ currentId='c'+Date.now(); all.unshift({id:currentId,ts:Date.now(),pairs:pairs}); }
  else{ const c=all.find(x=>x.id===currentId); if(c){c.pairs=pairs;c.ts=Date.now();} else all.unshift({id:currentId,ts:Date.now(),pairs:pairs}); }
  storeChats(all); renderHist();
}
function renderHist(){
  const all=loadChats(), list=$('histlist'); list.innerHTML='';
  if(!all.length){ list.innerHTML='<div class="hempty">এখনো কোনো আগের চ্যাট নেই। প্রশ্ন করলেই এখানে হিস্ট্রি জমা হবে।</div>'; return; }
  all.forEach(c=>{
    const d=document.createElement('div'); d.className='hitem'+(c.id===currentId?' cur':'');
    const t=document.createElement('div'); t.className='t'; t.textContent=(c.pairs&&c.pairs[0]?c.pairs[0].u:'নতুন চ্যাট');
    const s=document.createElement('div'); s.className='d'; s.textContent=new Date(c.ts).toLocaleDateString('bn-BD',{day:'numeric',month:'long'})+' · '+(c.pairs?c.pairs.length:0)+'টি প্রশ্ন';
    d.appendChild(t); d.appendChild(s);
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
function newChat(){
  pairs=[]; currentId=null; chat.innerHTML='';
  const g=document.createElement('div'); g.className='msg a'; g.textContent=GREETING; chat.appendChild(g);
  chipsEl.style.display='flex'; q.value=''; syncSend(); q.focus(); renderHist();
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
  row.appendChild(mk(IC.up,'ভালো লেগেছে',b=>{pairs[i].f=pairs[i].f==='up'?'':'up';persist();markFb(el,i);},'up'));
  row.appendChild(mk(IC.down,'ভালো লাগেনি',b=>{pairs[i].f=pairs[i].f==='down'?'':'down';persist();markFb(el,i);},'down'));
  row.appendChild(mk(IC.regen,'আবার উত্তর দাও',b=>regen(i,el,b)));
  row.appendChild(mk(IC.share,'শেয়ার',()=>shareText(pairs[i].a)));
  row.appendChild(mk(IC.copy,'কপি',()=>copyText(pairs[i].a)));
  row.appendChild(mk(IC.speak,'শুনুন',b=>speak(pairs[i].a,b)));
  el.appendChild(row);
}
function copyText(t){
  if(navigator.clipboard&&navigator.clipboard.writeText){ navigator.clipboard.writeText(t).then(()=>toast('কপি হয়েছে ✓')).catch(()=>toast('কপি হয়নি')); }
  else{ const ta=document.createElement('textarea'); ta.value=t; document.body.appendChild(ta); ta.select();
        try{document.execCommand('copy'); toast('কপি হয়েছে ✓');}catch(e){toast('কপি হয়নি');} ta.remove(); }
}
function shareText(t){
  if(navigator.share){ navigator.share({text:t}).catch(()=>{}); }
  else copyText(t);
}
let speaking=false;
function speak(t,btn){
  if(!('speechSynthesis' in window)){ toast('এই ডিভাইসে শোনানোর সুবিধা নেই'); return; }
  if(speaking){ speechSynthesis.cancel(); speaking=false; return; }
  t=t.replace(/\*\*/g,'').replace(/[*#`]/g,'');
  const u=new SpeechSynthesisUtterance(t); u.lang='bn-BD';
  u.onend=()=>{speaking=false;}; u.onerror=()=>{speaking=false;};
  speechSynthesis.speak(u); speaking=true;
}
async function ask(text,histPairs){
  let token=''; if(currentUser) token=await currentUser.getIdToken();
  const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({message:text,history:histPairs.map(p=>[p.u,p.a]),model:modelSel.value,idToken:token})});
  return {status:r.status,data:await r.json()};
}
async function regen(i,el,btn){
  if(btn.disabled) return;
  const old=pairs[i].a; btn.disabled=true;
  const txtEl=el.querySelector('.txt');
  el.classList.add('dots');
  if(txtEl) txtEl.textContent='ভাবছি…';
  try{
    const {status,data}=await ask(pairs[i].u,pairs.slice(0,i));
    el.classList.remove('dots');
    if(status===200&&data.reply){ pairs[i].a=data.reply; setRich(el,data.reply);
      if(data.remaining){remaining=data.remaining; renderAcct();} persist(); }
    else{ setRich(el,old); toast(data.reply||'হয়নি, আবার চেষ্টা করো'); }
  }catch(e){ el.classList.remove('dots'); setRich(el,old); toast('নেটওয়ার্কে সমস্যা'); }
  btn.disabled=false;
}

/* ---------------- account / menus ---------------- */
function renderAcct(){
  const box=$('acctrem'); box.innerHTML='';
  MODELS.forEach(m=>{ const r=remaining[m.key];
    const line=document.createElement('div'); line.className='remline';
    line.innerHTML='<span>'+m.name+'</span><span>'+(r===undefined?('সপ্তাহে '+m.cap+'টি'):('বাকি '+r+' / '+m.cap))+'</span>';
    box.appendChild(line); });
  if(currentUser){ $('acctname').textContent=currentUser.displayName||currentUser.email||'';
    $('dfoot').textContent=currentUser.email||''; $('logout').style.display='block'; }
  else{ $('acctname').textContent='লগইন করা নেই'; $('dfoot').textContent=''; $('logout').style.display='none'; }
}
function closeMenus(){ acct.classList.remove('open'); plusmenu.classList.remove('open'); }
$('dots').onclick=e=>{ e.stopPropagation(); renderAcct(); acct.classList.toggle('open'); plusmenu.classList.remove('open'); };
$('logout').onclick=()=>{ closeMenus(); if(AUTH_ON) firebase.auth().signOut(); };
document.addEventListener('click',e=>{ if(!acct.contains(e.target)&&e.target!==$('dots')) acct.classList.remove('open');
  if(!plusmenu.contains(e.target)&&e.target!==$('plus')&&!$('plus').contains(e.target)) plusmenu.classList.remove('open'); });

/* ---------------- drawer ---------------- */
function openDrawer(){ renderHist(); drawer.classList.add('on'); scrim.classList.add('on'); }
function closeDrawer(){ drawer.classList.remove('on'); scrim.classList.remove('on'); }
$('burger').onclick=openDrawer; $('dclose').onclick=closeDrawer; scrim.onclick=closeDrawer;
$('newchat').onclick=()=>{ newChat(); closeDrawer(); };
$('newtop').onclick=()=>{ newChat(); };

/* ---------------- plus menu ---------------- */
$('plus').onclick=e=>{ e.stopPropagation(); plusmenu.classList.toggle('open'); acct.classList.remove('open'); };
$('pm-new').onclick=()=>{ closeMenus(); newChat(); };
$('pm-hist').onclick=()=>{ closeMenus(); openDrawer(); };
$('pm-model').onclick=()=>{ closeMenus(); const i=MODELS.findIndex(m=>m.key===modelSel.value);
  modelSel.value=MODELS[(i+1)%MODELS.length].key; toast('মডেল: '+MODELS[(i+1)%MODELS.length].name); q.focus(); };

/* ---------------- chips ---------------- */
document.querySelectorAll('.chip').forEach(c=>c.onclick=()=>{ q.value=c.textContent; form.requestSubmit(); });

/* ---------------- mic / send toggle ---------------- */
function syncSend(){ const has=q.value.trim().length>0; send.style.display=has?'flex':'none'; mic.style.display=has?'none':'flex'; }
q.addEventListener('input',syncSend); syncSend();
const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
mic.onclick=()=>{
  if(!SR){ toast('এই ব্রাউজারে ভয়েস ইনপুট নেই — লিখে প্রশ্ন করো'); return; }
  try{
    const r=new SR(); r.lang='bn-BD'; r.interimResults=false;
    mic.classList.add('rec');
    r.onresult=e=>{ q.value=e.results[0][0].transcript; syncSend(); };
    r.onend=()=>mic.classList.remove('rec');
    r.onerror=()=>{ mic.classList.remove('rec'); toast('শোনা যায়নি, আবার চেষ্টা করো'); };
    r.start();
  }catch(e){ toast('ভয়েস ইনপুট চালু হয়নি'); }
};

/* ---------------- auth ---------------- */
async function refreshMe(){
  if(!currentUser) return;
  try{
    const t=await currentUser.getIdToken();
    const r=await fetch('/me',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({idToken:t})});
    const d=await r.json(); if(d.remaining){ remaining=d.remaining; renderAcct(); }
  }catch(e){}
}
if(AUTH_ON){
  firebase.initializeApp(FB_CONFIG);
  firebase.auth().onAuthStateChanged(u=>{
    currentUser=u;
    if(u){ loginEl.style.display='none'; refreshMe(); }
    else{ loginEl.style.display='flex'; }
    renderAcct();
  });
  $('gbtn').onclick=async()=>{
    const prov=new firebase.auth.GoogleAuthProvider();
    try{
      if(window.Capacitor){ await firebase.auth().signInWithRedirect(prov); }
      else{ await firebase.auth().signInWithPopup(prov); }
    }
    catch(e){ $('lerr').textContent='লগইন হয়নি: '+e.message; }
  };
}else{
  renderAcct();
}
renderAcct(); renderHist();

/* ---------------- submit ---------------- */
form.onsubmit=async e=>{
  e.preventDefault();
  const text=q.value.trim(); if(!text) return;
  if(AUTH_ON && !currentUser){ loginEl.style.display='flex'; return; }
  q.value=''; syncSend(); addUser(text); send.disabled=true;
  chipsEl.style.display='none';
  const wait=addBot('ভাবছি '); wait.classList.add('dots');
  wait.querySelector('.txt').innerHTML='ভাবছি <span>.</span><span>.</span><span>.</span>';
  const idx=pairs.length;
  try{
    const {status,data}=await ask(text,pairs);
    wait.classList.remove('dots');
    if(status===401){ setRich(wait,'লগইনের মেয়াদ শেষ — আবার লগইন করো।');
      if(AUTH_ON) loginEl.style.display='flex'; }
    else if(status===429){ setRich(wait,data.reply); if(data.remaining){remaining=data.remaining; renderAcct();} }
    else if(data.reply){ setRich(wait,data.reply);
      pairs.push({u:text,a:data.reply,f:''}); attachActions(wait,idx); persist();
      if(data.remaining){remaining=data.remaining; renderAcct();} }
    else setRich(wait,'কিছু একটা সমস্যা হয়েছে।');
  }catch(err){ wait.classList.remove('dots'); setRich(wait,'নেটওয়ার্কে সমস্যা — আবার চেষ্টা করো।'); }
  send.disabled=false; q.focus();
};
if('serviceWorker' in navigator){window.addEventListener('load',()=>{navigator.serviceWorker.register('/sw.js').catch(()=>{});});}
q.focus();
</script>
</body>
</html>
"""


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
