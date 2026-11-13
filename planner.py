#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
planner.py - Sahne Planlayıcı (yerel uygulama)

Metni yaz -> sahnelere bölünsün -> her sahne için Pexels/Pixabay'den video bul
-> beğendiğini seç (otomatik indirilir) -> inputs/eslesme.txt otomatik yazılsın.

Masaüstü uygulaması gibi çalışır (siyah cmd penceresi yok):
    1) Bir kez Kisayol_Olustur.bat dosyasına çift tıkla -> masaüstünde ikon oluşur.
    2) Bundan sonra ikona çift tıkla: kendi penceresinde açılır, pencereyi kapatınca tamamen kapanır.
Elle çalıştırmak istersen:  python planner.py   (kısayol için: python planner.py --kisayol)
Kurulum gerekmez, yalnızca Python standart kütüphanesi kullanılır (pencere için Edge veya Chrome yeterli).
pipeline.py ile aynı klasöre koy.

API anahtarı GEREKMEZ. Her sahne için Pexels / Pixabay / Mixkit / Coverr arama linkleri üretilir;
videoyu sen indirip sahnenin kutusuna sürüklersin. Opsiyonel: ANTHROPIC_API_KEY (daha akıllı
sahne bölme ve arama kelimeleri için).
"""

import base64
import concurrent.futures
import zlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
INPUT_DIR = ROOT / "inputs"
VIDEO_DIR = INPUT_DIR / "videos"
KEYS_FILE = ROOT / "planner_keys.json"
PROJECT_FILE = ROOT / "planner_project.json"
PORT = int(os.environ.get("PLANNER_PORT", "8765"))
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
THUMB_FILE = INPUT_DIR / "thumbnail.png"            # onaylanan kapak (ayrı dosya olarak inputs içinde durur)
EXPORT_DIR = ROOT / "ciktilar"                      # her üretilen video buraya tarih+isimle kaydedilir
EN_DIR = ROOT / "english_conversion"                 # İngilizce dönüştürme ayrı çalışma alanı
EN_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}
EN_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus"}
EN_OUTPUT = EN_DIR / "english_output.mp4"
RAW_VIDEO = ROOT / "output_video_kapaksiz.mp4"      # kapak eklenmemiş ham çıktı (kapak hep bundan eklenir)
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}
UA = "Mozilla/5.0 (planner.py)"
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".wma", ".opus"}
ALLOWED_DOWNLOAD_HOSTS = ("pexels.com", "pixabay.com", "wikimedia.org", "mixkit.co")


# --------------------------------------------------------------------------
# Türkçe yardımcılar (pipeline.py ile aynı mantık, eşleşme birebir tutsun diye)
# --------------------------------------------------------------------------
def tr_fold(text: str) -> str:
    t = text.replace("İ", "i").replace("I", "ı").lower()
    return t.translate(str.maketrans("çğışöü", "cgisou"))


def tr_lower(text: str) -> str:
    return text.replace("İ", "i").replace("I", "ı").lower()


def stem_match(word: str, tag: str) -> bool:
    L = len(tag)
    n = L if L <= 3 else (L - 1 if L <= 6 else L - 2)
    if len(word) >= n and word.startswith(tag[:n]):
        return True
    if L >= 4 and tag[-2] in "iu" and tag[-1] not in "aeiou":
        return word.startswith(tag[:-2] + tag[-1])
    return False


def norm_tag(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]|_", "", tr_fold(t.strip()))).strip()


def fold_tokens(text: str) -> list:
    toks = (tr_fold(re.sub(r"[\W_]+", "", w)) for w in text.split())
    return [t for t in toks if t]


def tag_positions(tokens: list, parts: list) -> list:
    n = len(parts)
    return [k for k in range(len(tokens) - n + 1)
            if all(stem_match(tokens[k + j], parts[j]) for j in range(n))]


STOP = {"bunun", "bunu", "icin", "gibi", "kadar", "daha", "ama", "fakat", "cunku",
        "sonra", "ancak", "ayrica", "veya", "sadece", "hepsi", "olarak", "kendi"}


# --------------------------------------------------------------------------
# Ayarlar / anahtarlar
# --------------------------------------------------------------------------
def load_keys() -> dict:
    keys = {
        "pexels": os.environ.get("PEXELS_API_KEY", ""),
        "pixabay": os.environ.get("PIXABAY_API_KEY", ""),
        "anthropic": os.environ.get("ANTHROPIC_API_KEY", ""),
        "groq": os.environ.get("GROQ_API_KEY", ""),
        "cf_token": os.environ.get("CLOUDFLARE_API_TOKEN", ""),
        "cf_account": os.environ.get("CLOUDFLARE_ACCOUNT_ID", ""),
    }
    try:
        saved = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
        for k in keys:
            if saved.get(k):
                keys[k] = saved[k]
    except Exception:
        pass
    return keys


def save_keys(new: dict) -> None:
    try:
        saved = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
    except Exception:
        saved = {}
    for k in ("pexels", "pixabay", "anthropic", "groq", "cf_token", "cf_account"):
        v = (new.get(k) or "").strip()
        if v:
            saved[k] = v
    KEYS_FILE.write_text(json.dumps(saved, indent=2), encoding="utf-8")


def http_json(url, headers=None, data=None, timeout=60):
    hdr = {"User-Agent": UA}
    hdr.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdr["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=hdr,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} ({urlparse(url).netloc}): {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Bağlantı hatası ({urlparse(url).netloc}): {e.reason}")
    except OSError as e:                      # zaman aşımı / bağlantı kopması
        raise RuntimeError(f"Yanıt gelmedi ({urlparse(url).netloc}): {e}")


# --------------------------------------------------------------------------
# Ücretsiz yapay zekâ katmanı (kart istemez): Groq ve Cloudflare Workers AI.
# Claude anahtarı varsa o da kullanılır. Hepsi yoksa uygulama yerel sözlükle çalışmaya devam eder.
# --------------------------------------------------------------------------
LLM_LAST = {"provider": ""}
GROQ_MODELS = ("openai/gpt-oss-120b", "openai/gpt-oss-20b")
CF_LLM_MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"


def llm_providers(keys=None) -> list:
    keys = keys or load_keys()
    out = []
    if keys.get("anthropic"):
        out.append("claude")
    if keys.get("groq"):
        out.append("groq")
    if keys.get("cf_token") and keys.get("cf_account"):
        out.append("cloudflare")
    return out


def _post_json(url, headers, body, timeout=60):
    hdr = {"User-Agent": UA, "Content-Type": "application/json"}
    hdr.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=hdr, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8")), {}
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        try:
            data = json.loads(raw)
        except Exception:
            data = {"error": raw[:300]}
        return e.code, data, {k.lower(): v for k, v in dict(e.headers).items()}
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"Bağlantı hatası: {getattr(e, 'reason', e)}")


def _llm_openai_compat(url, token, model, system, user, max_tokens, json_mode):
    body = {"model": model, "max_tokens": max_tokens, "temperature": 0.3,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    for attempt in range(3):
        code, data, hdr = _post_json(url, {"Authorization": "Bearer " + token}, body, 90)
        if code == 200:
            try:
                return data["choices"][0]["message"]["content"] or ""
            except Exception:
                return str((data.get("result") or {}).get("response") or "")
        if code == 429 and attempt < 2:                 # dakika sınırı: bekleyip tekrar dene
            try:
                wait = float(hdr.get("retry-after") or 6)
            except ValueError:
                wait = 6
            time.sleep(min(max(wait, 2), 25))
            continue
        err = data.get("error", data)
        if isinstance(err, dict):
            err = err.get("message") or err
        raise RuntimeError(f"HTTP {code}: {str(err)[:200]}")
    raise RuntimeError("HTTP 429: istek sınırı")


def llm_chat(system: str, user: str, max_tokens: int = 1500, json_mode: bool = False) -> str:
    """Sırayla Claude (anahtar varsa) -> Groq -> Cloudflare dener. Metin döner; hepsi başarısızsa hata fırlatır."""
    keys = load_keys()
    errs = []
    for prov in llm_providers(keys):
        try:
            if prov == "claude":
                resp = http_json("https://api.anthropic.com/v1/messages",
                                 headers={"x-api-key": keys["anthropic"], "anthropic-version": "2023-06-01"},
                                 data={"model": CLAUDE_MODEL, "max_tokens": max_tokens, "system": system,
                                       "messages": [{"role": "user", "content": user}]}, timeout=120)
                out = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
            elif prov == "groq":
                out, last = "", None
                for model in GROQ_MODELS:
                    try:
                        out = _llm_openai_compat("https://api.groq.com/openai/v1/chat/completions",
                                                 keys["groq"], model, system, user, max_tokens, json_mode)
                        break
                    except RuntimeError as e:
                        last = e
                        if "HTTP 401" in str(e) or "HTTP 403" in str(e):
                            break
                if not out and last:
                    raise last
            else:
                # Cloudflare: önce OpenAI-uyumlu chat, olmazsa /ai/run native
                acc = keys["cf_account"].strip()
                tok = keys["cf_token"].strip()
                out = ""
                try:
                    url = f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/v1/chat/completions"
                    out = _llm_openai_compat(url, tok, CF_LLM_MODEL, system, user, max_tokens, False)
                except Exception as e1:
                    # Native Workers AI run (messages şeması)
                    run_url = f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/run/{CF_LLM_MODEL}"
                    body = {
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "max_tokens": max_tokens,
                    }
                    code, data, _ = _post_json(run_url, {"Authorization": "Bearer " + tok}, body, 120)
                    if code != 200:
                        raise RuntimeError(f"HTTP {code}: {str(data)[:180]}") from e1
                    # Cloudflare run yanıt formatları
                    res = data.get("result") if isinstance(data, dict) else None
                    if isinstance(res, dict):
                        out = str(res.get("response") or res.get("text") or "")
                    elif isinstance(res, str):
                        out = res
                    else:
                        out = str((data or {}).get("response") or "")
                    if not out.strip():
                        raise RuntimeError(f"cloudflare boş yanıt: {str(data)[:160]}") from e1
            if out.strip():
                LLM_LAST["provider"] = prov
                return out
            errs.append(f"{prov}: boş yanıt")
        except Exception as e:
            errs.append(f"{prov}: {str(e)[:160]}")
    raise RuntimeError("Yapay zekâ yanıt vermedi (" + "; ".join(errs) + ")" if errs else "Yapay zekâ anahtarı yok.")


def parse_json_obj(out: str) -> dict:
    a, b = out.find("{"), out.rfind("}")
    if a < 0 or b < 0:
        raise RuntimeError("Yapay zekâ beklenen JSON'u döndürmedi.")
    return json.loads(out[a:b + 1])


def probe_duration(path: Path) -> float:
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=True)
        return float(res.stdout.strip())
    except Exception:
        return 0.0


def audio_info():
    if INPUT_DIR.is_dir():
        for p in sorted(INPUT_DIR.iterdir()):
            if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
                return p.name, probe_duration(p)
    return None, 0.0


# --------------------------------------------------------------------------
# Sahne üretimi
# --------------------------------------------------------------------------
SYSTEM_PROMPT = """You prepare scene plans for vertical short videos (YouTube Shorts / Reels / TikTok) from a Turkish voice-over script.

Split the script into scenes in spoken order. Every word of the script must end up in exactly one scene, unchanged: copy the Turkish text verbatim into "text". @@RULE@@

