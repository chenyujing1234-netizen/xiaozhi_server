#!/usr/bin/env bash
# 在 8443 端口启用 nginx 统一代理（HTTPS 页面 + WSS 同端口）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${PUBLIC_HOST:-124.221.115.174}"
NGINX_AVAILABLE="/etc/nginx/sites-available/speakpal-8443"
NGINX_ENABLED="/etc/nginx/sites-enabled/speakpal-8443"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请使用 root 运行: sudo $0"
  exit 1
fi

if [[ ! -f "${ROOT}/certs/server.crt" ]]; then
  echo "==> 生成自签证书 CN=${HOST}"
  mkdir -p "${ROOT}/certs"
  openssl req -x509 -newkey rsa:2048 \
    -keyout "${ROOT}/certs/server.key" \
    -out "${ROOT}/certs/server.crt" \
    -days 825 -nodes -subj "/CN=${HOST}"
fi

echo "==> 安装 nginx 配置"
cp "${ROOT}/deploy/nginx/speakpal-8443.conf" "${NGINX_AVAILABLE}"
ln -sf "${NGINX_AVAILABLE}" "${NGINX_ENABLED}"
nginx -t
systemctl reload nginx

echo ""
echo "完成。请用 HTTPS_ENABLED=0 重启 xiaozhi_server（Python 不再占用 8443/8444）。"
echo "用户入口: https://${HOST}:8443/english/"
