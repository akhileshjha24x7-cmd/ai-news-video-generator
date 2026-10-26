
import os
import re
import glob
import html
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from io import BytesIO
from urllib.parse import quote_plus, urljoin, urlparse

import requests
import streamlit as st
from PIL import Image, ImageDraw, ImageFont, features

APP_TITLE = "Hindi News Video Generator"
W, H, FPS = 1920, 1080, 25
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_VIDEO_BYTES = 100 * 1024 * 1024
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
)

st.set_page_config(page_title=APP_TITLE, page_icon="🎬", layout="wide")
st.title("Hindi News Video Generator")
st.write(
    "Upload narration → transcribe in Hindi → find related images → "
    "create a 1080p MP4."
)

session = requests.Session()
session.headers.update({"User-Agent": UA})


# -------------------- SYSTEM AND FONT --------------------

def run_cmd(args, timeout=600):
    result = subprocess.run(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=timeout
    )
    if result.returncode:
        raise RuntimeError(
            (result.stderr or result.stdout or "Command failed")[-3500:]
        )
    return result.stdout.strip()


def ffmpeg_ready():
    return shutil.which("ffmpeg") and shutil.which("ffprobe")


def audio_length(path):
    value = run_cmd([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path
    ], timeout=60)
    return max(1.0, float(value))


def find_hindi_font():
    # Known paths on Debian/Streamlit Cloud with fonts-noto-core installed.
    preferred = [
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSerifDevanagari-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansDevanagari-Regular.ttf",
    ]

    candidates = preferred[:]
    for pattern in (
        "/usr/share/fonts/**/*Devanagari*.ttf",
        "/usr/share/fonts/**/*devanagari*.ttf",
        "/usr/share/fonts/**/*Devanagari*.otf",
        "/usr/share/fonts/**/*devanagari*.otf",
    ):
        candidates.extend(glob.glob(pattern, recursive=True))

    seen = set()
    for path in candidates:
        if path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        try:
            font = ImageFont.truetype(path, 44)
            # Confirm the font contains some essential Devanagari glyphs.
            mask = font.getmask("हिंदी समाचार")
            if mask.getbbox():
                return path
        except Exception:
            continue

    # fc-match can return a Latin font even when asked for Devanagari.
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", "Noto Sans Devanagari"],
            capture_output=True, text=True, timeout=10
        )
        path = result.stdout.strip().splitlines()[0]
        if "devanagari" in os.path.basename(path).lower():
            return path
    except Exception:
        pass

    return None


FONT_PATH = None


def get_font(size, bold=False):
    if not FONT_PATH:
        raise RuntimeError(
            "Hindi font is missing. Install fonts-noto-core and "
            "fonts-noto-extra in packages.txt, then reboot Streamlit."
        )

    path = FONT_PATH
    if bold:
        bold_path = path.replace("Regular", "Bold")
        if os.path.isfile(bold_path):
            path = bold_path

    # RAQM enables complex-script shaping when Pillow was built with it.
    try:
        if features.check_feature("raqm"):
            return ImageFont.truetype(
                path, size, layout_engine=ImageFont.Layout.RAQM
            )
    except Exception:
        pass

    return ImageFont.truetype(path, size)


def transcribe_audio(path, model_size):
    from faster_whisper import WhisperModel

    model = WhisperModel(
        model_size, device="cpu", compute_type="int8"
    )
    segments, info = model.transcribe(
        path,
        language="hi",
        task="transcribe",
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=True,
    )

    rows = []
    for segment in segments:
        text = (segment.text or "").strip()
        if text:
            rows.append({
                "start": float(segment.start),
                "end": float(segment.end),
                "text": text,
            })

    return rows, getattr(info, "language", "hi")


# -------------------- TRANSCRIPT AND SCENE PLAN --------------------

def split_sentences(text):
    return [
        s.strip() for s in re.split(r"(?<=[।!?])\s+|\n+", text)
        if s.strip()
    ]


