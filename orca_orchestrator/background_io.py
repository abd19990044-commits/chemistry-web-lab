"""Bounded best-effort metadata I/O, never on the submission response path.

Calculation state and idempotency stay synchronous in SQLite/Kaggle. Cloudflare
replication is retried by subsequent status/sync calls if a task cannot run.
"""
from concurrent.futures import ThreadPoolExecutor
import logging
import threading

log = logging.getLogger('orca.background_io')
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='orca-metadata')
_slots = threading.BoundedSemaphore(32)
_lock = threading.Lock()
_pending = set()


def defer(key, action):
    with _lock:
        if key in _pending or not _slots.acquire(blocking=False):
            return False
        _pending.add(key)
    def run():
        try:
            action()
        except Exception as exc:
            # Do not include payloads, credentials or exception messages.
            log.warning('Deferred metadata operation failed: %s', type(exc).__name__)
        finally:
            with _lock:
                _pending.discard(key)
            _slots.release()
    try:
        _pool.submit(run)
    except RuntimeError:
        with _lock:
            _pending.discard(key)
        _slots.release()
        return False
    return True
