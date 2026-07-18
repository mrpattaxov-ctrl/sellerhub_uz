"""READ-ONLY: what would it COST to drop the worker's shop gate?

Abdulaziz wants the bg worker to sweep EVERY shop (today it skips any shop with
no FBS stock and no open active order — app.py _fbs_shop_has_fbs_stock /
_fbs_shop_has_open_active_orders; measured: 4 of 7 shops skipped).

The key structural fact that decides the cost: the sync is TOKEN-BATCHED. One
Uzum call per STATUS carries every shopId of that token as repeated ``shopIds=``
params (app.py _fbs_sync_one_token). So adding shops to a token group that
ALREADY makes calls adds ZERO extra requests — the shopIds just ride along in
the same query string. Extra cost appears only as:

  (a) a token group that currently makes NO calls at all (every shop gated out)
      starting to make its per-status calls, and
  (b) extra PAGES, if the newly-included shops push a status past 50 rows/page —
      one-time on the backfill, then steady-state.

This probe measures (b) exactly: for each token, it asks Uzum's /count for every
status the worker syncs, comparing the CURRENT batch (gated shops) with the FULL
batch (all shops), and converts the delta into pages (50 rows/page).

SAFETY: GET only, one /count per (token, status, batch). Paced through the shared
bucket. Nothing written.

Run:
    docker compose exec -e PYTHONPATH=/app app python tools/probe_fbs_gate_cost.py
"""
from __future__ import annotations

import math
import sys

from sqlalchemy import func, select

from extensions import SessionLocal
from models import Shop, User
from core.uzum_openapi import fetch_fbs_orders_count
from core.fbs_sync import FBS_ALL_SYNC_STATUSES, FBS_STATUS_SYNC_INTERVAL_MIN

import app as _app

PAGE = 50


def main() -> None:
    groups: dict[str, dict] = {}
    with SessionLocal() as db:
        users = db.execute(
            select(User).where(func.length(func.coalesce(User.uzum_openapi_token, "")) > 0)
            .order_by(User.id)
        ).scalars().all()
        for u in users:
            tok = (u.uzum_openapi_token or "").strip()
            shops = db.execute(select(Shop).where(Shop.owner_id == u.id)).scalars().all()
            if not shops:
                continue
            g = groups.setdefault(tok, {"users": [], "all": [], "gated": []})
            g["users"].append(int(u.id))
            for s in shops:
                sid = str(s.uzum_id)
                if sid in g["all"]:
                    continue
                g["all"].append(sid)
                if (_app._fbs_shop_has_fbs_stock(db, s.id)
                        or _app._fbs_shop_has_open_active_orders(db, s.uzum_id)):
                    g["gated"].append(sid)

    if not groups:
        sys.exit("No token groups.")

    grand_now = 0
    grand_after = 0

    for tok, g in groups.items():
        cur, allsh = g["gated"], g["all"]
        added = [s for s in allsh if s not in cur]
        cur_label = ", ".join(cur) if cur else "BITTA HAM YO'Q (fon 0 so'rov qiladi)"
        add_label = ", ".join(added) if added else "-"
        print("=== users=%s" % (g["users"],))
        print("    hozir sweep qilinadi : %s" % cur_label)
        print("    qo'shiladigan do'kon : %s" % add_label)
        if not added:
            print("    -> o'zgarish yo'q.\n")
            continue

        print("\n    %-38s %8s %8s   %s" % ("status", "hozirgi", "to'liq", "sahifa: hozir -> keyin"))
        pages_now = 0
        pages_after = 0
        for st in FBS_ALL_SYNC_STATUSES:
            iv = FBS_STATUS_SYNC_INTERVAL_MIN.get(st)
            if not iv:
                continue  # worker never syncs it (PENDING_DELIVERY)
            try:
                n_now = 0
                if cur:
                    n_now, _ = fetch_fbs_orders_count(tok, cur, status=st, fail_fast=True)
                n_all, _ = fetch_fbs_orders_count(tok, allsh, status=st, fail_fast=True)
            except Exception as e:
                print(f"    {st:<38} XATO {e!r}")
                continue
            p_now = max(1, math.ceil(int(n_now) / PAGE)) if cur else 0
            p_all = max(1, math.ceil(int(n_all) / PAGE))
            pages_now += p_now
            pages_after += p_all
            flag = "" if p_all == p_now else "   (+%d)" % (p_all - p_now)
            print("    %-38s %8s %8s   %6d -> %-6d%s" % (st, n_now, n_all, p_now, p_all, flag))

        print("\n    Bir to'liq aylanish (hamma status): %d sahifa -> %d sahifa"
              % (pages_now, pages_after))
        grand_now += pages_now
        grand_after += pages_after
        print()

    print("--- XULOSA ---")
    print("Bir to'liq sweep narxi: %d so'rov -> %d so'rov (farq %+d)."
          % (grand_now, grand_after, grand_after - grand_now))
    print("MUHIM: bu SAHIFA soni. So'rov soni statusga qarab o'z intervali bilan "
          "takrorlanadi (CREATED/PACKING 23 daq, DELIVERING+terminal 57-63 daq).")
    print("Kunlik kvota: 100 000 / token.")


if __name__ == "__main__":
    main()