def suggest_query(text):
    rules = [
        (r"मोदी|प्रधानमंत्री|पीएम", "Narendra Modi India latest news"),
        (r"अमित शाह|गृह मंत्री", "Amit Shah India latest news"),
        (r"राहुल गांधी|कांग्रेस", "Rahul Gandhi Congress India latest news"),
        (r"भाजपा|बीजेपी|BJP", "BJP India political news"),
        (r"सुप्रीम कोर्ट|अदालत|कोर्ट|जज", "Supreme Court of India news"),
        (r"चुनाव|वोटर|मतदान|चुनाव आयोग", "India election voting news"),
        (r"संसद|लोकसभा|राज्यसभा", "Indian parliament news"),
        (r"पुलिस|गिरफ्तार|लाठीचार्ज", "India police arrest protest news"),
        (r"प्रदर्शन|धरना|आंदोलन|रैली", "India protest rally news"),
        (r"बंगाल|ममता", "West Bengal Mamata Banerjee news"),
        (r"युद्ध|सेना|मिसाइल|सीमा", "military international conflict news"),
        (r"ट्रंप|अमेरिका|अमेरिकी", "Donald Trump United States news"),
        (r"चीन|चीनी", "China government latest news"),
        (r"किसान", "Indian farmers protest news"),
        (r"मौसम|बारिश|बाढ़", "India weather flood rain news"),
        (r"एआई|कृत्रिम बुद्धिमत्ता|तकनीक", "AI technology news"),
    ]
    for pattern, query in rules:
        if re.search(pattern, text or "", re.I):
            return query

    words = re.findall(r"[A-Za-z][A-Za-z -]{2,}", text or "")
    words = [
        x.strip() for x in words
        if x.strip().lower() not in {
            "the", "and", "for", "with", "from", "this", "that", "news"
        }
    ]
    return " ".join(words[:5]) + " India news" if words else "India latest news"


def make_headline(text, limit=82):
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= limit:
        return text or "ताज़ा खबर"
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,।-") + "…"


