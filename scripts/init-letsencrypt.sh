#!/usr/bin/env bash
# Let's Encrypt 初回証明書取得スクリプト。
# 使い方: bash scripts/init-letsencrypt.sh
# 前提: .env.production に DOMAIN と LETSENCRYPT_EMAIL が設定済み。
# nginx は HTTP の /.well-known/acme-challenge/ だけ受ける状態で先に起動されている必要があるが、
# 本スクリプトは Compose 起動の前提で動く（nginx + certbot を up する）。

set -euo pipefail

if [ ! -f .env.production ]; then
  echo "ERROR: .env.production が見つかりません。.env.production.example をコピーして編集してください。" >&2
  exit 1
fi

# shellcheck disable=SC1091
set -a; . ./.env.production; set +a

: "${DOMAIN:?DOMAIN が未設定}"
: "${LETSENCRYPT_EMAIL:?LETSENCRYPT_EMAIL が未設定}"

echo "→ Domain: $DOMAIN"
echo "→ Email:  $LETSENCRYPT_EMAIL"

COMPOSE="docker compose -f docker-compose.prod.yml --env-file .env.production"

# 1. ダミー証明書で nginx を起動できるようにする（chicken-and-egg 回避）
echo "→ ダミー証明書を生成..."
DUMMY_PATH="/etc/letsencrypt/live/$DOMAIN"
$COMPOSE run --rm --entrypoint "/bin/sh -c '\
  mkdir -p $DUMMY_PATH && \
  openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
    -keyout $DUMMY_PATH/privkey.pem \
    -out    $DUMMY_PATH/fullchain.pem \
    -subj /CN=localhost'" certbot

# 2. nginx を起動（HTTP/HTTPS 両方リッスン、ダミー証明書で起動可能に）
echo "→ nginx を起動..."
$COMPOSE up -d nginx

# 3. ダミー証明書を削除して本物を取得
echo "→ ダミー証明書を削除して本物を取得..."
$COMPOSE run --rm --entrypoint "/bin/sh -c '\
  rm -rf /etc/letsencrypt/live/$DOMAIN && \
  rm -rf /etc/letsencrypt/archive/$DOMAIN && \
  rm -rf /etc/letsencrypt/renewal/$DOMAIN.conf'" certbot

$COMPOSE run --rm --entrypoint "certbot certonly --webroot \
  -w /var/www/certbot \
  --email $LETSENCRYPT_EMAIL \
  -d $DOMAIN \
  --rsa-key-size 2048 \
  --agree-tos \
  --no-eff-email \
  --force-renewal" certbot

# 4. nginx を reload して新しい証明書を反映
echo "→ nginx reload..."
$COMPOSE exec nginx nginx -s reload

echo "✓ 完了。https://$DOMAIN/ にアクセスして確認してください。"
