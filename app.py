
import os
import re
import json
import time
import shutil
import tempfile
import subprocess
from pathlib import Path
from urllib.parse import quote

import requests
import streamlit as st
from PIL import Image, ImageDraw, ImageFont, ImageOps
from faster_whisper import WhisperModel

st.set_page_config(
    page_title="AI News Video Generator",
    page_icon="🎬",
    layout="wide"
)

st.title("🎬 AI News Video Generator")
st.caption(
    "Hindi narration → relevant visuals → news graphics → 16:9 MP4"
)

st.warning(
    "Prototype: Wikimedia Commons images are used as visuals. "
    "This version does not yet automatically retrieve broadcast news "
    "footage or guarantee that every search result matches the event."
)

# ---------- Helpers ----------

def run_command(command):
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=1800
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-4000:])
    return result.stdout


def audio_duration(path):
    output = run_command([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path)
    ])
    return max(1.0, float(output.strip()))


@st.cache_resource
def load_whisper():
    return WhisperModel(
        "base",
        device="cpu",
        compute_type="int8"
    )


def transcribe_audio(path):
    model = load_whisper()
    segments, info = model.transcribe(
        str(path),
        language="hi",
        vad_filter=True
    )
    rows = [
        {"start": s.start, "end": s.end, "text": s.text.strip()}
        for s in segments
        if s.text.strip()
    ]
    return rows, info.language


def split_scenes(text, max_chars=180):
    # Split at sentence boundaries, then group short sentences.
    sentences = re.split(r"(?<=[.!?।])\s+", text.strip())
    scenes = []
    current = ""

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        if current and len(current) + len(sentence) > max_chars:
            scenes.append(current)
            current = sentence
        else:
            current = (current + " " + sentence).strip()

    if current:
        scenes.append(current)

    return scenes


def search_commons(query):
    endpoint = "https://commons.wikimedia.org/w/api.php"

    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": query + " filetype:bitmap",
        "gsrnamespace": 6,
        "gsrlimit": 8,
        "prop": "imageinfo",
        "iiprop": "url|extmetadata",
        "iiurlwidth": 1280
    }

    headers = {
        "User-Agent": "AINewsVideoGenerator/0.1 (educational prototype)"
    }

    response = requests.get(
        endpoint,
        params=params,
        headers=headers,
        timeout=25
    )
    response.raise_for_status()

    pages = response.json().get("query", {}).get("pages", {})
    results = []

    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        metadata = info.get("extmetadata", {})

        def clean(key):
            value = metadata.get(key, {}).get("value", "")
            return re.sub(r"<[^>]+>", "", str(value)).strip()

        image_url = info.get("thumburl") or info.get("url")
        if not image_url:
            continue

        results.append({
            "title": page.get("title", "Untitled"),
            "url": image_url,
            "source": info.get("descriptionurl", ""),
            "license": clean("LicenseShortName") or "Check source",
            "artist": clean("Artist") or "Not listed",
            "credit": clean("Credit"),
            "license_url": clean("LicenseUrl")
        })

    return results


def download_image(url, destination):
    response = requests.get(
        url,
        headers={"User-Agent": "AINewsVideoGenerator/0.1"},
        timeout=30
    )
    response.raise_for_status()

    if len(response.content) > 20_000_000:
        raise ValueError("Image is too large.")

    with open(destination, "wb") as f:
        f.write(response.content)

    image = Image.open(destination)
    image.load()
    return image.convert("RGB")


def choose_font(size):
    candidates = [
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansDevanagariUI-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    ]

    for font_path in candidates:
        if os.path.exists(font_path):
            try:
                return ImageFont.truetype(font_path, size)
            except Exception:
                pass

    return ImageFont.load_default()


def make_graphic(source_image, headline, output_path):
    image = ImageOps.fit(
        source_image,
        (1280, 720),
        method=Image.Resampling.LANCZOS
    ).convert("RGB")

    # Darken the lower area for text contrast.
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw_overlay = ImageDraw.Draw(overlay)

    for y in range(440, 720):
        alpha = int(220 * (y - 440) / 280)
        draw_overlay.line(
            [(0, y), (1280, y)],
            fill=(0, 0, 0, alpha)
        )

    image = Image.alpha_composite(
        image.convert("RGBA"), overlay
    )
    draw = ImageDraw.Draw(image)

    draw.rectangle(
        [(0, 0), (1280, 66)],
        fill=(180, 15, 20, 240)
    )
    draw.text(
        (30, 13),
        "BREAKING NEWS",
        font=choose_font(34),
        fill="white",
        stroke_width=1,
        stroke_fill="black"
    )

    # Keep graphics readable on a mobile screen.
    headline = headline.strip()
    if len(headline) > 125:
        headline = headline[:122] + "..."

    words = headline.split()
    lines = []
    line = ""

    for word in words:
        test = (line + " " + word).strip()
        if len(test) > 30 and line:
            lines.append(line)
            line = word
        else:
            line = test

    if line:
        lines.append(line)

    lines = lines[-3:]
    font = choose_font(43)
    y = 535 - (len(lines) - 1) * 52

    for line in lines:
        draw.text(
            (36, y),
            line,
            font=font,
            fill="white",
            stroke_width=2,
            stroke_fill="black"
        )
        y += 54

    image.convert("RGB").save(output_path, quality=92)


# ---------- User interface ----------

audio_file = st.file_uploader(
    "1. Upload Hindi news narration",
    type=["mp3", "wav", "m4a", "aac", "ogg"]
)

st.write("2. Transcription")
transcription_mode = st.radio(
    "How should the app obtain the narration text?",
    [
        "Automatically transcribe audio",
        "I will paste the transcript"
    ]
)

