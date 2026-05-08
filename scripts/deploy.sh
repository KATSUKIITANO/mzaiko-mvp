#!/usr/bin/env bash
# =============================================================================
#  mzaiko-mvp 完全自動セットアップ for Ubuntu VPS
# -----------------------------------------------------------------------------
#  対象: Ubuntu 22.04 / 24.04 LTS（または互換）
#  内容:
#    1. システム更新 + 必要パッケージインストール
#    2. Docker Engine + Compose plugin インストール（未導入時のみ）
#    3. ufw でファイアウォール設定（22/80/443 のみ開放）
#    4. リポジトリ clone（既にあれば pull）
#    5. .env.production の存在確認（無ければテンプレからコピー＋編集を促す）
#    6. Let's Encrypt 初回証明書取得（既に証明書ありならスキップ）
#    7. docker compose up -d で全サービス起動
#    8. ヘルスチェック
#
#  使い方:
#    # VPS 上で root または sudo 権限ユーザーで:
#    curl -fsSL https://raw.githubusercontent.com/<owner>/mzaiko-mvp/main/scripts/deploy.sh -o deploy.sh
#    bash deploy.sh https://github.com/<owner>/mzaiko-mvp.git
#
#  または既に clone 済みなら:
#    cd /opt/mzaiko-mvp && bash scripts/deploy.sh
#
#  冪等: 何度実行しても壊れない（インストール済みはスキップ、既存設定は保持）
# =============================================================================

set -euo pipefail

# ── 引数 ────────────────────────────
REPO_URL="${1:-}"
INSTALL_DIR="${INSTALL_DIR:-/opt/mzaiko-mvp}"
COMPOSE_FILE="docker-compose.prod.yml"
ENV_FILE=".env.production"
ENV_EXAMPLE=".env.production.example"

# ── 色付きログ ───────────────────────
COLOR_OFF=$'\033[0m'
COLOR_RED=$'\033[31m'
COLOR_GREEN=$'\033[32m'
COLOR_YELLOW=$'\033[33m'
COLOR_BLUE=$'\033[34m'

log()    { echo -e "${COLOR_BLUE}[INFO]${COLOR_OFF}    $*"; }
warn()   { echo -e "${COLOR_YELLOW}[WARN]${COLOR_OFF}    $*"; }
error()  { echo -e "${COLOR_RED}[ERROR]${COLOR_OFF}   $*" >&2; }
ok()     { echo -e "${COLOR_GREEN}[OK]${COLOR_OFF}      $*"; }
step()   { echo; echo -e "${COLOR_BLUE}===${COLOR_OFF} ${COLOR_GREEN}$*${COLOR_OFF} ${COLOR_BLUE}===${COLOR_OFF}"; }

# ── 失敗時メッセージ ─────────────────
trap 'error "deploy.sh が失敗しました（line $LINENO）。上のログを確認してください。"' ERR

# ── 0. 権限チェック ──────────────────
if [[ $EUID -ne 0 ]] && ! sudo -n true 2>/dev/null; then
  error "このスクリプトは root 権限または sudo 権限が必要です"
  exit 1
fi
SUDO=""; [[ $EUID -ne 0 ]] && SUDO="sudo"

# ── 1. システム更新 ──────────────────
step "1/8 システム更新 + 基本パッケージ"
$SUDO apt-get update -y
$SUDO apt-get install -y \
  ca-certificates curl git ufw openssl gnupg lsb-release
ok "基本パッケージ OK"

# ── 2. Docker インストール ──────────
step "2/8 Docker Engine + Compose plugin"
if ! command -v docker >/dev/null 2>&1; then
  log "Docker が未インストール → 公式リポジトリから導入"
  $SUDO install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | $SUDO gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes
  $SUDO chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null
  $SUDO apt-get update -y
  $SUDO apt-get install -y \
    docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
  $SUDO systemctl enable --now docker
  if [[ $EUID -ne 0 ]] && ! groups | grep -q docker; then
    $SUDO usermod -aG docker "$(whoami)"
    warn "docker グループに追加しました。次回ログインから sudo なしで docker が使えます"
  fi
fi
docker --version
docker compose version
ok "Docker OK"

# ── 3. ファイアウォール ──────────────
step "3/8 ufw（22/80/443 のみ開放）"
$SUDO ufw allow OpenSSH || true
$SUDO ufw allow 80/tcp  || true
$SUDO ufw allow 443/tcp || true
$SUDO ufw --force enable
ok "ufw OK"

