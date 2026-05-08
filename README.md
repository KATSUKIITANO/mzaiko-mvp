# 無在庫販売 MVP

eBay 出品 + revise + sold sync + fulfillment queue + 配送見積 + **国別リスクスコアリング** を備えた MVP。

## 機能一覧（追加分）

| エンドポイント | 認証 | 機能 |
| --- | --- | --- |
| `GET /risk/countries` | X-API-Key | 国別リスクマッピング一覧（blocked/high/medium + score + buffer 調整 %） |
| `GET /risk/tasks` | X-API-Key | リスクレベル別の fulfillment task 一覧 |
| `GET /cj/search` | X-API-Key | CJ Dropshipping 商品検索（keyword） |
| `GET /cj/product/{pid}` | X-API-Key | CJ 商品詳細（画像URL含む） |
| `GET /cj/product/{pid}/images` | X-API-Key | CJ 商品の画像URL一覧 |
| `POST /ebay/listing/create` | X-API-Key | CJ画像を取得して eBay 出品（dry_run対応） |
| `POST /ebay/listing/preview` | X-API-Key | eBay未接続でも最終payloadを完全確認（API一切呼ばない） |
| `POST /ebay/listing/copy` | X-API-Key | eBay販売文章をAI生成（dry_run / templateフォールバック対応） |
| `POST /ebay/listing/publish` | X-API-Key | 本番出品（preview→inventory→offer→publish, dry_run対応） |
| `POST /ebay/listing/{sku}/withdraw` | X-API-Key | 手動でeBay listingを停止（offer withdraw） |
| `POST /ebay/orders/sync` | X-API-Key | eBay Fulfillment APIから注文を incremental 同期 |

## CJ → eBay 画像統合フロー

CJ Dropshipping API から取得した商品画像を eBay Inventory Item の `imageUrls` に組み込んで出品する。

### フロー

```
[Client]
  └→ POST /ebay/listing/create  {product_id, cj_product_id, dry_run}
       ↓
  [ebay_listing_cj_service]
       ├→ Product を DB から取得（title / description 元データ）
       ├→ cj_product_detail(cj_product_id)  ← CJ API
       ├→ extract_image_urls(detail)        ← productImageSet パース
       ├→ _filter_image_urls(urls, max=12)  ← 検証・重複除去・上限
       ├→ _build_inventory_payload(...)     ← imageUrls を含む
       ├→ _build_offer_payload(...)
       ├→ if dry_run → payload を返却して終了
       └→ else
            ├→ PUT  /sell/inventory/v1/inventory_item/{sku}
            ├→ POST /sell/inventory/v1/offer
            └→ POST /sell/inventory/v1/offer/{offerId}/publish
                  └→ 失敗時は best-effort で offer / inventory_item を削除
```

### 画像URLの検証ルール

- 空文字列を除外
- 重複URLを除外
- `http://` / `https://` 以外のスキームを除外（WARNING ログ）
- `EBAY_MAX_IMAGE_COUNT`（デフォルト 12）枚で打ち切り

### CJ画像取得失敗時の挙動（graceful fallback）

CJ API が 4xx / 5xx を返したり、ネットワークエラーが起きた場合：

- WARNING ログを出力
- `image_urls = []` で出品を継続（imageUrls 空のまま eBay に渡る）
- 出品自体は失敗させない

### dry_run 手順

```bash
KEY="your-secret-key"

# 1. CJ商品IDで画像URLを先に確認
curl -H "X-API-Key: $KEY" \
  "http://localhost:8000/cj/product/123456/images"

# 2. dry_run=true で payload を確認（eBay は呼ばない）
curl -X POST "http://localhost:8000/ebay/listing/create" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $KEY" \
  -d '{
    "product_id": 1,
    "cj_product_id": "123456",
    "dry_run": true
  }'
```

レスポンス（dry_run=true）:

```json
{
  "success": true,
  "image_count": 8,
  "inventory_item_id": "mzaiko-1",
  "offer_id": null,
  "published": false,
  "dry_run": true,
  "inventory_payload": {
    "availability": {"shipToLocationAvailability": {"quantity": 1}},
    "condition": "NEW",
    "product": {
      "title": "...",
      "description": "...",
      "imageUrls": ["https://...", "https://...", "..."]
    }
  },
  "offer_payload": {"sku": "mzaiko-1", "marketplaceId": "EBAY_US", ...}
}
```

### 本番出品（dry_run=false）

```bash
curl -X POST "http://localhost:8000/ebay/listing/create" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $KEY" \
  -d '{"product_id": 1, "cj_product_id": "123456", "dry_run": false}'
```

レスポンス:

```json
{
  "success": true,
  "image_count": 8,
  "inventory_item_id": "mzaiko-1",
  "offer_id": "1234567890",
  "published": true,
  "dry_run": false
}
```

### eBay imageUrls 仕様（参考）

- 配列の各要素は **公開アクセス可能なHTTPS URL** であること
- 1商品あたり **最大12枚**（`EBAY_MAX_IMAGE_COUNT` で調整可）
- 1枚目が **メイン画像** として扱われる
- eBay 側で画像をフェッチして自社CDNに配信するため、出品後にCJ側のURLが無効化されても表示は維持される

## CJ Dropshipping API キー

