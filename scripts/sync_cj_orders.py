#!/usr/bin/env python3
"""eBay で売れた注文を CJ に反映する CLI。

使い方（docker compose 環境なら `docker compose exec api` を前置）:

    # 1. まず発注できない SKU を洗い出す（マッピング未登録の商品）
    python -m scripts.sync_cj_orders --unmapped

    # 2. payload だけ作って内容を確認（CJ API は呼ばない）
    python -m scripts.sync_cj_orders --dry-run

    # 3. 本発注 + 状態同期 + tracking の eBay 反映
    python -m scripts.sync_cj_orders

    # 特定の注文だけ
    python -m scripts.sync_cj_orders --order 25-15185-11974 --dry-run

    # 現在の発注状況サマリ
    python -m scripts.sync_cj_orders --status

--dry-run を付けない場合でも、CJ_ORDER_DRY_RUN=true の環境では
payload 生成のみで CJ API は呼ばれない（既定は安全側）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import Counter

from app.config import settings
from app.db.database import AsyncSessionLocal
from app.services.cj_order_service import (
    CJOrderError,
    list_cj_orders,
    list_unmapped_skus,
    place_cj_order_for_ebay_order,
    run_cj_order_worker,
)


async def _show_unmapped() -> int:
    async with AsyncSessionLocal() as session:
        rows = await list_unmapped_skus(session)

    if not rows:
        print("マッピング未登録の SKU はありません（全件発注可能）")
        return 0

    total = sum(r["order_count"] for r in rows)
    print(f"発注できない SKU: {len(rows)}件（対象注文 {total}件）\n")
    for r in rows:
        title = (r["title"] or "")[:50]
        print(f"  {r['sku']:<24} 注文{r['order_count']:>3}件  {title}")
        print(f"      例: {', '.join(r['ebay_order_ids'])}")

    print("\n以下で CJ 商品を紐付けてください（cj_product_id は CJ の pid）:")
    print(f"""
  curl -X POST "http://localhost:8000/cj/mappings" \\\\
    -H "X-API-Key: $KEY" -H "Content-Type: application/json" \\\\
    -d '{{"sku": "{rows[0]['sku']}", "cj_product_id": "<CJのpid>"}}'
""")
    return 1


async def _show_status() -> int:
    async with AsyncSessionLocal() as session:
        rows = await list_cj_orders(session, status=None, limit=500, offset=0)

    if not rows:
        print("CJ 発注の記録はまだありません")
        return 0

    counts = Counter(r.status for r in rows)
    print(f"CJ 発注 {len(rows)}件（直近500件）")
    for status, n in counts.most_common():
        print(f"  {status:<12} {n:>4}件")

    attention = [r for r in rows if r.status in ("returned", "failed", "unmapped")]
    if attention:
        print("\n要対応:")
        for r in attention[:20]:
            print(
                f"  [{r.status}] {r.ebay_order_id} sku={r.sku} "
                f"{(r.error_message or r.cj_status or '')[:60]}"
            )
    return 0


async def _run(order_ref: str | None, dry_run: bool | None, force: bool) -> int:
    if order_ref:
        async with AsyncSessionLocal() as session:
            try:
                result = await place_cj_order_for_ebay_order(
                    session, order_ref, dry_run=dry_run, force=force
                )
            except CJOrderError as e:
                print(f"エラー: {e}", file=sys.stderr)
                return 1
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print(
            f"\n{result['ebay_order_id']}: {result['status']} "
            f"小包{result['parcel_count']}個"
            + (f" risk={result['customs_risk_level']}" if result.get("customs_risk_level") else "")
            + (f"\n  エラー: {result['error']}" if result.get("error") else ""),
            file=sys.stderr,
        )
        return 0 if result["success"] or result["skipped"] else 1

    metrics = await run_cj_order_worker()
    d = metrics.as_dict()
    print(json.dumps(d, ensure_ascii=False, indent=2))
    print(
        f"\n発注 {d['placed']}件 / 未マッピング {d['unmapped']}件 / "
        f"発注失敗 {d['place_failed']}件 / 状態同期 {d['synced']}件 / "
        f"出荷 {d['shipped']}件 / tracking反映 {d['tracking_pushed']}件",
        file=sys.stderr,
    )
    return 1 if (d["place_failed"] or d["unmapped"]) else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unmapped", action="store_true",
                        help="発注できない（マッピング未登録の）SKU を一覧表示")
    parser.add_argument("--status", action="store_true",
                        help="CJ 発注の状況サマリを表示")
    parser.add_argument("--order", metavar="EBAY_ORDER_ID",
                        help="指定した eBay 注文だけ発注する")
    parser.add_argument("--dry-run", action="store_true",
                        help="CJ API を呼ばず payload だけ作る")
    parser.add_argument("--force", action="store_true",
                        help="失敗回数の上限を無視して再発注")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    if args.unmapped:
        raise SystemExit(asyncio.run(_show_unmapped()))
    if args.status:
        raise SystemExit(asyncio.run(_show_status()))

    if not settings.cj_order_enabled:
        print("CJ_ORDER_ENABLED=false のため何もしません", file=sys.stderr)
        raise SystemExit(1)

    dry_run = True if args.dry_run else None
    effective = settings.cj_order_dry_run if dry_run is None else dry_run
    print(
        f"CJ発注を実行します（dry_run={effective}）", file=sys.stderr
    )
    raise SystemExit(asyncio.run(_run(args.order, dry_run, args.force)))


if __name__ == "__main__":
    main()
