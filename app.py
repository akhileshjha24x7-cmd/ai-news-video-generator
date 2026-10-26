import os
import re
import glob
import time
import json
import shutil
import subprocess
import tempfile
import textwrap
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus

import requests
import streamlit as st
from PIL import Image, ImageDraw, ImageFont

APP_TITLE = "Hindi News Video Generator"
W, H = 1920, 1080
FPS = 25
MAX_DOWNLOAD_MB = 100
USER_AGENT = "HindiNewsVideoGenerator/1.0 (Streamlit app; media-source attribution enabled)"

st.set_page_config(page_title=APP_TITLE, page_icon="🎬", layout="wide")
st.title("🎬 Hindi News Video Generator")
st.caption("Upload narration → transcribe → find suitable footage → add Hindi headlines → export 1080p MP4.")

st.info(
    "Copyright-aware mode: footage is downloaded only from Pexels or Wikimedia Commons after basic source/license checks. "
    "News/social links are shown as research leads; this app does not scrape or download media from Facebook, X, Google Images, "
    "or news publishers. Publicly visible media is not automatically reusable."
)

def run_cmd(cmd, timeout=600):
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "External command failed")[-5000:])
    return result.stdout.strip()

def ffmpeg_exists():
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

def get_audio_duration(path):
    out = run_cmd([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path
    ], timeout=60)
    return max(1.0, float(out))

def transcribe_audio(path, model_size="base"):
    from faster_whisper import WhisperModel
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
    path,
    language="hi",
    task="transcribe",
    beam_size=5,
    vad_filter=True
)
    rows = []
    for seg in segments:
        txt = (seg.text or "").strip()
        if txt:
            rows.append({"start": float(seg.start), "end": float(seg.end), "text": txt})
    return rows, getattr(info, "language", "unknown")

def group_transcript(rows, duration, scene_seconds):
    if not rows:
        return [{"start": 0, "end": duration, "text": ""}]
    scenes = []
    start = 0.0
    while start < duration:
        end = min(duration, start + scene_seconds)
        bits = [r["text"] for r in rows if r["end"] > start and r["start"] < end]
        scenes.append({"start": start, "end": end, "text": " ".join(bits).strip()})
        start = end
    return scenes

# Lightweight bilingual hints for common news subjects. Users can edit every search query.
HINTS = [
    (r"मोदी|प्रधानमंत्री|पीएम\b", "Indian prime minister government"),
    (r"शाह|गृह मंत्री|अमित शाह", "Indian home minister parliament"),
    (r"राहुल गांधी|कांग्रेस", "Indian opposition parliament political rally"),
    (r"भाजपा|बीजेपी|बजेप|BJP", "Indian political party rally"),
    (r"सुप्रीम कोर्ट|सुप्रीमकोर्ट|अदालत|कोर्ट|जज", "Supreme Court of India courthouse"),
    (r"चुनाव|वोटर|मतदान|चुनाव आयोग", "India election voting polling station"),
    (r"संसद|लोकसभा|राज्यसभा", "Indian parliament building"),
    (r"पुलिस|गिरफ्तार|लाठीचार्ज", "Indian police crowd protest"),
    (r"प्रदर्शन|धरना|आंदोलन|रैली", "India public protest crowd"),
    (r"दिल्ली", "New Delhi India city government buildings"),
    (r"पश्चिम बंगाल|बंगाल|ममता", "West Bengal Kolkata political rally"),
    (r"अर्थव्यवस्था|शेयर बाजार|महंगाई", "India economy stock market business"),
    (r"युद्ध|सेना|मिसाइल|सीमा", "military troops international conflict"),
    (r"ट्रंप|अमेरिका|अमेरिकी", "United States White House government"),
    (r"चीन|चीनी", "China government city skyline"),
    (r"किसान", "Indian farmers agricultural protest"),
    (r"मौसम|बारिश|बाढ़", "India monsoon rain flood"),
    (r"तकनीक|एआई|कृत्रिम बुद्धिमत्ता", "artificial intelligence technology data center"),
]

def suggest_query(text):
    for pattern, hint in HINTS:
        if re.search(pattern, text or "", re.I):
            return hint
    # Use common visible English words if present; otherwise a general India news visual.
    english = re.findall(r"[A-Za-z][A-Za-z -]{2,}", text or "")
    useful = []
    for token in english:
        token = token.strip()
        if token.lower() not in {"the", "and", "for", "with", "from", "this", "that", "news"} and token not in useful:
            useful.append(token)
    if useful:
        return " ".join(useful[:5]) + " India news"
    return "India news government building crowd"