`developers.cjdropshipping.com` で発行した APIキーを `.env` の `CJ_API_KEY` に設定する。
形式: `CJUserNum@api@xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`

トークンは Redis に最大13日キャッシュされ、401 応答時は自動で再取得される。

## eBay listing 3段階構造

eBay/Payoneer 審査待ち中でも開発を進めるため、出品処理を **preview → validate → publish** の3段階に分離した。

| 段階 | エンドポイント | eBay API | auth token | sandbox | 用途 |
| --- | --- | --- | --- | --- | --- |
| preview | `POST /ebay/listing/preview` | 呼ばない | 不要 | 不要 | 最終 payload を完全確認 |
| publish (dry) | `POST /ebay/listing/create` (dry_run=true) | 呼ばない | 不要 | 不要 | CJ画像取得を含めたフロー確認 |
| publish (live) | `POST /ebay/listing/create` (dry_run=false) | 呼ぶ | 必要 | production | 本番出品 |

### preview の使い方（eBay未接続）

```bash
curl -X POST "http://localhost:8000/ebay/listing/preview" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $KEY" \
  -d '{"product_id": 1, "cj_product_id": "123456"}'
```

レスポンス:

```json
{
  "sku": "MZ-1-1746700000",
  "inventory_payload": {
    "availability": {"shipToLocationAvailability": {"quantity": 1}},
    "condition": "NEW",
    "product": {
      "title": "...",
      "description": "<h2>...</h2><ul>...</ul>",
      "imageUrls": ["https://...", "..."]
    }
  },
  "offer_payload": {"sku": "MZ-1-...", "marketplaceId": "EBAY_US", ...},
  "publish_payload": {
    "sku": "MZ-1-...",
    "endpoint": "POST /sell/inventory/v1/offer/{offerId}/publish",
    "body": {}
  },
  "images": ["https://...", "..."],
  "validation": {
    "title_length_ok": true,
    "description_ok": true,
    "image_count_ok": true,
    "price_ok": true,
    "overall_ok": true,
    "warnings": [],
    "details": {"title": {...}, "description": {...}, "images": {...}, "price": {...}}
  }
}
```

### service 分離

| サービス | 責務 |
| --- | --- |
| `ebay_description_service.py` | HTML description 生成（title / features / shipping / returns / contact） |
| `ebay_validator.py` | title / description / images / price の validation |
| `ebay_payload_builder.py` | inventory / offer / publish payload 組み立て、SKU 生成（`MZ-{id}-{ts}`） |
| `ebay_listing_cj_service.py` | CJ画像取得 + preview/create のオーケストレーター |
| `ebay_listing_service.py` | 既存の eBay Inventory/Offer API ラッパ（既存の list/revise） |

### preview の SKU 形式

`MZ-{product_id}-{unix_seconds}` （例: `MZ-1-1746700000`）

既存 `ebay_listing_service.list_product` は `mzaiko-{id}` 形式のままなので、preview で生成した SKU をそのまま `/ebay/listing/create` に流用したい場合は `ebay_payload_builder.build_sku()` を使うこと。

## eBay 販売文章 AI 生成

`POST /ebay/listing/copy` で、商品情報から eBay 向け販売文章一式を自動生成する。

### Provider 切替

`.env` の `LISTING_COPY_PROVIDER`:
- `template`（デフォルト）：依存ゼロ・固定テンプレで常に成功
- `openai`：将来用スケルトン（現在は `CopyProviderError` で template に fallback）
- `claude`：将来用スケルトン（現在は `CopyProviderError` で template に fallback）

実 LLM 連携は `app/services/ai/{openai_provider,claude_provider}.py` に実装を追加することで差し替え可能。

### dry_run

`dry_run=true` を渡すと、設定にかかわらず常に template provider を使う（AI を呼ばない）。

### 使用例

```bash
curl -X POST "http://localhost:8000/ebay/listing/copy" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $KEY" \
  -d '{"product_id": 1, "dry_run": true}'
```

レスポンス:

```json
{
  "optimized_title": "Cosmo Water Smart Plus ... | Brand New from Japan",
  "subtitle": "Tracked shipping from Japan, brand new, fast delivery",
  "html_description": "<h2>...</h2><h3>Features</h3><ul>...</ul>...",
  "bullet_points": ["Brand new, unused, in original packaging", "..."],
  "seo_keywords": ["cosmo", "water", "smart", "plus", ...],
  "condition_note": "Brand new, unused. Original packaging.",
  "shipping_note": "Ships from Japan via tracked international shipping. ...",
  "return_note": "Returns accepted within 30 days. ..."
}
```

### title 最適化ルール（template provider）

- 80文字以内（超過時は末尾切り）
- `【】「」『』〈〉《》` などの記号を空白へ置換
- 連続スペースを1つに圧縮
- 末尾に `| Brand New from Japan` を付与

### HTML description 仕様

eBay 互換のため `<script>` / `<style>` は使わない。`<h2>` / `<h3>` / `<ul>` / `<li>` / `<p>` / `<b>` のみ。

## eBay 本番出品フロー

`POST /ebay/listing/publish` で、preview で確認した payload をそのまま eBay Inventory API + Offer API に流して本番出品する。

### フロー

