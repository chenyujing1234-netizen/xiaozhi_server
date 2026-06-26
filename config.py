"""服务端全局配置。

所有配置都可以通过环境变量覆盖，方便部署。默认值可以让服务“开箱即跑”。
"""
import os
import socket


def _detect_lan_ip() -> str:
    """尽量探测本机在局域网中的 IP，用于下发给设备的 WebSocket 地址。

    设备（ESP32）需要用这个 IP 连回服务端，所以不能用 127.0.0.1。
    如果探测不准，请用环境变量 PUBLIC_HOST 手动指定。
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # 不会真正发包，只是让系统选出一个出口网卡的地址
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class Config:
    # ---- 网络监听 ----
    # 对外暴露的主机地址（设备会用它连回来）。强烈建议手动设置为你服务器的局域网/公网 IP。
    PUBLIC_HOST = os.getenv("PUBLIC_HOST", _detect_lan_ip())

    # 设备使用哪种传输：mqtt（设备默认） 或 websocket
    # OTA 会据此下发对应的配置段。
    TRANSPORT = os.getenv("TRANSPORT", "mqtt").lower()

    # WebSocket 语音通道
    WS_HOST = os.getenv("WS_HOST", "0.0.0.0")
    WS_PORT = int(os.getenv("WS_PORT", "8000"))
    WS_PATH = os.getenv("WS_PATH", "/xiaozhi/v1/")

    # MQTT 网关（控制通道）
    MQTT_HOST = os.getenv("MQTT_HOST", "0.0.0.0")
    MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
    MQTT_PUBLISH_TOPIC = os.getenv("MQTT_PUBLISH_TOPIC", "device-server")

    # UDP 音频通道（MQTT 模式下）
    UDP_HOST = os.getenv("UDP_HOST", "0.0.0.0")
    UDP_PORT = int(os.getenv("UDP_PORT", "8001"))

    # OTA 配置下发 HTTP 接口
    OTA_HOST = os.getenv("OTA_HOST", "0.0.0.0")
    OTA_PORT = int(os.getenv("OTA_PORT", "8002"))

    # ---- DashScope / 阿里百炼 ----
    # 提示：把密钥放进环境变量更安全。这里内置了你提供的 key 作为默认值，方便直接运行。
    DASHSCOPE_API_KEY = os.getenv(
        "DASHSCOPE_API_KEY",
        "sk-ws-H.RYLDILR.NR8G.MEUCIQCWFz5V6w_-9HZQOfJj_dFkAPfI9dmBSwM9amxy_dctlQIgQZswktjuiCJzApdrVCU6H60CQU78HbGauiml-QvCQes",
    )

    # 大模型（对话）
    LLM_MODEL = os.getenv("LLM_MODEL", "qwen-plus")
    LLM_SYSTEM_PROMPT = os.getenv(
        "LLM_SYSTEM_PROMPT",
        "你是一个名叫小智的语音助手，运行在一个小型智能音箱上。"
        "请用简洁、口语化、自然的中文回答，不要使用 Markdown、表情符号或特殊格式，"
        "因为你的回答会被转成语音念出来。回答尽量控制在几句话以内。",
    )

    # 语音识别（流式 ASR）
    ASR_MODEL = os.getenv("ASR_MODEL", "paraformer-realtime-v2")

    # 语音合成（流式 TTS）
    TTS_MODEL = os.getenv("TTS_MODEL", "cosyvoice-v2")
    TTS_VOICE = os.getenv("TTS_VOICE", "longxiaochun_v2")

    # ---- 音频参数 ----
    # 上行（设备 -> 服务端）：设备固件固定为 16k / 单声道 / 60ms
    UPLINK_SAMPLE_RATE = 16000
    # 下行（服务端 -> 设备）：我们用 24k，会在 hello 里告诉设备
    DOWNLINK_SAMPLE_RATE = int(os.getenv("DOWNLINK_SAMPLE_RATE", "24000"))
    CHANNELS = 1
    FRAME_DURATION_MS = 60

    # 下行音频发送节奏（秒/帧）。略快于实时(0.06)，让设备播放缓冲略有富余但不溢出。
    DOWNLINK_PACING_SEC = float(os.getenv("DOWNLINK_PACING_SEC", "0.045"))

    # ---- 日志 ----
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    # 是否把每段对话的音频保存到 saved_audio/ 目录（上行解码后的 wav）
    SAVE_AUDIO = os.getenv("SAVE_AUDIO", "0") == "1"
    SAVE_AUDIO_DIR = os.getenv("SAVE_AUDIO_DIR", "saved_audio")

    @property
    def ws_url_for_device(self) -> str:
        return f"ws://{self.PUBLIC_HOST}:{self.WS_PORT}{self.WS_PATH}"


config = Config()
