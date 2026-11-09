"""
sherpa-onnx OfflineTts 封装（Matcha / VITS）。

默认引擎: matcha（中英混合 matcha-icefall-zh-en）
环境变量:
  TTS_BACKEND     matcha | vits（默认 matcha）
  TTS_MODEL_DIR   模型目录
  TTS_SID         说话人（VITS 用；Matcha 通常 0）
  TTS_SPEED       语速（默认 1.0）
  TTS_PROVIDER    cpu
  TTS_NUM_THREADS 默认 2
  TTS_VOCODER     Matcha vocoder 路径（默认 <model_dir>/vocos-16khz-univ.onnx）
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional, Union

import numpy as np

_ROOT = Path(__file__).resolve().parent
_DEFAULT_MATCHA = Path("/var/lib/sm-sdk/models/tts/matcha-icefall-zh-en")
_DEFAULT_VITS = _ROOT / "vits-zh-aishell3"


def _rule_fsts(model_dir: Path, names: tuple[str, ...]) -> str:
    parts = []
    for name in names:
        p = model_dir / name
        if p.is_file():
            parts.append(str(p))
    return ",".join(parts)


def _pick_vits_model(model_dir: Path, use_fp32: bool) -> Path:
    int8 = model_dir / "vits-aishell3.int8.onnx"
    fp32 = model_dir / "vits-aishell3.onnx"
    alt = model_dir / "model.onnx"
    order = (fp32, alt, int8) if use_fp32 else (int8, fp32, alt)
    for p in order:
        if p.is_file():
            return p
    raise FileNotFoundError(f"VITS 模型不存在于 {model_dir}")


def _generate(tts, text: str, sid: int, speed: float):
    try:
        import sherpa_onnx

        if hasattr(sherpa_onnx, "GenerationConfig"):
            gen = sherpa_onnx.GenerationConfig()
            gen.sid = sid
            gen.speed = speed
            return tts.generate(text, gen)
    except TypeError:
        pass
    return tts.generate(text, sid=sid, speed=speed)


class SherpaTts:
    """接口对齐旧 Melo：ort_predict(text) -> wav 路径。"""

    def __init__(
        self,
        backend: str = "matcha",
        model_dir: Optional[Union[str, Path]] = None,
        *,
        sid: int = 0,
        speed: float = 1.0,
        provider: str = "cpu",
        num_threads: int = 2,
        vocoder: Optional[str] = None,
        use_fp32: bool = False,
    ):
        import sherpa_onnx
        import soundfile as sf

        self._sf = sf
        self.backend = (backend or "matcha").strip().lower()
        self.sid = int(sid)
        self.speed = float(speed)

        if self.backend == "matcha":
            self.model_dir = Path(model_dir or _DEFAULT_MATCHA)
            self._tts = self._build_matcha(
                sherpa_onnx, self.model_dir, provider, num_threads, vocoder
            )
        elif self.backend == "vits":
            self.model_dir = Path(model_dir or _DEFAULT_VITS)
            self._tts = self._build_vits(
                sherpa_onnx, self.model_dir, provider, num_threads, use_fp32
            )
        else:
            raise ValueError(f"未知 TTS_BACKEND={backend!r}，请用 matcha 或 vits")

        self.sample_rate = int(getattr(self._tts, "sample_rate", 16000) or 16000)
        print(
            f"[TTS] ready ({self.backend}) dir={self.model_dir.name} "
            f"sr={self.sample_rate} sid={self.sid}"
        )

    def _build_matcha(self, sherpa_onnx, model_dir: Path, provider, num_threads, vocoder):
        if not model_dir.is_dir():
            raise FileNotFoundError(f"TTS 目录不存在: {model_dir}")

        acoustic = model_dir / "model-steps-3.onnx"
        if not acoustic.is_file():
            # 兼容 steps-6 等
            cands = sorted(model_dir.glob("model-steps-*.onnx"))
            if not cands:
                raise FileNotFoundError(f"缺少 Matcha 声学模型: {acoustic}")
            acoustic = cands[0]

        voc_path = Path(
            vocoder
            or os.getenv("TTS_VOCODER", "")
            or (model_dir / "vocos-16khz-univ.onnx")
        )
        if not voc_path.is_file():
            # 兼容量化版 vocos-16khz-univ.q.onnx 等
            cands = sorted(model_dir.glob("vocos-*.onnx"))
            if cands:
                voc_path = cands[0]
            else:
                raise FileNotFoundError(
                    f"缺少 Matcha vocoder: {voc_path}\n"
                    "请下载: https://github.com/k2-fsa/sherpa-onnx/releases/download/"
                    "vocoder-models/vocos-16khz-univ.onnx\n"
                    f"放到: {model_dir / 'vocos-16khz-univ.onnx'}"
                )

        lexicon = model_dir / "lexicon.txt"
        tokens = model_dir / "tokens.txt"
        data_dir = model_dir / "espeak-ng-data"
        for p in (lexicon, tokens, data_dir):
            if not (p.is_file() or p.is_dir()):
                raise FileNotFoundError(f"TTS 缺少: {p}")

        rule_fsts = _rule_fsts(
            model_dir, ("phone-zh.fst", "date-zh.fst", "number-zh.fst")
        )
        print(
            f"[TTS] matcha acoustic={acoustic.name} vocoder={voc_path.name} "
            f"rules={bool(rule_fsts)}"
        )

        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                matcha=sherpa_onnx.OfflineTtsMatchaModelConfig(
                    acoustic_model=str(acoustic),
                    vocoder=str(voc_path),
                    lexicon=str(lexicon),
                    tokens=str(tokens),
                    data_dir=str(data_dir),
                ),
                provider=provider,
                debug=False,
                num_threads=num_threads,
            ),
            rule_fsts=rule_fsts,
            max_num_sentences=2,
        )
        if hasattr(cfg, "validate") and not cfg.validate():
            raise ValueError("OfflineTtsConfig.validate() failed (matcha)")
        return sherpa_onnx.OfflineTts(cfg)

    def _build_vits(self, sherpa_onnx, model_dir: Path, provider, num_threads, use_fp32):
        if not model_dir.is_dir():
            raise FileNotFoundError(f"TTS 目录不存在: {model_dir}")
        model = _pick_vits_model(model_dir, use_fp32=use_fp32)
        lexicon = model_dir / "lexicon.txt"
        tokens = model_dir / "tokens.txt"
        for p in (lexicon, tokens):
            if not p.is_file():
                raise FileNotFoundError(f"TTS 缺少: {p}")
        rule_fsts = _rule_fsts(model_dir, ("phone.fst", "date.fst", "number.fst"))
        print(f"[TTS] vits model={model.name}")

        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=str(model),
                    lexicon=str(lexicon),
                    tokens=str(tokens),
                ),
                provider=provider,
                debug=False,
                num_threads=num_threads,
            ),
            rule_fsts=rule_fsts,
            max_num_sentences=2,
        )
        if hasattr(cfg, "validate") and not cfg.validate():
            raise ValueError("OfflineTtsConfig.validate() failed (vits)")
        return sherpa_onnx.OfflineTts(cfg)

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        text = (text or "").strip()
        if not text:
            return np.array([], dtype=np.float32), self.sample_rate
        audio = _generate(self._tts, text, self.sid, self.speed)
        samples = np.asarray(audio.samples, dtype=np.float32)
        sr = int(getattr(audio, "sample_rate", self.sample_rate) or self.sample_rate)
        if samples.size == 0:
            raise RuntimeError("TTS 合成结果为空")
        return samples, sr

    def ort_predict(self, text: str) -> str:
        samples, sr = self.synthesize(text)
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        self._sf.write(path, samples, sr)
        print(f"[TTS] Save to {path}")
        return path


def build_sherpa_tts_from_env() -> Optional[SherpaTts]:
    enabled = os.getenv("TTS_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }
    if not enabled:
        print("[TTS] disabled by TTS_ENABLED")
        return None

    backend = os.getenv("TTS_BACKEND", "matcha").strip().lower() or "matcha"
    default_dir = _DEFAULT_MATCHA if backend == "matcha" else _DEFAULT_VITS
    sid_default = "0" if backend == "matcha" else "21"

    try:
        return SherpaTts(
            backend=backend,
            model_dir=os.getenv("TTS_MODEL_DIR") or str(default_dir),
            sid=int(os.getenv("TTS_SID", sid_default)),
            speed=float(os.getenv("TTS_SPEED", "1.0")),
            provider=os.getenv("TTS_PROVIDER", "cpu"),
            num_threads=int(os.getenv("TTS_NUM_THREADS", "2")),
            vocoder=os.getenv("TTS_VOCODER") or None,
            use_fp32=os.getenv("TTS_USE_FP32", "0").strip().lower()
            in {"1", "true", "on", "yes"},
        )
    except Exception as e:
        print(f"[TTS] 初始化失败: {type(e).__name__}: {e}")
        err = str(e)
        if "VERS_" in err or "onnxruntime" in err.lower():
            print(
                "[TTS] 提示: 请先成功加载 KWS(sherpa)，再初始化 TTS；"
                "不要在 import sherpa_onnx 之前加载系统 onnxruntime。"
            )
        return None


# 兼容旧 import
def build_vits_tts_from_env() -> Optional[SherpaTts]:
    os.environ.setdefault("TTS_BACKEND", "vits")
    return build_sherpa_tts_from_env()