def make_scene_plan(transcript, duration, max_scenes, seconds_per_scene):
    sentences = split_sentences(transcript)
    if not sentences:
        sentences = [transcript.strip()]

    target = max(1, min(max_scenes, int(duration / seconds_per_scene) + 1))
    target = min(target, len(sentences))
    chunk_size = max(1, (len(sentences) + target - 1) // target)

    chunks = [
        " ".join(sentences[i:i + chunk_size])
        for i in range(0, len(sentences), chunk_size)
    ]

    return "\n".join(
        f"{suggest_query(chunk)} | {make_headline(chunk)}"
        for chunk in chunks
    )


# -------------------- GOOGLE NEWS AND IMAGE DISCOVERY --------------------

def search_news(query, limit=6):
    rss_url = (
        "https://news.google.com/rss/search?q="
        + quote_plus(query)
        + "&hl=hi&gl=IN&ceid=IN:hi"
    )
    response = session.get(rss_url, timeout=20)
    response.raise_for_status()
    root = ET.fromstring(response.content)

    articles = []
    for item in root.findall(".//item")[:limit]:
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        date = (item.findtext("pubDate") or "").strip()
        if title and url:
            articles.append({
                "title": title, "url": url, "published": date
            })
    return articles


def extract_meta_image(article_url):
    try:
        response = session.get(
            article_url, timeout=15, allow_redirects=True
        )
        if response.status_code != 200:
            return None

        page = response.text[:2_000_000]
        patterns = [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
        ]

        image_url = None
        for pattern in patterns:
            match = re.search(pattern, page, re.I)
            if match:
                image_url = html.unescape(match.group(1).strip())
                break

        if not image_url:
            return None

        image_url = urljoin(response.url, image_url)
        if urlparse(image_url).scheme not in ("http", "https"):
            return None

        with session.get(image_url, timeout=20, stream=True) as img_response:
            img_response.raise_for_status()
            content_type = img_response.headers.get("Content-Type", "").lower()
            if not content_type.startswith("image/"):
                return None

            data = bytearray()
            for chunk in img_response.iter_content(65536):
                if chunk:
                    data.extend(chunk)
                if len(data) > MAX_IMAGE_BYTES:
                    return None

        if len(data) < 4000:
            return None

        with Image.open(BytesIO(data)) as img:
            img.load()
            if img.width < 320 or img.height < 180:
                return None
            buffer = BytesIO()
            img.convert("RGB").save(buffer, "JPEG", quality=90)

        return {
            "bytes": buffer.getvalue(),
            "url": image_url,
            "article_url": response.url,
        }
    except Exception:
        return None


def find_commons_image(query):
    """
    Search Wikimedia Commons for images.
    Only accept files with a recognizable free-license label.
    Always retain the original file page for checking attribution.
    """
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrnamespace": 6,
        "gsrsearch": query,
        "gsrlimit": 12,
        "prop": "imageinfo",
        "iiprop": "url|mime|size|extmetadata",
    }

    try:
        response = session.get(
            "https://commons.wikimedia.org/w/api.php",
            params=params,
            timeout=20,
        )
        response.raise_for_status()
        pages = (response.json().get("query") or {}).get("pages") or {}

        for page in pages.values():
            infos = page.get("imageinfo") or []
            if not infos:
                continue

            info = infos[0]
            mime = info.get("mime", "")
            url = info.get("url", "")
            if not mime.startswith("image/") or not url:
                continue

            size = info.get("size") or 0
            if size > MAX_IMAGE_BYTES:
                continue

            meta = info.get("extmetadata") or {}

            def meta_value(key):
                return html.unescape(
                    re.sub(
                        r"<[^>]+>", "",
                        (meta.get(key) or {}).get("value", "")
                    )
                ).strip()

            license_name = meta_value("LicenseShortName")
            if not any(term in license_name.lower() for term in (
                "cc by", "cc0", "public domain", "cc by-sa",
                "creative commons",
            )):
                continue

            with session.get(url, timeout=20, stream=True) as img_response:
                img_response.raise_for_status()
                data = bytearray()
                for chunk in img_response.iter_content(65536):
                    if chunk:
                        data.extend(chunk)
                    if len(data) > MAX_IMAGE_BYTES:
                        break

            if len(data) > MAX_IMAGE_BYTES or len(data) < 4000:
                continue

            try:
                with Image.open(BytesIO(data)) as img:
                    img.load()
                    if img.width < 320 or img.height < 180:
                        continue
                    buffer = BytesIO()
                    img.convert("RGB").save(buffer, "JPEG", quality=90)
            except Exception:
                continue

            title = page.get("title", "Wikimedia Commons image")
            file_page = (
                "https://commons.wikimedia.org/wiki/"
                + quote_plus(title.replace(" ", "_"))
            )

            return {
                "bytes": buffer.getvalue(),
                "url": url,
                "article_url": file_page,
                "title": title,
                "creator": meta_value("Artist"),
                "license": license_name,
                "license_url": meta_value("LicenseUrl"),
                "provider": "Wikimedia Commons",
            }

    except Exception:
        return None

    return None


def find_image_and_news(query):
    articles = []
    try:
        articles = search_news(query, limit=6)
    except Exception:
        pass

    # First try a relevant image published with a recent news article.
    for article in articles:
        found = extract_meta_image(article["url"])
        if found:
            return {
                **found,
                "title": article["title"],
                "provider": "News article image",
                "creator": "See original article",
                "license": "Unverified; check reuse permission",
                "license_url": "",
            }, articles

    # Then try Wikimedia Commons, which exposes source and license metadata.
    commons = find_commons_image(query)
    if commons:
        return commons, articles

    return None, articles


# -------------------- IMAGE AND VIDEO RENDERING --------------------

def draw_wrapped(draw, text, font, max_width, max_lines=3):
    words = (text or "").split()
    lines, current = [], ""

    for word in words:
        candidate = word if not current else current + " " + word
        box = draw.textbbox((0, 0), candidate, font=font)
        if box[2] - box[0] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip("।, ") + "…"

    return lines


