
import os
import re
import glob
import html
import time
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from io import BytesIO
from urllib.parse import quote_plus, urljoin, urlparse

import requests
import streamlit as st
from PIL import Image, ImageDraw, ImageFont

APP_TITLE = "Hindi News Video Generator"
W, H = 1920, 1080
FPS = 25
MAX_IMAGE_MB = 15
MAX_VIDEO_MB = 100
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
)

st.set_page_config(page_title=APP_TITLE, page_icon="🎬", layout="wide")
st.title("Hindi News Video Generator")
st.caption(
    "Upload Hindi narration, find relevant news images, "
    "add Hindi headlines, and export a 1080p MP4."
)

st.info(
    "No API key is required. The app searches Google News RSS and attempts "
    "to retrieve images from related news articles. If an image cannot be "
    "retrieved, it creates a labelled fallback graphic and provides a "
    "Google Images search link. Image reuse permissions are not guaranteed."
)


def run_cmd(cmd, timeout=600):
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            (result.stderr or result.stdout or "Command failed")[-4000:]
        )
    return result.stdout.strip()


def check_ffmpeg():
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def audio_duration(path):
    value = run_cmd([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ], timeout=60)
    return max(1.0, float(value))


def transcribe_audio(path, model_size):
    from faster_whisper import WhisperModel

    model = WhisperModel(
        model_size,
        device="cpu",
        compute_type="int8",
    )

    segments, info = model.transcribe(
        path,
        language="hi",
        task="transcribe",
        beam_size=5,
        vad_filter=True,
    )

    rows = []
    for seg in segments:
        text = (seg.text or "").strip()
        if text:
            rows.append({
                "start": float(seg.start),
                "end": float(seg.end),
                "text": text,
            })

    return rows, getattr(info, "language", "hi")


def find_font():
    """Find a font intended for Devanagari; never silently select Latin-only."""

    patterns = [
        "/usr/share/fonts/**/*Devanagari*.ttf",
        "/usr/share/fonts/**/*devanagari*.ttf",
        "/usr/share/fonts/**/*Devanagari*.otf",
        "/usr/share/fonts/**/*devanagari*.otf",
    ]

    for pattern in patterns:
        for path in glob.glob(pattern, recursive=True):
            try:
                ImageFont.truetype(path, 40)
                return path
            except Exception:
                continue

    # Check a known Noto font location if available.
    known = [
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
    ]
    for path in known:
        if os.path.isfile(path):
            try:
                ImageFont.truetype(path, 40)
                return path
            except Exception:
                pass

    return None


def get_font(font_path, size):
    if not font_path:
        raise RuntimeError(
            "Hindi font not found. Add fonts-noto-core to packages.txt "
            "and reboot the Streamlit app."
        )
    return ImageFont.truetype(font_path, size)


def wrap_text(draw, text, font, max_width, max_lines=3):
    words = (text or "").split()
    lines = []
    line = ""

    for word in words:
        candidate = word if not line else line + " " + word
        width = draw.textbbox((0, 0), candidate, font=font)[2]

        if width <= max_width:
            line = candidate
        else:
            if line:
                lines.append(line)
            line = word

    if line:
        lines.append(line)

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip("।, ") + "…"

    return lines


def make_overlay(headline, output_path, font_path):
    image = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    label_font = get_font(font_path, 42)
    headline_font = get_font(font_path, 68)
    small_font = get_font(font_path, 28)

    draw.rounded_rectangle(
        (65, 55, 510, 145),
        radius=18,
        fill=(190, 0, 0, 240),
    )
    draw.text(
        (90, 72),
        "ताज़ा खबर",
        font=label_font,
        fill="white",
        stroke_width=1,
        stroke_fill="black",
    )

    draw.rectangle((0, 790, W, H), fill=(0, 0, 0, 215))
    draw.rectangle((0, 790, W, 808), fill=(255, 190, 0, 255))

    lines = wrap_text(draw, headline, headline_font, W - 140, 3)
    y = 820 if len(lines) <= 2 else 810

    for line in lines:
        draw.text(
            (70, y),
            line,
            font=headline_font,
            fill="white",
            stroke_width=2,
            stroke_fill="black",
        )
        y += 78

    draw.text(
        (70, 1030),
        "समाचार और विश्लेषण",
        font=small_font,
        fill=(255, 215, 80, 255),
    )

    image.save(output_path)


