#!/usr/bin/env bash
# Germany Career Crawler: install or update on a fresh Ubuntu server (22.04 / 24.04 / 26.04).
#
#   sudo ./install.sh                                    # crawdler.duckdns.org, defaults
#   sudo ./install.sh --duckdns-token <token> --email you@example.net
#   sudo ./install.sh --domain jobs.example.org          # any other domain
#
# Run it from the project directory (the one with docker-compose.yml). Running it again
# updates the installation: .env, passwords and data are kept, images are rebuilt.
#
# What it does:
#   1. installs Docker Engine + Compose plugin (Docker's apt repo, fallback: Ubuntu packages)
#   2. adds swap on small servers (the dashboard build needs ~1 GB RAM)
#   3. optionally updates the DuckDNS record now and every 5 minutes (systemd timer)
#   4. checks that the domain points to this server and ports 80/443 are free
#   5. creates .env with random secrets (only on first run) and opens 80/443 in ufw if active
#   6. builds and starts the stack, waits for the API and the HTTPS certificate
set -Eeuo pipefail

DOMAIN="${DOMAIN:-crawdler.duckdns.org}"
ACME_EMAIL="${ACME_EMAIL:-}"
DUCKDNS_TOKEN="${DUCKDNS_TOKEN:-}"
ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
SKIP_DNS_CHECK=0
NO_START=0
NO_DOCKER_INSTALL=0

usage() {
	sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
	cat <<'EOF'
Options:
  --domain NAME           public host name (default: crawdler.duckdns.org)
  --email ADDRESS         e-mail for Let's Encrypt expiry notices (optional)
  --duckdns-token TOKEN   DuckDNS token: point the domain to this server's IP and keep it updated
  --admin-user NAME       dashboard login (default: admin; only used on first run)
  --skip-dns-check        do not stop when the domain does not resolve to this server
  --no-start              prepare everything but do not build/start containers
  --no-docker-install     fail instead of installing Docker
  -h, --help              this help

The same settings can be given as environment variables: DOMAIN, ACME_EMAIL, DUCKDNS_TOKEN, ADMIN_USERNAME.
EOF
}

while [[ $# -gt 0 ]]; do
	case "$1" in
	--domain) DOMAIN="${2:?--domain needs a value}"; shift 2 ;;
	--email) ACME_EMAIL="${2:?--email needs a value}"; shift 2 ;;
	--duckdns-token) DUCKDNS_TOKEN="${2:?--duckdns-token needs a value}"; shift 2 ;;
	--admin-user) ADMIN_USERNAME="${2:?--admin-user needs a value}"; shift 2 ;;
	--skip-dns-check) SKIP_DNS_CHECK=1; shift ;;
	--no-start) NO_START=1; shift ;;
	--no-docker-install) NO_DOCKER_INSTALL=1; shift ;;
	-h | --help) usage; exit 0 ;;
	*) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
	esac
done

# ------------------------------------------------------------------ helpers
if [[ -t 1 ]]; then B=$'\e[1m' G=$'\e[32m' Y=$'\e[33m' R=$'\e[31m' N=$'\e[0m'; else B="" G="" Y="" R="" N=""; fi
step() { echo; echo "${B}==> $*${N}"; }
ok() { echo "  ${G}✓${N} $*"; }
warn() { echo "  ${Y}!${N} $*"; }
die() { echo "  ${R}✗ $*${N}" >&2; exit 1; }
trap 'die "failed at line $LINENO: $BASH_COMMAND"' ERR

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"
ENV_FILE="$PROJECT_DIR/.env"
export DEBIAN_FRONTEND=noninteractive

rand() { openssl rand -base64 64 | tr -dc 'A-Za-z0-9' | cut -c1-"$1"; } # no SIGPIPE under pipefail

env_get() { # KEY → value from .env (empty if missing)
	[[ -f "$ENV_FILE" ]] || return 0
	grep -E "^$1=" "$ENV_FILE" | tail -n1 | cut -d= -f2- || true
}

env_set() { # KEY VALUE → replace or append, value written verbatim
	local key="$1" value="$2" tmp
	tmp="$(mktemp)"
	if grep -qE "^${key}=" "$ENV_FILE"; then
		awk -v k="$key" -v v="$value" 'BEGIN{FS=OFS="="} $1==k {print k "=" v; next} {print}' "$ENV_FILE" >"$tmp"
	else
		cat "$ENV_FILE" >"$tmp"
		printf '%s=%s\n' "$key" "$value" >>"$tmp"
	fi
	cat "$tmp" >"$ENV_FILE"
	rm -f "$tmp"
}

