"""兼容旧入口：转发到 sherpa_tts。默认请用 TTS_BACKEND=matcha。"""

from sherpa_tts import SherpaTts as VitsTts
from sherpa_tts import build_sherpa_tts_from_env, build_vits_tts_from_env

__all__ = ["VitsTts", "build_sherpa_tts_from_env", "build_vits_tts_from_env"]
