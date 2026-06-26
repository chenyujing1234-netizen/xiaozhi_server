"""UDP 音频的 AES-128-CTR 加解密。

设备端用 mbedtls 的 AES-CTR：把每个 UDP 包前 16 字节的头当作初始计数器(IV)，
对其余字节做 CTR 流加密。CTR 模式加解密是同一套运算，所以这里一个函数搞定。
"""
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def aes_ctr_crypt(key: bytes, iv16: bytes, data: bytes) -> bytes:
    """key: 16 字节; iv16: 16 字节初始计数器; data: 任意长度。"""
    cipher = Cipher(algorithms.AES(key), modes.CTR(iv16))
    enc = cipher.encryptor()
    return enc.update(data) + enc.finalize()
