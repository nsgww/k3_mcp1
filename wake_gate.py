"""
服务端唤醒门控（方案 A）：KWS + VAD 状态机，见《语音唤醒实现方案.md》第 4 节。

前端只负责持续推 PCM（16kHz int16 mono，任意分包），本模块负责：
  IDLE       只跑 KWS，不缓冲、不进 ASR
  WOKEN      KWS 命中，等 VAD 检测到语音，超时 5s 回 IDLE
  LISTENING  累积指令音频，VAD 静音 >= 700ms 判说完 → 输出整段音频
  PROCESSING 音频已交出（ASR/LLM/TTS 执行中），期间帧丢弃
  FOLLOWUP   应答后 15s 连续对话窗口：VAD 语音直接收音，KWS 命中可重新唤醒

与端侧客户端 11_wake_client.py 同源，差异：
- 输入是任意分包的流，内部重组为 32ms（1024B）帧再喂 KWS/VAD
- 不播音：事件通过 drain_events() 交给 09 下发（wake_state / kws 消息）
- TTS 播报时长由 09 传入，FOLLOWUP 的 VAD 静默期覆盖播报全程（防前端
  喇叭回采触发误收音；前端播放期间暂停上行仍是更优解，见方案 5.3）

超时由帧流驱动：前端须持续推流（含静音），停推则超时判定停滞。
"""

from __future__ import annotations

import collections
import time
from enum import Enum
from typing import Callable, Optional

FRAME_BYTES = 1024          # 32ms @16kHz int16 mono，与 Silero VAD 帧对齐
FRAME_MS = 32

WOKEN_TIMEOUT = 5.0         # 唤醒后等开口超时（秒）
FOLLOWUP_TIMEOUT = 15.0     # 连续对话窗口（秒）
VAD_ON = 0.5                # 语音开始概率阈值
SILENCE_MS = 700            # 静音判说完
MAX_UTT_MS = 10_000         # 单句最长收音
PRE_ROLL_MS = 3000          # WOKEN 期预缓冲上限（连说补偿）
GRACE_S = 0.35              # 提示音/TTS 尾音的 VAD 静默期基础值
PROCESSING_TIMEOUT = 300.0  # PROCESSING 兜底（调用方忘调 on_reply_done 时回 IDLE）


class WakeState(Enum):
    IDLE = "idle"
    WOKEN = "woken"
    LISTENING = "listening"
    PROCESSING = "processing"
    FOLLOWUP = "followup"


