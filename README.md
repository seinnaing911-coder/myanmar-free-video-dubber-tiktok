# Myanmar Free Video Dubber

Streamlit app: one video upload, timestamped speech recognition, Burmese translation and TTS, producing a clean dubbed MP4 with the original audio removed.

## Included
- One MP4 / MOV / MKV / WEBM upload.
- Groq Whisper transcription with local faster-whisper fallback.
- OpenRouter translation with free MyMemory fallback.
- Burmese Edge TTS voices (Nilar and Thiha) for reliable Myanmar speech output.
- No-key Free mode using a tiny local Whisper model, direct MyMemory autodetect-to-Burmese translation, and Edge TTS.
- Original audio is removed from the final video; only generated Burmese voice is mixed.
- Final dubbed video preview plus MP4 and MP3 exports; no source preview, blur, or burned subtitles.
- Speech segments are fitted into their time slots to reduce overlap.
- Long Burmese speech uses chained tempo fitting so words are not cut off when a segment is shorter than the translated narration.
- Burmese-script validation blocks output when translation fails, instead of reading the original-language text with a Myanmar voice.
- Free translation sends adjacent narration lines as context-aware chunks so names, events, and story continuity are preserved better than isolated line translation.
- TikTok-ready 9:16 export at 1080×1920 with optional 3% center crop and Natural/Warm/Cool/Vivid color grading.
- Cloud-safe upload guard: maximum 200 MB and 15 minutes per video, with clearer memory/timeout errors.

## Deploy
Upload `app.py`, `requirements.txt`, `packages.txt`, and `.streamlit/config.toml` to the connected GitHub repository.

Set these in Streamlit Cloud > App > Settings > Secrets:
```toml
# Optional. The default tiny model is fastest for free local mode.
LOCAL_WHISPER_MODEL = "tiny"
GROQ_API_KEY = "your-groq-key"
OPENROUTER_API_KEY = "your-openrouter-key"
APP_PASSWORD = "your-private-password"
```
Keep API keys private. Do not commit real keys to GitHub.

## Provider order

The app uses Groq Whisper when `GROQ_API_KEY` is configured, otherwise it uses local faster-whisper. OpenRouter is used for Burmese translation when `OPENROUTER_API_KEY` is available; otherwise it uses MyMemory autodetect-to-Burmese translation. Burmese Edge TTS generates the replacement voice in both API and no-key Free modes. The final MP4 never maps the original audio track; only the generated Burmese voice is included.

Free mode has no AI API bill, but the first local Whisper run downloads a model and CPU processing is slower. Public free translation endpoints can also rate-limit heavy use. For the fastest mode, configure `GROQ_API_KEY` and optionally `OPENROUTER_API_KEY`.

## Notes
- The uploaded video must contain an audio track with speech for transcription.
- Speech recognition timestamps may not be perfect. Very long Burmese lines can be shortened by fitting them into the available slot.
- Streamlit Cloud CPU, memory, execution time, network access and provider quotas can limit large-video processing.
- For Streamlit Cloud, keep videos below 200 MB and 15 minutes. If processing stops, reboot the app from Manage app and retry with a shorter video or a smaller Whisper model such as `tiny`.

Free mode does not require `GROQ_API_KEY` or `OPENROUTER_API_KEY`; it uses local faster-whisper, free translation, and Edge TTS. The app must be allowed to process without an API key.

## Rights and responsible use

Use the uploader and export controls only with videos you own or are licensed to edit and publish. The crop/color controls are ordinary platform-formatting and creative-grading options; they are not intended to evade copyright enforcement or disguise an unauthorized copy. The upstream repository currently does not include an explicit license file, so obtain the original author's permission before publishing or redistributing a derived copy.
