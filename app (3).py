from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import base64
import os
import uuid
import json
import logging
from ai_client import AIPipeClient
from github_ops import GitHubOps
from evaluator import notify_evaluator
import requests
import shutil

# Do NOT hardcode secrets. Read the deployment secret from environment.
APP_SECRET = os.environ.get("DEPLOY_SECRET")
if not APP_SECRET:
    # Fail fast: require configuration of DEPLOY_SECRET at startup. This prevents leaking secrets into code.
    raise RuntimeError("DEPLOY_SECRET environment variable is required. Set DEPLOY_SECRET to the shared secret.")

# Initialize logging early so we can log directory creation/debug info
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("llm-deploy")

# Use a relative repos directory by default to avoid permission issues inside containers.
# Allow overriding via environment variable DEPLOY_REPOS_DIR if needed.
REPOS_DIR = os.environ.get("DEPLOY_REPOS_DIR", "generated_repos")

# Debug info: print current working directory and attempt to create the repos dir.
logger.info("Current working directory: %s", os.getcwd())
logger.info("Attempting to create repos directory at relative path: %s", REPOS_DIR)
try:
    os.makedirs(REPOS_DIR, exist_ok=True)
    logger.info("Created/verified repos directory: %s", REPOS_DIR)
except Exception:
    logger.exception("Failed to create repos directory '%s' in cwd %s; will try /tmp fallback", REPOS_DIR, os.getcwd())
    REPOS_DIR = "/tmp/generated_repos"
    try:
        os.makedirs(REPOS_DIR, exist_ok=True)
        logger.info("Created fallback repos directory: %s", REPOS_DIR)
    except Exception:
        logger.exception("Failed to create fallback repos directory '%s' - cannot proceed", REPOS_DIR)
        raise

app = FastAPI()


class Attachment(BaseModel):
    name: str
    url: str


class DeployRequest(BaseModel):
    email: str
    secret: str
    task: str
    round: int
    nonce: str
    brief: str
    checks: List[str]
    evaluation_url: str
    attachments: Optional[List[Attachment]] = []
    # Optional explicit template name to deterministically select a generator
    template: Optional[str] = None


def save_attachments(attachments: List[Attachment], target_dir: str) -> List[str]:
    saved = []
    os.makedirs(target_dir, exist_ok=True)
    for a in attachments:
        try:
            if not a.url:
                raise ValueError("attachment url is empty")
            path = os.path.join(target_dir, a.name)
    # Save attachments at repo root (per instructor spec)
            # Handle data URIs (data:<mime>;base64,AAAA)
            if a.url.startswith("data:"):
                if "," not in a.url:
                    raise ValueError("invalid data URI: missing comma separator")
                header, data = a.url.split(",", 1)
                if ";base64" in header:
                    b = base64.b64decode(data)
                else:
                    # Not base64 -- treat as raw text
                    b = data.encode("utf-8")
                with open(path, "wb") as f:
                    f.write(b)
                saved.append(path)
                logger.info(f"Saved data-uri attachment {a.name} -> {path}")
            # HTTP/HTTPS URL: download
            elif a.url.startswith("http://") or a.url.startswith("https://"):
                r = requests.get(a.url, timeout=10)
                r.raise_for_status()
                with open(path, "wb") as f:
                    f.write(r.content)
                saved.append(path)
                logger.info(f"Downloaded attachment {a.name} -> {path} from {a.url}")
            else:
                # Fallback: try raw base64 (no data: header)
                try:
                    b = base64.b64decode(a.url)
                    with open(path, "wb") as f:
                        f.write(b)
                    saved.append(path)
                    logger.info(f"Saved base64 attachment {a.name} -> {path}")
                except Exception:
                    raise ValueError("unsupported attachment url format; expected data URI, http(s) URL, or base64 string")
        except Exception as e:
            logger.exception("Failed to save attachment %s (url: %.200s)", a.name, str(a.url))
            raise
    return saved


