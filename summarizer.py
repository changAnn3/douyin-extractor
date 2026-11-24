"""调用 MiMo 文本模型生成摘要文案。"""
import random
import time
from typing import Any, Callable, Dict, Optional

import requests

from config import Settings

ProgressCb = Callable[[float, str], None]

SYSTEM_PROMPT = (
    "你是一名资深中文短视频运营编辑，擅长把视频内容提炼成准确、精炼的摘要文案。"
    "你只能依据用户提供的材料撰写，严禁凭空编造或补充材料之外的信息；"
    "材料不足时，就基于现有内容进行概括。"
)

USER_TEMPLATE = """请根据以下素材，为这条抖音视频撰写一段摘要文案。

【标题】
{title}

【作者原始文案】
{caption}

【口播转写】
{transcript}

【画面文字】
{ocr_text}

要求：
1. 正文必须为 100 到 160 个汉字的连贯段落，不足 100 字视为不合格，需要补充具体细节。
2. 采用“总—分”结构：先用一句话概括视频主旨，再用 2 到 3 个要点展开，要点之间用分号连接。
3. 不要使用“本视频”“该视频”等字样，直接描述内容。
4. 不要序号、Markdown 标记、表情符号、话题标签。
5. 只输出摘要正文，不要任何解释或前后缀。
"""


class SummarizerError(Exception):
    """摘要生成失败。"""


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if not text:
        return "（无）"
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _extract_content(payload: Dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        raise SummarizerError("摘要响应缺少 choices 字段。")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts).strip()
    if content is None:
        return ""
    return str(content).strip()


def _build_session(settings: Settings) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    if settings.mimo_proxy:
        session.proxies = {"http": settings.mimo_proxy, "https": settings.mimo_proxy}
    return session


def summarize(title: str, caption: str, transcript: str, ocr_text: str,
              settings: Settings,
              progress_cb: Optional[ProgressCb] = None) -> str:
    """生成摘要文案。失败抛出 SummarizerError。"""
    if not settings.mimo_api_key:
        raise SummarizerError(
            "未配置 MIMO_API_KEY，无法生成摘要。请复制 .env.example 为 .env 并填写 API Key。")

    user_prompt = USER_TEMPLATE.format(
        title=_truncate(title, 200),
        caption=_truncate(caption, 4000),
        transcript=_truncate(transcript, 6000),
        ocr_text=_truncate(ocr_text, 4000),
    )

    url = settings.mimo_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "api-key": settings.mimo_api_key,
        "Content-Type": "application/json",
    }
    body = {
        "model": settings.mimo_text_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 4096,   # mimo-v2.6-flash 为推理模型，reasoning 会占用较多 token
        "stream": False,
    }

    if progress_cb:
        progress_cb(0.2, "正在生成摘要文案…")

    session = _build_session(settings)
    try:
        last_error = ""
        best_text = ""
        length_retry = False
        for attempt in range(4):
            try:
                response = session.post(url, headers=headers, json=body, timeout=(10, 240))
            except requests.exceptions.Timeout:
                last_error = "摘要请求超时。"
                time.sleep(1 + attempt)
                continue
            except requests.exceptions.RequestException as exc:
                last_error = f"网络错误：{exc}"
                time.sleep(1 + attempt)
                continue

            status = response.status_code
            if status in (401, 403):
                raise SummarizerError(
                    "MiMo API Key 无效或无权限，请检查 .env 中的 MIMO_API_KEY。")
            if status == 429 or 500 <= status < 600:
                last_error = f"摘要服务返回 HTTP {status}"
                time.sleep(1 + attempt + random.uniform(0, 0.5))
                continue
            if status >= 400:
                raise SummarizerError(f"摘要请求失败（HTTP {status}）：{response.text[:200]}")

            try:
                data = response.json()
            except ValueError as exc:
                raise SummarizerError(
                    "摘要响应不是合法 JSON：" + response.text[:200]) from exc
            text = _extract_content(data)
            if not text:
                last_error = "摘要模型返回为空，请稍后重试。"
                if attempt < 3:
                    continue
                raise SummarizerError(last_error)

            if len(text) >= len(best_text):
                best_text = text
            # 100-220 字视为合格
            if 100 <= len(text) <= 220:
                if progress_cb:
                    progress_cb(1.0, "摘要生成完成")
                return text
            # 偏短时重新生成一次
            if not length_retry:
                length_retry = True
                if progress_cb:
                    progress_cb(0.6, "摘要偏短，正在重新生成…")
                continue
            break

        if best_text:
            if progress_cb:
                progress_cb(1.0, "摘要生成完成")
            return best_text
        raise SummarizerError("摘要生成失败：" + (last_error or "未知错误"))
    finally:
        session.close()
