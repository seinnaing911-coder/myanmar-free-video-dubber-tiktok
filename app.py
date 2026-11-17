import os, re, json, time, tempfile, subprocess, shutil, asyncio
from functools import lru_cache
from pathlib import Path

import streamlit as st
import requests
import edge_tts

APP_TITLE = "🎬 Myanmar Free Video Dubber"
MAX_MB = 500
GROQ_STT_MODEL = "whisper-large-v3-turbo"
GEMINI_MODEL = "gemini-3.8-flash"
OPENROUTER_MODEL = "openrouter/free"
XAI_STT_MODEL = "grok-voice-transcribe-2.0"
XAI_TTS_VOICE = "eve"
XAI_TTS_LANGUAGE = "my"
LOCAL_WHISPER_MODEL = "tiny"
CHUNK_SECONDS = 480

st.set_page_config(page_title=APP_TITLE, page_icon="🎬", layout="centered")


def secret(name, default=""):
    try:
        return str(st.secrets.get(name, default) or default).strip()
    except Exception:
        return os.getenv(name, default).strip()


def run_cmd(args, timeout=900):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout)[-2500:])
    return p.stdout.strip()


def validate_final_video(path, expected_duration):
    if not path.exists() or path.stat().st_size < 10_000:
        raise RuntimeError("Final MP4 ဖိုင်မပြည့်စုံပါ။ Output file size ကို စစ်ဆေးပါ။")
    raw = run_cmd(["ffprobe", "-v", "error", "-show_entries",
                   "stream=codec_type,duration", "-of", "json", str(path)])
    streams = json.loads(raw).get("streams") or []
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if not video_streams:
        raise RuntimeError("Final MP4 ထဲတွင် Video stream မတွေ့ပါ။")
    if not audio_streams:
        raise RuntimeError("Final MP4 ထဲတွင် မြန်မာအသံ Audio stream မတွေ့ပါ။")
    audio_duration = float(audio_streams[0].get("duration") or 0)
    if audio_duration < 0.25:
        raise RuntimeError("Final MP4 Audio duration မမှန်ပါ။ အသံဖိုင်မထွက်လာနိုင်ပါ။")
    probe = subprocess.run(
        [ffmpeg_path(), "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=300)
    volume_text = probe.stdout + probe.stderr
    match = re.search(r"mean_volume:\s*(-?[0-9.]+) dB", volume_text)
    mean_volume = float(match.group(1)) if match else -100.0
    if mean_volume <= -50:
        raise RuntimeError("Final MP4 အသံသည် silent ဖြစ်နေပါသည်။")
    duration_note = f"{audio_duration:.1f}s audio, mean volume {mean_volume:.1f} dB"
    if expected_duration > 0 and audio_duration < min(0.25, expected_duration):
        raise RuntimeError("Final MP4 အသံကြာချိန် မပြည့်စုံပါ။")
    return duration_note


def ffmpeg_path():
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("FFmpeg မတွေ့ပါ။ packages.txt ထဲတွင် ffmpeg ထည့်ထားကြောင်း စစ်ပါ။")
    return path


def probe_duration(path):
    value = run_cmd(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    return max(0.1, float(value))


def has_audio_stream(path):
    raw = run_cmd(["ffprobe", "-v", "error", "-select_streams", "a:0",
                   "-show_entries", "stream=index", "-of", "json", str(path)])
    return bool(json.loads(raw).get("streams") or [])


def extract_audio(video_path, audio_path):
    # Original audio is extracted only for transcription. It is never copied to the final MP4.
    if not has_audio_stream(video_path):
        raise RuntimeError("ဒီ Video ထဲမှာ မူရင်းအသံ Track မရှိပါ။ စကားပြောသံပါဝင်သော Video ကို တင်ပေးပါ။")
    run_cmd([ffmpeg_path(), "-y", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "libmp3lame", "-b:a", "48k", str(audio_path)], timeout=1800)


def split_audio(audio_path, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    run_cmd([ffmpeg_path(), "-y", "-i", str(audio_path), "-f", "segment",
             "-segment_time", str(CHUNK_SECONDS), "-reset_timestamps", "1",
             "-c:a", "libmp3lame", "-b:a", "48k", str(out_dir / "chunk_%03d.mp3")], timeout=1800)
    return sorted(out_dir.glob("chunk_*.mp3"))


def groq_transcribe(audio_path, api_key):
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {api_key}"}
    chunks = split_audio(audio_path, audio_path.parent / "audio_chunks")
    result = []
    for index, chunk in enumerate(chunks):
        with open(chunk, "rb") as file_obj:
            response = requests.post(
                url, headers=headers,
                data={"model": GROQ_STT_MODEL, "response_format": "verbose_json", "timestamp_granularities[]": "segment"},
                files={"file": (chunk.name, file_obj, "audio/mpeg")}, timeout=600)
        if response.status_code >= 400:
            raise RuntimeError(f"Groq STT error {response.status_code}: {response.text[:800]}")
        data = response.json()
        offset = index * CHUNK_SECONDS
        segments = data.get("segments") or []
        if segments:
            for item in segments:
                text = (item.get("text") or "").strip()
                if text:
                    result.append({"start": float(item.get("start", 0)) + offset,
                                   "end": float(item.get("end", item.get("start", 0) + 2)) + offset,
                                   "text": text})
        else:
            # Fallback for providers that return text but no segment timestamps.
            chunk_duration = probe_duration(chunk)
            text = (data.get("text") or "").strip()
            if text:
                result.extend(approximate_segments(text, offset, chunk_duration))
    return normalize_segments(result, probe_duration(audio_path))


def grok_transcribe(audio_path, api_key):
    """Transcribe audio with xAI's batch Grok Voice API."""
    url = "https://api.x.ai/v1/stt"
    chunks = split_audio(audio_path, audio_path.parent / "grok_audio_chunks")
    result = []
    for index, chunk in enumerate(chunks):
        with open(chunk, "rb") as file_obj:
            response = requests.post(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                data={"model": XAI_STT_MODEL},
                files={"file": (chunk.name, file_obj, "audio/mpeg")},
                timeout=900,
            )
        if response.status_code >= 400:
            raise RuntimeError(f"Grok STT error {response.status_code}: {response.text[:800]}")
        data = response.json()
        offset = index * CHUNK_SECONDS
        segments = data.get("segments") or []
        if segments:
            for item in segments:
                text = (item.get("text") or "").strip()
                if text:
                    result.append({"start": float(item.get("start", 0)) + offset,
                                   "end": float(item.get("end", item.get("start", 0) + 2)) + offset,
                                   "text": text})
        else:
            text = (data.get("text") or data.get("transcript") or "").strip()
            if text:
                result.extend(approximate_segments(text, offset, probe_duration(chunk)))
    return normalize_segments(result, probe_duration(audio_path))


def approximate_segments(text, offset, duration):
    parts = [p.strip() for p in re.split(r"(?<=[.!?。！？])\s+|\n+", text) if p.strip()]
    if not parts:
        parts = [text.strip()] if text.strip() else []
    if not parts:
        return []
    weights = [max(1, len(p)) for p in parts]
    total = sum(weights)
    cursor = 0.0
    result = []
    for part, weight in zip(parts, weights):
        span = max(1.0, duration * weight / total)
        result.append({"start": offset + cursor, "end": offset + min(duration, cursor + span), "text": part})
        cursor += span
    return result


def local_whisper_transcribe(audio_path):
    from faster_whisper import WhisperModel
    model = WhisperModel(secret("LOCAL_WHISPER_MODEL", LOCAL_WHISPER_MODEL),
                         device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(audio_path), beam_size=3, vad_filter=True)
    return normalize_segments([{"start": s.start, "end": s.end, "text": s.text.strip()}
                               for s in segments if s.text.strip()], probe_duration(audio_path))


def normalize_segments(segments, duration):
    clean = []
    for item in sorted(segments, key=lambda x: float(x.get("start", 0))):
        text = re.sub(r"\s+", " ", str(item.get("text", ""))).strip()
        if not text:
            continue
        start = max(0.0, min(float(duration), float(item.get("start", 0))))
        end = max(start + 0.25, min(float(duration), float(item.get("end", start + 2))))
        if start >= duration:
            continue
        clean.append({"start": start, "end": end, "text": text})
    return clean


def parse_json_array(raw):
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
    try:
        data = json.loads(raw)
    except Exception:
        match = re.search(r"\[.*\]", raw, flags=re.S)
        if not match:
            raise RuntimeError("AI ဘာသာပြန်ရလဒ်ကို JSON အဖြစ် ဖတ်မရပါ။")
        data = json.loads(match.group(0))
    if isinstance(data, dict):
        data = data.get("translations") or data.get("items") or []
    if not isinstance(data, list):
        raise RuntimeError("AI ဘာသာပြန်ရလဒ်ပုံစံ မမှန်ပါ။")
    return data


def contains_burmese(text):
    return bool(re.search(r"[\u1000-\u109F]", str(text or "")))


def call_grok_api(prompt, api_key):
    response = requests.post(
        "https://api.x.ai/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": "grok-4.7", "messages": [{"role": "user", "content": prompt}],
              "temperature": 0.2},
        timeout=300,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Grok translation error {response.status_code}: {response.text[:700]}")
    data = response.json()
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")


def call_translation_api(prompt, provider, api_key):
    if provider == "Grok":
        return call_grok_api(prompt, api_key)
    if provider == "Gemini":
        from google import genai
        client = genai.Client(api_key=api_key)
        return client.models.generate_content(model=GEMINI_MODEL, contents=prompt).text
    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "HTTP-Referer": "https://streamlit.io", "X-Title": "Myanmar Auto Video Dubbing"},
        json={"model": OPENROUTER_MODEL, "messages": [{"role": "user", "content": prompt}],
              "temperature": 0.2}, timeout=240)
    if response.status_code >= 400:
        raise RuntimeError(f"OpenRouter error {response.status_code}: {response.text[:700]}")
    return response.json()["choices"][0]["message"]["content"]


@lru_cache(maxsize=256)
def free_translate_text(text):
    """Translate to Burmese through MyMemory autodetect without Google rate limits."""
    errors = []
    for attempt in range(3):
        try:
            response = requests.get(
                "https://api.mymemory.translated.net/get",
                params={"q": text, "langpair": "autodetect|my-MM"},
                headers={"User-Agent": "Myanmar-Free-Video-Dubber/1.0"},
                timeout=45,
            )
            data = response.json()
            result = ((data.get("responseData") or {}).get("translatedText") or "").strip()
            if response.ok and data.get("responseStatus") == 200 and contains_burmese(result):
                return result
            errors.append(str(data.get("responseDetails") or result or response.status_code))
        except Exception as exc:
            errors.append(str(exc)[:160])
        if attempt < 2:
            time.sleep(1.0 + attempt)
    raise RuntimeError("MyMemory Burmese translation failed: " + " | ".join(errors[:2]))


def free_translate_segments(segments, progress_callback=None):
    translated = []
    errors = []
    chunk_size = 6
    for chunk_start in range(0, len(segments), chunk_size):
        batch = segments[chunk_start:chunk_start + chunk_size]
        source_text = "\n".join(seg["text"] for seg in batch)
        try:
            translated_text = free_translate_text(source_text)
            lines = [line.strip() for line in translated_text.splitlines() if line.strip()]
            if len(lines) != len(batch) or not all(contains_burmese(line) for line in lines):
                raise RuntimeError("context translation did not preserve all line boundaries")
            for seg, burmese in zip(batch, lines):
                translated.append({**seg, "burmese": burmese})
        except Exception as exc:
            errors.append(f"အပိုင်း {chunk_start + 1}: {str(exc)[:120]}")
        if progress_callback:
            progress_callback(min(1.0, (chunk_start + len(batch)) / len(segments)))
    if errors:
        raise RuntimeError("မြန်မာဘာသာပြန် မအောင်မြင်ပါ။ မူရင်းစာသားကို အသံမပြောင်းပါ။ " + " | ".join(errors[:3]))
    return translated, "Free MyMemory autodetect"


def translate_segments(segments, grok_key, gemini_key, openrouter_key, progress_callback=None):
    translated = []
    batch_size = 18
    provider_order = []
    for start in range(0, len(segments), batch_size):
        batch = segments[start:start + batch_size]
        source_lines = [{"id": i, "text": seg["text"]} for i, seg in enumerate(batch)]
        prompt = (
            "Translate each line into natural, concise spoken Burmese for video dubbing. "
            "Preserve meaning, names, emotion and order. Do not add explanations or events. "
            "Keep each translation short enough to speak within its original time slot. "
            "Return ONLY a JSON array with objects containing id and myanmar. Keep every id.\n" +
            json.dumps(source_lines, ensure_ascii=False))
        raw = None
        errors = []
        if grok_key:
            try:
                raw = call_translation_api(prompt, "Grok", grok_key)
                provider_order.append("Grok")
            except Exception as exc:
                errors.append(f"Grok: {str(exc)[:160]}")
        if raw is None and gemini_key:
            try:
                raw = call_translation_api(prompt, "Gemini", gemini_key)
                provider_order.append("Gemini")
            except Exception as exc:
                errors.append(f"Gemini: {str(exc)[:160]}")
        if raw is None and openrouter_key:
            try:
                raw = call_translation_api(prompt, "OpenRouter", openrouter_key)
                provider_order.append("OpenRouter")
            except Exception as exc:
                errors.append(f"OpenRouter: {str(exc)[:160]}")
        if raw is None:
            if not (gemini_key or openrouter_key):
                return free_translate_segments(segments, progress_callback)
            raise RuntimeError("မြန်မာဘာသာပြန် API မရပါ။ " + " | ".join(errors))
        rows = parse_json_array(raw)
        mapping = {}
        for row in rows:
            try:
                mapping[int(row.get("id"))] = str(row.get("myanmar", row.get("translation", ""))).strip()
            except Exception:
                continue
        for i, seg in enumerate(batch):
            burmese = mapping.get(i, "")
            if not burmese:
                raise RuntimeError(f"AI ဘာသာပြန်ရလဒ်တွင် အပိုင်း {i + 1} မပါပါ။")
            if not contains_burmese(burmese):
                raise RuntimeError(f"AI ဘာသာပြန်ရလဒ်သည် မြန်မာစာမဟုတ်ပါ (အပိုင်း {i + 1})။")
            translated.append({**seg, "burmese": burmese})
        if progress_callback:
            progress_callback(min(1.0, (start + len(batch)) / len(segments)))
    provider = " / ".join(dict.fromkeys(provider_order)) or "AI"
    return translated, provider


async def tts_save(text, voice, path):
    await edge_tts.Communicate(text, voice).save(str(path))


def stamp(seconds):
    milliseconds = max(0, int(seconds * 1000))
    hours, rem = divmod(milliseconds, 3600000)
    minutes, rem = divmod(rem, 60000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{ms:03}"


def write_srt(segments, duration, path):
    blocks = []
    for index, seg in enumerate(segments, 1):
        start = min(duration, max(0, seg["start"]))
        end = min(duration, max(start + 0.25, seg["end"]))
        blocks.append(f"{index}\n{stamp(start)} --> {stamp(end)}\n{seg['burmese']}\n")
    path.write_text("\n".join(blocks), encoding="utf-8-sig")


def build_voice_track(segments, voice, workdir, duration, progress_callback=None):
    tts_dir = workdir / "tts_segments"
    tts_dir.mkdir(exist_ok=True)
    prepared = []
    cursor = 0.0
    for index, seg in enumerate(segments):
        if not contains_burmese(seg.get("burmese", "")):
            raise RuntimeError(f"အပိုင်း {index + 1} တွင် မြန်မာစာမရှိပါ။ Final video မထုတ်ပါ။")
        out = tts_dir / f"voice_{index:04d}.mp3"
        asyncio.run(tts_save(seg["burmese"], voice, out))
        if not out.exists() or out.stat().st_size < 1000:
            raise RuntimeError("မြန်မာ Edge TTS အသံဖိုင် မထွက်လာပါ။ Network သို့မဟုတ် TTS service ကို စစ်ပါ။")
        tts_duration = probe_duration(out)
        requested_start = max(0.0, min(duration, float(seg["start"])))
        start = max(requested_start, cursor)
        if index + 1 < len(segments):
            next_start = max(start + 0.25, min(duration, float(segments[index + 1]["start"])))
            available = max(0.25, next_start - start)
        else:
            available = max(0.25, duration - start)
        speed = max(1.0, tts_duration / available)
        adjusted = out
        adjusted_duration = tts_duration / speed
        if speed > 1.02 or adjusted_duration > available:
            adjusted = tts_dir / f"voice_{index:04d}_fit.mp3"
            tempo_filters = []
            remaining_speed = speed
            while remaining_speed > 2.0:
                tempo_filters.append("atempo=2.0")
                remaining_speed /= 2.0
            tempo_filters.append(f"atempo={max(1.0, remaining_speed):.3f}")
            run_cmd([ffmpeg_path(), "-y", "-i", str(out), "-af",
                     ",".join(tempo_filters) +
                     f",atrim=duration={available:.3f},asetpts=PTS-STARTPTS",
                     "-c:a", "libmp3lame", "-b:a", "96k", str(adjusted)], timeout=180)
        prepared.append((adjusted, int(start * 1000)))
        cursor = min(duration, start + min(available, adjusted_duration))
        if progress_callback:
            progress_callback((index + 1) / len(segments))
    return prepared


def render_dubbed_video(source, prepared_audio, duration, output_path,
                        export_mode="TikTok 9:16", color_style="Natural", slight_crop=True):
    args = [ffmpeg_path(), "-y", "-i", str(source), "-f", "lavfi", "-t", str(duration),
            "-i", "anullsrc=r=24000:cl=mono"]
    for audio_path, _ in prepared_audio:
        args += ["-i", str(audio_path)]
    filters = [f"[1:a]atrim=duration={duration:.3f},asetpts=PTS-STARTPTS[base]"]
    mix_labels = ["[base]"]
    for i, (_, delay_ms) in enumerate(prepared_audio, start=2):
        label = f"voice{i}"
        filters.append(f"[{i}:a]aresample=24000,adelay=delays={delay_ms}:all=1[{label}]")
        mix_labels.append(f"[{label}]")
    filters.append("".join(mix_labels) +
                   f"amix=inputs={len(mix_labels)}:duration=first:dropout_transition=0:normalize=0,"
                   f"atrim=duration={duration:.3f},asetpts=PTS-STARTPTS,"
                   "loudnorm=I=-16:TP=-1.5:LRA=7[aout]")

    if export_mode == "TikTok 9:16 (1080x1920)":
        visual_filters = []
        if slight_crop:
            visual_filters.append("crop=iw*0.97:ih*0.97:(iw-ow)/2:(ih-oh)/2")
        visual_filters.append("scale=1080:1920:force_original_aspect_ratio=increase")
        visual_filters.append("crop=1080:1920:(iw-1080)/2:(ih-1920)/2")
        visual_filters.append("setsar=1")
    else:
        visual_filters = []

    if color_style == "Warm":
        visual_filters.append("colorbalance=rs=.05:gs=.02:bs=-.04")
    elif color_style == "Cool":
        visual_filters.append("colorbalance=rs=-.04:gs=.01:bs=.05")
    elif color_style == "Vivid":
        visual_filters.append("eq=contrast=1.04:saturation=1.08:brightness=0.01")

    video_filter = ",".join(visual_filters) if visual_filters else "null"
    args += ["-filter_complex", ";".join(filters)]
    args += ["-map", "0:v:0", "-map", "[aout]",
             "-t", f"{duration:.3f}", "-vf", video_filter,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
             "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-shortest", str(output_path)]
    run_cmd(args, timeout=7200)


def main():
    st.title(APP_TITLE)
    st.write("Video တစ်ခုတည်းတင်ပါ။ မူရင်းအသံအားလုံးကို ဖယ်ရှားပြီး မြန်မာအသံနဲ့ အစားထိုးပေးပါမယ်။")
    st.info("မူရင်းဘာသာစကားအသံကို Final MP4 ထဲ မထည့်ဘဲ မြန်မာအသံတစ်ခုတည်း ထည့်ပေးပါမယ်။ API key မရှိရင် Free mode နဲ့ local processing လုပ်နိုင်ပါတယ်။")

    configured_grok_key = secret("XAI_API_KEY")
    gemini_key = secret("GEMINI_API_KEY")
    groq_key = secret("GROQ_API_KEY")
    openrouter_key = secret("OPENROUTER_API_KEY")
    password = secret("APP_PASSWORD")
    if password:
        entered = st.text_input("ဝင်ရောက်ရန် Password", type="password")
        if entered != password:
            st.info("Password ထည့်ပြီးမှ ဆက်လုပ်နိုင်ပါမယ်။")
            st.stop()

    uploaded = st.file_uploader("Video တင်ပါ (MP4 / MOV / MKV / WEBM)", type=["mp4", "mov", "mkv", "webm"])
    entered_grok_key = st.text_input(
        "Grok (xAI) API key — ဒီ run အတွက်သာ အသုံးပြုမည်",
        type="password",
        placeholder="xai-...",
        help="Key ကို file ထဲ မရေးပါ။ Streamlit session အတွင်း ဒီ processing run အတွက်သာ အသုံးပြုပါမယ်။"
    )
    grok_key = entered_grok_key.strip() or configured_grok_key
    if configured_grok_key and not entered_grok_key:
        st.caption("Streamlit Secrets ထဲက XAI_API_KEY ကို အသုံးပြုနေသည်။")
    elif entered_grok_key:
        st.caption("ထည့်ထားသော Grok key ကို ဒီ session အတွင်းသာ အသုံးပြုနေသည်။")
    active_providers = []
    if grok_key:
        active_providers.append("Grok")
    if groq_key:
        active_providers.append("Groq fallback")
    if not active_providers:
        active_providers.append("Free local Whisper → Free translation → Edge TTS")
    st.caption("အသုံးပြုမည့် processing mode: " + " → ".join(active_providers))
    voice_label = st.selectbox("မြန်မာအသံရွေးချယ်ပါ", ["နီလာ (မိန်းကလေး)", "သီဟ (ယောက်ျားလေး)"])
    export_mode = st.selectbox("Final video format", ["TikTok 9:16 (1080x1920)", "Original aspect ratio"])
    color_style = st.selectbox("Color style", ["Natural", "Warm", "Cool", "Vivid"])
    slight_crop = st.checkbox("Slight center crop (3%)", value=True,
                              help="Portrait framing အတွက်သာ အသုံးပြုသည်။ ကိုယ်ပိုင် သို့မဟုတ် လိုင်စင်ရှိသော video များအတွက် သုံးပါ။")
    st.caption("ဒီ export controls တွေဟာ platform formatting နဲ့ creative grading အတွက်သာ ဖြစ်ပြီး မူပိုင်ခွင့်စစ်ဆေးမှုကို ရှောင်ရန် မဟုတ်ပါ။")

    if uploaded and uploaded.size > MAX_MB * 1024 * 1024:
        st.error(f"Video သည် {MAX_MB} MB ထက်ကျော်နေပါသည်။")
        st.stop()

    if st.button("🎙️ မြန်မာအသံနဲ့ Dubbing လုပ်မယ်", type="primary", disabled=uploaded is None):
        if not (grok_key or groq_key):
            st.info("API key မတွေ့ပါ။ Free local Whisper mode ကို အသုံးပြုနေပါတယ်။ ပထမဆုံး run မှာ Whisper model download လုပ်ရနိုင်ပါတယ်။")

        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            video = work / Path(uploaded.name).name
            video.write_bytes(uploaded.getbuffer())
            original_audio = work / "original_for_transcription.mp3"
            output_mp3 = work / "myanmar_voice.mp3"
            output_mp4 = work / "myanmar_dubbed_video.mp4"
            duration = probe_duration(video)
            progress = st.progress(0, text="Video ပြင်ဆင်နေသည်... (0%)")
            status = st.empty()

            def set_progress(value, label):
                percent = int(max(0, min(100, value)))
                progress.progress(percent, text=f"{label} ({percent}%)")

            try:
                status.info("မူရင်းအသံကို စာသားဖော်ထုတ်ရန် ယာယီထုတ်ယူနေသည်...")
                extract_audio(video, original_audio)
                set_progress(8, "အသံဖိုင် ပြင်ဆင်ပြီးပါပြီ")

                status.info("Grok Voice STT သို့မဟုတ် Free local Whisper ဖြင့် မူရင်းစကားပြောသံနှင့် အချိန်မှတ်တမ်း ထုတ်ယူနေသည်...")
                try:
                    if not grok_key:
                        raise RuntimeError("XAI_API_KEY မရှိပါ")
                    segments = grok_transcribe(original_audio, grok_key)
                except Exception as grok_error:
                    if not groq_key:
                        status.warning(f"Grok မအောင်မြင်ပါ။ Local Whisper ကို စမ်းသုံးနေသည်။ {str(grok_error)[:180]}")
                        segments = local_whisper_transcribe(original_audio)
                    else:
                        status.warning(f"Grok မအောင်မြင်ပါ။ Groq Whisper fallback ကို စမ်းသုံးနေသည်။ {str(grok_error)[:180]}")
                        try:
                            segments = groq_transcribe(original_audio, groq_key)
                        except Exception as groq_error:
                            status.warning(f"Groq မအောင်မြင်ပါ။ Local Whisper ကို စမ်းသုံးနေသည်။ {str(groq_error)[:180]}")
                            segments = local_whisper_transcribe(original_audio)
                if not segments:
                    raise RuntimeError("မူရင်းစကားပြောသံမှ စာသားမရရှိပါ။ Video ထဲတွင် အသံရှိ/မရှိ စစ်ဆေးပါ။")
                set_progress(35, f"စကားပြောအပိုင်း {len(segments)} ခု ဖော်ထုတ်ပြီးပါပြီ")

                status.info("စာသားအပိုင်းတိုင်းကို မြန်မာလို ဘာသာပြန်နေသည်...")
                segments, translation_provider = translate_segments(
                    segments, grok_key, gemini_key, openrouter_key,
                    lambda fraction: set_progress(35 + fraction * 25, "မြန်မာဘာသာပြန်နေသည်"))
                set_progress(62, f"မြန်မာဘာသာပြန်ပြီးပါပြီ ({translation_provider})")

                voice = "my-MM-NilarNeural" if voice_label.startswith("နီလာ") else "my-MM-ThihaNeural"
                status.info("မြန်မာအသံ အပိုင်းလိုက် ဖန်တီးပြီး မူရင်းအချိန်နဲ့ ညှိနေသည်...")
                prepared_audio = build_voice_track(
                    segments, voice, work, duration,
                    lambda fraction: set_progress(62 + fraction * 20, "မြန်မာအသံ ဖန်တီးနေသည်"))
                set_progress(84, "မြန်မာအသံ ပြင်ဆင်ပြီးပါပြီ")

                status.info("မူရင်းအသံ Track ကို ဖယ်ရှားပြီး မြန်မာအသံတစ်ခုတည်း ထည့်နေသည်...")
                render_dubbed_video(video, prepared_audio, duration, output_mp4,
                                    export_mode=export_mode, color_style=color_style,
                                    slight_crop=slight_crop)
                validation_note = validate_final_video(output_mp4, duration)
                # Export a standalone MP3 from the final MP4 audio, ensuring it matches the final mix.
                run_cmd([ffmpeg_path(), "-y", "-i", str(output_mp4), "-vn", "-c:a", "libmp3lame",
                         "-b:a", "160k", str(output_mp3)], timeout=1200)
                if not output_mp3.exists() or output_mp3.stat().st_size < 1000:
                    raise RuntimeError("MP3 အသံဖိုင် မပြည့်စုံပါ။")
                set_progress(100, "ပြီးပါပြီ")
                status.success(f"Dubbing အောင်မြင်ပါပြီ။ Final audio စစ်ဆေးပြီးပါပြီ ({validation_note})")
                st.subheader("Final Myanmar Dubbed Video")
                st.video(str(output_mp4))
                output_name = "myanmar_dubbed_tiktok_9x16.mp4" if export_mode.startswith("TikTok") else "myanmar_dubbed_video.mp4"
                st.download_button("⬇️ မြန်မာအသံ Video (MP4)", output_mp4.read_bytes(),
                                   file_name=output_name, mime="video/mp4")
                st.download_button("⬇️ မြန်မာအသံ (MP3)", output_mp3.read_bytes(),
                                   file_name="myanmar_voice.mp3", mime="audio/mpeg")
            except Exception as exc:
                progress.empty()
                status.error(f"လုပ်ဆောင်မှု မအောင်မြင်ပါ: {exc}")
                st.caption("API quota၊ Network၊ FFmpeg သို့မဟုတ် Streamlit Cloud resource ကန့်သတ်ချက်ကြောင့် ဖြစ်နိုင်ပါတယ်။")


if __name__ == "__main__":
    main()
