"""Uzum API response parsing helpers.

All Uzum quantity parsing must go through _safe_qty / _extract_uzum_qty.
"""
from __future__ import annotations

import json


def _safe_qty(val):
    """Convert common Uzum numeric-ish qty structures to int."""
    if val is None:
        return 0
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return 0
        try:
            return int(float(s.replace(",", ".")))
        except Exception:
            return 0
    if isinstance(val, dict):
        # Prefer likely keys first
        for k in (
            "quantityActive","quantity","qty","available","availableQty","stock","stockQty","stockQuantity",
            "left","leftQty","leftovers","balance","amount","value","count",
            "warehouseQty","warehouseQuantity","inStock","onStock","onHand","free","total"
        ):
            if k in val:
                q = _safe_qty(val.get(k))
                if q != 0:
                    return q
        # fallback sum
        return sum(_safe_qty(v) for v in val.values())
    if isinstance(val, (list, tuple)):
        return sum(_safe_qty(x) for x in val)
    return 0


def _extract_uzum_qty(obj: dict) -> int:
    """Best-effort quantity extraction.

    Uzum responses are not stable across endpoints/versions. We:
    1) check common direct keys
    2) check common nested containers
    3) do a shallow recursive scan and pick the *best-looking* numeric field
    """
    if not isinstance(obj, dict):
        return 0

    direct_keys = [
        # most common
        "quantityActive",
        "availableAmount", "availableQty", "availableQuantity", "available",
        "stockQty", "stockQuantity", "stock",
        "quantity", "qty",
        # other variants
        "left", "leftQty", "leftovers", "leftover", "remain", "remains", "rest", "balance",
        "warehouseQty", "warehouseQuantity", "freeStock", "free", "onStock", "inStock", "onHand", "on_hand",
        "totalAvailable", "totalQty",
        # our own stored field names
        "uzumQty", "uzumQuantity", "uzum_quantity",
    ]

    for k in direct_keys:
        if k in obj:
            q = _safe_qty(obj.get(k))
            if q != 0:
                return q

    nested_containers = [
        "stocks", "stockList", "warehouseStocks", "warehouseStock",
        "inventories", "inventory", "availability",
        "remainsByWarehouse", "leftoversByWarehouse",
        "warehouse", "warehouses", "stores", "storeStocks",
    ]
    for k in nested_containers:
        if k in obj:
            q = _safe_qty(obj.get(k))
            if q != 0:
                return q

    st = obj.get("status")
    if isinstance(st, dict):
        for k in direct_keys:
            if k in st:
                q = _safe_qty(st.get(k))
                if q != 0:
                    return q
        add = st.get("additional")
        if add is not None:
            q = _safe_qty(add)
            if q != 0:
                return q

    # last-resort recursive scan (bounded)
    best_score = -1
    best_qty = 0

    def _score_key(key: str) -> int:
        k = (key or "").lower()
        score = 0
        if "qty" in k or "quantity" in k:
            score += 5
        if "stock" in k or "remain" in k or "left" in k or "available" in k:
            score += 4
        if "price" in k or "cost" in k or "amount" == k:
            score -= 3
        return score

    def _walk(node, depth: int = 0):
        nonlocal best_score, best_qty
        if depth > 4:
            return
        if isinstance(node, dict):
            for kk, vv in node.items():
                if isinstance(vv, (int, float, str, bool)):
                    q = _safe_qty(vv)
                    if q == 0:
                        continue
                    sc = _score_key(str(kk))
                    # prefer more plausible qty range (avoid gigantic accidental numbers)
                    if q > 1000000:
                        sc -= 2
                    if sc > best_score:
                        best_score, best_qty = sc, q
                else:
                    _walk(vv, depth + 1)
        elif isinstance(node, (list, tuple)):
            for vv in node:
                _walk(vv, depth + 1)

    _walk(obj, 0)
    return int(best_qty or 0)


def _extract_sku(row: dict) -> str:
    """Extract a SKU-like identifier from a row; fallback to barcode/id."""
    if not isinstance(row, dict):
        return ""
    for k in (
        "skuFullTitle", "skuTitle",
        "sku","sellerSku","merchantSku","vendorCode","offerId","article","code","productSku","skuCode",
        "productSkuId","skuId","id","seller_sku","merchant_sku"
    ):
        v = row.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    # fallback to barcode
    for k in ("barcode","ean","gtin"):
        v = row.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    # last resort
    pid = row.get("productId") or row.get("product_id")
    vid = row.get("id") or row.get("skuId")
    if pid or vid:
        return f"{pid or 'p'}-{vid or 'v'}"
    return ""


def _collect_variant_rows(item: dict) -> list:
    """Collect possible variant/SKU rows from an Uzum product item."""
    rows = []
    if isinstance(item, dict):
        # if item itself looks like a SKU row (has barcode or sku-ish keys)
        if any(k in item for k in ("sku","sellerSku","merchantSku","vendorCode","offerId","barcode","skuTitle","skuCode","skuId")):
            rows.append(item)
        # nested lists
        for k in ("skuList","skus","variants","offers","offerList","items","sku_table"):
            v = item.get(k)
            if isinstance(v, list) and v:
                rows.extend([x for x in v if isinstance(x, dict)])
        # sometimes nested under payload/data
        for k in ("payload","data","result"):
            v = item.get(k)
            if isinstance(v, dict):
                for kk in ("skuList","skus","variants","offers","items"):
                    vv = v.get(kk)
                    if isinstance(vv, list) and vv:
                        rows.extend([x for x in vv if isinstance(x, dict)])
    # de-dup by object id
    seen = set()
    out = []
    for r in rows:
        key = id(r)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _safe_status_text(val):
    """Uzum sometimes returns status as an object. Normalize it to text."""
    if val is None:
        return None
    if isinstance(val, (str, int, float, bool)):
        return str(val)
    if isinstance(val, dict):
        # Prefer human-friendly fields if present
        for k in ("title", "value", "name", "code", "status"):
            if k in val and val[k] is not None:
                return str(val[k])
        return json.dumps(val, ensure_ascii=False)
    if isinstance(val, (list, tuple)):
        return json.dumps(val, ensure_ascii=False)
    return str(val)


def _safe_text(val):
    if val is None:
        return None
    if isinstance(val, (str, int, float, bool)):
        s = str(val).strip()
        return s if s else None
    return json.dumps(val, ensure_ascii=False)