def make_fallback_card(output_path, font_path):
    """Create a background only; headline is drawn by the overlay once."""

    image = Image.new("RGB", (W, H), (25, 38, 60))
    draw = ImageDraw.Draw(image)
    small_font = get_font(font_path, 44)

    draw.rectangle((0, 0, W, 28), fill=(210, 0, 0))
    draw.text(
        (90, 100),
        "NEWS EXPLAINER",
        font=small_font,
        fill=(255, 200, 0),
    )
    draw.text(
        (90, 180),
        "Illustrative graphic — not actual event footage",
        font=get_font(font_path, 30),
        fill=(225, 230, 240),
    )
    image.save(output_path)


def search_news_links(query, limit=5):
    """Discover recent articles using Google News RSS."""

    url = (
        "https://news.google.com/rss/search?q="
        + quote_plus(query)
        + "&hl=hi&gl=IN&ceid=IN:hi"
    )

    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    response.raise_for_status()

    root = ET.fromstring(response.content)
    articles = []

    for item in root.findall(".//item")[:limit]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()

        if title and link:
            articles.append({
                "title": title,
                "url": link,
                "published": pub_date,
            })

    return articles


def extract_image_from_article(article):
    """
    Try to obtain the main image from an article's Open Graph metadata.
    Some publishers block requests or do not expose their images.
    """

    try:
        response = requests.get(
            article["url"],
            headers={"User-Agent": USER_AGENT},
            timeout=18,
            allow_redirects=True,
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
            match = re.search(pattern, page, re.IGNORECASE)
            if match:
                image_url = html.unescape(match.group(1).strip())
                break

        if not image_url:
            return None

        image_url = urljoin(response.url, image_url)
        parsed = urlparse(image_url)

        if parsed.scheme not in ("http", "https"):
            return None

        with requests.get(
            image_url,
            headers={"User-Agent": USER_AGENT},
            timeout=20,
            stream=True,
        ) as image_response:

            image_response.raise_for_status()

            content_type = image_response.headers.get(
                "Content-Type", ""
            ).lower()

            if not content_type.startswith("image/"):
                return None

            data = bytearray()
            for chunk in image_response.iter_content(64 * 1024):
                if not chunk:
                    continue
                data.extend(chunk)

                if len(data) > MAX_IMAGE_MB * 1024 * 1024:
                    return None

        if len(data) < 5000:
            return None

        try:
            with Image.open(BytesIO(data)) as img:
                img.load()
                if img.width < 320 or img.height < 180:
                    return None
                # Convert unusual image formats into standard JPEG.
                converted = img.convert("RGB")
                buffer = BytesIO()
                converted.save(buffer, format="JPEG", quality=92)
                image_bytes = buffer.getvalue()
        except Exception:
            return None

        return {
            "image_bytes": image_bytes,
            "image_url": image_url,
            "article_url": response.url,
            "title": article["title"],
            "published": article.get("published", ""),
        }

    except Exception:
        return None


def find_news_image(query):
    articles = search_news_links(query, limit=5)

    for article in articles:
        result = extract_image_from_article(article)
        if result:
            result["provider"] = "News article image"
            result["creator"] = "See original article"
            result["license"] = "Not verified; permission must be checked"
            result["source"] = result["article_url"]
            result["license_url"] = ""
            result["kind"] = "image"
            return result, articles

    return None, articles


def download_file(url, destination, max_mb=MAX_VIDEO_MB):
    total = 0

    with requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        stream=True,
        timeout=(20, 90),
    ) as response:
        response.raise_for_status()

        with open(destination, "wb") as output:
            for chunk in response.iter_content(1024 * 1024):
                if not chunk:
                    continue

                total += len(chunk)
                if total > max_mb * 1024 * 1024:
                    raise RuntimeError("Video exceeds the download size limit.")

                output.write(chunk)

    if os.path.getsize(destination) < 20000:
        raise RuntimeError("Downloaded video is too small to use.")

    return destination


def render_scene(media_path, overlay_path, output_path, duration, is_image):
    if is_image:
        media_args = ["-loop", "1", "-framerate", str(FPS), "-i", media_path]
    else:
        media_args = ["-stream_loop", "-1", "-i", media_path]

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
    ] + media_args + [
        "-loop", "1", "-i", overlay_path,
        "-t", f"{duration:.3f}",
        "-filter_complex",
        "[0:v]scale=1920:1080:force_original_aspect_ratio=increase,"
        "crop=1920:1080,fps=25,setsar=1[bg];"
        "[1:v]format=rgba[ov];"
        "[bg][ov]overlay=0:0:shortest=1,format=yuv420p[v]",
        "-map", "[v]",
        "-an",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "23",
        "-r", str(FPS),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        output_path,
    ]

    run_cmd(cmd, timeout=max(180, int(duration * 20)))