```
[Client]
 └→ POST /ebay/listing/publish  {product_id, cj_product_id, marketplace_id, dry_run}
      ↓
 [ebay_publish_service.publish_listing]
   1. _fetch_cj_image_urls(cj_product_id)        ← CJ画像取得（失敗時 graceful fallback）
   2. build_preview(product, image_urls)         ← payload組み立て（preview と共通）
   3. if dry_run → payload返却して終了
   4. PUT  /sell/inventory/v1/inventory_item/{sku}
        → Product.ebay_sku / ebay_listing_status="INVENTORY_CREATED"
   5. POST /sell/inventory/v1/offer
        → Product.ebay_offer_id / ebay_listing_status="OFFER_CREATED"
        → 失敗時: inventory item は残す（再試行可能）/ status="OFFER_FAILED"
   6. POST /sell/inventory/v1/offer/{offerId}/publish
        → Product.ebay_listing_id / ebay_published_at / status="PUBLISHED"
        → 失敗時: offer_id は保存（再試行可能）/ status="PUBLISH_FAILED"
   7. listing_url = "https://www.ebay.com/itm/{listing_id}" を組み立てて返却
```

### service 分離

| サービス | 責務 |
| --- | --- |
| `ebay_publish_service.py` | publish フロー全体のオーケストレーター + DB更新 + 401再試行 + listing_url生成 |
| `ebay_inventory_service.py` | inventory item の upsert / get / safe_delete（idempotent） |
| `ebay_offer_service.py` | offer の create / update / publish / safe_withdraw / safe_delete |

HTTP レイヤー（tenacity retry / timeout / Bearer token）は既存 `ebay_listing_service._ebay_request` を再利用。

### Product に追加した列

| 列 | 型 | 用途 |
| --- | --- | --- |
| `ebay_sku` | VARCHAR(64) | preview で生成した `MZ-{id}-{ts}` |
| `ebay_offer_id` | VARCHAR(64) | offer create 後に書き込む |
| `ebay_listing_id` | VARCHAR(64) | publish 成功後に書き込む |
| `ebay_listing_status` | VARCHAR(32) | INVENTORY_CREATED / OFFER_CREATED / PUBLISHED / *_FAILED |
| `ebay_published_at` | TIMESTAMP | publish 成功時刻 |

DB 列追加なので、初回起動時は `docker compose down -v && docker compose up --build` 必須。

### dry_run 手順

```bash
KEY="your-secret-key"

# eBay APIを呼ばずに publish payload を確認
curl -X POST "http://localhost:8000/ebay/listing/publish" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $KEY" \
  -d '{
    "product_id": 1,
    "cj_product_id": "123456",
    "marketplace_id": "EBAY_US",
    "dry_run": true
  }'
```

レスポンス（dry_run=true）：

```json
{
  "success": true,
  "sku": "MZ-1-1746700000",
  "offer_id": null,
  "listing_id": null,
  "listing_url": null,
  "published": false,
  "dry_run": true,
  "inventory_payload": {...},
  "offer_payload": {...},
  "publish_payload": {...}
}
```

### 本番出品（dry_run=false）

```bash
curl -X POST "http://localhost:8000/ebay/listing/publish" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $KEY" \
  -d '{"product_id": 1, "cj_product_id": "123456", "dry_run": false}'
```

レスポンス：

```json
{
  "success": true,
  "sku": "MZ-1-1746700000",
  "offer_id": "1234567890",
  "listing_id": "v1|1234567890|0",
  "listing_url": "https://www.ebay.com/itm/v1|1234567890|0",
  "published": true,
  "dry_run": false
}
```

### sandbox / production 切替

```env
# .env
EBAY_ENVIRONMENT=production   # または sandbox
```

`config.py` の `ebay_api_base` プロパティが `EBAY_ENVIRONMENT` を見て base URL を切り替える：
- `production` → `https://api.ebay.com`
- `sandbox` → `https://api.sandbox.ebay.com`

sandbox を使うには別途 sandbox の `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` / `EBAY_REFRESH_TOKEN` が必要（dev.ebay.com の Sandbox Keyset）。

### 401 token 失効時の挙動

`ebay_publish_service._call_with_token_refresh()` が各ステップ（inventory/offer/publish）の 401 を検知し：
1. WARNING ログ出力
2. `invalidate_token_cache()` で token を破棄
3. 新しい token を取得
4. 同じステップを **1 回だけ** retry

2 回目も 401 の場合は raise（refresh token 自体が失効している可能性）。

### refresh token revoke 時の対応

eBay の User OAuth refresh token が revoke された（dev portal で手動取消、180日経過、ユーザがアプリ連携を解除など）場合、`get_access_token()` が `EbayAuthError` を投げて `publish_listing` も失敗する。対応：

1. dev.ebay.com → My Account → Application Keys から再認可
2. 新しい refresh token を `.env` の `EBAY_REFRESH_TOKEN` に書き込み
3. コンテナ再起動 `docker compose restart api`

### graceful rollback の方針

| 失敗ステップ | 対応 | DB status |
| --- | --- | --- |
| inventory create | inventory item 作成失敗→何も残らない | INVENTORY_FAILED |
| offer create | inventory item は **残す**（再試行で同じ sku を upsert） | OFFER_FAILED |
| publish | offer_id は **残す**（再試行で publish のみ叩ける） | PUBLISH_FAILED |

