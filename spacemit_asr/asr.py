import base64
import io
import os
import time
import warnings
import wave

import numpy as np

from .models.postprocess_utils import rich_transcription_postprocess

# Qwen3-ASR HTTP 服务（llama-multi-asr），由 qwen3asr-server.service 提供
ASR_SERVER_URL = os.environ.get("ASR_SERVER_URL", "http://127.0.0.1:8091")
ASR_PROMPT = os.environ.get("ASR_PROMPT", "language Chinese<asr_text>")
ASR_MAX_TOKENS = int(os.environ.get("ASR_MAX_TOKENS", "256"))
ASR_TIMEOUT = float(os.environ.get("ASR_TIMEOUT", "60"))


def _encode_wav_base64(samples: np.ndarray, sample_rate: int) -> str:
    pcm16 = np.clip(samples, -1.0, 1.0)
    pcm16 = (pcm16 * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16.tobytes())
    return base64.b64encode(buf.getvalue()).decode("ascii")


class ASRModel:
    def __init__(self):
        self._url = ASR_SERVER_URL.rstrip("/")
        print(f"初始化ASR模型，服务: {self._url} (Qwen3-ASR)")
        try:
            import httpx

            r = httpx.get(f"{self._url}/health", timeout=5)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            print("ASR模型初始化成功")
        except Exception as e:
            print(f"ASR服务连接失败（首轮调用时重试）: {e}")

    def _load_samples(self, audio_file, sr):
        """归一化为 float32 单声道采样。"""
        if isinstance(audio_file, np.ndarray):
            if audio_file.dtype == np.int16:
                samples = audio_file.astype(np.float32) / 32768.0
            else:
                samples = audio_file.astype(np.float32)
            return samples, len(audio_file) / sr
        if isinstance(audio_file, str):
            import soundfile as sf
            from speex_resample import resample

            data, file_sr = sf.read(audio_file, dtype="float32")
            if data.ndim > 1:
                data = data.mean(axis=1)
            if file_sr != sr:
                data = resample(data, file_sr, sr)
            return data, len(data) / sr
        warnings.warn(
            f"[ASR] Unsupported type {type(audio_file).__name__}; "
            "expect str or np.ndarray. Skip this turn."
        )
        return None, 0.0

    def generate(self, audio_file, sr=16000):
        samples, audio_dur = self._load_samples(audio_file, sr)
        if samples is None or len(samples) == 0:
            return None

        print(f"开始ASR推理，音频长度: {audio_dur:.3f}s")
        t0 = time.perf_counter()
        try:
            import httpx

            payload = {
                "model": "qwen3-asr",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": _encode_wav_base64(samples, sr),
                                    "format": "wav",
                                },
                            },
                            {"type": "text", "text": ASR_PROMPT},
                        ],
                    }
                ],
                "temperature": 0,
                "max_tokens": ASR_MAX_TOKENS,
            }
            r = httpx.post(
                f"{self._url}/v1/chat/completions",
                json=payload,
                timeout=ASR_TIMEOUT,
            )
            r.raise_for_status()
            raw_text = r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"[ASR] 推理错误详情: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return None
        infer_time = time.perf_counter() - t0
        rtf = infer_time / audio_dur if audio_dur > 0 else float("inf")
        print(f"infer_time: {infer_time:.3f}s, audio_dur: {audio_dur:.3f}s, RTF: {rtf:.2f}")
        # Qwen3-ASR 输出为纯文本（无标签），postprocess 只起清理作用
        text = rich_transcription_postprocess(raw_text)
        return text
