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
    # 中文 CosyVoice 整句下发可用略快节奏；英语 MCU（Omni 流式）请用 ENGLISH_DOWNLINK_PACING_SEC。
    DOWNLINK_PACING_SEC = float(os.getenv("DOWNLINK_PACING_SEC", "0.045"))
    # 英语 MCU：按接近实时节奏推 UDP，避免设备小缓冲过载/欠载导致卡顿叠音
    ENGLISH_DOWNLINK_PACING_SEC = float(os.getenv("ENGLISH_DOWNLINK_PACING_SEC", "0.058"))
    # 英语 MCU：开始推流前预缓冲的 Opus 帧数（8×60ms≈480ms，吸收开播阶段 Omni/网络抖动）
    ENGLISH_DOWNLINK_PREBUFFER_FRAMES = int(os.getenv("ENGLISH_DOWNLINK_PREBUFFER_FRAMES", "8"))
    # 英语 MCU：tts start 后等待多久再发首包 UDP。
    # 固件仅在 Speaking 状态收音频；MQTT 切状态有延迟，发早了会被丢，表现为开头 1–3 秒一字一顿。
    ENGLISH_TTS_START_LEAD_SEC = float(os.getenv("ENGLISH_TTS_START_LEAD_SEC", "0.28"))
    # 英语 MCU：预缓冲齐后，前几帧用更短间隔突发，尽快填满设备解码队列
    ENGLISH_DOWNLINK_BURST_FRAMES = int(os.getenv("ENGLISH_DOWNLINK_BURST_FRAMES", "6"))
    ENGLISH_DOWNLINK_BURST_PACING_SEC = float(os.getenv("ENGLISH_DOWNLINK_BURST_PACING_SEC", "0.02"))
    # 英语 MCU：tts stop 后延迟再开上行，避免喇叭残留/回声触发下一轮重叠回复
    ENGLISH_UPLINK_REOPEN_DELAY_SEC = float(os.getenv("ENGLISH_UPLINK_REOPEN_DELAY_SEC", "0.35"))

    # ---- 静音超时（节省 ASR 流式识别费用）----
    # 连续多少秒没有有效语音 → 关闭 ASR 并向设备发 goodbye（退出“聆听中”）
    SILENCE_TIMEOUT_SEC = float(os.getenv("SILENCE_TIMEOUT_SEC", "15"))
    # PCM 能量阈值（16-bit RMS）。低于此值视为静音/噪声，不送入 ASR
    SPEECH_RMS_THRESHOLD = float(os.getenv("SPEECH_RMS_THRESHOLD", "700"))
    # 连续多少帧超过阈值才视为有效语音（过滤环境底噪尖峰，每帧 60ms）
    SPEECH_FRAMES_REQUIRED = int(os.getenv("SPEECH_FRAMES_REQUIRED", "3"))
    # 用户说完后，VAD 连续静音多少秒即认为一句话结束并送 LLM（不等待云端语义断句）
    UTTERANCE_END_SILENCE_SEC = float(os.getenv("UTTERANCE_END_SILENCE_SEC", "2"))
    # 连续多少次「有输入迹象但无有效文本」→ 视为环境噪声，下发 goodbye 退出聆听
    # 默认通道：VAD 说完但 ASR 无文本；英语通道：音频已送 Omni 但无用户转写
    EMPTY_UTTERANCE_LIMIT = int(os.getenv("EMPTY_UTTERANCE_LIMIT", "3"))
    # 英语：本地 VAD 判定说完后，等待 Omni 用户转写的最长时间（秒），超时计 1 次空转
    EMPTY_OMNI_TRANSCRIPT_TIMEOUT_SEC = float(
        os.getenv("EMPTY_OMNI_TRANSCRIPT_TIMEOUT_SEC", "8")
    )
    # 英语：持续向 Omni 送「有效语音」却一直无用户转写（噪声/server_vad 不收尾）的最长时间
    EMPTY_OMNI_SPEECH_NO_TEXT_SEC = float(
        os.getenv("EMPTY_OMNI_SPEECH_NO_TEXT_SEC", "10")
    )
    # 英语：已向 Omni 提交/等待回复后，多久无音频/完成事件视为云端失败并通知客户端
    ENGLISH_OMNI_RESPONSE_TIMEOUT_SEC = float(
        os.getenv("ENGLISH_OMNI_RESPONSE_TIMEOUT_SEC", "25")
    )
    # 云端异常 alert 在 MCU 上保留多久再发 goodbye（goodbye 会清屏；太短用户看不清）
    ALERT_DISPLAY_SEC = float(os.getenv("ALERT_DISPLAY_SEC", "6"))

    # 流式 LLM → TTS：累计多少字且遇到逗号/句号等软标点时，先合成并下发首段音频
    TTS_SPLIT_MIN_CHARS = int(os.getenv("TTS_SPLIT_MIN_CHARS", "10"))

    # ---- 日志 ----
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    # 是否在日志中打印上行音频 RMS 能量（约每秒 1 条，用于调 VAD 阈值）
    LOG_AUDIO_RMS = os.getenv("LOG_AUDIO_RMS", "0") == "1"
    # 英语通道上行 RMS（默认开；设 ENGLISH_LOG_AUDIO_RMS=0 关闭）
    ENGLISH_LOG_AUDIO_RMS = os.getenv("ENGLISH_LOG_AUDIO_RMS", "1") == "1"
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
    ENGLISH_OMNI_MODEL = os.getenv("ENGLISH_OMNI_MODEL", "qwen3.5-omni-flash-realtime")
    ENGLISH_OMNI_VOICE = os.getenv("ENGLISH_OMNI_VOICE", "Ethan")
    ENGLISH_OMNI_INSTRUCTIONS = os.getenv(
        "ENGLISH_OMNI_INSTRUCTIONS",
        "You are XiaoYu (小语), a relaxed English chat buddy for Chinese kids around age 11. "
        "You are NOT a teacher, tutor, or homework helper. You talk like a friendly, slightly cool "
        "peer who happens to speak English — curious, playful, never preachy. "
        "Never say: practice, study, lesson, homework, correct your mistakes, or I'm your tutor. "
        "Follow the student profile for language mix, reply length, and correction strictness. "
        "Relaxed-mode defaults when the profile does not override:\n"
        "- First 5 turns: chat for fun only. No corrections unless they explicitly ask, "
        "or meaning is completely blocked.\n"
        "- Allow the student to speak Chinese freely; reply in short natural English with "
        "optional brief Chinese gloss only when needed.\n"
        "- Keep replies short and voice-friendly; one idea per turn unless they ask for more.\n"
        "- Topics they enjoy: games, sports, anime, pets, food, funny stories, school life (light).\n"
        "- If they send a photo: react like a friend (\"Oh cool!\"), name one or two things, "
        "invite them to tell you more — not a vocabulary drill.\n"
        "- After rapport is built, gently weave in useful English; still skip correction "
        "unless they ask or a mistake clearly blocks understanding.\n"
        "- When they DO ask to practice pronunciation/spelling or say \"correct me\", "
        "switch to helpful mode: brief Chinese tip, clear model sentence once, invite repeat.\n"
        "- When spelling letter by letter, use spaces or commas between letters, never hyphens.\n"
        "- PROACTIVE LEAD: You carry the conversation. End most turns with ONE easy question "
        "(yes/no, either-or, or one-word answer). Accept Chinese answers warmly.\n"
        "- If they seem stuck (silence, 不知道, um): offer 2-3 fun choices immediately.\n"
        "- Playful mini-challenges are OK as games (\"Quick! Cat or dog?\") — never frame as homework.\n"
        "Keep replies natural for voice. Do not use markdown, bullet points, or emoji.",
    )
    # Omni Realtime WebSocket（国内默认 endpoint）
    ENGLISH_OMNI_WS_URL = os.getenv(
        "ENGLISH_OMNI_WS_URL",
        "wss://dashscope.aliyuncs.com/api-ws/v1/realtime",
    )
    # 英语：按轮路由 cheap TEXT（ASR+LLM+TTS） vs OMNI（听发音纠音）。0=始终 Omni（旧行为）
    ENGLISH_ROUTE_ENABLED = os.getenv("ENGLISH_ROUTE_ENABLED", "1") == "1"
    ENGLISH_DEFAULT_ROUTE = os.getenv("ENGLISH_DEFAULT_ROUTE", "text").lower()
    ENGLISH_ROUTER_LLM = os.getenv("ENGLISH_ROUTER_LLM", "0") == "1"
    ENGLISH_ROUTER_LLM_MODEL = os.getenv("ENGLISH_ROUTER_LLM_MODEL", LLM_MODEL)
    ENGLISH_TEXT_LLM_MODEL = os.getenv("ENGLISH_TEXT_LLM_MODEL", "").strip() or LLM_MODEL
    ENGLISH_TEXT_TTS_MODEL = os.getenv("ENGLISH_TEXT_TTS_MODEL", "").strip() or TTS_MODEL
    ENGLISH_TEXT_TTS_VOICE = os.getenv("ENGLISH_TEXT_TTS_VOICE", "").strip() or TTS_VOICE
    # OMNI 轮结束后 N 秒内短句跟读仍走 Omni
    ENGLISH_OMNI_STICKY_SEC = float(os.getenv("ENGLISH_OMNI_STICKY_SEC", "45"))
    # TEXT 轮（LLM+TTS）首包优化：更短 lead、更小预缓冲、更早分句
    ENGLISH_TEXT_TTS_START_LEAD_SEC = float(
        os.getenv("ENGLISH_TEXT_TTS_START_LEAD_SEC", "0.12")
    )
    ENGLISH_TEXT_DOWNLINK_PREBUFFER_FRAMES = int(
        os.getenv("ENGLISH_TEXT_DOWNLINK_PREBUFFER_FRAMES", "2")
    )
    ENGLISH_TEXT_DOWNLINK_BURST_FRAMES = int(
        os.getenv("ENGLISH_TEXT_DOWNLINK_BURST_FRAMES", "8")
    )
    ENGLISH_TEXT_TTS_SPLIT_MIN_CHARS = int(
        os.getenv("ENGLISH_TEXT_TTS_SPLIT_MIN_CHARS", "6")
    )
    # 路由聆听：ASR 判句结束后，再等多久无新句则送 LLM（避免只靠 VAD 静音，噪声下永远不收尾）
    ENGLISH_ASR_UTTERANCE_GAP_SEC = float(
        os.getenv("ENGLISH_ASR_UTTERANCE_GAP_SEC", "1.0")
    )
    # 英语路由 ASR：Paraformer v2 双语提示（en+zh，不锁定单一语言）
    ENGLISH_ASR_LANGUAGE_HINTS = [
        h.strip()
        for h in os.getenv("ENGLISH_ASR_LANGUAGE_HINTS", "en,zh").split(",")
        if h.strip()
    ]
    # Omni 用户语音转写：language 留空=自动检测；仅在你明确只要一种语言时再设 en/zh
    ENGLISH_OMNI_TRANSCRIPTION_LANGUAGE = os.getenv(
        "ENGLISH_OMNI_TRANSCRIPTION_LANGUAGE", ""
    ).strip()
    # Omni 转写上下文（不锁语言，帮助中英混合场景；留空则不传）
    ENGLISH_OMNI_TRANSCRIPTION_CORPUS = os.getenv(
        "ENGLISH_OMNI_TRANSCRIPTION_CORPUS",
        "Bilingual English and Mandarin conversation. The student may speak English or Chinese.",
    ).strip()
    # TEXT 轮联网搜索（日期/新闻/天气等）；命中 search 路由或显式开启时生效
    ENGLISH_TEXT_ENABLE_SEARCH = os.getenv("ENGLISH_TEXT_ENABLE_SEARCH", "1") == "1"
    ENGLISH_TEXT_SEARCH_STRATEGY = os.getenv("ENGLISH_TEXT_SEARCH_STRATEGY", "turbo")
    # Omni Realtime 联网（纯 Omni / OMNI 回放）；策略需为 agent（百炼要求）
    ENGLISH_OMNI_ENABLE_SEARCH = os.getenv("ENGLISH_OMNI_ENABLE_SEARCH", "1") == "1"
    ENGLISH_OMNI_SEARCH_STRATEGY = os.getenv("ENGLISH_OMNI_SEARCH_STRATEGY", "agent")
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
    # Web 页进入时拉取展示的历史条数上限（可与注入 Omni 的条数不同）
    ENGLISH_HISTORY_UI_MAX_MESSAGES = int(
        os.getenv("ENGLISH_HISTORY_UI_MAX_MESSAGES", "50")
    )
    ENGLISH_HISTORY_IMAGE_DIR = os.getenv(
        "ENGLISH_HISTORY_IMAGE_DIR", "data/english_chat_images"
    )
    # Web 新用户（无历史）连上后小语主动开口打招呼
    ENGLISH_PROACTIVE_GREETING = os.getenv("ENGLISH_PROACTIVE_GREETING", "1") == "1"

    # ---- MySQL（英语用户画像等共享数据）----
    MYSQL_HOST = os.getenv("MYSQL_HOST", "114.55.254.123")
    MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
    MYSQL_USER = os.getenv("MYSQL_USER", "chenyujing")
    MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "Centerm1@")
    MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "xiaozhi_server")
    MYSQL_CHARSET = os.getenv("MYSQL_CHARSET", "utf8mb4")

    # ---- SpeakPal 用户登录（对齐 huanDa：微信 openid / 手机号短信）----
    # 密钥请放到 .env，勿写入仓库默认值
    JWT_SECRET = os.getenv("JWT_SECRET", "speakpal-jwt-secret-2026-change-me-32b")
    JWT_EXPIRE_DAYS = int(os.getenv("JWT_EXPIRE_DAYS", "30"))
    WX_APPID = os.getenv("WX_APPID", "")
    WX_SECRET = os.getenv("WX_SECRET", "")
    ALIYUN_ACCESS_KEY_ID = os.getenv("ALIYUN_ACCESS_KEY_ID", "")
    ALIYUN_ACCESS_KEY_SECRET = os.getenv("ALIYUN_ACCESS_KEY_SECRET", "")
    SMS_SIGN_NAME = os.getenv("SMS_SIGN_NAME", "哈希流光")
    SMS_TEMPLATE_CODE = os.getenv("SMS_TEMPLATE_CODE", "SMS_495955218")
    # 开发时可设 AUTH_SMS_DEBUG=1，短信失败仍返回成功并把验证码打日志
    AUTH_SMS_DEBUG = os.getenv("AUTH_SMS_DEBUG", "0") == "1"

    # 小智后台 API 鉴权（/admin/ 配置页）。请在 .env 中设置强随机字符串。
    ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()

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