def make_source_report(rows, output_path):
    with open(output_path, "w", encoding="utf-8") as file:
        file.write("HINDI NEWS VIDEO — SOURCE REPORT\n")
        file.write("Verify image permissions before publication.\n\n")

        for index, row in enumerate(rows, 1):
            file.write(f"Scene {index}: {row.get('headline', '')}\n")
            file.write(f"Search query: {row.get('query', '')}\n")
            file.write(f"Provider: {row.get('provider', '')}\n")
            file.write(f"Source: {row.get('source', '')}\n")
            file.write(f"Image URL: {row.get('image_url', '')}\n")
            file.write(f"License: {row.get('license', '')}\n")
            file.write(f"Google Images: {row.get('google_images', '')}\n")

            for article in row.get("news", []):
                file.write(
                    f"Related article: {article['title']} — {article['url']}\n"
                )

            file.write("\n")


if not check_ffmpeg():
    st.error(
        "FFmpeg is missing. Make sure packages.txt contains ffmpeg "
        "and redeploy the app."
    )
    st.stop()

font_path = find_font()
if not font_path:
    st.error(
        "A Devanagari font was not found. Add fonts-noto-core to "
        "packages.txt and reboot the Streamlit app."
    )
    st.stop()

with st.sidebar:
    st.header("Settings")
    model_size = st.selectbox(
        "Speech transcription model",
        ["tiny", "base", "small"],
        index=1,
    )
    scene_seconds = st.slider(
        "Scene length (seconds)",
        min_value=8,
        max_value=25,
        value=15,
    )
    max_scenes = st.slider(
        "Maximum scenes",
        min_value=5,
        max_value=60,
        value=35,
    )

audio_file = st.file_uploader(
    "1. Upload Hindi narration",
    type=["mp3", "wav", "m4a", "aac", "ogg", "flac"],
)

transcript_upload = st.file_uploader(
    "Optional: upload Hindi transcript (.txt)",
    type=["txt"],
)

if audio_file:
    st.audio(audio_file)

if "transcript_text" not in st.session_state:
    st.session_state.transcript_text = ""

if "scene_plan" not in st.session_state:
    st.session_state.scene_plan = ""

col1, col2 = st.columns(2)

with col1:
    transcribe_clicked = st.button(
        "2. Transcribe audio",
        type="primary",
        disabled=audio_file is None,
    )

with col2:
    clear_clicked = st.button("Clear transcript")

if clear_clicked:
    st.session_state.transcript_text = ""
    st.session_state.scene_plan = ""
    st.rerun()

if transcribe_clicked:
    with st.spinner("Transcribing audio in Hindi..."):
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                audio_path = os.path.join(temp_dir, "narration_audio")
                with open(audio_path, "wb") as file:
                    file.write(audio_file.getbuffer())

                rows, detected_language = transcribe_audio(
                    audio_path, model_size
                )

            st.session_state.transcript_text = "\n".join(
                row["text"] for row in rows
            )
            st.success(
                f"Transcription complete. Language: {detected_language}"
            )

        except Exception as error:
            st.error(f"Transcription failed: {error}")
            st.info("You can upload a transcript as a .txt file instead.")

if transcript_upload is not None:
    st.session_state.transcript_text = transcript_upload.getvalue().decode(
        "utf-8", errors="replace"
    )

transcript_text = st.text_area(
    "3. Review and edit the Hindi transcript",
    value=st.session_state.transcript_text,
    height=200,
    placeholder="Your Hindi narration transcript appears here.",
)
st.session_state.transcript_text = transcript_text