For every scene return:
- "slug": a short English file-name part using only a-z, 0-9 and underscore, describing the visual (for example "brain_scan"). It must be English because it becomes part of a file name.
- "triggers": 1-3 Turkish trigger words or short phrases copied from THAT scene's own text. A speech-recognition pipeline switches to the scene's video the moment a trigger is spoken. Rules: the first trigger should be as close to the start of the scene as possible (ideally the scene's first one or two words, which may be a two-word phrase); prefer distinctive words; a trigger must not occur anywhere in the text of any OTHER scene, not even as a similar word form (Turkish suffixes are tolerated by the matcher, so "beyin" also matches "beyninizin"); avoid very common words such as "bir", "bu", "ve", "ama" unless they are part of a longer distinctive phrase; lowercase, no punctuation.
- "queries": exactly 3 English stock-footage search queries for Pexels/Pixabay, 2-4 words each, best first. First decide the MAIN TOPIC of the whole script (for example "airplane safety") and its anchor noun ("airplane"). Rules: (1) Query 1 = the most literal, concrete thing a camera could film for THIS line, written with the anchor noun whenever it fits (e.g. "airplane boarding passengers", never just "passengers"). (2) Query 2 = a broader but clearly related visual of the same subject, with simpler words. (3) Query 3 = a safe, topic-matching b-roll for abstract or emotional lines. (4) At least two of the three queries must visibly belong to the script's topic; a query that could belong to any video (like "person" or "city") is not allowed. (5) For a line that is only a number, statistic, name or abstract claim (e.g. "241 people died", "28 percent"), never search the number; film the SUBJECT or the MOOD with safe, non-graphic imagery (e.g. "airplane smoke dark", "dark storm clouds", "empty airplane seats"). Never ask for gore, injured people, bodies or real disaster footage. (6) Use only common tag words that really exist on stock sites (subject + setting/action), never sentences; no abstract nouns (concept, fact, truth, success, idea), no brand/people/movie names, no words like vertical, 4k, hd, video. (7) Neighbouring scenes should not share the same query 1.

Return ONLY JSON, with no markdown and no commentary:
{"scenes":[{"text":"...","slug":"...","triggers":["..."],"queries":["...","..."]}]}"""


# Sahne sıklığı: min = bundan kısa parça komşusuna eklenir, max = bundan uzun cümle bölünür (0 = bölme),
# avg = sahne başına ortalama kelime (hedef sahne sayısı için).
DENSITY = {
    # Sahne başına aşırı az kelime üretme; uzun cümleleri de doğal anlam bloklarına ayır.
    "normal": {"min": 5, "max": 16, "avg": 10.5},
    "sik":    {"min": 5, "max": 12, "avg": 8.0},
    "cok":    {"min": 4, "max": 8, "avg": 5.8},
}
# KANCA MODU (ilk 3 saniye): ilk cümle, planındaki 0-3 sn tablosuna uysun diye çok kısa vuruşlara bölünür.
# Kelime sayıları ~2,6 kelime/sn ile yaklaşık 0,8 / 0,8 / 1,2 / 0,8 sn eder. İstersen buradan oynat.
HOOK_BEATS = [2, 2, 3, 2]
HOOK_MAX_WORDS = 10           # ilk cümle bundan uzunsa 3 sn'ye sığmaz (uyarı verilir)
HOOK_TYPES = {"A": "Kişisel deneyim", "B": "Çelişki", "C": "Mini test"}
HOOK_VISUAL = {
    "A": "Hook type A (personal experience): the opening scenes show a close-up of a real person living the everyday moment the line describes (hands, doorway, face reacting), so the viewer thinks 'this happens to me'.",
    "B": "Hook type B (contradiction): the opening scenes show an image that visually reverses the common belief, a surprising or opposite everyday situation.",
    "C": "Hook type C (mini test): the opening scenes show a close-up of a hand, phone or headphones doing the simple action the viewer is asked to do.",
}
HOOK_QUERY = {"A": "person confused close up", "B": "surprised face close up", "C": "hand close up"}
HOOK_GREETINGS = ("merhaba", "selam", "herkese", "arkadaslar", "hos geldin", "hosgeldin", "gunaydin",
                  "bugun sizlere", "bugun size", "kanalima", "kanalimiza")
CLAIM_WORDS = {"kesin", "kesinlikle", "kesinkes", "kanitlandi", "kanitlanmis", "kanitlanmistir"}
CONJ = {"ve", "ama", "ancak", "cunku", "fakat", "veya", "yoksa", "ayrica", "ustelik", "peki", "boylece"}


def density_cfg(name) -> dict:
    return DENSITY.get(str(name or ""), DENSITY["sik"])


def scene_prompt(density: str, text: str, hook: str = "") -> str:
    cfg = density_cfg(density)
    n = max(1, round(len(text.split()) / cfg["avg"]))
    if density == "cok":
        span, wmin, wmax = "1-2 seconds", 4, 8
    elif density == "normal":
        span, wmin, wmax = "2-4 seconds", 5, 16
    else:
        span, wmin, wmax = "1.5-3 seconds", 5, 12
    rule = (f"Create natural, meaningful visual beats, usually every {span} of speech "
            f"(roughly {wmin}-{wmax} words per scene). Never split merely to hit an exact word count. "
            "Keep each complete short sentence as its own scene; do not merge a meaningful short sentence with an unrelated one. "
            "Keep a question and its answer as separate scenes. Split a long sentence into two or more complete-sounding "
            "meaning units at a comma, semicolon, colon, or before a meaningful conjunction such as ve, ama, ancak, çünkü. "
            "If punctuation is absent, choose a grammatical pause where both parts remain understandable. "
            "Avoid fragments of only 1-3 words unless the original script itself is a deliberate short punchline. "
            f"Aim for about {n} scenes, but meaning and natural speech take priority over the count.")
    if hook:
        rule += (" HOOK MODE: the FIRST sentence may produce ONE scene, or TWO scenes maximum if it is long. "
                 "If it is split, make exactly two reasonably balanced, meaningful parts; aim for at least five words "
                 "in each part. Never split the opening sentence into tiny fragments and never let later word-count "
                 "rules split either half again. Prefer a real comma or conjunction; otherwise use a natural grammatical pause. "
                 "Keep the opening image literal and directly related to the spoken words. " + HOOK_VISUAL.get(hook, ""))
    return SYSTEM_PROMPT.replace("@@RULE@@", rule)


def split_long(sentence: str, max_words: int, min_piece: int = 5) -> list:
    """Uzun cümleyi doğal bağlaç/noktalama noktasından; yoksa dengeli dilsel duraktan böler."""
    words = sentence.split()
    if not max_words or len(words) <= max_words:
        return [sentence]
    if len(words) < min_piece * 2:
        return [sentence]

    natural = []
    for k in range(min_piece, len(words) - min_piece + 1):
        cur = tr_fold(re.sub(r"[\W_]+", "", words[k]))
        if words[k - 1].endswith((",", ";", ":", "—", "–")) or cur in CONJ:
            natural.append(k)

    # Noktalama/bağlaç yoksa da cümleyi tek parça bırakma: orta noktaya yakın,
    # çok kısa parça üretmeyen bir kelime sınırı seç.
    candidates = natural or list(range(min_piece, len(words) - min_piece + 1))
    midpoint = len(words) / 2
    k = min(candidates, key=lambda c: abs(c - midpoint))
    left = " ".join(words[:k]).strip()
    right = " ".join(words[k:]).strip()
    return split_long(left, max_words, min_piece) + split_long(right, max_words, min_piece)


def split_hook(sentence: str, beats=None) -> list:
    """İlk cümleyi gerekirse iki dengeli ve anlamlı sahneye böler; asla 3+ parça üretmez."""
    words = sentence.split()
    # Kısa/orta uzunluktaki kanca tek sahne kalsın; ikiye bölmek için iki tarafta da bağlam olmalı.
    if len(words) < 12:
        return [sentence]
    candidates = []
    for k in range(5, len(words) - 4):
        cur = tr_fold(re.sub(r"[\W_]+", "", words[k]))
        if words[k - 1].endswith((",", ";", ":", "—", "–")):
            candidates.append((k, 0))
        elif cur in CONJ:
            candidates.append((k, 1))
    midpoint = len(words) / 2
    # Doğal kırılma orta noktadan çok uzaktaysa cümleyi dengeli bir dilsel duraktan böl.
    if not candidates or min(abs(item[0] - midpoint) for item in candidates) > max(3, len(words) * 0.15):
        candidates = [(k, 2) for k in range(5, len(words) - 4)
                      if tr_fold(re.sub(r"[\W_]+", "", words[k])) not in {"bile", "de", "da", "ki", "ise"}]
    if not candidates:
        return [sentence]
    # Önce iki parçanın dengesi; eşitlikte noktalama/bağlaç önceliği.
    k, _ = min(candidates, key=lambda item: (abs(item[0] - midpoint), item[1]))
    left, right = " ".join(words[:k]).strip(), " ".join(words[k:]).strip()
    if len(left.split()) < 5 or len(right.split()) < 5:
        return [sentence]
    return [left, right]


def split_sentences(text: str, density: str = "sik", hook: str = "") -> list:
    cfg = density_cfg(density)
    text = re.sub(r"\s+", " ", text.strip())
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", text) if p.strip()]
    head = []
    if hook and parts:                        # ilk cümle en fazla iki anlamlı sahne olur
        head = split_hook(parts[0])
        parts = parts[1:]
    pieces = []
    for p in parts:
        pieces += split_long(p, cfg["max"])
    # Tamamlanmış kısa cümleleri komşusuna yapıştırma; yalnızca uzun cümleler
    # split_long içinde, her iki tarafı da yeterli bağlamla bölünür.
    return head + pieces


def first_sentence_scenes(scenes: list, limit: int = 8) -> int:
    """İlk cümleyi oluşturan sahne sayısı (cümle işaretiyle biten ilk sahneye kadar)."""
    n = 0
    for s in scenes[:limit]:
        n += 1
        if re.search(r"[.!?…][\"”’']*\s*$", s.get("text", "").strip()):
            break
    return n


def enforce_hook(scenes: list) -> list:
    """İlk cümleyi en fazla iki anlamlı sahneye indir; gereksiz kanca parçalanmasını önle."""
    if not scenes:
        return scenes
    n = first_sentence_scenes(scenes)
    orig = scenes[:n]
    first = " ".join(s["text"].strip() for s in orig)
    pieces = split_hook(first)
    if len(pieces) == n and all((orig[i].get("text", "").strip() == pieces[i]) for i in range(n)):
        return scenes
    new = []
    for j, piece in enumerate(pieces):
        # İlk sahneye en yakın mevcut sorguları koru; metin değiştiği için tetikleyiciler sonra onarılır.
        old = orig[min(j, len(orig)-1)] if orig else {}
        new.append({"text": piece, "slug": old.get("slug") or f"hook{j+1}",
                    "triggers": [], "queries": list(old.get("queries") or [])})
    return new + scenes[n:]


def apply_hook_queries(scenes: list, hook: str) -> None:
    """İlk sahnenin 3. arama kelimesini kanca türüne uygun genel bir yakın plan kelimesiyle değiştirir (son çare)."""
    q = HOOK_QUERY.get(hook)
    if not q or not scenes:
        return
    qs = list(scenes[0].get("queries") or [])
    if q in qs:
        return
    if len(qs) >= 3:
        qs[2] = q
    else:
        qs.append(q)
    scenes[0]["queries"] = qs


def auto_triggers(i: int, texts: list, toks: list) -> list:
    """
    Sahnenin BAŞINDAKİ en kısa, başka hiçbir sahnede geçmeyen ifadeyi seçer (video tam
    sahne başlarken değişsin diye). Ardından yedek olarak sahneye özgü bir kelime ekler.
    """
    words = [re.sub(r"^[^\w]+|[^\w]+$", "", w) for w in texts[i].split()]
    words = [w for w in words if w]
    folds = [tr_fold(re.sub(r"[\W_]+", "", w)) for w in words]

    def conflicts(parts):
        return any(tag_positions(ts, parts) for j, ts in enumerate(toks) if j != i)

    def meaningful(fs):
        return len(fs) >= 3 or any(len(f) >= 4 and f not in STOP for f in fs)

    picks, used_upto = [], 0
    for start in range(0, min(3, len(words))):
        for n in (1, 2, 3):
            fs = folds[start:start + n]
            if len(fs) < n or not meaningful(fs) or conflicts(fs):
                continue
            picks.append(" ".join(tr_lower(w) for w in words[start:start + n]))
            used_upto = start + n
            break
        if picks:
            break
    for k in range(used_upto, len(words)):      # yedek: sahneye özgü tek kelime
        if len(picks) >= 2:
            break
        f = folds[k]
        if len(f) >= 5 and f not in STOP and not conflicts([f]):
            picks.append(tr_lower(words[k]))
    if not picks and words:
        picks = [tr_lower(max(words, key=len))]
    return picks


def repair_triggers(scenes: list) -> None:
    """Claude'un seçtiği tetikleyicileri doğrular: metinde yoksa veya başka sahnede geçiyorsa atar,
    ilk tetikleyici sahne başından uzaksa sahne başı ifadesini öne ekler."""
    texts = [s["text"] for s in scenes]
    toks = [fold_tokens(t) for t in texts]
    for i, s in enumerate(scenes):
        good = []
        for t in s.get("triggers", []):
            parts = norm_tag(t).split()
            pos = tag_positions(toks[i], parts) if parts else []
            if not pos:
                continue
            if any(tag_positions(ts, parts) for j, ts in enumerate(toks) if j != i):
                continue
            good.append((pos[0], t))
        good.sort(key=lambda x: x[0])
        trig = [t for _, t in good]
        auto = auto_triggers(i, texts, toks)
        if not good or good[0][0] > 2:
            trig = [a for a in auto[:1] if a not in trig] + trig
        if len(trig) < 2:
            trig += [a for a in auto[1:] if a not in trig][:1]
        s["triggers"] = trig[:3]


def scenes_basic(text: str, density: str = "sik", hook: str = "") -> list:
    texts = split_sentences(text, density, hook)
    prefetch_translations(texts)
    toks = [fold_tokens(t) for t in texts]
    scenes = [{"text": t, "slug": f"scene{i + 1}", "triggers": auto_triggers(i, texts, toks),
               "queries": basic_queries(t, text)} for i, t in enumerate(texts)]
    diversify(scenes)
    for s in scenes:                      # dosya adı için ilk arama kelimesinden İngilizce slug
        if s["queries"]:
            s["slug"] = re.sub(r"[^a-z0-9]+", "_", s["queries"][0]).strip("_") or s["slug"]
    return scenes


def scenes_with_claude(text: str, key: str, density: str = "sik", hook: str = "") -> list:
    body = {"model": CLAUDE_MODEL, "max_tokens": 12000, "system": scene_prompt(density, text, hook),
            "messages": [{"role": "user", "content": text}]}
    resp = http_json("https://api.anthropic.com/v1/messages",
                     headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                     data=body, timeout=180)
    out = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
    a, b = out.find("{"), out.rfind("}")
    if a < 0 or b < 0:
        raise RuntimeError("Claude beklenen JSON'u döndürmedi.")
    data = json.loads(out[a:b + 1])
    scenes = []
    for s in data.get("scenes", []):
        slug = re.sub(r"[^a-z0-9]+", "_", str(s.get("slug", "")).lower()).strip("_") or "clip"
        scenes.append({
            "text": str(s.get("text", "")).strip(),
            "slug": slug,
            "triggers": [str(t).strip() for t in s.get("triggers", []) if str(t).strip()],
            "queries": [str(q).strip() for q in s.get("queries", []) if str(q).strip()],
        })
    scenes = [s for s in scenes if s["text"]]
    if not scenes:
        raise RuntimeError("Claude sahne üretmedi.")
    return scenes



LLM_Q_SYSTEM = """You choose stock-footage search queries (Pexels / Pixabay / Mixkit / Coverr) for a short vertical video made from a Turkish voice-over script.
You get the FULL SCRIPT (so you know the topic) and a numbered list of SCENES (one spoken line each).
For EVERY scene:
1. First write "en": a short, plain English paraphrase of what that line says (max 14 words). Translate the real nouns and verbs of THAT line; do not drift to the general topic.
2. Then give exactly 3 stock-footage queries, 2-4 English words each, best first:
   - Query 1 = the most literal, concrete thing a camera could film for THIS line, built from the nouns/actions you wrote in "en" (add the script's topic noun only if it still fits the line).
   - Query 2 = a broader but clearly related visual of the same subject, simpler words.
   - Query 3 = a safe topic-matching b-roll for abstract or emotional lines.
Rules: queries must be visually filmable tag words that really exist on stock sites (subject + setting/action), never sentences. No abstract nouns (concept, fact, truth, success, idea, problem), no brand/people/movie names, no words like vertical, 4k, hd, video. A query that could belong to any video ("person", "city", "people") is not allowed. For a line that is only a number, statistic or abstract claim, film the SUBJECT or the MOOD instead of the number. Never ask for gore, injuries, bodies or real disaster footage; use safe imagery (e.g. "dark storm clouds", "empty hospital corridor"). Neighbouring scenes should not share the same query 1.
Return ONLY JSON: {"scenes":[{"n":1,"en":"...","queries":["...","...","..."]}]} with one entry for every scene number given."""


def scenes_split_only(text: str, density: str = "sik", hook: str = "") -> list:
    texts = split_sentences(text, density, hook)
    toks = [fold_tokens(t) for t in texts]
    return [{"text": t, "slug": f"scene{i + 1}", "triggers": auto_triggers(i, texts, toks), "queries": []}
            for i, t in enumerate(texts)]


def llm_fill_queries(scenes: list, text: str):
    """Sahneleri 12'şerli gruplar halinde (tüm senaryoyu bağlam vererek) yapay zekâya sorar. -> (dolan sahne sayısı, hata)."""
    ctx, done, err = text.strip()[:2500], 0, ""
    for a in range(0, len(scenes), 12):
        chunk = scenes[a:a + 12]
        lines = "\n".join(f"{a + i + 1}. {s['text']}" for i, s in enumerate(chunk))
        try:
            out = llm_chat(LLM_Q_SYSTEM, f"FULL SCRIPT:\n{ctx}\n\nSCENES:\n{lines}", max_tokens=1800, json_mode=True)
            for it in parse_json_obj(out).get("scenes", []):
                try:
                    n = int(it.get("n")) - 1
                except Exception:
                    continue
                qs = clean_queries(it.get("queries"))
                if 0 <= n < len(scenes) and len(qs) >= 2:
                    scenes[n]["queries"] = qs
                    scenes[n]["en"] = str(it.get("en") or "")[:160]
                    done += 1
        except Exception as e:
            err = str(e)[:200]
            if "HTTP 401" in err or "HTTP 403" in err:
                break
    return done, err


def suggest_scene_visuals(scenes: list) -> None:
    """Sahne metnine göre görsel türünü önerir; süre bazlı hedef dağılımını da gözetir."""
    if not scenes:
        return
    targets = {"video": 45.0, "diagram": 25.0, "photo": 30.0}
    total = sum(max(len(re.findall(r"\S+", s.get("text", ""))), 1) for s in scenes)
    used = {k: 0.0 for k in targets}
    for i, scene in enumerate(scenes):
        txt = tr_fold(scene.get("text", ""))
        duration = max(len(re.findall(r"\S+", scene.get("text", ""))), 1)
        # Açılış kancasında hareketli, kolay anlaşılır görüntü öncelikli.
        if i == 0:
            scene["visual"] = "video"
            used["video"] += duration
            scene["visual_reason"] = "Açılış kancası: hareketli görüntü"
            continue
        diagram_words = ("yuzde", "oran", "istatistik", "veri", "grafik", "sayisinin", "artti", "azaldi", "iki kat", "uc kat", "birinci", "ikinci", "adim", "surec", "nasil calisir", "beyin", "beynin", "beyinde", "beyni", "hormon", "sinir sistemi", "kan sekeri", "dopamin", "kortizol", "adrenalin", "mekanizma", "yeniden duzenle", "bilgiyi isleme")
        # AI fotoğrafı yalnızca tarihî arşiv için değil; soyut psikolojik anları somut, sahneye özel bir kareye dönüştürmek için de kullanılır.
        photo_words = ("yilinda", "yil once", "tarihinde", "tarihi", "arsiv", "eski fotograf", "kesfedildi", "kesif", "antik", "roma", "misir", "savasi", "savas", "tarihi kisi", "muzede", "belgesel", "unut", "hatirla", "hafiza", "dikkat", "duygu", "bilinc", "zihin", "algila", "stres", "korku", "dusunce", "dusun", "karar", "beklenti")
        ds = sum(2 if w in txt else 0 for w in diagram_words)
        ps = sum(2 if w in txt else 0 for w in photo_words)
        # Sahne türü seçilirken hem içerik uygunluğu hem hedefte kalan süre hesaba katılır.
        remaining = {k: targets[k] * total / 100.0 - used[k] for k in targets}
        scores = {
            "video": 1.0 + max(0, remaining["video"]) / max(total, 1) * 2.0,
            "diagram": ds + max(0, remaining["diagram"]) / max(total, 1) * 2.0,
            "photo": ps + max(0, remaining["photo"]) / max(total, 1) * 2.0,
        }
        # Genel/soyut anlatım stok videoda kalır; anlamlı bir işaret varsa özel türü seç.
        if ds == 0: scores["diagram"] -= 1.25
        if ps == 0: scores["photo"] -= 1.25
        kind = max(scores, key=scores.get)
        scene["visual"] = kind
        scene["visual_reason"] = {"video": "Hareketli stok görüntü", "diagram": "Sayı, süreç veya bilimsel açıklama", "photo": "Tarihî / arşiv bağlamı"}[kind]
        used[kind] += duration


def make_scenes(text: str, density: str = "sik", hook: str = "") -> dict:
    # Kanca varsayılan olarak açık: ilk cümle ilk saniyeler için kısa vuruşlara ayrılır.
    hook = hook if hook in ("X", "A", "B", "C") else "X"
    res = _make_scenes(text, density, hook)
    if res.get("scenes"):
        suggest_scene_visuals(res["scenes"])
        apply_hook_queries(res["scenes"], hook)
        # Stil kartını önceden üret (sonraki AI görseller aynı görsel dili otomatik kullansın)
        try:
            bible = style_card_for_script(text)
            if bible:
                res["visual_bible"] = bible
        except Exception:
            pass
        n = first_sentence_scenes(res["scenes"])
        extra = f"Kanca korundu: ilk cümle {n} sahne (en fazla 2). Gereksiz kelime parçaları oluşturulmadı; görsel türleri içeriğe göre önerildi."
        if hook in HOOK_TYPES:
            extra += f" Kanca türü: {hook} ({HOOK_TYPES[hook]})."
        if res.get("visual_bible"):
            extra += " Film stil rehberi otomatik hazırlandı."
        res["message"] = ((res.get("message") or "") + " " + extra).strip()
    return res


SCENE_AI_SYSTEM = """You are an expert Turkish short-form film editor and semantic scene segmenter.
Split the Turkish narration into visually coherent shots, in spoken order.
CRITICAL TEXT FIDELITY: copy every character and word from the source into scene.text verbatim. Do not paraphrase, translate, correct spelling, omit, duplicate, or reorder anything. Scene texts concatenated with single spaces must reproduce the original text with whitespace normalized.
Split by meaning, not arbitrary word counts. Each scene must express ONE clear visual idea/action and be understandable on its own. Keep tightly connected phrases together; split when subject, action, time, place, example, statistic, or visual metaphor changes. Avoid tiny fragments and unrelated ideas in one scene.
For every scene return text (exact source excerpt), en (one concise English sentence capturing the actual meaning, nouns and action of THIS scene), slug (short English snake_case visual label), triggers (1-3 distinctive Turkish words/phrases), queries (3 concrete English stock-footage tags, 2-4 words each, specific to this scene).
Return ONLY JSON: {"scenes":[{"text":"...","en":"...","slug":"...","triggers":["..."],"queries":["...","...","..."]}]}.
"""


def scenes_with_ai(text: str, density: str = "sik", hook: str = "") -> list:
    pacing = {"cok": "fast visual beats, usually 4-8 words", "normal": "complete sentence or meaning unit, usually 8-16 words", "sik": "natural visual beats, usually 5-12 words"}.get(density, "natural visual beats")
    system = SCENE_AI_SYSTEM + " Requested pacing: " + pacing + ". Opening hook may be split into at most two meaningful scenes."
    # 12000 çoğu ücretsiz modelde kesiliyor / zaman aşımına düşüyor; 4500 yeterli
    out = llm_chat(system, "FULL TURKISH SCRIPT (preserve exactly):\n" + text, max_tokens=4500, json_mode=True)
    data = parse_json_obj(out)
    scenes = []
    for item in data.get("scenes", []):
        part = str(item.get("text") or "")
        if not part.strip():
            continue
        slug = re.sub(r"[^a-z0-9]+", "_", str(item.get("slug") or "scene").lower()).strip("_") or "scene"
        scenes.append({"text": part, "slug": slug,
                       "triggers": [str(x).strip().lower() for x in item.get("triggers", []) if str(x).strip()][:3],
                       "queries": clean_queries(item.get("queries"))[:3],
                       "en": str(item.get("en") or "")[:160]})
    if not scenes:
        raise RuntimeError("Yapay zekâ sahne üretemedi.")
    norm = lambda x: re.sub(r"\s+", " ", str(x)).strip()
    joined = norm(" ".join(x["text"] for x in scenes))
    source = norm(text)
    # Tam eşleşme şartını gevşet: küçük farklarda AI sahnelerini yine kullan
    if joined != source:
        # Kelime örtüşmesi yüksekse kabul et
        sw, jw = set(source.split()), set(joined.split())
        if not sw or len(sw & jw) / max(len(sw), 1) < 0.85:
            raise RuntimeError("Yapay zekâ metni eksiksiz koruyamadı; güvenli yerel bölme kullanılacak.")
    return scenes


def _make_scenes(text: str, density: str = "sik", hook: str = "") -> dict:
    """Önce bağlı yapay zekâ ile anlam odaklı böl; başarısızsa yerel bölme. Sahneler her durumda döner."""
    text = (text or "").strip()
    if not text:
        return {"mode": "basit", "scenes": [], "message": "Metin boş."}
    ai_error = "AI metin anahtarı bağlı değil (Groq / Cloudflare / Anthropic)."
    providers = llm_providers()
    if providers:
        try:
            scenes = scenes_with_ai(text, density, hook)
            if hook:
                scenes = enforce_hook(scenes)
            repair_triggers(scenes)
            done, qerr = 0, ""
            try:
                done, qerr = llm_fill_queries(scenes, text)
            except Exception as e:
                qerr = str(e)[:120]
            try:
                fix_scene_queries(scenes, text)
            except Exception:
                pass
            for scene in scenes:
                qs = scene.get("queries") or []
                if qs:
                    scene["slug"] = re.sub(r"[^a-z0-9]+", "_", qs[0].lower()).strip("_") or scene["slug"]
            msg = f"Yapay zekâ ({LLM_LAST.get('provider') or providers[0]}) metni {len(scenes)} anlama ayırdı."
            if done:
                msg += f" {done} sahne için arama etiketleri AI ile dolduruldu."
            if qerr:
                msg += " Bazı arama etiketlerinde yerel yedek kullanıldı."
            return {"mode": "ai", "scenes": scenes, "message": msg}
        except Exception as e:
            ai_error = str(e)[:200]
    try:
        scenes = scenes_basic(text, density, hook)
    except Exception as e:
        # En son çare: cümle cümle kaba böl
        parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", text) if p.strip()] or [text]
        scenes = [{"text": p, "slug": f"scene{i+1}", "triggers": [], "queries": []} for i, p in enumerate(parts)]
        return {"mode": "basit", "scenes": scenes,
                "message": f"Yerel bölme hatası ({e}); kaba cümle bölmesi yapıldı. AI için: {ai_error}"}
    return {"mode": "basit", "scenes": scenes,
            "message": f"AI bölme kullanılamadı ({ai_error}). Yerel bölme ile {len(scenes)} sahne oluşturuldu. ⚙ API anahtarlarından Groq eklemen önerilir."}


# --------------------------------------------------------------------------
# Claude'suz arama kelimesi üretimi: önce metnin KONUSU bulunur (uçak, beyin, para...), sonra her sahne
# kurallarla konuya bağlı İngilizce kelimelere çevrilir. İnternetten tek kelime çevirisi yoktur
# (alakasız sonuç veriyordu). {a} = konunun İngilizce çapası (örn. airplane).
# --------------------------------------------------------------------------
TOPICS = [
    (r"uça[kğ]|havaalan|havayol|pilot|uçuş|kabin|yolcu|havalan",
     "airplane", ["airplane window view clouds", "airplane cabin aisle", "airplane flying sky"]),
    (r"beyin|nöron|zihin|hafıza|bellek|düşünce",
     "brain", ["brain scan", "neurons network animation", "person thinking close up"]),
    (r"araba|otomobil|sürücü|trafik|otoyol|direksiyon",
     "car", ["car driving road", "highway traffic night", "car interior steering wheel"]),
    (r"uzay|gezegen|astronot|galaksi|yıldız|güneş sistemi",
     "space", ["galaxy stars space", "planet earth orbit", "night sky stars timelapse"]),
    (r"deniz|okyanus|balık|dalga|dalış",
     "ocean", ["ocean waves aerial", "underwater fish", "sea sunset beach"]),
    (r"(?<!\w)para(?!ş|graf|metre|lel)|ekonomi|zengin|borsa|yatırım|banka|maaş",
     "money", ["money cash counting", "stock market screen", "coins falling"]),
    (r"doktor|hastane|hastalık|sağlık|ameliyat|vücut|kalp",
     "doctor", ["doctor hospital corridor", "heartbeat monitor hospital", "doctor talking patient"]),
    (r"yemek|yiyecek|mutfak|tarif|lezzet|besin",
     "food", ["cooking kitchen hands", "food close up", "chef preparing meal"]),
    (r"telefon|internet|yapay zeka|bilgisayar|teknoloji|robot|yazılım",
     "technology", ["smartphone scrolling hands", "computer code screen", "futuristic technology abstract"]),
    (r"tarih|savaş|antik|imparator|medeniyet|osmanlı|roma",
     "history", ["ancient ruins", "old map vintage", "historic architecture"]),
    (r"hayvan|kedi|köpek|kuş|aslan|doğa|orman",
     "nature", ["forest aerial", "wild animals nature", "river waterfall nature"]),
    (r"uyku|rüya|uyu(du|yan|mak)",
     "sleep", ["sleeping person bed", "bedroom night", "moon night sky"]),
    (r"spor|futbol|maç|antrenman|sporcu",
     "sports", ["athlete running track", "football stadium crowd", "gym workout"]),
]
# --------------------------------------------------------------------------
# Ücretsiz, anahtarsız çeviri (MyMemory -> yedek: Google gtx). Cümle İngilizceye çevrilir, somut isimlerden
# arama kelimesi çıkarılır. Hata olursa (internet yok, limit dolu) sessizce atlanır; kurallar yine çalışır.
# --------------------------------------------------------------------------
TR_CACHE_FILE = ROOT / "planner_translations.json"
_TR_CACHE: dict = {}
_TR_LOADED = False
_TR_LOCK = threading.Lock()
_TR_FAILS = {"n": 0, "until": 0.0}


def _tr_load() -> None:
    global _TR_LOADED
    if _TR_LOADED:
        return
    _TR_LOADED = True
    try:
        _TR_CACHE.update(json.loads(TR_CACHE_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass


def _mymemory(text: str) -> str:
    q = urllib.parse.urlencode({"q": text[:450], "langpair": "tr|en"})
    d = http_json("https://api.mymemory.translated.net/get?" + q, timeout=6)
    t = str((d.get("responseData") or {}).get("translatedText", "")).strip()
    if not t or "MYMEMORY WARNING" in t.upper() or str(d.get("responseStatus", 200)) != "200":
        raise RuntimeError("mymemory")
    return t


def _gtx(text: str) -> str:
    q = urllib.parse.urlencode({"client": "gtx", "sl": "tr", "tl": "en", "dt": "t", "q": text[:450]})
    d = http_json("https://translate.googleapis.com/translate_a/single?" + q, timeout=6)
    t = "".join(seg[0] for seg in d[0] if seg and seg[0]).strip()
    if not t:
        raise RuntimeError("gtx")
    return t


def translate_tr_en(text: str) -> str:
    """Türkçe cümle -> İngilizce. Başarısızsa '' döner (asla hata fırlatmaz)."""
    try:
        text = re.sub(r"\s+", " ", str(text)).strip()
        if not text or not re.search(r"[A-Za-zÇĞİÖŞÜçğıöşü]", text):
            return ""
        key = text.lower()
        with _TR_LOCK:
            _tr_load()
            if key in _TR_CACHE:
                return _TR_CACHE[key]
            if time.time() < _TR_FAILS["until"]:
                return ""
        for fn in (_mymemory, _gtx):
            try:
                t = fn(text)
            except Exception:
                continue
            if t:
                with _TR_LOCK:
                    _TR_CACHE[key] = t
                    _TR_FAILS["n"] = 0
                    while len(_TR_CACHE) > 3000:
                        _TR_CACHE.pop(next(iter(_TR_CACHE)))
                    try:
                        TR_CACHE_FILE.write_text(json.dumps(_TR_CACHE, ensure_ascii=False), encoding="utf-8")
                    except OSError:
                        pass
                return t
        with _TR_LOCK:                                   # iki servis de yanıt vermedi: bir süre hiç deneme
            _TR_FAILS["n"] += 1
            if _TR_FAILS["n"] >= 2:
                _TR_FAILS["until"] = time.time() + 180
                _TR_FAILS["n"] = 0
    except Exception:
        pass
    return ""


def prefetch_translations(texts: list) -> None:
    """Sahne metinlerini paralel çevirip önbelleğe alır (sahne üretimi hızlı olsun)."""
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            list(ex.map(translate_tr_en, texts))
    except Exception:
        pass


EN_STOP = set("""a an the and or but if then than so as of in on at by for with without from to into onto over under about
after before between through during against up down out off again further once here there when where why how all any both each
few more most other some such no nor not only own same too very can will just should now is are was were be been being have has
had do does did doing i me my we our you your he him his she her it its they them their this that these those what which who whom
would could might must shall may also even still yet already ever never always often sometimes while because until however
although though whether either neither one two three four five six seven eight nine ten""".split())
EN_WEAK = set("""height high higher greater great bigger big small little long short thing things time times way ways people person
persons woman women man men girl boy lady guy someone anyone everyone nobody something anything everything fact reason story truth
life case part point end number lot lots bit kind sort order meter meters metre metres kilometer kilometers feet foot percent
thousand million hundred billion year years day days minute minutes hour hours first last next new old good bad best worst same
different important possible real true full whole many much make made makes get got gets take took taken takes come came comes go
went gone goes see saw seen look looks want need use used find found give gave given think thought feel felt know knew said say says
tell told ask asked fell fall falls fallen ran run runs began begin begun kept keep held hold brought bring stood stand lost lose
met meet sat sit spoke speak wrote write heard hear left leave became become turned turn put let seem seemed show showed shown
able like likely really actually maybe perhaps probably surely simply exactly almost nearly later earlier soon today tonight
air side area place moment world level group member members dead death died die kill killed body bodies blood bloody victim victims injured wounded murder gore corpse massacre""".split())
EN_SYN = {"hostess": "flight attendant", "stewardess": "flight attendant", "steward": "flight attendant",
          "stewardesses": "flight attendant", "hostesses": "flight attendant", "plane": "airplane", "aircraft": "airplane",
          "planes": "airplane", "airliner": "airplane", "jet": "airplane"}


EN_VERB_NOUN = {"exploded": "explosion", "explode": "explosion", "explodes": "explosion", "exploding": "explosion",
                "crashed": "crash", "crashes": "crash", "burned": "fire", "burnt": "fire", "burning": "fire",
                "collapsed": "collapse", "flooded": "flood", "erupted": "eruption", "drowned": "water",
                "froze": "ice", "frozen": "ice", "shipwreck": "shipwreck", "sank": "ship sinking"}


def en_keywords(en: str):
    """İngilizce cümleden (özel isimler, somut isimler) çıkarır; soyut/genel kelimeleri atar."""
    props, commons = [], []
    for i, raw in enumerate(re.findall(r"[A-Za-z][A-Za-z\-]*(?:'s)?", en or "")):
        w = re.sub(r"'s$", "", raw)
        lw = w.lower()
        lw = EN_VERB_NOUN.get(lw, lw)
        if len(lw) < 3 or lw in EN_STOP or lw in EN_WEAK or lw.endswith("ly"):
            continue
        if lw.endswith("ed") and len(lw) > 4:
            continue
        lw = EN_SYN.get(lw, lw)
        if lw in props or lw in commons:
            continue
        (props if (w[0].isupper() and i > 0) else commons).append(lw)
    return props, commons


def translation_queries(text: str):
    """-> (özel isim aramaları, somut isim aramaları). Çeviri yoksa ([], [])."""
    en = translate_tr_en(text)
    if not en:
        return [], []
    props, commons = en_keywords(en)
    ok = lambda q: q and not any(x in UNSAFE_QUERY_WORDS or x in BAD_QUERY_WORDS for x in q.split())
    return [q for q in props[:2] if ok(q)], [q for q in commons[:3] if ok(q)]


GENERIC_BROLL = ["city night timelapse", "person thinking window", "clouds timelapse sky"]

# (regex Türkçe küçük harfli sahne metninde aranır, [arama kelimeleri]) - özelden genele sıralı
# Özel isim/meslek gibi çok belirgin konular: eşleşirse kendi 3 araması genel fiil kurallarından (düştü, gitti...) önce gelir
PRIMARY_RULES = [
    (r"everest|himalaya|(?<!\w)k2(?!\w)|mont blan|kilimanjaro",
     ["mount everest", "himalaya mountain peak", "snow mountain summit"]),
    (r"hostes|kabin memur|uçuş görevlis|kabin ekib|kabin görevlis",
     ["flight attendant", "stewardess airplane cabin", "airplane cabin crew"]),
    (r"(?<!\w)(pilot|kaptan pilot|kokpit)",
     ["airplane pilot cockpit", "pilot flying airplane", "cockpit instruments"]),
    (r"(?<!\w)(itfaiye|itfaiyeci)",
     ["firefighter fire truck", "firefighters fighting fire", "fire rescue"]),
    (r"(?<!\w)(astronot|uzay istasyonu|roket)",
     ["astronaut space", "rocket launch", "space station earth"]),
]
SCENE_RULES = [
    (r"patla|patlama|patlad|infilak|bomba",
     ["{a} explosion", "explosion fire", "{a} smoke dark"]),
    (r"(?<!\w)(öl(dü|dük|düm|düler|ü|üm|ürdü|en|ecek|mek)|ölüm\w*|can ver\w*|hayatını kaybet\w*|yaşamını yitir\w*|kurban\w*)",
     ["{a} smoke dark", "dark smoke fire", "dark storm clouds"]),
    (r"(?<!\w)(bin(di|iyor|diği|ecek|mek)|biniş\w*|yolcu\w*)",
     ["{a} boarding passengers", "passengers walking airport", "{a} passengers seats"]),
    (r"orta koltu[kğ]|ortadaki|arkadaki|sıkış|dar koltuk",
     ["middle seat {a}", "crowded {a} seats", "{a} seats rows"]),
    (r"koridor",
     ["{a} aisle seat", "{a} cabin aisle", "empty {a} aisle"]),
    (r"acil çıkış|çıkış|(?<!\w)kapı",
     ["{a} emergency exit door", "emergency exit sign", "exit door light"]),
    (r"kemer",
     ["fasten seatbelt {a}", "seatbelt buckle close up", "hand buckling seatbelt"]),
    (r"sürün|emekle",
     ["crawling dark smoke", "person crawling floor", "person walking toward light"]),
    (r"kurtul|hayatta kal|sağ kal|kurtar",
     ["escape smoke light silhouette", "person walking toward light", "rescue team emergency lights"]),
    (r"paraşüt|serbest düşüş|atladı|atlayarak|atlama",
     ["skydiver freefall", "parachute skydiving sky", "skydiving aerial view"]),
    (r"düş(tü|er|en|erek|me|mek|ük|üş)(?!\w*ünce)",
     ["person falling sky", "falling through clouds", "skydiver freefall"]),
    (r"yükseklik|yüksekten|irtifa|bulutlar|bulutların",
     ["aerial view clouds", "clouds from above", "{a} window view clouds"]),
    (r"kaza|çarp|enkaz",
     ["{a} crash investigation", "emergency lights night", "dark storm clouds"]),
    (r"harita|plan(ı|ları|lar)\b|incele|veri|istatistik|rapor|araştır",
     ["data analysis screen", "analyst reviewing charts", "scientist laboratory"]),
    (r"koltu[kğ]|otur",
     ["passenger sitting {a} seat", "{a} seats rows", "seat close up"]),
    (r"uzman|bilim insan|profesör|araştırmacı",
     ["scientist laboratory", "expert talking interview", "doctor talking"]),
    (r"yorum|abone|gönder|paylaş|beğen|takip|izle",
     ["person typing smartphone", "scrolling social media phone", "friends looking at phone"]),
    (r"tehlike|risk|korku|korkunç|endişe",
     ["red warning light flashing", "danger sign close up", "dark scary hallway"]),
    (r"güvenli|güven|emniyet|koru",
     ["safety instructions {a}", "safe lock close up", "green check mark"]),
    (r"uça[kğ]",
     ["airplane flying sky", "airplane cabin interior", "airplane window view clouds"]),
    (r"beyin|beyn", ["brain scan", "neurons network animation", "person thinking close up"]),
    (r"göz|bak(ış|tı|ıyor)", ["eye close up", "person looking window", "eyes blinking macro"]),
    (r"kulak|ses(i|ler)?(?!\w)|duy", ["sound waves abstract", "person listening headphones", "ear close up"]),
    (r"kalp|nabız", ["heartbeat monitor hospital", "heart rate pulse", "doctor stethoscope"]),
    (r"uyku|uyu(du|yan|mak)|rüya", ["sleeping person bed", "bedroom night", "moon night sky"]),
    (r"(?<!\w)para(?!ş|graf|metre|lel)|zengin|maaş|borsa", ["money cash counting", "stock market screen", "coins falling"]),
    (r"telefon|ekran|internet", ["smartphone scrolling hands", "person using phone dark", "wifi city night"]),
    (r"bilgisayar|yazılım|yapay zeka|robot", ["computer code screen", "robot technology", "futuristic technology abstract"]),
    (r"kitap|okul|öğren|ders", ["student studying books", "classroom school", "reading book library"]),
    (r"yemek|yiyecek|mutfak|besin", ["cooking kitchen hands", "food close up", "chef preparing meal"]),
    (r"deniz|okyanus|dalga", ["ocean waves aerial", "underwater fish", "sea sunset beach"]),
    (r"orman|ağaç|doğa", ["forest aerial", "trees sunlight forest", "river waterfall nature"]),
    (r"dağ|zirve", ["mountain peak aerial", "mountain clouds timelapse", "hiker mountain"]),
    (r"uzay|gezegen|yıldız|galaksi", ["galaxy stars space", "planet earth orbit", "night sky stars timelapse"]),
    (r"gece|karanlık", ["city night timelapse", "dark empty street", "night sky stars"]),
    (r"yağmur|fırtına", ["rain window drops", "storm clouds lightning", "rainy street"]),
    (r"araba|trafik|yol(da|a)?(?!\w)", ["car driving road", "highway traffic night", "road aerial"]),
    (r"kalabalık|herkes|insanlar", ["crowd walking street", "people crowd city", "crowded subway commuters"]),
    (r"(?<!o )zaman|saat(?!\w)|dakika|yıl(lar)?(?!\w)", ["clock ticking close up", "time lapse clouds", "sand hourglass"]),
    (r"şehir|kent", ["city skyline aerial", "city street timelapse", "downtown night traffic"]),
    (r"çocuk|bebek", ["children playing park", "baby smiling", "kids running"]),
    (r"nasıl|neden|acaba|merak|soru|düşün|peki",
     ["person thinking window", "thoughtful face close up", "question mark chalkboard"]),
]
WEAK_RULES = [
    (r"kişi|insan|herkes|biri", ["people walking street", "crowd of people", "person walking city"]),
]

# Tek tek Türkçe kelime -> görüntülenebilir İngilizce 2 kelimelik ifade (kural tutmazsa devreye girer)
WORD_MAP = [
    (r"(?<!\w)(uçurum|dağ|zirve)", "mountain peak"), (r"(?<!\w)(buz|kar|karlı|donmuş)", "snow ice"),
    (r"(?<!\w)(ateş|yangın|alev|yan(dı|ıyor|mak))", "fire flames"), (r"(?<!\w)(su(yun|dan|yu|lar)?|nehir|ırmak|şelale)(?!\w{4})", "river water"),
    (r"(?<!\w)(bulut|gökyüzü|gökyüz|gök(?!\w{3}))", "sky clouds"), (r"(?<!\w)(ay(ın|a)?|dolunay)(?!\w)", "moon night"),
    (r"(?<!\w)(güneş|gün batımı|gün doğ)", "sunset sky"), (r"(?<!\w)(hız|hızlı|koş(tu|uyor|mak))", "running fast"),
    (r"(?<!\w)(gemi|tekne|yat(?!\w))", "ship sea"), (r"(?<!\w)(tren|istasyon|metro)", "train station"),
    (r"(?<!\w)(köprü)", "bridge aerial"), (r"(?<!\w)(bina|gökdelen|inşaat)", "skyscraper city"),
    (r"(?<!\w)(ev(in|de|e)?|oda|salon)(?!\w{3})", "house interior"), (r"(?<!\w)(sokak|cadde)", "street walking"),
    (r"(?<!\w)(el(ler|im|i)?(?!\w{2})|parmak)", "hands close"), (r"(?<!\w)(yüz(ü|ler)?(?!\w{2})|gülümse)", "face smiling"),
    (r"(?<!\w)(ağla(dı|mak|yan|ıyor)|gözyaşı|üzgün|hüzün)", "sad crying"), (r"(?<!\w)(mutlu|sevinç|gül(dü|üyor|mek))", "happy laughing"),
    (r"(?<!\w)(korku|korktu|çığlık|panik)", "scared dark"), (r"(?<!\w)(sevgi|aşk|evlen|düğün)", "wedding couple"),
    (r"(?<!\w)(kitap|okumak|okudu)", "reading book"), (r"(?<!\w)(yazı|yazdı|kalem|defter)", "writing pen"),
    (r"(?<!\w)(çalış(tı|ıyor|mak|ma)|iş(yeri|te)?(?!\w{2})|ofis|toplantı)", "office working"),
    (r"(?<!\w)(kamera|fotoğraf|çek(im|ti))", "camera filming"), (r"(?<!\w)(müzik|şarkı|gitar|piyano)", "playing guitar"),
    (r"(?<!\w)(dans|dansçı)", "dancing people"), (r"(?<!\w)(polis|hırsız|suç|cinayet|dedektif)", "police lights"),
    (r"(?<!\w)(mahkeme|hakim|yasa|kanun|hukuk)", "court law"), (r"(?<!\w)(asker|ordu|tank|savaş)", "soldiers field"),
    (r"(?<!\w)(deprem|bina yık|yıkıl|enkaz)", "ruins rubble"), (r"(?<!\w)(sel|taşkın|su bas)", "flood water"),
    (r"(?<!\w)(volkan|lav|patlama)", "volcano lava"), (r"(?<!\w)(fırtına|şimşek|yıldırım)", "lightning storm"),
    (r"(?<!\w)(sıcak|çöl|kum)", "desert sand"), (r"(?<!\w)(soğuk|kış|don(du)?)", "winter snow"),
    (r"(?<!\w)(çiçek|bahçe|bitki)", "flowers garden"), (r"(?<!\w)(kedi|köpek)", "cat dog"),
    (r"(?<!\w)(at(lar)?(?!\w)|aslan|kaplan|fil|kurt)", "wild animals"), (r"(?<!\w)(kuş|kartal|uçan)", "bird flying"),
    (r"(?<!\w)(balık|köpekbalığı|dalgıç)", "underwater fish"), (r"(?<!\w)(laboratuvar|deney|kimya)", "laboratory science"),
    (r"(?<!\w)(ilaç|hap|tedavi|hasta)", "hospital doctor"), (r"(?<!\w)(spor|egzersiz|antrenman|koşu)", "gym workout"),
    (r"(?<!\w)(kilo|diyet|beslen)", "healthy food"), (r"(?<!\w)(alışveriş|market|mağaza)", "shopping mall"),
    (r"(?<!\w)(altın|mücevher|elmas)", "gold jewelry"), (r"(?<!\w)(sır|gizem|gizli|bilinmeyen)", "dark mystery"),
    (r"(?<!\w)(hayalet|korkunç|lanet)", "dark fog"), (r"(?<!\w)(kadın|hanım|anne)", "woman portrait"),
    (r"(?<!\w)(adam|erkek|baba)", "man portrait"),
]


def dict_queries(text: str, anchor: str = "") -> list:
    """Sahne metnindeki bilinen kelimelerden 2 kelimelik arama ifadeleri (Claude'suz)."""
    t = tr_lower(text)
    found = []
    for rx, q in WORD_MAP:
        if re.search(rx, t) and q not in found:
            found.append(q)
    return found[:4]


BAD_QUERY_WORDS = {"vertical", "4k", "hd", "uhd", "video", "videos", "footage", "clip", "stock",
                   "concept", "idea", "truth", "fact", "success"}


UNSAFE_QUERY_WORDS = {"victim", "victims", "body", "bodies", "dead", "corpse", "corpses", "gore", "blood",
                      "bloody", "injured", "wounded", "killed", "death", "massacre", "murder"}


def clean_queries(qs, min_words: int = 2) -> list:
    """Arama kelimelerini doğrular: yalnızca a-z/0-9, yasak kelime yok, 2-4 kelime, tek kelimelik ve tekrar eden atılır."""
    out = []
    for q in qs or []:
        q = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 \-]", " ", str(q).lower())).strip()
        if any(x in UNSAFE_QUERY_WORDS for x in q.split()):      # grafik/rahatsız edici içerik aramasın
            continue
        w = [x for x in q.split() if x not in BAD_QUERY_WORDS and not x.isdigit()]
        if len(w) < min_words:
            continue
        q = " ".join(w[:4])
        if q not in out:
            out.append(q)
    return out


def _wb(rx: str) -> str:
    return rx if rx.startswith("(?<!\\w)(?:") else "(?<!\\w)(?:" + rx + ")"


# Türkçede ekler sondan gelir: kural kelimenin BAŞINDA başlamalı ("pa-tladı" içinde "atladı" eşleşmesin)
TOPICS = [(_wb(a), b, c) for a, b, c in TOPICS]
PRIMARY_RULES = [(_wb(a), b) for a, b in PRIMARY_RULES]
SCENE_RULES = [(_wb(a), b) for a, b in SCENE_RULES]
WEAK_RULES = [(_wb(a), b) for a, b in WEAK_RULES]
WORD_MAP = [(_wb(a), b) for a, b in WORD_MAP]


def topic_of(text: str):
    """Tüm metnin konusu -> (İngilizce çapa, o konuya özel b-roll kelimeleri). Bulunamazsa ('', genel b-roll)."""
    t = tr_lower(text)
    best, score = None, 0
    for rx, anchor, broll in TOPICS:
        n = len(re.findall(rx, t))
        if n > score:
            best, score = (anchor, broll), n
    return best if best else ("", GENERIC_BROLL)


def basic_queries(text: str, context: str = "", avoid=None, n: int = 3) -> list:
    anchor, broll = topic_of(context or text)
    t = tr_lower(text)

    def fill(q):
        if "{a}" in q:
            if not anchor:
                return ""
            q = q.replace("{a}", anchor)
        return q.strip()

    def collect(rules):
        res = []
        for rx, qs in rules:
            if re.search(rx, t):
                m = [x for x in (fill(q) for q in qs) if x]
                if m:
                    res.append(m)
        return res

    pool = []
    for rx, qs in PRIMARY_RULES:                 # çok belirgin konular önce
        if re.search(rx, t):
            pool += [x for x in (fill(q) for q in qs) if x and x not in pool]
    nprim = len(pool)
    for depth in range(3):                       # önce her kuralın en iyisi, sonra ikincileri...
        for m in collect(SCENE_RULES):
            if depth < len(m) and m[depth] not in pool:
                pool.append(m[depth])
    try:                                         # cümlenin çevirisinden somut isimler (kural olmayan kelimeler için)
        pq, cq = translation_queries(text)
    except Exception:
        pq, cq = [], []
    rule_words = {w for q in pool for w in q.split()}
    pq = [q for q in pq if q not in pool and not set(q.split()) <= rule_words]
    cq = [q for q in cq if q not in pool and not set(q.split()) <= rule_words]
    prim, pool = pool[:nprim], pool[nprim:]
    if pool:                                     # özel isim en başa; sonra kuralın en iyisi, çeviri araya girer
        pool = pq + [pool[0]] + cq[:1] + pool[1:] + cq[1:]
    else:
        pool = pq + cq
    pool = prim + pool
    for q in dict_queries(text, anchor):         # tek tek kelime sözlüğü: kural tutmayan sahneler için
        if q not in pool:
            pool.append(q)
    for m in collect(WEAK_RULES):                # genel kurallar yalnızca eksik kalanı doldurur
        pool += [x for x in m if x not in pool]
    for q in list(broll) + GENERIC_BROLL:        # hiç kural tutmazsa (örn. "Yüzde 28.") konuya özel b-roll
        if q not in pool:
            pool.append(q)
    bad = {str(a).strip().lower() for a in (avoid or [])}
    return clean_queries([q for q in pool if q not in bad], min_words=1)[:n]


def diversify(scenes: list) -> None:
    """Ardışık iki sahne aynı 1. arama kelimesini kullanmasın (aynı videoyu önermesinler)."""
    prev = None
    for s in scenes:
        qs = s.get("queries") or []
        if len(qs) > 1 and prev and qs[0] == prev:
            qs.append(qs.pop(0))
        prev = qs[0] if qs else None


def fix_scene_queries(scenes: list, text: str) -> None:
    """Her sahnenin arama kelimelerini doğrular, eksikse konuya özel kelimelerle tamamlar, tekrarları dağıtır."""
    for s in scenes:
        qs = clean_queries(s.get("queries"))
        for q in basic_queries(s.get("text", ""), text):
            if len(qs) >= 3:
                break
            if q not in qs:
                qs.append(q)
        s["queries"] = qs[:3]
    diversify(scenes)


REVIEW_SYSTEM = """You are a strict quality checker for the stock-footage search queries of a short vertical video.
You get the whole Turkish script and a numbered list of scenes, each with its line and 3 English queries (best first).
For EVERY scene decide whether query 1 would return footage that clearly matches the line AND the overall topic. A query set is BAD if: it is too generic ("person", "city"), off-topic, abstract, a number or statistic, a literal translation that stock sites would not tag that way, or it could return graphic/disturbing footage (accidents with victims, bodies, gore).
Rewrite ONLY the bad scenes: 3 new queries, 2-4 English words each, best first, using the script's topic noun where it fits (for example "airplane boarding passengers"), and safe non-graphic imagery for deaths and accidents (for example "airplane smoke dark").
Return ONLY JSON: {"fixes":[{"n":1,"queries":["...","...","..."]}]} listing only the scenes you changed; {"fixes":[]} if all are good."""


def review_queries(scenes: list, text: str, key: str) -> int:
    lines = [f"{i + 1}. {s['text']} || " + " ; ".join(s.get("queries") or []) for i, s in enumerate(scenes)]
    out = llm_chat(REVIEW_SYSTEM, "SCRIPT:\n" + text.strip()[:4000] + "\n\nSCENES:\n" + "\n".join(lines), max_tokens=4000)
    i, j = out.find("{"), out.rfind("}")
    if i < 0 or j < 0:
        return 0
    fixed = 0
    for f in json.loads(out[i:j + 1]).get("fixes", []):
        try:
            n = int(f.get("n")) - 1
        except Exception:
            continue
        qs = clean_queries(f.get("queries"))
        if 0 <= n < len(scenes) and len(qs) >= 2:
            scenes[n]["queries"] = qs
            fixed += 1
    return fixed


QUERY_SYSTEM = """You choose stock-footage search queries (Pexels / Pixabay / Mixkit / Coverr) for ONE line of the Turkish voice-over of a short vertical video.
You get the whole script (so you know the topic and mood), the line to illustrate, and optionally queries that were already tried and gave irrelevant results.
Return ONLY JSON: {"queries":["...","...","..."]}
Rules:
- First work out the MAIN TOPIC of the whole script and its anchor noun (for example "airplane"). 3 queries, 2-4 English words each, best first.
- Query 1: the most literal, concrete thing a camera could actually film for this line, using the anchor noun when it fits ("airplane boarding passengers", not "passengers").
- Query 2: a broader but still clearly related visual (same subject, simpler words).
- Query 3: a safe b-roll that fits the topic and mood, for lines that are abstract or about feelings.
- At least two queries must visibly belong to the script's topic. Queries that could fit any video ("person", "city", "people") are not allowed.
- If the line is just a number, statistic, name or abstract claim ("241 people died", "28 percent"), never search the number: show the SUBJECT or the MOOD with safe, non-graphic imagery ("airplane smoke dark", "dark storm clouds", "empty airplane seats"). Never request gore, injured people, bodies or real disaster footage.
- Stock sites search tags, so use common tag words that really exist there: subject + setting or action. Never full sentences.
- No abstract nouns (concept, fact, truth, success, idea), no brand / people / movie names, no words like vertical, 4k, hd, video.
- If queries are listed under AVOID, do not repeat them or close synonyms; come up with a different visual idea."""


def claude_queries(text: str, key: str, context: str = "", avoid=None) -> list:
    parts = []
    if context.strip():
        parts.append("FULL SCRIPT:\n" + context.strip()[:3000])
    parts.append("LINE TO ILLUSTRATE:\n" + text.strip())
    avoid = [str(a).strip() for a in (avoid or []) if str(a).strip()]
    if avoid:
        parts.append("AVOID (already tried, results were not relevant):\n" + "\n".join("- " + a for a in avoid))
    out = llm_chat(QUERY_SYSTEM, "\n\n".join(parts), max_tokens=300, json_mode=True)
    i, j = out.find("{"), out.rfind("}")
    if i < 0 or j < 0:
        raise RuntimeError("Claude beklenen JSON'u döndürmedi.")
    data = json.loads(out[i:j + 1])
    qs = clean_queries(data.get("queries", []))
    bad = {str(a).strip().lower() for a in (avoid or [])}
    qs = [q for q in qs if q not in bad]
    for q in basic_queries(text, context, avoid):      # eksik/geçersizse konuya özel kelimelerle tamamla
        if len(qs) >= 3:
            break
        if q not in qs:
            qs.append(q)
    return qs[:3]


def auto_queries(text: str, context: str = "", avoid=None):
    """-> (arama_kelimeleri, kaynak, hata). Yalnızca yerel sözlük + kurallar."""
    return basic_queries(text, context, avoid), "basit", ""


# --------------------------------------------------------------------------
# Video arama / indirme
# --------------------------------------------------------------------------
def slug_tags(url: str) -> str:
    """Pexels sayfa adresinden (…/video/airplane-flying-over-clouds-3571264/) etiket kelimeleri çıkarır."""
    m = re.search(r"/video/([a-z0-9\-]+?)(?:-\d+)?/?$", url or "")
    return ", ".join(m.group(1).split("-")) if m else ""


def search_pexels(q: str, orientation: str, key: str, per_page: int = 8) -> list:
    params = {"query": q, "per_page": per_page}
    if orientation in ("portrait", "landscape"):
        params["orientation"] = orientation
    d = http_json("https://api.pexels.com/videos/search?" + urllib.parse.urlencode(dict(params, size="medium")),
                  headers={"Authorization": key})              # medium = en az Full HD
    if len(d.get("videos", [])) < 4:
        d = http_json("https://api.pexels.com/videos/search?" + urllib.parse.urlencode(params),
                      headers={"Authorization": key})
    res = []
    for v in d.get("videos", []):
        files = [{"url": f["link"], "w": f.get("width") or 0, "h": f.get("height") or 0}
                 for f in v.get("video_files", [])
                 if f.get("file_type") == "video/mp4" and f.get("link")]
        if files:
            res.append({"source": "Pexels", "id": str(v.get("id")), "page": v.get("url", ""),
                        "thumb": v.get("image", ""), "duration": v.get("duration") or 0,
                        "files": files, "tags": slug_tags(v.get("url", "")), "downloads": 0})
    return res


def search_pixabay(q: str, orientation: str, key: str, per_page: int = 8) -> list:
    params = {"key": key, "q": q[:100], "per_page": min(per_page, 200), "safesearch": "true", "lang": "en"}
    d = http_json("https://pixabay.com/api/videos/?" + urllib.parse.urlencode(params))
    res = []
    for h in d.get("hits", []):
        vids = h.get("videos", {}) or {}
        files = []
        for name in ("large", "medium", "small", "tiny"):
            f = vids.get(name) or {}
            if f.get("url"):
                files.append({"url": f["url"], "w": f.get("width") or 0, "h": f.get("height") or 0})
        if not files:
            continue
        thumb = ((vids.get("medium") or {}).get("thumbnail")
                 or (vids.get("tiny") or {}).get("thumbnail") or "")
        if not thumb and h.get("picture_id"):
            thumb = f"https://i.vimeocdn.com/video/{h['picture_id']}_640x360.jpg"
        res.append({"source": "Pixabay", "id": str(h.get("id")), "page": h.get("pageURL", ""),
                    "thumb": thumb, "duration": h.get("duration") or 0, "files": files,
                    "tags": h.get("tags", ""), "downloads": h.get("downloads") or 0})
    if orientation == "portrait":   # Pixabay'de yön filtresi yok: dikeyleri öne al
        res.sort(key=lambda r: 0 if max(r["files"], key=lambda f: f["h"])["h"] >
                 max(r["files"], key=lambda f: f["w"])["w"] else 1)
    return res


def search_pixabay_photos(q: str, orientation: str, key: str, per_page: int = 30) -> list:
    """Pixabay fotoğraf kütüphanesi (aynı anahtar): videolardan çok daha geniş; sonradan hareketli videoya çevrilir."""
    params = {"key": key, "q": q[:100], "image_type": "photo", "per_page": min(per_page, 200),
              "safesearch": "true", "lang": "en",
              "orientation": "vertical" if orientation == "portrait" else "horizontal"}
    d = http_json("https://pixabay.com/api/?" + urllib.parse.urlencode(params))
    res = []
    for h in d.get("hits", []):
        try:
            url = h.get("largeImageURL") or h.get("webformatURL")
            if not url:
                continue
            res.append({"source": "Pixabay", "id": "p" + str(h.get("id")), "kind": "photo",
                        "page": h.get("pageURL", ""), "thumb": h.get("webformatURL") or h.get("previewURL") or "",
                        "duration": 0, "files": [{"url": url, "w": h.get("imageWidth") or 0, "h": h.get("imageHeight") or 0}],
                        "tags": h.get("tags", ""), "downloads": h.get("downloads") or 0})
        except Exception:
            continue
    return res


def search_commons(q: str, orientation: str, key: str = "", per_page: int = 15) -> list:
    """Wikimedia Commons (anahtarsız, ücretsiz). Yalnızca yedek kaynak: Pixabay yetersiz kalırsa kullanılır."""
    params = {"action": "query", "format": "json", "formatversion": "2", "generator": "search",
              "gsrsearch": "filetype:video " + q[:100], "gsrnamespace": "6", "gsrlimit": str(per_page),
              "prop": "imageinfo", "iiprop": "url|size|mime|mediatype|extmetadata", "iiurlwidth": "480"}
    d = http_json("https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params),
                  headers={"User-Agent": "planner.py/1.0 (local video planner; personal use)"}, timeout=25)
    pages = (d.get("query") or {}).get("pages") or []
    if isinstance(pages, dict):
        pages = list(pages.values())
    res = []
    for pg in pages:
        try:
            ii = (pg.get("imageinfo") or [{}])[0]
            if ii.get("mime") not in ("video/webm", "video/mp4") or not ii.get("url"):
                continue
            title = re.sub(r"\.[A-Za-z0-9]+$", "", str(pg.get("title", "")).replace("File:", ""))
            title = re.sub(r"[_\-]+", " ", title)
            ext = ii.get("extmetadata") or {}
            cats = str((ext.get("Categories") or {}).get("value", "")).replace("_", " ").split("|")
            tags = ", ".join([t for t in [title] + [c.strip() for c in cats if c.strip()] if t])
            w, h = int(ii.get("width") or 0), int(ii.get("height") or 0)
            res.append({"source": "Wikimedia", "id": str(pg.get("pageid") or pg.get("title")),
                        "page": ii.get("descriptionurl", ""), "thumb": ii.get("thumburl", ""),
                        "duration": float(ii.get("duration") or 0) if ii.get("duration") else 10,
                        "files": [{"url": ii["url"], "w": w, "h": h}], "tags": tags, "downloads": 0})
        except Exception:
            continue
    return res


# --------------------------------------------------------------------------
# Puanlı klip önerisi: Pixabay (ve eski Pexels anahtarı varsa Pexels) sonuçlarını etiketlerine göre puanlar.
# - Pixabay kuralı: sonuçlar 24 saat önbellekte tutulur (planner_cache.json) ve dakikada 100 istek aşılmaz.
# - Yapay zeka/model gerekmez; tamamen yerel ve ücretsizdir.
# --------------------------------------------------------------------------
CACHE_FILE = ROOT / "planner_cache.json"
CACHE_TTL = 24 * 3600
_CACHE: dict = {}
_CACHE_LOADED = False
_CACHE_LOCK = threading.Lock()
_PIX_TIMES: list = []
_PIX_LOCK = threading.Lock()


def _cache_load() -> None:
    global _CACHE_LOADED
    if _CACHE_LOADED:
        return
    _CACHE_LOADED = True
    try:
        _CACHE.update(json.loads(CACHE_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass


def cache_get(key: str):
    with _CACHE_LOCK:
        _cache_load()
        v = _CACHE.get(key)
        if v and time.time() - v[0] < CACHE_TTL:
            return v[1]
    return None


def cache_put(key: str, val) -> None:
    with _CACHE_LOCK:
        _cache_load()
        now = time.time()
        _CACHE[key] = [now, val]
        for k in [k for k, v in _CACHE.items() if now - v[0] >= CACHE_TTL]:
            del _CACHE[k]
        while len(_CACHE) > 600:                       # dosya şişmesin: en eskileri at
            del _CACHE[min(_CACHE, key=lambda k: _CACHE[k][0])]
        try:
            CACHE_FILE.write_text(json.dumps(_CACHE, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass


def pixabay_throttle(limit: int = 90) -> None:
    """Pixabay: 60 saniyede en fazla 100 istek. Güvenli tarafta kalıp gerekirse bekler."""
    while True:
        with _PIX_LOCK:
            now = time.time()
            _PIX_TIMES[:] = [t for t in _PIX_TIMES if now - t < 60]
            if len(_PIX_TIMES) < limit:
                _PIX_TIMES.append(now)
                return
            wait = 60 - (now - _PIX_TIMES[0]) + 0.3
        time.sleep(max(wait, 0.5))


def cached_search(name: str, q: str, orientation: str, key: str) -> list:
    ck = f"{name}|{orientation}|{q.lower()}"
    hit = cache_get(ck)
    if hit is not None:
        return hit
    if name == "Pixabay":
        pixabay_throttle()
        res = search_pixabay(q, orientation, key, per_page=30)
    elif name == "PixabayPhoto":
        pixabay_throttle()
        res = search_pixabay_photos(q, orientation, key)
    elif name == "Wikimedia":
        res = search_commons(q, orientation, key)
    else:
        res = search_pexels(q, orientation, key, per_page=12)
    cache_put(ck, res)
    return res


CLIP_STOP = {"a", "an", "the", "of", "in", "on", "at", "with", "and", "for", "to", "close", "up", "view", "shot"}
SYNONYMS = [
    {"airplane", "plane", "aircraft", "airliner", "jet", "flight", "aviation", "airline", "airport"},
    {"car", "auto", "automobile", "vehicle"},
    {"people", "person", "man", "woman", "crowd", "persons", "human"},
    {"phone", "smartphone", "mobile", "cellphone"},
    {"passenger", "passengers", "traveler", "travelers", "tourist", "traveller"},
    {"smoke", "fire", "fog", "haze", "burning"},
    {"dark", "night", "black", "dim"},
    {"sky", "clouds", "cloud"},
    {"seat", "seats", "chair", "armchair"},
    {"cabin", "interior", "inside"},
    {"exit", "door", "doorway"},
    {"money", "cash", "dollar", "currency"},
    {"brain", "mind", "neurons", "neuron"},
    {"doctor", "physician", "medical", "hospital"},
    {"ocean", "sea", "water", "waves"},
]


def _stem(w: str) -> str:
    w = w.lower()
    for suf in ("ing", "es", "s", "ed"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[:-len(suf)]
    return w


def _group(word: str) -> set:
    st = _stem(word)
    g = {st, word.lower()}
    for syn in SYNONYMS:
        if word.lower() in syn or st in {_stem(x) for x in syn}:
            g |= {_stem(x) for x in syn} | {x for x in syn}
    return g


def tag_match(query: str, tags: str):
    """(kapsama 0..1, eşleşen sorgu kelimeleri): sorgu kelimelerinin kaçı klibin etiketlerinde (eş anlamlılarıyla) var."""
    toks = set()
    for w in re.findall(r"[a-z0-9]+", str(tags).lower()):
        toks |= {w, _stem(w)}
    qw = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in CLIP_STOP and not w.isdigit()]
    if not qw:
        return 0.0, []
    matched = [w for w in qw if _group(w) & toks]
    return len(matched) / len(qw), matched


def clip_score(r: dict, query: str, rank: int, anchor: str, orientation: str):
    """0..1. Ağırlığın büyük kısmı ALAKA (etiket örtüşmesi x sorgu sırası); süre/yön/popülerlik yalnızca küçük bonus."""
    import math
    cov, matched = tag_match(query, r.get("tags", ""))
    toks = set()
    for w in re.findall(r"[a-z0-9]+", str(r.get("tags", "")).lower()):
        toks |= {w, _stem(w)}
    sc = 0.85 * cov * (1.0, 0.6, 0.4)[min(rank, 2)]       # 1. sorgu en birebir; 2. ve 3. sorgu (genel b-roll) daha az güvenilir
    if anchor and _group(anchor) & toks:
        sc += 0.04                                           # etikette konu kelimesi de geçiyor
    d = r.get("duration") or 0
    if r.get("kind") == "photo":
        sc -= 0.02                                           # eşit alakada gerçek video biraz önde
    else:
        sc += 0.04 if 3 <= d <= 40 else -0.05                # çok kısa klip sahneyi doldurmaz
    best = max(r["files"], key=lambda f: f["w"] * f["h"]) if r.get("files") else {"w": 0, "h": 0}
    if (orientation == "portrait") == (best["h"] > best["w"]):
        sc += 0.06
    if r.get("kind") != "photo":                             # düşük çözünürlüklü klip geri düşsün
        side = max(best["w"], best["h"])
        sc += 0.03 if side >= 1920 else (-0.08 if side < 1080 else 0.0)
    sc += min(0.02, math.log10(1 + (r.get("downloads") or 0)) / 200)
    return max(0.0, min(1.0, sc)), matched


def query_variants(q: str, rank: int) -> list:
    """Pixabay çok kelimeli aramada HEPSİNİ arar (AND); sonuç azsa sorguyu kısaltıp tekrar dener."""
    w = [x for x in re.findall(r"[a-z0-9]+", q.lower()) if x not in CLIP_STOP and not x.isdigit()]
    vs = []
    if len(w) >= 3:
        vs.append(" ".join(w[:2]))
    if len(w) >= 2:
        vs.append(w[0])
        if w[-1] != w[0]:
            vs.append(w[-1])
    too_generic = {"person", "people", "man", "woman", "city", "sky", "night", "dark", "aerial", "close", "street", "hands"}
    vs = [v for v in dict.fromkeys(vs) if v != q.lower() and v not in too_generic]
    return vs if rank == 0 else vs[:2] if rank == 1 else []


def collect_clips(queries: list, orientation: str, text: str, context: str = "", extra=None):
    """Sahne için aday klipleri toplar ve puanlar -> (en iyi 6 klip, hata metni).
    Önce tam sorgu aranır; yeterince güçlü sonuç yoksa sorgu kısaltılarak genişletilir.
    Etiketleri sorguyla yeterince örtüşmeyen klipler hiç gösterilmez."""
    keys = load_keys()
    srcs = [(n, k) for n, k in (("Pixabay", keys["pixabay"]), ("PixabayPhoto", keys["pixabay"]),
                                ("Pexels", keys["pexels"])) if k]
    if not srcs:
        return [], ""
    anchor, _ = topic_of(context or text)
    best, errors = {}, []

    def strong() -> int:
        return sum(1 for v in best.values() if v[0] >= 0.6)

    def run(q: str, rank: int, search_q: str, only=None) -> None:
        for name, key in (only or srcs):
            try:
                res = cached_search(name, search_q, orientation, key)
            except Exception as e:
                if name in ("Wikimedia", "PixabayPhoto"):
                    continue                              # yardımcı kaynak: hata olursa sessizce atla
                msg = f"{name}: {e}"
                if "429" in msg:
                    msg = f"{name}: istek limiti doldu, bir dakika bekleyip tekrar dene."
                if msg not in errors:
                    errors.append(msg)
                continue
            for r in res:
                try:
                    cov, _m = tag_match(q, r.get("tags", ""))
                    if r.get("tags") and cov < (0.5 if rank == 0 else 0.67):
                        continue                          # etiketi aramayla yeterince örtüşmüyor: alakasız
                    sc, matched = clip_score(r, q, rank, anchor, orientation)
                    if r["source"] == "Wikimedia":
                        sc -= 0.05                        # genelde düşük kalite/yatay: Pixabay eşitse Pixabay önde
                    tag = (r["source"], r["id"])
                    if tag not in best or sc > best[tag][0]:
                        best[tag] = (sc, matched, q, r)
                except Exception:
                    continue

    for rank, q in enumerate(queries[:3]):
        if strong() >= 6:
            break
        run(q, rank, q)
        for v in query_variants(q, rank):
            if strong() >= 6:
                break
            run(q, rank, v)
    if strong() < 3 and srcs:                            # 2) Pixabay yetmedi: otomatik yeni sorgular dene
        for q in [x for x in (extra or []) if x not in queries][:4]:
            if strong() >= 3:
                break
            try:
                run(q, 2, q)
            except Exception:
                pass
    if strong() < 3:                                     # 3) hâlâ yetmedi: anahtarsız Wikimedia yedeği (hata verirse sessizce atlanır)
        try:
            for q in queries[:2]:
                for v in [q] + query_variants(q, 0)[:2]:
                    if strong() >= 3:
                        break
                    run(q, 0, v, only=[("Wikimedia", "")])
        except Exception:
            pass
    ranked = [x for x in sorted(best.values(), key=lambda x: -x[0]) if x[0] >= 0.30]   # zayıf eşleşmeyi hiç gösterme
    vids = [x for x in ranked if x[3].get("kind") != "photo"]
    phs = [x for x in ranked if x[3].get("kind") == "photo"]
    pick = vids[:3] + phs[:3]                             # hem video hem fotoğraf seçeneği göster
    if len(pick) < 6:
        rest = sorted([x for x in ranked if x not in pick], key=lambda x: -x[0])
        pick += rest[:6 - len(pick)]
    clips = []
    for sc, matched, q, r in sorted(pick, key=lambda x: -x[0]):
        clips.append({"source": r["source"], "id": r["id"], "kind": r.get("kind", "video"), "orient": orientation,
                      "page": r.get("page", ""), "thumb": r.get("thumb", ""),
                      "duration": r.get("duration") or 0, "files": r["files"][:4],
                      "tags": ", ".join([t.strip() for t in str(r.get("tags", "")).split(",") if t.strip()][:8]),
                      "score": round(sc * 100), "matched": matched, "q": q})
    if clips and clips[0]["score"] < 60 and not errors:
        errors.append("Bu sahne için güçlü eşleşme çıkmadı; en yakınları gösteriliyor. 'Arama kelimeleri' "
                      "kutusuna sahnenin görüntüsünü basit, tek-iki İngilizce kelimeyle yaz "
                      "(örn. skydiving, falling clouds) ve 'Video bul'a tekrar bas.")
    elif not clips and not errors:
        errors.append("Bu sahneyle gerçekten örtüşen klip bulunamadı (alakasızlar bilerek gösterilmedi). "
                      "'Arama kelimeleri' kutusuna daha basit, tek-iki kelimelik İngilizce yaz "
                      "(örn. skydiving) ya da 'Başka kelimelerle öner'e bas.")
    return clips, " ".join(errors)


def do_search(queries: list, orientation: str, text: str = "") -> dict:
    keys = load_keys()
    if not keys["pexels"] and not keys["pixabay"]:
        raise RuntimeError("Pexels veya Pixabay API anahtarı yok. Üstteki 'API anahtarları' düğmesinden ekle.")
    queries = [q for q in (queries or []) if str(q).strip()]
    if not queries:
        queries = auto_queries(text)[0]
    if not queries:
        raise RuntimeError("Bu sahne için arama kelimesi üretilemedi. Sahne metnini uzat ya da "
                           "arama kelimesini kendin yaz (ya da 'Başka kelimelerle öner'e bas).")
    errors, per_source = [], {"Pexels": [], "Pixabay": []}
    for q in queries[:3]:
        for name, fn, key in (("Pexels", search_pexels, keys["pexels"]),
                              ("Pixabay", search_pixabay, keys["pixabay"])):
            if not key:
                continue
            try:
                per_source[name].extend(fn(q, orientation, key))
            except Exception as e:
                msg = f"{name}: {e}"
                if msg not in errors:
                    errors.append(msg)
    merged, seen = [], set()
    lists = [per_source["Pexels"], per_source["Pixabay"]]
    for k in range(max(len(lists[0]), len(lists[1]), 1)):   # kaynakları sırayla karıştır
        for lst in lists:
            if k < len(lst):
                r = lst[k]
                tag = (r["source"], r["id"])
                if tag not in seen:
                    seen.add(tag)
                    merged.append(r)
    return {"results": merged[:14], "errors": errors, "queries": queries[:3]}


def choose_file(files: list) -> dict:
    def score(f):
        side = max(f["w"], f["h"])
        pen = abs(side - 1920)
        if side < 1080:
            pen += 5000      # çok düşük çözünürlüğü sona at
        return pen
    return min(files, key=score)


def download(url: str, dest: Path) -> None:
    host = (urlparse(url).hostname or "").lower()
    if not url.startswith("https://") or not any(host == h or host.endswith("." + h)
                                                  for h in ALLOWED_DOWNLOAD_HOSTS):
        raise RuntimeError(f"İzin verilmeyen indirme adresi: {host}")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    tmp = dest.with_suffix(".part")
    try:
        with urllib.request.urlopen(req, timeout=180) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        tmp.replace(dest)
    except urllib.error.URLError as e:
        raise RuntimeError(f"İndirilemedi: {e}")
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def do_links(queries: list, orientation: str, text: str = "", context: str = "", avoid=None) -> dict:
    """API'siz: sahne için stok video sitelerinin arama sayfası linklerini üretir.
    avoid doluysa (\"Başka kelimelerle öner\") eski kelimeler atılır ve Claude'dan farklı fikirler istenir."""
    avoid = [str(a).strip() for a in (avoid or []) if str(a).strip()]
    queries = [] if avoid else [str(q).strip() for q in (queries or []) if str(q).strip()]
    source, err = "elle", ""
    if not queries:
        queries, source, err = auto_queries(text, context, avoid)
    if not queries:
        raise RuntimeError("Arama kelimesi üretilemedi. Kutuya İngilizce arama kelimesi yaz.")
    port = orientation == "portrait"
    links = []
    for q in queries[:3]:
        u = urllib.parse.quote(q)
        links += [
            {"site": "Pexels", "q": q, "url": f"https://www.pexels.com/search/videos/{u}/?orientation=" + ("portrait" if port else "landscape")},
            {"site": "Pixabay", "q": q, "url": f"https://pixabay.com/videos/search/{u}/?orientation=" + ("vertical" if port else "horizontal")},
            {"site": "Mixkit", "q": q, "url": f"https://mixkit.co/free-stock-video/search/?q={u}"},
            {"site": "Coverr", "q": q, "url": f"https://coverr.co/s?q={u}"},
        ]
    try:
        extra = basic_queries(text, context, avoid=queries, n=8)
    except Exception:
        extra = []
    try:
        clips, clip_error = collect_clips(queries, orientation, text, context, extra)
    except Exception as e:
        clips, clip_error = [], "Klip önerisi şu an alınamadı; aşağıdaki arama linklerini kullanabilirsin."
    keys = load_keys()
    warning = ""
    if source == "basit":
        warning = ""        # anahtarsız kullanım normal: uyarı gösterme
    return {"queries": queries[:3], "links": links, "source": source, "warning": warning,
            "clips": clips, "clip_error": clip_error, "need_key": not (keys["pixabay"] or keys["pexels"])}


def do_upload(q: dict, rfile, n: int) -> dict:
    """Tarayıcıdan gelen videoyu inputs/videos içine NN_slug.uzanti adıyla kaydeder."""
    ext = Path(q.get("name", [""])[0]).suffix.lower()
    if ext not in (".mp4", ".mov", ".webm", ".mkv", ".m4v"):
        raise RuntimeError("Yalnızca video dosyası eklenebilir (mp4, mov, webm, mkv).")
    idx = int(q.get("index", ["1"])[0])
    slug = re.sub(r"[^a-z0-9]+", "_", q.get("slug", [""])[0].lower()).strip("_") or "clip"
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{idx:02d}_{slug}{ext}"
    old = q.get("old_file", [""])[0]
    if old and old != name:
        op = (VIDEO_DIR / old).resolve()
        if VIDEO_DIR.resolve() in op.parents and op.is_file():
            op.unlink()
    tmp = VIDEO_DIR / (name + ".part")
    with open(tmp, "wb") as f:
        left = n
        while left > 0:
            chunk = rfile.read(min(1 << 20, left))
            if not chunk:
                break
            f.write(chunk)
            left -= len(chunk)
    tmp.replace(VIDEO_DIR / name)
    return {"file": name, "size_mb": round((VIDEO_DIR / name).stat().st_size / 1e6, 1)}


AUDIO_NAMES = ["audio.mp3", "audio.wav", "audio.m4a", "audio.aac", "audio.ogg", "audio.flac"]   # pipeline.py ile aynı
OUTPUT_VIDEO = ROOT / "output_video.mp4"
RUN = {"proc": None, "lines": [], "status": "idle", "stopping": False, "thumb": None, "name": "", "export": None}


def make_export_name(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", tr_fold(text))[:5]
    slug = "-".join(words)[:40].strip("-") or "video"
    return time.strftime("%Y-%m-%d_%H-%M") + "_" + slug + ".mp4"


def export_copy() -> Path:
    """Son videoyu ciktilar klasörüne kopyalar (aynı üretim için aynı dosya adı, tekrar eklemede üzerine yazar)."""
    EXPORT_DIR.mkdir(exist_ok=True)
    if not RUN.get("name"):
        RUN["name"] = make_export_name("")
    dest = EXPORT_DIR / RUN["name"]
    shutil.copy2(OUTPUT_VIDEO, dest)
    RUN["export"] = dest
    return dest
RUN_LOCK = threading.Lock()
CHOICES = {"model": ("medium", "large-v3", "small", "base", "tiny"), "style": ("kelime", "grup"), "highlight": ("sari", "cyan")}


def do_audio(q: dict, rfile, n: int) -> dict:
    ext = Path(q.get("name", [""])[0]).suffix.lower()
    if ext not in {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}:
        raise RuntimeError("Ses dosyası mp3, wav, m4a, aac, ogg veya flac olmalı.")
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INPUT_DIR / ("audio" + ext + ".part")
    left = n
    with open(tmp, "wb") as f:
        while left > 0:
            chunk = rfile.read(min(1 << 20, left))
            if not chunk:
                break
            f.write(chunk)
            left -= len(chunk)
    if left:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Ses dosyası eksik yüklendi, tekrar dene.")
    for nm in AUDIO_NAMES:          # pipeline yanlış sesi seçmesin diye eski ses dosyalarını sil
        (INPUT_DIR / nm).unlink(missing_ok=True)
    tmp.replace(INPUT_DIR / ("audio" + ext))
    return {"file": "audio" + ext}


def _pump(proc) -> None:
    for line in proc.stdout:
        RUN["lines"].append(line.rstrip())
    rc = proc.wait()
    if RUN["stopping"]:
        RUN["lines"].append("■ Durduruldu.")
        RUN["status"], RUN["stopping"] = "idle", False
    elif rc == 0 and OUTPUT_VIDEO.is_file():
        try:
            shutil.copy2(OUTPUT_VIDEO, RAW_VIDEO)          # kapaksız ham kopya
            if RUN.get("thumb") and THUMB_FILE.is_file():
                RUN["lines"].append("-> Onaylı kapak videonun başına ekleniyor...")
                add_thumb_intro(RUN["thumb"])
                RUN["lines"].append("✓ Kapak eklendi.")
            elif RUN.get("thumb"):
                RUN["lines"].append("ℹ Onaylı kapak yok, kapak eklenmedi.")
        except Exception as e:
            RUN["lines"].append(f"⚠ Kapak eklenemedi: {e}")
        try:
            RUN["lines"].append(f"✓ Kaydedildi: ciktilar/{export_copy().name}")
        except Exception as e:
            RUN["lines"].append(f"⚠ ciktilar klasörüne kopyalanamadı: {e}")
        RUN["status"] = "done"
    else:
        RUN["lines"].append(f"✖ Üretim hata verdi (çıkış kodu {rc}).")
        RUN["status"] = "error"


def do_run(body: dict) -> dict:
    with RUN_LOCK:
        if RUN["proc"] and RUN["proc"].poll() is None:
            raise RuntimeError("Üretim zaten sürüyor.")
        if not any((INPUT_DIR / n).is_file() for n in AUDIO_NAMES):
            raise RuntimeError("Önce ses dosyasını ekle (Üretim bölümündeki ses kutusu).")
        scenes = body.get("scenes", [])
        if not scenes:
            raise RuntimeError("Önce sahneleri oluştur.")
        if not (ROOT / "pipeline.py").is_file():
            raise RuntimeError("pipeline.py bulunamadı; planner.py ile aynı klasörde olmalı.")
        do_write(scenes)                                  # eslesme.txt + metin.txt güncel olsun
        o = body.get("opts") or {}
        pick = lambda k: o.get(k) if o.get(k) in CHOICES[k] else CHOICES[k][0]
        try:
            margin = max(0, min(1200, int(o.get("margin") or 400)))
        except Exception:
            margin = 400
        py = Path(sys.executable).with_name("python.exe")
        cmd = [str(py if py.is_file() else sys.executable), "-u", str(ROOT / "pipeline.py"), "--match", "kelime",
               "--min-scene", "1.0", "--model", pick("model"), "--style", pick("style"),
               "--highlight", pick("highlight"), "--margin-v", str(margin), "--output", str(OUTPUT_VIDEO)]
        if o.get("upper") is False:
            cmd.append("--no-uppercase")
        if o.get("clean"):
            cmd.append("--clean-audio")
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", bufsize=1,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
                                creationflags=0x08000000 if sys.platform.startswith("win") else 0)
        th = max(1, min(150, int(o.get("thumb_frames") or 1))) if o.get("thumb") else None
        RUN.update(proc=proc, lines=["▶ Üretim başlatıldı."], status="running", stopping=False, thumb=th,
                   name=make_export_name(" ".join(str(x.get("text", "")) for x in scenes)), export=None)
        threading.Thread(target=_pump, args=(proc,), daemon=True).start()
    return {"ok": True}


def do_new_project() -> dict:
    """Yeni proje: bu projeye ait çalışma dosyalarını siler (sahneler, indirilen videolar, ses, kapak, ham çıktı).
    Üretilen bitmiş videolar 'ciktilar' klasöründe kalır; font.ttf ve API anahtarına dokunulmaz."""
    with RUN_LOCK:
        if RUN["proc"] and RUN["proc"].poll() is None:
            raise RuntimeError("Üretim sürerken yeni proje başlatılamaz. Önce durdur.")
        do_disarm()
        removed, failed = 0, []

        def rm(p: Path) -> None:
            nonlocal removed
            try:
                if p.is_file():
                    p.unlink()
                    removed += 1
            except OSError as e:
                failed.append(f"{p.name}: {e}")

        if VIDEO_DIR.is_dir():
            for p in VIDEO_DIR.iterdir():
                if p.is_file() and (p.suffix.lower() in VIDEO_EXTS or p.suffix.lower() in TEMP_EXTS):
                    rm(p)
        if INPUT_DIR.is_dir():
            own = {"eslesme.txt", "eslesme.txt.bak", "metin.txt", "metin.txt.bak", "thumbnail.png"}
            for p in INPUT_DIR.iterdir():
                if not p.is_file():
                    continue
                n = p.name.lower()
                if (p.suffix.lower() in AUDIO_EXTS or n in own or n.startswith("_thumb")
                        or (n.startswith("audio") and n.endswith(".part"))):
                    rm(p)
        if AI_REF_DIR.is_dir():
            for p in AI_REF_DIR.iterdir():
                if p.is_file():
                    rm(p)
        for p in (OUTPUT_VIDEO, RAW_VIDEO, ROOT / "output_video.kapak.tmp.mp4", PROJECT_FILE):
            rm(p)
        RUN.update(proc=None, lines=[], status="idle", stopping=False, thumb=None, name="", export=None)
    if failed:
        raise RuntimeError("Bazı dosyalar silinemedi (başka program kullanıyor olabilir): " + "; ".join(failed[:3]))
    return {"ok": True, "removed": removed}


def do_stop() -> dict:
    p = RUN["proc"]
    if p and p.poll() is None:
        RUN["stopping"] = True
        p.terminate()
    return {"ok": True}


def do_reveal() -> dict:
    target = RUN.get("export") if RUN.get("export") and Path(RUN["export"]).is_file() else OUTPUT_VIDEO
    if sys.platform.startswith("win"):
        subprocess.Popen(["explorer", "/select,", str(target)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(ROOT)])
    return {"ok": True}


AI_STYLE = ("photorealistic live-action cinematic documentary photograph, looks like a real frame captured "
            "with a professional full-frame DSLR cinema camera, realistic human skin pores and facial details, "
            "natural anatomy and body proportions, physically accurate light and shadows, believable real-world "
            "materials and environments, subtle film grain, natural cinematic color grading, vertical 9:16-friendly "
            "composition with the main subject inside the central safe area, clear foreground and background, "
            "no illustration, no drawing, no hand-drawn look, no 2D animation, no cartoon, no anime, no 3D render, "
            "no text, no captions, no UI, no collage, no split screen, no duplicate people, no extra limbs or fingers, "
            "no watermark, no logos, non-graphic")

AI_PROMPT_SYSTEM = (
    "You write ONE detailed English image-generation prompt for a photorealistic live-action image model. "
    "The user message contains a Turkish voice-over line (SCENE), the full script (CONTEXT), and a VIDEO "
    "CONTINUITY GUIDE. Depict exactly what the SCENE says in one concrete, filmable moment: subject, action, "
    "setting, camera distance and angle, lighting, and emotion. The continuity guide is mandatory: whenever a "
    "recurring person appears, repeat the SAME identity details exactly (age, face shape, skin tone, hair, facial "
    "hair, build, signature clothing and distinguishing features); do not redesign, replace, age, or randomly alter "
    "that person between scenes. Keep recurring locations, era, props and color palette consistent too. Only show "
    "a character when the scene calls for them; do not add random people. Translate abstract claims into a clear, "
    "topic-specific visual metaphor without changing the story. Aim for 60-100 words. Strictly photorealistic, "
    "like a real film still, never illustration/cartoon/2D/3D. Non-graphic: no gore, injuries or bodies. No text, "
    "captions, logos or watermarks. Return ONLY the prompt.")

AI_DIR = ROOT / "ai_gorseller"
AI_REF_DIR = AI_DIR / "referanslar"
_AI_PROMPT_CACHE = {}

STYLE_CARD_SYSTEM = (
    "Create a LOCKED VISUAL CONTINUITY BIBLE for all images in one short film, based on the Turkish script. "
    "Write 70-110 English words that will be prefixed UNCHANGED onto every scene prompt. It MUST specify: "
    "(1) photorealistic live-action film-still look, never illustration; "
    "(2) one fixed cinematic color palette, white-balance and lighting recipe (e.g. warm tungsten / cool daylight); "
    "(3) recurring main character identity in concrete repeatable detail if the story has one: approximate age, "
    "gender presentation, face shape, skin tone, hair, facial hair, build, exact outfit and one distinctive feature; "
    "(4) recurring location, period and important props. If there is no recurring human character, explicitly say so "
    "and define the recurring visual world instead. Do not invent a protagonist if the script does not imply one. "
    "Keep details concise and unambiguous. Return ONLY the guide, no label or quotation marks.")

_STYLE_CARD_CACHE = {}


def style_card_for_script(context: str) -> str:
    """Tüm video için BİR KEZ üretilir: genel görsel stil, renk paleti, tekrar eden karakter/nesne.
    Her sahnenin AI görsel prompt'unun başına eklenir ki sahneler birbirine benzesin (bkz. do_aigen).
    Senaryo metnine göre önbelleğe alınır; LLM yoksa (anahtar girilmemişse) sessizce boş döner."""
    ctx = (context or "").strip()
    if not ctx:
        return ""
    key = zlib.crc32(ctx[:4000].encode("utf-8"))
    if key in _STYLE_CARD_CACHE:
        return _STYLE_CARD_CACHE[key]
    out = ""
    if llm_providers():
        try:
            out = llm_chat(STYLE_CARD_SYSTEM, ctx[:4000], max_tokens=220).strip().strip('"')
        except Exception:
            out = ""
    _STYLE_CARD_CACHE[key] = out
    return out


def cf_ready() -> bool:
    k = load_keys()
    return bool(k.get("cf_token") and k.get("cf_account"))


def ai_prompt_from_scene(text: str, context: str) -> str:
    """Görsel prompt'u doğrudan sahne metninden yazar (arama/tetik kelimelerine bakmaz)."""
    key = zlib.crc32((text + "|" + context[:2000]).encode("utf-8"))
    if key in _AI_PROMPT_CACHE:
        return _AI_PROMPT_CACHE[key]
    out = ""
    if llm_providers():
        try:
            guide = style_card_for_script(context or text)
            out = llm_chat(AI_PROMPT_SYSTEM,
                           f"VIDEO CONTINUITY GUIDE:\n{guide}\n\nSCENE:\n{text}\n\nFULL CONTEXT:\n{context[:3500]}",
                           max_tokens=420).strip().strip('"')
        except Exception:
            out = ""
    if not out:
        out = translate_tr_en(text) or text
    out = " ".join(w for w in out.split() if re.sub(r"[^a-z]", "", w.lower()) not in UNSAFE_QUERY_WORDS)
    _AI_PROMPT_CACHE[key] = out
    return out


def project_base_seed(context: str, continuity_guide: str = "") -> int:
    """Tüm proje için TEK sabit seed: senaryo + süreklilik notundan üretilir, sahne metnine bağlı değildir.
    Böylece her AI görsel aynı 'görsel aile'den gelir; 'yeniden üret' sadece küçük ofset ekler."""
    material = (context or "").strip()[:4000] + "|" + (continuity_guide or "").strip()[:1500]
    if not material.strip("|"):
        material = "sahne-planlayici-default"
    return zlib.crc32(material.encode("utf-8")) % 90000


def cf_generate_image(prompt: str, seed: int, dest: Path) -> None:
    """Cloudflare Workers AI / FLUX.1 schnell (günlük ücretsiz kota, filigran yok). Çıktı 1024x1024 JPEG.
    FLUX.1 Schnell seed kabul etmez; tutarlılık zorunlu stil öneki + stil kartı ile sağlanır.
    seed yalnızca dosya adı / varyasyon için kullanılır."""
    k = load_keys()
    url = (f"https://api.cloudflare.com/client/v4/accounts/{k['cf_account'].strip()}"
           "/ai/run/@cf/black-forest-labs/flux-1-schnell")
    code, data, _ = _post_json(url, {"Authorization": "Bearer " + k["cf_token"].strip()},
                               {"prompt": prompt[:2000], "steps": 8}, 120)
    if code in (401, 403):
        raise RuntimeError("Cloudflare anahtarı reddedildi: API Token'ın 'Workers AI' iznine ve Account ID'ye bak.")
    if code == 429 or (code != 200 and "neuron" in json.dumps(data).lower()):
        raise RuntimeError("Cloudflare'ın bugünkü ücretsiz kotası doldu; yarın yenilenir. Şimdilik stok video seç.")
    img = ((data.get("result") or {}).get("image") or data.get("image")) if isinstance(data, dict) else None
    if code != 200 or not img:
        raise RuntimeError(f"Cloudflare görsel üretemedi (HTTP {code}): {str(data)[:160]}")
    raw = base64.b64decode(img)
    if len(raw) < 5000:
        raise RuntimeError("Cloudflare geçersiz görsel döndürdü, tekrar dene.")
    dest.write_bytes(raw)



def prepare_reference_image(raw: bytes, dest: Path) -> None:
    """Referansları FLUX.2 Klein gereksinimi olan 512x512 altına indirip JPEG saklar."""
    if not raw or len(raw) > 12 * 1024 * 1024:
        raise RuntimeError("Referans görsel boş veya 12 MB sınırını aşıyor.")
    tmp = dest.with_suffix(".source")
    tmp.write_bytes(raw)
    try:
        try:
            from PIL import Image
            import io
            im = Image.open(io.BytesIO(raw)).convert("RGB")
            im.thumbnail((480, 480), Image.Resampling.LANCZOS)
            im.save(dest, "JPEG", quality=88, optimize=True)
        except ImportError:
            ff = shutil.which("ffmpeg")
            if not ff:
                raise RuntimeError("Referans görselini küçültmek için Pillow veya ffmpeg gerekli.")
            r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", str(tmp),
                                "-vf", "scale=480:480:force_original_aspect_ratio=decrease", "-frames:v", "1",
                                "-q:v", "3", str(dest)], capture_output=True, text=True, timeout=30)
            if r.returncode or not dest.is_file():
                raise RuntimeError("Referans görseli okunamadı; JPG veya PNG deneyin.")
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    finally:
        tmp.unlink(missing_ok=True)
    if not dest.is_file() or dest.stat().st_size < 500:
        raise RuntimeError("Referans görseli işlenemedi.")


def do_reference_upload(q: dict, rfile, n: int) -> dict:
    ext = Path((q.get("name") or [""])[0]).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        raise RuntimeError("Karakter/stil referansı PNG, JPG veya WEBP olmalı.")
    raw = rfile.read(min(max(n, 0), 12 * 1024 * 1024 + 1))
    AI_REF_DIR.mkdir(parents=True, exist_ok=True)
    name = "ref_" + uuid.uuid4().hex[:14] + ".jpg"
    dest = AI_REF_DIR / name
    prepare_reference_image(raw, dest)
    return {"name": name, "url": "/airef/" + name, "size_kb": round(dest.stat().st_size / 1024)}


def cf_generate_image_with_refs(prompt: str, seed: int, dest: Path, refs: list, width: int = 1024, height: int = 1024) -> None:
    """Cloudflare Workers AI FLUX.2 Klein 9B; en fazla dört 512px referans görseli alır."""
    k = load_keys()
    boundary = "----PlannerFlux2" + uuid.uuid4().hex
    fields = [("prompt", prompt[:4000].encode("utf-8")), ("width", str(width).encode("ascii")),
              ("height", str(height).encode("ascii")), ("seed", str(seed).encode("ascii"))]
    chunks = []
    for key, value in fields:
        chunks.extend([f"--{boundary}\r\n".encode(),
                       f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(), value, b"\r\n"])
    for i, ref in enumerate(refs[:4]):
        chunks.extend([f"--{boundary}\r\n".encode(),
                       f'Content-Disposition: form-data; name="input_image_{i}"; filename="reference_{i}.jpg"\r\n'.encode(),
                       b"Content-Type: image/jpeg\r\n\r\n", ref, b"\r\n"])
    chunks.append(f"--{boundary}--\r\n".encode())
    payload = b"".join(chunks)
    url = (f"https://api.cloudflare.com/client/v4/accounts/{k['cf_account'].strip()}"
           "/ai/run/@cf/black-forest-labs/flux-2-klein-9b")
    req = urllib.request.Request(url, data=payload, headers={
        "User-Agent": UA, "Authorization": "Bearer " + k["cf_token"].strip(),
        "Content-Type": "multipart/form-data; boundary=" + boundary}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as response:
            data = json.loads(response.read().decode("utf-8"))
            code = response.status
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if e.code == 429:
            raise RuntimeError("Cloudflare görsel üretim kotası doldu. Bir süre sonra tekrar dene.")
        raise RuntimeError(f"FLUX.2 Klein HTTP {e.code}: {detail}")
    if code != 200:
        raise RuntimeError(f"FLUX.2 Klein görsel üretemedi (HTTP {code}).")
    img = ((data.get("result") or {}).get("image") or data.get("image")) if isinstance(data, dict) else None
    if not img:
        raise RuntimeError("FLUX.2 Klein görsel verisi döndürmedi.")
    raw = base64.b64decode(img)
    if len(raw) < 5000:
        raise RuntimeError("Cloudflare geçersiz görsel döndürdü, tekrar dene.")
    dest.write_bytes(raw)


def do_reference_generate(body: dict) -> dict:
    """Senaryoyu analiz eder; tekrar eden karakterleri çıkarır ve her biri için portre referansı üretir."""
    if not cf_ready():
        raise RuntimeError("Otomatik karakter referansı için önce Cloudflare Account ID ve Workers AI token ekle.")
    context = str(body.get("context") or "").strip()
    manual_guide = str(body.get("continuity_guide") or "").strip()
    if len(context) < 20:
        raise RuntimeError("Önce senaryoyu/metni ekle; karakterleri metinden çıkaracağım.")
    if not llm_providers():
        raise RuntimeError("Senaryoyu karakterlere ayırmak için API anahtarlarından en az birini ekle (Groq veya Cloudflare Workers AI yeterli).")

    system = (
        "You are a meticulous film casting director and visual continuity supervisor. Analyze the FULL Turkish script. "
        "Return valid JSON only with keys visual_bible and characters. visual_bible is a concise English continuity guide "
        "for the film's photographic style, palette, lighting, period, recurring locations and props. characters is an array "
        "of up to four important recurring or clearly identifiable human characters, ordered by importance. For each use keys "
        "name, description, role. description must contain stable visible identity only: approximate age, gender presentation, "
        "face shape, skin tone, eyes, hair, facial hair, build, signature clothing and distinctive features. Use details stated "
        "in the script; if unstated, make restrained consistent casting choices and mark them as visual interpretation, not plot fact. "
        "Do not create a character for a passing crowd or invent people when the script has none. Do not merge distinct characters. "
        "Keep each description specific enough to reproduce the same person in every scene. Return no markdown."
    )
    user = "USER VISUAL NOTES (optional):\n" + manual_guide[:2000] + "\n\nFULL TURKISH SCRIPT:\n" + context[:10000]
    try:
        extracted = parse_json_obj(llm_chat(system, user, max_tokens=1100, json_mode=True))
    except Exception as e:
        raise RuntimeError("Senaryodan karakter analizi yapılamadı: " + str(e)[:220])

    bible = str(extracted.get("visual_bible") or "").strip()[:2200]
    if manual_guide:
        bible = (bible + " User direction: " + manual_guide).strip()[:2500]
    characters = extracted.get("characters") if isinstance(extracted.get("characters"), list) else []
    characters = [c for c in characters if isinstance(c, dict) and str(c.get("name") or "").strip() and str(c.get("description") or "").strip()][:4]
    AI_REF_DIR.mkdir(parents=True, exist_ok=True)
    profiles = []
    for index, character in enumerate(characters):
        cname = str(character.get("name") or "Karakter").strip()[:80]
        desc = str(character.get("description") or "").strip()[:900]
        role = str(character.get("role") or "").strip()[:180]
        prompt = (
            "Create ONE canonical casting identity portrait for a recurring character in a photorealistic live-action film. "
            "This is a consistent identity reference, not a scene illustration. Character name: " + cname + ". "
            "Stable visual identity: " + desc + ". Role: " + role + ". "
            "Show one person only, head and upper body, front-facing three-quarter view, neutral expression, plain muted background, "
            "soft natural cinematic light, realistic skin texture, clearly visible hair and signature clothing, no text, no collage. " + AI_STYLE
        )
        # Karakter referansları da aynı proje seed ailesinden (tutarlı yüz/renk)
        seed = (project_base_seed(context, manual_guide) + 1000 + index * 31) % 90000
        raw_dest = AI_DIR / ("character_" + uuid.uuid4().hex[:12] + ".jpg")
        ref_name = "ref_" + uuid.uuid4().hex[:14] + ".jpg"
        ref_dest = AI_REF_DIR / ref_name
        try:
            cf_generate_image(prompt, seed, raw_dest)
            prepare_reference_image(raw_dest.read_bytes(), ref_dest)
        finally:
            raw_dest.unlink(missing_ok=True)
        profiles.append({"name": cname, "role": role, "description": desc,
                         "imageName": ref_name, "url": "/airef/" + ref_name})

    if not bible:
        bible = style_card_for_script(context)
    return {"characters": profiles, "visual_bible": bible,
            "message": (f"{len(profiles)} karakter bulundu ve referansları oluşturuldu." if profiles
                        else "Tekrarlayan ana karakter bulunamadı; film için görsel stil rehberi çıkarıldı.")}

def do_aigen(body: dict) -> dict:
    """Sahne görseli üretir; proje referansları varsa FLUX.2 Klein çoklu görsel girdisi kullanır.
    Tüm sahnelerde AYNI proje seed'i + zorunlu stil öneki kullanılır (tutarlı görsel dil)."""
    if not cf_ready():
        raise RuntimeError("AI görsel için ⚙ API anahtarları'na ücretsiz Cloudflare bilgilerini ekle.")
    text = str(body.get("text") or "").strip()
    context = str(body.get("context") or "")
    portrait = str(body.get("orientation") or "portrait") != "landscape"
    W = H = 1024  # FLUX.1 Schnell yedek akışının gerçek kare ölçüsü
    subject = ai_prompt_from_scene(text, context or text)
    style = style_card_for_script(context or text)
    user_guide = str(body.get("continuity_guide") or "").strip()[:2500]
    guide = user_guide or style
    refs = []
    ref_labels = []
    used_ref_names = set()
    character_profiles = [x for x in (body.get("character_profiles") or []) if isinstance(x, dict)][:4]
    for profile in character_profiles:
        name = Path(str(profile.get("imageName") or "")).name
        if not re.fullmatch(r"ref_[a-f0-9]{14}\.jpg", name):
            continue
        path = AI_REF_DIR / name
        if path.is_file() and path.stat().st_size <= 3 * 1024 * 1024:
            refs.append(path.read_bytes())
            used_ref_names.add(name)
            ref_labels.append(str(profile.get("name") or "Unnamed character") + ": " + str(profile.get("description") or "")[:450])
    for item in (body.get("reference_images") or []):
        if len(refs) >= 4:
            break
        name = Path(str(item)).name
        if name in used_ref_names or not re.fullmatch(r"ref_[a-f0-9]{14}\.jpg", name):
            continue
        path = AI_REF_DIR / name
        if path.is_file() and path.stat().st_size <= 3 * 1024 * 1024:
            refs.append(path.read_bytes())
            used_ref_names.add(name)
            ref_labels.append("Additional user-provided visual reference")
    if refs:
        W, H = (832, 1248) if portrait else (1248, 832)
    # SAHNE ÖNCE: modeller prompt'un başını daha güçlü uygular → her sahne farklı konu
    # Stil/karakter sonra gelir (tutarlı yüz/renk, ama kompozisyon sahneye özel kalır)
    scene_focus = (subject or text)[:900].strip()
    short_guide = (guide or "")[:500].strip()
    parts = []
    parts.append("PRIMARY SUBJECT FOR THIS FRAME ONLY: " + scene_focus)
    parts.append("Different shot than other scenes: change camera angle, framing and action to match THIS line.")
    if short_guide:
        parts.append("Shared film look (color/light only, do not copy the same pose): " + short_guide)
    if character_profiles:
        parts.append("If this line involves a recurring person, keep their face/outfit consistent: "
                     + " | ".join(ref_labels)[:700]
                     + ". Only show them when the line needs them.")
    elif refs:
        parts.append("Keep referenced identity consistent when that person appears; otherwise focus on the scene subject.")
    parts.append(AI_STYLE)
    prompt = ". ".join(p.strip().rstrip(".") for p in parts if p.strip())
    # Her sahne FARKLI seed → farklı dosya, farklı kompozisyon (stil prompt ile korunur)
    again = int(body.get("again") or 0)
    base = project_base_seed(context or text, user_guide)
    scene_hash = zlib.crc32((text or subject or str(time.time())).encode("utf-8")) % 50000
    seed = (base + scene_hash + again * 17) % 90000
    name = f"ai{seed}_{W}_{H}.jpg"
    AI_DIR.mkdir(parents=True, exist_ok=True)
    dest = AI_DIR / name
    used_fallback = False
    if not (dest.is_file() and dest.stat().st_size > 5000):
        if refs:
            try:
                cf_generate_image_with_refs(prompt, seed, dest, refs, W, H)
            except RuntimeError as e:
                err = str(e)
                if not any(code in err for code in ("HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504")):
                    raise
                cf_generate_image(prompt, seed, dest)
                used_fallback = True
        else:
            cf_generate_image(prompt, seed, dest)
    match_note = ("FLUX.1 yedek · sahne seed " + str(seed)
                  if used_fallback else
                  ("FLUX.2 + ref · sahne seed " + str(seed) if refs
                   else "sahne odaklı · seed " + str(seed)))
    clip = {"source": "AI", "id": f"ai{seed}", "kind": "photo", "orient": "portrait" if portrait else "landscape",
            "page": f"/aiimg/{name}", "thumb": f"/aiimg/{name}", "duration": 0,
            "files": [{"url": "local:" + name, "w": W, "h": H}],
            "tags": subject[:120], "score": 100, "matched": [match_note], "q": "AI görsel"}
    return {"clips": [clip], "prompt": prompt}


# --------------------------------------------------------------------------
# AI VİDEO PANELİ: sahne cümlesinden video promptu yazar + ücretsiz sitelerin günlük limit takibi
# --------------------------------------------------------------------------
LIMITS_FILE = ROOT / "planner_limits.json"


def do_vprompt(body: dict) -> dict:
    """Türkçe sahne metnini ücretsiz çeviri (MyMemory/Google gtx) ile İngilizceye çevirir.
    LLM kullanmaz — anahtar gerekmez. Çeviri gelmezse Türkçe bırakır."""
    text = str(body.get("text") or "").strip()
    if not text:
        raise RuntimeError("Sahne metni boş.")
    shape = "vertical 9:16" if str(body.get("orientation") or "portrait") != "landscape" else "horizontal 16:9"
    en = ""
    try:
        en = translate_tr_en(text)
    except Exception:
        en = ""
    if not en or en.lower().strip() == text.lower().strip():
        return {"prompt": text + f". Cinematic, slow camera, realistic documentary style, {shape}, no text, no watermark."}
    prompt = (en.strip().rstrip(".") +
              f". Slow cinematic camera movement, realistic documentary style, natural lighting, "
              f"smooth motion, {shape}, no text, no watermark.")
    return {"prompt": prompt}


def do_limits_get() -> dict:
    try:
        d = json.loads(LIMITS_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def do_limits_set(body: dict) -> dict:
    clean = {"date": str(body.get("date") or "")[:10], "sites": {}}
    for k, v in (body.get("sites") or {}).items():
        if not isinstance(v, dict):
            continue
        try:
            used = max(0, int(v.get("used") or 0))
        except (TypeError, ValueError):
            used = 0
        clean["sites"][str(k)[:30]] = {"used": used, "done": bool(v.get("done"))}
    LIMITS_FILE.write_text(json.dumps(clean, ensure_ascii=False), encoding="utf-8")
    return {"ok": True}


def pick_photo(r: dict, f: dict, name: str, body: dict) -> dict:
    """Fotoğrafı indirir, ffmpeg ile yavaş yakınlaşma/uzaklaşma yapan kısa videoya çevirir."""
    ff = shutil.which("ffmpeg")
    if not ff:
        raise RuntimeError("ffmpeg bulunamadı (fotoğrafı videoya çevirmek için gerekli).")
    portrait = r.get("orient", "portrait") != "landscape"
    W, H = (1080, 1920) if portrait else (1920, 1080)
    words = max(1, int(body.get("words") or 6))
    dur = max(4, min(12, round(words / 2.6 + 2.5)))          # sahneden biraz uzun: kurgu kırpabilir
    fps = 30
    n = dur * fps
    idx = int(body.get("index", 1))
    tmp_img = VIDEO_DIR / f".tmp_photo_{idx}.jpg"
    out = VIDEO_DIR / name
    tmp_out = VIDEO_DIR / f".tmp_photo_{idx}.mp4"
    try:
        if str(f["url"]).startswith("local:"):
            nm = Path(str(f["url"])[6:]).name
            src = AI_DIR / nm
            if not re.fullmatch(r"ai\d+_\d+(?:_\d+)?\.jpg", nm) or not src.is_file():
                raise RuntimeError("AI görseli bulunamadı; 'AI ile üret'e tekrar bas.")
            shutil.copyfile(src, tmp_img)
        else:
            download(f["url"], tmp_img)
        # Gereksiz 2x ara çözünürlükten kaçın: Vega 11 gibi dahili GPU'lu
        # sistemlerde FFmpeg CPU yükünü ve işlem süresini azaltır.
        # Daha akıcı Ken Burns hareketi:
        # Görseli hedefin 2 katına ölçekle, çift piksel koordinatları kullan,
        # çok hafif ve doğrusal yakınlaş; keskinleştirme kaynaklı titreşimi kaldır.
        SW, SH = W * 2, H * 2
        zoom_in = idx % 2 == 1                                    # sahneler arası yön değişsin
        ease = f"(0.5-0.5*cos(PI*on/{max(1, n - 1)}))"            # yavaş başla, yavaş bitir (mekanik görünmez)
        AMPL = 0.045                                              # hafif zoom: 2x ölçekte titreşim/artefakt azalır
        z = f"1+{AMPL}*{ease}" if zoom_in else f"1+{AMPL}-{AMPL}*{ease}"
        vf = (f"scale={SW}:{SH}:force_original_aspect_ratio=increase,crop={SW}:{SH},"
              f"zoompan=z='{z}':"
              f"x='2*trunc((iw-iw/zoom)/4)':y='2*trunc((ih-ih/zoom)/4)':"
              f"d={n}:s={W}x{H}:fps={fps},"
              f"setsar=1,format=yuv420p")
        res = subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(tmp_img), "-vf", vf, "-t", str(dur),
                              "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-movflags", "+faststart",
                              "-an", str(tmp_out)], capture_output=True, text=True, timeout=600)
        if res.returncode != 0 or not tmp_out.is_file():
            raise RuntimeError("Fotoğraf videoya çevrilemedi: " + (res.stderr or "")[-300:])
        tmp_out.replace(out)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Fotoğraf videoya çevirme zaman aşımına uğradı, tekrar dene.")
    finally:
        for t in (tmp_img, tmp_out):
            try:
                t.unlink(missing_ok=True)
            except OSError:
                pass
    return {"file": name, "size_mb": round(out.stat().st_size / 1e6, 1), "resolution": f"{W}x{H} (fotoğraftan, {dur} sn)"}



def _svg_escape(value: str) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;"))


def _diagram_phrases(text: str) -> list:
    """Sahnede gerçekten geçen ifadelerden 3-4 kısa kart çıkarır; yeni bilgi eklemez."""
    raw = re.split(r"(?<=[.!?;])\s+|\s+(?:çünkü|bu yüzden|ancak|fakat|sonra|önce|böylece|yani)\s+", text.strip(), flags=re.I)
    parts = []
    for item in raw:
        item = re.sub(r"\s+", " ", item).strip(" ,.;:!?")
        if not item:
            continue
        words = item.split()
        if len(words) > 9:
            # Uzun cümleyi kendi kelimelerinden iki okunabilir parçaya ayır.
            mid = len(words) // 2
            parts.extend([" ".join(words[:mid]), " ".join(words[mid:])])
        else:
            parts.append(item)
    if len(parts) < 3:
        words = text.split()
        if len(words) >= 6:
            third = max(2, len(words) // 3)
            parts = [" ".join(words[:third]), " ".join(words[third:2*third]), " ".join(words[2*third:])]
        else:
            parts = [text.strip(), "Önemli nokta", "Sonuç"]
    # Tekrarları ve boş parçaları temizle; maksimum 4 kart.
    out, seen = [], set()
    for part in parts:
        key = tr_fold(part).lower()
        if key and key not in seen:
            seen.add(key)
            out.append(part)
    return (out[:4] if len(out) >= 3 else (out + ["Bir sonraki adım", "Sonuç"])[:3])


def _wrap_svg(text: str, limit: int = 25, max_lines: int = 3) -> list:
    words = text.split()
    lines, cur = [], ""
    for word in words:
        candidate = (cur + " " + word).strip()
        if len(candidate) > limit and cur:
            lines.append(cur)
            cur = word
        else:
            cur = candidate
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:max(1, limit - 1)].rstrip() + "…"
    return lines


def _ffmpeg_filter_path(path: Path) -> str:
    """FFmpeg filtrelerinde kullanılacak dosya yolunu güvenli biçimde kaçır."""
    value = str(path.resolve()).replace("\\", "/")
    value = value.replace(":", r"\:").replace("'", r"\'")
    return value


def create_diagram_video(body: dict) -> dict:
    """FFmpeg drawbox/drawtext ile SVG çözücüsü gerektirmeden hareketli şema üretir."""
    ff = shutil.which("ffmpeg")
    if not ff:
        raise RuntimeError("Şema oluşturmak için FFmpeg gerekli.")
    text = re.sub(r"\s+", " ", str(body.get("text") or "")).strip()
    if not text:
        raise RuntimeError("Şema metni boş.")
    idx = max(1, min(999, int(body.get("index") or 1)))
    slug = re.sub(r"[^a-z0-9]+", "_", str(body.get("slug") or "diagram").lower()).strip("_") or "diagram"
    portrait = str(body.get("orientation") or "portrait") != "landscape"
    W, H = (1080, 1920) if portrait else (1920, 1080)
    duration = max(4, min(12, round(max(1, int(body.get("words") or len(text.split()))) / 2.6 + 2.5)))
    phrases = _diagram_phrases(text)
    title = phrases[0]
    cards = phrases[1:5] or phrases[:1]
    margin = 76 if portrait else 120
    card_w = W - margin * 2
    card_h = 245 if portrait else 170
    gap = 46 if portrait else 28
    total_h = len(cards) * card_h + (len(cards) - 1) * gap
    top = (H - total_h) // 2 + (100 if portrait else 30)
    font_candidates = [
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf",
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "segoeui.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    font = next((f for f in font_candidates if f.is_file()), None)
    if font is None:
        raise RuntimeError("Şema yazı tipi bulunamadı. Windows Arial yazı tipinin yüklü olduğundan emin ol.")

    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{idx:02d}_{slug}_diagram.mp4"
    out = VIDEO_DIR / name
    tmp_out = VIDEO_DIR / f".diagram_{idx}.tmp.mp4"
    temp_texts = []

    def add_text_file(value: str) -> str:
        path = VIDEO_DIR / f".diagram_{idx}_{len(temp_texts)}.txt"
        path.write_text(value, encoding="utf-8-sig")
        temp_texts.append(path)
        return _ffmpeg_filter_path(path)

    try:
        filters = []
        # Başlık ve üst etiket
        filters.append("drawtext=fontfile='" + _ffmpeg_filter_path(font) + "':text='KONU SEMASI':fontcolor=0x7dd3fc:fontsize=" + str(44 if portrait else 38) + ":x=" + str(margin) + ":y=" + str(int(H * .12)) + ":expansion=none")
        title_lines = _wrap_svg(title, 28 if portrait else 42, 2)
        title_file = add_text_file("\n".join(title_lines))
        filters.append("drawtext=fontfile='" + _ffmpeg_filter_path(font) + "':textfile='" + title_file + "':fontcolor=white:fontsize=" + str(62 if portrait else 52) + ":line_spacing=12:x=" + str(margin) + ":y=" + str(int(H * .17)) + ":expansion=none:shadowcolor=black@0.3:shadowx=2:shadowy=2")

        for n, phrase in enumerate(cards):
            y = top + n * (card_h + gap)
            # Kart gövdesi ve sol vurgu çizgisi
            filters.append(f"drawbox=x={margin}:y={y}:w={card_w}:h={card_h}:color=0x243b5b:t=fill")
            filters.append(f"drawbox=x={margin}:y={y}:w=12:h={card_h}:color=0x22a6a1:t=fill")
            # Numara ve metin
            filters.append("drawtext=fontfile='" + _ffmpeg_filter_path(font) + "':text='" + str(n + 1) + "':fontcolor=0x071b2a:fontsize=42:x=" + str(margin + 48) + ":y=" + str(y + card_h // 2 - 25) + ":box=1:boxcolor=0x22a6a1:boxborderw=18:expansion=none")
            lines = _wrap_svg(phrase, 28 if portrait else 52, 3)
            phrase_file = add_text_file("\n".join(lines))
            text_y = y + card_h // 2 - (len(lines) - 1) * (34 if portrait else 27) - (24 if portrait else 18)
            filters.append("drawtext=fontfile='" + _ffmpeg_filter_path(font) + "':textfile='" + phrase_file + "':fontcolor=0xf4f8ff:fontsize=" + str(43 if portrait else 36) + ":line_spacing=10:x=" + str(margin + 145) + ":y=" + str(text_y) + ":expansion=none")
            if n < len(cards) - 1:
                filters.append(f"drawbox=x={W//2-3}:y={y+card_h+8}:w=6:h={max(12,gap-16)}:color=0x67d6ce:t=fill")

        filters.append("drawtext=fontfile='" + _ffmpeg_filter_path(font) + "':text='KISA BILGI - GORSEL OZET':fontcolor=0x9fb3c8:fontsize=27:x=w-tw-" + str(margin) + ":y=h-90:expansion=none")
        filters.append(f"zoompan=z='min(1.035,1+0.000025*on)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps=30")
        filters.append("setsar=1,format=yuv420p")
        vf = ",".join(filters)
        res = subprocess.run(
            [ff, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
             f"color=c=0x10182b:s={W}x{H}:r=30:d={duration}", "-vf", vf, "-t", str(duration),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", "-an", str(tmp_out)],
            capture_output=True, text=True, timeout=180
        )
        if res.returncode != 0 or not tmp_out.is_file() or tmp_out.stat().st_size < 5000:
            raise RuntimeError("FFmpeg şemayı videoya çeviremedi: " + (res.stderr or "")[-700:])
        tmp_out.replace(out)
    finally:
        for path in temp_texts:
            path.unlink(missing_ok=True)
        tmp_out.unlink(missing_ok=True)

    old = str(body.get("old_file") or "")
    if old and old != name:
        old_path = (VIDEO_DIR / Path(old).name).resolve()
        if VIDEO_DIR.resolve() in old_path.parents and old_path.is_file():
            old_path.unlink()
    return {"file": name, "size_mb": round(out.stat().st_size / 1e6, 1),
            "resolution": f"{W}x{H} (hareketli şema, {duration} sn)"}


def do_diagram(body: dict) -> dict:
    return create_diagram_video(body)


def do_pick(body: dict) -> dict:
    r = body.get("result") or {}
    files = r.get("files") or []
    if not files:
        raise RuntimeError("Bu sonuçta indirilebilir dosya yok.")
    idx = int(body.get("index", 1))
    slug = re.sub(r"[^a-z0-9]+", "_", str(body.get("slug", "")).lower()).strip("_") or "clip"
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{idx:02d}_{slug}.mp4"
    old = str(body.get("old_file") or "")
    # Yeni klip başarıyla oluşmadan mevcut sahne videosunu silme.
    if r.get("kind") == "photo":
        result = pick_photo(r, files[0], name, body)
    else:
        f = choose_file(files)
        download(f["url"], VIDEO_DIR / name)
        result = {"file": name, "size_mb": round((VIDEO_DIR / name).stat().st_size / 1e6, 1),
                  "resolution": f"{f['w']}x{f['h']}"}
    if old and old != name:
        op = (VIDEO_DIR / old).resolve()
        if VIDEO_DIR.resolve() in op.parents and op.is_file():
            op.unlink()
    return result


# --------------------------------------------------------------------------
# İndirilenler klasörünü izle: link tıklanıp indirilen video otomatik sahneye gelsin
# --------------------------------------------------------------------------
TEMP_EXTS = (".crdownload", ".part", ".tmp", ".download", ".opdownload")
WATCH = {"armed": False, "index": 0, "slug": "", "old_file": "", "base": {}, "result": None, "since": 0.0}
WATCH_LOCK = threading.Lock()


def downloads_dir() -> Path:
    env = os.environ.get("PLANNER_DOWNLOADS")
    if env:
        return Path(env)
    if sys.platform.startswith("win"):
        try:                                   # taşınmış / OneDrive'a yönlendirilmiş İndirilenler klasörünü de bulur
            import ctypes

            class GUID(ctypes.Structure):
                _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort),
                            ("d", ctypes.c_ubyte * 8)]
            g = GUID(0x374DE290, 0x123F, 0x4565, (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
            p = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(p)) == 0:
                path = p.value
                ctypes.windll.ole32.CoTaskMemFree(p)
                if path:
                    return Path(path)
        except Exception:
            pass
    return Path.home() / "Downloads"


def _scan_downloads() -> dict:
    out = {}
    try:
        for p in downloads_dir().iterdir():
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                st = p.stat()
                out[p.name] = (st.st_size, st.st_mtime)
    except OSError:
        pass
    return out


def do_arm(body: dict) -> dict:
    """Bu sahne için indirme beklemeye başla: indirilenlere düşen ilk yeni video sahneye taşınır."""
    idx = int(body.get("index", 1))
    slug = re.sub(r"[^a-z0-9]+", "_", str(body.get("slug", "")).lower()).strip("_") or "clip"
    with WATCH_LOCK:
        WATCH.update(armed=True, index=idx, slug=slug, old_file=str(body.get("old_file") or ""),
                     base=_scan_downloads(), result=None, since=time.time())
    return {"armed": True, "folder": str(downloads_dir())}


def do_disarm() -> dict:
    with WATCH_LOCK:
        WATCH.update(armed=False, result=None)
    return {"armed": False}


def do_watch() -> dict:
    with WATCH_LOCK:
        res = WATCH["result"]
        if res:
            WATCH["result"] = None
        return {"armed": WATCH["armed"], "index": WATCH["index"], "result": res, "folder": str(downloads_dir())}


def _adopt_download(src: Path) -> None:
    idx, slug, old = WATCH["index"], WATCH["slug"], WATCH["old_file"]
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{idx:02d}_{slug}{src.suffix.lower()}"
    if old and old != name:
        op = (VIDEO_DIR / old).resolve()
        if VIDEO_DIR.resolve() in op.parents and op.is_file():
            op.unlink()
    dest = VIDEO_DIR / name
    if dest.exists():
        dest.unlink()
    shutil.move(str(src), str(dest))
    WATCH.update(armed=False, result={"index": idx, "file": name, "original": src.name,
                                      "size_mb": round(dest.stat().st_size / 1e6, 1)})


def watch_loop() -> None:
    pending = {}
    while True:
        time.sleep(0.8)
        try:
            with WATCH_LOCK:
                if not WATCH["armed"]:
                    pending.clear()
                    continue
                base = dict(WATCH["base"])
            cur = _scan_downloads()
            for name, (size, mt) in cur.items():
                b = base.get(name)
                if (b and mt <= b[1] + 1) or size == 0:
                    continue
                if pending.get(name) != size:          # boyut hâlâ değişiyorsa indirme sürüyor
                    pending[name] = size
                    continue
                with WATCH_LOCK:
                    if WATCH["armed"]:
                        try:
                            _adopt_download(downloads_dir() / name)
                        except OSError:
                            continue                    # tarayıcı dosyayı hâlâ tutuyor, sonraki turda dene
                        pending.clear()
                        break
        except Exception:
            pass


# --------------------------------------------------------------------------
# Kapak (thumbnail): yükle + videonun başına ekle
# --------------------------------------------------------------------------
def finish_image(src: Path) -> None:
    """Yüklenen görseli (png/jpg/webp) inputs/thumbnail.png olarak kaydeder."""
    if src.suffix.lower() == ".png":
        shutil.copy2(src, THUMB_FILE)
        return
    ff = shutil.which("ffmpeg")
    if not ff:
        raise RuntimeError("ffmpeg bulunamadı.")
    tmp = INPUT_DIR / "_thumb_out.png"
    r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src.resolve()), "-frames:v", "1", tmp.name],
                       cwd=str(INPUT_DIR), capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=0x08000000 if sys.platform.startswith("win") else 0)
    if r.returncode != 0 or not tmp.is_file():
        raise RuntimeError("Görsel işlenemedi: " + (r.stderr or "")[-300:])
    tmp.replace(THUMB_FILE)


def do_thumb_upload(q: dict, rfile, n: int) -> dict:
    ext = Path(q.get("name", [""])[0]).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        raise RuntimeError("Kapak png, jpg veya webp olmalı.")
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INPUT_DIR / ("_thumb_up" + ext)
    left = n
    with open(tmp, "wb") as f:
        while left > 0:
            chunk = rfile.read(min(1 << 20, left))
            if not chunk:
                break
            f.write(chunk)
            left -= len(chunk)
    try:
        finish_image(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    return {"ok": True}


def do_thumb_clear() -> dict:
    THUMB_FILE.unlink(missing_ok=True)
    return {"ok": True}


def thumb_state() -> dict:
    return {"final": THUMB_FILE.stat().st_mtime if THUMB_FILE.is_file() else 0}


def add_thumb_intro(frames: int) -> None:
    """Onaylı kapağı videonun başına N kare olarak ekler (30 kare = 1 sn). Hep kapaksız ham videodan eklenir."""
    if not THUMB_FILE.is_file():
        raise RuntimeError("Kapak yok (inputs/thumbnail.png).")
    ff = shutil.which("ffmpeg")
    if not ff:
        raise RuntimeError("ffmpeg bulunamadı.")
    if not RAW_VIDEO.is_file():
        if not OUTPUT_VIDEO.is_file():
            raise RuntimeError("Önce videoyu üret.")
        shutil.copy2(OUTPUT_VIDEO, RAW_VIDEO)
    frames = max(1, min(150, int(frames)))
    dur = frames / 30
    tmp = ROOT / "output_video.kapak.tmp.mp4"
    # Ses ikinci kez kodlanmasın: yalnızca görüntü birleştirilir, ses ham videodan AYNEN kopyalanır
    # (-itsoffset ile kapak süresi kadar geciktirilir). Eskiden ses burada yeniden kodlanıyor ve
    # mono sesin seviyesi ~3 dB düşüyor, konuşma aralarındaki taban gürültüsü sıfırlanıyordu.
    fc = ("[0:v]split=2[a][b];"
          "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=30:5[bg];"
          "[b]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
          f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30,format=yuv420p,trim=end_frame={frames},setpts=PTS-STARTPTS[iv];"
          "[1:v]setsar=1,fps=30,format=yuv420p[mv];"
          "[iv][mv]concat=n=2:v=1:a=0[v]")
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y",
           "-loop", "1", "-framerate", "30", "-t", f"{dur + 0.5:.3f}", "-i", str(THUMB_FILE),
           "-i", str(RAW_VIDEO),
           "-itsoffset", f"{dur:.4f}", "-i", str(RAW_VIDEO),
           "-filter_complex", fc, "-map", "[v]", "-map", "2:a:0",
           "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
           "-c:a", "copy", "-movflags", "+faststart", str(tmp)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=0x08000000 if sys.platform.startswith("win") else 0)
    if r.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg kapağı eklerken hata verdi: " + (r.stderr or "")[-400:])
    tmp.replace(OUTPUT_VIDEO)


def do_prepend(body: dict) -> dict:
    if RUN["proc"] and RUN["proc"].poll() is None:
        raise RuntimeError("Üretim sürerken kapak eklenemez.")
    add_thumb_intro(int(body.get("frames") or 1))
    try:
        export_copy()
    except Exception:
        pass
    return {"ok": True}


# --------------------------------------------------------------------------
# Kontrol + dosya yazma
# --------------------------------------------------------------------------
def hook_warnings(scenes: list) -> list:
    """Kanca planındaki 'ilk 3 saniyede olmamalı' kuralları ve kesin iddia uyarıları (video seçilmese de çalışır)."""
    warns = []
    if not scenes:
        return warns
    first_toks = " ".join(fold_tokens(scenes[0].get("text", "")))
    if any(first_toks == g or first_toks.startswith(g + " ") for g in HOOK_GREETINGS):
        warns.append("Kanca: video selam / tanıtımla ('Merhaba', 'Bugün sizlere'…) açılıyor. İlk 3 saniyede genel giriş olmamalı, "
                     "doğrudan kancayla başla.")
    n = first_sentence_scenes(scenes)
    nw = sum(len(s.get("text", "").split()) for s in scenes[:n])
    if nw > HOOK_MAX_WORDS:
        warns.append(f"Kanca: ilk cümle {nw} kelime; ~2,6 kelime/sn ile yaklaşık {nw / 2.6:.1f} sn sürer. "
                     f"İlk 3 saniyeye sığması için en fazla {HOOK_MAX_WORDS} kelime olmalı.")
    alltok = [t for s in scenes for t in fold_tokens(s.get("text", ""))]
    found = sorted({t for t in alltok if t in CLAIM_WORDS or t.startswith("kanitlan")})
    if found:
        warns.append("Kesin iddia: metinde " + ", ".join(f"'{t}'" for t in found) + " geçiyor. Bilimsel/tıbbi konularda "
                     "'olabilir', 'ilişkili olabilir' gibi ihtiyatlı ifade daha güvenli.")
    return warns


def check_scenes(scenes: list) -> list:
    warns = hook_warnings(scenes)
    toks = [fold_tokens(s.get("text", "")) for s in scenes]
    for i, s in enumerate(scenes):
        n = i + 1
        if not s.get("file"):
            warns.append(f"Sahne {n}: video seçilmedi, eslesme.txt'ye yazılmaz.")
            continue
        trig_list = [t for t in s.get("triggers", []) if norm_tag(t)]
        if not trig_list:
            warns.append(f"Sahne {n}: tetikleyici kelime yok, bu video hiç gelmez.")
        for t in trig_list:
            parts = norm_tag(t).split()
            if not tag_positions(toks[i], parts):
                warns.append(f"Sahne {n}: '{t}' bu sahnenin metninde geçmiyor, tetiklenmez.")
            for j in range(len(scenes)):
                if j != i and tag_positions(toks[j], parts):
                    warns.append(f"Sahne {n}: '{t}' Sahne {j + 1} metninde de geçiyor; "
                                 "orada yanlış videoyu tetikleyebilir.")
    return warns


def backup_and_write(path: Path, content: str) -> None:
    if path.exists():
        try:
            if path.read_text(encoding="utf-8-sig") != content:
                shutil.copy(path, path.with_name(path.name + ".bak"))
        except Exception:
            pass
    path.write_text(content, encoding="utf-8")


def do_write(scenes: list) -> dict:
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["# Kesin Eşleşme Dosyası (Son Sürüm)"]
    used = 0
    for s in scenes:
        if not s.get("file"):
            continue
        trig = [re.sub(r"\s+", " ", re.sub(r"[#:,]", " ", t)).strip() for t in s.get("triggers", [])]
        trig = [t for t in trig if t]
        if not trig:
            continue
        lines.append(f"{s['file']} : {', '.join(trig)}")
        used += 1
    backup_and_write(INPUT_DIR / "eslesme.txt", "\n".join(lines) + "\n")
    text = " ".join(s.get("text", "").strip() for s in scenes if s.get("text", "").strip())
    backup_and_write(INPUT_DIR / "metin.txt", text + "\n")
    return {"warnings": check_scenes(scenes),
            "message": f"{used} satır inputs/eslesme.txt dosyasına, metin inputs/metin.txt dosyasına yazıldı. "
                       "Şimdi çalıştır: python pipeline.py --match kelime --min-scene 1.0"}


# --------------------------------------------------------------------------
# Ayrı İngilizce video dönüştürme akışı (mevcut Türkçe üretim akışına dokunmaz)
# --------------------------------------------------------------------------
EN_STATE = {"status": "idle", "lines": [], "error": "", "video": "", "audio": ""}
EN_LOCK = threading.Lock()


def en_log(message: str) -> None:
    with EN_LOCK:
        EN_STATE["lines"].append(str(message))


def en_upload(kind: str, q: dict, rfile, n: int) -> dict:
    EN_DIR.mkdir(parents=True, exist_ok=True)
    original = q.get("name", [""])[0]
    ext = Path(original).suffix.lower()
    if kind == "video":
        if ext not in EN_VIDEO_EXTS:
            raise RuntimeError("Video dosyası MP4, MOV, WEBM veya MKV olmalı.")
        if n <= 0 or n > 2_000_000_000:
            raise RuntimeError("Video dosyası boş veya 2 GB sınırını aşıyor.")
        dest = EN_DIR / ("source_video" + ext)
    else:
        if ext not in EN_AUDIO_EXTS:
            raise RuntimeError("İngilizce ses MP3, WAV, M4A, AAC, OGG, FLAC veya OPUS olmalı.")
        if n <= 0 or n > 500_000_000:
            raise RuntimeError("Ses dosyası boş veya 500 MB sınırını aşıyor.")
        dest = EN_DIR / ("english_voice" + ext)
    tmp = dest.with_suffix(dest.suffix + ".part")
    left = n
    with open(tmp, "wb") as f:
        while left > 0:
            chunk = rfile.read(min(1 << 20, left))
            if not chunk:
                break
            f.write(chunk)
            left -= len(chunk)
    if left:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Dosya eksik yüklendi. Lütfen yeniden yükle.")
    for old in EN_DIR.glob(("source_video.*" if kind == "video" else "english_voice.*")):
        # .part geçici dosyasını silme (yoksa tmp.replace hata verir ve yükleme başarısız olur)
        if old != dest and old != tmp and old.is_file() and old.suffix.lower() != ".part":
            old.unlink(missing_ok=True)
    tmp.replace(dest)
    with EN_LOCK:
        EN_STATE["video" if kind == "video" else "audio"] = dest.name
    return {"file": dest.name, "size_mb": round(dest.stat().st_size / 1e6, 1)}


def en_probe_duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError("Dosyanın süresi okunamadı: " + (r.stderr or "ffprobe hatası")[-400:])
    try:
        return float(r.stdout.strip())
    except ValueError:
        raise RuntimeError("Medya süresi geçersiz.")


def en_probe_dimensions(path: Path) -> tuple:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(path)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError("Video boyutları okunamadı.")
    try:
        w, h = (int(x) for x in r.stdout.strip().split("x", 1))
        return w, h
    except Exception:
        raise RuntimeError("Video çözünürlüğü geçersiz.")


def en_ass_time(seconds: float) -> str:
    cs = max(0, int(round(seconds * 100)))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    sec, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{sec:02d}.{cs:02d}"


def en_font_family(path: Path) -> str:
    """TTF/OTF dosyasından yazı tipi ailesinin adını okur (ASS stilinde bu ad gerekir)."""
    import struct
    try:
        data = path.read_bytes()
        num = struct.unpack(">H", data[4:6])[0]
        name_off = None
        for i in range(num):
            tag, _, off, _ = struct.unpack(">4sIII", data[12 + 16 * i: 28 + 16 * i])
            if tag == b"name":
                name_off = off
                break
        if name_off is None:
            return ""
        _, count, str_off = struct.unpack(">HHH", data[name_off:name_off + 6])
        found = {}
        for i in range(count):
            plat, enc, lang, nid, ln, off = struct.unpack(">HHHHHH", data[name_off + 6 + 12 * i: name_off + 18 + 12 * i])
            if nid not in (1, 16):
                continue
            raw = data[name_off + str_off + off: name_off + str_off + off + ln]
            txt = raw.decode("utf-16-be" if plat in (0, 3) else "latin-1", errors="ignore").strip()
            if txt and (nid not in found or plat == 3):
                found[nid] = txt
        return found.get(16) or found.get(1) or ""
    except Exception:
        return ""


def en_build_ass(words: list, width: int = 1080, height: int = 1920,
                 font: str = "Arial", bold: bool = True) -> str:
    # Gerçek kelime zamanları: 2-3 kelimelik kısa gruplar, BÜYÜK HARF, kalın konturlu yazı;
    # konuşulan kelime sarı renkte vurgulanır (Türkçe versiyondaki kelime kelime stile benzer).
    groups, current, chars = [], [], 0
    for w in words:
        token = re.sub(r"[{}\\]", "", str(w.get("text", ""))).strip().upper()
        if not token:
            continue
        ends_sentence = bool(current) and current[-1]["text"][-1:] in ".!?"
        if current and (len(current) >= 3 or chars + len(token) + 1 > 20 or ends_sentence or
                        float(w["start"]) - float(current[-1]["end"]) > 0.55):
            groups.append(current)
            current, chars = [], 0
        current.append({"text": token, "start": float(w["start"]), "end": float(w["end"])})
        chars += len(token) + 1
    if current:
        groups.append(current)
    if not groups:
        raise RuntimeError("İngilizce seste konuşma algılanamadı.")

    fsize = max(36, int(height * 0.045))
    outline = max(4, int(fsize * 0.10))
    shadow = max(2, int(fsize * 0.045))
    margin_v = int(height * 0.16)
    header = (
        f"[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\n"
        "WrapStyle: 0\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{fsize},&H00FFFFFF,&H0000FFFF,&H00000000,&H96000000,{-1 if bold else 0},0,0,0,100,100,1,0,1,{outline},{shadow},2,{int(width*0.06)},{int(width*0.06)},{margin_v},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = []
    for gi, group in enumerate(groups):
        g_end = group[-1]["end"] + 0.15
        if gi + 1 < len(groups):
            g_end = min(g_end, groups[gi + 1][0]["start"])      # gruplar üst üste binmesin
        for i, w in enumerate(group):
            start = max(0.0, w["start"] if i else max(0.0, w["start"] - 0.03))
            end = group[i + 1]["start"] if i + 1 < len(group) else g_end
            end = max(end, start + 0.10)
            parts = []
            for j, x in enumerate(group):
                if j == i:      # konuşulan kelime: sarı + hafif büyük
                    parts.append(r"{\c&H00D7FF&\fscx112\fscy112}" + x["text"] + r"{\c&HFFFFFF&\fscx100\fscy100}")
                else:
                    parts.append(x["text"])
            lines.append(f"Dialogue: 0,{en_ass_time(start)},{en_ass_time(end)},Default,,0,0,0,,{' '.join(parts)}")
    return header + "\n".join(lines) + "\n"


def en_filter_complex(width: int, height: int, remove_turkish: bool) -> str:
    """Eski altyazıyı kapatır (delogo yerine bulanık, koyulaştırılmış, kenarı yumuşak şerit) ve yeni altyazıyı basar."""
    ass = "ass=english_subtitles.ass:fontsdir=fonts"
    if not remove_turkish:
        return f"[0:v]tpad=stop_mode=clone:stop_duration=5,{ass}[v]"
    by = (int(height * 0.68) // 2) * 2
    bh = ((height - by) // 2) * 2
    fade = max(4, int(height * 0.04))
    # Eski altyazının hiçbir rengi sızmasın diye şerit tamamen siyah ve opaktır; yalnızca üst kenarı yumuşak geçişlidir.
    return (
        "[0:v]tpad=stop_mode=clone:stop_duration=5[a];"
        f"color=c=black:s={width}x{bh},format=rgba[bl];"
        f"nullsrc=s={width}x{bh},format=gray,geq=lum='min(255,255*Y/{fade})'[mk];"
        "[bl][mk]alphamerge[band];"
        f"[a][band]overlay=0:{by}:shortest=1,{ass}[v]"
    )


def en_process(remove_turkish: bool) -> None:
    try:
        EN_DIR.mkdir(parents=True, exist_ok=True)
        videos = sorted(EN_DIR.glob("source_video.*"))
        audios = sorted(EN_DIR.glob("english_voice.*"))
        if not videos or not audios:
            raise RuntimeError("Önce orijinal videoyu ve İngilizce ses dosyasını yükle.")
        video, audio = videos[0], audios[0]
        for tool in ("ffmpeg", "ffprobe"):
            if shutil.which(tool) is None:
                raise RuntimeError(f"{tool} bulunamadı. FFmpeg kurulumunu kontrol et.")
        en_log("İngilizce ses analiz ediliyor (Whisper / English)...")
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise RuntimeError("faster-whisper kurulu değil. Mevcut uygulamanın Python ortamına: pip install faster-whisper")
        try:
            model = WhisperModel("small", device="cpu", compute_type="int8")
            segments, _ = model.transcribe(str(audio), language="en", word_timestamps=True,
                                           vad_filter=True, beam_size=5,
                                           condition_on_previous_text=False)
            words = []
            for seg in segments:
                for word in (seg.words or []):
                    text = re.sub(r"[{}\\]", "", (word.word or "")).strip()
                    if text and re.search(r"\w", text):
                        words.append({"text": text, "start": float(word.start), "end": float(word.end)})
        except Exception as e:
            raise RuntimeError("İngilizce ses çözümlenemedi: " + str(e)[:350])
        if not words:
            raise RuntimeError("İngilizce seste konuşma bulunamadı.")
        total = en_probe_duration(audio)
        if total <= 0:
            raise RuntimeError("İngilizce sesin süresi okunamadı.")
        width, height = en_probe_dimensions(video)
        font_name, bold = "Arial", True
        fdir = EN_DIR / "fonts"
        fdir.mkdir(parents=True, exist_ok=True)
        for old_font in fdir.glob("*"):
            old_font.unlink(missing_ok=True)
        user_font = ROOT / "font.ttf"
        if user_font.is_file():                  # Türkçe videoda kullanılan font.ttf varsa aynısı kullanılır
            fam = en_font_family(user_font)
            if fam:
                shutil.copy2(user_font, fdir / "subtitle_font.ttf")
                font_name, bold = fam, False
        (EN_DIR / "english_subtitles.ass").write_text(en_build_ass(words, width, height, font_name, bold), encoding="utf-8")
        en_log(f"{len(words)} İngilizce kelime zamanlandı. Altyazı hazırlanıyor ({font_name})...")
        fc = en_filter_complex(width, height, remove_turkish)
        if remove_turkish:
            en_log("Alt bölümdeki eski altyazı alanı siyah şeritle kapatılıyor.")
        tmpout = EN_DIR / "english_output.part.mp4"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video), "-i", str(audio),
               "-filter_complex", fc, "-map", "[v]", "-map", "1:a:0", "-t", f"{total:.3f}",
               "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "256k", "-ar", "44100", "-movflags", "+faststart", str(tmpout)]
        en_log("Video ve yeni İngilizce ses birleştiriliyor...")
        r = subprocess.run(cmd, cwd=str(EN_DIR), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            tmpout.unlink(missing_ok=True)
            raise RuntimeError("FFmpeg video oluşturamadı: " + (r.stderr or "bilinmeyen hata")[-1200:])
        tmpout.replace(EN_OUTPUT)
        en_log(f"Tamamlandı: {EN_OUTPUT.name} ({total:.1f} saniye).")
        with EN_LOCK:
            EN_STATE["status"] = "done"
    except Exception as e:
        en_log("HATA: " + str(e))
        with EN_LOCK:
            EN_STATE["status"] = "error"
            EN_STATE["error"] = str(e)


def en_start(body: dict) -> dict:
    with EN_LOCK:
        if EN_STATE["status"] == "running":
            raise RuntimeError("İngilizce dönüştürme zaten çalışıyor.")
        EN_STATE.update({"status": "running", "lines": [], "error": ""})
    thread = threading.Thread(target=en_process, args=(bool(body.get("remove_turkish", True)),), daemon=True)
    thread.start()
    return {"ok": True}


# --------------------------------------------------------------------------
# Yayın öncesi kalite kapısı: seçilen sahnelerin görüntü/metin uyumu
# --------------------------------------------------------------------------
QUALITY_WEIGHTS = {"text_match": 35, "visual_match": 25, "sequence": 15,
                   "continuity": 10, "technical": 10, "coverage": 5}
QUALITY_STATE = {"fingerprint": "", "result": None}


def _resolve_quality_scene_files(scenes: list) -> list:
    """Eksik sahne.file alanını, sahne sıra numarası ve slug ile inputs/videos içinden bul."""
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    missing = []
    for i, sc in enumerate(scenes):
        raw = str(sc.get("file") or sc.get("filename") or sc.get("video_file") or "").strip()
        name = Path(raw).name if raw else ""
        candidate = (VIDEO_DIR / name).resolve() if name else None
        if candidate and VIDEO_DIR.resolve() in candidate.parents and candidate.is_file():
            sc["file"] = name
            continue

        slug = re.sub(r"[^a-z0-9]+", "_", str(sc.get("slug") or "clip").lower()).strip("_") or "clip"
        prefix = f"{i + 1:02d}_{slug}"
        matches = sorted(
            [x for x in VIDEO_DIR.glob(prefix + ".*") if x.is_file() and x.suffix.lower() in VIDEO_EXTS],
            key=lambda x: x.stat().st_mtime_ns, reverse=True
        )
        # Eski projelerde slug değişmiş olabilir; yalnızca sıra numarası eşleşen dosyaya
        # tek ve açık bir aday varsa izin ver.
        if not matches:
            indexed = sorted(
                [x for x in VIDEO_DIR.glob(f"{i + 1:02d}_*") if x.is_file() and x.suffix.lower() in VIDEO_EXTS],
                key=lambda x: x.stat().st_mtime_ns, reverse=True
            )
            if len(indexed) == 1:
                matches = indexed
        if matches:
            sc["file"] = matches[0].name
        else:
            missing.append(i + 1)
    if missing:
        raise RuntimeError(
            "Bazı sahnelerin seçili video dosyası proje klasöründe bulunamadı. "
            "Eksik sahneler: " + ", ".join(map(str, missing)) +
            ". Videoları ilgili sahneye yeniden seç veya yükle."
        )
    return scenes


def _quality_fingerprint(scenes: list, refs: list) -> str:
    material = []
    for sc in scenes:
        name = Path(str(sc.get("file", ""))).name
        path = (VIDEO_DIR / name).resolve()
        if VIDEO_DIR.resolve() not in path.parents or not path.is_file():
            raise RuntimeError(f"Kalite kontrolü için dosya bulunamadı: {name}")
        st = path.stat()
        material.append({"text": str(sc.get("text", "")), "file": name, "size": st.st_size, "mtime": st.st_mtime_ns})
    ref_meta = []
    for ref in refs[:3]:
        name = Path(str(ref)).name; rp = (AI_REF_DIR / name).resolve()
        if re.fullmatch(r"ref_[a-f0-9]{14}\.jpg", name) and AI_REF_DIR.resolve() in rp.parents and rp.is_file():
            st = rp.stat(); ref_meta.append((name, st.st_size, st.st_mtime_ns))
    raw = json.dumps({"scenes": material, "refs": ref_meta}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return f"{zlib.crc32(raw) & 0xffffffff:08x}"

def _sample_video_frames(path: Path) -> list:
    """Klip başından ve son yarısından örnek kareler alır; iki anı da kontrol eder."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("Görsel kalite analizi için FFmpeg bulunamadı.")
    dur = probe_duration(path)
    stamps = [max(0.05, dur * 0.22), max(0.1, dur * 0.72)] if dur > 1.0 else [0.1]
    out = []
    for stamp in stamps:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{stamp:.2f}",
               "-i", str(path), "-frames:v", "1", "-vf", "scale=512:-2", "-q:v", "5", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
        r = subprocess.run(cmd, capture_output=True, timeout=45)
        if r.returncode or not r.stdout:
            continue
        out.append(base64.b64encode(r.stdout).decode("ascii"))
    if not out:
        raise RuntimeError(f"Sahne karesi okunamadı: {path.name}")
    return out


def do_quality_check(body: dict) -> dict:
    scenes = body.get("scenes") or []
    if not scenes:
        raise RuntimeError("Önce sahneleri oluştur.")
    _resolve_quality_scene_files(scenes)
    keys = load_keys()
    if not keys.get("anthropic"):
        raise RuntimeError("Gerçek kareleri görerek kalite analizi için API anahtarlarından Anthropic anahtarını ekle. Bu kontrol yalnızca etiket puanına bakmaz; sahne karelerini de inceler.")
    frames = []
    for i, sc in enumerate(scenes):
        name = Path(str(sc.get("file", ""))).name
        path = (VIDEO_DIR / name).resolve()
        if VIDEO_DIR.resolve() not in path.parents or not path.is_file():
            raise RuntimeError(f"Sahne {i + 1} dosyası bulunamadı: {name}")
        try:
            w, h = en_probe_dimensions(path)
        except Exception:
            w, h = 0, 0
        frames.append({"n": i + 1, "text": str(sc.get("text", ""))[:1200],
                       "file": name, "duration": round(probe_duration(path), 2),
                       "resolution": f"{w}x{h}", "frames": _sample_video_frames(path)})
    # Referans karakter portrelerini de karşılaştırma için modele ver.
    refs = []
    for ref in (body.get("reference_images") or [])[:3]:
        name = Path(str(ref)).name
        rp = (AI_REF_DIR / name).resolve()
        if re.fullmatch(r"ref_[a-f0-9]{14}\.jpg", name) and AI_REF_DIR.resolve() in rp.parents and rp.is_file():
            refs.append(base64.b64encode(rp.read_bytes()).decode("ascii"))
    system = ("You are a strict pre-publication video quality auditor. Inspect every supplied frame against its exact Turkish narration line and the complete script. "
              "Score six dimensions from 0 to 100: text_match (does the visible subject/action illustrate the spoken line), visual_match (specificity and relevance), "
              "sequence (does the scene order form a coherent visual story), continuity (consistent recurring people, appearance, era and cinematic style; compare supplied reference portraits), "
              "technical (sharpness, composition, aspect suitability, obvious defects), coverage (all lines have a fitting visual). "
              "Do not reward generic stock footage for an abstract line unless it clearly supports the meaning. Be strict, do not inflate scores. "
              "Return ONLY JSON: {\"scores\":{\"text_match\":0,\"visual_match\":0,\"sequence\":0,\"continuity\":0,\"technical\":0,\"coverage\":0},"
              "\"scene_scores\":[{\"n\":1,\"score\":0,\"issue\":\"...\",\"fix\":\"...\"}],\"strengths\":[\"...\"],\"verdict\":\"...\"}. Each scene_scores item must correspond to an input scene.")
    content = [{"type": "text", "text": "FULL SCRIPT (Turkish):\n" + str(body.get("context") or " ".join(x["text"] for x in frames))[:9000] + "\n\nReview every scene in order."}]
    for j, b64 in enumerate(refs):
        content.extend([{"type": "text", "text": f"CHARACTER REFERENCE PORTRAIT {j+1}; use only for identity/appearance continuity."},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}}])
    for fr in frames:
        content.append({"type": "text", "text": f"SCENE {fr['n']} | narration: {fr['text']} | file: {fr['file']} | duration: {fr['duration']} sec | resolution: {fr['resolution']}"})
        for fi, frame_b64 in enumerate(fr["frames"], 1):
            content.extend([{"type": "text", "text": f"Scene {fr['n']} sample frame {fi}"},
                            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": frame_b64}}])
    resp = http_json("https://api.anthropic.com/v1/messages",
                     headers={"x-api-key": keys["anthropic"], "anthropic-version": "2023-06-01"},
                     data={"model": CLAUDE_MODEL, "max_tokens": 3500, "temperature": 0.1,
                           "system": system, "messages": [{"role": "user", "content": content}]}, timeout=240)
    out = "".join(x.get("text", "") for x in resp.get("content", []) if x.get("type") == "text")
    data = parse_json_obj(out)
    raw = data.get("scores") or {}
    scores = {k: max(0, min(100, float(raw.get(k, 0)))) for k in QUALITY_WEIGHTS}
    total = round(sum(scores[k] * w for k, w in QUALITY_WEIGHTS.items()) / 100)
    scene_scores = data.get("scene_scores") or []
    # Her sahnenin puanı da kapı kontrolüne dahil; en zayıf sahne 85 altındaysa onay yok.
    weak = [x for x in scene_scores if float(x.get("score", 0) or 0) < 85]
    passed = total >= 85 and not weak and len(scene_scores) >= len(scenes)
    result = {"score": total, "passed": passed, "threshold": 85, "scores": scores,
              "scene_scores": scene_scores, "scene_files": [str(x.get("file") or "") for x in scenes],
              "strengths": data.get("strengths") or [],
              "verdict": data.get("verdict") or "", "visual_verified": True,
              "message": "Kalite eşiği geçildi; üretime izin verildi." if passed else "%85 eşiği geçilmedi. Zayıf sahneleri düzeltip yeniden analiz et."}
    QUALITY_STATE["fingerprint"] = _quality_fingerprint(scenes, body.get("reference_images") or [])
    QUALITY_STATE["result"] = result
    return result


# --------------------------------------------------------------------------
# Sunucu
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, p: Path, ctype: str) -> None:
        size = p.stat().st_size
        start, end, rng = 0, size - 1, self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a) if a.isdigit() else 0
            end = min(int(b), size - 1) if b.isdigit() else size - 1
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with open(p, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except OSError:
            pass

    def _allowed(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return False
        origin = self.headers.get("Origin")
        if origin:
            return urlparse(origin).hostname in ("127.0.0.1", "localhost")
        return True

    def do_GET(self):
        if not self._allowed():
            return self._send(403, {"error": "Yasak"})
        path = urlparse(self.path).path
        try:
            if path == "/":
                return self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
            if path == "/api/info":
                k = load_keys()
                name, dur = audio_info()
                return self._send(200, {"keys": {a: bool(b) for a, b in k.items()},
                                        "audio_name": name, "audio_seconds": dur,
                                        "inputs": str(INPUT_DIR), "thumb": thumb_state(),
                                        "downloads": str(downloads_dir())})
            if path == "/api/en/status":
                with EN_LOCK:
                    return self._send(200, {"status": EN_STATE["status"], "lines": EN_STATE["lines"],
                                            "video": EN_STATE["video"], "audio": EN_STATE["audio"],
                                            "error": EN_STATE["error"], "has_output": EN_OUTPUT.is_file()})
            if path == "/video/english":
                if not EN_OUTPUT.is_file():
                    return self._send(404, {"error": "İngilizce video henüz hazır değil"})
                return self._serve_file(EN_OUTPUT, "video/mp4")
            if path == "/api/watch":
                return self._send(200, do_watch())
            if path == "/thumb/final":
                if not THUMB_FILE.is_file():
                    return self._send(404, {"error": "Kapak yok"})
                return self._serve_file(THUMB_FILE, "image/png")
            if path == "/api/status":
                off = int(urllib.parse.parse_qs(urlparse(self.path).query).get("from", ["0"])[0])
                return self._send(200, {"status": RUN["status"], "lines": RUN["lines"][off:],
                                        "total": len(RUN["lines"]), "has_output": OUTPUT_VIDEO.is_file()})
            if path == "/video/output":
                if not OUTPUT_VIDEO.is_file():
                    return self._send(404, {"error": "Video yok"})
                return self._serve_file(OUTPUT_VIDEO, "video/mp4")
            if path.startswith("/airef/"):
                nm = path[len("/airef/"):]
                if not re.fullmatch(r"ref_[a-f0-9]{14}\.jpg", nm) or not (AI_REF_DIR / nm).is_file():
                    return self._send(404, {"error": "Referans görsel yok"})
                return self._serve_file(AI_REF_DIR / nm, "image/jpeg")
            if path.startswith("/aiimg/"):
                nm = path[len("/aiimg/"):]
                if not re.fullmatch(r"ai\d+_\d+(?:_\d+)?\.jpg", nm) or not (AI_DIR / nm).is_file():
                    return self._send(404, {"error": "Görsel yok"})
                return self._serve_file(AI_DIR / nm, "image/jpeg")
            if path == "/icon.png":
                return self._send(200, ICON_PNG, "image/png")
            if path == "/api/ping":
                STATE["last_ping"], STATE["bye_at"] = time.time(), 0.0
                return self._send(200, {"app": "sahne-planlayici"})
            if path == "/api/limits":
                return self._send(200, do_limits_get())
            if path == "/api/load":
                try:
                    return self._send(200, json.loads(PROJECT_FILE.read_text(encoding="utf-8")))
                except Exception:
                    return self._send(200, {})
            self._send(404, {"error": "Bulunamadı"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        if not self._allowed():
            return self._send(403, {"error": "Yasak"})
        path = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if path == "/api/en/video":
                return self._send(200, en_upload("video", urllib.parse.parse_qs(urlparse(self.path).query), self.rfile, n))
            if path == "/api/en/audio":
                return self._send(200, en_upload("audio", urllib.parse.parse_qs(urlparse(self.path).query), self.rfile, n))
            if path == "/api/ref_upload":
                return self._send(200, do_reference_upload(urllib.parse.parse_qs(urlparse(self.path).query), self.rfile, n))
            if path == "/api/upload":
                return self._send(200, do_upload(urllib.parse.parse_qs(urlparse(self.path).query), self.rfile, n))
            if path == "/api/thumb_upload":
                return self._send(200, do_thumb_upload(urllib.parse.parse_qs(urlparse(self.path).query), self.rfile, n))
            if path == "/api/audio":
                return self._send(200, do_audio(urllib.parse.parse_qs(urlparse(self.path).query), self.rfile, n))
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            if path == "/api/en/run":
                return self._send(200, en_start(body))
            if path == "/api/quality":
                return self._send(200, do_quality_check(body))
            if path == "/api/bye":
                STATE["bye_at"] = time.time() + 6
                return self._send(200, {"ok": True})
            if path == "/api/arm":
                return self._send(200, do_arm(body))
            if path == "/api/disarm":
                return self._send(200, do_disarm())
            if path == "/api/thumb_clear":
                return self._send(200, do_thumb_clear())
            if path == "/api/prepend":
                return self._send(200, do_prepend(body))
            if path == "/api/config":
                save_keys(body)
                return self._send(200, {"ok": True})
            if path == "/api/scenes":
                return self._send(200, make_scenes(str(body.get("text", "")), str(body.get("density", "sik")), str(body.get("hook", ""))))
            if path == "/api/run":
                return self._send(200, do_run(body))
            if path == "/api/stop":
                return self._send(200, do_stop())
            if path == "/api/new_project":
                return self._send(200, do_new_project())
            if path == "/api/reveal":
                return self._send(200, do_reveal())
            if path == "/api/links":
                return self._send(200, do_links(body.get("queries", []), body.get("orientation", "portrait"), str(body.get("text", "")), str(body.get("context", "")), body.get("avoid") or []))
            if path == "/api/ref_generate":
                return self._send(200, do_reference_generate(body))
            if path == "/api/aigen":
                return self._send(200, do_aigen(body))
            if path == "/api/diagram":
                return self._send(200, do_diagram(body))
            if path == "/api/vprompt":
                return self._send(200, do_vprompt(body))
            if path == "/api/limits":
                return self._send(200, do_limits_set(body))
            if path == "/api/search":
                return self._send(200, do_search(body.get("queries", []), body.get("orientation", "portrait"), str(body.get("text", ""))))
            if path == "/api/pick":
                return self._send(200, do_pick(body))
            if path == "/api/check":
                return self._send(200, {"warnings": check_scenes(body.get("scenes", []))})
            if path == "/api/write":
                return self._send(200, do_write(body.get("scenes", [])))
            if path == "/api/save":
                PROJECT_FILE.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
                return self._send(200, {"ok": True})
            self._send(404, {"error": "Bulunamadı"})
        except Exception as e:
            self._send(500, {"error": str(e)})


ICON_B64 = """
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
"""
ICON_PNG = base64.b64decode("".join(ICON_B64.split()))


HTML = r"""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sahne Planlayıcı</title>
<link rel="icon" type="image/png" href="/icon.png">
<style>
:root{--bg:#F5F6FB;--panel:#fff;--ink:#0B1020;--mute:#667085;--line:#E6E9F2;--soft:#F7F8FC;--accent:#5B4BFF;--tint:#EFEDFF;--warn:#B45309;--bad:#DC2626;--good:#059669;--g:linear-gradient(135deg,#4F46E5,#8B5CF6 55%,#EC4899);--shadow:0 1px 2px rgba(16,24,40,.04),0 18px 40px -24px rgba(16,24,40,.25);--r:20px}
@media (prefers-color-scheme:dark){:root{--bg:#080C18;--panel:#11182B;--ink:#EAEEF9;--mute:#97A3BC;--line:#222C47;--soft:#0C1222;--accent:#8B84FF;--tint:#1D2352;--warn:#F5B454;--bad:#F87171;--good:#34D399;--shadow:0 1px 2px rgba(0,0,0,.4),0 18px 40px -24px rgba(0,0,0,.8)}}
*{box-sizing:border-box}
::selection{background:color-mix(in srgb,var(--accent) 30%,transparent)}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:var(--line);border-radius:10px;border:2px solid transparent;background-clip:padding-box}
body{margin:0;min-height:100vh;color:var(--ink);background:var(--bg);font:15px/1.55 Inter,"Segoe UI Variable","Segoe UI",system-ui,-apple-system,sans-serif;-webkit-font-smoothing:antialiased}
.top{position:sticky;top:0;z-index:20;backdrop-filter:saturate(1.5) blur(16px);background:color-mix(in srgb,var(--bg) 80%,transparent);border-bottom:1px solid var(--line)}
.top>div{display:flex;align-items:center;gap:14px;flex-wrap:wrap;max-width:1280px;margin:0 auto;padding:11px clamp(16px,4vw,40px)}
.logo{width:40px;height:40px;border-radius:12px;background:var(--g);display:grid;place-items:center;box-shadow:0 10px 22px -10px #8B5CF6}
.logo svg{width:22px;height:22px}
h1{font-size:18px;margin:0;letter-spacing:-.02em;line-height:1.2}
.top p{margin:0;color:var(--mute);font-size:12.5px}
h2{font-size:15px;margin:0 0 10px;letter-spacing:-.01em}
main{padding:0 clamp(16px,4vw,40px) 28px;max-width:1280px;margin:0 auto}
.hero{position:relative;overflow:hidden;margin:20px 0 8px;padding:36px clamp(22px,4vw,46px) 26px;border-radius:30px;color:#fff;background:radial-gradient(620px 320px at 92% -10%,rgba(236,72,153,.5),transparent 62%),radial-gradient(720px 380px at 4% 115%,rgba(79,70,229,.75),transparent 62%),#0B1020;box-shadow:0 36px 70px -36px rgba(91,75,255,.6)}
.heroin{display:flex;gap:28px;align-items:flex-end;justify-content:space-between;flex-wrap:wrap}
.eyebrow{display:inline-block;font-size:11.5px;font-weight:650;letter-spacing:.09em;text-transform:uppercase;padding:5px 13px;border-radius:99px;background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.18)}
.big{font-size:clamp(28px,3.8vw,44px);line-height:1.07;letter-spacing:-.04em;margin:16px 0 12px;max-width:660px;font-weight:760}
.big em{font-style:normal;background:linear-gradient(90deg,#A5B4FC,#F9A8D4);-webkit-background-clip:text;background-clip:text;color:transparent}
.hero p{margin:0;max-width:540px;color:rgba(255,255,255,.72);font-size:15.5px}
.stats{display:flex;gap:10px;flex-wrap:wrap}
.stats div{min-width:108px;padding:14px 18px;border-radius:18px;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.15);backdrop-filter:blur(8px)}
.stats b{display:block;font-size:28px;letter-spacing:-.03em;line-height:1.1;font-variant-numeric:tabular-nums}
.stats span{font-size:12px;color:rgba(255,255,255,.62)}
.steps{display:flex;gap:8px;flex-wrap:wrap;list-style:none;margin:26px 0 0;padding:0;counter-reset:s}
.steps li{counter-increment:s;display:flex;align-items:center;gap:9px;padding:6px 15px 6px 6px;border-radius:99px;font-size:13px;font-weight:560;color:rgba(255,255,255,.65);background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);transition:.25s}
.steps li::before{content:counter(s);display:grid;place-items:center;width:24px;height:24px;border-radius:50%;background:rgba(255,255,255,.14);font-size:12px}
.steps li.done{color:#fff;border-color:rgba(110,231,183,.55);background:rgba(16,185,129,.14)}
.steps li.done::before{content:"\2713";background:#10B981}
.panel,.scene{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:22px;margin:16px 0;box-shadow:var(--shadow)}
.scene{transition:border-color .2s,transform .2s}
.scene:hover{border-color:color-mix(in srgb,var(--accent) 45%,var(--line))}
textarea,input,select{width:100%;font:inherit;color:var(--ink);background:var(--soft);border:1px solid var(--line);border-radius:12px;padding:10px 13px;transition:border-color .15s,box-shadow .15s}
textarea:focus,input:focus,select:focus{outline:0;border-color:var(--accent);box-shadow:0 0 0 4px color-mix(in srgb,var(--accent) 18%,transparent)}
textarea{resize:vertical}
label{display:block;font-size:12.5px;font-weight:600;color:var(--mute);margin-bottom:8px}
label>textarea,label>input,label>select{margin-top:5px;font-size:15px;font-weight:400}
.inline{display:flex;align-items:center;gap:8px;margin:0}
.inline select{width:auto;margin:0}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:12px}
button{font:inherit;font-weight:600;border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:12px;padding:9px 16px;cursor:pointer;transition:transform .12s,box-shadow .15s,border-color .15s,filter .15s}
button:hover{border-color:var(--accent);box-shadow:0 8px 18px -10px var(--accent)}
button:active{transform:translateY(1px)}
button:disabled{opacity:.5;cursor:default;transform:none;box-shadow:none}
button.primary{background:var(--g);border-color:transparent;color:#fff;box-shadow:0 12px 24px -12px #8B5CF6}
button.primary:hover{filter:brightness(1.08)}
button.quiet{border-color:transparent;color:var(--mute);background:transparent;box-shadow:none}
button.quiet:hover{color:var(--ink);background:var(--tint)}
.spacer{flex:1}
.chips{display:flex;gap:6px}
.chip{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;padding:4px 11px;border-radius:99px;border:1px solid var(--line);background:var(--panel);color:var(--mute)}
.chip i{width:7px;height:7px;border-radius:50%;background:var(--good)}
.note{margin:16px 0;padding:12px 18px;border-radius:14px;border:1px solid var(--line);border-left:4px solid var(--mute);background:var(--panel);box-shadow:var(--shadow)}
.note.warn{border-left-color:var(--warn);color:var(--warn)}
.note.bad{border-left-color:var(--bad);color:var(--bad)}
.note.good{border-left-color:var(--good);color:var(--good)}
.scene{display:grid;grid-template-columns:112px minmax(0,1fr) minmax(0,1.25fr);gap:22px}
.when{display:flex;flex-direction:column;gap:6px;align-items:flex-start}
.when b{display:grid;place-items:center;width:46px;height:46px;border-radius:14px;background:var(--g);color:#fff;font-size:21px;line-height:1;box-shadow:0 12px 22px -12px #8B5CF6}
.tc{font-variant-numeric:tabular-nums;font-size:13px;font-weight:650}
.len{font-size:12px;color:var(--mute)}
.pill{font-size:11px;font-weight:650;padding:3px 10px;border-radius:99px;background:var(--soft);color:var(--mute);border:1px solid var(--line)}
.pill.ok{background:rgba(16,185,129,.12);color:var(--good);border-color:rgba(16,185,129,.35)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.busy{color:var(--mute);font-size:13px}
.busy:not(:empty)::before{content:"";display:inline-block;width:12px;height:12px;border:2px solid var(--line);border-top-color:var(--accent);border-radius:50%;margin-right:7px;vertical-align:-2px;animation:sp .7s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.clips{display:grid;grid-template-columns:repeat(auto-fill,minmax(148px,1fr));gap:10px;margin-bottom:12px}
.clip{border:1px solid var(--line);border-radius:12px;padding:7px;background:var(--soft);font-size:12px}
.clip.on{border-color:var(--good);box-shadow:0 0 0 2px rgba(16,185,129,.25)}
.cv{position:relative;aspect-ratio:9/12;border-radius:9px;overflow:hidden;background:rgba(0,0,0,.15);cursor:pointer}
.cv img,.cv video{width:100%;height:100%;object-fit:cover;display:block}
.cv .play{position:absolute;inset:0;display:grid;place-items:center;color:#fff;font-size:22px;text-shadow:0 1px 6px rgba(0,0,0,.55)}
.cv .sc{position:absolute;top:5px;left:5px;padding:1px 7px;border-radius:99px;font-weight:700;color:#fff;font-size:11px}
.sc.hi{background:#059669}.sc.mid{background:#d97706}.sc.lo{background:#6b7280}
.cv .du{position:absolute;bottom:5px;right:5px;padding:0 6px;border-radius:6px;background:rgba(0,0,0,.65);color:#fff;font-size:11px}
.clip .ct{margin:6px 0 2px;font-weight:650}
.clip small{display:block;color:var(--mute);line-height:1.3;max-height:2.7em;overflow:hidden}
.clip small+small{margin-top:3px;font-style:italic}
.clip .row{margin-top:6px;gap:6px}
.cliphead{margin:0 0 6px;font-size:12px;color:var(--mute)}
.links{display:flex;flex-wrap:wrap;gap:8px}
.lnk{display:flex;flex-direction:column;min-width:124px;padding:9px 13px;border:1px solid var(--line);border-radius:12px;background:var(--soft);color:var(--ink);text-decoration:none;font-size:13px;transition:border-color .15s,transform .12s,box-shadow .15s}
.lnk:hover{border-color:var(--accent);transform:translateY(-2px);box-shadow:var(--shadow)}
.lnk b::before{content:"";display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;background:var(--accent)}
.lnk[data-s=Pexels] b::before{background:#05A081}
.lnk[data-s=Pixabay] b::before{background:#2EC66D}
.lnk[data-s=Coverr] b::before{background:#FF7A45}
.lnk[data-s=Mixkit] b::before{background:#00C2A8}
.lnk span{color:var(--mute)}
.drop{display:block;margin:12px 0 0;padding:18px;text-align:center;border:1.5px dashed var(--line);border-radius:14px;cursor:pointer;color:var(--mute);font-weight:600;transition:.15s}
.drop:hover,.drop.over{border-color:var(--accent);background:var(--tint);color:var(--accent)}
.chosen{margin-bottom:10px;font-size:13px;padding:8px 13px;border-radius:12px;background:rgba(16,185,129,.1);color:var(--ink);border:1px solid rgba(16,185,129,.3)}
.empty{text-align:center;color:var(--mute);padding:38px 16px;border:1.5px dashed var(--line);border-radius:var(--r);margin:0}
.empty b{display:block;color:var(--ink);font-size:16px;margin-bottom:4px}
#warns{margin:0 0 12px}
#warns li{color:var(--warn);margin:3px 0}
.sec{margin:30px 4px 10px;font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--mute)}
.out{padding:0;overflow:hidden;margin-top:0;background:#0B1020;border-color:#1E2745;box-shadow:0 30px 60px -34px rgba(91,75,255,.55)}
.winbar{display:flex;align-items:center;gap:7px;padding:12px 16px;border-bottom:1px solid rgba(255,255,255,.08);color:#9AA6C4;font:600 13px/1 ui-monospace,Consolas,monospace}
.winbar i{width:11px;height:11px;border-radius:50%;background:#FF5F57}
.winbar i:nth-child(2){background:#FEBC2E}
.winbar i:nth-child(3){background:#28C840}
.winbar span{margin-left:8px}
.winbar button{padding:7px 13px;font-size:13px;color:#C7D0EA;border-color:rgba(255,255,255,.16);background:transparent}
.winbar button.primary{color:#fff;border-color:transparent;background:var(--g)}
pre{margin:0;padding:18px 22px;overflow:auto;max-height:340px;color:#C7D0EA;font:13.5px/1.8 ui-monospace,"Cascadia Code",Consolas,monospace}
pre .c,pre .k{color:#6B7799}
pre .f{color:#A5B4FC}
pre .t{color:#F9A8D4}
.bar{position:sticky;bottom:14px;z-index:15;width:min(1180px,calc(100% - 32px));margin:0 auto 14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:10px 12px 10px 24px;border-radius:99px;border:1px solid var(--line);backdrop-filter:blur(16px) saturate(1.4);background:color-mix(in srgb,var(--panel) 82%,transparent);box-shadow:0 24px 50px -20px rgba(16,24,40,.4);font-size:13px;color:var(--mute)}
.bar button{border-radius:99px}
dialog{border:1px solid var(--line);border-radius:22px;padding:26px;max-width:480px;width:92vw;background:var(--panel);color:var(--ink);box-shadow:0 40px 90px -24px rgba(0,0,0,.55)}
dialog::backdrop{background:rgba(8,12,24,.55);backdrop-filter:blur(5px)}
dialog p{margin:4px 0 14px;color:var(--mute);font-size:13px}
dialog a{color:var(--accent)}
[hidden]{display:none!important}
.thumb{display:grid;grid-template-columns:minmax(0,1fr) 290px;gap:26px}
.phone img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain}
#tEmpty{position:absolute;inset:0;display:grid;place-items:center;text-align:center;color:#6B7799;font-size:14px;padding:20px}
.alts{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 4px}
.alts button{font-size:12.5px;padding:5px 11px;border-radius:99px}
.wait{margin:10px 0 0;padding:10px 13px;border-radius:12px;background:var(--tint);border:1px solid var(--accent);font-size:13px}
.wait small{display:block;margin-top:4px;color:var(--mute)}
.err{color:var(--bad)!important}.err::before{display:none!important}
.hint{font-size:12.5px;color:var(--mute);margin:6px 2px 0}
@media (max-width:900px){.thumb{grid-template-columns:1fr}}
.studio{display:grid;grid-template-columns:minmax(0,1fr) 290px;gap:26px}
.opts{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:14px}
.chk{display:flex;align-items:center;gap:8px;margin:2px 0 0}
.chk input{width:auto}
.aud{margin:8px 2px 0;font-size:13px;color:var(--mute)}
.prog{height:10px;border-radius:99px;background:var(--line);overflow:hidden;margin-top:14px}
.prog i{display:block;height:100%;width:0;background:var(--g);transition:width .6s}
.logbox{margin-top:12px;padding:14px 16px;border-radius:14px;background:#0B1020;color:#C7D0EA;font:12.5px/1.7 ui-monospace,Consolas,monospace;max-height:230px;overflow:auto;white-space:pre-wrap}
.phone{position:relative;aspect-ratio:9/16;border-radius:30px;border:7px solid #1B2342;background:#0B1020;overflow:hidden;box-shadow:0 30px 60px -30px rgba(91,75,255,.6)}
.phone video{position:absolute;inset:0;width:100%;height:100%;object-fit:contain}
#pvEmpty{position:absolute;inset:0;display:grid;place-items:center;text-align:center;color:#6B7799;font-size:14px;padding:20px}
.abtn{display:inline-block;padding:9px 16px;border-radius:12px;background:var(--g);color:#fff;font-weight:600;text-decoration:none}
@media (max-width:900px){.studio{grid-template-columns:1fr}.phone{max-width:290px}}
@media (max-width:900px){.scene{grid-template-columns:1fr}.when{flex-direction:row;align-items:center;gap:12px}.two{grid-template-columns:1fr}.bar{border-radius:22px}}
.english-panel{border:1px solid color-mix(in srgb,#6366F1 35%,var(--line));background:linear-gradient(135deg,color-mix(in srgb,#6366F1 7%,var(--panel)),var(--panel));}
.en-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}
.en-upload{display:block;padding:20px;border:1.5px dashed var(--line);border-radius:14px;background:var(--soft);text-align:center;cursor:pointer;color:var(--mute);font-weight:650}
.en-upload:hover{border-color:var(--accent);background:var(--tint);color:var(--accent)}
.en-file{font-size:12px;color:var(--good);margin-top:6px;overflow-wrap:anywhere}
.en-preview{max-width:260px;margin:12px auto 0}
@media(max-width:700px){.en-grid{grid-template-columns:1fr}}
.aivid{margin:14px 0 0;padding:12px 13px;border:1.5px solid var(--accent);border-radius:14px;background:var(--tint)}
.avh{font-weight:700;font-size:13.5px;margin-bottom:8px}
.aivid .row{margin-top:8px}
.aivid small,.aihelp{display:block;margin-top:8px;color:var(--mute);line-height:1.5;font-size:12.5px}
button.vgo{background:var(--g);border-color:transparent;color:#fff;box-shadow:0 12px 24px -12px #8B5CF6}
button.vgo:hover{filter:brightness(1.08)}
.vchips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
perf-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:11px;margin:12px 0}.perf-grid label{font-size:12.5px;color:var(--mute)}.perf-grid input{display:block;width:100%;margin-top:5px}.perf-result{margin-top:12px;padding:14px 16px;border:1px solid var(--line);border-radius:13px;background:var(--soft);line-height:1.65}.perf-result b{color:var(--text)}.perf-result ul{margin:7px 0 0;padding-left:20px}@media(max-width:700px){.perf-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
/* Kalite yüzdeleri: büyük ve ilk bakışta okunur gösterge */
.quality-overview{display:grid;grid-template-columns:190px 1fr;gap:18px;align-items:stretch;margin:16px 0}
.quality-total{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:18px;border:1px solid var(--line);border-radius:16px;background:var(--soft)}
.quality-donut{width:132px;height:132px;border-radius:50%;display:grid;place-items:center;background:conic-gradient(var(--good) 0deg,var(--line) 0deg);position:relative;margin-bottom:8px}.quality-donut:before{content:"";position:absolute;inset:10px;border-radius:50%;background:var(--panel)}.quality-donut strong{position:relative;z-index:1;font-size:30px;line-height:1.1;letter-spacing:-.04em;font-variant-numeric:tabular-nums}.scene-quality{float:right;margin-left:8px;padding:4px 8px;border-radius:99px;background:var(--soft);border:1px solid var(--line);font-size:11px;color:var(--mute);font-weight:700}.scene-meaning{margin:-3px 0 10px;padding:8px 10px;border-left:3px solid var(--accent);background:var(--soft);border-radius:0 8px 8px 0;font-size:12px;color:var(--mute)}.scene-meaning b{color:var(--text)}
.quality-total span{font-size:12px;color:var(--mute);margin-top:6px}.quality-total small{font-size:11px;color:var(--mute);margin-top:8px}
.quality-metrics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.quality-metric{padding:12px;border:1px solid var(--line);border-radius:13px;background:var(--panel);min-width:0}
.quality-metric-head{display:flex;justify-content:space-between;gap:8px;align-items:center;font-size:12px}
.quality-metric-head b{font-size:17px;font-variant-numeric:tabular-nums;white-space:nowrap}
.quality-metric small{display:block;color:var(--mute);font-size:10px;margin-top:2px}
.quality-track{height:9px;background:var(--line);border-radius:99px;overflow:hidden;margin-top:9px}
.quality-fill{height:100%;width:0;background:linear-gradient(90deg,#F59E0B,#10B981);border-radius:99px;transition:width .35s}
.quality-scenes{display:grid;gap:9px;margin:12px 0}.quality-scene{padding:12px;border:1px solid var(--line);border-radius:12px;background:var(--panel)}
.quality-scene-top{display:flex;justify-content:space-between;gap:10px;font-size:13px}.quality-scene-top b{font-size:16px;font-variant-numeric:tabular-nums}.quality-scene .quality-track{margin:7px 0}
@media(max-width:700px){.quality-overview{grid-template-columns:1fr}.quality-metrics{grid-template-columns:1fr 1fr}}@media(max-width:420px){.quality-metrics{grid-template-columns:1fr}}

button.vchip{padding:4px 11px;font-size:12.5px;border-radius:99px;background:var(--panel)}
button.vchip.cur{border-color:var(--accent);color:var(--accent)}
button.vchip.off{opacity:.55;text-decoration:line-through}
button.vchip i{font-style:normal;color:var(--mute);margin-left:4px}
</style>
</head>
<body>
<header class="top"><div>
  <div class="logo"><svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="3"/><path d="M10 9.5v5l4.5-2.5z" fill="#fff"/></svg></div>
  <div><h1>Sahne Planlayıcı</h1><p>Metin → sahneler → stok videolar → eslesme.txt</p></div>
  <span class="spacer"></span>
  <span class="chips" id="keys"></span><button class="quiet" id="keysBtn">⚙ API anahtarları</button>
</div></header>
<main>
  <section class="hero">
    <div class="heroin">
      <div><span class="eyebrow">Video planlama stüdyosu</span>
        <h2 class="big">Metinden sahneye, sahneden <em>eşleşmeye.</em></h2>
        <p>Metni yapay zekâyla anlamlı sahnelere ayır; önce her sahne için özgün AI görseli üret, gerekirse Pixabay/Pexels yedeğini seç ve videonu hazırla.</p></div>
      <div class="stats"><div><b id="stS">0</b><span>Sahne</span></div><div><b id="stV">0</b><span>Video eklendi</span></div><div><b id="stT">0:00</b><span>Tahmini süre</span></div></div>
    </div>
    <ol class="steps" id="steps"><li>Metin</li><li>Sahneler</li><li>Videolar</li><li>Ses</li><li>Video</li></ol>
  </section>
  
  <section class="panel">
    <h2>Seslendirme metni</h2>
    <textarea id="text" rows="6" placeholder="Metni buraya yapıştır."></textarea>
    <div class="row">
      <label class="inline">Video yönü
        <select id="orient"><option value="portrait">Dikey (Shorts / Reels)</option><option value="landscape">Yatay</option></select>
      </label>
      <label class="inline">Sahne sıklığı
        <select id="density"><option value="sik">Sık, akıcı (~2 sn)</option><option value="cok">Çok sık, hızlı kurgu (~1,3 sn)</option><option value="normal">Normal (cümle cümle)</option></select>
      </label>
      <span class="spacer"></span>
      <button class="primary" id="splitBtn">✦ AI ile anlamlı sahnelere böl + görselleri üret</button>
      <button id="allBtn">Tüm sahneler için linkleri hazırla</button>
    </div>
  </section>
  <section class="panel" id="continuityPanel" style="margin-top:14px">
    <h2>🎬 Film görsel kimliği · otomatik stil + karakter</h2>
    <p class="hint">“Sahnelere böl”e basınca otomatik çalışır: stil rehberi, sabit seed ve (Cloudflare varsa) karakter portreleri hazırlanır. Bir şey yazmana gerek yok. AI görsel üretirken hepsi birlikte kullanılır.</p>
    <label>Otomatik doldurulur (istersen düzenle)<textarea id="continuityGuide" rows="2" placeholder="Sahnelere bölünce otomatik dolar. Elle de yazabilirsin."></textarea></label>
    <div class="row" style="margin-top:10px">
      <button class="vgo" id="refGenerate">✨ Yeniden analiz et (otomatik de çalışır)</button>
      <label class="drop" style="flex:1;margin:0">🧑 Referans görseli yükle (en fazla 4)<input id="refUpload" type="file" accept="image/png,image/jpeg,image/webp" hidden multiple></label>
      <span class="busy" id="refBusy"></span>
    </div>
    <div id="refList" class="row" style="flex-wrap:wrap;margin-top:10px"></div>
  </section>
  <p class="note" id="note" hidden></p>
  <section id="scenes"></section>
  <ul id="warns"></ul>
  <h2 class="sec">İngilizceye dönüştür</h2>
  <section class="panel english-panel" id="englishStudio">
    <h2>Mevcut videoyu İngilizce ses ve altyazıyla hazırla</h2>
    <p class="hint">Orijinal görüntü korunur, eski ses kullanılmaz. İngilizce ses dosyanı ElevenLabs'ten hazırlayıp buraya yükle.</p>
    <div class="en-grid">
      <div>
        <label class="en-upload" for="enVideo">🎬 Orijinal videoyu yükle<input id="enVideo" type="file" accept="video/mp4,video/quicktime,video/webm,video/x-matroska" hidden></label>
        <div class="en-file" id="enVideoName">Henüz video seçilmedi.</div>
      </div>
      <div>
        <label class="en-upload" for="enAudio">🎙 İngilizce ElevenLabs sesini yükle<input id="enAudio" type="file" accept="audio/*" hidden></label>
        <div class="en-file" id="enAudioName">Henüz ses seçilmedi.</div>
      </div>
    </div>
    <label class="chk" style="margin-top:16px"><input type="checkbox" id="enClean" checked> Videonun altındaki Türkçe altyazı alanını görüntü onarmayla temizle</label>
    <p class="hint">Not: Bu seçenek alt %32'lik bölgeyi tamamen siyah bir şeritle kapatır (üst kenarı yumuşaktır, yeni altyazı bu şeridin üstüne yazılır). Başka bölgelerdeki yazıları silemez.</p>
    <div class="row"><button class="primary" id="enRun">🌐 İngilizce videoyu oluştur</button><span class="busy" id="enStatus"></span></div>
    <div class="logbox" id="enLog" data-empty="1">İngilizce dönüştürme henüz başlatılmadı.</div>
    <div class="en-preview" id="enPreviewWrap" hidden>
      <div class="phone"><video id="enPreview" controls playsinline></video></div>
      <div class="row"><a class="abtn" href="/video/english" download="english_output.mp4">⬇ İngilizce videoyu indir</a></div>
    </div>
  </section>
  <h2 class="sec">Kapak (videonun en başına eklenir)</h2>
  <section class="panel thumb" id="thumbPanel">
    <div>
      <label class="drop" id="tDrop" style="margin-top:0">🖼 Kapak görselini buraya sürükle ya da tıkla (png, jpg)<input type="file" accept="image/*" hidden id="tUp"></label>
      <p class="hint" id="tInfo"></p>
      <label class="chk" style="margin-top:12px"><input type="checkbox" id="tPrepend" checked> Video üretilince kapağı en başa ekle</label>
      <label style="max-width:240px">Başta kaç kare görünsün (30 kare = 1 sn)<input type="number" id="tFrames" value="1" min="1" max="150" step="1"></label>
      <div class="row"><button class="quiet" id="tClear" hidden>Kapağı kaldır</button><span class="busy" id="tBusy"></span></div>
    </div>
    <div><div class="phone"><img id="tImg" hidden alt="Kapak"><div id="tEmpty">Kapak<br>burada görünecek</div></div></div>
  </section>
  <h2 class="sec">Üretim</h2>
  <section class="panel studio" id="studio">
    <div>
      <label class="drop" id="audDrop">🎵 Ses dosyasını sürükle ya da tıkla (mp3, wav, m4a…)<input type="file" accept="audio/*" hidden id="audIn"></label>
      <p class="aud" id="audioInfo"></p>
      <div class="opts">
        <label>Konuşma tanıma modeli<select id="oModel"><option>medium</option><option>large-v3</option><option>small</option><option>base</option></select></label>
        <label>Altyazı stili<select id="oStyle"><option value="kelime">Kelime kelime</option><option value="grup">Grup (2-3 kelime)</option></select></label>
        <label>Vurgu rengi<select id="oHl"><option value="sari">Sarı</option><option value="cyan">Camgöbeği</option></select></label>
        <label>Altyazı yüksekliği (px)<input type="number" id="oMargin" value="400" min="0" max="1200" step="20"></label>
      </div>
      <label class="chk"><input type="checkbox" id="oUp" checked> Altyazı BÜYÜK HARF</label>
      <label class="chk"><input type="checkbox" id="oClean"> Sesi temizle (cümle aralarındaki tıs/cızırtı sesleri)</label>
      <div class="row"><button class="primary gen">▶ Videoyu üret</button><button id="stopBtn" hidden>■ Durdur</button><span class="spacer"></span><span class="busy" id="pct"></span></div>
      <div class="prog"><i id="progBar"></i></div>
      <div class="logbox" id="log" data-empty="1">Henüz üretim yapılmadı.</div>
    </div>
    <div>
      <div class="phone"><video id="pv" controls playsinline hidden></video><div id="pvEmpty">Önizleme<br>burada görünecek</div></div>
      <div class="row"><a class="abtn" id="dlVid" href="/video/output" download="output_video.mp4" hidden>⬇ Videoyu indir</a><button id="revBtn" hidden>Klasörde göster</button><button class="primary newproj" hidden>＋ Yeni proje</button><button id="prepBtn" hidden>🖼 Kapağı başa ekle</button></div>
    </div>
  </section>
  <h2 class="sec">Çıktı</h2>
  <section class="panel out"><div class="winbar"><i></i><i></i><i></i><span>eslesme.txt</span><span class="spacer"></span><button id="copyBtn">Kopyala</button><button class="primary dlb">⬇ İndir</button></div><pre id="outTxt"></pre></section>
</main>
<div class="bar">
  <span id="total"></span><span class="spacer"></span>
  <button id="checkBtn">Kontrol et</button>
  <button id="writeBtn">Proje klasörüne kaydet</button>
  <button class="dlb">⬇ eslesme.txt indir</button>
  <button class="primary gen">▶ Videoyu üret</button>
  <button class="primary newproj" hidden>＋ Yeni proje</button>
</div>

<dialog id="dlg">
  <h2>API anahtarları (opsiyonel)</h2>
  <p>Anahtarlar yalnızca bu bilgisayardaki planner_keys.json dosyasına kaydedilir. Boş bırakırsan uygulama anahtarsız da çalışır (yalnızca arama linkleri gelir).</p>
  <label>Pixabay (ücretsiz; her sahneye puanlı, önizlemeli klip önerir. <a href="https://pixabay.com/api/docs/" target="_blank" rel="noopener">buradan al</a>: giriş yapınca anahtar sayfanın en üstünde görünür)<input type="password" id="kPixabay" autocomplete="off"></label>
  <label>Groq (ÖNERİLEN, ücretsiz, kart istemez: arama kelimelerini her sahnenin metnine göre akıllıca üretir. <a href="https://console.groq.com/keys" target="_blank" rel="noopener">buradan al</a>)<input type="password" id="kGroq" autocomplete="off"></label>
  <label>Cloudflare Account ID (ücretsiz; Groq'a yedek + filigransız AI görsel. <a href="https://dash.cloudflare.com/profile/api-tokens" target="_blank" rel="noopener">API Token oluştur</a>, şablon: "Workers AI")<input type="password" id="kCfAcc" autocomplete="off"></label>
  <label>Cloudflare API Token<input type="password" id="kCfTok" autocomplete="off"></label>
  <label>Anthropic (kalite kontrolü gerçek sahne karelerini görerek yapar)<input type="password" id="kAnthropic" autocomplete="off"></label>
  <div class="row"><span class="spacer"></span><button id="kCancel">Vazgeç</button><button class="primary" id="kSave">Kaydet</button></div>
</dialog>

<script>
const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let hasCF = false;
let S = {text: "", orientation: "portrait", density: "sik", hook: "X", scenes: [], continuityGuide: "", referenceImages: [], characterProfiles: [], characterAnalysisScript: ""};
let armedIdx = -1, watchTimer = null, thumbInfo = {final: 0}, keyHintShown = false;
let armSite = "", LIM = {date: "", sites: {}}, vpBusy = false;
const VSITES = [
  {id: "gflow", name: "Google Flow", url: "https://labs.google/fx/tools/flow"},
  {id: "dreamina", name: "Dreamina", url: "https://dreamina.capcut.com/"},
  {id: "pixverse", name: "PixVerse", url: "https://app.pixverse.ai/"},
  {id: "kling", name: "Kling", url: "https://klingai.com/"},
  {id: "hailuo", name: "Hailuo", url: "https://hailuoai.video/"}
];
let audioSec = 0, saveTimer = null, hasAudio = false, outReady = false, runOff = 0, runSteps = 0, poll = null;
let qualityApproved = true, qualityLast = null;

async function api(path, body) {
  const opt = body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)};
  const r = await fetch(path, opt);
  const d = await r.json().catch(() => ({error: "Sunucu yanıtı okunamadı"}));
  if (!r.ok || d.error) throw new Error(d.error || ("HTTP " + r.status));
  return d;
}
function note(msg, kind) { const n = $("#note"); n.textContent = msg || ""; n.className = "note " + (kind || ""); n.hidden = !msg; }
function fail(el, e) {                       // hatayı düğmenin yanında göster, anahtar eksikse pencereyi aç
  const m = e.message || String(e);
  el.className = "busy err"; el.textContent = "⚠ " + m; note(m, "bad");
  if (/anahtar/i.test(m)) $("#dlg").showModal();
}
function save() { clearTimeout(saveTimer); saveTimer = setTimeout(() => api("/api/save", S).catch(() => {}), 400); }
const words = t => (String(t).match(/\S+/g) || []).length;
function fmt(s) { s = Math.max(0, s); return Math.floor(s / 60) + ":" + (s % 60).toFixed(1).padStart(4, "0"); }
function norm(s) {
  return {text: s.text || "", en: s.en || "", slug: s.slug || "clip", triggers: s.triggers || [], queries: s.queries || [],
          file: s.file || "", page: s.page || "", pickedId: s.pickedId || "", links: s.links || [], clips: s.clips || [], vprompt: s.vprompt || "", visual: s.visual || "video", visual_reason: s.visual_reason || "", prepError: s.prepError || "", busy: ""};
}
function paintContinuity() {
  const guide = $("#continuityGuide"), list = $("#refList");
  if (guide && document.activeElement !== guide) guide.value = S.continuityGuide || "";
  if (!list) return;
  list.innerHTML = (S.referenceImages || []).map((r, i) => `<div class="clip" style="width:130px;padding:7px"><img src="${esc(r.url)}" alt="Referans ${i+1}" style="width:100%;height:100px;object-fit:cover;border-radius:8px"><small>${esc(r.label || (i === 0 ? "Karakter" : "Ek referans"))}</small><button class="quiet" data-ref-remove="${i}">Kaldır</button></div>`).join("") || '<small class="hint">Henüz referans görsel eklenmedi.</small>';
}
async function uploadReference(file) {
  if (!file) return;
  if ((S.referenceImages || []).length >= 4) return note("En fazla 4 referans görsel kullanabilirsin.", "warn");
  $("#refBusy").textContent = "referans işleniyor…";
  try {
    const r = await fetch("/api/ref_upload?name=" + encodeURIComponent(file.name), {method:"POST", body:file});
    const d = await r.json().catch(()=>({}));
    if (!r.ok || d.error) throw new Error(d.error || "Referans yüklenemedi.");
    S.referenceImages = [...(S.referenceImages || []), {name:d.name, url:d.url}].slice(0,4);
    paintContinuity(); save(); note("Referans görsel eklendi. İlk görsel karakter kimliği için kullanılacak.", "good");
  } catch(e) { note(e.message, "bad"); }
  $("#refBusy").textContent = ""; $("#refUpload").value = "";
}

async function ensureCharacterReferences(force = false) {
  if (!S.text || S.text.trim().length < 20) {
    if (force) throw new Error("Önce senaryoyu/metni ekle; karakterleri metinden çıkaracağım.");
    return false;
  }
  if (!force && S.characterAnalysisScript === S.text) return true;
  if (!hasCF) {
    if (force) throw new Error("Otomatik karakter referansı için Cloudflare API anahtarlarını ekle.");
    return false; // sessizce atla; stok video akışı bozulmasın
  }
  const busy = $("#refBusy"), btn = $("#refGenerate");
  if (busy) busy.textContent = "senaryo analiz ediliyor, stil + karakter portreleri otomatik oluşturuluyor…";
  if (btn) btn.disabled = true;
  try {
    const d = await api("/api/ref_generate", {context:S.text, continuity_guide:S.continuityGuide || ""});
    const oldAutoNames = new Set((S.characterProfiles || []).map(c => c.imageName));
    const manualRefs = (S.referenceImages || []).filter(r => !oldAutoNames.has(r.name));
    S.continuityGuide = d.visual_bible || S.continuityGuide || "";
    S.characterProfiles = Array.isArray(d.characters) ? d.characters : [];
    const autoRefs = S.characterProfiles.map(c => ({name:c.imageName, url:c.url, label:c.name}));
    S.referenceImages = autoRefs.concat(manualRefs).slice(0,4);
    S.characterAnalysisScript = S.text;
    paintContinuity(); save();
    note(d.message || "Stil ve karakter referansları otomatik hazır.", "good");
    return true;
  } finally {
    if (busy) busy.textContent = "";
    if (btn) btn.disabled = false;
  }
}
async function generateCharacterReference() {
  try { await ensureCharacterReferences(true); }
  catch(e) { note(e.message, "bad"); }
}

function totalSec() { const tw = S.scenes.reduce((a, s) => a + Math.max(words(s.text), 1), 0); return audioSec || tw / 2.6; }
function times() {
  const w = S.scenes.map(s => Math.max(words(s.text), 1)), tw = w.reduce((a, b) => a + b, 0) || 1, total = totalSec();
  let t = 0; return w.map(x => { const d = total * x / tw, r = [t, t + d]; t += d; return r; });
}

function updateTimes() {
  const T = times();
  document.querySelectorAll(".scene").forEach((el, i) => {
    if (!T[i]) return;
    $(".tc", el).textContent = fmt(T[i][0]) + "–" + fmt(T[i][1]);
    $(".len", el).textContent = "≈ " + (T[i][1] - T[i][0]).toFixed(1) + " sn";
  });
  $("#total").textContent = S.scenes.length
    ? "Toplam " + fmt(totalSec()) + (audioSec ? " (ses dosyasından; dağılım kelime sayısına göre)" : " (tahmini, saniyede ~2,6 kelime)")
    : "";
}
function smallest(files) { return [...files].sort((a, b) => Math.max(a.w, a.h) - Math.max(b.w, b.h)).find(f => Math.max(f.w, f.h) >= 320) || files[0]; }
function clipsHtml(s, i) {
  if (!s.clips || !s.clips.length) return "";
  const used = new Set(S.scenes.map((x, j) => j !== i ? x.pickedId : "").filter(Boolean));
  return '<p class="cliphead">Etiket puanına göre sıralı öneriler (puan = arama kelimesinin klibin etiketleriyle örtüşmesi). Videoyu kendin de kontrol et. "📷 foto" olanlar seçince yavaş hareketli videoya çevrilir.</p><div class="clips">'
    + s.clips.map((c, k) => {
      const id = c.source + ":" + c.id, lvl = c.score >= 70 ? "hi" : c.score >= 50 ? "mid" : "lo", on = s.pickedId === id;
      return `<div class="clip${on ? " on" : ""}" data-k="${k}">
        <div class="cv"${c.kind === "photo" ? "" : ' data-a="preview"'}>${c.thumb ? `<img loading="lazy" src="${esc(c.thumb)}" alt="" onerror="this.style.opacity=.25;this.alt='yüklenemedi'">` : ""}${c.kind === "photo" ? "" : '<span class="play">▶</span>'}<span class="sc ${lvl}">${c.source === "AI" ? "AI üretimi" : `Eşleşme %${c.score}`}</span><span class="du">${c.kind === "photo" ? "📷 foto → video" : Math.round(c.duration || 0) + " sn"}</span></div>
        <div class="ct">${esc((c.matched || []).join(", ") || "etiket eşleşmesi yok")}</div>
        <small>${esc(c.tags || "")}</small>
        <small>aranan: ${esc(c.q || "")}</small>
        <div class="row"><button class="quiet" data-a="pick">${on ? "✓ Seçildi" : "Seç"}</button><a href="${esc(c.page)}" target="_blank" rel="noopener">${esc(c.source)} ↗</a>${used.has(id) ? '<span class="pill">başka sahnede</span>' : ""}</div>
      </div>`;
    }).join("") + "</div>";
}
async function pickClip(i, k) {
  const s = S.scenes[i], c = s.clips && s.clips[k]; if (!c) return;
  s.busy = (c.kind === "photo" ? "fotoğraf videoya çevriliyor…" : "video indiriliyor…"); render();
  try {
    const d = await api("/api/pick", {result: c, index: i + 1, slug: s.slug, old_file: s.file || "", words: words(s.text)});
    s.file = d.file; s.pickedId = c.source + ":" + c.id; s.page = c.page; s.visual = c.kind === "photo" ? "photo" : "video";
    qualityApproved = false; qualityLast = null;
    note(`Sahne ${i + 1}: ${d.file} eklendi (${d.size_mb} MB, ${d.resolution}).`, "good");
  } catch (e) { note(e.message, "bad"); }
  s.busy = ""; render(); save();
}
function previewClip(el, c) {
  const f = smallest(c.files || []); if (!f) return;
  el.innerHTML = `<video src="${esc(f.url)}" muted autoplay loop playsinline></video>`; el.removeAttribute("data-a");
}
function mediaHtml(s, i) {
  let h = s.file ? `<div class="chosen"><b>✓ Video eklendi:</b> ${esc(s.file)}</div>` : "";
  h += clipsHtml(s, i);
  h += s.links.length
    ? '<div class="links">' + s.links.map(l => `<a class="lnk" data-s="${esc(l.site)}" href="${esc(l.url)}" target="_blank" rel="noopener"><b>${esc(l.site)} ↗</b><span>${esc(l.q)}</span></a>`).join("") + "</div>"
    : '<p class="empty">"Video bul" düğmesine bas; bu sahne için öneriler ve arama linkleri gelsin.</p>';
  h += armedIdx === i
    ? `<div class="wait"><span class="busy">Sahne ${i + 1} için indirme bekleniyor…</span> <button class="quiet" data-a="disarm">İptal</button><small>Siteden videoyu indir; indirilenler klasörüne düşen ilk video otomatik olarak bu sahneye taşınır ve <b>${String(i + 1).padStart(2, "0")}_${esc(s.slug)}</b> adıyla kaydedilir.</small></div>`
    : `<div class="row"><button class="quiet" data-a="arm">⏳ Bu sahne için indirmeyi bekle</button></div>`;
  return h + `<label class="drop">⬆ Ya da videoyu buraya sürükle / tıkla<input type="file" accept="video/*" hidden></label>`;
}
function render() {
  $("#scenes").innerHTML = S.scenes.map((s, i) => `
    <article class="scene" data-i="${i}">
      <div class="when"><b>${i + 1}</b><span class="tc"></span><span class="len"></span><span class="pill ${s.file ? "ok" : ""}">${s.file ? "Video eklendi" : "Video bekliyor"}</span></div>
      <div>
        <label>Metin <textarea data-f="text" rows="3">${esc(s.text)}</textarea></label>
        ${s.en ? `<div class="scene-meaning"><b>AI görsel anlamı:</b> ${esc(s.en)}</div>` : ""}
        <div class="two">
          <label>Tetikleyici kelimeler (Türkçe, virgülle)<input data-f="triggers" value="${esc(s.triggers.join(", "))}"></label>
          <label>Arama kelimeleri (İngilizce, otomatik üretilir, istersen değiştir)<input data-f="queries" value="${esc(s.queries.join(", "))}"></label>
        </div>
        <div class="row">
          <label>Otomatik görsel önerisi · elle değiştirebilirsin
            <select data-f="visual">
              <option value="video" ${s.visual !== "diagram" ? "selected" : ""}>Hareketli stok video / fotoğraf</option>
              <option value="diagram" ${s.visual === "diagram" ? "selected" : ""}>Şema / bilgi grafiği</option>
              <option value="photo" ${s.visual === "photo" ? "selected" : ""}>Fotoğraf / arşiv görüntüsü</option>
            </select>
          </label>
          <button data-a="search">🔎 Yeniden ara</button>
          <button data-a="diagram" class="vgo">✦ Şemayı oluştur / yenile</button>
          <button data-a="retry">↻ Başka sonuçlar</button>
          <button class="quiet" data-a="merge">Alttakiyle birleştir</button>
          <button class="quiet" data-a="del">Sil</button>
          <span class="busy">${esc(s.busy)}</span>
        </div>
        ${s.visual_reason ? `<small class="hint">Otomatik öneri: ${esc(s.visual_reason)} · Dilersen yukarıdan değiştirebilirsin.</small>` : ""}
        ${s.prepError ? `<p class="note warn">⚠ ${esc(s.prepError)}</p>` : ""}
        ${aiVidHtml(s, i)}
      </div>
      <div>${mediaHtml(s, i)}</div>
    </article>`).join("") || '<div class="empty"><b>Henüz sahne yok</b>Metni yukarı yapıştırıp "Sahnelere böl" düğmesine bas.</div>';
  updateTimes(); paintOut(); paintContinuity(); fillPrompts();
}
function today() { return new Date().toLocaleDateString("sv-SE"); }
function limSite(id) { return LIM.sites[id] || (LIM.sites[id] = {used: 0, done: false}); }
function curSite() { return VSITES.find(v => !limSite(v.id).done) || null; }
async function loadLimits() {
  try { const d = await api("/api/limits"); if (d && d.sites) LIM = d; } catch (e) {}
  if (LIM.date !== today()) LIM = {date: today(), sites: {}};          // her gün limitler sıfırlanır
}
function saveLimits() { api("/api/limits", LIM).catch(() => {}); }
function aiVidHtml(s, i) {
  return `<div class="aivid">
    <div class="avh">✨ Bu sahnenin AI görseli</div>
    <p class="aihelp">Öncelik sahneye özel AI görselidir. Otomatik üretimde AI görseli sahneye eklenir; Pixabay/Pexels alternatifleri de yanında görünür. İstersen stok klibi seçerek AI görselinin yerine geçirebilirsin.</p>
    <div class="row"><button class="vgo" data-a="aigen">🎨 Bu sahne için AI görseli üret</button></div>
    <small>AI görseli otomatik eklenir. Pixabay/Pexels kartlarından birini seçersen görüntüyü istediğin zaman değiştirebilirsin.</small>
  </div>`;
}
async function aiMotion(i) {
  const s = S.scenes[i]; if (!s) return;
  if (!hasCF) { note("Önce ⚙ API anahtarları bölümünden ücretsiz Cloudflare Account ID ve Workers AI token eklemelisin.", "warn"); $("#dlg").showModal(); return; }
  try { await ensureCharacterReferences(false); } catch(e) { note(e.message, "bad"); return false; }
  s.busy = "Sahneye uygun görsel üretiliyor…"; render();
  try {
    s.aiTry = (s.aiTry || 0) + 1;
    const generated = await api("/api/aigen", {text:s.text, context:S.text, orientation:S.orientation, queries:s.queries || [], again:s.aiTry - 1, continuity_guide:S.continuityGuide || "", reference_images:(S.referenceImages || []).map(r=>r.name), character_profiles:S.characterProfiles || []});
    const clip = (generated.clips || []).find(c => c.source === "AI" && c.kind === "photo");
    if (!clip) throw new Error("AI görseli üretildi ancak sahneye uygun dosya alınamadı.");
    s.busy = "Görsel hareketli MP4'e dönüştürülüyor…"; render();
    const d = await api("/api/pick", {result:clip, index:i+1, slug:s.slug, old_file:s.file || "", words:words(s.text)});
    s.file = d.file; s.pickedId = clip.source + ":" + clip.id; s.page = clip.page; s.visual = "photo";
    qualityApproved = false; qualityLast = null;
    s.clips = [clip].concat((s.clips || []).filter(c => c.source !== "AI")).slice(0, 4).concat((s.clips || []).filter(c => c.source !== "AI").slice(4));
    note(`Sahne ${i+1}: Görsel üretildi ve ${d.file} olarak videoya dönüştürülüp eklendi (${d.size_mb} MB, ${d.resolution}).`, "good");
    s.busy = ""; render(); save();
    return true;
  } catch (e) {
    note(`Sahne ${i+1}: ${e.message}`, "bad");
    s.busy = ""; render(); save();
    return false;
  }
}
function copyNow(text, ta) {                                           // tıklama anında, senkron kopyala
  let ok = false;
  try { if (ta) { ta.focus(); ta.select(); ok = document.execCommand("copy"); ta.setSelectionRange(0, 0); } } catch (e) {}
  try { if (navigator.clipboard) navigator.clipboard.writeText(text).catch(() => {}); } catch (e) {}
  return ok;
}
function aiGo(i) {
  const s = S.scenes[i], v = curSite(); if (!s || !v) return;
  const ta = document.querySelector(`.scene[data-i="${i}"] [data-f=vprompt]`);
  const p = ((ta && ta.value) || s.vprompt || "").trim();
  if (!p) return note("Prompt henüz hazır değil, birkaç saniye bekle.", "warn");
  s.vprompt = p; copyNow(p, ta);
  window.open(v.url, "_blank", "noopener");
  note(`Prompt kopyalandı. ${v.name} sitesinde Ctrl+V ile yapıştır, üret ve indir; video otomatik Sahne ${i + 1}'e gelecek.`, "good");
  arm(i, v.id); save();
}
function aiLimit() {
  const v = curSite(); if (!v) return;
  limSite(v.id).done = true; saveLimits();
  const nx = curSite();
  note(nx ? `${v.name} limiti bitti olarak işaretlendi. Sıradaki: ${nx.name}.` : `${v.name} limiti bitti. Bugün kullanılabilir site kalmadı.`, nx ? "" : "warn");
  render();
}
function aiSite(id) {
  const st = limSite(id); st.done = !st.done; saveLimits(); render();
}
async function fetchPrompt(s, again) {
  try {
    const d = await api("/api/vprompt", {text: s.text, context: S.text, orientation: S.orientation, again: again || 0});
    s.vprompt = d.prompt; s.vpFail = false;
  } catch (e) { s.vpFail = true; }
  const j = S.scenes.indexOf(s);
  if (j >= 0) {
    const ta = document.querySelector(`.scene[data-i="${j}"] [data-f=vprompt]`);
    if (ta && document.activeElement !== ta) { ta.value = s.vprompt || ""; ta.placeholder = s.vpFail ? "Prompt otomatik yazılamadı, buraya kendin yaz." : "Prompt hazırlanıyor…"; }
  }
  save();
}
async function fillPrompts() {                                          // eksik promptları sırayla, arka planda yazar
  if (vpBusy) return; vpBusy = true;
  try {
    for (let i = 0; i < S.scenes.length; i++) {
      const s = S.scenes[i];
      if (s.vprompt || s.vpFail || !s.text.trim()) continue;
      await fetchPrompt(s, 0);
    }
  } finally { vpBusy = false; }
}
async function aiNew(i) {
  const s = S.scenes[i]; if (!s) return;
  s.vTry = (s.vTry || 0) + 1; s.vprompt = "";
  const ta = document.querySelector(`.scene[data-i="${i}"] [data-f=vprompt]`);
  if (ta) { ta.value = ""; ta.placeholder = "Yeni prompt hazırlanıyor…"; }
  await fetchPrompt(s, s.vTry);
}
function showWarns(w) { $("#warns").innerHTML = (w || []).map(x => `<li>${esc(x)}</li>`).join(""); }

async function split() {
  const text = $("#text").value.trim();
  if (!text) return note("Önce metni yaz.", "bad");
  if (S.scenes.length && !confirm("Mevcut sahneler ve video seçimleri silinecek. Devam edilsin mi?")) return;
  S.text = text; S.hook = "X"; // Kanca otomatik ve daima açık; arayüzde ayrı sekme yok.
  qualityApproved = false; qualityLast = null;
  $("#splitBtn").disabled = true; note("Metin AI ile sahnelere ayrılıyor…");
  try {
    const d = await api("/api/scenes", {text, density: S.density, hook: "X"});
    const list = Array.isArray(d.scenes) ? d.scenes : [];
    if (!list.length) throw new Error(d.message || "Sahne üretilemedi. Cloudflare/Groq anahtarını kontrol et.");
    S.scenes = list.map(norm);
    if (d.visual_bible && !S.continuityGuide) S.continuityGuide = d.visual_bible;
    showWarns([]); render(); save();
    note((d.message || (list.length + " sahne hazır.")), "good");
    try {
      if (hasCF) {
        note("Stil + karakter referansları otomatik hazırlanıyor…");
        await ensureCharacterReferences(false);
      }
    } catch (e) { note("Referans adımı atlandı: " + (e.message || e), "warn"); }
    try {
      note(hasCF ? "Her sahne için AI görsel üretiliyor (Cloudflare)…" : "Stok video aranıyor…");
      await autoPrepareScenes();
    } catch (e) {
      note("Otomatik görsel adımı: " + (e.message || e) + " — sahneler hazır, elle devam edebilirsin.", "warn");
    }
  } catch (e) { note(e.message, "bad"); }
  finally { $("#splitBtn").disabled = false; render(); save(); }
}
async function autoPrepareScenes() {
  let ready = 0, failed = 0;
  for (let i = 0; i < S.scenes.length; i++) {
    const s = S.scenes[i];
    if (s.file) { ready++; continue; }
    try {
      let aiOk = false;
      // Cloudflare varsa önce AI görsel (önceki davranış)
      if (hasCF) {
        note(`AI görsel üretiliyor: ${i + 1}/${S.scenes.length} · ${s.text.slice(0, 60)}…`);
        try { aiOk = await aiMotion(i); } catch (e) { s.prepError = e.message || String(e); }
      }
      // AI olmadıysa veya yanına stok alternatifleri
      if (!s.file || aiOk) {
        try {
          s.visual = s.visual || "video";
          note(`Stok alternatifleri: ${i + 1}/${S.scenes.length}`);
          await search(i);
          if (!s.file) {
            const choices = (s.clips || []).map((c,k)=>({c,k}))
              .filter(x=>x.c.source !== "AI")
              .sort((a,b)=>Number(b.c.score||0)-Number(a.c.score||0));
            if (choices.length && Number(choices[0].c.score || 0) >= 40) await pickClip(i, choices[0].k);
          }
        } catch (e) { /* stok zorunlu değil */ }
      }
      if (s.file) {
        ready++;
        if (!aiOk && s.pickedId && !String(s.pickedId).startsWith("AI:"))
          s.prepError = "Stok video kullanıldı; istersen AI görselini elle üretebilirsin.";
      } else {
        s.prepError = hasCF
          ? "AI ve stok eşleşmesi bulunamadı; elle seç veya AI butonuna bas."
          : "Stok bulunamadı. AI için Cloudflare anahtarı ekle veya elle video yükle.";
        failed++;
      }
    } catch (e) { s.prepError = e.message || String(e); failed++; }
    s.busy = ""; render(); save();
  }
  const pending = S.scenes.filter(s=>!s.file).length;
  note(`Hazırlık tamamlandı: ${ready}/${S.scenes.length} sahne hazır. ${pending ? pending + " sahnede manuel seçim gerekiyor." : "Tüm sahneler hazır."}`, pending ? "warn" : "good");
}

async function makeDiagram(i) {
  const s = S.scenes[i]; if (!s) return;
  if (!s.text.trim()) { note("Önce sahne metnini yaz.", "warn"); return false; }
  s.visual = "diagram"; s.busy = "Şema hazırlanıyor…"; render();
  try {
    const d = await api("/api/diagram", {
      text: s.text, index: i + 1, slug: s.slug,
      orientation: S.orientation, words: words(s.text), old_file: s.file || ""
    });
    s.file = d.file; s.pickedId = "diagram:" + (i + 1); s.page = "";
    note(`Sahne ${i + 1}: Şema videosu oluşturuldu (${d.size_mb} MB, ${d.resolution}).`, "good");
    s.busy = ""; render(); save();
    return true;
  } catch (e) {
    note("Şema oluşturulamadı: " + e.message, "bad");
    s.busy = ""; render(); save();
    return false;
  }
}
async function aiGen(i) {
  const s = S.scenes[i]; if (!s) return;
  try { await ensureCharacterReferences(false); } catch(e) { note(e.message, "bad"); return; }
  s.busy = "AI görseli üretiliyor (10-30 sn)…"; render();
  try {
    s.aiTry = (s.aiTry || 0) + 1;
    const d = await api("/api/aigen", {text: s.text, context: S.text, orientation: S.orientation, queries: s.queries || [], again: s.aiTry - 1, continuity_guide:S.continuityGuide || "", reference_images:(S.referenceImages || []).map(r=>r.name), character_profiles:S.characterProfiles || []});
    const old = s.clips || [];
    s.clips = (d.clips || []).concat(old.filter(c => c.source === "AI")).slice(0, 4).concat(old.filter(c => c.source !== "AI"));
    note("AI görseli hazır. Beğenmezsen 'AI ile üret'e tekrar bas (yeni görsel gelir). Seçince yavaş hareketli videoya çevrilir.", "");
  } catch (e) { note(e.message, "bad"); }
  s.busy = ""; render(); save();
}
async function search(i, retry) {
  const s = S.scenes[i];
  s.busy = (s.queries.length && !retry) ? "linkler hazırlanıyor…" : "arama kelimeleri üretiliyor…"; render();
  try {
    const d = await api("/api/links", {queries: retry ? [] : s.queries, orientation: S.orientation, text: s.text, context: S.text, avoid: retry ? s.queries : []});
    if ((retry || !s.queries.length) && d.queries) s.queries = d.queries;
    if (/^scene\d+$/.test(s.slug) && s.queries[0]) s.slug = s.queries[0].toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "") || s.slug;
    s.links = d.links; s.clips = (s.clips || []).filter(c => c.source === "AI").concat(d.clips || []);
    const msgs = [d.warning, d.clip_error, (d.need_key && !keyHintShown) ? "Puanlı ve önizlemeli video önerisi için ⚙ API anahtarları'ndan ücretsiz Pixabay anahtarını ekle." : ""].filter(Boolean);
    if (d.need_key) keyHintShown = true;
    note(msgs.join(" "), msgs.length ? "warn" : "");
  } catch (e) { note(e.message, "bad"); }
  s.busy = ""; render(); save();
}
async function upload(i, file) {
  const s = S.scenes[i]; if (!file) return;
  s.busy = "yükleniyor…"; render();
  try {
    const u = `/api/upload?index=${i + 1}&slug=${encodeURIComponent(s.slug)}&name=${encodeURIComponent(file.name)}&old_file=${encodeURIComponent(s.file || "")}`;
    const r = await fetch(u, {method: "POST", body: file}), d = await r.json().catch(() => ({}));
    if (!r.ok || d.error) throw new Error(d.error || "Yükleme başarısız");
    s.file = d.file; s.visual = "video"; qualityApproved = false; qualityLast = null; note(`Sahne ${i + 1}: ${d.file} eklendi (${d.size_mb} MB).`, "good");
  } catch (e) { note(e.message, "bad"); }
  s.busy = ""; render(); save();
}
async function arm(i, siteId) {
  const s = S.scenes[i]; if (!s) return;
  try {
    await api("/api/arm", {index: i + 1, slug: s.slug, old_file: s.file || ""});
    armedIdx = i; armSite = siteId || ""; clearInterval(watchTimer); watchTimer = setInterval(watchTick, 1000); render();
  } catch (e) { note(e.message, "bad"); }
}
async function disarm() {
  armedIdx = -1; armSite = ""; clearInterval(watchTimer); watchTimer = null; render();
  try { await api("/api/disarm", {}); } catch (e) {}
}
async function watchTick() {
  try {
    const d = await api("/api/watch");
    if (d.result) {
      const r = d.result, s = S.scenes[r.index - 1];
      armedIdx = -1; clearInterval(watchTimer); watchTimer = null;
      if (s) s.file = r.file;
      if (armSite) { limSite(armSite).used++; saveLimits(); armSite = ""; }
      note(`Sahne ${r.index}: "${r.original}" indirildi, otomatik ${r.file} olarak eklendi (${r.size_mb} MB).`, "good");
      render(); save();
    } else if (!d.armed && armedIdx >= 0) { armedIdx = -1; clearInterval(watchTimer); watchTimer = null; render(); }
  } catch (e) {}
}
async function uploadAudio(file) {
  if (!file) return;
  $("#audioInfo").textContent = "yükleniyor…";
  try {
    const r = await fetch("/api/audio?name=" + encodeURIComponent(file.name), {method: "POST", body: file}), d = await r.json().catch(() => ({}));
    if (!r.ok || d.error) throw new Error(d.error || "Yükleme başarısız");
    note("Ses eklendi: " + d.file, "good");
  } catch (e) { note(e.message, "bad"); }
  loadInfo();
}

let enPoll = null, enOffset = 0;
async function uploadEnglishFile(file, kind) {
  if (!file) return;
  const label = kind === "video" ? "#enVideoName" : "#enAudioName";
  $(label).textContent = "Yükleniyor: " + file.name;
  try {
    const endpoint = kind === "video" ? "/api/en/video" : "/api/en/audio";
    const r = await fetch(endpoint + "?name=" + encodeURIComponent(file.name), {method:"POST", body:file});
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.error) throw new Error(d.error || "Yükleme başarısız");
    $(label).textContent = "✓ " + d.file + " · " + d.size_mb + " MB";
    note(kind === "video" ? "Orijinal video İngilizce dönüştürme alanına eklendi." : "İngilizce ses dosyası eklendi.", "good");
  } catch (e) { $(label).textContent = "Yükleme başarısız: " + e.message; note(e.message, "bad"); }
}
async function pollEnglish() {
  try {
    const d = await api("/api/en/status"), box = $("#enLog");
    if (d.lines && d.lines.length) {
      if (box.dataset.empty) { box.textContent = ""; delete box.dataset.empty; }
      box.textContent = d.lines.join("\n"); box.scrollTop = box.scrollHeight;
    }
    if (d.video) $("#enVideoName").textContent = "✓ " + d.video;
    if (d.audio) $("#enAudioName").textContent = "✓ " + d.audio;
    if (d.status === "running") {
      $("#enStatus").textContent = "İşleniyor…";
      if (!enPoll) enPoll = setInterval(pollEnglish, 1500);
    } else {
      clearInterval(enPoll); enPoll = null;
      $("#enRun").disabled = false;
      $("#enStatus").textContent = d.status === "done" ? "Tamamlandı" : d.status === "error" ? "Hata oluştu" : "";
      if (d.status === "done" && d.has_output) {
        $("#enPreview").src = "/video/english?t=" + Date.now();
        $("#enPreviewWrap").hidden = false;
        note("İngilizce video hazır. Önizleyebilir veya indirebilirsin.", "good");
      }
      if (d.status === "error") note(d.error || "İngilizce dönüştürme başarısız oldu. Günlüğü kontrol et.", "bad");
    }
  } catch (e) { $("#enStatus").textContent = e.message; }
}
async function startEnglish() {
  $("#enRun").disabled = true; $("#enStatus").textContent = "Başlatılıyor…";
  try {
    await api("/api/en/run", {remove_turkish: $("#enClean").checked});
    $("#enLog").textContent = ""; delete $("#enLog").dataset.empty;
    enPoll = setInterval(pollEnglish, 1200); pollEnglish();
  } catch (e) { $("#enRun").disabled = false; $("#enStatus").textContent = ""; note(e.message, "bad"); }
}
function showVideo() {
  $("#pv").src = "/video/output?t=" + Date.now();
  ["#pv", "#dlVid", "#revBtn", "#prepBtn"].forEach(x => $(x).hidden = false); $("#pvEmpty").hidden = true;
  document.querySelectorAll(".newproj").forEach(b => b.hidden = false);
  outReady = true; paintOut();
}
async function tick() {
  try {
    const d = await api("/api/status?from=" + runOff), lg = $("#log"), run = d.status === "running";
    runOff = d.total;
    if (d.lines.length) {
      if (lg.dataset.empty) { lg.textContent = ""; delete lg.dataset.empty; }
      lg.textContent += d.lines.join("\n") + "\n"; lg.scrollTop = lg.scrollHeight;
      d.lines.forEach(l => { if (l.startsWith("->")) { runSteps++; $("#pct").textContent = l.replace(/^->\s*/, ""); } });
    }
    document.querySelectorAll(".gen").forEach(b => b.disabled = run); $("#stopBtn").hidden = !run;
    $("#progBar").style.width = (d.status === "done" ? 100 : run ? Math.min(92, Math.round(runSteps / 9 * 100)) : (parseFloat($("#progBar").style.width) || 0)) + "%";
    if (run && !poll) poll = setInterval(tick, 1000);
    if (!run) {
      clearInterval(poll); poll = null;
      if (d.status === "done" || (d.status === "idle" && d.has_output)) showVideo();
      if (d.status === "done") { $("#pct").textContent = "Tamamlandı"; note("Video hazır! Önizleyip indirebilirsin.", "good"); }
      if (d.status === "error") note("Üretim hata verdi. Ayrıntı için Üretim bölümündeki günlüğe bak.", "bad");
    }
  } catch (e) {}
}
async function startRun() {
  if (!S.scenes.length) return note("Önce sahne oluştur.", "bad");
  try {
    await api("/api/run", {reference_images:(S.referenceImages || []).map(r=>r.name), scenes: S.scenes.map(s => ({text: s.text, triggers: s.triggers, file: s.file})),
      opts: {model: $("#oModel").value, style: $("#oStyle").value, highlight: $("#oHl").value, margin: +$("#oMargin").value || 400, upper: $("#oUp").checked, clean: $("#oClean").checked,
      thumb: $("#tPrepend").checked, thumb_frames: Math.max(1, Math.round(+$("#tFrames").value || 1))}});
    runOff = 0; runSteps = 0; $("#log").textContent = ""; delete $("#log").dataset.empty; $("#progBar").style.width = "0%";
    note(""); $("#studio").scrollIntoView({behavior: "smooth", block: "center"}); tick();
  } catch (e) { note(e.message, "bad"); }
}
function buildTxt() {
  const L = ["# Kesin Eşleşme Dosyası (Son Sürüm)"];
  S.scenes.forEach(s => {
    if (!s.file) return;
    const t = s.triggers.map(x => x.replace(/[#:,]/g, " ").replace(/\s+/g, " ").trim()).filter(Boolean);
    if (t.length) L.push(`${s.file} : ${t.join(", ")}`);
  });
  return L.join("\n") + "\n";
}
function paintOut() {
  const lines = buildTxt().trimEnd().split("\n");
  $("#outTxt").innerHTML = lines.map(l => {
    const m = l.match(/^(.+?) : (.*)$/);
    return m ? `<span class="f">${esc(m[1])}</span> <span class="k">:</span> <span class="t">${esc(m[2])}</span>` : `<span class="c">${esc(l)}</span>`;
  }).join("\n");
  $("#stS").textContent = S.scenes.length;
  $("#stV").textContent = S.scenes.filter(s => s.file).length;
  $("#stT").textContent = S.scenes.length ? fmt(totalSec()) : "0:00";
  const done = [!!S.text.trim(), S.scenes.length > 0, S.scenes.length > 0 && S.scenes.every(s => s.file), hasAudio, outReady];
  document.querySelectorAll("#steps li").forEach((li, i) => li.classList.toggle("done", done[i]));
}
function download() {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([buildTxt()], {type: "text/plain;charset=utf-8"}));
  a.download = "eslesme.txt"; document.body.appendChild(a); a.click(); a.remove();
  const n = buildTxt().trimEnd().split("\n").length - 1;
  note(n ? `eslesme.txt indirildi (${n} satır).` : "eslesme.txt indirildi ama henüz video eklenmiş sahne yok; içinde yalnızca başlık satırı var.", n ? "good" : "warn");
}
async function run(write) {
  if (!S.scenes.length) return note("Önce sahne oluştur.", "bad");
  try {
    const d = await api(write ? "/api/write" : "/api/check", {scenes: S.scenes.map(s => ({text: s.text, triggers: s.triggers, file: s.file}))});
    showWarns(d.warnings);
    if (write) note(d.message, d.warnings.length ? "warn" : "good");
    else note(d.warnings.length ? "" : "Sorun bulunmadı.", d.warnings.length ? "" : "good");
  } catch (e) { note(e.message, "bad"); }
}

$("#scenes").addEventListener("input", e => {
  const f = e.target.dataset.f; if (!f) return;
  const s = S.scenes[+e.target.closest(".scene").dataset.i];
  if (f === "text") { s.text = e.target.value; updateTimes(); }
  else if (f === "vprompt") s.vprompt = e.target.value;
  else s[f] = e.target.value.split(",").map(x => x.trim()).filter(Boolean);
  save(); paintOut();
});
$("#scenes").addEventListener("click", e => {
  const lk = e.target.closest("a.lnk");
  if (lk) { arm(+lk.closest(".scene").dataset.i); return; }          // linke tıklanınca o sahne indirme beklemeye geçer
  const b = e.target.closest("[data-a]"); if (!b) return;
  const i = +b.closest(".scene").dataset.i, a = b.dataset.a;
  if (a === "search") search(i);
  else if (a === "retry") search(i, true);
  else if (a === "diagram") makeDiagram(i);
  else if (a === "aimotion") aiMotion(i);
  else if (a === "aigen") aiGen(i);
  else if (a === "vgo") aiGo(i);
  else if (a === "vlimit") aiLimit();
  else if (a === "vsite") aiSite(b.dataset.v);
  else if (a === "vnew") aiNew(i);
  else if (a === "pick") pickClip(i, +b.closest(".clip").dataset.k);
  else if (a === "preview") previewClip(b, S.scenes[i].clips[+b.closest(".clip").dataset.k]);
  else if (a === "arm") arm(i);
  else if (a === "disarm") disarm();
  else if (a === "del") { if (confirm("Sahne " + (i + 1) + " silinsin mi?")) { if (armedIdx >= 0) disarm(); S.scenes.splice(i, 1); render(); save(); } }
  else if (a === "merge") {
    const n = S.scenes[i + 1]; if (!n) return note("Bu son sahne.", "warn");
    if (armedIdx >= 0) disarm();
    const s = S.scenes[i]; s.text = (s.text + " " + n.text).trim(); s.vprompt = ""; s.vpFail = false;
    s.triggers = [...new Set([...s.triggers, ...n.triggers])]; S.scenes.splice(i + 1, 1); render(); save();
  }
});
const sc = $("#scenes");
sc.addEventListener("change", e => {
  const card = e.target.closest(".scene");
  if (!card) return;
  if (e.target.type === "file") upload(+card.dataset.i, e.target.files[0]);
  if (e.target.matches('[data-f="visual"]')) {
    S.scenes[+card.dataset.i].visual = e.target.value;
    S.scenes[+card.dataset.i].visual_reason = "Kullanıcı tarafından seçildi";
    S.scenes[+card.dataset.i].prepError = "";
    save();
  }
});
sc.addEventListener("input", e => {
  const card = e.target.closest(".scene");
  if (!card || !e.target.dataset.f) return;
  const i = Number(card.dataset.i), scene = S.scenes[i];
  if (!scene) return;
  if (e.target.dataset.f === "text") {
    scene.text = e.target.value;
    updateTimes(); paintOut();
  } else if (e.target.dataset.f === "triggers") scene.triggers = e.target.value.split(",").map(x=>x.trim()).filter(Boolean);
  else if (e.target.dataset.f === "queries") scene.queries = e.target.value.split(",").map(x=>x.trim()).filter(Boolean);
  save();
});
sc.addEventListener("dragover", e => { const d = e.target.closest(".drop"); if (d) { e.preventDefault(); d.classList.add("over"); } });
sc.addEventListener("dragleave", e => { const d = e.target.closest(".drop"); if (d) d.classList.remove("over"); });
sc.addEventListener("drop", e => { const d = e.target.closest(".drop"); if (d) { e.preventDefault(); upload(+d.closest(".scene").dataset.i, e.dataTransfer.files[0]); } });
$("#orient").addEventListener("change", e => { S.orientation = e.target.value; save(); });
$("#density").addEventListener("change", e => { S.density = e.target.value; save(); });
$("#text").addEventListener("input", e => { S.text = e.target.value; save(); paintOut(); });
$("#splitBtn").onclick = split;
$("#allBtn").onclick = async () => {
  if (!S.scenes.length) return note("Önce sahnelere böl.", "bad");
  $("#allBtn").disabled = true;
  for (let i = 0; i < S.scenes.length; i++) { await search(i); }
  $("#allBtn").disabled = false;
};
$("#checkBtn").onclick = () => run(false);
$("#writeBtn").onclick = () => run(true);
document.querySelectorAll(".dlb").forEach(b => b.onclick = download);
document.querySelectorAll(".gen").forEach(b => b.onclick = startRun);
async function newProject() {
  if (!confirm("Yeni proje başlatılsın mı?\n\nMetin, sahneler, indirilen videolar, ses ve kapak silinir; sayfa sıfırlanır.\nÜretilen video 'ciktilar' klasöründe kalır.")) return;
  clearTimeout(saveTimer);                       // bekleyen otomatik kayıt silinen projeyi geri yazmasın
  try { await api("/api/new_project", {}); } catch (e) { return note(e.message, "bad"); }
  location.reload();                             // sayfa tamamen yenilenir, her şey boş başlar
}
document.querySelectorAll(".newproj").forEach(b => b.onclick = newProject);
$("#stopBtn").onclick = () => api("/api/stop", {}).catch(e => note(e.message, "bad"));
$("#revBtn").onclick = () => api("/api/reveal", {}).catch(e => note(e.message, "bad"));
$("#audIn").onchange = e => uploadAudio(e.target.files[0]);
$("#audDrop").addEventListener("dragover", e => { e.preventDefault(); e.currentTarget.classList.add("over"); });
$("#audDrop").addEventListener("dragleave", e => e.currentTarget.classList.remove("over"));
$("#audDrop").addEventListener("drop", e => { e.preventDefault(); e.currentTarget.classList.remove("over"); uploadAudio(e.dataTransfer.files[0]); });
$("#copyBtn").onclick = () => navigator.clipboard.writeText(buildTxt()).then(() => note("Panoya kopyalandı.", "good"), () => note("Kopyalanamadı.", "bad"));

async function loadInfo() {
  try {
    const d = await api("/api/info"); audioSec = d.audio_seconds || 0; hasAudio = !!d.audio_name;
    $("#audioInfo").textContent = d.audio_name ? `✓ ${d.audio_name}${audioSec ? " · " + fmt(audioSec) : ""}` : "Henüz ses dosyası yok.";
    render();
    const k = d.keys;
    hasCF = !!(k.cf_token && k.cf_account);
    $("#keys").innerHTML = (k.anthropic ? '<span class="chip ok"><i></i>Claude bağlı</span>' : "")
      + (k.groq ? '<span class="chip ok"><i></i>Groq bağlı</span>' : "")
      + (hasCF ? '<span class="chip ok"><i></i>Cloudflare bağlı</span>' : "")
      + (k.pixabay ? '<span class="chip ok"><i></i>Pixabay bağlı</span>' : "")
      + "";
    thumbInfo = d.thumb || thumbInfo; paintThumb();
  } catch (e) {}
}
$("#keysBtn").onclick = () => $("#dlg").showModal();
$("#kCancel").onclick = () => $("#dlg").close();
$("#kSave").onclick = async () => {
  try {
    await api("/api/config", {anthropic: $("#kAnthropic").value, pixabay: $("#kPixabay").value, groq: $("#kGroq").value, cf_account: $("#kCfAcc").value, cf_token: $("#kCfTok").value});
    ["#kAnthropic", "#kPixabay", "#kGroq", "#kCfAcc", "#kCfTok"].forEach(i => $(i).value = ""); $("#dlg").close(); loadInfo();
  } catch (e) { note(e.message, "bad"); }
};
function paintThumb() {
  const t = thumbInfo, img = $("#tImg"), src = t.final ? "/thumb/final?v=" + t.final : "";
  img.hidden = !src; $("#tEmpty").hidden = !!src; $("#tClear").hidden = !t.final;
  if (src && img.dataset.src !== src) { img.src = src; img.dataset.src = src; }
  $("#tInfo").textContent = t.final ? "✓ Kapak hazır (inputs/thumbnail.png). Video üretilince en başa eklenir." : "Henüz kapak yok.";
}
async function thumbUpload(file) {
  if (!file) return; $("#tBusy").className = "busy"; $("#tBusy").textContent = "yükleniyor…";
  try {
    const r = await fetch("/api/thumb_upload?name=" + encodeURIComponent(file.name), {method: "POST", body: file}), d = await r.json().catch(() => ({}));
    if (!r.ok || d.error) throw new Error(d.error || "Yükleme başarısız");
    const i = await api("/api/info"); thumbInfo = i.thumb; paintThumb(); $("#tBusy").textContent = "";
    note("Kapak eklendi. Video üretilince en başa gelecek.", "good");
  } catch (e) { $("#tBusy").className = "busy err"; $("#tBusy").textContent = "⚠ " + e.message; note(e.message, "bad"); }
}
$("#tUp").onchange = e => thumbUpload(e.target.files[0]);
$("#tDrop").addEventListener("dragover", e => { e.preventDefault(); e.currentTarget.classList.add("over"); });
$("#tDrop").addEventListener("dragleave", e => e.currentTarget.classList.remove("over"));
$("#tDrop").addEventListener("drop", e => { e.preventDefault(); e.currentTarget.classList.remove("over"); thumbUpload(e.dataTransfer.files[0]); });
$("#tClear").onclick = async () => {
  try { await api("/api/thumb_clear", {}); const i = await api("/api/info"); thumbInfo = i.thumb; paintThumb(); } catch (e) { note(e.message, "bad"); }
};
$("#prepBtn").onclick = async () => {
  try { note("Kapak videonun başına ekleniyor…"); await api("/api/prepend", {frames: Math.max(1, Math.round(+$("#tFrames").value || 1))}); showVideo(); note("Kapak videonun başına eklendi.", "good"); }
  catch (e) { note(e.message, "bad"); }
};

function paintQuality(d) {
  qualityLast = d; qualityApproved = true;
  const totalScore = Math.max(0, Math.min(100, Number(d.score || 0)));
  const qs = $("#qualityScore"), qd = $("#qualityDonut"), qst = $("#qualityStatus");
  if (qs) qs.textContent = `%${Math.round(totalScore)}`;
  if (qd) qd.style.background = `conic-gradient(var(--good) ${totalScore * 3.6}deg, var(--line) 0deg)`;
  if (qst) { qst.textContent = "Kalite paneli kapatıldı"; qst.style.color = "var(--mute)"; }
  const labels = {text_match:"Metin uyumu",visual_match:"Görsel uygunluğu",sequence:"Sahne sırası",continuity:"Karakter / stil devamlılığı",technical:"Teknik kalite",coverage:"Metin kapsamı"};
  const weights = {text_match:35,visual_match:25,sequence:15,continuity:10,technical:10,coverage:5};
  $("#qualityMetrics").innerHTML = Object.entries(weights).map(([k,w]) => {
    const v = Math.max(0, Math.min(100, Number((d.scores || {})[k] || 0)));
    const color = v >= 85 ? "var(--good)" : "var(--bad)";
    return `<div class="quality-metric"><div class="quality-metric-head"><span>${esc(labels[k])}</span><b style="color:${color}">%${Math.round(v)}</b></div><small>Genel puana etkisi: %${w}</small><div class="quality-track"><div class="quality-fill" style="width:${v}%;background:${color}"></div></div></div>`;
  }).join("");
  const scenes = (d.scene_scores || []).map(x => {
    const v = Math.max(0, Math.min(100, Number(x.score || 0))); const color = v >= 85 ? "var(--good)" : "var(--bad)";
    return `<div class="quality-scene"><div class="quality-scene-top"><span><b>Sahne ${x.n}</b></span><b style="color:${color}">%${Math.round(v)}</b></div><div class="quality-track"><div class="quality-fill" style="width:${v}%;background:${color}"></div></div><div>${esc(x.issue || "Sorun yok")}${x.fix ? `<br><small>Öneri: ${esc(x.fix)}</small>` : ""}</div></div>`;
  }).join("");
  const strengths = (d.strengths || []).map(x => `<li>${esc(x)}</li>`).join("");
  const qr = $("#qualityResult");
  if (qr) qr.innerHTML = `<b>${esc(d.message || "")}</b>${d.verdict ? `<p>${esc(d.verdict)}</p>` : ""}${scenes ? `<b>Sahne bazında puanlar</b><div class="quality-scenes">${scenes}</div>` : ""}${strengths ? `<b>Güçlü yönler</b><ul>${strengths}</ul>` : ""}`;
  document.querySelectorAll(".gen").forEach(b => b.disabled = false);
  render();
}
async function runQualityAnalysis() {
  note("Kalite paneli kapatıldı; üretim için zorunlu değil.", "");
}
if ($("#qualityBtn")) $("#qualityBtn").onclick = runQualityAnalysis;
$("#continuityGuide").addEventListener("input", e => { S.continuityGuide = e.target.value; save(); });
$("#refUpload").addEventListener("change", e => [...e.target.files].forEach(uploadReference));
$("#refGenerate").addEventListener("click", generateCharacterReference);
$("#refList").addEventListener("click", e => {
  const b = e.target.closest("[data-ref-remove]"); if (!b) return;
  const i = Number(b.dataset.refRemove); const removed = S.referenceImages.splice(i, 1)[0];
  if (removed && removed.name) S.characterProfiles = (S.characterProfiles || []).filter(c => c.imageName !== removed.name);
  paintContinuity(); save();
});

$("#enVideo").addEventListener("change", e => uploadEnglishFile(e.target.files[0], "video"));
$("#enAudio").addEventListener("change", e => uploadEnglishFile(e.target.files[0], "audio"));
$("#enRun").addEventListener("click", startEnglish);

setInterval(() => fetch("/api/ping").catch(() => {}), 4000);
addEventListener("pagehide", () => navigator.sendBeacon("/api/bye"));
(async function init() {
  await loadLimits();
  await loadInfo();
  renderPerfHistory();
  try {
    const d = await api("/api/load");
    if (d && d.scenes) { S = {text: d.text || "", orientation: d.orientation || "portrait", density: d.density || "sik", hook: d.hook || "X", scenes: d.scenes.map(norm), continuityGuide: d.continuityGuide || "", referenceImages: Array.isArray(d.referenceImages) ? d.referenceImages.slice(0,4) : [], characterProfiles: Array.isArray(d.characterProfiles) ? d.characterProfiles.slice(0,4) : [], characterAnalysisScript: d.characterAnalysisScript || ""}; }
  } catch (e) {}
  $("#text").value = S.text; $("#orient").value = S.orientation; $("#density").value = S.density; S.hook = "X"; render(); tick();
})();
</script>
</body>
</html>
"""


STATE = {"last_ping": time.time(), "bye_at": 0.0}


def find_app_browser():
    """Adres çubuğu olmayan 'uygulama penceresi' için Edge / Chrome / Brave bulur."""
    c = []
    if sys.platform.startswith("win"):
        for base in filter(None, (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"),
                                  os.environ.get("LOCALAPPDATA"))):
            b = Path(base)
            c += [b / "Google/Chrome/Application/chrome.exe"]
        for base in filter(None, (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"),
                                  os.environ.get("LOCALAPPDATA"))):
            b = Path(base)
            c += [b / "BraveSoftware/Brave-Browser/Application/brave.exe", b / "Microsoft/Edge/Application/msedge.exe"]
    elif sys.platform == "darwin":
        c += [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
              Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")]
    for n in ("google-chrome", "chrome", "chromium", "chromium-browser", "microsoft-edge"):
        w = shutil.which(n)
        if w:
            c.append(Path(w))
    return next((str(p) for p in c if p.is_file()), None)


def open_window(url: str) -> None:
    exe = find_app_browser()
    if exe:
        try:
            subprocess.Popen([exe, f"--app={url}", "--window-size=1360,900", "--no-first-run",
                              "--no-default-browser-check", f"--user-data-dir={ROOT / ('.planner_profile_' + Path(exe).stem)}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except Exception:
            pass
    webbrowser.open(url)


def running_port():
    for p in range(PORT, PORT + 10):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{p}/api/ping", timeout=0.6) as r:
                if json.loads(r.read().decode("utf-8")).get("app") == "sahne-planlayici":
                    return p
        except Exception:
            continue
    return None


def make_shortcut() -> str:
    """Masaüstüne ve Başlat menüsüne ikonlu kısayol koyar (Windows)."""
    if not sys.platform.startswith("win"):
        return "Kısayol oluşturma şimdilik yalnızca Windows'ta destekleniyor."
    ico = ROOT / "planner.ico"
    ico.write_bytes(struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(ICON_PNG), 22)
                    + ICON_PNG)
    pw = Path(sys.executable).with_name("pythonw.exe")
    exe = pw if pw.is_file() else Path(sys.executable)
    q = lambda p: str(p).replace("'", "''")
    ps = ("foreach($d in @([Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('Programs'))){"
          "$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Sahne Planlayici.lnk'));"
          f"$s.TargetPath='{q(exe)}';$s.Arguments='\"{q(Path(__file__).resolve())}\"';"
          f"$s.WorkingDirectory='{q(ROOT)}';$s.IconLocation='{q(ico)},0';$s.Description='Sahne Planlayici';$s.Save()}}")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], check=True)
    return "Tamam: masaüstüne ve Başlat menüsüne 'Sahne Planlayici' kısayolu eklendi."


def watchdog(server) -> None:
    """Uygulama penceresi kapanınca sunucuyu da kapatır (pencere bir 'bye' ve düzenli 'ping' yollar)."""
    last_tick = time.time()
    while True:
        time.sleep(1)
        now = time.time()
        if now - last_tick > 10:            # bilgisayar uyku modundaydı
            STATE["last_ping"] = now
        last_tick = now
        if (STATE["bye_at"] and now > STATE["bye_at"]) or now - STATE["last_ping"] > 180:
            server.shutdown()
            return


def log_crash() -> None:
    try:
        import traceback
        (ROOT / "planner.log").write_text(traceback.format_exc(), encoding="utf-8")
    except Exception:
        pass


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if "--kisayol" in sys.argv:
        try:
            print(make_shortcut())
        except Exception as e:
            print("Kısayol oluşturulamadı:", e)
        return
    old = running_port()                     # zaten açıksa yeni pencere aç, ikinci sunucu başlatma
    if old:
        open_window(f"http://127.0.0.1:{old}")
        return
    port, server = PORT, None
    for p in range(PORT, PORT + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if server is None:
        print("Uygun port bulunamadı.")
        sys.exit(1)
    url = f"http://127.0.0.1:{port}"
    print(f"Sahne Planlayıcı çalışıyor: {url}")
    STATE["last_ping"] = time.time()
    threading.Thread(target=watchdog, args=(server,), daemon=True).start()
    threading.Thread(target=watch_loop, daemon=True).start()
    threading.Timer(0.6, open_window, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
    p = RUN.get("proc")
    if p and p.poll() is None:
        p.terminate()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log_crash()
        raise
