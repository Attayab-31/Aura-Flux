# Render free deployment with Supabase

This repository now supports Supabase PostgreSQL for accounts and queues, and a private Supabase Storage bucket for scene assets and finished videos. The service stays at one Render instance and one Gunicorn worker. That matches the app's single queue-consumer design.

## Supabase setup

1. Create a Supabase project and save its database password.
2. In **Connect**, copy the **Session pooler** connection string (port `5432`) for an IPv4 host such as Render. Replace the password, URL-encoding reserved characters. Use this as Render's `SUPABASE_DB_URL`.
3. Create a **private** Storage bucket named `fluxstory-media` (or choose another name and set `SUPABASE_STORAGE_BUCKET`). Allow `video/mp4`, `image/png`, and `audio/mpeg` for uploads.
4. Copy the project's URL and its server-side `service_role` key into Render as `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`. Keep the key server-side; never expose it in a template or browser JavaScript.

The app creates its PostgreSQL tables and enables row-level security at startup. The web process connects as the database owner and performs all account ownership checks itself. Storage objects remain private; the app checks project ownership and then issues a one-hour signed URL for video playback.

## Render setup

1. Push this repository to a Git provider that Render can access, then create a **Blueprint** from it. Render reads `render.yaml` and builds the included Dockerfile.
2. Fill every `sync: false` variable in the Render dashboard. In addition to the three Supabase values, set `DEEPGRAM_API_KEY`, one LLM provider key, Kaggle credentials, your actual `KAGGLE_KERNEL_ID`, and a high-entropy `WORKER_CALLBACK_TOKEN` if you want video generation enabled.
3. Set `KAGGLE_BATCH_AUTOSTART=true` only after the private Kaggle notebook callback URL and matching token are configured. The worker notebook needs the Render HTTPS origin and the same callback token.
4. Wait for the `/healthz` health check to pass, then create an account and confirm the app can save a project.

If the service is deployed without `SUPABASE_DB_URL`, `SUPABASE_URL`, or `SUPABASE_SERVICE_ROLE_KEY`, startup fails instead of silently falling back to ephemeral SQLite or local-only media. Database connection and Storage upload errors are also surfaced in the service logs.

## Free-tier limitations

Render free web services sleep after 15 minutes without inbound traffic, cold starts take roughly a minute, files on the container are erased on restarts/redeploys, and the service cannot scale beyond one instance. Render describes free services as suitable for hobby use and previews, not production. Keep the generation page open while a job is processing so its status polling keeps the service awake. Completed scene audio, images, and MP4s are copied to Supabase Storage so a restarted container can restore them.

The job consumer and Kaggle supervisor run in the web process. Do not add Gunicorn workers, replicas, or a second Render service using the same queue. Free CPU and memory can also be insufficient for longer/high-resolution MoviePy renders; use short, low-scene-count videos and watch memory and deploy logs. Supabase's free-plan storage/database quotas and pause/backup policies are separate from Render's quotas, so review current limits before relying on it for retained user data.

## Operational notes

- `SUPABASE_DB_URL` should be the **session** pooler string, not the transaction pooler. The process needs ordinary PostgreSQL transactions and row locks.
- The service role key bypasses Supabase Storage RLS. It is used only by server code, while the bucket itself stays private.
- There is no SQLite-to-Supabase data import in this deployment path. Existing local accounts and project records are not migrated automatically.
- Object retention and deletion are not automated yet. Delete obsolete media objects and accounts according to your retention policy.
