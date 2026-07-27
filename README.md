# 小智 ESP32 自建服务端（Python）· LinkPal / SpeakPal

为 [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) 客户端配套开发的服务端，
用来替代官方的 `mqtt.xiaozhi.me`，把语音助手的“大脑”放到你自己的服务器上。

## 在线访问 / 试用

| 入口 | 链接 | 说明 |
|------|------|------|
| **官网（LinkPal）** | https://linkpal.cloud/ | 产品介绍、硬件与部署说明 |
| **英语口语练习（SpeakPal）** | https://linkpal.cloud/english/ | 网页版 AI 口语陪练，可直接试用 |
| **短链** | https://linkpal.cloud/speak/ | 同上，便于分享 |

> 浏览器打开 SpeakPal 后按提示登录（手机号验证码），允许麦克风即可开口练习；也支持看图练英语。

整条链路：

```
设备麦克风 --Opus(16k)--> [本服务端] --解码--> 流式ASR(识别) --> 文本
                                                              │
                                                          大模型(Qwen)
                                                              │
设备扬声器 <--Opus(24k)-- [本服务端] <--编码-- 流式TTS(合成) <-- 文本(按句)
```

- **ASR（语音转文字）**：阿里百炼 `paraformer-realtime-v2`（流式）
- **LLM（对话大脑）**：阿里通义千问 `qwen-plus`
- **TTS（文字转语音）**：阿里百炼 CosyVoice `cosyvoice-v2`

## 支持两种传输方式

设备固件会根据 OTA 下发的配置自动选协议。本服务端两种都实现了，用环境变量 `TRANSPORT` 切换：

| `TRANSPORT` | 说明 | 用到的服务 |
|-------------|------|-----------|
| `mqtt`（默认，**与设备出厂一致**） | 控制消息走 MQTT，音频走 UDP（AES-CTR 加密），和官方 `mqtt.xiaozhi.me` 一样 | MQTT 网关 + UDP + OTA |
| `websocket` | 单条 WebSocket 连接，JSON + 二进制帧，最简单 | WebSocket + OTA |

> 你的设备当前就是走 **MQTT+UDP**，所以默认 `TRANSPORT=mqtt` 即可直接对接。

### 端口一览

| 服务 | 默认端口 | 作用 |
|------|---------|------|
| OTA HTTP | `8002` | 下发配置，把设备引导到下面的语音通道 |
| MQTT 网关 | `1883` | 控制通道（hello/listen/stt/tts/...），明文 TCP（非 TLS） |
| UDP 音频 | `8001` | 加密 Opus 音频上下行 |
| WebSocket | `8000` | 仅 `TRANSPORT=websocket` 时使用 |

> 关于 MQTT：设备端**从不订阅主题**，只发布消息并期望服务器把回复直接推回它的连接。
> 所以这里不是用标准 broker（如 Mosquitto），而是自己实现了一个**轻量 MQTT 网关**
> （`xiaozhi/mqtt_broker.py`），每条 TCP 连接对应一个设备会话，直接往该连接推 PUBLISH。

---

## 一、安装与运行（服务器侧）

### 1. 系统依赖 libopus

```bash
# Ubuntu / Debian
sudo apt-get install -y libopus0
# macOS
brew install opus
```

### 2. Python 依赖（虚拟环境）

```bash
cd /home/chenyj/xiaozhi_server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. 配置

最关键的两项：

- `PUBLIC_HOST`：**你服务器的局域网 IP**（设备要用它连 MQTT/UDP，不能是 127.0.0.1）
- `DASHSCOPE_API_KEY`：阿里百炼 key（已内置你提供的 key 作默认值，可直接跑）

```bash
export PUBLIC_HOST=192.168.1.50    # 改成你服务器实际 IP
export TRANSPORT=mqtt              # 默认就是 mqtt，可省略
```

更多可调项见 `.env.example` 和 `config.py`。

### 4. 启动

```bash
python server.py
```

看到这些就说明起来了：

```
OTA 服务监听 http://0.0.0.0:8002/xiaozhi/ota/
MQTT 网关监听 0.0.0.0:1883
UDP 音频服务监听 0.0.0.0:8001
服务已就绪。
```

---

## 二、把设备指向你的服务端（设备侧，只需一次）

设备的 OTA 地址来自固件配置 `CONFIG_OTA_URL`（当前是 `https://api.tenclass.net/xiaozhi/ota/`）。
把它改成你服务器的 OTA 地址，设备开机后就会从你的 OTA 拿到 **你的 MQTT 地址**（而不是 `mqtt.xiaozhi.me`）。

在 ESP32 工程 `E:\Source\xiaozhi-esp32-main` 里：

**方法 A：menuconfig（推荐）**

```powershell
idf.py menuconfig
# 进入: Xiaozhi Assistant  ->  Default OTA URL
# 改成: http://192.168.1.50:8002/xiaozhi/ota/   （结尾带斜杠，用你服务器实际 IP）
```