中途半端な状態をクリーンにしたい場合は、`ebay_publish_service.rollback_listing(sku=..., offer_id=...)` を直接呼び、offer withdraw + inventory delete を best-effort で実行できる。

### Common eBay errors

| HTTP | reason | 対処 |
| --- | --- | --- |
| 400 | inputErrors の `product.title` 80字超 | preview の validation で事前に弾けるはず |
| 400 | `imageUrls` 非HTTPS / 12枚超 | preview の `_filter_image_urls` で弾く |
| 400 | `categoryId` が leaf でない | EBAY_LISTING_CATEGORY_ID を leaf category に変更 |
| 401 | invalid_token | `_call_with_token_refresh` が 1 回 retry。2 回目も 401 なら refresh_token 失効 |
| 403 | Insufficient permissions | EBAY_OAUTH_USER_SCOPE に `sell.inventory` が含まれているか確認 |
| 409 | Conflict on `inventory_item` | idempotent upsert なので通常起きないが、起きたら sku 衝突を疑う |
| 429 | Rate limit | tenacity が指数バックオフで自動 retry（`max_retries` まで） |
| 5xx | eBay側障害 | tenacity が指数バックオフで自動 retry |
| 25001 | Listing has business policies error | ebay_fulfillment / payment / return policy id を再確認 |

## eBay listing 自動運営（auto-stop / revise）

publish 済みの商品を **既存 monitoring scheduler** が定期巡回し、状態に応じて
revise（quantity / price）または withdraw する。「分析」から「自動運営」への進化。

### 判定優先順位

scheduler の `_recheck_one()` が Amazon / eBay を再スクレイプ → Product を更新した直後に、
`ebay_listing_status == 'PUBLISHED'` の商品だけ `sync_ebay_listing()` が走る。
判定は以下の優先順位（先にマッチした action のみ実行）：

| 優先 | 条件 | action | 状態 |
| --- | --- | --- | --- |
| C | `risk_level == 'high'` | **withdraw** | risk再検知（VeRO等）→即停止 |
| A | `is_profitable == False` | **withdraw** | 赤字化 → 停止 |
| D | `in_stock == False` | quantity=0 | Amazon/CJ 在庫切れ（停止せず再入荷待ち） |
| B | `profit_rate < MIN_PROFIT_RATE` | **revise_price** または **withdraw** | 利益率低下 |
| - | 上記いずれも該当しない | noop | そのまま継続 |

B の分岐：
- `AUTO_REVISE_PRICE=true`（既定）→ `revise_price`（offer の pricingSummary 更新 + quantity 同期）
- `AUTO_REVISE_PRICE=false` → `withdraw`

### service 分離

| サービス | 責務 |
| --- | --- |
| `ebay_revise_service.py` | 判定 + 実行 + DB更新（sync_ebay_listing / revise_quantity / revise_price / withdraw_by_sku） |
| `ebay_inventory_service.py` | inventory item の get / upsert / safe_delete（既存） |
| `ebay_offer_service.py` | offer の create / update / publish / safe_withdraw / safe_delete（既存） |

`revise_quantity()` は eBay の PUT /inventory_item/{sku} が createOrReplace なので、
**事前に GET → quantity だけ書き換えて → PUT** することで他フィールド（imageUrls 等）を保つ。

### env

```env
AUTO_STOP_ENABLED=true     # false にすれば判定だけ走って action しない（ドライ運用）
AUTO_REVISE_PRICE=true     # false なら利益率低下時は revise せず withdraw
MIN_PROFIT_RATE=10.0       # %, これ未満で B 条件発火
```

### Product に追加した sync 用列

| 列 | 型 | 用途 |
| --- | --- | --- |
| `ebay_last_sync_at` | TIMESTAMP | 最後に sync_ebay_listing が走った時刻 |
| `ebay_sync_status` | VARCHAR(32) | withdraw / quantity_zero / revise_price / noop / sync_failed |
| `ebay_sync_error` | TEXT | sync_failed 時のエラーメッセージ（先頭500字） |

DB 列追加なので、初回起動時は `docker compose down -v && docker compose up --build` 必須。

### fail-safe behavior（scheduler graceful継続）

scheduler は1商品の sync 失敗でジョブ全体を止めない：

1. `sync_ebay_listing()` 内で eBay API 失敗を try/except でキャッチ
2. DB に `ebay_sync_status='sync_failed'` + `ebay_sync_error=...` を記録（success=False で return）
3. scheduler 側でも `try: await sync_ebay_listing(...)` で囲み、想定外クラッシュもログのみ
4. 次回 monitoring 周回で再試行される

**rollback safety**: B の `revise_price` 中、price 更新が失敗しても **quantity 同期は続行**する。
逆も同じ（quantity 失敗 → price は試みる）。両方失敗した場合のみ sync_failed 扱い。

### 401 token 失効時

publish 同様、各 API コール直前に `get_access_token()` を呼ぶ。
ただし publish_service と異なり、revise_service 内では invalidate→retry は **していない**
（scheduler が次回周回で再試行するので二重に retry する必要がない）。

### 手動 withdraw

```bash
curl -X POST "http://localhost:8000/ebay/listing/MZ-1-1746700000/withdraw" \
  -H "X-API-Key: $KEY"
```