# ── 4. リポジトリ clone or pull ──────
step "4/8 リポジトリ取得"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  log "$INSTALL_DIR は既に clone 済み → git pull"
  cd "$INSTALL_DIR"
  git pull --ff-only
elif [[ -n "$REPO_URL" ]]; then
  log "git clone $REPO_URL → $INSTALL_DIR"
  $SUDO mkdir -p "$(dirname "$INSTALL_DIR")"
  $SUDO chown "$(whoami):$(whoami)" "$(dirname "$INSTALL_DIR")"
  git clone "$REPO_URL" "$INSTALL_DIR"
  cd "$INSTALL_DIR"
else
  error "リポジトリが未 clone です。第1引数に GitHub URL を渡してください:"
  error "  bash deploy.sh https://github.com/<owner>/mzaiko-mvp.git"
  exit 1
fi
ok "$INSTALL_DIR で作業します"

# ── 5. .env.production の確認 ────────
step "5/8 .env.production の確認"
if [[ ! -f "$ENV_FILE" ]]; then
  if [[ -f "$ENV_EXAMPLE" ]]; then
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    warn "$ENV_FILE が無かったので $ENV_EXAMPLE からコピーしました"
    warn "→ 必ず以下を編集してから再実行してください:"
    warn "    nano $INSTALL_DIR/$ENV_FILE"
    warn "→ 最低限 DOMAIN / LETSENCRYPT_EMAIL / POSTGRES_PASSWORD / API_KEY /"
    warn "         CJ_API_KEY / EBAY_CLIENT_ID/SECRET/REFRESH_TOKEN を実値に"
    exit 1
  else
    error "$ENV_FILE も $ENV_EXAMPLE も無いのでデプロイできません"
    exit 1
  fi
fi

# 必須キーがプレースホルダのままだとエラー
set -a; . "./$ENV_FILE"; set +a
missing_keys=()
[[ "${DOMAIN:-example.com}" == "example.com" ]]                      && missing_keys+=("DOMAIN")
[[ "${LETSENCRYPT_EMAIL:-admin@example.com}" == "admin@example.com" ]] && missing_keys+=("LETSENCRYPT_EMAIL")
[[ "${POSTGRES_PASSWORD:-CHANGE_ME_STRONG_PASSWORD}" == "CHANGE_ME_STRONG_PASSWORD" ]] && missing_keys+=("POSTGRES_PASSWORD")
[[ "${API_KEY:-CHANGE_ME_RANDOM_32_OR_MORE_CHARS}" == "CHANGE_ME_RANDOM_32_OR_MORE_CHARS" ]] && missing_keys+=("API_KEY")
if (( ${#missing_keys[@]} > 0 )); then
  error "以下が未設定（プレースホルダのまま）: ${missing_keys[*]}"
  error "→ nano $INSTALL_DIR/$ENV_FILE で実値に書き換えてから再実行してください"
  exit 1
fi
ok "必須環境変数 OK（DOMAIN=$DOMAIN）"

# ── 6. Let's Encrypt 初回取得 ────────
step "6/8 Let's Encrypt 証明書"
COMPOSE="docker compose -f $COMPOSE_FILE --env-file $ENV_FILE"
CERT_PATH="/etc/letsencrypt/live/$DOMAIN/fullchain.pem"
if $COMPOSE run --rm --entrypoint "test -f $CERT_PATH" certbot 2>/dev/null; then
  ok "既に証明書あり → スキップ"
else
  log "init-letsencrypt.sh を実行..."
  bash scripts/init-letsencrypt.sh
fi

# ── 7. docker compose up -d ──────────
step "7/8 全サービス起動"
$COMPOSE up -d --build
ok "compose up 完了"

# ── 8. ヘルスチェック ────────────────
step "8/8 ヘルスチェック（最大 60 秒待機）"
for i in $(seq 1 12); do
  sleep 5
  if curl -fsS -o /dev/null -k "https://$DOMAIN/" 2>/dev/null \
     || curl -fsS -o /dev/null "http://localhost/" 2>/dev/null; then
    ok "サイトに到達できました"
    break
  fi
  log "...待機中 ($((i*5))s)"
done

echo
$COMPOSE ps
echo
ok "deploy 完了:  https://$DOMAIN/"
log "ログ確認:    $COMPOSE logs -f api"
log "再デプロイ:  cd $INSTALL_DIR && git pull && bash scripts/deploy.sh"