**方法 B：直接改 sdkconfig**

```
CONFIG_OTA_URL="http://192.168.1.50:8002/xiaozhi/ota/"
```

然后重新编译烧录（清掉旧的 mqtt.xiaozhi.me 配置）：

```powershell
idf.py build
idf.py -p COM6 erase-flash flash monitor
```

烧录后串口里应该能看到：

```
Configured MQTT endpoint: 192.168.1.50:1883      <- 变成你的地址了
MQTT: Connecting to endpoint 192.168.1.50:1883
[MQTT+UDP] server=192.168.1.50 port=8001 ...
```

说“你好小智”唤醒，正常就会连到你的服务端开始对话。

> 注意：设备和服务器要在**同一局域网**，服务器防火墙放行 `1883`、`8001`、`8002`（UDP 8001 别忘了放行）。

---

## 三、不接真机也能测（端到端自测）

先启动 `server.py`，再开另一个终端：

```bash
source .venv/bin/activate

# 测 MQTT+UDP 全链路（模拟设备：CONNECT->hello->加密UDP上行->收下行音频）
python tools/e2e_mqtt_test.py

# 或测 WebSocket 全链路（需先用 TRANSPORT=websocket 启动 server）
python tools/e2e_test.py
```

`e2e_mqtt_test.py` 实测输出：

```
[client] 收到 hello, session=xxxx, udp=127.0.0.1:8001
[client] STT = 你好，请用一句话介绍一下你自己。
[client] TTS 句子 = 你好呀，我是小智……
下行音频(UDP解密+解码)时长 ≈ 8.0s
MQTT+UDP 端到端链路: ✅ 成功
```

---

## 四、代码结构

```
xiaozhi_server/
├── server.py              # 入口：同时启动 OTA + MQTT网关 + UDP + WebSocket
├── config.py              # 配置（环境变量可覆盖）
├── requirements.txt
├── .env.example
├── tools/
│   ├── e2e_mqtt_test.py   # MQTT+UDP 端到端自测（模拟设备）
│   └── e2e_test.py        # WebSocket 端到端自测
└── xiaozhi/
    ├── ota_server.py      # OTA 配置下发（按 TRANSPORT 下发 mqtt 或 websocket）
    ├── mqtt_broker.py     # 轻量 MQTT 网关（控制通道）
    ├── udp_server.py      # UDP 音频通道（按 ssrc 路由到会话）
    ├── ws_server.py       # WebSocket 接入层
    ├── transports.py      # 传输实现：WebSocketTransport / MqttUdpTransport(AES-CTR)
    ├── crypto.py          # UDP 音频 AES-128-CTR 加解密
    ├── session.py         # 会话编排：ASR->LLM->TTS 全链路 + 消息时序（与传输无关）
    ├── opus_codec.py      # Opus 编解码 + 分帧
    ├── text_utils.py      # 流式文本按句切分（给 TTS）
    └── providers/         # AI 能力（可替换成私有化模型）
        ├── asr.py         # DashScope Paraformer 流式识别
        ├── llm.py         # DashScope Qwen 流式对话
        └── tts.py         # DashScope CosyVoice 合成
```

---

## 五、协议要点（与设备固件对齐）

**MQTT+UDP 模式：**

- 控制通道（MQTT）：设备 PUBLISH `hello/listen/abort/goodbye`；服务器 PUBLISH `hello/stt/tts/...` 回设备连接
- hello 回复里带 `udp{server,port,key,nonce}`，设备据此建立 UDP 并初始化 AES
- UDP 包结构：`type(1)|flags(1)|payload_len(2)|ssrc(4)|timestamp(4)|sequence(4)|加密Opus`
  - 前 16 字节明文头同时用作 **AES-CTR 的 IV**
  - 我们把每个会话的 id 放进 `ssrc`（4 字节），上行包据此路由到对应会话
- 加密：AES-128-CTR，key/nonce 由服务器在 hello 时生成下发
- 下行音频参数：`sample_rate=24000, frame_duration=60`

**通用时序：** `stt` → `tts start` → 若干 `sentence_start` + 音频 → `tts stop`。
**关键：必须先发 `tts start` 再发音频帧**，否则设备还没切到 speaking 会丢弃音频。

---

## 六、后续可扩展

1. **流式更细**：现在按整句做 TTS，可改成 LLM 增量片段直接流式合成，降低首字延迟。
2. **MCP 设备控制**：实现 `type: "mcp"` 的 JSON-RPC，让“调音量/开灯”真的生效。
3. **私有化替换**：把 `providers/` 换成本地 FunASR / 本地 CosyVoice，即可离线运行。
4. **多设备**：已天然支持（每条连接一个独立会话，UDP 按 ssrc 路由）。

> 安全提醒：`config.py` 内置了你在对话中提供的 DashScope key，建议尽快改用环境变量并轮换该 key。