def headline_from_text(text, max_chars=82):
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return "ताज़ा खबर"
    # Avoid slicing in the middle of a word where possible.
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,।-") + "…"

def find_font():
    """Find a font that supports Hindi / Devanagari."""
    import os
    import glob
    import subprocess

    # First try the installed Noto Devanagari font.
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", "Noto Sans Devanagari"],
            capture_output=True,
            text=True,
            timeout=10
        )
        font_path = result.stdout.strip().splitlines()[0]
        if font_path and os.path.isfile(font_path):
            ImageFont.truetype(font_path, 40)
            return font_path
    except Exception:
        pass

    # Search common font folders.
    patterns = [
        "/usr/share/fonts/**/*Devanagari*.ttf",
        "/usr/share/fonts/**/*devanagari*.ttf",
        "/usr/share/fonts/**/*Devanagari*.otf",
        "/usr/share/fonts/**/*devanagari*.otf",
    ]

    for pattern in patterns:
        for font_path in glob.glob(pattern, recursive=True):
            try:
                ImageFont.truetype(font_path, 40)
                return font_path
            except Exception:
                continue

    # No suitable font found.
    return None

def wrap_text(draw, text, font, max_width):
    words = (text or "").split()
    lines, line = [], ""
    for word in words:
        trial = word if not line else line + " " + word
        try:
            width = draw.textbbox((0, 0), trial, font=font)[2]
        except Exception:
            width = len(trial) * 30
        if width <= max_width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines[:3]

def make_overlay(headline, out_path, font_path):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Header ribbon
    draw.rounded_rectangle((65, 55, 510, 145), radius=18, fill=(190, 0, 0, 235))
    try:
        label_font = ImageFont.truetype(font_path, 42) if font_path else ImageFont.load_default()
        headline_font = ImageFont.truetype(font_path, 72) if font_path else ImageFont.load_default()
        small_font = ImageFont.truetype(font_path, 30) if font_path else ImageFont.load_default()
    except Exception:
        label_font = headline_font = small_font = ImageFont.load_default()
    draw.text((92, 73), "ताज़ा खबर", font=label_font, fill=(255, 255, 255, 255), stroke_width=1, stroke_fill=(0,0,0,255))
    # Bottom banner with alpha
    draw.rectangle((0, 790, W, H), fill=(0, 0, 0, 205))
    draw.rectangle((0, 790, W, 808), fill=(255, 190, 0, 255))
    lines = wrap_text(draw, headline, headline_font, W - 140)
    y = 830 if len(lines) <= 2 else 815
    for line in lines:
        draw.text((70, y), line, font=headline_font, fill=(255,255,255,255),
                  stroke_width=2, stroke_fill=(0,0,0,255))
        y += 82
    draw.text((70, 1030), "रिपोर्टिंग / विश्लेषण", font=small_font, fill=(255, 215, 80, 255))
    img.save(out_path)

def request_json(url, params=None, headers=None, timeout=25):
    r = requests.get(url, params=params, headers=headers or {"User-Agent": USER_AGENT}, timeout=timeout)
    r.raise_for_status()
    return r.json()

def pexels_search(query, api_key):
    if not api_key:
        return None
    data = request_json(
        "https://api.pexels.com/v1/videos/search",
        params={"query": query, "orientation": "landscape", "size": "medium", "per_page": 8},
        headers={"Authorization": api_key, "User-Agent": USER_AGENT},
    )
    videos = data.get("videos", [])
    # Prefer landscape Full HD / HD MP4s, then choose smallest acceptable among the top results.
    for v in videos:
        files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4" and f.get("link")]
        files.sort(key=lambda f: (0 if (f.get("width") or 0) >= 1280 else 1, abs((f.get("width") or 0) - 1920)))
        if files:
            f = files[0]
            return {
                "kind": "video", "download_url": f["link"], "source": v.get("url", "https://www.pexels.com/"),
                "creator": (v.get("user") or {}).get("name", "Pexels creator"),
                "license": "Pexels License (check current terms)", "title": f"Pexels video {v.get('id')}",
                "provider": "Pexels"
            }
    return None

