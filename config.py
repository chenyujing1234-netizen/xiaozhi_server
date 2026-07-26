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

    # 面向用户的 Web 页（SpeakPal）：HTTPS + WSS，微信内录音需安全上下文
    HTTPS_ENABLED = os.getenv("HTTPS_ENABLED", "1") == "1"
    HTTPS_PORT = int(os.getenv("HTTPS_PORT", "8443"))
    WSS_PORT = int(os.getenv("WSS_PORT", "8444"))
    SSL_CERT_FILE = os.getenv("SSL_CERT_FILE", "certs/server.crt")
    SSL_KEY_FILE = os.getenv("SSL_KEY_FILE", "certs/server.key")
    # 正式域名（nginx + Let's Encrypt 终止 TLS 时使用，如 linkpal.cloud）
    ENGLISH_WEB_DOMAIN = os.getenv("ENGLISH_WEB_DOMAIN", "").strip()

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

    # ---- 静音超时（节省 ASR 流式识别费用）----
    # 连续多少秒没有有效语音 → 关闭 ASR 并向设备发 goodbye（退出“聆听中”）
    SILENCE_TIMEOUT_SEC = float(os.getenv("SILENCE_TIMEOUT_SEC", "15"))
    # PCM 能量阈值（16-bit RMS）。低于此值视为静音/噪声，不送入 ASR
    SPEECH_RMS_THRESHOLD = float(os.getenv("SPEECH_RMS_THRESHOLD", "700"))
    # 连续多少帧超过阈值才视为有效语音（过滤环境底噪尖峰，每帧 60ms）
    SPEECH_FRAMES_REQUIRED = int(os.getenv("SPEECH_FRAMES_REQUIRED", "3"))
    # 用户说完后，VAD 连续静音多少秒即认为一句话结束并送 LLM（不等待云端语义断句）
    UTTERANCE_END_SILENCE_SEC = float(os.getenv("UTTERANCE_END_SILENCE_SEC", "2"))
    # 连续多少次「VAD 有语音但 ASR 无文本」→ 视为环境噪声，下发 goodbye 退出聆听
    EMPTY_UTTERANCE_LIMIT = int(os.getenv("EMPTY_UTTERANCE_LIMIT", "3"))

    # 流式 LLM → TTS：累计多少字且遇到逗号/句号等软标点时，先合成并下发首段音频
    TTS_SPLIT_MIN_CHARS = int(os.getenv("TTS_SPLIT_MIN_CHARS", "10"))

    # ---- 日志 ----
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    # 是否在日志中打印上行音频 RMS 能量（约每秒 1 条，用于调 VAD 阈值）
    LOG_AUDIO_RMS = os.getenv("LOG_AUDIO_RMS", "0") == "1"
    # 是否把每段对话的音频保存到 saved_audio/ 目录（上行解码后的 wav）
    SAVE_AUDIO = os.getenv("SAVE_AUDIO", "0") == "1"
    SAVE_AUDIO_DIR = os.getenv("SAVE_AUDIO_DIR", "saved_audio")

    @property
    def ws_url_for_device(self) -> str:
        return f"ws://{self.PUBLIC_HOST}:{self.WS_PORT}{self.WS_PATH}"

    # ---- 英语口语练习智能体（独立 MQTT+UDP 端口，不影响默认小智通道）----
    ENGLISH_ENABLED = os.getenv("ENGLISH_ENABLED", "1") == "1"
    ENGLISH_MQTT_PORT = int(os.getenv("ENGLISH_MQTT_PORT", "1884"))
    ENGLISH_UDP_PORT = int(os.getenv("ENGLISH_UDP_PORT", "8003"))
    ENGLISH_WS_PATH = os.getenv("ENGLISH_WS_PATH", "/xiaozhi/english/v1/")

    # 语音转语音多模态模型（百炼 S2S，音频进 → 音频出，适合口语纠正）
    ENGLISH_OMNI_MODEL = os.getenv("ENGLISH_OMNI_MODEL", "qwen3.5-omni-plus-realtime")
    ENGLISH_OMNI_VOICE = os.getenv("ENGLISH_OMNI_VOICE", "Ethan")
    ENGLISH_OMNI_INSTRUCTIONS = os.getenv(
        "ENGLISH_OMNI_INSTRUCTIONS",
        "You are SpeakPal, a patient English speaking tutor for Chinese learners. "
        "You hear the student's spoken English directly. "
        "Follow the student profile section for language mix, reply length, and correction strictness. "
        "Default correction protocol when the profile does not say otherwise: "
        "1) Briefly acknowledge what they meant. "
        "2) If there is a clear issue in pronunciation, word choice, tense, or sentence structure, "
        "explain the issue in simple Chinese. "
        "3) Then give the correct English sentence and read it clearly once so they can repeat. "
        "Correct at most one or two important issues per turn; ignore tiny slips if meaning is clear. "
        "If the profile asks for gentle or fewer corrections, praise first and correct only blocking errors. "
        "If the profile asks for strict correction, be more thorough but still keep a warm tone. "
        "If the profile asks for English-only, explain corrections in simple English instead of Chinese. "
        "When the student asks for a story or detailed help, finish the content; do not stop after an intro. "
        "Keep replies natural for voice. Do not use markdown, bullet points, or emoji.",
    )
    # Omni Realtime WebSocket（国内默认 endpoint）
    ENGLISH_OMNI_WS_URL = os.getenv(
        "ENGLISH_OMNI_WS_URL",
        "wss://dashscope.aliyuncs.com/api-ws/v1/realtime",
    )
    # 英语用户画像（自然语言段落，按 device_id 存 MySQL）
    ENGLISH_PROFILE_AUTO_UPDATE = os.getenv("ENGLISH_PROFILE_AUTO_UPDATE", "1") == "1"
    ENGLISH_PROFILE_UPDATE_COOLDOWN_SEC = float(
        os.getenv("ENGLISH_PROFILE_UPDATE_COOLDOWN_SEC", "20")
    )
    # 每 N 轮对话沉淀一次画像（显式偏好会立刻更新）
    ENGLISH_PROFILE_REFINE_EVERY_TURNS = int(
        os.getenv("ENGLISH_PROFILE_REFINE_EVERY_TURNS", "3")
    )
    ENGLISH_PROFILE_LLM_MODEL = os.getenv("ENGLISH_PROFILE_LLM_MODEL", LLM_MODEL)
    # 英语对话历史：重连时注入 Omni instructions 作为上下文
    ENGLISH_HISTORY_ENABLED = os.getenv("ENGLISH_HISTORY_ENABLED", "1") == "1"
    ENGLISH_HISTORY_MAX_MESSAGES = int(os.getenv("ENGLISH_HISTORY_MAX_MESSAGES", "20"))
    ENGLISH_HISTORY_MAX_CHARS = int(os.getenv("ENGLISH_HISTORY_MAX_CHARS", "2500"))

    # ---- MySQL（英语用户画像等共享数据）----
    MYSQL_HOST = os.getenv("MYSQL_HOST", "114.55.254.123")
    MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
    MYSQL_USER = os.getenv("MYSQL_USER", "chenyujing")
    MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "Centerm1@")
    MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "xiaozhi_server")
    MYSQL_CHARSET = os.getenv("MYSQL_CHARSET", "utf8mb4")

    @property
    def english_ws_url_for_device(self) -> str:
        return f"ws://{self.PUBLIC_HOST}:{self.WS_PORT}{self.ENGLISH_WS_PATH}"

    @property
    def english_wss_url_for_web(self) -> str:
        if self.ENGLISH_WEB_DOMAIN:
            return f"wss://{self.ENGLISH_WEB_DOMAIN}{self.ENGLISH_WS_PATH}"
        if self.HTTPS_ENABLED:
            return f"wss://{self.PUBLIC_HOST}:{self.WSS_PORT}{self.ENGLISH_WS_PATH}"
        return self.english_ws_url_for_device

    @property
    def english_web_url(self) -> str:
        if self.ENGLISH_WEB_DOMAIN:
            return f"https://{self.ENGLISH_WEB_DOMAIN}/english/"
        if self.HTTPS_ENABLED:
            return f"https://{self.PUBLIC_HOST}:{self.HTTPS_PORT}/english/"
        return f"http://{self.PUBLIC_HOST}:{self.OTA_PORT}/english/"


config = Config()