レスポンス：

```json
{
  "success": true,
  "action": "withdraw",
  "sku": "MZ-1-1746700000",
  "reason": "manual"
}
```

### sync 結果のクエリ

`ebay_sync_status` で集計すれば自動運営の挙動を確認できる：

```sql
SELECT ebay_sync_status, COUNT(*) FROM products
 WHERE ebay_listing_status='PUBLISHED'
 GROUP BY ebay_sync_status;
```

## eBay Order Sync System

`POST /ebay/orders/sync` および 15 分間隔の scheduler ジョブが eBay Fulfillment API から注文を取得して DB に同期する。「publish 自動化」→「**operations 自動化**」への進化点。

### フロー

```
[scheduler 15min] OR [手動: POST /ebay/orders/sync]
       ↓
 [ebay_order_service.sync_orders]
   1. last_modified_date を Redis から読み込み（key: ebay_orders:last_modified_at）
   2. eBay GET /sell/fulfillment/v1/order?filter=lastmodifieddate:[<since>..]&limit=50
   3. ページング（最大 20 ページ＝1000 件 / 1 回）
   4. 各 order を per-order transaction で upsert
        - 既存（ebay_order_id 一致）+ last_modified_date が後退してれば skipped
        - 既存 + last_modified_date が新しければ updated
        - 新規なら imported
   5. OrderItem は order_id で一旦 DELETE → lineItems から再 INSERT
   6. 最大 last_modified_date を Redis に保存 → 次回 sync の since にする
```

### incremental sync の仕組み

- 状態保存先: **Redis**（key: `ebay_orders:last_modified_at`、TTL 1 年）
- Redis 失効/未設定時は **全件 sync**（safe fallback）
- eBay 側の filter syntax: `lastmodifieddate:[<ISO-Z>..]`（半開区間、UTC）

### dedupe

- DB レベル: `Order.ebay_order_id` UNIQUE 制約
- アプリレベル: SELECT-based upsert + `last_modified_date` 比較で更新スキップ

### atomic / graceful

- 1 注文ごとに `await session.commit()`（部分失敗で他注文を巻き込まない）
- 1 注文の upsert 失敗 → WARNING ログ + `session.rollback()` + 次の注文へ
- 全体の 401 / ネットワーク失敗 → 早期 break + 次回 schedule を待つ
- scheduler は `run_ebay_order_sync()` ラッパで例外を握り潰す

### Order に追加した列

| 列 | 型 | 用途 |
| --- | --- | --- |
| `ebay_listing_id` | VARCHAR(64) | eBay 側 legacyItemId（既存 `listing_id` (FK) とは別） |
| `buyer_username` | VARCHAR(64) | 購入者 |
| `payment_status` | VARCHAR(20) | PAID / PENDING / FAILED / REFUNDED |
| `fulfillment_status` | VARCHAR(20) | NOT_STARTED / IN_PROGRESS / FULFILLED |
| `tracking_number` | VARCHAR(64) | 出荷時の追跡番号 |
| `last_modified_date` | TIMESTAMP | eBay 側の最終更新時刻（incremental key） |

### OrderItem（新規テーブル）

`orders` の正規化（複数 lineItem 対応）。`order_id` FK CASCADE。
sku / title / quantity / price / currency。

### env

```env
EBAY_ORDER_SYNC_ENABLED=true
EBAY_ORDER_SYNC_INTERVAL_MINUTES=15
```

### eBay OAuth scope

Fulfillment API には以下の scope が必要：

```
https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly
```

既に `EBAY_OAUTH_USER_SCOPE` のデフォルトに含まれている。

### dashboard 拡張

`GET /dashboard/summary` のレスポンスに 3 項目追加：

| 項目 | 算出 |
| --- | --- |
| `today_orders` | 今日 (UTC) `Order.created_at >= 00:00` の件数 |
| `today_sales` | 今日の `sold_price` 合計（USD等の生値） |
| `pending_shipments` | `fulfillment_status` が SHIPPED/DELIVERED/CANCELLED/FULFILLED 以外（NULL含む） |

### 手動 sync

```bash
curl -X POST "http://localhost:8000/ebay/orders/sync" \
  -H "X-API-Key: $KEY"
```

レスポンス：

```json
{"imported": 5, "updated": 2, "skipped": 10}
```

### 将来の CJ 自動発注フック

`Order` の `payment_status='PAID'` かつ `fulfillment_status='NOT_STARTED'` を SELECT して、`OrderItem.sku` から元商品 → CJ Product を引き、CJ API で発注を作る、という流れに接続できる。`raw_payload` に shipping address フル情報が残っているので、配送先抽出はそこから可能。

## Production Deploy（VPS）

### 構成

```
[ Internet ]
     │ 80/443
     ▼
  ┌─────────┐
  │  nginx  │ ←─ Let's Encrypt（certbot コンテナで自動更新）
  └────┬────┘
       │ /api/* → api:8000 / それ以外 → frontend:80
   ┌───┴───┬─────────┬─────────┐
   ▼       ▼         ▼         ▼
  api  frontend     db       redis
  (FastAPI) (Vite)  (Postgres) (appendonly)
                       │           │
                  postgres_data  redis_data （named volume で永続化）
```

