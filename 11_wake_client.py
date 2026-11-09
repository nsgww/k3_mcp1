"""
端侧语音唤醒客户端（方案 B）：KWS + VAD 全在本地跑，待机音频不出设备。

与 09_asr_llm_ws.py 通过既有协议通信（PCM 推流 + {"type":"end"}），
服务端无需感知唤醒过程；唤醒后连说的词头由服务端 strip_wake_prefix 剥离。

状态机：
  IDLE       本地 KWS 常驻监听，不推流、不耗服务端资源
  WOKEN      KWS 命中 → 播提示音「叮」→ VAD 等开口，5s 无语音回 IDLE
  LISTENING  推流 PCM；VAD 静音 >= 700ms 判说完 → 发 end
  PROCESSING 等服务端 ASR/LLM/MCP/TTS；播报期间丢帧不喂 KWS/VAD（防自激）
  FOLLOWUP   应答后 15s 连续对话窗口：VAD 检测到语音直接收音，免唤醒

运行（板端）：
  python 11_wake_client.py [ws://192.168.1.228:8060/stream]
  python 11_wake_client.py --devices          # 列出麦克风
  python 11_wake_client.py --device-index 1   # 指定麦克风

依赖（板端与 09 同款环境）：sherpa_onnx / pyaudio / websockets / numpy / onnxruntime
模型：项目内 KWS 模型目录 + ~/.cache/sensevoice/silero_vad.onnx（缺失时板端自动 wget）
提示音：assets/wake_ding.wav 不存在时用 numpy 现场合成，无需下载。
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import sys
import threading
import time
import wave
from enum import Enum
from pathlib import Path

# 加载顺序（板端实测，同 09_asr_llm_ws.py）：必须先 import sherpa_onnx，
# 让它绑定自带 ORT（VERS_1.24.2）；再由 VAD 加载系统 onnxruntime（spacemit
# 1.24.0）。顺序反了 sherpa 的 .so 会链到系统 ORT，报 VERS_1.24.2 not found
import sherpa_onnx  # noqa: F401

import numpy as np
import pyaudio
import websockets

from kws_spotter import build_kws_engine_from_env, load_keyword_phrases
from spacemit_audio.record_vad import SileroVAD

# ---------------- 参数 ---------------- #
RATE = 16000
CHUNK = 512                      # 32ms @16kHz，与 Silero VAD 帧对齐
FRAME_MS = 32
DEFAULT_URI = "ws://192.168.1.228:8060/stream"

WOKEN_TIMEOUT = 5.0              # 唤醒后等开口的超时（秒）
FOLLOWUP_TIMEOUT = 15.0          # 连续对话窗口（秒）
VAD_ON = 0.5                     # 语音开始概率阈值
SILENCE_MS = 700                 # 静音判说完
MAX_UTT_MS = 10_000              # 单句最长收音
PRE_ROLL_FRAMES = 3000 // FRAME_MS   # WOKEN 期预缓冲上限（连说补偿，~3s）
DING_GRACE_MS = 350              # 提示音/播报后 VAD 静默期（防喇叭回采误触）
MIC_QUEUE_MAX = 200              # 采集队列（200×32ms ≈ 6.4s）
# PROCESSING 兜底：板端 TTS 慢，正常几分钟内 done 必达；超时说明服务端异常
PROCESSING_TIMEOUT = float(os.getenv("REPLY_TIMEOUT", "300"))

DING_PATH = Path(__file__).resolve().parent / "assets" / "wake_ding.wav"


class State(Enum):
    IDLE = "idle"
    WOKEN = "woken"
    LISTENING = "listening"
    PROCESSING = "processing"
    FOLLOWUP = "followup"


# ---------------- 提示音：内置合成，零下载 ---------------- #
def ensure_ding(path: Path = DING_PATH) -> Path | None:
    """提示音不存在时用 numpy 合成 260ms 双音「叮」并缓存。"""
    if path.is_file():
        return path
    try:
        sr = 16000

        def tone(freq: float, ms: int) -> np.ndarray:
            n = int(sr * ms / 1000)
            t = np.arange(n) / sr
            return np.sin(2 * np.pi * freq * t) * np.exp(-t * 10)

        pcm = np.concatenate([tone(880, 100), tone(1318, 160)])
        pcm = (pcm / max(np.abs(pcm).max(), 1e-6) * 0.55 * 32767).astype(np.int16)
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(pcm.tobytes())
        print(f"[ding] 已生成提示音 {path}")
        return path
    except Exception as e:
        print(f"[ding] 提示音生成失败（不影响唤醒）: {e}")
        return None


# ---------------- 播放 ---------------- #
def play_wav_file(path: Path) -> None:
    """板端 Linux 走 PyAudio；Windows 优先 winsound。"""
    if sys.platform == "win32":
        try:
            import winsound

            winsound.PlaySound(str(path), winsound.SND_FILENAME)
            return
        except Exception:
            pass
    with wave.open(str(path), "rb") as wf:
        pa = pyaudio.PyAudio()
        try:
            stream = pa.open(
                format=pa.get_format_from_width(wf.getsampwidth()),
                channels=wf.getnchannels(),
                rate=wf.getframerate(),
                output=True,
            )
            while True:
                frames = wf.readframes(1024)
                if not frames:
                    break
                stream.write(frames)
            stream.stop_stream()
            stream.close()
        finally:
            pa.terminate()


def play_wav_bytes(data: bytes) -> None:
    """服务端下发的 TTS wav（bytes）落临时文件播放。"""
    import tempfile

    fd, tmp = tempfile.mkstemp(suffix=".wav")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        play_wav_file(Path(tmp))
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ---------------- 麦克风采集线程 ---------------- #
class MicSource:
    """阻塞读麦克风（32ms/帧）→ asyncio.Queue；队列满丢新帧。"""

    def __init__(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop,
                 device_index: int | None = None):
        self.queue = queue
        self.loop = loop
        self.pa = pyaudio.PyAudio()
        self.stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=RATE,
            input=True,
            frames_per_buffer=CHUNK,
            input_device_index=device_index,
        )
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _try_put(self, data: bytes) -> None:
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data = self.stream.read(CHUNK, exception_on_overflow=False)
            except OSError:
                continue
            try:
                self.loop.call_soon_threadsafe(self._try_put, data)
            except RuntimeError:
                return

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self._stop.set()
        self.thread.join(timeout=1.0)
        try:
            self.stream.stop_stream()
            self.stream.close()
        finally:
            self.pa.terminate()


# ---------------- 唤醒状态机 ---------------- #
class WakeClient:
    def __init__(self, ws, kws_session, vad: SileroVAD, ding: Path | None,
                 followup: bool = True):
        self.ws = ws
        self.kws = kws_session
        self.vad = vad
        self.ding = ding
        self.followup_enabled = followup

        self.state = State.IDLE
        self.deadline = 0.0            # WOKEN/FOLLOWUP 超时点
        self.vad_mute_until = 0.0      # 提示音/播报后的 VAD 静默期
        self.pre_roll: collections.deque[bytes] = collections.deque(
            maxlen=PRE_ROLL_FRAMES
        )
        self.silence_ms = 0
        self.utt_ms = 0
        self.processing_since = 0.0    # PROCESSING 超时兜底计时
        self.broken = False            # 连接断开标记，触发外层重连

    # ---- 状态迁移 ----
    def _enter(self, state: State) -> None:
        if state is not self.state:
            print(f"[gate] {self.state.value} → {state.value}  ({time.strftime('%H:%M:%S')})")
            self.state = state

    def _enter_idle(self) -> None:
        self._enter(State.IDLE)
        self.pre_roll.clear()
        if self.kws is not None:
            self.kws.reset()

    def _enter_woken(self, now: float, keyword: str) -> None:
        print(f"[wake] 命中「{keyword}」")
        self._enter(State.WOKEN)
        self.deadline = now + WOKEN_TIMEOUT
        self.vad_mute_until = now + DING_GRACE_MS / 1000
        self.pre_roll.clear()
        self.vad.reset()
        if self.ding is not None:
            asyncio.create_task(asyncio.to_thread(play_wav_file, self.ding))

    def _enter_listening(self) -> None:
        self._enter(State.LISTENING)
        self.silence_ms = 0
        self.utt_ms = 0
        self.vad.reset()

    def on_reply_done(self) -> None:
        """服务端 done：TTS 播完（或纯文本轮结束）→ 连续对话窗口。"""
        now = time.monotonic()
        if self.followup_enabled:
            self._enter(State.FOLLOWUP)
            self.deadline = now + FOLLOWUP_TIMEOUT
            print(f"[gate] {FOLLOWUP_TIMEOUT:.0f}s 内可直接追问，无需再喊唤醒词")
        else:
            self._enter_idle()
        # 喇叭余振/尾音可能回采，给 VAD 一段静默期
        self.vad_mute_until = now + DING_GRACE_MS / 1000
        self.pre_roll.clear()
        self.vad.reset()
        if self.kws is not None:
            self.kws.reset()

    # ---- 每帧入口 ----
    async def feed_frame(self, pcm: bytes) -> None:
        now = time.monotonic()

        # 等回复/播报中：丢帧，不喂 KWS/VAD，防止喇叭回采自激
        if self.state is State.PROCESSING:
            if now - self.processing_since > PROCESSING_TIMEOUT:
                print(f"[gate] 等待回复超过 {PROCESSING_TIMEOUT:.0f}s，回待机")
                self._enter_idle()
            return

        if self.state is State.IDLE:
            if self.kws is None:
                return
            try:
                hits = self.kws.accept_pcm16(pcm)
            except Exception as e:
                print(f"[KWS] decode error: {e}")
                hits = []
            if hits:
                self._enter_woken(now, hits[0])
            return

        if self.state in (State.WOKEN, State.FOLLOWUP):
            if now >= self.deadline:
                print(f"[gate] {self.state.value} 超时无语音，回待机")
                self._enter_idle()
                return
            # FOLLOWUP 窗口内也接受再次喊唤醒词（用户习惯）
            if self.state is State.FOLLOWUP and self.kws is not None:
                try:
                    hits = self.kws.accept_pcm16(pcm)
                except Exception:
                    hits = []
                if hits:
                    self._enter_woken(now, hits[0])
                    return
            # 静默期（提示音/TTS 播报中）的帧直接丢弃：进 pre_roll 会污染指令音频
            if now < self.vad_mute_until:
                return
            self.pre_roll.append(pcm)
            if self.vad(pcm) > VAD_ON:
                self._enter_listening()
                # 预缓冲（含触发帧，即唤醒词之后的音频）整段上送，连说不丢字
                if self.pre_roll:
                    await self._send(bytes().join(self.pre_roll))
                    self.utt_ms = len(self.pre_roll) * FRAME_MS
                    self.pre_roll.clear()
            return

        if self.state is State.LISTENING:
            await self._send(pcm)
            self.utt_ms += FRAME_MS
            if self.vad(pcm) > VAD_ON:
                self.silence_ms = 0
            else:
                self.silence_ms += FRAME_MS
            if self.silence_ms >= SILENCE_MS or self.utt_ms >= MAX_UTT_MS:
                why = "说完" if self.silence_ms >= SILENCE_MS else "超时截断"
                print(f"[gate] {why}（{self.utt_ms / 1000:.1f}s），发送 end")
                self._enter(State.PROCESSING)
                self.processing_since = now
                await self._send_json({"type": "end"})
            return

    async def _send(self, data: bytes) -> None:
        try:
            await self.ws.send(data)
        except websockets.exceptions.ConnectionClosed as e:
            print(f"[ws] 发送失败，连接已断: {e}")
            self.broken = True

    async def _send_json(self, payload: dict) -> None:
        try:
            await self.ws.send(json.dumps(payload, ensure_ascii=False))
        except websockets.exceptions.ConnectionClosed as e:
            print(f"[ws] 发送失败，连接已断: {e}")
            self.broken = True


# ---------------- 服务端消息接收 ---------------- #
async def receiver(client: WakeClient) -> None:
    expect_tts_bytes = 0
    tts_text = ""
    llm_started = False
    async for msg in client.ws:
        if isinstance(msg, bytes):
            if expect_tts_bytes > 0:
                print(f"[TTS] 播放 {len(msg)} bytes ...")
                try:
                    await asyncio.to_thread(play_wav_bytes, msg)
                except Exception as e:
                    print(f"[TTS] 播放失败: {e}")
                expect_tts_bytes = 0
            continue
        try:
            d = json.loads(msg)
        except json.JSONDecodeError:
            continue
        mtype = d.get("type")

        if mtype == "status":
            extra = ""
            if "mcp_connected" in d:
                extra = f" | mcp={d.get('mcp_connected')} tools={d.get('mcp_tools')}"
            print(f"[status] {d.get('message', '')}{extra}")
        elif mtype == "kws":
            print(f"[服务端KWS] {d.get('message') or d.get('keyword')}")
        elif mtype == "asr":
            print(f"[ASR] {d.get('text', '')}")
        elif mtype == "function":
            print(
                f"[MCP] {d.get('name')} ok={d.get('ok')} "
                f"result={d.get('result') or d.get('error') or ''}"
            )
        elif mtype == "llm":
            text = d.get("text") or ""
            if text:
                if not llm_started:
                    print("[LLM] ", end="", flush=True)
                    llm_started = True
                print(text, end="", flush=True)
            elif d.get("done") and llm_started:
                print()
                llm_started = False
        elif mtype == "tts":
            expect_tts_bytes = int(d.get("byte_length") or 0)
            tts_text = d.get("text", "")
            print(f"[TTS] 合成完成: {tts_text[:60]!r}")
        elif mtype == "error":
            print(f"[ERROR] {d.get('message', '')}")
        elif mtype == "done":
            client.on_reply_done()
        elif mtype == "pong":
            pass
        else:
            print(f"[?] {d}")


# ---------------- 主流程：长连接 + 断线重连 ---------------- #
async def run_once(uri: str, kws_engine, vad: SileroVAD, ding: Path | None,
                   device_index: int | None, followup: bool) -> None:
    async with websockets.connect(uri, ping_interval=None,
                                  max_size=16 * 1024 * 1024) as ws:
        print(f"已连接 {uri}")
        client = WakeClient(
            ws,
            kws_engine.create_session() if kws_engine else None,
            vad,
            ding,
            followup=followup,
        )
        recv_task = asyncio.create_task(receiver(client))
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=MIC_QUEUE_MAX)
        mic = MicSource(queue, asyncio.get_running_loop(), device_index)
        mic.start()
        try:
            while not client.broken:
                pcm = await queue.get()
                await client.feed_frame(pcm)
                if recv_task.done():
                    exc = recv_task.exception()
                    if exc is not None:
                        raise exc
                    break
        finally:
            mic.close()
            recv_task.cancel()
            try:
                await recv_task
            except asyncio.CancelledError:
                pass


async def run(args) -> None:
    kws_engine = build_kws_engine_from_env()
    if kws_engine is None:
        sys.exit("[KWS] 引擎不可用（检查 sherpa_onnx 与模型目录），无法端侧唤醒")
    print(f"[KWS] 唤醒词: {' / '.join(load_keyword_phrases())}")
    vad = SileroVAD()  # 板端模型缺失时会自动 wget 下载
    ding = None if args.no_ding else ensure_ding()

    print(f"端侧唤醒已就绪：喊唤醒词 → 叮 → 说指令"
          f"{'（连续对话 ' + str(int(FOLLOWUP_TIMEOUT)) + 's）' if not args.no_followup else ''}")
    while True:
        try:
            await run_once(args.uri, kws_engine, vad, ding,
                           args.device_index, not args.no_followup)
        except (websockets.exceptions.ConnectionClosed, OSError) as e:
            print(f"[ws] 连接断开: {e}")
        except Exception as e:
            print(f"[ws] 异常: {type(e).__name__}: {e}")
        print("[ws] 2s 后重连...")
        await asyncio.sleep(2)


def list_devices() -> None:
    pa = pyaudio.PyAudio()
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info["maxInputChannels"] > 0:
            print(f"  [{i}] {info['name']} (默认采样率 {int(info['defaultSampleRate'])})")
    pa.terminate()


def main() -> None:
    ap = argparse.ArgumentParser(description="端侧语音唤醒客户端（方案 B）")
    ap.add_argument("uri", nargs="?", default=DEFAULT_URI)
    ap.add_argument("--devices", action="store_true", help="列出麦克风后退出")
    ap.add_argument("--device-index", type=int, default=None, help="麦克风索引")
    ap.add_argument("--no-followup", action="store_true", help="关闭 15s 连续对话窗口")
    ap.add_argument("--no-ding", action="store_true", help="关闭唤醒提示音")
    args = ap.parse_args()

    if args.devices:
        list_devices()
        return
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
