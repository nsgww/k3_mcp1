"""
为 09_asr_llm_ws.py 边界测试合成标准语音样本，并可选叠加背景噪声增强数据多样性。

运行环境：SpacemiT K1 板端（已安装 sherpa_onnx / spacemit-ort 等依赖）。

用法：
    # 只生成干净样本
    python tests/gen_test_wavs.py

    # 生成干净样本 + 多信噪比噪声样本
    python tests/gen_test_wavs.py --noise --snr-levels 20 15 10 5

    # 使用外部噪声文件
    python tests/gen_test_wavs.py --noise --noise-file /path/to/noise.wav --snr-levels 10 5

    # 只输出噪声样本（不保留干净版）
    python tests/gen_test_wavs.py --noise --noise-only --snr-levels 20 10

输出文件：
    wake_nihao_junge.wav            干净版
    wake_nihao_junge_snr20db.wav    20dB 噪声版
    wake_nihao_junge_snr10db.wav    10dB 噪声版
    ...

注意：
    必须先 import sherpa_onnx，再加载系统 onnxruntime，
    否则会出现 VERS_1.24.2 not found 错误。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import wave
from pathlib import Path
from typing import Iterable

import numpy as np

# 把项目根目录加入 sys.path，以便导入同项目下的 sherpa_tts 等本地模块
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 加载顺序：先 sherpa_onnx，再系统 ORT，与 09_asr_llm_ws.py 保持一致
import sherpa_onnx  # noqa: F401

# 默认使用项目内 Matcha 中英混合 TTS
os.environ.setdefault("TTS_BACKEND", "matcha")
os.environ.setdefault(
    "TTS_MODEL_DIR",
    "/var/lib/sm-sdk/models/tts/matcha-icefall-zh-en",
)

from sherpa_tts import build_sherpa_tts_from_env


SAMPLES = [
    ("wake_nihao_junge.wav", "你好军哥"),
    ("cmd_open_light.wav", "打开办公室灯"),
    ("cmd_close_light.wav", "关闭办公室灯"),
    ("cmd_status.wav", "办公室灯什么状态"),
    ("cmd_chitchat.wav", "今天天气怎么样"),
    ("wake_cmd_concat.wav", "你好军哥打开办公室灯"),
]


# ---------------------------------------------------------------------------
# 噪声生成与混音
# ---------------------------------------------------------------------------
def _load_wav(path: Path) -> tuple[np.ndarray, int]:
    """读取 wav，返回 (float32 单声道样本, sample_rate)。"""
    with wave.open(str(path), "rb") as wf:
        nch = wf.getnchannels()
        sr = wf.getframerate()
        sw = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())
    dtype = np.int16 if sw == 2 else np.int8
    audio = np.frombuffer(frames, dtype=dtype).astype(np.float32)
    if nch == 2:
        audio = audio.reshape(-1, 2).mean(axis=1)
    return audio / 32768.0, sr


def _save_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    """保存 float32 单声道音频为 16bit wav。"""
    clipped = np.clip(audio * 32767, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(clipped.tobytes())


def _white_noise(length: int) -> np.ndarray:
    return np.random.randn(length).astype(np.float32)


def _pink_noise(length: int) -> np.ndarray:
    """Voss-McCartney 算法生成粉红噪声。"""
    rows = 16
    array = np.zeros((rows, length // rows + 1), dtype=np.float32)
    for i in range(rows):
        array[i, :] = np.random.randn(array.shape[1])
    samples = np.cumsum(array, axis=0, dtype=np.float32)
    samples = np.reshape(samples[:-1, :-1], (-1,))
    if len(samples) < length:
        samples = np.pad(samples, (0, length - len(samples)), mode="edge")
    else:
        samples = samples[:length]
    # 归一化
    samples = samples - samples.mean()
    max_val = np.abs(samples).max()
    return samples / max_val if max_val > 0 else samples


def _brown_noise(length: int) -> np.ndarray:
    """积分白噪声得到棕噪声。"""
    white = np.random.randn(length).astype(np.float32)
    brown = np.cumsum(white)
    brown = brown - brown.mean()
    max_val = np.abs(brown).max()
    return brown / max_val if max_val > 0 else brown


def generate_noise(length: int, noise_type: str = "white") -> np.ndarray:
    if noise_type == "white":
        return _white_noise(length)
    if noise_type == "pink":
        return _pink_noise(length)
    if noise_type == "brown":
        return _brown_noise(length)
    raise ValueError(f"不支持的噪声类型: {noise_type}")


def mix_with_noise(
    signal: np.ndarray,
    noise: np.ndarray,
    snr_db: float,
) -> np.ndarray:
    """
    按信噪比（dB）混合信号与噪声。
    SNR = 10 * log10(signal_power / noise_power)
    """
    if len(noise) < len(signal):
        # 噪声太短则循环拼接
        repeats = int(np.ceil(len(signal) / len(noise)))
        noise = np.tile(noise, repeats)
    noise = noise[: len(signal)].astype(np.float32)

    sig_power = np.mean(signal ** 2)
    noise_power = np.mean(noise ** 2)
    if noise_power <= 0:
        return signal

    target_noise_power = sig_power / (10 ** (snr_db / 10.0))
    scale = np.sqrt(target_noise_power / noise_power)
    mixed = signal + noise * scale

    # 柔和限幅，避免削波严重
    peak = np.abs(mixed).max()
    if peak > 1.0:
        mixed = mixed / peak * 0.95
    return mixed


def load_external_noise(path: Path) -> np.ndarray:
    audio, sr = _load_wav(path)
    # 统一重采样到 16k（假设 TTS 输出 16k）
    if sr != 16000:
        from scipy.signal import resample
        new_len = int(len(audio) * 16000 / sr)
        if new_len > 0:
            audio = resample(audio, new_len)
    return audio


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        frames = wf.getnframes()
        sr = wf.getframerate()
        return frames / float(sr) if sr else 0.0


def main():
    ap = argparse.ArgumentParser(description="合成边界测试语音样本（支持加噪）")
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("test_wavs"),
        help="输出目录（默认 test_wavs/）",
    )
    ap.add_argument(
        "--backend",
        choices=["matcha", "vits"],
        default=None,
        help="TTS 后端（默认读取 TTS_BACKEND 环境变量）",
    )
    ap.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="语速（默认 1.0）",
    )
    ap.add_argument(
        "--noise",
        action="store_true",
        help="同时生成带背景噪声的样本",
    )
    ap.add_argument(
        "--noise-only",
        action="store_true",
        help="只生成噪声样本，不保留干净版",
    )
    ap.add_argument(
        "--noise-type",
        choices=["white", "pink", "brown"],
        default="white",
        help="合成噪声类型（默认 white）",
    )
    ap.add_argument(
        "--noise-file",
        type=Path,
        default=None,
        help="外部噪声文件路径（wav），指定后忽略 --noise-type",
    )
    ap.add_argument(
        "--snr-levels",
        type=float,
        nargs="+",
        default=[20, 15, 10, 5],
        help="信噪比列表（dB），默认 20 15 10 5",
    )
    args = ap.parse_args()

    if args.backend:
        os.environ["TTS_BACKEND"] = args.backend
    if args.speed != 1.0:
        os.environ["TTS_SPEED"] = str(args.speed)

    print(f"[TTS] 后端={os.environ.get('TTS_BACKEND', 'matcha')}, 语速={args.speed}")
    tts = build_sherpa_tts_from_env()
    if tts is None:
        print("[ERROR] TTS 初始化失败，请检查模型目录与环境变量", file=sys.stderr)
        sys.exit(1)

    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[OUT] 输出目录: {out_dir.resolve()}")

    external_noise: np.ndarray | None = None
    if args.noise_file:
        if not args.noise_file.is_file():
            print(f"[ERROR] 噪声文件不存在: {args.noise_file}", file=sys.stderr)
            sys.exit(1)
        print(f"[NOISE] 外部噪声: {args.noise_file}")
        external_noise = load_external_noise(args.noise_file)
    elif args.noise:
        print(f"[NOISE] 噪声类型: {args.noise_type}, SNR={args.snr_levels} dB")

    ok = 0
    total = 0
    for filename, text in SAMPLES:
        print(f"\n[SYNTH] {filename}: {text!r}")
        try:
            src = tts.ort_predict(text)
            if not src or not Path(src).is_file():
                print(f"  [FAIL] 合成失败，输出为空")
                continue

            signal, sr = _load_wav(Path(src))
            base = filename.replace(".wav", "")

            # 干净版
            if not args.noise_only:
                dst = out_dir / filename
                shutil.copy2(src, dst)
                dur = wav_duration(dst)
                print(f"  [OK] {dst}  时长={dur:.2f}s")
                ok += 1
                total += 1

            # 噪声版
            if args.noise:
                for snr in args.snr_levels:
                    noise = (
                        external_noise
                        if external_noise is not None
                        else generate_noise(len(signal), args.noise_type)
                    )
                    mixed = mix_with_noise(signal, noise, snr)
                    noisy_name = f"{base}_snr{int(snr)}db.wav"
                    noisy_path = out_dir / noisy_name
                    _save_wav(noisy_path, mixed, sr)
                    dur = wav_duration(noisy_path)
                    print(f"  [OK] {noisy_path}  SNR={snr}dB  时长={dur:.2f}s")
                    ok += 1
                    total += 1
        except Exception as e:
            print(f"  [FAIL] {type(e).__name__}: {e}")

    print(f"\n完成: {ok}/{total} 个文件成功")
    sys.exit(0 if ok == total else 1)


if __name__ == "__main__":
    main()
