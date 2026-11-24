"""全局配置：解析 .env、构建 Settings 单例、清理代理环境变量。"""
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

BASE_DIR = Path(__file__).resolve().parent

# Windows 系统里可能设置了不可用的系统代理，pip/requests 会读取它。
# 为避免请求被错误地导向代理，导入本模块时清空代理相关环境变量。
_PROXY_ENV_KEYS = (
    "HTTP_PROXY", "http_proxy",
    "HTTPS_PROXY", "https_proxy",
    "ALL_PROXY", "all_proxy",
    "FTP_PROXY", "ftp_proxy",
    "SOCKS_PROXY", "socks_proxy",
)
for _key in _PROXY_ENV_KEYS:
    os.environ.pop(_key, None)

# 明确禁止代理绕过本机/外网请求，避免 Windows 注册表代理影响 Gradio 本地可用性检查
os.environ["NO_PROXY"] = os.environ.get("NO_PROXY") or "*"
os.environ["no_proxy"] = os.environ.get("no_proxy") or "*"


def parse_env_file(path: Path) -> Dict[str, str]:
    """解析简易 .env 文件：支持 KEY=VALUE、引号、# 注释。"""
    result: Dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return result

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        elif " #" in value:
            # 去掉行尾注释（要求 # 前有空白）
            value = value.split(" #", 1)[0].strip()
        result[key] = value
    return result


def _resolve_bin(configured: str, name: str) -> str:
    """如果配置的路径不存在，尝试从 PATH 中查找同名可执行文件。"""
    if configured and Path(configured).exists():
        return configured
    found = shutil.which(name)
    return found or configured


def _as_optional(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _as_int(value: Optional[str], default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_float(value: Optional[str], default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    """运行配置。字段均可通过 .env 或环境变量覆盖。"""

    mimo_api_key: str = ""
    mimo_base_url: str = "https://api.xiaomimimo.com/v1"
    mimo_asr_model: str = "mimo-v2.5-asr"
    mimo_text_model: str = "mimo-v2.6-flash"

    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    output_dir: str = str(BASE_DIR / "output")

    asr_chunk_seconds: int = 180
    asr_max_base64_bytes: int = 10 * 1024 * 1024
    ocr_max_frames: int = 120
    ocr_min_score: float = 0.5

    cookies_from_browser: Optional[str] = None
    cookies_file: Optional[str] = None
    mimo_proxy: Optional[str] = None

    # 下载时是否请求模拟浏览器指纹（需要 curl_cffi，未安装会自动忽略）
    impersonate: Optional[str] = None

    @property
    def ffmpeg_location(self) -> str:
        """yt-dlp 使用的 ffmpeg 位置（目录）。"""
        return str(Path(self.ffmpeg_bin).parent)


def _env_value(env: Dict[str, str], key: str, default: Optional[str] = None) -> Optional[str]:
    """真实环境变量优先，其次 .env，最后默认值。"""
    value = os.environ.get(key)
    if value is not None and value != "":
        return value
    value = env.get(key)
    if value is not None and value != "":
        return value
    return default


def load_settings(env_path: Optional[Path] = None) -> Settings:
    """从 .env / 环境变量构建 Settings。"""
    if env_path is None:
        env_path = BASE_DIR / ".env"
    env = parse_env_file(env_path)

    ffmpeg_bin = _env_value(env, "FFMPEG_BIN", "ffmpeg")
    ffprobe_bin = _env_value(env, "FFPROBE_BIN", "ffprobe")

    return Settings(
        mimo_api_key=(_env_value(env, "MIMO_API_KEY", "") or "").strip(),
        mimo_base_url=(_env_value(env, "MIMO_BASE_URL",
                                  "https://api.xiaomimimo.com/v1") or "").rstrip("/"),
        mimo_asr_model=_env_value(env, "MIMO_ASR_MODEL", "mimo-v2.5-asr"),
        mimo_text_model=_env_value(env, "MIMO_TEXT_MODEL", "mimo-v2.6-flash"),
        ffmpeg_bin=_resolve_bin(ffmpeg_bin, "ffmpeg"),
        ffprobe_bin=_resolve_bin(ffprobe_bin, "ffprobe"),
        output_dir=str(_env_value(env, "OUTPUT_DIR", str(BASE_DIR / "output"))),
        asr_chunk_seconds=_as_int(_env_value(env, "ASR_CHUNK_SECONDS"), 180),
        asr_max_base64_bytes=_as_int(_env_value(env, "ASR_MAX_BASE64_BYTES"), 10 * 1024 * 1024),
        ocr_max_frames=_as_int(_env_value(env, "OCR_MAX_FRAMES"), 120),
        ocr_min_score=_as_float(_env_value(env, "OCR_MIN_SCORE"), 0.5),
        cookies_from_browser=_as_optional(_env_value(env, "COOKIES_FROM_BROWSER")),
        cookies_file=_as_optional(_env_value(env, "COOKIES_FILE")),
        mimo_proxy=_as_optional(_env_value(env, "MIMO_PROXY")),
        impersonate=_as_optional(_env_value(env, "IMPERSONATE")),
    )


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """获取全局配置单例。"""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reload_settings() -> Settings:
    """重新加载配置（例如修改 .env 后）。"""
    global _settings
    _settings = load_settings()
    return _settings