### VPS 要件

- Ubuntu 22.04 / 24.04 LTS（または同等）
- Docker Engine + Docker Compose plugin インストール済み
- ドメインの A レコードを VPS の IPv4 に向ける
- 80 / 443 ポートをファイアウォールで開放

### 初回デプロイ

```bash
# 1. リポジトリ clone
git clone https://github.com/<owner>/mzaiko-mvp.git
cd mzaiko-mvp

# 2. .env.production を作成（例ファイルからコピーして値を埋める）
cp .env.production.example .env.production
nano .env.production
#   - DOMAIN=mydomain.com
#   - LETSENCRYPT_EMAIL=admin@mydomain.com
#   - POSTGRES_PASSWORD=...（強いランダム値）
#   - API_KEY=...（32文字以上）
#   - CJ_API_KEY=...
#   - EBAY_CLIENT_ID/SECRET/REFRESH_TOKEN=...
#   ほか env

# 3. Let's Encrypt 初回証明書取得（nginx と certbot が自動で立ち上がる）
bash scripts/init-letsencrypt.sh

# 4. 残りのサービスも全て起動
docker compose -f docker-compose.prod.yml --env-file .env.production up -d

# 5. ヘルスチェック
docker compose -f docker-compose.prod.yml ps
curl -I https://${DOMAIN}/
```

### 更新デプロイ（コード変更時）

```bash
git pull
docker compose -f docker-compose.prod.yml --env-file .env.production up -d --build
```

`restart: unless-stopped` が全サービスに付いているので、サーバ再起動時も自動復帰する。

### Backup

#### PostgreSQL

```bash
# 取得
docker exec mzaiko-db pg_dump -U $POSTGRES_USER $POSTGRES_DB | gzip > backups/db_$(date +%F).sql.gz

# 復元
gunzip -c backups/db_2026-05-08.sql.gz | docker exec -i mzaiko-db psql -U $POSTGRES_USER -d $POSTGRES_DB
```

cron 例（毎日 03:15 にバックアップ + 30日以上経過したファイルを削除）：

```
15 3 * * * cd /opt/mzaiko-mvp && docker exec mzaiko-db pg_dump -U $POSTGRES_USER $POSTGRES_DB | gzip > backups/db_$(date +\%F).sql.gz && find backups -name 'db_*.sql.gz' -mtime +30 -delete
```

#### Redis（appendonly）

`redis_data` named volume に `appendonly.aof` と RDB スナップショットが書かれる。
ホスト側にコピーするには：

```bash
docker run --rm -v mzaiko-mvp_redis_data:/data -v "$PWD/backups":/backup \
  alpine tar czf /backup/redis_$(date +%F).tar.gz -C /data .
```

### ログ確認

```bash
docker compose -f docker-compose.prod.yml logs -f api
docker compose -f docker-compose.prod.yml logs -f nginx
docker exec mzaiko-nginx tail -f /var/log/nginx/access.log
```

### Let's Encrypt 自動更新

`certbot` コンテナが 12 時間ごとに `certbot renew` を実行する。
更新後の証明書反映は次の nginx reload まで待つか、即時反映したい場合：

```bash
docker compose -f docker-compose.prod.yml exec nginx nginx -s reload
```

### secrets / volumes の取り扱い

`.gitignore` で除外済みのものは絶対にコミットしない：

- `.env`、`.env.production`
- `postgres_data/`、`redis_data/`、`certbot/`
- `*.pem`、`*.key`、`*.crt`
- `backups/`

### GitHub Actions（optional）

最小例（main ブランチに push されたら VPS で git pull + compose up）：

```yaml
# .github/workflows/deploy.yml
name: deploy
on:
  push:
    branches: [main]
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: appleboy/ssh-action@v1.0.3
        with:
          host: ${{ secrets.VPS_HOST }}
          username: ${{ secrets.VPS_USER }}
          key: ${{ secrets.VPS_SSH_KEY }}
          script: |
            cd /opt/mzaiko-mvp
            git pull
            docker compose -f docker-compose.prod.yml --env-file .env.production up -d --build
```

GitHub Secrets に `VPS_HOST` / `VPS_USER` / `VPS_SSH_KEY` を登録しておく。

## 起動方法

### DB スキーマ変更（FulfillmentTask に3列追加）→ `-v` 必須

```bash
docker compose down -v
docker compose up --build
```

`.env` に追加:
```
RISK_BLOCKED_COUNTRIES=
RISK_HIGH_COUNTRIES=
RISK_MEDIUM_COUNTRIES=
RISK_BUFFER_LOW_PERCENT=3.0
RISK_BUFFER_MEDIUM_PERCENT=7.0
RISK_BUFFER_HIGH_PERCENT=15.0
```

CSV 空ならコード内デフォルト（ユーザ指定値）を使用。

## Country Risk 仕様

### Risk Level

| Level | Score | refund_buffer 加算 % | 動作 |
| --- | --- | --- | --- |
| `low` | 10 | +3% | 通常フロー（pending） |
| `medium` | 40 | +7% | 通常フロー（pending） |
| `high` | 75 | +15% | **task_status=reviewing** + manual review note |
| `blocked` | 100 | — | **task_status=failed**（auto） |

### デフォルト国マッピング

