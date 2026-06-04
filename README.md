# Todo Sync Backend

FastAPI backend for syncing the Todo web app and Todo PWA through Neon/Postgres.

## Render service type

Create a new Render **Web Service**.

- Runtime: Python
- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Health Check Path: `/healthz`

## Required environment variables

Set these in Render dashboard:

```env
DATABASE_URL=postgresql://USER:PASSWORD@HOST/neondb?sslmode=require&channel_binding=require
API_BEARER_TOKEN=replace_with_long_random_secret
TODO_SYNC_DEFAULT_USER_TOKEN=emmanuel_main
TODO_SYNC_DEFAULT_EMAIL=emmabarca123@gmail.com
TODO_SYNC_ALLOWED_EMAILS=emmabarca123@gmail.com
TODO_SYNC_NOTE_WINDOW_DAYS=92
TODO_SYNC_HTTP_TIMEOUT_SEC=8
CORS_ALLOW_ORIGINS=https://todo-finalboss.onrender.com,https://todo-celular-prototipo.onrender.com
```

The included `render.yaml` Blueprint asks for `DATABASE_URL` in the Render
dashboard with `sync: false`, and generates `API_BEARER_TOKEN` without
committing secrets.

Use the same `API_BEARER_TOKEN` in the web and PWA as `VITE_SYNC_AUTH_TOKEN` during phase 1.
The manual Neon import uses `user_token=emmanuel_main`. When phase 2 Google
login is enabled, the backend reuses that imported account when the verified
Gmail address matches, so the recovered records stay visible.
Google login remains disabled until `GOOGLE_CLIENT_ID` is configured in both
the backend and the frontends.

## Endpoints

- `GET /healthz`
- `GET /auth/me`
- `GET /v1/sync/schema`
- `POST /v1/sync/import`
- `POST /v1/sync/push`
- `GET /v1/sync/pull?since=...`
- `GET /v1/sync/export`