public_ipv4() {
	local ip
	for url in https://api.ipify.org https://ipv4.icanhazip.com https://ifconfig.me/ip; do
		ip="$(curl -4fsS --max-time 8 "$url" 2>/dev/null | tr -d '[:space:]')" || continue
		[[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] && { echo "$ip"; return 0; }
	done
	return 1
}

resolve_ipv4() { getent ahostsv4 "$1" 2>/dev/null | awk '{print $1; exit}'; }

# ------------------------------------------------------------------ 1. preflight
step "Preflight"
[[ $EUID -eq 0 ]] || die "run as root: sudo ./install.sh"
missing=""
for f in docker-compose.yml backend/Dockerfile frontend/package.json deploy/Caddyfile deploy/Dockerfile.web; do
	[[ -e "$f" ]] || missing+=" $f"
done
[[ -z "$missing" ]] || die "project files missing in $PROJECT_DIR:$missing (copy the whole project folder, including hidden files)"
if [[ -r /etc/os-release ]]; then
	# shellcheck disable=SC1091
	. /etc/os-release
	[[ "${ID:-}" == "ubuntu" ]] || warn "tested on Ubuntu; this is ${PRETTY_NAME:-unknown}"
	ok "${PRETTY_NAME:-Linux} ($(uname -m))"
fi
[[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]] || die "invalid domain: $DOMAIN"
if [[ -n "$ACME_EMAIL" && ! "$ACME_EMAIL" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]]; then
	die "invalid e-mail '$ACME_EMAIL' (needs a domain like name@example.net); leave out --email if you don't want certificate notices"
fi
ok "domain: $DOMAIN"

step "Base packages"
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg openssl iproute2 >/dev/null
ok "curl, openssl, gnupg"

# ------------------------------------------------------------------ 2. Docker
step "Docker"
if docker compose version >/dev/null 2>&1; then
	ok "$(docker --version | cut -d, -f1), compose $(docker compose version --short)"
else
	[[ $NO_DOCKER_INSTALL -eq 0 ]] || die "Docker with the compose plugin is not installed"
	install -m 0755 -d /etc/apt/keyrings
	codename="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
	installed=0
	if curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc; then
		chmod a+r /etc/apt/keyrings/docker.asc
		echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${codename} stable" \
			>/etc/apt/sources.list.d/docker.list
		if apt-get update -qq 2>/dev/null &&
			apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null 2>&1; then
			installed=1
			ok "Docker CE from download.docker.com ($codename)"
		else
			warn "Docker's repository has no packages for '$codename' yet, using Ubuntu's packages"
			rm -f /etc/apt/sources.list.d/docker.list
			apt-get update -qq
		fi
	fi
	if [[ $installed -eq 0 ]]; then
		apt-get install -y -qq docker.io docker-compose-v2 >/dev/null || die "could not install Docker"
		ok "docker.io + docker-compose-v2 from Ubuntu"
	fi
	systemctl enable --now docker >/dev/null 2>&1 || true
	docker compose version >/dev/null 2>&1 || die "docker compose is not working after installation"
	ok "$(docker --version | cut -d, -f1), compose $(docker compose version --short)"
fi

# ------------------------------------------------------------------ 3. swap
step "Memory"
mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
swap_mb=$(awk '/SwapTotal/ {print int($2/1024)}' /proc/meminfo)
if ((mem_mb < 2000 && swap_mb < 1000)); then
	if [[ ! -f /swapfile ]]; then
		fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
		chmod 600 /swapfile
		mkswap /swapfile >/dev/null
	fi
	swapon /swapfile 2>/dev/null || true
	grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
	ok "${mem_mb} MB RAM: added 2 GB swap for the image build"
else
	ok "${mem_mb} MB RAM, ${swap_mb} MB swap"
fi

# ------------------------------------------------------------------ 4. DuckDNS + DNS check
SERVER_IP="$(public_ipv4 || true)"
if [[ -n "$SERVER_IP" ]]; then ok "public IPv4 of this server: $SERVER_IP"; else warn "could not determine the public IPv4"; fi

if [[ "$DOMAIN" == *.duckdns.org ]]; then
	step "DuckDNS"
	sub="${DOMAIN%.duckdns.org}"
	if [[ -n "$DUCKDNS_TOKEN" ]]; then
		install -m 0600 /dev/null /etc/duckdns.env
		printf 'DUCKDNS_DOMAIN=%s\nDUCKDNS_TOKEN=%s\n' "$sub" "$DUCKDNS_TOKEN" >/etc/duckdns.env
		answer="$(curl -fsS --max-time 15 "https://www.duckdns.org/update?domains=${sub}&token=${DUCKDNS_TOKEN}&ip=" || true)"
		[[ "$answer" == OK* ]] || die "DuckDNS rejected the update (answer: '${answer:-no answer}'): check token and subdomain '$sub'"
		ok "DuckDNS record '$sub' points to this server"
		cat >/etc/systemd/system/duckdns-update.service <<'EOF'
[Unit]
Description=Update DuckDNS record with the current public IP
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
EnvironmentFile=/etc/duckdns.env
ExecStart=/bin/sh -c 'curl -fsS --max-time 15 "https://www.duckdns.org/update?domains=$${DUCKDNS_DOMAIN}&token=$${DUCKDNS_TOKEN}&ip=" | grep -q "^OK"'
EOF
		cat >/etc/systemd/system/duckdns-update.timer <<'EOF'
[Unit]
Description=Update DuckDNS every 5 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF
		systemctl daemon-reload
		systemctl enable --now duckdns-update.timer >/dev/null 2>&1
		ok "systemd timer duckdns-update.timer keeps the IP current (token in /etc/duckdns.env, mode 600)"
	elif systemctl is-enabled duckdns-update.timer >/dev/null 2>&1; then
		ok "duckdns-update.timer already installed"
	else
		warn "no --duckdns-token: make sure '$sub' points to ${SERVER_IP:-this server} at https://www.duckdns.org"
	fi
fi

step "DNS"
dns_ip=""
for _ in $(seq 1 12); do
	dns_ip="$(resolve_ipv4 "$DOMAIN" || true)"
	[[ -n "$dns_ip" && ( -z "$SERVER_IP" || "$dns_ip" == "$SERVER_IP" ) ]] && break
	[[ -n "$DUCKDNS_TOKEN" ]] || break # only wait when we just updated the record
	sleep 5
done
if [[ -z "$dns_ip" ]]; then
	msg="$DOMAIN does not resolve"
elif [[ -n "$SERVER_IP" && "$dns_ip" != "$SERVER_IP" ]]; then
	msg="$DOMAIN resolves to $dns_ip, but this server is $SERVER_IP"
else
	msg=""
	ok "$DOMAIN → $dns_ip"
fi
if [[ -n "$msg" ]]; then
	if [[ $SKIP_DNS_CHECK -eq 1 ]]; then
		warn "$msg (continuing because of --skip-dns-check; Let's Encrypt will fail until this is fixed)"
	else
		die "$msg. Fix the DNS record (or pass --duckdns-token), or use --skip-dns-check."
	fi
fi

# ------------------------------------------------------------------ 5. ports + firewall
step "Ports 80/443"
busy=""
for port in 80 443; do
	line="$(ss -Hltnp "sport = :$port" 2>/dev/null | head -n1 || true)"
	if [[ -n "$line" && "$line" != *docker-proxy* ]]; then
		busy+="  port $port: $(grep -o 'users:(("[^"]*"' <<<"$line" | cut -d'"' -f2 || echo "$line")"$'\n'
	fi
done
[[ -z "$busy" ]] || die "ports in use by another program (stop it first, e.g. 'systemctl disable --now nginx'):
$busy"
ok "free (or already used by this stack)"

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
	ufw allow OpenSSH >/dev/null
	ufw allow 80/tcp >/dev/null
	ufw allow 443/tcp >/dev/null
	ufw allow 443/udp >/dev/null
	ok "ufw: opened 80/tcp, 443/tcp, 443/udp (SSH kept open)"
else
	ok "ufw not active; nothing to change"
fi
warn "if your provider has a cloud firewall, allow inbound TCP 80 and 443 there as well"

# ------------------------------------------------------------------ 6. configuration
step "Configuration (.env)"
NEW_PASSWORD=""
if [[ ! -f "$ENV_FILE" ]]; then
	if [[ -f .env.example ]]; then
		install -m 0600 .env.example "$ENV_FILE"
	else
		install -m 0600 /dev/null "$ENV_FILE"
		cat >"$ENV_FILE" <<'EOF'
# Generated by install.sh (.env.example was not copied). Change values, then: docker compose up -d
ADMIN_USERNAME=admin
OVERPASS_URL=https://overpass-api.de/api/interpreter
OVERPASS_STATUS_URL=https://overpass-api.de/api/status
OVERPASS_MAX_REQUESTS_PER_DAY=50
OVERPASS_MAX_MB_PER_DAY=200
OVERPASS_MIN_INTERVAL_S=30
OVERPASS_CACHE_HOURS=24
OVERPASS_QUERY_TIMEOUT_S=180
VERIFY_CONCURRENCY=8
VERIFY_TIMEOUT_S=15
VERIFY_RECHECK_DAYS=30
AUTO_QUEUE_VERIFIED=true
IMPORT_MAX_MB=20
LOG_LEVEL=INFO
SESSION_TTL_HOURS=12
EOF
	fi
	NEW_PASSWORD="$(rand 20)"
	env_set POSTGRES_PASSWORD "$(rand 32)"
	env_set SECRET_KEY "$(openssl rand -hex 32)"
	env_set ADMIN_USERNAME "$ADMIN_USERNAME"
	env_set ADMIN_PASSWORD "$NEW_PASSWORD"
	ok "created .env with random database password, session key and admin password"
else
	chmod 600 "$ENV_FILE"
	[[ -n "$(env_get POSTGRES_PASSWORD)" && "$(env_get POSTGRES_PASSWORD)" != "change-me-long-random" ]] \
		|| env_set POSTGRES_PASSWORD "$(rand 32)"
	(($(env_get SECRET_KEY | wc -c) > 32)) || env_set SECRET_KEY "$(openssl rand -hex 32)"
	if (($(env_get ADMIN_PASSWORD | wc -c) <= 12)); then
		NEW_PASSWORD="$(rand 20)"
		env_set ADMIN_PASSWORD "$NEW_PASSWORD"
	fi
	ok "kept existing .env (secrets unchanged)"
fi
env_set SITE_ADDRESS "$DOMAIN"
env_set COOKIE_SECURE true
ua="$(env_get CRAWLER_USER_AGENT)"
if [[ -z "$ua" || "$ua" == *jobs.example.org* || "$ua" == *your-org* ]]; then
	env_set CRAWLER_USER_AGENT "GermanyCareerCrawler/0.1 (+https://${DOMAIN}/bot)"
fi
sed -i '/^ACME_EMAIL=/d' "$ENV_FILE" # replaced by deploy/caddy.d/acme.caddy
ok "SITE_ADDRESS=$DOMAIN, CRAWLER_USER_AGENT=$(env_get CRAWLER_USER_AGENT)"

mkdir -p deploy/caddy.d
if [[ -n "$ACME_EMAIL" ]]; then
	printf 'email %s\n' "$ACME_EMAIL" >deploy/caddy.d/acme.caddy
	ok "Let's Encrypt notices go to $ACME_EMAIL"
elif [[ -f deploy/caddy.d/acme.caddy ]]; then
	ok "keeping $(cat deploy/caddy.d/acme.caddy)"
fi

if [[ $NO_START -eq 1 ]]; then
	step "Done (--no-start)"
	echo "  Start later with: cd $PROJECT_DIR && docker compose up -d --build"
	[[ -n "$NEW_PASSWORD" ]] && echo "  Dashboard login: $(env_get ADMIN_USERNAME) / $NEW_PASSWORD"
	exit 0
fi

# ------------------------------------------------------------------ 7. build + start
step "Build and start (first build takes a few minutes)"
docker compose pull --quiet db redis 2>/dev/null || true
docker compose build --pull
docker compose up -d --remove-orphans
ok "containers started"

step "Waiting for the API"
api_ok=0
for _ in $(seq 1 60); do
	state="$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q api)" 2>/dev/null || echo starting)"
	if [[ "$state" == "healthy" ]]; then api_ok=1; break; fi
	sleep 5
done
if [[ $api_ok -eq 1 ]]; then
	ok "API healthy (migrations applied)"
else
	docker compose ps
	docker compose logs --tail=40 api || true
	die "API did not become healthy within 5 minutes (see logs above)"
fi

step "Waiting for HTTPS on https://$DOMAIN"
https_ok=0
for _ in $(seq 1 36); do
	if curl -fsS --max-time 10 --resolve "${DOMAIN}:443:127.0.0.1" "https://${DOMAIN}/api/health" >/dev/null 2>&1; then
		https_ok=1
		break
	fi
	sleep 5
done
if [[ $https_ok -eq 1 ]]; then
	ok "certificate issued, dashboard reachable"
else
	warn "no valid certificate yet. Check: docker compose logs web | grep -i -E 'error|challenge'"
	warn "usual causes: DNS not pointing here, port 80 blocked by a cloud firewall, Let's Encrypt rate limit"
fi

docker compose ps --format 'table {{.Service}}\t{{.Status}}'

# ------------------------------------------------------------------ summary
echo
echo "${B}Germany Career Crawler is running${N}"
echo "  URL:       https://$DOMAIN/"
echo "  API docs:  https://$DOMAIN/api/docs"
if [[ -n "$NEW_PASSWORD" ]]; then
	echo "  Login:     $(env_get ADMIN_USERNAME) / ${B}${NEW_PASSWORD}${N}"
	echo "             (shown once; stored in $ENV_FILE)"
else
	echo "  Login:     $(env_get ADMIN_USERNAME) / unchanged (see ADMIN_PASSWORD in $ENV_FILE)"
fi
cat <<EOF

  Update:    cd $PROJECT_DIR && sudo ./install.sh
  Logs:      docker compose logs -f api worker-discovery worker-verify
  Status:    docker compose ps
  Backup:    docker compose exec -T db pg_dump -U gcc gcc | gzip > gcc-\$(date +%F).sql.gz
  Stop:      docker compose down          (data volumes are kept)
EOF