def suggest_query(text):
    hints = [
        (r"मोदी|प्रधानमंत्री|पीएम", "Narendra Modi Indian government latest news"),
        (r"अमित शाह|गृह मंत्री", "Amit Shah Indian home minister latest news"),
        (r"राहुल गांधी|कांग्रेस", "Rahul Gandhi Congress latest news India"),
        (r"भाजपा|बीजेपी|BJP", "BJP Indian politics latest news"),
        (r"सुप्रीम कोर्ट|अदालत|कोर्ट|जज", "Supreme Court of India latest news"),
        (r"चुनाव|वोटर|मतदान|चुनाव आयोग", "India election commission voting news"),
        (r"संसद|लोकसभा|राज्यसभा", "Indian parliament latest news"),
        (r"पुलिस|गिरफ्तार|लाठीचार्ज", "Indian police arrest protest latest news"),
        (r"प्रदर्शन|धरना|आंदोलन|रैली", "India protest rally latest news"),
        (r"बंगाल|ममता", "West Bengal Mamata Banerjee latest news"),
        (r"युद्ध|सेना|मिसाइल|सीमा", "international conflict military latest news"),
        (r"ट्रंप|अमेरिका|अमेरिकी", "Donald Trump White House latest news"),
        (r"चीन|चीनी", "China government latest news"),
        (r"किसान", "Indian farmers protest latest news"),
        (r"मौसम|बारिश|बाढ़", "India weather rain flood latest news"),
        (r"एआई|कृत्रिम बुद्धिमत्ता|तकनीक", "AI technology latest news"),
    ]

    for pattern, query in hints:
        if re.search(pattern, text or "", re.I):
            return query

    english = re.findall(r"[A-Za-z][A-Za-z -]{2,}", text or "")
    useful = [
        item.strip()
        for item in english
        if item.strip().lower() not in {
            "the", "and", "for", "with", "from", "this", "that", "news"
        }
    ]

    if useful:
        return " ".join(useful[:5]) + " India latest news"

    return "India government latest news"


def make_headline(text, limit=85):
    text = re.sub(r"\s+", " ", text or "").strip()

    if len(text) <= limit:
        return text or "ताज़ा खबर"

    shortened = text[:limit]
    if " " in shortened:
        shortened = shortened.rsplit(" ", 1)[0]

    return shortened.rstrip(" ,।-") + "…"


