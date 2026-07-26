#!/usr/bin/env bash
# 为 SpeakPal HTTPS/WSS 生成自签证书（开发/内网测试用）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CERT_DIR="${ROOT}/certs"
HOST="${1:-${PUBLIC_HOST:-localhost}}"
mkdir -p "$CERT_DIR"
openssl req -x509 -newkey rsa:2048 \
  -keyout "${CERT_DIR}/server.key" \
  -out "${CERT_DIR}/server.crt" \
  -days 825 -nodes \
  -subj "/CN=${HOST}"
echo "已生成:"
echo "  ${CERT_DIR}/server.crt"
echo "  ${CERT_DIR}/server.key"
echo "CN=${HOST} — 微信内建议使用正式域名 + Let's Encrypt 证书"
