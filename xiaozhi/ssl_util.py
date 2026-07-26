"""TLS 证书加载与自签证书生成（供 HTTPS / WSS 使用）。"""
import logging
import ssl
import subprocess
from pathlib import Path

logger = logging.getLogger("ssl")


def ensure_cert(cert_file: str, key_file: str, host: str) -> tuple[Path, Path]:
    cert_path = Path(cert_file)
    key_path = Path(key_file)
    if cert_path.is_file() and key_path.is_file():
        return cert_path, key_path

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    logger.warning(
        "未找到 SSL 证书，正在为 %s 生成自签证书（微信内建议使用正式域名证书）",
        host,
    )
    subject = f"/CN={host}"
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", str(key_path),
        "-out", str(cert_path),
        "-days", "825",
        "-nodes",
        "-subj", subject,
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    logger.info("自签证书已写入 %s", cert_path.parent)
    return cert_path, key_path


def load_server_ssl_context(cert_file: str, key_file: str, host: str) -> ssl.SSLContext:
    cert_path, key_path = ensure_cert(cert_file, key_file, host)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_path), str(key_path))
    return ctx