if st.button(
    "4. Create scene plan",
    type="primary",
    disabled=not (audio_file and transcript_text.strip()),
):
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = os.path.join(temp_dir, "narration_audio")
            with open(audio_path, "wb") as file:
                file.write(audio_file.getbuffer())
            duration = audio_duration(audio_path)

        sentences = [
            item.strip()
            for item in re.split(r"(?<=[।!?])\s+|\n+", transcript_text)
            if item.strip()
        ]

        count = max(
            1,
            min(max_scenes, int(duration // scene_seconds) + 1),
        )
        count = min(count, max(1, len(sentences)))

        chunk_size = max(1, (len(sentences) + count - 1) // count)
        groups = [
            " ".join(sentences[i:i + chunk_size])
            for i in range(0, len(sentences), chunk_size)
        ]

        lines = []
        for group in groups:
            lines.append(
                f"{suggest_query(group)} | {make_headline(group)}"
            )

        st.session_state.scene_plan = "\n".join(lines)
        st.success(f"Created {len(lines)} scene suggestions.")

    except Exception as error:
        st.error(f"Could not create the scene plan: {error}")

scene_plan = st.text_area(
    "5. Edit scenes — one per line: English search keywords | Hindi headline",
    value=st.session_state.scene_plan,
    height=300,
    placeholder=(
        "Indian parliament latest news | संसद में बड़ी हलचल\n"
        "Supreme Court of India latest news | सुप्रीम कोर्ट का बड़ा फैसला"
    ),
)
st.session_state.scene_plan = scene_plan

st.caption(
    "The app first searches related news articles and attempts to retrieve "
    "their main images. If that fails, it creates a labelled graphic. "
    "Review every source and its reuse permission before publishing."
)

generate = st.button(
    "6. Generate 1080p MP4",
    type="primary",
    disabled=not (audio_file and scene_plan.strip()),
)

if generate:
    parsed = []

    for line in scene_plan.splitlines():
        line = line.strip()
        if not line:
            continue

        parts = line.split("|", 1)
        query = parts[0].strip()
        headline = (
            parts[1].strip()
            if len(parts) > 1
            else make_headline(query)
        )

        if query:
            parsed.append({
                "query": query,
                "headline": headline,
            })

    if not parsed:
        st.error("Add at least one scene.")
        st.stop()

    progress = st.progress(0)
    status = st.empty()
    sources = []

    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = os.path.join(temp_dir, "narration_audio")
            with open(audio_path, "wb") as file:
                file.write(audio_file.getbuffer())

            total_duration = audio_duration(audio_path)
            scene_duration = total_duration / len(parsed)

            scene_files = []

            for index, item in enumerate(parsed):
                status.info(
                    f"Scene {index + 1}/{len(parsed)}: "
                    f"searching {item['query']}"
                )

                image_result, articles = None, []
                try:
                    image_result, articles = find_news_image(item["query"])
                except Exception as error:
                    st.warning(
                        f"News search failed for scene {index + 1}: {error}"
                    )

                google_images_url = (
                    "https://www.google.com/search?tbm=isch&q="
                    + quote_plus(item["query"])
                )

                if image_result:
                    media_path = os.path.join(
                        temp_dir, f"image_{index:03d}.jpg"
                    )
                    with open(media_path, "wb") as file:
                        file.write(image_result["image_bytes"])

                    is_image = True
                    source_row = {
                        **item,
                        **image_result,
                        "news": articles,
                        "google_images": google_images_url,
                    }

                else:
                    media_path = os.path.join(
                        temp_dir, f"fallback_{index:03d}.jpg"
                    )
                    make_fallback_card(media_path, font_path)
                    is_image = True

                    source_row = {
                        **item,
                        "provider": "Generated fallback graphic",
                        "title": "Illustrative background",
                        "creator": "Generated by this app",
                        "license": "Original graphic",
                        "source": "",
                        "image_url": "",
                        "news": articles,
                        "google_images": google_images_url,
                    }

                overlay_path = os.path.join(
                    temp_dir, f"overlay_{index:03d}.png"
                )
                make_overlay(item["headline"], overlay_path, font_path)

                scene_path = os.path.join(
                    temp_dir, f"scene_{index:03d}.mp4"
                )
                render_scene(
                    media_path,
                    overlay_path,
                    scene_path,
                    scene_duration,
                    is_image,
                )

                scene_files.append(scene_path)
                sources.append(source_row)
                progress.progress((index + 1) / (len(parsed) + 1))

            concat_file = os.path.join(temp_dir, "concat.txt")
            with open(concat_file, "w", encoding="utf-8") as file:
                for path in scene_files:
                    safe_path = path.replace("'", "'\\''")
                    file.write(f"file '{safe_path}'\n")

            silent_video = os.path.join(temp_dir, "silent_video.mp4")

            run_cmd([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", concat_file,
                "-c", "copy", "-movflags", "+faststart", silent_video,
            ], timeout=900)

            output_path = os.path.join(
                temp_dir, "hindi_news_1080p.mp4"
            )

            status.info("Adding narration and exporting final video...")

            run_cmd([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", silent_video,
                "-i", audio_path,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-t", f"{total_duration:.3f}",
                "-movflags", "+faststart",
                output_path,
            ], timeout=900)

            report_path = os.path.join(temp_dir, "source_report.txt")
            make_source_report(sources, report_path)

            with open(output_path, "rb") as file:
                video_bytes = file.read()

            with open(report_path, "rb") as file:
                report_bytes = file.read()

            progress.progress(1.0)
            status.success("Video generated successfully.")

            st.video(video_bytes)

            st.download_button(
                "Download 1080p MP4",
                data=video_bytes,
                file_name="hindi_news_1080p.mp4",
                mime="video/mp4",
            )

            st.download_button(
                "Download source report",
                data=report_bytes,
                file_name="source_report.txt",
                mime="text/plain",
            )

            st.subheader("Scene sources")

            for index, row in enumerate(sources, 1):
                with st.expander(
                    f"Scene {index}: {row.get('headline', '')}"
                ):
                    st.write("Search query:", row.get("query", ""))
                    st.write("Media source:", row.get("provider", ""))
                    st.write("License:", row.get("license", ""))

                    if row.get("source"):
                        st.markdown(
                            f"[Open original article]({row['source']})"
                        )

                    if row.get("image_url"):
                        st.markdown(
                            f"[Open original image]({row['image_url']})"
                        )

                    st.markdown(
                        f"[Search Google Images]({row['google_images']})"
                    )

                    if row.get("news"):
                        st.write("Related news articles:")
                        for article in row["news"]:
                            st.markdown(
                                f"- [{article['title']}]({article['url']})"
                            )

            st.warning(
                "Some scenes may use fallback graphics. Check the scene "
                "sources and image reuse permissions before uploading."
            )

    except Exception as error:
        st.error(f"Video generation failed: {error}")
        st.info(
            "Try a short 30–60 second audio file first. Free hosting can "
            "run out of memory or time when rendering long videos."
        )