class WakeGate:
    """每 WS 连接一个：feed() 进 PCM，drain_events() 出下行消息。"""

    def __init__(
        self,
        kws_session,
        vad,
        format_kws_message: Callable[[str], str] = lambda kw: f"检测到关键词:{kw}",
        woken_timeout: float = WOKEN_TIMEOUT,
        followup_timeout: float = FOLLOWUP_TIMEOUT,
        one_shot: bool = False,
    ):
        self.kws = kws_session      # kws_spotter.KwsSession，可为 None（纯 VAD 模式）
        self.vad = vad              # SileroVAD，必须
        self._fmt_kw = format_kws_message
        self.woken_timeout = float(woken_timeout)       # 唤醒后等开口超时（秒）
        self.followup_timeout = float(followup_timeout) # 连续对话窗口（秒）
        self.one_shot = bool(one_shot)  # 单发模式：应答后回 IDLE，需再次唤醒才能继续

        self.state = WakeState.IDLE
        self._pending = bytearray()     # 未凑满一帧的残留
        self.pre_roll: collections.deque[bytes] = collections.deque(
            maxlen=PRE_ROLL_MS // FRAME_MS
        )
        self.buf = bytearray()          # LISTENING 累积的指令音频
        self.silence_ms = 0
        self.utt_ms = 0
        self.deadline = 0.0             # WOKEN/FOLLOWUP 超时点
        self.vad_mute_until = 0.0       # VAD 静默期（回采保护）
        self.processing_since = 0.0
        self._events: list[dict] = []

    # ---- 运行时可调参数 ----
    def set_timeouts(
        self,
        woken_timeout: Optional[float] = None,
        followup_timeout: Optional[float] = None,
    ) -> None:
        """运行时调整唤醒控制时间（秒），下一次状态转换生效。0 = 不限时。"""
        if woken_timeout is not None:
            if not 0 <= float(woken_timeout) <= 120:
                raise ValueError("woken_timeout 需在 0~120 秒之间（0 = 不限时）")
            self.woken_timeout = float(woken_timeout)
        if followup_timeout is not None:
            if not 0 <= float(followup_timeout) <= 600:
                raise ValueError("followup_timeout 需在 0~600 秒之间（0 = 不限时）")
            self.followup_timeout = float(followup_timeout)

    # ---- 事件（由 09 取走下发） ----
    def drain_events(self) -> list[dict]:
        ev, self._events = self._events, []
        return ev

    def _enter(self, state: WakeState) -> None:
        if state is self.state:
            return
        print(f"[gate] {self.state.value} → {state.value}  ({time.strftime('%H:%M:%S')})")
        self.state = state
        self._events.append({"type": "wake_state", "state": state.value})

    def _enter_idle(self) -> None:
        self._enter(WakeState.IDLE)
        self.pre_roll.clear()
        self.buf = bytearray()
        self._pending = bytearray()
        if self.kws is not None:
            self.kws.reset()

    def _enter_woken(self, now: float, keyword: str) -> None:
        print(f"[wake] 命中「{keyword}」")
        self._events.append(
            {
                "type": "kws",
                "keyword": keyword,
                "message": self._fmt_kw(keyword),
            }
        )
        self._enter(WakeState.WOKEN)
        # 0 = 不限时：等到开口为止
        self.deadline = now + self.woken_timeout if self.woken_timeout > 0 else float("inf")
        # 前端收到 wake_state 后播提示音（含网络延迟），此期间不判 VAD
        self.vad_mute_until = now + GRACE_S
        self.pre_roll.clear()
        self.vad.reset()

    # ---- 每帧入口 ----
    def feed(self, pcm: bytes, now: Optional[float] = None) -> Optional[bytes]:
        """喂任意长度 PCM；返回完整指令音频（交 _process_utterance）或 None。"""
        now = time.monotonic() if now is None else now
        self._pending.extend(pcm)
        out: Optional[bytes] = None
        while len(self._pending) >= FRAME_BYTES:
            frame = bytes(self._pending[:FRAME_BYTES])
            del self._pending[:FRAME_BYTES]
            audio = self._feed_frame(frame, now)
            if audio is not None:
                out = audio          # 一段指令只会从某一帧边界产出一次
        return out

    def _feed_frame(self, frame: bytes, now: float) -> Optional[bytes]:
        if self.state is WakeState.PROCESSING:
            if now - self.processing_since > PROCESSING_TIMEOUT:
                print(f"[gate] PROCESSING 超过 {PROCESSING_TIMEOUT:.0f}s 未收尾，回待机")
                self._enter_idle()
            return None

        if self.state is WakeState.IDLE:
            if self.kws is None:
                return None
            try:
                hits = self.kws.accept_pcm16(frame)
            except Exception as e:
                print(f"[KWS] decode error: {e}")
                hits = []
            if hits:
                self._enter_woken(now, hits[0])
            return None

        if self.state in (WakeState.WOKEN, WakeState.FOLLOWUP):
            if now >= self.deadline:
                print(f"[gate] {self.state.value} 超时无语音，回待机")
                self._enter_idle()
                return None
            if self.state is WakeState.FOLLOWUP and self.kws is not None:
                try:
                    hits = self.kws.accept_pcm16(frame)
                except Exception:
                    hits = []
                if hits:
                    self._enter_woken(now, hits[0])
                    return None
            # 静默期（提示音/TTS 播报中）的帧直接丢弃：进 pre_roll 会污染指令音频
            if now < self.vad_mute_until:
                return None
            self.pre_roll.append(frame)
            if self.vad(frame) > VAD_ON:
                self._enter(WakeState.LISTENING)
                self.silence_ms = 0
                self.vad.reset()
                # 预缓冲（含触发帧）并入指令，连说不丢字
                self.buf = bytearray().join(self.pre_roll)
                self.utt_ms = len(self.pre_roll) * FRAME_MS
                self.pre_roll.clear()
            return None

        if self.state is WakeState.LISTENING:
            self.buf.extend(frame)
            self.utt_ms += FRAME_MS
            if self.vad(frame) > VAD_ON:
                self.silence_ms = 0
            else:
                self.silence_ms += FRAME_MS
            if self.silence_ms >= SILENCE_MS or self.utt_ms >= MAX_UTT_MS:
                why = "说完" if self.silence_ms >= SILENCE_MS else "超时截断"
                print(f"[gate] {why}（{self.utt_ms / 1000:.1f}s），输出指令音频")
                audio = bytes(self.buf)
                self.buf = bytearray()
                self._enter(WakeState.PROCESSING)
                self.processing_since = now
                return audio
        return None

    def mute_vad_for(self, seconds: float) -> None:
        """延长 VAD 静默期（如唤醒应答语音播放期间），防止回采触发收音。"""
        if seconds <= 0:
            return
        until = time.monotonic() + seconds + GRACE_S
        self.vad_mute_until = max(self.vad_mute_until, until)

    def on_reply_done(self, tts_seconds: float = 0.0,
                      now: Optional[float] = None) -> None:
        """一轮应答完成。tts_seconds 为下发的 TTS 播报时长，用于回采静默期。"""
        now = time.monotonic() if now is None else now
        if self.one_shot:
            # 单发模式：一次控制后结束，回待机，需再次唤醒才能继续
            print("[gate] one-shot：应答结束，回待机等待再次唤醒")
            self._enter_idle()
            return
        self._enter(WakeState.FOLLOWUP)
        # 0 = 不限时：连续对话窗口不关闭
        self.deadline = (
            now + self.followup_timeout if self.followup_timeout > 0 else float("inf")
        )
        self.vad_mute_until = now + max(tts_seconds, 0.0) + GRACE_S
        self.pre_roll.clear()
        self.vad.reset()
        if self.kws is not None:
            self.kws.reset()
