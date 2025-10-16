import requests
import time
import logging

logger = logging.getLogger('evaluator')


def notify_evaluator(url: str, payload: dict):
    headers = {'Content-Type': 'application/json'}
    backoff = [1, 2, 4, 8]
    for wait in backoff:
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=10)
            logger.info('Notify evaluator: %s -> %s', url, r.status_code)
            if r.status_code == 200:
                return True
        except Exception as e:
            logger.exception('Notify attempt failed')
        time.sleep(wait)
    raise RuntimeError('Failed to notify evaluator after retries')
