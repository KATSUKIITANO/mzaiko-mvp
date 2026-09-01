#!/usr/bin/env python3
"""eBay listing のトップ画像を「現在の n 枚目」に差し替える CLI。

使い方（プロジェクトルートから / API 経由ではなく直接 DB + eBay API を使う）:

    python -m scripts.set_primary_images 5222:2 5248:5 5259:2 5297:2 --dry-run
    python -m scripts.set_primary_images 5222:2 5248:5 5259:2 5297:2

docker compose 環境:

    docker compose exec api python -m scripts.set_primary_images 5222:2 --dry-run

引数は `<ref>:<position>`。ref は SKU / SKU の数字部分 / product id、
position は 1 始まり（2 なら「2枚目をトップにする」）。
--no-republish を付けると inventory item の更新だけ行い、再 publish しない。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from app.db.database import AsyncSessionLocal
from app.services.ebay_image_service import bulk_set_primary_image


def _parse_items(raw: list[str]) -> list[tuple[str, int]]:
    items: list[tuple[str, int]] = []
    for token in raw:
        if ":" not in token:
            raise SystemExit(f"形式エラー: '{token}' は <ref>:<position> で指定する")
        ref, _, pos = token.partition(":")
        if not ref.strip() or not pos.strip().isdigit() or int(pos) < 1:
            raise SystemExit(f"形式エラー: '{token}' は <ref>:<position(1以上)>")
        items.append((ref.strip(), int(pos)))
    return items


async def _run(items: list[tuple[str, int]], dry_run: bool, republish: bool) -> int:
    async with AsyncSessionLocal() as session:
        result = await bulk_set_primary_image(
            session, items=items, dry_run=dry_run, republish=republish
        )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    for r in result["results"]:
        mark = "OK " if r.get("success") else "NG "
        if r.get("success") and not r.get("changed"):
            mark = "-- "
        print(
            f"{mark}{r.get('ref')} position={r.get('position')} "
            f"sku={r.get('sku') or '-'} "
            f"{'(dry_run) ' if r.get('dry_run') else ''}"
            f"{r.get('error') or r.get('new_primary') or ''}",
            file=sys.stderr,
        )
    return 0 if result["failed"] == 0 else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("items", nargs="+", metavar="REF:POSITION")
    parser.add_argument(
        "--dry-run", action="store_true", help="eBay を呼ばず差し替え結果だけ表示"
    )
    parser.add_argument(
        "--no-republish", action="store_true", help="再 publish せず PUT のみ"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    raise SystemExit(
        asyncio.run(
            _run(_parse_items(args.items), args.dry_run, not args.no_republish)
        )
    )


if __name__ == "__main__":
    main()
