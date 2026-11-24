"""yt-dlp 封装 + 抖音分享页备用下载：下载视频与元数据、进度回调、错误中文归类。"""
import http.cookiejar
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests
import yt_dlp
from yt_dlp.utils import DownloadError

import extractor
from config import Settings

ProgressCb = Callable[[float, str], None]

VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".flv", ".mov", ".m4v", ".ts", ".avi")

# 抖音分享页 SSR 数据只对移动端 UA 返回
_MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 "
              "Mobile/15E148 Safari/604.1")


class DownloaderError(Exception):
    """带用户友好中文提示的下载错误。"""


@dataclass
class DownloadResult:
    aweme_id: str
    url: str
    title: str
    caption: str
    author: str
    duration: float
    video_path: str
    info_json_path: str
    raw_info: Dict[str, Any]
    media_type: str = "video"
    image_paths: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def _find_video_file(directory: Path) -> Optional[Path]:
    for ext in VIDEO_EXTS:
        candidate = directory / ("video" + ext)
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    for candidate in sorted(directory.glob("video.*")):
        if candidate.suffix.lower() in VIDEO_EXTS and candidate.stat().st_size > 0:
            return candidate
    return None


def _build_hook(progress_cb: Optional[ProgressCb]):
    def hook(data: Dict[str, Any]) -> None:
        if not progress_cb:
            return
        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            downloaded = data.get("downloaded_bytes") or 0
            frac = (downloaded / total) if total else 0.0
            progress_cb(min(frac, 1.0), "正在下载视频…")
        elif status == "finished":
            progress_cb(1.0, "下载完成，正在合并音视频…")

    return hook


def _apply_impersonate(opts: Dict[str, Any], settings: Settings) -> None:
    if not settings.impersonate:
        return
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        # 未安装 curl_cffi 时静默去掉，避免 yt-dlp 直接报错
        return
    opts["impersonate"] = settings.impersonate