def create_overlay(headline, path):
    image = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    label_font = get_font(40, bold=True)
    title_font = get_font(66, bold=True)
    footer_font = get_font(28)

    draw.rounded_rectangle(
        (65, 55, 500, 145), radius=18, fill=(190, 0, 0, 245)
    )
    draw.text(
        (90, 72), "ताज़ा खबर", font=label_font, fill="white",
        stroke_width=1, stroke_fill="black"
    )

    draw.rectangle((0, 790, W, H), fill=(0, 0, 0, 220))
    draw.rectangle((0, 790, W, 807), fill=(255, 190, 0, 255))

    lines = draw_wrapped(draw, headline, title_font, W - 140, 3)
    y = 820 if len(lines) < 3 else 808

    for line in lines:
        draw.text(
            (70, y), line, font=title_font, fill="white",
            stroke_width=2, stroke_fill="black"
        )
        y += 78

    draw.text(
        (70, 1030), "समाचार और विश्लेषण",
        font=footer_font, fill=(255, 215, 80, 255)
    )
    image.save(path)


def create_fallback(path):
    image = Image.new("RGB", (W, H), (24, 37, 60))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, W, 25), fill=(205, 0, 0))
    draw.text(
        (80, 100), "NEWS EXPLAINER",
        font=get_font(44, bold=True), fill=(255, 210, 0)
    )
    draw.text(
        (80, 180), "Illustrative background — not actual event footage",
        font=get_font(30), fill="white"
    )
    image.save(path, quality=92)


def render_scene(media_path, overlay_path, output_path, duration):
    args = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-loop", "1", "-framerate", str(FPS), "-i", media_path,
        "-loop", "1", "-i", overlay_path,
        "-t", f"{duration:.3f}",
        "-filter_complex",
        "[0:v]scale=1920:1080:force_original_aspect_ratio=increase,"
        "crop=1920:1080,fps=25,setsar=1[bg];"
        "[1:v]format=rgba[ov];"
        "[bg][ov]overlay=0:0:shortest=1,format=yuv420p[v]",
        "-map", "[v]", "-an",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-crf", "23", "-r", str(FPS), "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", output_path,
    ]
    run_cmd(args, timeout=max(180, int(duration * 20)))


def write_report(rows, path):
    with open(path, "w", encoding="utf-8") as file:
        file.write("VIDEO SOURCE REPORT\n")
        file.write("Check source permissions before publication.\n\n")
        for i, row in enumerate(rows, 1):
            file.write(f"Scene {i}: {row.get('headline', '')}\n")
            file.write(f"Query: {row.get('query', '')}\n")
            file.write(f"Provider: {row.get('provider', '')}\n")
            file.write(f"Source: {row.get('source', '')}\n")
            file.write(f"Image: {row.get('image_url', '')}\n")
            file.write(f"Creator: {row.get('creator', '')}\n")
            file.write(f"License: {row.get('license', '')}\n")
            file.write(f"License link: {row.get('license_url', '')}\n")
            file.write(f"Google Images: {row.get('google_images', '')}\n")
            for article in row.get("news", []):
                file.write(f"Article: {article['title']} — {article['url']}\n")
            file.write("\n")


# -------------------- STREAMLIT INTERFACE --------------------

if not ffmpeg_ready():
    st.error("FFmpeg is missing. Add ffmpeg to packages.txt and redeploy.")
    st.stop()

FONT_PATH = find_hindi_font()
if not FONT_PATH:
    st.error(
        "No Devanagari font was found. Your app cannot render Hindi correctly. "
        "Add fonts-noto-core and fonts-noto-extra to packages.txt, then reboot."
    )
    st.stop()

st.success(f"Hindi font loaded: {os.path.basename(FONT_PATH)}")

with st.sidebar:
    st.header("Settings")
    model_size = st.selectbox(
        "Hindi speech model", ["tiny", "base", "small"], index=1
    )
    scene_seconds = st.slider(
        "Average scene length", 8, 25, 15
    )
    max_scenes = st.slider(
        "Maximum scenes", 5, 60, 35
    )

