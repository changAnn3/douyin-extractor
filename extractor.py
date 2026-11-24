"""从抖音分享口令/文本中提取链接与作品 ID。"""
import re
from typing import Optional

import requests

# 匹配文本中的任意 http(s) 链接（排除常见中文标点结尾）
_GENERIC_URL_RE = re.compile(r"https?://[^\s<>\"'，。、；！？】【()（）]+", re.IGNORECASE)
# 没有协议时的兜底
_BARE_DOUYIN_RE = re.compile(r"(v\.douyin\.com/[^\s<>\"'，。、；！？】【()（）]+)", re.IGNORECASE)

_TRAILING_PUNCT = "，。、；！？,.!?;:：)]）】\"'"


def _is_douyin_host(url: str) -> bool:
    try:
        host_part = re.match(r"https?://([^/\s]+)", url, re.IGNORECASE)
    except Exception:
        return False
    if not host_part:
        return False
    host = host_part.group(1).lower()
    # 去掉端口
    host = host.rsplit(":", 1)[0] if ":" in host else host
    return host.endswith(".douyin.com") or host.endswith("iesdouyin.com") or host == "douyin.com"


def extract_url(text: str) -> str:
    """从分享口令或文本中提取第一条抖音链接。

    支持：https://v.douyin.com/xxx 、https://www.douyin.com/video/<id>、
    /note/<id> 、iesdouyin.com/share/... 等。
    找不到时抛出 ValueError。
    """
    if not text or not text.strip():
        raise ValueError("请先粘贴抖音分享链接或分享口令。")

    for match in _GENERIC_URL_RE.finditer(text):
        url = match.group(0).rstrip(_TRAILING_PUNCT)
        if _is_douyin_host(url):
            return url

    # 兜底：没有协议但包含 v.douyin.com
    for match in _BARE_DOUYIN_RE.finditer(text):
        url = "https://" + match.group(1).rstrip(_TRAILING_PUNCT)
        return url

    raise ValueError(
        "未找到有效的抖音链接。请粘贴分享链接或分享口令，例如："
        "https://v.douyin.com/xxxxxxx/"
    )


_ID_PATTERNS = (
    re.compile(r"/(?:video|note)/(\d+)"),
    re.compile(r"[?&]modal_id=(\d+)"),
    re.compile(r"[?&]aweme_id=(\d+)"),
)


def extract_aweme_id(url: str) -> Optional[str]:
    """从可识别链接中提取数字作品 ID；短链返回 None（交给 yt-dlp 跟随跳转）。"""
    if not url:
        return None
    for pattern in _ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return None


def canonical_video_url(url: str) -> str:
    """把带 modal_id 的用户页/主页链接转换为 yt-dlp 支持的 /video/<id> 形式。

    短链（无作品 ID）保持原样，交给 yt-dlp 跟随重定向。
    """
    aweme_id = extract_aweme_id(url)
    if aweme_id and "/video/" not in url and "/note/" not in url:
        return f"https://www.douyin.com/video/{aweme_id}"
    return url


_MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 "
              "Mobile/15E148 Safari/604.1")


def resolve_url(url: str, timeout: float = 15.0) -> str:
    """跟随抖音短链重定向，尽量拿到带作品 ID 的最终链接。"""
    if extract_aweme_id(url):
        return url
    if not _is_douyin_host(url):
        return url
    try:
        session = requests.Session()
        session.trust_env = False
        response = session.get(url, allow_redirects=True, timeout=timeout,
                               headers={"User-Agent": _MOBILE_UA})
    except requests.exceptions.RequestException:
        return url

    final = response.url or ""
    if extract_aweme_id(final):
        return final

    html = response.text or ""
    for pattern in (r"/(?:video|note)/(\d+)", r'"awemeId"\s*:\s*["\'](\d+)',
                    r"modal_id=(\d+)"):
        match = re.search(pattern, html)
        if match:
            return f"https://www.douyin.com/video/{match.group(1)}"
    return url
