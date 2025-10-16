import os
import logging
import subprocess
import tempfile
from typing import Tuple
import base64
import json
import requests

logger = logging.getLogger('github-ops')


def _run(cmd, cwd=None):
    logger.info('Run: %s', ' '.join(cmd))
    r = subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)
    logger.info(r.stdout)
    return r.stdout.strip()


class GitHubOps:
    """Simple GitHub automation using either the GitHub REST API (preferred) or local git.

    Environment variables expected:
    - GITHUB_TOKEN (if present, used to call the GitHub REST API)

    This class creates a new public repo under the authenticated user/org and uploads files.
    It attempts to enable GitHub Pages via the REST API when possible.
    """

    def __init__(self, token_env: str = 'GITHUB_TOKEN'):
        self.token = os.environ.get(token_env)
        if not self.token:
            logger.warning('GITHUB_TOKEN not set; github operations may fail')

    def create_and_push_repo(self, local_dir: str, repo_name: str) -> Tuple[str, str, str]:
        # Initialize git if not already
        if not os.path.exists(os.path.join(local_dir, '.git')):
            _run(['git', 'init'], cwd=local_dir)
        # Create .gitignore if missing
        gi = os.path.join(local_dir, '.gitignore')
        if not os.path.exists(gi):
            with open(gi, 'w') as f:
                f.write('.env\nAIPIPE_TOKEN\n')
        # Ensure .nojekyll exists so GitHub Pages serves static assets without Jekyll
        nj = os.path.join(local_dir, '.nojekyll')
        if not os.path.exists(nj):
            try:
                with open(nj, 'w') as f:
                    f.write('')
            except Exception:
                logger.exception('Failed to create .nojekyll')

        _run(['git', 'add', '.'], cwd=local_dir)
        try:
            _run(['git', 'commit', '-m', 'Initial commit from LLM deploy'], cwd=local_dir)
        except Exception:
            # likely nothing to commit; try creating an initial empty commit so HEAD exists
            logger.info('No new changes to commit; attempting initial empty commit to create HEAD')
            try:
                _run(['git', 'commit', '--allow-empty', '-m', 'Initial empty commit from LLM deploy'], cwd=local_dir)
            except Exception:
                logger.info('Initial empty commit failed')

        # Prefer using GitHub REST API if token available; otherwise try manual git push
        url = ''
        pages_owner = None
        token = self.token
        if token:
            logger.info('GITHUB_TOKEN detected: attempting GitHub REST API repo creation and file upload')
            try:
                headers = {'Authorization': f'token {token}', 'Accept': 'application/vnd.github+json'}
                r = requests.get('https://api.github.com/user', headers=headers, timeout=10)
                r.raise_for_status()
                owner = r.json().get('login')
                pages_owner = owner

                # Create repo under the authenticated user (ignore if already exists)
                pr = requests.post('https://api.github.com/user/repos', headers=headers, json={'name': repo_name, 'private': False}, timeout=10)
                if pr.status_code == 201:
                    logger.info('Created GitHub repo %s/%s', owner, repo_name)
                elif pr.status_code == 422:
                    logger.info('Repo already exists (422)')
                else:
                    logger.warning('Create repo returned %s: %s', pr.status_code, pr.text)
                    pr.raise_for_status()

                url = f'https://github.com/{owner}/{repo_name}.git'

                # Upload files by walking local_dir and using contents API
                for root, dirs, files in os.walk(local_dir):
                    # avoid descending into .git or other build cache folders
                    if '.git' in dirs:
                        dirs.remove('.git')
                    if '__pycache__' in dirs:
                        dirs.remove('__pycache__')
                    for fname in files:
                        full = os.path.join(root, fname)
                        rel = os.path.relpath(full, start=local_dir).replace('\\', '/')

                        # Skip hidden/system files and folders (but allow .gitignore, .gitattributes, .nojekyll)
                        allowed_dot_files = {'.gitignore', '.gitattributes', '.nojekyll'}
                        parts = rel.split('/')
                        skip = False
                        for p in parts:
                            if p.startswith('.') and p not in allowed_dot_files:
                                skip = True
                                break
                            if p == '__pycache__':
                                skip = True
                                break
                        if skip:
                            logger.info('Skipping hidden/system file or folder: %s', rel)
                            continue

                        with open(full, 'rb') as fh:
                            content_b64 = base64.b64encode(fh.read()).decode('ascii')
                        put_url = f'https://api.github.com/repos/{owner}/{repo_name}/contents/{rel}'

                        # Check if the file already exists to obtain its sha (required for updates)
                        try:
                            gr = requests.get(put_url, headers=headers, timeout=10)
                            if gr.status_code == 200:
                                existing_sha = gr.json().get('sha')
                            else:
                                existing_sha = None
                        except Exception:
                            existing_sha = None

                        payload = {'message': f'Add {rel}', 'content': content_b64}
                        if existing_sha:
                            payload['sha'] = existing_sha
                            payload['message'] = f'Update {rel}'

                        upr = requests.put(put_url, headers=headers, json=payload, timeout=30)
                        if upr.status_code not in (201, 200):
                            logger.warning('Uploading %s returned %s: %s', rel, upr.status_code, upr.text)
                logger.info('Uploaded files via GitHub API to %s', url)
            except Exception:
                logger.exception('GitHub REST API attempt failed')
        else:
            # No token: try manual git push if git user is configured
            try:
                try:
                    user = _run(['git', 'config', '--get', 'user.name'])
                except Exception:
                    user = None
                if not user:
                    user = os.environ.get('GITHUB_ACTOR') or os.environ.get('GITHUB_USER') or None
                if user:
                    url = f"https://github.com/{user}/{repo_name}.git"
                    pages_owner = user
                    try:
                        _run(['git', 'remote', 'add', 'origin', url], cwd=local_dir)
                    except Exception:
                        logger.info('remote origin already exists')
                    _run(['git', 'branch', '-M', 'main'], cwd=local_dir)
                    _run(['git', 'push', '-u', 'origin', 'main'], cwd=local_dir)
                else:
                    raise RuntimeError('No git user configured and no GITHUB_TOKEN available')
            except Exception:
                logger.exception('Manual git push failed')

        # Get latest commit sha
        try:
            sha = _run(['git', 'rev-parse', 'HEAD'], cwd=local_dir)
        except Exception:
            logger.exception('Failed to resolve git HEAD; returning empty sha')
            sha = ''

        # Try to enable GitHub Pages via REST API if we have a token and owner; always compute a fallback URL
        pages_url = ''
        if token and 'owner' in locals():
            try:
                headers = {'Authorization': f'token {token}', 'Accept': 'application/vnd.github+json'}
                pages_payload = {'source': {'branch': 'main', 'path': '/'}}
                pr = requests.post(f'https://api.github.com/repos/{owner}/{repo_name}/pages', headers=headers, json=pages_payload, timeout=15)
                if pr.status_code in (201, 204, 409, 422):
                    logger.info('Pages enablement response %s', pr.status_code)
                else:
                    logger.warning('Enable Pages returned %s: %s', pr.status_code, pr.text)
                # Poll for html_url
                get_url = f'https://api.github.com/repos/{owner}/{repo_name}/pages'
                html_url = ''
                for attempt in range(10):
                    try:
                        gr = requests.get(get_url, headers=headers, timeout=10)
                        if gr.status_code == 200:
                            j = gr.json()
                            html_url = j.get('html_url') or ''
                            status = j.get('status') or ''
                            logger.info('Pages status: %s html_url=%s', status, html_url)
                            if html_url:
                                pages_url = html_url
                                break
                    except Exception:
                        logger.exception('Error polling Pages status')
                    try:
                        import time as _t
                        _t.sleep(2)
                    except Exception:
                        pass
            except Exception:
                logger.exception('GitHub Pages enablement via REST API failed')
        else:
            logger.info('Skipping GitHub Pages enablement: no GITHUB_TOKEN or owner available')

        # As a final fallback, synthesize the Pages URL from known owner/user
        if not pages_url:
            # Try from pages_owner first; if missing, parse from repo URL
            if pages_owner:
                pages_url = f'https://{pages_owner}.github.io/{repo_name}/'
            else:
                try:
                    # url like https://github.com/{owner}/{repo}.git
                    parts = url.split('/')
                    guessed_owner = parts[-2] if len(parts) >= 2 else ''
                    if guessed_owner:
                        pages_url = f'https://{guessed_owner}.github.io/{repo_name}/'
                except Exception:
                    # leave pages_url empty as last resort
                    pass

        return url, sha, pages_url
