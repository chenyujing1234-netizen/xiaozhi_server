#!/usr/bin/env bash
# 为 linkpal.cloud 申请 Let's Encrypt 证书并启用 nginx 反向代理
#
# 前置条件（DNSPod 或其他 DNS 控制台）:
#   linkpal.cloud     A  -> 服务器公网 IP（当前 124.221.115.174）
#   www.linkpal.cloud A  -> 同上（或与根域相同）
#
# 用法:
#   sudo ./tools/setup_linkpal_ssl.sh
#   sudo ./tools/setup_linkpal_ssl.sh --email your@email.com
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DOMAIN="linkpal.cloud"
WWW="www.linkpal.cloud"
NGINX_AVAILABLE="/etc/nginx/sites-available/${DOMAIN}"
NGINX_ENABLED="/etc/nginx/sites-enabled/${DOMAIN}"
PUBLIC_IP="$(curl -s --max-time 5 ifconfig.me || true)"
EMAIL="${CERTBOT_EMAIL:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --email) EMAIL="$2"; shift 2 ;;
    --domain) DOMAIN="$2"; WWW="www.${DOMAIN}"; shift 2 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请使用 root 运行: sudo $0"
  exit 1
fi

echo "==> 检查 DNS: ${DOMAIN}"
RESOLVED="$(dig +short A "${DOMAIN}" | head -1 || true)"
if [[ -z "${RESOLVED}" ]]; then
  echo "错误: ${DOMAIN} 尚无 A 记录。"
  echo "请先在 DNS 控制台添加:"
  echo "  ${DOMAIN}     A  ${PUBLIC_IP:-你的服务器IP}"
  echo "  ${WWW}  A  ${PUBLIC_IP:-你的服务器IP}"
  exit 1
fi
echo "    ${DOMAIN} -> ${RESOLVED}"
if [[ -n "${PUBLIC_IP}" && "${RESOLVED}" != "${PUBLIC_IP}" ]]; then
  echo "警告: 解析 IP (${RESOLVED}) 与当前公网 IP (${PUBLIC_IP}) 不一致，证书申请可能失败。"
fi

mkdir -p /var/www/certbot

echo "==> 写入临时 HTTP nginx 配置（用于 ACME 验证）"
cat > "${NGINX_AVAILABLE}" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN} ${WWW};

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 200 'LinkPal SpeakPal — waiting for SSL certificate';
        add_header Content-Type text/plain;
    }
}
EOF

ln -sf "${NGINX_AVAILABLE}" "${NGINX_ENABLED}"
nginx -t
systemctl reload nginx

echo "==> 申请 Let's Encrypt 证书"
CERTBOT_ARGS=(certonly --webroot -w /var/www/certbot -d "${DOMAIN}" -d "${WWW}" --agree-tos --non-interactive)
if [[ -n "${EMAIL}" ]]; then
  CERTBOT_ARGS+=(--email "${EMAIL}")
else
  CERTBOT_ARGS+=(--register-unsafely-without-email)
fi
certbot "${CERTBOT_ARGS[@]}"

# certbot 证书目录名通常与 -d 的第一个域名或已有命名规则一致；统一到 www.${DOMAIN}
CERT_NAME="${WWW}"
if [[ ! -d "/etc/letsencrypt/live/${CERT_NAME}" ]]; then
  CERT_NAME="${DOMAIN}"
fi
if [[ ! -d "/etc/letsencrypt/live/${CERT_NAME}" ]]; then
  CERT_NAME="$(certbot certificates 2>/dev/null | awk -v d="${DOMAIN}" '/Certificate Name:/ {n=$3} /Domains:/ && index($0,d) {print n; exit}')"
fi

if [[ -z "${CERT_NAME}" || ! -d "/etc/letsencrypt/live/${CERT_NAME}" ]]; then
  echo "错误: 找不到证书目录，请检查 certbot certificates"
  exit 1
fi

echo "==> 部署完整 HTTPS nginx 配置（证书: ${CERT_NAME}）"
sed "s|www.linkpal.cloud|${CERT_NAME}|g; s|linkpal.cloud|${DOMAIN}|g; s|www.${DOMAIN}|${WWW}|g" \
  "${ROOT}/deploy/nginx/linkpal.cloud.conf" > "${NGINX_AVAILABLE}"

# 若 cert 名就是 www.domain，上面 sed 可能重复替换；用实际路径再写一次
cat > "${NGINX_AVAILABLE}" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN} ${WWW};

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://\$host\$request_uri;
    }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name ${DOMAIN} ${WWW};

    ssl_certificate /etc/letsencrypt/live/${CERT_NAME}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${CERT_NAME}/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    location / {
        proxy_pass http://127.0.0.1:8002;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location /xiaozhi/english/v1/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }
}
EOF

nginx -t
systemctl reload nginx

echo ""
echo "=========================================="
echo "  SpeakPal 正式入口已就绪"
echo "  https://${DOMAIN}/english/"
echo "  https://${DOMAIN}/speak/"
echo "  WSS: wss://${DOMAIN}/xiaozhi/english/v1/"
echo "=========================================="
echo ""
echo "建议在 xiaozhi_server 环境变量中设置:"
echo "  ENGLISH_WEB_DOMAIN=${DOMAIN}"
echo "  HTTPS_ENABLED=0"
echo ""
echo "证书自动续期由 certbot 系统定时任务处理。"