| Level | デフォルト国 |
| --- | --- |
| `blocked` | RU / IR / KP |
| `high` | BR / MX / IT / ZA |
| `medium` | AR / CL / TR |
| `low`(default) | 上記未収載すべて |

env で完全上書き可能（CSV 空ならデフォルト）:
```
RISK_BLOCKED_COUNTRIES=RU,IR,KP,SY
RISK_HIGH_COUNTRIES=BR,MX,IT,ZA,GR
RISK_MEDIUM_COUNTRIES=AR,CL,TR,PE
```

## 利益計算式（拡張版）

```
expected_profit       = ebay_sold − amazon_price − fee − shipping
                        − fx_buffer (2%)
                        − refund_buffer (3%)

risk_buffer_jpy       = ebay_sold × buffer_adjustment[risk_level] / 100
                        # low: 3% / medium: 7% / high: 15%

risk_adjusted_profit  = expected_profit − risk_buffer_jpy
```

両方が DB に保存され、`profit_breakdown` で全項目が表示されます。

### 利益ガード（gate）

| 条件 | 動作 |
| --- | --- |
| `country = blocked` | `task_status=failed`（最優先、profit ガード前に判定） |
| `expected_profit < MIN_PROFIT_YEN` | `task_status=failed` |
| `country = high` | `task_status=reviewing` + manual review note |
| `risk_adjusted_profit < MIN_PROFIT_YEN` | WARNING のみ（status 維持） |

## DB 追加列（FulfillmentTask）

| カラム | 型 | 説明 |
| --- | --- | --- |
| `country_risk_level` | VARCHAR(20) | low / medium / high / blocked（INDEX） |
| `country_risk_score` | INTEGER | 0-100 の数値スコア |
| `risk_adjusted_profit` | INTEGER | risk_buffer 引き後の利益（JPY） |

## 動作確認

### 1. リスク mapping 確認

```bash
KEY="your-secret-key"

curl -H "X-API-Key: $KEY" http://localhost:8000/risk/countries
```

レスポンス:
```json
{
  "blocked": ["IR", "KP", "RU"],
  "high": ["BR", "IT", "MX", "ZA"],
  "medium": ["AR", "CL", "TR"],
  "default_low": "all other countries default to low",
  "score_table": {"low": 10, "medium": 40, "high": 75, "blocked": 100},
  "buffer_adjustment_percent": {"low": 3.0, "medium": 7.0, "high": 15.0, "blocked": 0.0}
}
```

### 2. リスクレベル別 task 一覧

```bash
# blocked のみ（auto failed）
curl -H "X-API-Key: $KEY" "http://localhost:8000/risk/tasks?risk_level=blocked"

# high のみ（manual review 待ち）
curl -H "X-API-Key: $KEY" "http://localhost:8000/risk/tasks?risk_level=high"

# 全件
curl -H "X-API-Key: $KEY" "http://localhost:8000/risk/tasks"
```

### 3. task 詳細で profit_breakdown を確認

```bash
curl -H "X-API-Key: $KEY" http://localhost:8000/fulfillment/tasks/1
```

```json
{
  "id": 1,
  "buyer_country": "BR",
  "country_risk_level": "high",
  "country_risk_score": 75,
  "expected_profit": 1370,
  "risk_adjusted_profit": -2405,
  "task_status": "reviewing",
  "notes": "[2026-05-07T...] [manual-review] high-risk country=BR (manual review required)\n[2026-05-07T...] [risk-warning] risk_adjusted_profit=-2405 JPY < MIN_PROFIT_YEN=500",
  "profit_breakdown": {
    "ebay_sold_jpy": 18500,
    "amazon_price_jpy": 12000,
    "estimated_fee_jpy": 2405,
    "estimated_shipping_jpy": 1800,
    "fx_buffer_jpy": 370,
    "refund_buffer_jpy": 555,
    "risk_buffer_jpy": 2775,
    "expected_profit_jpy": 1370,
    "risk_adjusted_profit_jpy": -1405
  }
}
```

### 4. blocked 国の auto-fail を確認

`buyer_country=RU` の sold order が来ると:
```
[INFO]  sold detected order_id=...
[WARNING] blocked country task auto-failed order_id=... country=RU
[INFO]  task created id=N order_id=... status=failed country=RU risk_level=blocked ...
```

DB:
```sql
SELECT id, buyer_country, country_risk_level, task_status FROM fulfillment_tasks;
-- N | RU | blocked | failed
```

### 5. high 国の manual review 待ち

`buyer_country=BR` の場合:
```
[WARNING] high risk shipment country=BR order_id=... status=reviewing
[INFO]    task created id=N status=reviewing
```

→ `GET /risk/tasks?risk_level=high` で一覧化、人間が確認後 `/approve`。

### 6. shipping recalculate で risk_adjusted_profit も更新

```bash
curl -X POST -H "X-API-Key: $KEY" http://localhost:8000/shipping/recalculate/1
```
→ shipping、expected_profit、risk_adjusted_profit すべて再計算。

### 7. DB 確認

```bash
docker exec -it mzaiko-db psql -U mzaiko -d mzaiko_db -c \
  "SELECT id, sku, buyer_country, country_risk_level, country_risk_score, expected_profit, risk_adjusted_profit, task_status FROM fulfillment_tasks ORDER BY id DESC;"
```

