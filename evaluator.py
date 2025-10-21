import logging
import random
import socket
import time
from typing import Dict, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger('evaluator')


RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _dns_resolves(url: str) -> bool:
    try:
        host = requests.utils.urlparse(url).hostname
        if not host:
            return False
        socket.getaddrinfo(host, None)
        return True
    except Exception:
        return False


def _build_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1,  # 1s, 2s, 4s
        status_forcelist=RETRYABLE_STATUS,
        allowed_methods=frozenset({'POST'}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount('https://', adapter)
    s.mount('http://', adapter)
    return s


def notify_evaluator(
    url: str,
    payload: Dict,
    *,
    headers: Optional[Dict] = None,
    attempts: int = 4,
    timeout: float = 10.0,
) -> bool:
    base_headers = {'Content-Type': 'application/json', 'User-Agent': 'tds-evaluator/1.0'}
    if headers:
        base_headers.update(headers)

    if not url:
        logger.error('Evaluator URL is empty')
        raise RuntimeError('Evaluator URL is empty')

    # Fast‑fail if hostname cannot be resolved to avoid wasting retries
    if not _dns_resolves(url):
        logger.error('Evaluator hostname does not resolve: %s', url)
        raise RuntimeError('Evaluator hostname cannot be resolved')

    session = _build_session()

    for i in range(attempts):
        wait = (2 ** i) + random.random()  # exponential backoff with jitter
        try:
            logger.info('Notify evaluator: %s (attempt %d/%d)', url, i + 1, attempts)
            r = session.post(url, json=payload, headers=base_headers, timeout=timeout)
            logger.info('Notify status: %s', r.status_code)

            # Treat 2xx as success; many evaluators respond 202 Accepted
            if 200 <= r.status_code < 300:
                return True

            if r.status_code in RETRYABLE_STATUS and i < attempts - 1:
                logger.warning('Retryable response %s: %s', r.status_code, (r.text or '')[:500])
                time.sleep(wait)
                continue

            # Non-retryable
            logger.error('Non-retryable status %s: %s', r.status_code, (r.text or '')[:500])
            break

        except requests.exceptions.RequestException:
            if i < attempts - 1:
                logger.exception('Notify attempt failed (will retry)')
                time.sleep(wait)
                continue
            logger.exception('Notify attempt failed (no more retries)')
            break

    raise RuntimeError('Failed to notify evaluator after retries')

