"""Redis-backed job status for async bulk FBS actions (Bosqich 2).

``POST /fbs/api/orders/bulk-confirm-async`` kicks off a daemon thread that
confirms many orders, paced through the shared 2/s token bucket. A browser
can't hold a ~60s request open, so instead of blocking we track per-job
progress here and the front-end polls ``.../bulk-confirm-status/<job_id>``.

State lives in Redis (shared across gunicorn workers — the poll may hit a
different worker than the one that started the job) under
``fbs:bulkjob:<job_id>`` with a TTL so finished/abandoned jobs self-clean.

Only the single daemon thread that owns a job ever WRITES it, so the
read-modify-write in :func:`record_result` has no cross-writer race; the
poller only reads. Every Redis call is wrapped so a Redis outage degrades
gracefully (the confirms still run + write PACKING to the DB — only the
progress read-out is lost, and the front-end falls back to list polling).
"""
from __future__ import annotations

import json
import uuid

from core.redis_client import redis_client

_KEY = "fbs:bulkjob:{job_id}"
_TTL_SECONDS = 600  # finished/abandoned jobs self-expire after 10 min


def _key(job_id: str) -> str:
    return _KEY.format(job_id=job_id)


def _save(state: dict) -> None:
    try:
        redis_client.set(_key(state["job_id"]), json.dumps(state), ex=_TTL_SECONDS)
    except Exception as e:  # Redis down — confirms still run, only tracking lost
        print(f"[fbs.bulkjob] redis save failed job={state.get('job_id')}: {e!r}")


def new_job(user_id: int, total: int) -> str:
    """Create a job record and return its id.

    Returns a usable id even if the Redis write fails (the daemon still runs);
    the status endpoint would then 404 and the front-end falls back to list
    polling — confirms are never blocked on the tracker.
    """
    job_id = uuid.uuid4().hex
    _save({
        "job_id": job_id,
        "user_id": int(user_id),
        "total": int(total),
        "done": 0,
        "ok": 0,
        "failed": [],          # [{"order_id": int, "error": str, "code": str|None}]
        "status": "running",   # "running" | "done"
    })
    return job_id


def get_job(job_id: str) -> dict | None:
    try:
        raw = redis_client.get(_key(job_id))
    except Exception as e:
        print(f"[fbs.bulkjob] redis get failed job={job_id}: {e!r}")
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def record_result(job_id: str, order_id: int, *, ok: bool,
                  error: str | None = None, code: str | None = None) -> None:
    """Record one order's outcome. Single-writer (the job's daemon thread)."""
    state = get_job(job_id)
    if state is None:
        return  # expired / never existed / Redis down — nothing to update
    state["done"] = int(state.get("done", 0)) + 1
    if ok:
        state["ok"] = int(state.get("ok", 0)) + 1
    else:
        state.setdefault("failed", []).append({
            "order_id": int(order_id),
            "error": (error or "")[:200],
            "code": code,
        })
    _save(state)


def finish_job(job_id: str) -> None:
    state = get_job(job_id)
    if state is None:
        return
    state["status"] = "done"
    _save(state)
