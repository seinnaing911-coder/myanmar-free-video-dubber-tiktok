# Myanmar Free Video Dubber

Streamlit app: one video upload, timestamped speech recognition, Burmese translation and TTS, producing a clean dubbed MP4 with the original audio removed.

## Included
- One MP4 / MOV / MKV / WEBM upload.
- Grok Voice batch transcription with Groq Whisper and local faster-whisper fallbacks.
- Grok translation first, then the existing Gemini/OpenRouter fallback.
- Burmese Edge TTS voices (Nilar and Thiha) for reliable Myanmar speech output.
- No-key Free mode using a tiny local Whisper model, direct MyMemory autodetect-to-Burmese translation, and Edge TTS.
- Original audio is removed from the final video; only generated Burmese voice is mixed.
- Final dubbed video preview plus MP4 and MP3 exports; no source preview, blur, or burned subtitles.
- Speech segments are fitted into their time slots to reduce overlap.
- Long Burmese speech uses chained tempo fitting so words are not cut off when a segment is shorter than the translated narration.
- Burmese-script validation blocks output when translation fails, instead of reading the original-language text with a Myanmar voice.
- Free translation sends adjacent narration lines as context-aware chunks so names, events, and story continuity are preserved better than isolated line translation.
- Optional per-session Grok (xAI) API-key input beside the video uploader; the entered key is not written to the repository or output files.
- TikTok-ready 9:16 export at 1080×1920 with optional 3% center crop and Natural/Warm/Cool/Vivid color grading.

## Deploy
Upload `app.py`, `requirements.txt`, `packages.txt`, and `.streamlit/config.toml` to the connected GitHub repository.

Set these in Streamlit Cloud > App > Settings > Secrets:
```toml
XAI_API_KEY = "your-xai-key"
# Optional. The default tiny model is fastest for free local mode.
LOCAL_WHISPER_MODEL = "tiny"
GEMINI_API_KEY = "your-gemini-key"
GROQ_API_KEY = "your-groq-key"
OPENROUTER_API_KEY = "your-openrouter-key"
APP_PASSWORD = "your-private-password"
```
Keep API keys private. Do not commit real keys to GitHub.

The app also accepts a Grok key in the upload form for a single processing session. Prefer Streamlit Secrets for a private deployment, and never paste a real key into source code or commit history.

## Provider order

The app uses Grok as the primary provider when `XAI_API_KEY` is configured. Grok Voice STT transcribes the uploaded audio and Grok translates the timestamped segments into concise spoken Burmese. Burmese Edge TTS generates the replacement voice reliably in both API and no-key Free modes. If no API key is configured, the app runs a no-key Free mode using local faster-whisper, direct MyMemory autodetect-to-Burmese translation, and Edge TTS. If Grok fails, it falls back to Groq, local Whisper, or free translation. The final MP4 never maps the original audio track; only the generated Burmese voice is included.

Free mode has no AI API bill, but the first local Whisper run downloads a model and CPU processing is slower. Public free translation endpoints can also rate-limit heavy use. For the fastest mode, configure `XAI_API_KEY` or `GROQ_API_KEY`.

## Notes
- The uploaded video must contain an audio track with speech for transcription.
- Speech recognition timestamps may not be perfect. Very long Burmese lines can be shortened by fitting them into the available slot.
- Streamlit Cloud CPU, memory, execution time, network access and provider quotas can limit large-video processing.

Free mode does not require `XAI_API_KEY` or `GROQ_API_KEY`; it uses local faster-whisper, free translation, and Edge TTS. The app must be allowed to process without an API key.

## Rights and responsible use

Use the uploader and export controls only with videos you own or are licensed to edit and publish. The crop/color controls are ordinary platform-formatting and creative-grading options; they are not intended to evade copyright enforcement or disguise an unauthorized copy. The upstream repository currently does not include an explicit license file, so obtain the original author's permission before publishing or redistributing a derived copy.
