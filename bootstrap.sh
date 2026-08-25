#!/usr/bin/env bash
# bootstrap.sh — scaffold relay-roof-check and enable the Google Cloud services.
# Run once, from an empty parent directory. Safe to re-run.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-relay-roof-check}"
REGION="${REGION:-us-central1}"
SA_NAME="roof-check-agent"

echo "==> Scaffolding repository"
mkdir -p relay-roof-check && cd relay-roof-check

mkdir -p app/{agents,tools,checks,store,report} renderer docs

# ── Python project ────────────────────────────────────────────────────────────
cat > requirements.txt <<'EOF'
google-adk
google-genai
google-cloud-firestore
google-cloud-storage
google-cloud-pubsub
google-auth
fastapi
uvicorn[standard]
httpx
selectolax
phonenumbers
python-dotenv
pydantic
EOF

cat > .env.example <<'EOF'
# Copy to .env and fill in. Never commit .env.

# Vertex routing. Set FALSE and supply GOOGLE_API_KEY to fall back to AI Studio.
GOOGLE_GENAI_USE_VERTEXAI=TRUE
GOOGLE_CLOUD_PROJECT=
GOOGLE_CLOUD_LOCATION=us-central1
GOOGLE_API_KEY=

GEMINI_MODEL=gemini-3.5-flash

# Services
RENDERER_URL=
RENDERER_SHARED_SECRET=

# External APIs
GOOGLE_PLACES_API_KEY=
PAGESPEED_API_KEY=

# Probe identity
PROBE_EMAIL_DOMAIN=relayforroofers.com
PROBE_GMAIL_ADDRESS=
PUBSUB_TOPIC=gmail-probe-events

# Storage
GCS_EVIDENCE_BUCKET=
REPORT_IP_SALT=

# Crawl politeness
CRAWL_USER_AGENT=RelayRoofCheck/1.0 (+https://relayforroofers.com/bot)
EOF

cat > .gitignore <<'EOF'
.env
__pycache__/
*.pyc
.venv/
venv/
node_modules/
.DS_Store
*.png
!docs/architecture.png
EOF

cat > README.md <<'EOF'
# Relay Roof Check

An agent that audits a roofing contractor's own website with his consent, submits a
real lead through his contact form, waits for his team to respond, and reports how
long it took.

Built for the All Things Agentic Hackathon. Track: Taskmaster.

## Spin-up

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in
gcloud auth application-default login
python -m app.cli audit https://example-roofer.com
```

## Architecture

See `docs/architecture.png`.

## Stack

Gemini 3.5 via Vertex AI, Google ADK, Cloud Run, Firestore, Pub/Sub, Cloud Scheduler.
EOF

touch app/__init__.py app/agents/__init__.py app/tools/__init__.py \
      app/checks/__init__.py app/store/__init__.py

# ── Renderer service ──────────────────────────────────────────────────────────
cat > renderer/package.json <<'EOF'
{
  "name": "relay-roof-check-renderer",
  "type": "module",
  "scripts": { "start": "node server.js" },
  "dependencies": {
    "fastify": "^5.0.0",
    "playwright": "^1.49.0"
  }
}
EOF

cat > renderer/Dockerfile <<'EOF'
FROM mcr.microsoft.com/playwright:v1.49.0-jammy
WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev
COPY . .
ENV PORT=8080
CMD ["node", "server.js"]
EOF

# ── Agent service ─────────────────────────────────────────────────────────────
cat > Dockerfile <<'EOF'
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV PORT=8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
EOF

git init -q 2>/dev/null || true

echo "==> Repository scaffolded"

# ── Google Cloud ──────────────────────────────────────────────────────────────
if ! command -v gcloud >/dev/null 2>&1; then
  echo "!! gcloud not found. Install the SDK, then re-run to finish cloud setup."
  exit 0
fi

echo "==> Configuring project ${PROJECT_ID}"
gcloud config set project "${PROJECT_ID}" >/dev/null

echo "==> Enabling services (this takes a minute)"
gcloud services enable \
  aiplatform.googleapis.com \
  firestore.googleapis.com \
  run.googleapis.com \
  pubsub.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  places-backend.googleapis.com \
  pagespeedonline.googleapis.com \
  gmail.googleapis.com

echo "==> Creating service account"
gcloud iam service-accounts create "${SA_NAME}" \
  --display-name="Relay Roof Check agent" 2>/dev/null || echo "   already exists"

SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
for ROLE in roles/aiplatform.user roles/datastore.user \
            roles/storage.objectAdmin roles/pubsub.editor \
            roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" --role="${ROLE}" \
    --condition=None >/dev/null
  echo "   granted ${ROLE}"
done

echo "==> Creating Firestore database (native mode)"
gcloud firestore databases create --location="${REGION}" 2>/dev/null \
  || echo "   already exists"

echo "==> Creating Pub/Sub topic"
gcloud pubsub topics create gmail-probe-events 2>/dev/null || echo "   already exists"

echo "==> Local credentials"
gcloud auth application-default login

cat <<EOS

Done.

Next:
  1. Claim the \$150 hackathon credits on the Devpost Resources tab. Approval takes
     one to five business days, so do this before anything else.
  2. Set a billing budget alert at \$50 in the Cloud Console.
  3. Copy docs into place: CLAUDE.md at the root, the three spec files in docs/.
  4. Make one real Vertex call today to confirm quota on this new project.

Service account: ${SA_EMAIL}
EOS