def _load_cookie_session(settings: Settings) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    if settings.mimo_proxy:
        session.proxies = {"http": settings.mimo_proxy, "https": settings.mimo_proxy}
    if settings.cookies_file:
        path = Path(settings.cookies_file)
        if path.exists():
            try:
                jar = http.cookiejar.MozillaCookieJar(str(path))
                jar.load(ignore_discard=True, ignore_expires=True)
                session.cookies = jar
            except (OSError, http.cookiejar.LoadError):
                pass
    session.headers.update({
        "User-Agent": _MOBILE_UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    return session


def _parse_router_item(html: str) -> Optional[Dict[str, Any]]:
    idx = html.find("window._ROUTER_DATA")
    if idx < 0:
        return None
    start = html.find("{", idx)
    if start < 0:
        return None
    end = html.find("</script>", start)
    raw = html[start:end if end >= 0 else len(html)].strip()
    raw = raw.rstrip(";").strip()
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    loader = (data or {}).get("loaderData") or {}
    for value in loader.values():
        if not isinstance(value, dict):
            continue
        res = value.get("videoInfoRes")
        if isinstance(res, dict):
            items = res.get("item_list") or []
            if items and isinstance(items[0], dict):
                return items[0]
    return None


def _fetch_douyin_item(aweme_id: str, settings: Settings) -> requests.Session:
    """优先请求移动端分享页并重试：首次请求常返回风控挑战页，
    服务器下发新 cookie 后重试即可拿到 _ROUTER_DATA。"""
    session = _load_cookie_session(settings)
    urls = [
        f"https://www.iesdouyin.com/share/video/{aweme_id}/",
        f"https://www.douyin.com/video/{aweme_id}",
    ]
    last_error = ""
    for attempt in range(3):
        for url in urls:
            try:
                response = session.get(url, timeout=30,
                                       headers={"Referer": "https://www.douyin.com/"})
            except requests.exceptions.RequestException as exc:
                last_error = f"网络错误：{exc}"
                continue
            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}"
                continue
            item = _parse_router_item(response.text or "")
            if item:
                return session, item
            last_error = "页面未包含视频数据（可能触发风控验证）"
        if attempt < 2:
            time.sleep(2.5 + attempt)
    raise DownloaderError(
        f"无法获取视频详情（{last_error}）。请确认 cookies 有效，或稍后重试。")


def _copy_cookies_for_ytdlp(settings: Settings) -> Optional[Path]:
    """yt-dlp 会改写 cookiefile；用临时副本避免污染用户导出的 cookies.txt。"""
    if not settings.cookies_file:
        return None
    src = Path(settings.cookies_file)
    if not src.exists():
        return None
    tmp = Path(tempfile.gettempdir()) / f"ytdlp_cookies_{os.getpid()}.txt"
    try:
        shutil.copyfile(src, tmp)
        return tmp
    except OSError:
        return None


def _pick_video_urls(item: Dict[str, Any]) -> list:
    video = item.get("video") or {}
    play = video.get("play_addr") or {}
    uri = play.get("uri")
    urls = []
    if uri:
        urls.append(f"https://aweme.snssdk.com/aweme/v1/play/"
                    f"?video_id={uri}&ratio=1080p&line=0")
    for candidate in play.get("url_list") or []:
        if not isinstance(candidate, str):
            continue
        cleanup = candidate.replace("playwm", "play")
        if cleanup not in urls:
            urls.append(cleanup)
    return urls


def _stream_download(session: requests.Session, src_url: str, dest: str,
                     progress_cb: Optional[ProgressCb]) -> int:
    last_error = ""
    for attempt in range(3):
        try:
            with session.get(src_url, stream=True, timeout=(10, 600),
                             headers={"Referer": "https://www.douyin.com/"},
                             allow_redirects=True) as response:
                if response.status_code != 200:
                    if response.status_code >= 500 and attempt < 2:
                        last_error = f"HTTP {response.status_code}"
                        time.sleep(1 + attempt)
                        continue
                    raise DownloaderError(f"视频下载失败（HTTP {response.status_code}）")
                ctype = (response.headers.get("Content-Type") or "").lower()
                if "video" not in ctype and "octet-stream" not in ctype:
                    raise DownloaderError("视频地址返回了非视频内容，链接可能已失效。")
                total = int(response.headers.get("Content-Length") or 0)
                done = 0
                with open(dest, "wb") as handle:
                    for chunk in response.iter_content(64 * 1024):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        done += len(chunk)
                        if progress_cb and total:
                            progress_cb(min(done / total, 1.0),
                                        f"正在下载视频（{done * 100 // total}%）")
                if done <= 0:
                    raise DownloaderError("下载到的视频为空。")
                if progress_cb:
                    progress_cb(1.0, "视频下载完成")
                return done
        except requests.exceptions.RequestException as exc:
            last_error = f"网络错误：{exc}"
            if attempt < 2:
                time.sleep(1 + attempt)
                continue
            raise DownloaderError(last_error + "（已重试 3 次）") from exc
    raise DownloaderError(last_error or "视频下载失败。")


def _pick_image_urls(item: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    for image in item.get("images") or []:
        if isinstance(image, str) and image:
            urls.append(image)
            continue
        if not isinstance(image, dict):
            continue
        for key in ("url_list", "download_url_list", "urlList"):
            candidates = image.get(key)
            if isinstance(candidates, list):
                chosen = next((u for u in candidates if isinstance(u, str) and u), None)
                if chosen:
                    urls.append(chosen)
                    break
    return urls


_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")


def _image_suffix(url: str, content_type: str) -> str:
    ctype = (content_type or "").lower()
    if "png" in ctype:
        return ".png"
    if "webp" in ctype:
        return ".webp"
    if "gif" in ctype:
        return ".gif"
    if "avif" in ctype:
        return ".avif"
    if "jpeg" in ctype or "jpg" in ctype:
        return ".jpg"
    path = (url or "").split("?", 1)[0].lower()
    for suffix in _IMAGE_SUFFIXES:
        if path.endswith(suffix):
            return ".jpg" if suffix == ".jpeg" else suffix
    return ".jpg"


def _save_image(session: requests.Session, image_url: str, image_dir: Path,
                index: int) -> Optional[str]:
    for attempt in range(2):
        try:
            response = session.get(image_url, stream=True, timeout=(10, 180),
                                   headers={"Referer": "https://www.douyin.com/"},
                                   allow_redirects=True)
            if response.status_code != 200:
                if attempt == 0 and response.status_code >= 500:
                    time.sleep(1)
                    continue
                return None
            ctype = (response.headers.get("Content-Type") or "").lower()
            if "image" not in ctype:
                return None
            dest = image_dir / ("img_%03d%s" % (index, _image_suffix(image_url, ctype)))
            size = 0
            with open(dest, "wb") as handle:
                for chunk in response.iter_content(64 * 1024):
                    if chunk:
                        handle.write(chunk)
                        size += len(chunk)
            if size <= 0:
                try:
                    dest.unlink()
                except OSError:
                    pass
                if attempt == 0:
                    continue
                return None
            return str(dest)
        except requests.exceptions.RequestException:
            if attempt == 0:
                time.sleep(1)
                continue
            return None
    return None


def _download_images(session: requests.Session, item: Dict[str, Any], settings: Settings,
                     url: str, aweme_id: str,
                     progress_cb: Optional[ProgressCb]) -> DownloadResult:
    image_urls = _pick_image_urls(item)
    if not image_urls:
        raise DownloaderError("未找到图文作品的图片地址，作品可能不可访问。")

    workdir = Path(settings.output_dir) / aweme_id
    image_dir = workdir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)

    warnings: List[str] = []
    saved: List[str] = []
    failures = 0
    total = len(image_urls)
    for index, image_url in enumerate(image_urls, start=1):
        if progress_cb:
            progress_cb((index - 1) / total, f"正在下载图片（{index}/{total}）")
        saved_path = _save_image(session, image_url, image_dir, index)
        if saved_path:
            saved.append(saved_path)
        else:
            failures += 1

    if not saved:
        raise DownloaderError("图文作品图片下载失败，请稍后重试。")
    if failures:
        warnings.append(f"有 {failures} 张图片下载失败，已跳过。")
    if progress_cb:
        progress_cb(1.0, "图片下载完成")

    desc = (item.get("desc") or "").strip()
    first_line = desc.splitlines()[0].strip() if desc else ""
    title = first_line[:80] if first_line else f"抖音图文 {aweme_id}"
    author = ((item.get("author") or {}).get("nickname") or "").strip()

    info_json_path = workdir / "video.info.json"
    try:
        info_json_path.write_text(json.dumps({
            "id": aweme_id,
            "media_type": "images",
            "title": title,
            "description": desc,
            "uploader": author,
            "image_urls": image_urls,
            "images": saved,
            "webpage_url": url,
            "raw": item,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass

    return DownloadResult(
        aweme_id=aweme_id,
        url=url,
        title=title,
        caption=desc,
        author=author,
        duration=0.0,
        video_path="",
        info_json_path=str(info_json_path),
        raw_info=item,
        media_type="images",
        image_paths=saved,
        warnings=warnings,
    )


def _download_via_share_page(url: str, aweme_id: str, settings: Settings,
                             progress_cb: Optional[ProgressCb]) -> DownloadResult:
    """通过分享页 SSR 数据下载（用于 yt-dlp detail 接口被风控拦截时）。"""
    session, item = _fetch_douyin_item(aweme_id, settings)

    if item.get("images"):
        return _download_images(session, item, settings, url, aweme_id, progress_cb)

    video = item.get("video") or {}
    urls = _pick_video_urls(item)
    if not urls:
        raise DownloaderError("未找到可用的视频地址，作品可能不可访问。")

    workdir = Path(settings.output_dir) / aweme_id
    workdir.mkdir(parents=True, exist_ok=True)
    dest = workdir / "video.mp4"

    chosen_url = ""
    last_error = ""
    downloaded = 0
    for candidate in urls:
        try:
            downloaded = _stream_download(session, candidate, str(dest), progress_cb)
            chosen_url = candidate
            break
        except DownloaderError as exc:
            last_error = str(exc)
        except requests.exceptions.RequestException as exc:
            last_error = f"网络错误：{exc}"
    if downloaded <= 0:
        raise DownloaderError("视频下载失败：" + (last_error or "未知错误"))

    desc = (item.get("desc") or "").strip()
    first_line = desc.splitlines()[0].strip() if desc else ""
    title = first_line[:80] if first_line else f"抖音作品 {aweme_id}"
    author = ((item.get("author") or {}).get("nickname") or "").strip()
    try:
        duration = float(video.get("duration") or 0) / 1000.0
    except (TypeError, ValueError):
        duration = 0.0

    info_json_path = workdir / "video.info.json"
    try:
        info_json_path.write_text(json.dumps({
            "id": aweme_id,
            "title": title,
            "description": desc,
            "uploader": author,
            "duration": duration,
            "webpage_url": url,
            "download_url": chosen_url,
            "raw": item,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass

    return DownloadResult(
        aweme_id=aweme_id,
        url=url,
        title=title,
        caption=desc,
        author=author,
        duration=duration,
        video_path=str(dest),
        info_json_path=str(info_json_path),
        raw_info=item,
    )


def download_video(url: str, settings: Settings,
                   progress_cb: Optional[ProgressCb] = None) -> DownloadResult:
    """下载抖音视频并返回元数据。失败时抛出 DownloaderError。"""
    output_root = Path(settings.output_dir)
    output_root.mkdir(parents=True, exist_ok=True)

    ydl_opts: Dict[str, Any] = {
        "outtmpl": str(output_root / "%(id)s" / "video.%(ext)s"),
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "writeinfojson": True,
        "noplaylist": True,
        "quiet": True,
        "noprogress": True,
        "socket_timeout": 30,
        "retries": 5,
        "ffmpeg_location": settings.ffmpeg_bin,
        "proxy": settings.mimo_proxy or "",
        "progress_hooks": [_build_hook(progress_cb)],
    }
    tmp_cookie = None
    if settings.cookies_from_browser:
        ydl_opts["cookiesfrombrowser"] = (settings.cookies_from_browser,)
    else:
        # yt-dlp 退出时会改写 cookiefile，使用临时副本避免污染原始 cookies.txt
        tmp_cookie = _copy_cookies_for_ytdlp(settings)
        if tmp_cookie:
            ydl_opts["cookiefile"] = str(tmp_cookie)
        elif settings.cookies_file:
            ydl_opts["cookiefile"] = settings.cookies_file

    _apply_impersonate(ydl_opts, settings)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadError as exc:
        # 抖音 web detail 接口已要求签名，yt-dlp 常返回 403；改用分享页 SSR 数据下载
        aweme_id = extractor.extract_aweme_id(url)
        if aweme_id:
            try:
                return _download_via_share_page(url, aweme_id, settings, progress_cb)
            except DownloaderError:
                raise
            except Exception as inner:  # 防御性兜底
                raise DownloaderError(friendly_error(inner)) from exc
        raise DownloaderError(friendly_error(exc)) from exc
    finally:
        if tmp_cookie:
            try:
                Path(tmp_cookie).unlink()
            except OSError:
                pass

    if not info:
        raise DownloaderError("未能获取视频信息，请确认链接是否有效。")

    # noplaylist 已启用，但保险起见处理 entries
    if info.get("_type") == "playlist" and info.get("entries"):
        info = info["entries"][0]

    aweme_id = str(info.get("id") or "").strip()
    if not aweme_id:
        raise DownloaderError("未能识别视频作品 ID，请更换链接重试。")

    workdir = output_root / aweme_id
    workdir.mkdir(parents=True, exist_ok=True)

    video_path = ""
    requested = info.get("requested_downloads") or []
    if requested and requested[0].get("filepath"):
        candidate = Path(requested[0]["filepath"])
        if candidate.exists():
            video_path = str(candidate)
    if not video_path:
        found = _find_video_file(workdir)
        if found:
            video_path = str(found)

    if not video_path:
        # 区分图文作品与其他情况
        if info.get("thumbnails") and not info.get("formats"):
            raise DownloaderError("该作品为图文作品，暂不支持，请提供视频作品链接。")
        raise DownloaderError("未找到已下载的视频文件，下载可能失败，请稍后重试。")

    info_json_path = str(workdir / "video.info.json")

    caption = (info.get("description") or "").strip()
    raw_title = (info.get("title") or "").strip()
    if not raw_title:
        first_line = caption.splitlines()[0].strip() if caption else ""
        raw_title = first_line[:80] if first_line else f"抖音作品 {aweme_id}"

    author = (info.get("uploader") or info.get("creator")
              or info.get("channel") or "").strip()

    try:
        duration = float(info.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0

    return DownloadResult(
        aweme_id=aweme_id,
        url=url,
        title=raw_title,
        caption=caption,
        author=author,
        duration=duration,
        video_path=video_path,
        info_json_path=info_json_path,
        raw_info=info,
    )


def friendly_error(exc: Exception) -> str:
    """把 yt-dlp / 网络异常转换为中文提示。"""
    if isinstance(exc, DownloaderError):
        return str(exc)

    msg = str(exc)
    low = msg.lower()

    if "cookies" in low or "sign in" in low or "login" in low or "log in" in low:
        return ("下载需要登录信息：请在 .env 中设置 COOKIES_FILE（浏览器扩展导出的 cookies.txt），"
                "或设置 COOKIES_FROM_BROWSER=chrome/edge（需先关闭浏览器），"
                "并确保对应浏览器已登录抖音。")
    if "unsupported url" in low or "no suitable extractor" in low:
        return ("暂不支持该链接：目前仅支持抖音视频或图文作品，"
                "请粘贴 v.douyin.com 分享链接或分享口令。")
    if ("private" in low or "not available" in low or "removed" in low
            or "does not exist" in low or "unavailable" in low
            or "不存在" in msg or "已删除" in msg):
        return ("视频不可访问：作品可能已删除、设为私密或仅登录可见，"
                "请确认链接是否有效。")
    if ("http error 401" in low or "http error 403" in low
            or "requires authentication" in low or "forbidden" in low):
        return ("抖音拒绝了本次下载（可能触发了风控）：请稍后重试，"
                "或在 .env 中配置浏览器 cookies 后再试。")
    if ("proxy" in low or "timed out" in low or "timeout" in low
            or "connection" in low or "network" in low
            or "getaddrinfo" in low or "ssl" in low
            or "failed to resolve" in low):
        return ("网络异常：无法连接到抖音服务器。请检查网络；"
                "如需代理，请在 .env 中设置 MIMO_PROXY；否则请保持直连。")
    return "下载失败：" + msg.strip()[:300]