transcript_input = st.text_area(
    "Paste transcript (used in manual mode)",
    height=180,
    placeholder="अपनी हिंदी न्यूज़ स्क्रिप्ट यहाँ पेस्ट करें..."
)

st.write("3. Video settings")
headline_prefix = st.text_input(
    "Top banner",
    value="BREAKING NEWS"
)

fps = st.selectbox("Frame rate", [24, 25, 30], index=1)

st.caption(
    "This prototype renders a 720p video for faster free-cloud testing. "
    "The next rendering upgrade can add 1080p output."
)

if st.button("🚀 Generate news video", type="primary"):
    if audio_file is None:
        st.error("Please upload an audio file first.")
        st.stop()

    if transcription_mode == "I will paste the transcript":
        if not transcript_input.strip():
            st.error("Please paste the transcript.")
            st.stop()

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        st.error("FFmpeg/FFprobe is unavailable on this server.")
        st.stop()

    with tempfile.TemporaryDirectory() as temp_dir:
        work = Path(temp_dir)
        audio_path = work / ("narration" + Path(audio_file.name).suffix)
        audio_path.write_bytes(audio_file.getvalue())

        try:
            duration = audio_duration(audio_path)

            if transcription_mode == "Automatically transcribe audio":
                with st.spinner("Transcribing narration. First run may take time..."):
                    transcript_rows, detected_language = transcribe_audio(
                        audio_path
                    )
                    transcript = " ".join(
                        row["text"] for row in transcript_rows
                    )
                st.success(f"Detected language: {detected_language}")
            else:
                transcript = transcript_input.strip()

            scenes = split_scenes(transcript)

            if not scenes:
                st.error("No usable text was found.")
                st.stop()

            # Avoid excessive API calls and long cloud renders.
            if len(scenes) > 30:
                scenes = [
                    " ".join(scenes[i:i + 3])
                    for i in range(0, len(scenes), 3)
                ]

            st.write(f"Audio duration: {duration / 60:.2f} minutes")
            st.write(f"Scenes planned: {len(scenes)}")

            records = []
            progress = st.progress(0)

            for index, scene in enumerate(scenes):
                st.write(f"Searching visual {index + 1}/{len(scenes)}")

                # Search a short meaningful portion of each scene.
                query = " ".join(scene.split()[:7])
                try:
                    results = search_commons(query)
                except Exception:
                    results = []

                selected = results[0] if results else None

                if selected:
                    try:
                        img = download_image(
                            selected["url"],
                            work / f"source_{index}.img"
                        )
                    except Exception:
                        selected = None

                if not selected:
                    # Safe fallback: create a graphic without claiming
                    # it depicts the event.
                    img = Image.new(
                        "RGB", (1280, 720), (35, 40, 55)
                    )

                graphic_path = work / f"scene_{index:03d}.jpg"
                make_graphic(img, scene, graphic_path)

                records.append({
                    "scene": scene,
                    "visual": selected,
                    "graphic": graphic_path
                })

                progress.progress((index + 1) / len(scenes))

            # Keep all scenes synchronized to the narration duration.
            scene_duration = duration / len(records)
            concat_path = work / "concat.txt"

            with open(concat_path, "w", encoding="utf-8") as f:
                for record in records:
                    path = str(record["graphic"]).replace("\\", "/")
                    path = path.replace("'", "'\\''")
                    f.write(f"file '{path}'\n")
                    f.write(f"duration {scene_duration:.6f}\n")

                last_path = str(records[-1]["graphic"]).replace("\\", "/")
                last_path = last_path.replace("'", "'\\''")
                f.write(f"file '{last_path}'\n")

            output_path = work / "news_video.mp4"

            with st.spinner("Rendering video. Please keep this page open..."):
                run_command([
                    "ffmpeg", "-y",
                    "-f", "concat", "-safe", "0",
                    "-i", str(concat_path),
                    "-i", str(audio_path),
                    "-map", "0:v:0",
                    "-map", "1:a:0",
                    "-vf", f"fps={fps},scale=1280:720,format=yuv420p",
                    "-c:v", "libx264",
                    "-preset", "ultrafast",
                    "-crf", "26",
                    "-c:a", "aac",
                    "-b:a", "128k",
                    "-t", str(duration),
                    "-movflags", "+faststart",
                    str(output_path)
                ])

            # Export asset details for license review.
            manifest = []
            for record in records:
                visual = record["visual"]
                manifest.append({
                    "scene_text": record["scene"],
                    "source_title": visual["title"] if visual else "Generated fallback graphic",
                    "source_page": visual["source"] if visual else "",
                    "license": visual["license"] if visual else "Original graphic",
                    "artist": visual["artist"] if visual else "",
                    "license_url": visual["license_url"] if visual else ""
                })

            st.success("Video generated!")
            st.video(str(output_path))

            with open(output_path, "rb") as f:
                video_bytes = f.read()

            st.download_button(
                "⬇️ Download news video (MP4)",
                data=video_bytes,
                file_name="hindi_news_video.mp4",
                mime="video/mp4"
            )

            st.download_button(
                "⬇️ Download visual source and license report",
                data=json.dumps(
                    manifest, ensure_ascii=False, indent=2
                ),
                file_name="visual_sources_and_licenses.json",
                mime="application/json"
            )

            st.info(
                "Review every visual and its license before publishing. "
                "Search results can be irrelevant, and metadata may be "
                "incomplete. This first version uses still images rather "
                "than downloaded news video clips."
            )

        except Exception as error:
            st.error(f"Generation failed: {error}")
            st.info(
                "If this happened during transcription, try manual "
                "transcript mode. If rendering failed, check the app logs."
            )
