# AuraFLUX Video Studio: AI-Driven Automated Video Generation Control Plane

A production-grade, modular Flask application that acts as the primary **Control Plane** for an end-to-end automated AI video generation pipeline.

The architecture decouples the Flask control plane (durable user jobs, LLM scene planning, Deepgram narration, media ownership, and MoviePy rendering) from an on-demand finite Kaggle GPU batch worker. Flask persists individual image requests, starts a private notebook run through the Kaggle CLI, receives authenticated callbacks, and remains the only public API.

---

## 1. System Architecture

```
+-----------------------------------------------------------------------------------+
|                           FLASK CONTROL PLANE (Local / Server)                    |
|                                                                                   |
|  +--------------------+      +--------------------+      +---------------------+  |
|  |   Tailwind UI &    | ---> |    LLM Director    | ---> | Deepgram Aura 2 TTS |  |
|  |   Web Controller   |      |  (Scene Manifests) |      | (Exact Audio Sync)  |  |
|  +--------------------+      +--------------------+      +---------------------+  |
|            |                                                        |             |
|            v                                                        v             |
|  +--------------------+                                  +---------------------+  |
|  | Durable GPU queue  |                                  | MoviePy v2 Composer |  |
|  | (FIFO + callbacks) |                                  |  (Ken Burns Pan/    |  |
|  +--------------------+                                  |  Zoom + Crossfades) |  |
|            |                                             +---------------------+  |
+------------------|------------------------------------------------|----------------+
                   | Kaggle CLI: push / status                    v
                   |                                 +---------------------+
                   v                                 | Production Master   |
+-----------------------------------------+           |      MP4 Video      |
| Private finite Kaggle notebook batch    |           +---------------------+
| Sequential FluxStoryEngine; idle exit   |
| Authenticated Flask callbacks; no server|
+-----------------------------------------+
```

---

## 2. Directory Structure

```
E:\Transitional_video_automation\
├── app.py                     # Flask routes, SQLite queue consumer, orchestration
├── config.py                  # App setup, path management, resolution profiles
├── requirements.txt           # Production dependencies
├── .env.example               # Environment variables template
├── services/
│   ├── __init__.py            # Services package entry point
│   ├── llm_director.py        # LLM narrative decomposition, visual prompts, consistency anchors
│   ├── tts_service.py         # Deepgram Aura REST API client & sub-second audio duration parser
│   ├── image_client.py        # Authenticated Kaggle client (PNG validation, retries, health checks)
│   └── video_composer.py      # MoviePy v2 engine (sub-pixel Ken Burns, crossfade transitions, H.264)
├── templates/
│   ├── base.html              # Tailwind CSS layout, header status indicators
│   └── index.html             # Video creation dashboard, progress tracker, storyboard, player
└── static/
    ├── css/
    │   └── style.css          # Sleek custom animations, dark slate theme
    ├── js/
    │   └── main.js            # Reactive form handling, polling loop, video playback
    └── previews/              # Fixed local voice and visual style previews
```

For local development, runtime data is stored under `instance/` and `temp/`. On Render, set the Supabase environment variables described in [DEPLOY_RENDER.md](DEPLOY_RENDER.md); the PostgreSQL database becomes the source of truth and the private Storage bucket holds generated media.

---

## 3. Quick Start & Setup

### Step 1: Install Dependencies
Ensure Python 3.10+ and FFmpeg are available on your system:

```bash
pip install -r requirements.txt
```

### Step 2: Configure Environment Variables
Copy the template configuration file:

```bash
cp .env.example .env
```

Edit `.env` to supply your credentials. Generate a unique session signing secret with `python -c "import secrets; print(secrets.token_hex(32))"` and place it in `SECRET_KEY`:
```ini
# Port and Flask settings
PORT=5000
FLASK_DEBUG=false
SECRET_KEY=your_unique_random_64_character_hex_secret
SESSION_COOKIE_SECURE=false # Set true when served over HTTPS

# Deepgram Aura Text-to-Speech API Key
DEEPGRAM_API_KEY=your_deepgram_api_key_here

# Finite on-demand Kaggle batch runner; explicitly opt in after account/policy review.
KAGGLE_BATCH_AUTOSTART=false
KAGGLE_API_TOKEN=your_kaggle_cli_token
KAGGLE_KERNEL_ID=your-kaggle-username/fluxstory-on-demand-worker
KAGGLE_KERNEL_PATH=workers/kaggle_fluxstory
WORKER_CALLBACK_TOKEN=generate-a-random-secret-at-least-32-characters
KAGGLE_BATCH_MAX_STARTS_PER_DAY=6
KAGGLE_BATCH_IDLE_EXIT_SECONDS=45

# Primary server-side LLM attempt
LLM_PROVIDER=gemini
DEFAULT_LLM_MODEL=gemini-3.8-flash
GEMINI_API_KEY=your_gemini_key_here
OPENAI_API_KEY=sk-your_openai_key_here
GROQ_API_KEY=your_groq_key_here
# For custom OpenAI-compatible services only
CUSTOM_LLM_BASE_URL=
CUSTOM_LLM_API_KEY=
# Ordered fallbacks, attempted only when the previous model errors or returns invalid output
LLM_FALLBACKS=openai:gpt-4o-mini,groq:openai/gpt-oss-20b
```

