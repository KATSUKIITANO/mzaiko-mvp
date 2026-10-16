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

「9742 3枚目」形式のリストをそのまま貼り付けて渡すこともできる:

    python -m scripts.set_primary_images --from-text - --dry-run <<'EOF'
    9742 3枚目
    9749 3枚目
    9755 2枚目
    EOF

    python -m scripts.set_primary_images --from-text list.txt
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys

from app.db.database import AsyncSessionLocal
from app.services.ebay_image_service import bulk_set_primary_image


# 「9742 3枚目」「9742:3」「9742 3」を拾う。
# 「9786 2枚目トップ画像変更」のように枚目の後ろに文字が続くのも許す
# （貼り付け時に指示文が最終行にくっつくため）。
_TEXT_LINE = re.compile(
    r"^\s*(\S+?)\s*[:\s]\s*(\d+)\s*(?:(?:枚目|枚|番目)\s*\S*)?\s*$"
)


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


def _parse_text(text: str) -> list[tuple[str, int]]:
    """「9742 3枚目」形式のリストを貼り付けたまま解釈する。"""
    items: list[tuple[str, int]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        m = _TEXT_LINE.match(line)
        if m is None:
            raise SystemExit(
                f"形式エラー: {lineno}行目 '{line.strip()}' を解釈できない"
                "（例: '9742 3枚目'）"
            )
        position = int(m.group(2))
        if position < 1:
            raise SystemExit(f"形式エラー: {lineno}行目 position は 1 以上")
        items.append((m.group(1), position))

    if not items:
        raise SystemExit("入力が空: 対象が1件も読み取れない")
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
    parser.add_argument("items", nargs="*", metavar="REF:POSITION")
    parser.add_argument(
        "--from-text",
        metavar="FILE",
        help="「9742 3枚目」形式のリストを読む（- で標準入力）",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="eBay を呼ばず差し替え結果だけ表示"
    )
    parser.add_argument(
        "--no-republish", action="store_true", help="再 publish せず PUT のみ"
    )
    args = parser.parse_args()

    if args.from_text:
        if args.items:
            raise SystemExit("--from-text と REF:POSITION は同時に指定できない")
        text = (
            sys.stdin.read()
            if args.from_text == "-"
            else open(args.from_text, encoding="utf-8").read()
        )
        items = _parse_text(text)
    elif args.items:
        items = _parse_items(args.items)
    else:
        parser.error("REF:POSITION か --from-text のどちらかが必要")

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    print(
        "対象 " + str(len(items)) + "件: "
        + " ".join(f"{r}:{p}" for r, p in items),
        file=sys.stderr,
    )
    raise SystemExit(
        asyncio.run(_run(items, args.dry_run, not args.no_republish))
    )


if __name__ == "__main__":
    main()
