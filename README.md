---
title: LLM Code Deployment
emoji: 🚀
colorFrom: blue
colorTo: green
sdk: docker
sdk_version: "0.2.12"
app_file: app.py
pinned: false
---

Check out the configuration reference at [https://huggingface.co/docs/hub/spaces-config-reference](https://huggingface.co/docs/hub/spaces-config-reference)



# LLM Code Deployment - Automated Generator

This repository contains a FastAPI-based automation service that accepts a deployment brief (with attachments), uses an LLM via AI Pipe to generate a minimal web app, creates a GitHub repository, enables Pages, and notifies an evaluator endpoint.

Overview
- `app.py` - The FastAPI application exposing `/api-deploy` and `/health`.
- `ai_client.py` - Wrapper for AI Pipe (OpenRouter) calls. Uses `AIPIPE_TOKEN` from environment.
- `github_ops.py` - Automates repository creation and GitHub Pages using `gh` or git.
- `evaluator.py` - Posts results back to `evaluation_url` with retries (exponential backoff).

Setup

1. Create a Python 3.10+ virtualenv and install requirements:

   pip install -r requirements.txt

2. Set environment variables (do NOT commit these):

   - `AIPIPE_TOKEN` - token for AI Pipe (do not commit). The service expects this to be available at runtime.
   - `GITHUB_TOKEN` - GitHub personal access token (or ensure `gh` is authenticated via `gh auth login`).
   - `DEPLOY_SECRET` - shared secret used to authenticate incoming POST requests. This must be set before starting the server.

3. Run the app locally:

   uvicorn app:app --reload

API

POST /api-deploy

Request JSON (example):

```
{
   "email": "student@example.com",
   "secret": "<your DEPLOY_SECRET value>",
   "task": "captcha-solver-...",
   "round": 1,
   "nonce": "ABCD-123",
   "brief": "Create a captcha solver that handles ?url=https://.../image.png. Default to attached sample.",
   "checks": [
      "Repo has MIT license",
      "README.md is professional",
      "Page displays captcha URL passed at ?url=...",
      "Page displays solved captcha text within 15 seconds"
   ],
   "evaluation_url": "https://example.com/notify",
   "attachments": [{ "name": "sample.png", "url": "data:image/png;base64,iVBORw..." }]
}
```

Responses
- 200 OK: {"ok": true, "repo_url": ..., "commit_sha": ..., "pages_url": ...}
- 400 Bad Request: {"ok": false, "error": "..."} on validation failures

Deployment

- The main entrypoint is `app.py`. This can be deployed to Vercel (using serverless functions), Render, or Heroku. Ensure environment variables are set in the hosting environment.
- The service will create a new GitHub repo and attempt to enable GitHub Pages for the generated project. If automatic enabling fails, follow the Pages setup in the repository settings.

Docker

Build the Docker image:

```powershell
docker build -t llm-deploy:latest .
```

Run the container (PowerShell example):

```powershell
docker run -e AIPIPE_TOKEN="<token>" -e DEPLOY_SECRET="<secret>" -e GITHUB_TOKEN="<token>" -p 8000:8000 llm-deploy:latest
```

Notes:
- Do NOT put tokens in your Dockerfile or commit them. Provide them via environment variables or a secret manager.
- The service exposes port 8000.

Security notes

- The secret expected in the request body is hardcoded to `abc123xyz` for validation as required. Do NOT place any private tokens or secrets into the repository. Use environment variables instead.

Maintenance
Quick test (local)

1. Start the server:

    uvicorn app:app --reload

2. Send a POST using curl (PowerShell-friendly):

```powershell
$body = @'
{
   "email": "student@example.com",
   "secret": "<your DEPLOY_SECRET value>",
   "task": "captcha-solver-test",
   "round": 1,
   "nonce": "TEST-001",
   "brief": "Create a captcha solver that shows the image from ?url=... and solves it.",
   "checks": ["Repo has MIT license"],
   "evaluation_url": "https://example.com/notify",
   "attachments": []
}
'@

Invoke-RestMethod -Uri http://localhost:8000/api-deploy -Method POST -Body $body -ContentType 'application/json'
```

This should return a JSON response with `repo_url` and `commit_sha` if successful.

If GitHub automation fails due to missing tokens/gh CLI, the server still generates the project into `generated_repos/<task>-<nonce>`.


- Generated repos are stored in `generated_repos/` locally. This directory is gitignored by default.


API Usage
POST /api-deploy
- Accepts a JSON body matching the spec in the challenge. Validates `secret` against the hard-coded value `abc123xyz`.

Response
- 200 OK: {"ok": true, "repo_url": ..., "commit_sha": ..., "pages_url": ...}

Security
- Secrets must be provided as environment variables. The service writes `.gitignore` to ignore sensitive files.

License
MIT