audio_file = st.file_uploader(
    "1. Upload narration audio",
    type=["mp3", "wav", "m4a", "aac", "ogg", "flac"],
)
transcript_file = st.file_uploader(
    "Optional: upload a Hindi transcript (.txt)", type=["txt"]
)

if "transcript_text" not in st.session_state:
    st.session_state.transcript_text = ""
if "scene_plan" not in st.session_state:
    st.session_state.scene_plan = ""

if audio_file:
    st.audio(audio_file)

left, right = st.columns(2)
with left:
    transcribe_clicked = st.button(
        "2. Transcribe in Hindi",
        type="primary", disabled=audio_file is None
    )
with right:
    if st.button("Clear transcript and scenes"):
        st.session_state.transcript_text = ""
        st.session_state.scene_plan = ""
        st.rerun()

if transcribe_clicked:
    try:
        with st.spinner("Transcribing audio with Hindi forced..."):
            with tempfile.TemporaryDirectory() as td:
                path = os.path.join(td, "narration")
                with open(path, "wb") as file:
                    file.write(audio_file.getbuffer())
                rows, language = transcribe_audio(path, model_size)

        st.session_state.transcript_text = "\n".join(
            row["text"] for row in rows
        )
        st.success(
            f"Transcription finished. Model language setting: Hindi. "
            f"Reported language: {language}."
        )
        st.info(
            "Please review names and spelling. Forcing Hindi improves "
            "Devanagari output but cannot guarantee every word is correct."
        )
    except Exception as error:
        st.error(f"Transcription failed: {error}")

if transcript_file:
    st.session_state.transcript_text = transcript_file.getvalue().decode(
        "utf-8", errors="replace"
    )

transcript = st.text_area(
    "3. Review or correct your Hindi transcript",
    value=st.session_state.transcript_text,
    height=200,
)
st.session_state.transcript_text = transcript

if st.button(
    "4. Create scene plan",
    type="primary",
    disabled=not (audio_file and transcript.strip()),
):
    try:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "narration")
            with open(path, "wb") as file:
                file.write(audio_file.getbuffer())
            duration = audio_length(path)

        st.session_state.scene_plan = make_scene_plan(
            transcript, duration, max_scenes, scene_seconds
        )
        st.success("Scene plan created. Review the search keywords.")
    except Exception as error:
        st.error(f"Could not create scene plan: {error}")

scene_plan = st.text_area(
    "5. Edit scenes: English search query | Hindi headline",
    value=st.session_state.scene_plan,
    height=300,
    placeholder=(
        "Supreme Court of India latest news | सुप्रीम कोर्ट का बड़ा फैसला\n"
        "Rahul Gandhi Congress latest news | राहुल गांधी का बड़ा बयान"
    ),
)
st.session_state.scene_plan = scene_plan

st.caption(
    "News article images are tried first, followed by Wikimedia Commons. "
    "When no image can be retrieved, a labelled graphic is used. "
    "Google Images links are provided for manual searching."
)

