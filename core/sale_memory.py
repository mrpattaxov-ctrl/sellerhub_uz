"""Per-user "price memory" for Uzum sale enrollment.

Every time a seller enrolls SKUs into a sale through our app we remember the
exact sale price they set for each SKU. When they open a *new* sale later, the
picker's «Автозаполнить» button reads this back and refills the same prices, so
a repeating campaign doesn't have to be re-priced from scratch.

Cache-only by design — this lives entirely in Redis (a per-user hash), never in
the DB. Losing it is harmless: the next enrollment repopulates it, and every
read/write is best-effort (a Redis outage degrades autofill to "nothing
remembered", it never breaks enrollment or the page).

    Key    : ``salemem:{uid}``           (one hash per user)
    Field  : ``{sku_id}``                (stringified Uzum skuId)
    Value  : ``{"price": <int>, "ts": <iso8601>}``  (JSON)
    TTL    : ~6 months, refreshed on every write
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from core.redis_client import redis_client

logger = logging.getLogger(__name__)

# ~6 months. Long enough that a seasonal campaign a few months apart still finds
# its prices; re-set on every write so an actively-used shop never expires.
_TTL_SECONDS = 180 * 24 * 3600


def _key(uid: int | str) -> str:
    return f"salemem:{int(uid)}"


def remember_sale_prices(uid: int | str, sku_price_map: dict) -> None:
    """Store each SKU's last sale price for the user and refresh the TTL.

    ``sku_price_map``: ``{sku_id: price}`` (ids/prices may be str or int).
    Non-positive or unparseable entries are skipped. Best-effort — any Redis
    error is logged and swallowed, because the enrollment that produced these
    prices has already succeeded and must not be reported as failed.
    """
    if not sku_price_map:
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    mapping: dict[str, str] = {}
    for sid, price in sku_price_map.items():
        try:
            p = int(round(float(price)))
            key_sid = str(int(sid))
        except (TypeError, ValueError):
            continue
        if p <= 0:
            continue
        mapping[key_sid] = json.dumps({"price": p, "ts": now})
    if not mapping:
        return
    try:
        key = _key(uid)
        pipe = redis_client.pipeline()
        pipe.hset(key, mapping=mapping)
        pipe.expire(key, _TTL_SECONDS)
        pipe.execute()
    except Exception:
        logger.warning("sale_memory: store failed for uid=%s", uid, exc_info=True)


def get_remembered_prices(uid: int | str) -> dict[str, int]:
    """Return ``{sku_id(str): price(int)}`` remembered for the user.

    Best-effort: returns ``{}`` on any Redis error or if nothing is stored.
    """
    try:
        raw = redis_client.hgetall(_key(uid))
    except Exception:
        logger.warning("sale_memory: read failed for uid=%s", uid, exc_info=True)
        return {}
    out: dict[str, int] = {}
    for sid, val in (raw or {}).items():
        try:
            p = int(json.loads(val).get("price"))
        except (TypeError, ValueError):
            continue
        if p > 0:
            out[str(sid)] = p
    return out
