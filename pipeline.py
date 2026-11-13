#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pipeline.py - Tam otomatik Shorts/Reels video üretim hattı (API'siz)

Girdi:
    inputs/videos/*.mp4   -> kendi stok videolarınız
    inputs/audio.mp3      -> seslendirme (.wav/.m4a/.aac/.ogg/.flac da olur)
    inputs/font.ttf       -> (opsiyonel) altyazı fontu

Çıktı:
    output_video.mp4      -> 1080x1920, 30 FPS, altyazısı gömülü video

Akış:
    1) faster-whisper ile Türkçe konuşma analizi (kelime bazlı zaman damgası)
    2) Cümleler videolarla eşleştirilir (sirali/kelime/serbest), 2-4 sn'lik kesimler 1080x1920'ye doldurulur
    3) Kesimler uç uca eklenir (video kısaysa başa sarılır / loop)
    4) Kelime vurgulu ASS altyazı üretilir ve FFmpeg ile videoya gömülür
"""

import argparse
import difflib
import hashlib
import json
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# Sabitler
# --------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
INPUT_DIR = ROOT / "inputs"
VIDEO_DIR = INPUT_DIR / "videos"
FONT_PATH = INPUT_DIR / "font.ttf"
MAP_FILE = INPUT_DIR / "eslesme.txt"
TEMP_DIR = ROOT / "_temp"
CACHE_DIR = ROOT / "_cache"
DEFAULT_OUTPUT = ROOT / "output_video.mp4"

WIDTH, HEIGHT, FPS = 1080, 1920, 30

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
SCRIPT_NAMES = ["metin.txt", "script.txt", "senaryo.txt"]
AUDIO_NAMES = ["audio.mp3", "audio.wav", "audio.m4a", "audio.aac", "audio.ogg", "audio.flac"]

# ASS renkleri BGR formatındadır
HIGHLIGHT_COLORS = {
    "sari": "00FFFF",   # sarı
    "cyan": "FFFF00",   # camgöbeği
}


# --------------------------------------------------------------------------
# Yardımcılar
# --------------------------------------------------------------------------
def log(msg: str) -> None:
    print(msg, flush=True)


def fail(msg: str, code: int = 1) -> None:
    print(f"\n[HATA] {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def run(cmd, cwd=None) -> subprocess.CompletedProcess:
    """Komutu çalıştırır; hata olursa stderr'in sonunu gösterip çıkar."""
    try:
        return subprocess.run(
            [str(c) for c in cmd],
            cwd=str(cwd) if cwd else None,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.CalledProcessError as e:
        tail = "\n".join((e.stderr or "").strip().splitlines()[-15:])
        fail(f"Komut başarısız oldu: {' '.join(str(c) for c in cmd[:3])} ...\n{tail}")
    except FileNotFoundError:
        fail(f"Komut bulunamadı: {cmd[0]}")


def check_tools() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            fail(
                f"'{tool}' bulunamadı. FFmpeg'i kurup PATH'e eklediğinizden emin olun.\n"
                "  Windows: winget install Gyan.FFmpeg\n"
                "  macOS  : brew install ffmpeg\n"
                "  Linux  : sudo apt install ffmpeg"
            )


def probe_duration(path: Path) -> float:
    """Medya dosyasının süresini saniye olarak döndürür (hata olursa 0)."""
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=True,
        )
        return float(res.stdout.strip())
    except Exception:
        return 0.0


def prepare_folders() -> None:
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    INPUT_DIR.mkdir(parents=True, exist_ok=True)


AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".wma", ".opus"}


def find_audio() -> Path:
    # 1) Önce bilinen adlar
    for name in AUDIO_NAMES:
        p = INPUT_DIR / name
        if p.is_file():
            return p

    # 2) inputs içindeki herhangi bir ses dosyası (adı önemli değil)
    candidates = sorted(p for p in INPUT_DIR.iterdir()
                        if p.is_file() and p.suffix.lower() in AUDIO_EXTS)
    if candidates:
        if len(candidates) > 1:
            log(f"   (Birden fazla ses dosyası var; ilki kullanılıyor: {candidates[0].name})")
        else:
            log(f"   (Ses dosyası adı 'audio' değil; bulundu: {candidates[0].name})")
        return candidates[0]

    # 3) Bulunamadı: kullanıcıya ne gördüğümüzü göster
    listing = sorted(p.name for p in INPUT_DIR.iterdir())
    shown = "\n".join(f"    - {n}" for n in listing) if listing else "    (klasör boş)"
    fail(
        "Ses dosyası bulunamadı.\n"
        f"  Aranan klasör : {INPUT_DIR}\n"
        f"  Klasörde şunlar var:\n{shown}\n"
        "  Ses dosyasını bu klasörün İÇİNE koyun (videos alt klasörüne değil).\n"
        "  Dosya adı 'audio.mp3' görünüyorsa gerçek adı 'audio.mp3.mp3' olabilir; "
        "Windows'ta Görünüm > Dosya adı uzantıları seçeneğini açıp kontrol edin."
    )


def find_videos() -> list:
    def natural_key(path: Path):
        return [int(t) if t.isdigit() else t.lower()
                for t in re.split(r"(\d+)", path.name)]

    vids = sorted((p for p in VIDEO_DIR.iterdir()
                   if p.is_file() and p.suffix.lower() in VIDEO_EXTS),
                  key=natural_key)
    if not vids:
        fail(f"Video bulunamadı. Lütfen MP4 dosyalarını şuraya koyun: {VIDEO_DIR}")
    return vids


def tr_upper(text: str) -> str:
    """Türkçe'ye uygun büyük harf (i -> İ, ı -> I)."""
    return text.replace("i", "İ").replace("ı", "I").upper()


def tr_fold(text: str) -> str:
    """Küçük harfe çevirir, Türkçe karakterleri sadeleştirir (ş->s, ğ->g, ı->i ...)."""
    t = text.replace("İ", "i").replace("I", "ı").lower()
    return t.translate(str.maketrans("çğışöü", "cgisou"))


def clean_token(text: str) -> str:
    """Kelimenin başındaki/sonundaki noktalama ve tırnakları atar (iç kısım: Türkiye'nin kalır)."""
    return re.sub(r"^[^\w]+|[^\w]+$", "", text or "")


# --------------------------------------------------------------------------
# 1) Ses analizi (faster-whisper)
# --------------------------------------------------------------------------
def transcribe_words(audio_path: Path, model_size: str, device: str, prompt: str = None) -> list:
    """Kelime bazlı zaman damgalarını [{'text','start','end'}, ...] olarak döndürür."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        fail("faster-whisper kurulu değil. Çalıştırın: pip install -r requirements.txt")

    def _load(dev: str, compute: str):
        return WhisperModel(model_size, device=dev, compute_type=compute)

    try:
        model = _load(device, "auto")
    except Exception as e:
        log(f"   (Uyarı: '{device}' cihazı başlatılamadı: {e}. CPU/int8'e geçiliyor.)")
        model = _load("cpu", "int8")

    def _run(m):
        segs, info = m.transcribe(
            str(audio_path),
            language="tr",
            word_timestamps=True,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 300},
            beam_size=5,
            best_of=5,
            temperature=[0.0, 0.2, 0.4],
            condition_on_previous_text=False,   # tekrar/halüsinasyon zincirini engeller
            compression_ratio_threshold=2.4,
            initial_prompt=prompt or None,
        )
        out = []
        for seg in segs:  # jeneratör; burada gerçekten çözümleme yapılır
            for w in (seg.words or []):
                txt = re.sub(r"[{}\\]", "", (w.word or "")).strip()
                if txt and re.search(r"\w", txt):   # sadece noktalamadan oluşan "kelimeleri" at
                    out.append({"text": txt, "start": float(w.start), "end": float(w.end)})
        return out

    try:
        words = _run(model)
    except Exception as e:
        # Örn. CUDA kütüphaneleri eksikse çalışma anında hata verebilir
        log(f"   (Uyarı: çözümleme hatası: {e}. CPU/int8 ile yeniden deneniyor.)")
        words = _run(_load("cpu", "int8"))

    if not words:
        fail("Seste konuşma algılanamadı (kelime bulunamadı). Ses dosyasını kontrol edin.")
    return words


# --------------------------------------------------------------------------
# Transkript önbelleği: aynı ses + aynı model + aynı prompt için Whisper'ı
# tekrar çalıştırmaz. Anahtar, ses dosyasının içerik hash'inden türetilir;
# ses değişirse (örn. yeniden seslendirme) anahtar da değişir ve önbellek
# otomatik olarak atlanır.
# --------------------------------------------------------------------------
def audio_cache_key(audio_path: Path, model_size: str, device: str, prompt: str) -> str:
    h = hashlib.sha256()
    with open(audio_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    h.update(f"|{model_size}|{device}|{prompt or ''}".encode("utf-8"))
    return h.hexdigest()[:32]


def load_cached_transcript(key: str) -> list:
    p = CACHE_DIR / f"{key}.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list) and data and all(
            isinstance(w, dict) and "text" in w and "start" in w and "end" in w for w in data
        ):
            return data
    except Exception:
        pass
    return None


def save_cached_transcript(key: str, words: list) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_DIR / f"{key}.json.tmp"
        tmp.write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CACHE_DIR / f"{key}.json")
    except OSError:
        pass   # önbelleğe yazılamazsa sessizce geç; üretim yine de tamamlanır


def transcribe_words_cached(audio_path: Path, model_size: str, device: str,
                            prompt: str = None, use_cache: bool = True) -> list:
    """transcribe_words'ü önbellekli çağırır. Önbellek isabet ederse Whisper hiç çalışmaz."""
    key = audio_cache_key(audio_path, model_size, device, prompt) if use_cache else None
    if key:
        cached = load_cached_transcript(key)
        if cached is not None:
            log(f"   Önbellekten okundu: {len(cached)} kelime "
                "(ses/model/prompt değişmedi, Whisper yeniden çalıştırılmadı).")
            return cached
    words = transcribe_words(audio_path, model_size, device, prompt)
    if key:
        save_cached_transcript(key, words)
    return words


def collapse_repeats(words: list, max_run: int = 3) -> list:
    """Whisper'ın takılıp aynı kelimeyi defalarca yazdığı durumları (halüsinasyon) temizler."""
    out, run_len = [], 0
    for w in words:
        if out and tr_fold(clean_token(w["text"])) == tr_fold(clean_token(out[-1]["text"])):
            run_len += 1
            if run_len >= max_run:
                continue
        else:
            run_len = 0
        out.append(w)
    return out


def sanitize_words(words: list, total: float) -> list:
    """Zaman damgalarını düzeltir: sıfır/negatif süre ve üst üste binmeleri giderir."""
    n = len(words)
    for i, w in enumerate(words):
        w["start"] = min(max(w["start"], 0.0), total)
        if i:   # iki kelime asla aynı anda başlamasın
            w["start"] = max(w["start"], words[i - 1]["start"] + 0.06)
    for i, w in enumerate(words):
        w["end"] = max(w["end"], w["start"] + 0.06)
        if i + 1 < n:
            nxt = words[i + 1]["start"]
            if w["end"] > nxt and nxt >= w["start"] + 0.06:
                w["end"] = nxt
        w["end"] = min(w["end"], max(total, w["start"] + 0.06))
    return words


def load_script() -> str:
    """inputs/metin.txt (veya script.txt / senaryo.txt) varsa içeriğini döndürür."""
    for name in SCRIPT_NAMES:
        p = INPUT_DIR / name
        if p.is_file():
            try:
                return p.read_text(encoding="utf-8-sig")
            except UnicodeDecodeError:
                return p.read_text(encoding="cp1254", errors="replace")
    return ""


def _spread(tokens: list, t0: float, t1: float) -> list:
    """Kelimeleri [t0, t1] aralığına harf sayısıyla orantılı dağıtır."""
    weights = [max(len(re.sub(r"\W", "", t)), 1) for t in tokens]
    tot, cur, res = sum(weights), t0, []
    for t, wt in zip(tokens, weights):
        d = (t1 - t0) * wt / tot
        res.append({"text": t, "start": cur, "end": cur + d})
        cur += d
    return res


def align_script(words: list, script_text: str) -> list:
    """
    Whisper'ın duyduğu kelimeleri, elinizdeki doğru metinle değiştirir; zamanlar
    Whisper'dan gelir. Böylece yazım/anlam hataları kalmaz.
    """
    sw = [re.sub(r"[{}\\]", "", t) for t in script_text.split()]
    sw = [t for t in sw if re.search(r"\w", t)]
    if not sw or not words:
        return words

    def key(t):
        return re.sub(r"[^\w]", "", tr_fold(t))

    wk = [key(w["text"]) for w in words]
    sk = [key(t) for t in sw]
    sm = difflib.SequenceMatcher(None, wk, sk, autojunk=False)
    out = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal" or (op == "replace" and i2 - i1 == j2 - j1):
            for k in range(i2 - i1):
                out.append({"text": sw[j1 + k], "start": words[i1 + k]["start"],
                            "end": words[i1 + k]["end"]})
        elif op == "replace":
            out += _spread(sw[j1:j2], words[i1]["start"], words[i2 - 1]["end"])
        elif op == "insert":
            n = j2 - j1
            lo = words[i1 - 1]["end"] if i1 > 0 else max(0.0, words[0]["start"] - 0.35 * n)
            hi = words[i1]["start"] if i1 < len(words) else lo + 0.35 * n
            if hi - lo < 0.08 * n:
                hi = lo + 0.08 * n
            out += _spread(sw[j1:j2], lo, hi)
        # "delete": metinde olmayan, Whisper'ın uydurduğu kelime -> atılır
    return out


# --------------------------------------------------------------------------
# 2) Klip planlama ve kırpma
# --------------------------------------------------------------------------
def build_sentences(words: list, total: float, min_scene: float) -> list:
    """
    Kelimeleri cümlelere böler. [(başlangıç, bitiş, [kelimeler]), ...] döndürür.
    Sahneler 0 -> toplam süre arasını boşluksuz kaplar; sınır, cümleler
    arasındaki sessizliğin ortasıdır. Çok kısa cümleler komşusuyla birleştirilir.
    """
    sents, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if (nxt is None or re.search(r"[.!?…]$", w["text"])
                or nxt["start"] - w["end"] > 1.0):
            sents.append(cur)
            cur = []

    bounds = [0.0]
    for a, b in zip(sents, sents[1:]):
        mid = (a[-1]["end"] + b[0]["start"]) / 2
        bounds.append(min(max(mid, bounds[-1]), total))
    bounds.append(total)

    scenes = [[bounds[i], bounds[i + 1], sents[i][:]] for i in range(len(sents))]

    merged = []
    for sc in scenes:
        if merged and (merged[-1][1] - merged[-1][0]) < min_scene:
            merged[-1][1] = sc[1]
            merged[-1][2] += sc[2]
        else:
            merged.append(sc)
    if len(merged) > 1 and (merged[-1][1] - merged[-1][0]) < min_scene:
        last = merged.pop()
        merged[-1][1] = last[1]
        merged[-1][2] += last[2]
    return [(a, b, w) for a, b, w in merged]


def assign_sequential(sentences: list, videos: list, min_scene: float) -> list:
    """
    SIRALI mod: videolar dosya sırasıyla hikaye akışına dağıtılır.
    - Cümle sayısı >= video sayısı: ardışık cümleler aynı videoyu paylaşır.
    - Cümle sayısı < video sayısı: her cümleye sırayla birden fazla video düşer
      (her video en az min_scene saniye görünecek şekilde).
    """
    S, V = len(sentences), len(videos)
    out = []
    for i, (t0, t1, words) in enumerate(sentences):
        lo = i * V // S
        hi = max((i + 1) * V // S, lo + 1)
        vids = videos[lo:hi]
        cap = max(1, int((t1 - t0) / max(min_scene, 0.5)))
        if len(vids) > cap:  # çok kısa parçalara bölme: eşit aralıklı seç
            vids = [vids[int(j * len(vids) / cap)] for j in range(cap)]
        n = len(vids)
        step = (t1 - t0) / n
        for k, v in enumerate(vids):
            a_ = t0 + k * step
            b_ = t1 if k == n - 1 else t0 + (k + 1) * step
            out.append((a_, b_, [v], words))
    return out


def load_mapping(videos: list) -> dict:
    """
    inputs/eslesme.txt okur. Satır biçimi:
        video_adi.mp4 : kelime1, kelime2, iki kelimelik ifade
    Dönüş: {video_yolu: [sadeleştirilmiş anahtar kelimeler]}
    """
    mapping = {}
    if not MAP_FILE.exists():
        return mapping
    try:
        text = MAP_FILE.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = MAP_FILE.read_text(encoding="cp1254", errors="replace")

    by_name = {v.name.lower(): v for v in videos}
    by_stem = {v.stem.lower(): v for v in videos}
    for ln, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            log(f"   (Uyarı: eslesme.txt satır {ln} anlaşılamadı, ':' yok: {raw.strip()})")
            continue
        name, kws = line.split(":", 1)
        key = name.strip().lower()
        v = by_name.get(key) or by_stem.get(key)
        if v is None:
            log(f"   (Uyarı: eslesme.txt satır {ln}: '{name.strip()}' adlı video bulunamadı)")
            continue
        tags = []
        for k in kws.split(","):
            t = re.sub(r"\s+", " ", re.sub(r"[^\w\s]|_", "", tr_fold(k.strip()))).strip()
            if t:
                tags.append(t)
        mapping.setdefault(v, []).extend(tags)
    return mapping


def video_tags(path: Path, mapping: dict = None) -> list:
    """Anahtar kelimeler: eslesme.txt'dekiler + dosya adındakiler ('03_yemek.mp4' -> 'yemek')."""
    stem = re.sub(r"^\d+[\s_\-.]*", "", path.stem)
    tags = []
    for t in re.split(r"[\s_\-.]+", stem):
        t = re.sub(r"\d+$", "", tr_fold(t))
        if len(t) >= 3:
            tags.append(t)
    if mapping and path in mapping:
        tags.extend(mapping[path])
    return tags


def stem_match(word: str, tag: str) -> bool:
    """
    Türkçe ekleri tolere eden kaba eşleşme: 'yemeği' ~ 'yemek', 'şehirde' ~ 'şehir'.
    Kelime, anahtar kelimenin kökü (son 1-2 harfi atılmış hali) ile başlamalı.
    """
    L = len(tag)
    n = L if L <= 3 else (L - 1 if L <= 6 else L - 2)
    if len(word) >= n and word.startswith(tag[:n]):
        return True
    # Ünlü düşmesi: beyin -> beyni/beynin, burun -> burnu, şehir -> şehri ...
    if L >= 4 and tag[-2] in "iu" and tag[-1] not in "aeiou":
        return word.startswith(tag[:-2] + tag[-1])
    return False


def tag_hits(norm: list, i: int, tag: str) -> bool:
    """norm[i] konumunda tag (tek kelime veya ardışık kelime ifadesi) geçiyor mu?"""
    parts = tag.split()
    if not parts or i + len(parts) > len(norm):
        return False
    return all(stem_match(norm[i + k], part) for k, part in enumerate(parts))


def tag_score(norm: list, i: int, tag: str) -> int:
    """0: eşleşme yok, 1: kök eşleşmesi (ek tolere edilir), 2: birebir aynı kelime."""
    if not tag_hits(norm, i, tag):
        return 0
    parts = tag.split()
    return 2 if all(norm[i + k] == part for k, part in enumerate(parts)) else 1


def assign_keywords(words: list, videos: list, total: float, min_scene: float,
                    mapping: dict) -> list:
    """
    KELİME modu (kelime bazlı): bir anahtar kelime söylendiği anda ilgili video
    ekrana gelir ve bir sonraki anahtar kelimeye kadar kalır.
    Anahtar kelimeler eslesme.txt'den ve dosya adlarından gelir.
    """
    tags = {v: video_tags(v, mapping) for v in videos}
    general = [v for v in videos if any(t in ("genel", "general", "diger") for t in tags[v])]
    untagged = [v for v in videos if not tags[v]]
    pool = general or untagged

    norm = [tr_fold(re.sub(r"[\W_]+", "", w["text"])) for w in words]
    LEAD = 0.15  # görüntü kelimeden azıcık önce değişsin

    accepted = []  # [kesim_zamanı, [videolar], tetikleyen_kelime]
    for i, w in enumerate(words):
        scores = {}
        for v in videos:
            if v in general:
                continue
            sc = max((tag_score(norm, i, t) for t in tags[v]), default=0)
            if sc:
                scores[v] = sc
        if not scores:
            continue
        best = max(scores.values())     # birebir eşleşme, kök eşleşmesinden önce gelir
        hits = [v for v in videos if scores.get(v) == best]
        t_cut = max(0.0, w["start"] - LEAD)
        if accepted and accepted[-1][1] == hits:
            continue                                  # aynı video zaten ekranda
        if accepted and (t_cut - accepted[-1][0]) < min_scene:
            continue                                  # çok yakın, flaş yapmasın
        accepted.append([t_cut, hits, w["text"]])

    if not accepted:
        log("   (Uyarı: hiçbir anahtar kelime eşleşmedi! inputs/eslesme.txt dosyasına "
            "Türkçe kelimeleri yazın. Videolar sırayla kullanılacak.)")
        return [(0.0, total, pool or videos, [])]

    scenes = []
    if accepted[0][0] > 0.3 and pool:
        scenes.append((0.0, accepted[0][0], [pool[0]], "giriş"))
    else:
        accepted[0][0] = 0.0

    for k, (t, hits, trig) in enumerate(accepted):
        end = accepted[k + 1][0] if k + 1 < len(accepted) else total
        scenes.append((t, end, hits, trig))
    return scenes


def assign_free(total: float, videos: list, rng: random.Random, shuffle: bool) -> list:
    """SERBEST mod: tüm videolar baştan sona sırayla (veya karışık) döner."""
    cands = videos[:]
    if shuffle:
        rng.shuffle(cands)
    return [(0.0, total, cands, [])]


def log_scenes(scenes: list) -> None:
    for i, (t0, t1, cands, words) in enumerate(scenes, 1):
        if isinstance(words, str):
            snippet = f' kelime: "{words}"'
        else:
            snippet = " ".join(w["text"] for w in words[:6])
            snippet = f' "{snippet}..."' if snippet else ""
        names = ", ".join(c.name for c in cands)
        log(f"   Sahne {i:>2} [{t0:6.1f}s - {t1:6.1f}s]{snippet}  ->  {names}")


def fill_scenes(scenes: list, info: dict, seg_min: float, seg_max: float,
                rng: random.Random) -> list:
    """
    Her sahnenin süresini, atanan videolardan alınan 2-4 sn'lik kesimlerle doldurur.
    Sahne sınırları ses zamanına göre hesaplandığı için kayma birikmez.
    Dönüş: [(video, başlangıç, süre), ...]
    """
    cursors = {v: 0.0 for v in info}
    segments, acc = [], 0.0
    eps = 0.5 / FPS

    for t0, t1, cands, _words in scenes:
        ci = 0
        while acc < t1 - eps:
            v = cands[ci % len(cands)]
            ci += 1
            vdur = info[v]
            remaining = t1 - acc

            seg = rng.uniform(seg_min, seg_max)
            if remaining - seg < 1.0:        # çok kısa artık parça bırakma
                seg = remaining

            start = cursors[v]
            if start + min(seg, 0.5) > vdur:  # video sonuna gelindi -> başa sar (loop)
                start = 0.0
            seg_len = min(seg, vdur - start)
            seg_len = max(round(seg_len * FPS) / FPS, 1 / FPS)

            nxt = start + seg_len
            cursors[v] = 0.0 if nxt >= vdur - 0.3 else nxt

            segments.append((v, start, seg_len))
            acc += seg_len
    return segments


def build_clips(segments: list, tmp: Path) -> list:
    """Her kesimi 1080x1920 / 30 FPS / yuv420p standardına getirip kaydeder."""
    vf = (
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={WIDTH}:{HEIGHT},setsar=1,fps={FPS},"
        "eq=contrast=1.03:saturation=1.05,"
        "format=yuv420p"
    )
    clips = []
    for i, (vpath, start, dur) in enumerate(segments, 1):
        out = tmp / f"seg_{i:04d}.mp4"
        log(f"   Klip {i}/{len(segments)}: {vpath.name} "
            f"[{start:.1f}s -> {start + dur:.1f}s]")
        run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", vpath,
            "-vf", vf, "-r", FPS, "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "17",
            "-pix_fmt", "yuv420p",
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            out,
        ])
        clips.append(out)
    return clips


def concat_clips(clips: list, tmp: Path) -> Path:
    list_file = tmp / "concat.txt"
    list_file.write_text("".join(f"file '{c.name}'\n" for c in clips), encoding="utf-8")
    out = tmp / "concat.mp4"
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", "concat.txt", "-c", "copy", out.name,
    ], cwd=tmp)
    return out


# --------------------------------------------------------------------------
# 3) ASS altyazı üretimi
# --------------------------------------------------------------------------
def ass_time(t: float) -> str:
    t = max(t, 0.0)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def get_font_family(font_path: Path) -> str:
    """Font dosyasındaki aile adını okur; olmazsa Arial'a düşer."""
    if font_path.exists():
        try:
            from fontTools.ttLib import TTFont
            name = TTFont(str(font_path), lazy=True)["name"]
            fam = name.getDebugName(16) or name.getDebugName(1)
            if fam:
                return fam
        except Exception as e:
            log(f"   (Uyarı: font adı okunamadı: {e}. 'Arial' kullanılacak.)")
    return "Arial"


def group_words(words: list, per_group: int, max_chars: int, max_gap: float = 0.8) -> list:
    groups, cur, chars = [], [], 0
    for w in words:
        gap = (w["start"] - cur[-1]["end"]) if cur else 0.0
        ends_sentence = bool(cur) and re.search(r"[.!?…]$", cur[-1]["text"])
        if cur and (len(cur) >= per_group
                    or chars + len(w["text"]) + 1 > max_chars
                    or gap > max_gap
                    or ends_sentence):
            groups.append(cur)
            cur, chars = [], 0
        cur.append(w)
        chars += len(w["text"]) + 1
    if cur:
        groups.append(cur)
    return groups


def word_events(words: list, total: float, fmt, hl: str, args) -> list:
    """
    Tek kelime modu: her kelime söylendiği anda ekrana gelir (küçük bir "pop" animasyonuyla),
    okunduktan sonra kaybolur. Sessizlik boyunca ekranda yazı kalmaz.
    """
    color = "FFFFFF" if args.white else hl
    lines = []
    for i, w in enumerate(words):
        txt = fmt(clean_token(w["text"]))
        if not txt:
            continue
        start = w["start"]
        end = w["end"] + 0.12
        if i + 1 < len(words):
            nxt = words[i + 1]["start"]
            if nxt - w["end"] < 0.25:      # kelimeler bitişikse boşluk bırakma (titreme olmasın)
                end = nxt
            else:
                end = min(end, nxt)
        end = min(end, total)
        if end <= start:
            end = start + 0.08

        size_tag = ""
        if len(txt) > 11:                  # uzun kelime ekrana sığsın diye küçült
            size_tag = f"\\fs{max(int(args.font_size * 11 / len(txt)), int(args.font_size * 0.55))}"
        tags = (f"{{\\c&H{color}&{size_tag}\\fscx80\\fscy80"
                f"\\t(0,90,\\fscx100\\fscy100)}}")
        lines.append(
            f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{tags}{txt}"
        )
    return lines


def build_ass(words: list, total: float, font_family: str, args) -> str:
    hl = HIGHLIGHT_COLORS[args.highlight]
    bold = 0 if args.no_bold else -1

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {WIDTH}\n"
        f"PlayResY: {HEIGHT}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_family},{args.font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,"
        f"&H96000000,{bold},0,0,0,100,100,0,0,1,{args.outline},{args.shadow},2,60,60,"
        f"{args.margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    def fmt(text: str) -> str:
        return tr_upper(text) if args.uppercase else text

    lines = []
    if args.style == "kelime":
        lines = word_events(words, total, fmt, hl, args)
        return header + "\n".join(lines) + "\n"
    groups = group_words(words, args.words_per_group, args.max_chars)
    for gi, group in enumerate(groups):
        texts = [fmt(clean_token(w["text"]) or w["text"]) for w in group]
        next_group_start = groups[gi + 1][0]["start"] if gi + 1 < len(groups) else total
        for i, w in enumerate(group):
            start = w["start"]
            if i + 1 < len(group):
                end = group[i + 1]["start"]          # boşluk bırakmadan sonraki kelimeye geç
            else:
                end = min(w["end"] + 0.15, next_group_start)   # bir sonraki grupla çakışma
            end = min(end, total)
            if end <= start:
                end = start + 0.05

            parts = []
            for j, t in enumerate(texts):
                if j == i:
                    parts.append(
                        f"{{\\c&H{hl}&\\fscx110\\fscy110}}{t}{{\\c&HFFFFFF&\\fscx100\\fscy100}}"
                    )
                else:
                    parts.append(t)
            lines.append(
                f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{' '.join(parts)}"
            )
    return header + "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# 4) Birleştirme + altyazı gömme
# --------------------------------------------------------------------------
# Konuşma aralarındaki tıslama / cızırtı / tık seslerini azaltır (konuşmanın kendisine dokunmaz)
# (Eski sürümdeki afftdn/adeclick ve sert gate, "tıs tıs" gibi yapay ses üretebiliyordu; yumuşak gate yeterli.)
CLEAN_AF = "highpass=f=80,agate=threshold=0.01:ratio=2:range=0.1:attack=15:release=300:knee=4"

# Ses kalitesi: AAC'yi yüksek bit hızında kodla (tıs/cızırtı gibi kodlama artefaktlarını önler)
AUDIO_BITRATE = "320k"


def burn_and_mux(video: Path, audio: Path, ass_name: str, total: float,
                 tmp: Path, output: Path, clean: bool = False) -> None:
    # ASS dosyası ve fontlar tmp içinde; komut tmp'de çalışır, böylece
    # Windows yol/iki nokta (C:\...) kaçış sorunları yaşanmaz.
    vf = (
        "tpad=stop_mode=clone:stop_duration=3,"   # olası ufak süre farkına karşı son kareyi uzat
        f"ass={ass_name}:fontsdir=fonts"
    )
    def cmd(af):
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", video, "-i", audio,
            "-map", "0:v:0", "-map", "1:a:0",
            "-vf", vf,
            *(["-af", af] if af else []),
            "-t", f"{total:.3f}",
            "-r", FPS,
            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", "44100",
            "-movflags", "+faststart",
            output,
        ]
    if clean:
        r = subprocess.run([str(c) for c in cmd(CLEAN_AF)], cwd=str(tmp), capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            log("   Ses temizlendi (tıslama/cızırtı filtresi).")
            return
        log("   Ses temizleme bu ffmpeg sürümünde çalışmadı, normal ses kullanılıyor.")
    run(cmd(None), cwd=tmp)


# --------------------------------------------------------------------------
# Ana akış
# --------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="Otomatik Shorts/Reels video oluşturucu")
    p.add_argument("--model", default="medium",
                   help="Whisper modeli: tiny, base, small, medium, large-v3 (varsayılan: medium; "
                        "Türkçede small çok hata yapar, en iyisi large-v3)")
    p.add_argument("--prompt", default=None,
                   help="Whisper'a ipucu: özel isim/terimleri yazın (örn. \"Atatürk, Çorum, Hattuşa\")")
    p.add_argument("--script", default=None,
                   help="Seslendirme metni dosyası (txt). Verilirse altyazı bu metinle yazılır, "
                        "zamanlar sesten alınır. Verilmezse inputs/metin.txt aranır.")
    p.add_argument("--device", default="auto", help="auto, cpu veya cuda")
    p.add_argument("--match", choices=["sirali", "kelime", "serbest"], default=None,
                   help="Video eşleştirme modu: kelime (anahtar kelime söylenince o video; "
                        "inputs/eslesme.txt varsa varsayılan), sirali (dosya sırası = hikaye sırası), "
                        "serbest (sürekli döngü)")
    p.add_argument("--min-scene", type=float, default=1.2,
                   help="Bundan kısa cümleler bir sonrakiyle birleştirilir (sn)")
    p.add_argument("--shuffle", action="store_true", help="(serbest mod) Videoları karıştır")
    p.add_argument("--seed", type=int, default=None, help="Rastgelelik için sabit tohum")
    p.add_argument("--seg-min", type=float, default=2.0, help="En kısa sahne süresi (sn)")
    p.add_argument("--seg-max", type=float, default=4.0, help="En uzun sahne süresi (sn)")
    p.add_argument("--highlight", choices=list(HIGHLIGHT_COLORS), default="sari",
                   help="Vurgu rengi: sari veya cyan")
    p.add_argument("--no-uppercase", dest="uppercase", action="store_false",
                   help="Altyazıyı BÜYÜK HARF yapma")
    p.add_argument("--style", choices=["kelime", "grup"], default="kelime",
                   help="Altyazı biçimi: kelime (her kelime tek tek gelip gider, varsayılan) "
                        "veya grup (2-3 kelime birlikte, okunan kelime renkli)")
    p.add_argument("--white", action="store_true",
                   help="(kelime stili) Kelimeyi renkli değil beyaz yaz")
    p.add_argument("--words-per-group", type=int, default=3, help="(grup stili) Aynı anda kelime sayısı")
    p.add_argument("--max-chars", type=int, default=20, help="Bir altyazı grubundaki en fazla karakter")
    p.add_argument("--font-size", type=int, default=None,
                   help="Yazı boyutu (varsayılan: kelime stilinde 110, grupta 88)")
    p.add_argument("--outline", type=float, default=7, help="Kontur kalınlığı")
    p.add_argument("--shadow", type=float, default=3, help="Gölge mesafesi")
    p.add_argument("--margin-v", type=int, default=400,
                   help="Alttan boşluk (px). Küçültürseniz altyazı aşağı iner, "
                        "büyütürseniz yukarı çıkar. (varsayılan: 400)")
    p.add_argument("--no-bold", action="store_true", help="Kalın yazıyı kapat")
    p.add_argument("--clean-audio", action="store_true",
                   help="Konuşma aralarındaki tıslama/cızırtı/tık seslerini azalt")
    p.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Çıktı dosyası")
    p.add_argument("--keep-temp", action="store_true", help="Geçici dosyaları silme")
    p.add_argument("--no-cache", dest="use_cache", action="store_false",
                   help="Transkript önbelleğini kullanma, Whisper'ı her seferinde yeniden çalıştır")
    args = p.parse_args()

    args.seg_min = max(args.seg_min, 0.5)
    args.seg_max = max(args.seg_max, args.seg_min)
    args.words_per_group = max(args.words_per_group, 1)
    if args.font_size is None:
        args.font_size = 110 if args.style == "kelime" else 88
    return args


def main() -> None:
    args = parse_args()
    rng = random.Random(args.seed)

    log("=== Shorts/Reels Video Hattı ===")
    check_tools()
    prepare_folders()

    audio = find_audio()
    videos = find_videos()
    log(f"-> Girdiler bulundu: {len(videos)} video, ses: {audio.name}")

    # Geçici klasörü temizle / hazırla
    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR, ignore_errors=True)
    (TEMP_DIR / "fonts").mkdir(parents=True, exist_ok=True)

    try:
        # --- Ses süresi ---
        total = probe_duration(audio)
        if total <= 0:
            fail("Ses süresi okunamadı. Dosya bozuk olabilir.")
        log(f"-> Ses süresi: {total:.2f} saniye")

        # --- Videoları doğrula ---
        log("-> Videolar kontrol ediliyor...")
        info = {}
        for v in videos:
            d = probe_duration(v)
            if d >= 0.5:
                info[v] = d
            else:
                log(f"   (Atlandı: okunamadı veya çok kısa -> {v.name})")
        if not info:
            fail("Kullanılabilir video yok (hepsi bozuk veya çok kısa).")
        if sum(info.values()) < total:
            log("   Videolar sesten kısa; başa sarılarak (loop) kullanılacak.")

        # --- Ses analizi ---
        log(f"-> Ses analiz ediliyor (faster-whisper '{args.model}', Türkçe)...")
        log("   (İlk çalıştırmada model indirilir, biraz sürebilir.)")
        words = transcribe_words_cached(audio, args.model, args.device, args.prompt, args.use_cache)
        words = collapse_repeats(words)
        log(f"   {len(words)} kelime ve zaman damgaları çıkarıldı.")

        # --- (Opsiyonel) doğru metinle hizalama ---
        script_text = ""
        if args.script:
            sp = Path(args.script)
            if not sp.is_file():
                fail(f"Metin dosyası bulunamadı: {sp}")
            try:
                script_text = sp.read_text(encoding="utf-8-sig")
            except UnicodeDecodeError:
                script_text = sp.read_text(encoding="cp1254", errors="replace")
        else:
            script_text = load_script()
        if script_text.strip():
            log("-> Seslendirme metni bulundu; altyazı bu metinle yazılacak (zamanlar sesten).")
            aligned = align_script(words, script_text)
            if aligned:
                words = aligned
        words = sanitize_words(words, total)

        # --- Klip planı + kırpma ---
        mode = args.match or ("kelime" if MAP_FILE.exists() else "sirali")
        log(f"-> Sahneler ve video eşleşmesi planlanıyor (mod: {mode})...")
        vlist = list(info.keys())
        if mode == "kelime":
            mapping = load_mapping(vlist)
            if mapping:
                log(f"   eslesme.txt okundu: {len(mapping)} video için anahtar kelime var.")
            scenes = assign_keywords(words, vlist, total, args.min_scene, mapping)
        elif mode == "sirali":
            sentences = build_sentences(words, total, args.min_scene)
            log(f"   {len(sentences)} cümle algılandı.")
            scenes = assign_sequential(sentences, vlist, args.min_scene)
        else:
            scenes = assign_free(total, vlist, rng, args.shuffle)
        log_scenes(scenes)
        segments = fill_scenes(scenes, info, args.seg_min, args.seg_max, rng)
        log(f"   {len(segments)} klip oluşturulacak.")
        log(f"-> Klipler kırpılıyor ve {WIDTH}x{HEIGHT} @ {FPS} FPS'e dönüştürülüyor...")
        clips = build_clips(segments, TEMP_DIR)

        log("-> Klipler birleştiriliyor...")
        merged = concat_clips(clips, TEMP_DIR)

        # --- Altyazı ---
        log("-> Altyazı dosyası (ASS) hazırlanıyor...")
        font_family = "Arial"
        if FONT_PATH.exists():
            shutil.copy(FONT_PATH, TEMP_DIR / "fonts" / "font.ttf")
            font_family = get_font_family(FONT_PATH)
            log(f"   Özel font kullanılıyor: {font_family}")
        else:
            log("   inputs/font.ttf yok; sistem fontu (Arial) kullanılacak.")
        ass_text = build_ass(words, total, font_family, args)
        (TEMP_DIR / "subs.ass").write_text(ass_text, encoding="utf-8")

        # --- Son birleştirme ---
        log("-> Altyazı gömülüyor ve ses ekleniyor (bu adım biraz sürebilir)...")
        output = Path(args.output).resolve()
        burn_and_mux(merged, audio, "subs.ass", total, TEMP_DIR, output, clean=args.clean_audio)

        if not output.exists() or output.stat().st_size == 0:
            fail("Çıktı dosyası oluşturulamadı.")
        log(f"\n✔ Tamamlandı! Video kaydedildi: {output}")

    except KeyboardInterrupt:
        fail("Kullanıcı tarafından durduruldu.", 130)
    finally:
        if not args.keep_temp:
            shutil.rmtree(TEMP_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()