def commons_search(query):
    # Wikimedia Commons files are individually licensed; accept only recognizable free licenses.
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrnamespace": 6, "gsrsearch": query + " filetype:video",
        "gsrlimit": 10, "prop": "imageinfo", "iiprop": "url|mime|size|extmetadata"
    }
    data = request_json("https://commons.wikimedia.org/w/api.php", params=params)
    pages = (data.get("query") or {}).get("pages") or {}
    allowed_ext = (".mp4", ".webm", ".ogv", ".ogg", ".mov")
    for page in pages.values():
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        info = infos[0]
        url = info.get("url", "")
        mime = info.get("mime", "")
        if not url.lower().endswith(allowed_ext) and not mime.startswith("video/"):
            continue
        size = info.get("size") or 0
        if size and size > MAX_DOWNLOAD_MB * 1024 * 1024:
            continue
        meta = info.get("extmetadata") or {}
        license_name = re.sub("<[^>]+>", "", (meta.get("LicenseShortName") or {}).get("value", "")).strip()
        artist = re.sub("<[^>]+>", "", (meta.get("Artist") or {}).get("value", "")).strip()
        license_url = re.sub("<[^>]+>", "", (meta.get("LicenseUrl") or {}).get("value", "")).strip()
        if not any(x in license_name.lower() for x in ["cc by", "cc0", "public domain", "pd", "creative commons"]):
            continue
        return {
            "kind": "video", "download_url": url,
            "source": "https://commons.wikimedia.org/wiki/" + quote_plus(page.get("title", "").replace(" ", "_")),
            "creator": artist or "See Wikimedia Commons file page",
            "license": license_name or "See file page",
            "license_url": license_url, "title": page.get("title", "Wikimedia Commons video"),
            "provider": "Wikimedia Commons"
        }
    return None

def search_news_links(query, limit=4):
    # Google News RSS is used only to discover articles; media is not downloaded from publishers.
    url = "https://news.google.com/rss/search?q=" + quote_plus(query) + "&hl=hi&gl=IN&ceid=IN:hi"
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        out = []
        for item in root.findall(".//item")[:limit]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if title and link:
                out.append({"title": title, "url": link})
        return out
    except Exception:
        return []

def download_file(url, destination, max_mb=MAX_DOWNLOAD_MB):
    total = 0
    with requests.get(url, headers={"User-Agent": USER_AGENT}, stream=True, timeout=(20, 90)) as r:
        r.raise_for_status()
        content_type = r.headers.get("Content-Type", "").lower()
        with open(destination, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_mb * 1024 * 1024:
                    raise RuntimeError(f"Download exceeded {max_mb} MB limit.")
                f.write(chunk)
    if os.path.getsize(destination) < 20_000:
        raise RuntimeError("Downloaded media file is unexpectedly small.")
    return destination

def is_video_valid(path):
    try:
        run_cmd(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                 "-of", "csv=p=0", path], timeout=30)
        return True
    except Exception:
        return False

def make_fallback_card(headline, out_path, font_path):
    img = Image.new("RGB", (W, H), (18, 28, 48))
    draw = ImageDraw.Draw(img)
    try:
        title_font = ImageFont.truetype(font_path, 78) if font_path else ImageFont.load_default()
        small_font = ImageFont.truetype(font_path, 42) if font_path else ImageFont.load_default()
    except Exception:
        title_font = small_font = ImageFont.load_default()
    draw.rectangle((0, 0, W, 28), fill=(210, 0, 0))
    draw.text((90, 90), "NEWS EXPLAINER", font=small_font, fill=(255, 200, 0))
    lines = wrap_text(draw, headline, title_font, W - 180)
    y = 380
    for line in lines:
        draw.text((90, y), line, font=title_font, fill=(255,255,255), stroke_width=2, stroke_fill=(0,0,0))
        y += 105
    draw.text((90, 930), "Illustrative graphic — not event footage", font=small_font, fill=(200,210,225))
    img.save(out_path, quality=92)