if st.button(
    "6. Generate 1080p MP4",
    type="primary",
    disabled=not (audio_file and scene_plan.strip()),
):
    scenes = []
    for line in scene_plan.splitlines():
        if not line.strip():
            continue
        parts = line.split("|", 1)
        query = parts[0].strip()
        headline = parts[1].strip() if len(parts) > 1 else make_headline(query)
        if query:
            scenes.append({"query": query, "headline": headline})

    if not scenes:
        st.error("No scenes found.")
        st.stop()

    progress = st.progress(0)
    status = st.empty()
    source_rows = []

    try:
        with tempfile.TemporaryDirectory() as td:
            audio_path = os.path.join(td, "narration")
            with open(audio_path, "wb") as file:
                file.write(audio_file.getbuffer())

            duration = audio_length(audio_path)
            each_duration = duration / len(scenes)
            scene_files = []

            for i, scene in enumerate(scenes):
                status.info(
                    f"Scene {i + 1}/{len(scenes)}: searching news images..."
                )

                found = None
                articles = []
                try:
                    found, articles = find_image_and_news(scene["query"])
                except Exception as error:
                    st.warning(f"Search failed for scene {i + 1}: {error}")

                google_url = (
                    "https://www.google.com/search?tbm=isch&q="
                    + quote_plus(scene["query"])
                )

                if found:
                    media_path = os.path.join(td, f"image_{i:03d}.jpg")
                    with open(media_path, "wb") as file:
                        file.write(found["bytes"])

                    source_row = {
                        **scene,
                        "provider": found.get("provider", "Image"),
                        "title": found.get("title", ""),
                        "source": found.get("article_url", ""),
                        "image_url": found.get("url", ""),
                        "creator": found.get("creator", "Check source"),
                        "license": found.get("license", "Unverified"),
                        "license_url": found.get("license_url", ""),
                        "news": articles,
                        "google_images": google_url,
                    }
                else:
                    media_path = os.path.join(td, f"fallback_{i:03d}.jpg")
                    create_fallback(media_path)
                    source_row = {
                        **scene,
                        "provider": "Fallback graphic — no image retrieved",
                        "title": "Illustrative graphic",
                        "source": "",
                        "image_url": "",
                        "creator": "Generated by this app",
                        "license": "Original graphic",
                        "license_url": "",
                        "news": articles,
                        "google_images": google_url,
                    }

                overlay_path = os.path.join(td, f"overlay_{i:03d}.png")
                create_overlay(scene["headline"], overlay_path)

                output_scene = os.path.join(td, f"scene_{i:03d}.mp4")
                render_scene(
                    media_path, overlay_path, output_scene, each_duration
                )

                scene_files.append(output_scene)
                source_rows.append(source_row)
                progress.progress((i + 1) / (len(scenes) + 1))

            concat_path = os.path.join(td, "concat.txt")
            with open(concat_path, "w", encoding="utf-8") as file:
                for path in scene_files:
                    file.write("file '" + path.replace("'", "'\\''") + "'\n")

            silent_path = os.path.join(td, "silent.mp4")
            run_cmd([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", concat_path,
                "-c", "copy", "-movflags", "+faststart", silent_path,
            ], timeout=900)

            final_path = os.path.join(td, "hindi_news_1080p.mp4")
            status.info("Combining scenes with narration...")
            run_cmd([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", silent_path, "-i", audio_path,
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-t", f"{duration:.3f}", "-movflags", "+faststart",
                final_path,
            ], timeout=900)

            report_path = os.path.join(td, "source_report.txt")
            write_report(source_rows, report_path)

            with open(final_path, "rb") as file:
                video_bytes = file.read()
            with open(report_path, "rb") as file:
                report_bytes = file.read()

            progress.progress(1.0)
            status.success("Video export finished.")
            st.video(video_bytes)
            st.download_button(
                "Download 1080p MP4",
                video_bytes,
                file_name="hindi_news_1080p.mp4",
                mime="video/mp4",
            )
            st.download_button(
                "Download source report",
                report_bytes,
                file_name="source_report.txt",
                mime="text/plain",
            )

            st.subheader("Scene image results")
            for i, row in enumerate(source_rows, 1):
                with st.expander(
                    f"Scene {i}: {row.get('headline', '')}"
                ):
                    st.write("Search:", row.get("query", ""))
                    st.write("Result:", row.get("provider", ""))
                    st.write("License:", row.get("license", ""))

                    if row.get("source"):
                        st.markdown(
                            f"[Open original source]({row['source']})"
                        )
                    if row.get("image_url"):
                        st.markdown(
                            f"[Open image]({row['image_url']})"
                        )
                    st.markdown(
                        f"[Search Google Images]({row['google_images']})"
                    )

                    for article in row.get("news", []):
                        st.markdown(
                            f"- [{article['title']}]({article['url']})"
                        )

            st.warning(
                "Review every image's permission before publication. "
                "Some scenes may still use fallback graphics if sources "
                "block image retrieval."
            )

    except Exception as error:
        st.error(f"Video generation failed: {error}")
        st.info(
            "Test a short narration first. Free hosting may have limited "
            "memory and processing time."
        )