@app.post("/api-deploy")
async def api_deploy(req: Request):
    # Read evaluator skip flag early to avoid UnboundLocalError on error paths
    skip_eval = os.environ.get('SKIP_EVALUATOR', '').lower() in ('1', 'true', 'yes')
    try:
        body = await req.body()
        if not body:
            logger.warning("Empty request body received")
            raise HTTPException(status_code=400, detail="Empty request body")
        payload = json.loads(body)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Invalid JSON payload: %s", e)
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    try:
        dr = DeployRequest(**payload)
    except Exception as e:
        logger.exception("Payload validation failed")
        raise HTTPException(status_code=400, detail=f"Payload validation failed: {e}")

    if dr.secret != APP_SECRET:
        logger.warning("Invalid secret provided")
        return JSONResponse(status_code=400, content={"ok": False, "error": "invalid secret"})

    # Prepare repo folder (use task+nonce as unique repo name)
    repo_name = dr.task.replace(' ', '-').lower()
    repo_path = os.path.join(REPOS_DIR, repo_name)
    os.makedirs(repo_path, exist_ok=True)

    # Save attachments
    saved_files = []
    try:
        # Save attachments directly into repo root
        saved_files = save_attachments(dr.attachments or [], repo_path)
    except Exception as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": "failed saving attachments"})

    # Use AI to generate or update project
    ai = AIPipeClient()
    try:
        # Pass task and round so the AI client can perform round-aware updates
        generated = ai.generate_project(brief=dr.brief, checks=dr.checks, attachments=saved_files, target_dir=repo_path, task=dr.task, round=dr.round)
    except Exception as e:
        logger.exception("AI generation failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": "AI generation failed"})
    # Create & push GitHub repo (can be skipped for local testing)
    skip_github = os.environ.get('SKIP_GITHUB', '').lower() in ('1', 'true', 'yes')
    repo_url = ''
    commit_sha = ''
    pages_url = ''
    if skip_github:
        logger.info('SKIP_GITHUB is set; skipping GitHub operations. Using local repo path.')
        repo_url = f"file://{os.path.abspath(repo_path)}"
    else:
        gh = GitHubOps()
        try:
            # If repo folder already contains a git repo, treat this as a revision/update
            repo_url, commit_sha, pages_url = gh.create_and_push_repo(repo_path, repo_name)
        except Exception as e:
            logger.exception("GitHub operations failed")
            return JSONResponse(status_code=500, content={"ok": False, "error": "GitHub operations failed"})
    callback_payload = {
        "email": dr.email,
        "task": dr.task,
        "round": dr.round,
        "nonce": dr.nonce,
        "repo_url": repo_url,
        "commit_sha": commit_sha,
        "pages_url": pages_url,
    }
    # Notify evaluator (can be skipped for testing)
    if skip_eval:
        logger.info('SKIP_EVALUATOR is set; skipping evaluator notification')
    else:
        try:
            notify_evaluator(dr.evaluation_url, callback_payload)
        except Exception as e:
            logger.exception("Evaluator notification failed")

    return {"ok": True, **callback_payload}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/debug")
def debug_info():
    """Return runtime diagnostics helpful for debugging token and tool availability.

    This endpoint intentionally does not return secret values. It only reports presence
    and basic GitHub user info when possible.
    """
    info = {
        "cwd": os.getcwd(),
        "repos_dir": REPOS_DIR,
        "GITHUB_TOKEN_set": bool(os.environ.get('GITHUB_TOKEN')),
        "AIPIPE_TOKEN_set": bool(os.environ.get('AIPIPE_TOKEN')),
        "DEPLOY_SECRET_set": bool(os.environ.get('DEPLOY_SECRET')),
        "git_available": shutil.which('git') is not None,
        "gh_available": shutil.which('gh') is not None,
    }

    # If token is present, try to GET /user to validate token and show login
    token = os.environ.get('GITHUB_TOKEN')
    if token:
        try:
            headers = {'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}
            r = requests.get('https://api.github.com/user', headers=headers, timeout=5)
            info['github_api_status'] = r.status_code
            if r.status_code == 200:
                info['github_user'] = r.json().get('login')
            else:
                info['github_user'] = None
                # include truncated body for debugging (no tokens)
                info['github_api_body'] = r.text[:1000]
        except Exception as e:
            info['github_api_status'] = 'error'
            info['github_api_error'] = str(e)

    return info