## ログ例

### blocked
```
[WARNING] blocked country task auto-failed order_id=12-345-67 country=RU
[INFO]    task created id=1 order_id=12-345-67 sku=mzaiko-42 status=failed country=RU risk_level=blocked risk_score=100 amazon=12000 ebay_sold=18500 shipping=1800 fee=2405 fx_buf=370 refund_buf=555 risk_buf=0 expected=1370 risk_adjusted=1370
```

### high
```
[WARNING] high risk shipment country=BR order_id=... status=reviewing
[INFO]    task created id=2 ... status=reviewing country=BR risk_level=high risk_score=75 ... risk_buf=2775 expected=1370 risk_adjusted=-1405
[WARNING] adjusted profit below threshold order_id=... country=BR expected=1370 adjusted=-1405 threshold=500
```

### medium
```
[INFO]    task created id=3 ... status=pending country=AR risk_level=medium risk_score=40 ... risk_buf=1295 expected=1370 risk_adjusted=75
```

### low (US)
```
[INFO]    task created id=4 ... status=pending country=US risk_level=low risk_score=10 ... risk_buf=555 expected=1370 risk_adjusted=815
```

## 設計ポイント実装対応表

| 必須要件 | 実装 |
| --- | --- |
| FulfillmentTask に3列追加 | country_risk_level / country_risk_score / risk_adjusted_profit |
| 4 risk levels | `CountryRiskLevel` enum + `LEVEL_TO_SCORE` |
| country mapping (blocked/high/medium) | `country_risk_service.py` のデフォルト + env CSV 上書き |
| risk_buffer 加算 (low+3% / medium+7% / high+15%) | `risk_buffer_*_percent` env、`calc_risk_buffer_jpy()` |
| `risk_adjusted_profit = expected_profit − risk_buffer` | `create_task_from_order` で計算してDB保存 |
| worker integration（create_task_from_order） | sold_sync の hook 経由で自動適用 |
| blocked → task_status='failed' | risk gate を最優先で判定 |
| high → manual review required | task_status='reviewing' に自動遷移 + notes |
| `GET /risk/countries` | mapping 一覧返却 |
| `GET /risk/tasks` | risk_level クエリパラメータ対応 |
| logging WARNING | blocked country / high risk shipment / adjusted profit below threshold |
| future delivery analytics / dynamic scoring / fraud detection | `compute_country_risk_score()` を1か所差し替えで対応可能 |

## 拡張への布石

### 1. 配送成功率からの動的スコアリング

`country_risk_service.compute_country_risk_score()` を:
```python
async def compute_country_risk_score(country):
    # delivery_success_rate を集計
    rate = await fetch_delivery_success_rate(country)
    # rate が低いほどスコアが高い
    if rate < 0.7: return 100  # blocked-tier
    if rate < 0.85: return 75  # high
    if rate < 0.95: return 40  # medium
    return 10  # low
```

注意: 動的化すると非同期になるので、create_task_from_order の呼び出しも調整が必要。

### 2. fraud detection 連携

buyer reputation / payment method / order amount から fraud score を計算:
```python
fraud_score = await detect_fraud(order, buyer_address)
if fraud_score > FRAUD_THRESHOLD:
    risk_level = "high"
```

### 3. 配送成功率トラッキング

新テーブル `country_delivery_stats` を作り、completed / failed の比率を集計:
```sql
CREATE TABLE country_delivery_stats (
    country VARCHAR(8) PRIMARY KEY,
    total_orders INT,
    completed_orders INT,
    failed_orders INT,
    avg_delivery_days FLOAT,
    updated_at TIMESTAMP
);
```

`task_status` 遷移時にこのテーブルを更新 → `compute_country_risk_score` がここから読む。

## 環境変数（country risk 関連）

| 変数 | デフォルト | 説明 |
| --- | --- | --- |
| `RISK_BLOCKED_COUNTRIES` | (空・コード内 RU/IR/KP) | CSV で上書き |
| `RISK_HIGH_COUNTRIES` | (空・コード内 BR/MX/IT/ZA) | CSV で上書き |
| `RISK_MEDIUM_COUNTRIES` | (空・コード内 AR/CL/TR) | CSV で上書き |
| `RISK_BUFFER_LOW_PERCENT` | `3.0` | low 国の refund_buffer 加算 % |
| `RISK_BUFFER_MEDIUM_PERCENT` | `7.0` | medium 国の同 % |
| `RISK_BUFFER_HIGH_PERCENT` | `15.0` | high 国の同 % |

## エラーハンドリング

| 場面 | 挙動 |
| --- | --- |
| 国コード未収載 | `low` 扱い（spec: default low） |
| 国コード None | `low` 扱い |
| blocked + 利益不足 | blocked が優先（最初に判定）→ failed |
| high + 利益不足 | reviewing + warning（profit ガードは expected_profit に対して再判定） |
| 既存 task の country が変更 | `recalculate_shipping_for_task` で risk も再計算 |

## 停止方法

```bash
docker compose down
docker compose down -v
```

## 未実装（次の段階）

- delivery_success_rate からの動的スコアリング
- fraud detection 連携
- buyer reputation 統合
- country 別 delivery 統計テーブル
- 配送中・到着までの追跡時間記録