Provider credentials, provider selection, model, custom endpoint, worker URL, and Deepgram key are read by the server from `.env`. The browser does not collect or persist those settings. The server tries `LLM_PROVIDER` + `DEFAULT_LLM_MODEL` first, then walks `LLM_FALLBACKS` from left to right. It skips entries with missing credentials, and uses the built-in rule-based planner if every configured call fails. Restart the server after changing `.env`.

To use another OpenAI-compatible provider, set `LLM_PROVIDER=custom`, set `DEFAULT_LLM_MODEL` to that service's model ID, and provide `CUSTOM_LLM_BASE_URL` and `CUSTOM_LLM_API_KEY`. Add a fallback as `custom:model-id` in `LLM_FALLBACKS`. Keep keys private in `.env`; never paste them into the browser UI or commit `.env`.

On the video creation page, **Preview selected voice** plays a bundled sample, and choosing a visual preset displays a fixed bundled illustration. Neither control calls Deepgram or the image worker. Voice preview files are already included; the optional script below only creates missing voice assets:

```bash
python scripts/build_preview_assets.py
```

The script skips files already present in `static/previews/`; remove a specific file only when you want to regenerate that sample.

### User accounts and project storage

Users can create an account with an email address and a password of at least 12 characters, then sign in to a private workspace. Passwords are stored as scrypt hashes. Sessions use HttpOnly, SameSite=Lax cookies, CSRF tokens protect form and generation requests, and sign-in attempts are throttled. Configure `SESSION_COOKIE_SECURE=true` when serving the app over HTTPS; use `false` only for local HTTP development.

The email address is the account's sign-in identifier. Outbound email verification and password-reset messages are not configured in this app yet.

Accounts, projects, queues and worker state use local SQLite when developing without `SUPABASE_DB_URL`. On Render, configure Supabase PostgreSQL and Storage as described in [DEPLOY_RENDER.md](DEPLOY_RENDER.md); deployment startup refuses to use ephemeral SQLite or local-only media. Local runtime folders are private and excluded from source control.

The queue holds at most 100 waiting user jobs, excluding the active job. One database-backed consumer claims jobs atomically. A distinct durable GPU queue batches scene images across requests. Its supervisor invokes the supported Kaggle CLI (`kernels push`, `kernels status`) and the private finite notebook exits after the configured idle interval. There is no HTTP server, ngrok tunnel, or unsupported stop call. If autostart is disabled, CLI credentials or callback secrets are missing, the CLI is unavailable, or quota caps are reached, the user queue remains pending and the service reports GPU worker unavailable.


### Step 4: Launch the Control Plane
```bash
python app.py
```
Open your browser at **`http://localhost:5000`**.

---

## 4. Kaggle GPU Worker Integration

1. Review Kaggle's current terms, quotas, and suitability for your serving workload. Batches are best-effort cold starts, not guaranteed availability or a 24/7/free-GPU promise.
2. Create a private Kaggle notebook from `workers/kaggle_fluxstory/fluxstory_kaggle_auto_batch_worker.ipynb`, attach a supported GPU, and enable Internet. Set Kaggle Secrets `FLASK_WORKER_BASE_URL` (public HTTPS Flask origin), `WORKER_CALLBACK_TOKEN` (same high-entropy value as Flask `.env`), `KAGGLE_KERNEL_ID`, `WORKER_IDLE_EXIT_SECONDS`, and `WORKER_MAX_RUNTIME_SECONDS`. Keep the kernel ID equal to Flask's setting and the two worker runtime values aligned with `KAGGLE_BATCH_IDLE_EXIT_SECONDS` and `KAGGLE_BATCH_MAX_RUNTIME_SECONDS`. Do not place secrets in notebook source.
3. Replace the `owner` segment in `kernel-metadata.json` and `KAGGLE_KERNEL_ID` with your Kaggle username/kernel slug. Install the Kaggle CLI and provide `KAGGLE_API_TOKEN` (preferred), or legacy username/key through deployment secrets.
4. Set `KAGGLE_BATCH_AUTOSTART=true` only after setup/policy review. Configure start/runtime caps to your account. `kaggle kernels push` creates a new notebook version; the dispatcher polls `kaggle kernels status`. The notebook processes FIFO requests one at a time and exits after idle or max runtime. If it gets stuck, use Kaggle's supported UI manually; this app does not automate a stop operation.
5. An alternative GPU provider can replace the dispatcher behind `KaggleBatchGpuClient` while keeping Flask's durable user/GPU request queues and callback contract.

---

## 5. Pipeline Stages & Execution Flow