def render_scene(media_path, overlay_path, out_path, duration, is_image=False):
    duration = max(1.0, float(duration))
    if is_image:
        input_args = ["-loop", "1", "-i", media_path]
    else:
        input_args = ["-stream_loop", "-1", "-i", media_path]
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + input_args + [
        "-loop", "1", "-i", overlay_path,
        "-t", f"{duration:.3f}",
        "-filter_complex",
        "[0:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps=25,setsar=1[bg];"
        "[1:v]format=rgba[ov];[bg][ov]overlay=0:0:shortest=1,format=yuv420p[v]",
        "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "ultrafast",
        "-crf", "23", "-r", str(FPS), "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", out_path
    ]
    run_cmd(cmd, timeout=max(120, int(duration * 15)))

def make_report(rows, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write("HINDI NEWS VIDEO — SOURCE AND ATTRIBUTION REPORT\n")
        f.write("Review every source and its current license before publishing.\n\n")
        for i, row in enumerate(rows, 1):
            f.write(f"Scene {i}: {row.get('headline','')}\n")
            f.write(f"Search query: {row.get('query','')}\n")
            f.write(f"Provider: {row.get('provider','Fallback graphic')}\n")
            f.write(f"Title: {row.get('title','')}\n")
            f.write(f"Creator: {row.get('creator','')}\n")
            f.write(f"License: {row.get('license','Illustrative graphic; no external media')}\n")
            f.write(f"Source URL: {row.get('source','')}\n")
            f.write(f"License URL: {row.get('license_url','')}\n")
            for n in row.get("news", []):
                f.write(f"Related news lead: {n['title']} — {n['url']}\n")
            f.write("\n")

if not ffmpeg_exists():
    st.error("FFmpeg/FFprobe not found. In Streamlit Cloud, make sure packages.txt contains: ffmpeg")
    st.stop()

with st.sidebar:
    st.header("Settings")
    pexels_key = st.text_input(
        "Pexels API key (recommended)",
        value=st.secrets.get("PEXELS_API_KEY", "") if hasattr(st, "secrets") else "",
        type="password",
        help="Get a key at https://www.pexels.com/api/ . The key is used only for video search."
    )
    model_size = st.selectbox("Speech transcription model", ["tiny", "base", "small"], index=1,
                              help="Base is a balance of speed and accuracy. Small may be slower on free hosting.")
    scene_seconds = st.slider("Average scene length (seconds)", min_value=8, max_value=25, value=15)
    max_scenes = st.slider("Maximum scenes to render", min_value=5, max_value=60, value=35)
    st.caption("Pexels API is free to request, but API limits apply. Attribution is shown in the report.")

audio_file = st.file_uploader("1. Upload your Hindi narration audio", type=["mp3", "wav", "m4a", "aac", "ogg", "flac"])
transcript_upload = st.file_uploader("Optional: upload an existing Hindi transcript (.txt)", type=["txt"])
if audio_file:
    st.audio(audio_file)
st.markdown("**Optional source links:** You can paste article or public-post URLs for your own reference. This app will not download their copyrighted media.")
source_links_text = st.text_area("Reference URLs (one per line)", height=80, placeholder="https://...")

if "transcript_rows" not in st.session_state:
    st.session_state.transcript_rows = []
if "transcript_text" not in st.session_state:
    st.session_state.transcript_text = ""

c1, c2 = st.columns(2)
with c1:
    transcribe_clicked = st.button("2. Transcribe narration", type="primary", disabled=audio_file is None)
with c2:
    clear_clicked = st.button("Clear transcription")

if clear_clicked:
    st.session_state.transcript_rows = []
    st.session_state.transcript_text = ""
    st.rerun()

if transcribe_clicked:
    with st.spinner("Transcribing audio. The first run may download the speech model..."):
        try:
            with tempfile.TemporaryDirectory() as td:
                audio_path = os.path.join(td, audio_file.name.replace("/", "_").replace("\\", "_"))
                with open(audio_path, "wb") as f:
                    f.write(audio_file.getbuffer())
                rows, language = transcribe_audio(audio_path, model_size)
            st.session_state.transcript_rows = rows
            st.session_state.transcript_text = "\n".join(r["text"] for r in rows)
            st.success(f"Transcription complete. Detected language: {language}.")
        except Exception as e:
            st.error(f"Transcription failed: {e}")
            st.info("You can upload a .txt transcript below instead. If this is a Python/PyAV error, keep av pinned below 19 in requirements.txt.")

if transcript_upload is not None:
    try:
        st.session_state.transcript_text = transcript_upload.getvalue().decode("utf-8", errors="replace")
    except Exception:
        pass

transcript_text = st.text_area(
    "3. Review/edit the transcript (important for accurate visuals)",
    value=st.session_state.transcript_text,
    height=180,
    placeholder="Your narration transcript will appear here. You can edit spelling and names."
)
st.session_state.transcript_text = transcript_text

st.caption("The app proposes search queries from the transcript. Review them before rendering; automated keyword matching can be imperfect.")
build_plan = st.button("4. Create editable scene plan", disabled=not (audio_file and transcript_text.strip()))
if build_plan:
    try:
        with tempfile.TemporaryDirectory() as td:
            audio_path = os.path.join(td, "narration")
            with open(audio_path, "wb") as f:
                f.write(audio_file.getbuffer())
            duration = get_audio_duration(audio_path)
        if st.session_state.transcript_rows:
            groups = group_transcript(st.session_state.transcript_rows, duration, scene_seconds)
        else:
            sentences = [x.strip() for x in re.split(r"(?<=[।!?])\s+|\n+", transcript_text) if x.strip()]
            count = max(1, min(max_scenes, int(duration // scene_seconds) + 1))
            chunk_size = max(1, (len(sentences) + count - 1) // count)
            grouped = [" ".join(sentences[i:i+chunk_size]) for i in range(0, len(sentences), chunk_size)]
            groups = []
            for i in range(count):
                start = duration * i / count
                end = duration * (i+1) / count
                groups.append({"start": start, "end": end, "text": grouped[i] if i < len(grouped) else ""})
        if len(groups) > max_scenes:
            groups = groups[:max_scenes]
            # stretch the final scene to cover the remaining narration
            groups[-1]["end"] = duration
        plan_lines = []
        for g in groups:
            headline = headline_from_text(g["text"])
            query = suggest_query(g["text"])
            plan_lines.append(f"{query} | {headline}")
        st.session_state.scene_plan = "\n".join(plan_lines)
        st.session_state.scene_duration = duration
        st.success(f"Scene plan created: {len(plan_lines)} scenes for {duration/60:.1f} minutes.")
    except Exception as e:
        st.error(f"Could not create scene plan: {e}")

scene_plan = st.text_area(
    "5. Edit the scene plan — one scene per line: English search keywords | Hindi headline",
    value=st.session_state.get("scene_plan", ""),
    height=300,
    placeholder="Indian parliament building | संसद में आज बड़ी हलचल\nIndian police public protest | प्रदर्शनकारियों पर कार्रवाई"
)
st.session_state.scene_plan = scene_plan

st.warning("Real news footage may not be available from free stock libraries. The app uses clearly illustrative stock footage or a labelled graphic instead of pretending generic footage is the real event.")

generate = st.button("6. Generate 1080p MP4", type="primary", disabled=not (audio_file and scene_plan.strip()))
if generate:
    lines = [line.strip() for line in scene_plan.splitlines() if line.strip()]
    parsed = []
    for line in lines:
        parts = line.split("|", 1)
        query = parts[0].strip()
        headline = parts[1].strip() if len(parts) > 1 else headline_from_text(parts[0])
        parsed.append({"query": query, "headline": headline})
    if not parsed:
        st.error("Add at least one scene to the scene plan.")
        st.stop()

    progress = st.progress(0)
    status = st.empty()
    sources = []
    try:
        with tempfile.TemporaryDirectory() as td:
            audio_path = os.path.join(td, "narration_audio")
            with open(audio_path, "wb") as f:
                f.write(audio_file.getbuffer())
            audio_duration = get_audio_duration(audio_path)
            # Scene durations cover the full narration exactly.
            durations = []
            for i in range(len(parsed)):
                start = audio_duration * i / len(parsed)
                end = audio_duration * (i + 1) / len(parsed)
                durations.append(max(1.0, end - start))
            font_path = find_font()
            scene_files = []
            for idx, item in enumerate(parsed):
                status.info(f"Scene {idx+1}/{len(parsed)}: searching for “{item['query']}”")
                source = None
                # Pexels first, then Commons only if the file metadata shows a recognizable free license.
                try:
                    source = pexels_search(item["query"], pexels_key)
                except Exception as e:
                    st.warning(f"Pexels search for scene {idx+1} failed: {e}")
                if source is None:
                    try:
                        source = commons_search(item["query"])
                    except Exception as e:
                        st.warning(f"Wikimedia search for scene {idx+1} failed: {e}")

                media_path = None
                is_image = False
                if source:
                    ext = ".mp4"
                    if source["download_url"].lower().split("?")[0].endswith(".webm"):
                        ext = ".webm"
                    elif source["download_url"].lower().split("?")[0].endswith(".ogv"):
                        ext = ".ogv"
                    candidate = os.path.join(td, f"source_{idx:03d}{ext}")
                    try:
                        download_file(source["download_url"], candidate)
                        if is_video_valid(candidate):
                            media_path = candidate
                        else:
                            source = None
                    except Exception as e:
                        st.warning(f"Could not download scene {idx+1} media: {e}")
                        source = None

                news = search_news_links(item["query"], limit=3)
                if media_path is None:
                    # Clearly labelled original graphic fallback; no third-party media.
                    media_path = os.path.join(td, f"fallback_{idx:03d}.png")
                    make_fallback_card(item["headline"], media_path, font_path)
                    is_image = True
                    source_row = {
                        **item, "provider": "Original generated graphic",
                        "title": "Generated title card", "creator": "Generated by this app",
                        "license": "Original graphic; headline text is user-provided",
                        "source": "", "license_url": "", "news": news
                    }
                else:
                    source_row = {**item, **source, "news": news}
                overlay_path = os.path.join(td, f"overlay_{idx:03d}.png")
                make_overlay(item["headline"], overlay_path, font_path)
                scene_out = os.path.join(td, f"scene_{idx:03d}.mp4")
                render_scene(media_path, overlay_path, scene_out, durations[idx], is_image=is_image)
                scene_files.append(scene_out)
                sources.append(source_row)
                progress.progress((idx + 1) / len(parsed) * 0.85)

            concat_list = os.path.join(td, "concat.txt")
            with open(concat_list, "w", encoding="utf-8") as f:
                for p in scene_files:
                    f.write("file '" + p.replace("'", "'\\''") + "'\n")
            silent_video = os.path.join(td, "silent_video.mp4")
            run_cmd(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                     "-f", "concat", "-safe", "0", "-i", concat_list,
                     "-c", "copy", "-movflags", "+faststart", silent_video], timeout=600)
            output_path = os.path.join(td, "hindi_news_1080p.mp4")
            status.info("Combining visuals with narration and exporting 1080p MP4...")
            run_cmd(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                     "-i", silent_video, "-i", audio_path,
                     "-map", "0:v:0", "-map", "1:a:0",
                     "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                     "-shortest", "-movflags", "+faststart", output_path], timeout=900)
            report_path = os.path.join(td, "source_report.txt")
            make_report(sources, report_path)
            with open(output_path, "rb") as f:
                video_bytes = f.read()
            with open(report_path, "rb") as f:
                report_bytes = f.read()
            progress.progress(1.0)
            status.success("Video generated.")
            st.video(video_bytes)
            st.download_button("Download 1080p MP4", video_bytes, file_name="hindi_news_1080p.mp4", mime="video/mp4")
            st.download_button("Download source and attribution report", report_bytes, file_name="source_report.txt", mime="text/plain")
            st.subheader("Source review")
            for i, s in enumerate(sources, 1):
                with st.expander(f"Scene {i}: {s.get('headline','')}"):
                    st.write(f"**Search query:** {s.get('query','')}")
                    st.write(f"**Provider:** {s.get('provider','')}")
                    st.write(f"**Creator:** {s.get('creator','')}")
                    st.write(f"**License:** {s.get('license','')}")
                    if s.get("source"):
                        st.markdown(f"[Open original media page]({s['source']})")
                    if s.get("license_url"):
                        st.markdown(f"[Open license]({s['license_url']})")
                    if s.get("news"):
                        st.write("Related news links (research leads only):")
                        for n in s["news"]:
                            st.markdown(f"- [{n['title']}]({n['url']})")
            st.caption("Pexels credit: [Pexels videos](https://www.pexels.com/videos/). Always review source terms and each item's license before publication.")
    except Exception as e:
        st.error(f"Generation failed: {e}")
        st.info("If the app crashes on a long video, test a short 30–60 second narration first. Free hosting may not have enough RAM/CPU for a long export.")