| Stage | Executing Component | Key Inputs | Primary Output | Guardrails |
| :--- | :--- | :--- | :--- | :--- |
| **1. Narrative Decomposition** | `services/llm_director.py` | Story script, duration, style anchor | Structured JSON scene manifest | Enforces ~6-8s pacing per scene; rule-based fallback if LLM key is absent. |
| **2. Voiceover Synthesis** | `services/tts_service.py` | Narration text, Deepgram token | High-fidelity MP3 per scene | Measures sub-second audio length ($T_{\text{audio}}$) via Mutagen and FFprobe. |
| **3. Generative Visuals** | `services/gpu_request_client.py` + finite Kaggle notebook | Visual prompts, width/height, seed | PNG frames (`scene_XX.png`) | Durable GPU queue; coalesced CLI starts; sequential GPU inference; bounded leases and retries. |
| **4. Motion & Composition** | `services/video_composer.py` | Static images, audio durations | Sub-clips with continuous Ken Burns motion | Sub-pixel `cv2.INTER_CUBIC` scaling eliminates camera jitter. |
| **5. Final Master Rendering** | `services/video_composer.py` | Composited layers, synced audio | Master H.264 / AAC MP4 file | Encodes with `-pix_fmt yuv420p` for universal mobile & browser playback. |

---

## 6. Mathematical Ken Burns Motion & Audio Sync

### Ken Burns Pan & Zoom Formulation
For an image scaled over duration $T$, scale factor $S(t)$ is smoothly interpolated:
$$S(t) = S_{\text{start}} + \left( \frac{S_{\text{end}} - S_{\text{start}}}{T} \right) \cdot t$$

Original dimensions $(W_{\text{orig}}, H_{\text{orig}})$ are continuously cropped relative to the evolving camera viewport:
$$W_{\text{crop}}(t) = \frac{W_{\text{orig}}}{S(t)}, \quad H_{\text{crop}}(t) = \frac{H_{\text{orig}}}{S(t)}$$

Horizontal crop bounding coordinates for camera panning:
$$x_1(t) = (W_{\text{orig}} - W_{\text{crop}}(t)) \cdot \text{progress}$$

### Audio-Visual Crossfade Synchronization
To prevent visual cuts over spoken words, the visual display duration $T_{\text{scene}}$ is set to:
$$T_{\text{scene}} = T_{\text{audio}} + \Delta t$$
where $\Delta t$ is the crossfade overlap buffer ($\approx 1.0\text{s}$). Scene $B$ fades in over Scene $A$ during this buffer while Scene $B$'s voiceover initiates, producing seamless transitions without speech overlap.

---

## 7. REST API Endpoints

- `GET /`: Interactive web studio dashboard.
- `POST /api/create_video`: Submits a video generation job. Returns `202 Accepted` with `task_id`, `job_id`, initial FIFO `position`, and `poll_url`. At most 100 jobs wait in the queue; a full queue returns `429`.
  - JSON payload: `{ "story_text": "...", "duration_minutes": 0.7, "aspect_ratio": "16:9", ... }`
- `POST /generate`: Authenticated image-generation request with JSON `{ "prompt": "...", "width": 1024, "height": 576, "seed": 42 }`. Returns `202 Accepted` with `task_id`, lowercase `status`, queue `position`, acceptance `message`, and `poll_url`.
- `GET /status/`: Public safe service status with waiting and processing counts, consumer health, and queue capacity.
- `GET /api/status/<job_id>` (also `/status/<job_id>`): Authenticated real-time job state, `task_id`, FIFO `position` (`null` while running or finished), progress, scene previews, and console logs. The `/status/` path uses lowercase states (`queued`, `processing`, `completed`, `failed`); `/api/status/` retains uppercase state values for the existing web UI. Once an image task completes, the response includes its PNG as `image_b64` with `width` and `height`. A completed video task includes its first rendered scene PNG and the finished `video_url`.
- `POST /api/check_worker`: Pings the remote Kaggle worker `/health` endpoint and returns VRAM and latency.
- `GET /exports/<job_id>/<filename>`: Streams the owner's production H.264 MP4 master video after an ownership check.
- `GET /temp/<job_id>/images/<filename>`: Streams real-time scene thumbnail previews during generation.

---

## 8. Production Deployment

For the Render free plan with Supabase PostgreSQL and Storage, follow [DEPLOY_RENDER.md](DEPLOY_RENDER.md). Render Free has an ephemeral filesystem and sleeps when idle, so the local setup below is not appropriate for that host.

### Running with Gunicorn (Linux / Production)
```bash
gunicorn -w 1 --threads 8 -b 0.0.0.0:5000 app:app
```
*(Run one Gunicorn worker process. The database stores durable queue state; the cross-process lock and single replica guard the consumer. Do not add multiple replicas until queue consumers and the Kaggle supervisor are moved to separately coordinated workers.)*

### Clean Up & Maintenance
Temporary job assets are isolated in `temp/<job_id>/`. You can configure automatic cron cleanup or purge expired job folders as needed.
