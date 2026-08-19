from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .models import ChatResponse

logger = logging.getLogger(__name__)

# 图片后缀（按常见程度排序）
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico")

# Markdown 图片语法: ![alt](url)
MARKDOWN_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)")

# HTML img 标签: <img src="url"> 或 <img src='url'>
HTML_IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)

# 裸 URL（以图片后缀结尾的 http(s) 链接）
BARE_IMG_RE = re.compile(
    r'(?<!["\'\(\[/])(https?://[^\s<>"\')\]]+?\.(?:png|jpg|jpeg|gif|webp|svg|bmp|ico))(?:[?#\s][^\s<>"\')\]]*)?',
    re.IGNORECASE,
)

# 引用标记：[1]、[2]、[12] 等，用于删除 answer 中的引用编号。
# 用前后断言避免误删 markdown 链接文本 [1](url) 与图片 alt ![1](url) 语法。
REFERENCE_MARK_RE = re.compile(r"(?<!!)\[\d+\](?!\()")


def strip_reference_marks(text: str) -> str:
    """删除文本中的 [数字] 引用标记。

    仅删除独立出现的引用编号（如句尾的 [1]、[2]），
    保留 markdown 链接 [1](url) 与图片 ![1](url) 不被破坏。
    """
    if not text:
        return text
    return REFERENCE_MARK_RE.sub("", text)

# 下载图片时使用的请求头，绕过 Referer 防盗链
DEFAULT_DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
}


class ImageCache:
    """图片缓存处理器：从文本中提取图片 URL，下载到本地，替换为本地服务地址。"""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        cache_dir: str,
        base_url: str,
        referer: str | None = None,
    ):
        self.client = http_client
        self.cache_dir = Path(cache_dir)
        self.base_url = base_url.rstrip("/")
        self.referer = referer
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # URL 提取
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_image_urls(text: str) -> set[str]:
        """从文本中提取所有图片 URL（markdown、HTML img、裸 URL）。"""
        urls: set[str] = set()
        for match in MARKDOWN_IMG_RE.finditer(text):
            urls.add(match.group(2))
        for match in HTML_IMG_RE.finditer(text):
            urls.add(match.group(1))
        for match in BARE_IMG_RE.finditer(text):
            urls.add(match.group(1))
        return urls

    # ------------------------------------------------------------------
    # 文件命名与路径
    # ------------------------------------------------------------------

    def _local_path_for(self, url: str) -> tuple[str, str]:
        """根据 URL 生成本地文件名和完整路径。

        使用 URL 的 SHA-256 前 16 位作为文件名，避免路径穿越和重复下载。
        尽量从 URL 路径中提取文件后缀。
        """
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
        parsed = urlparse(url)
        path = parsed.path.lower()
        ext = ""
        for known_ext in IMAGE_EXTENSIONS:
            if path.endswith(known_ext):
                ext = known_ext
                break
        if not ext:
            ext = ".png"
        filename = f"{url_hash}{ext}"
        return filename, str(self.cache_dir / filename)

    def _local_url_for(self, filename: str) -> str:
        """生成对外的本地服务 URL。"""
        return f"{self.base_url}/{filename}"

    # ------------------------------------------------------------------
    # 图片下载
    # ------------------------------------------------------------------

    async def _download_image(self, url: str) -> str | None:
        """下载单张图片到本地，返回文件名；已缓存则直接返回；失败返回 None。"""
        filename, filepath = self._local_path_for(url)
        if os.path.exists(filepath):
            return filename

        headers = dict(DEFAULT_DOWNLOAD_HEADERS)
        if self.referer:
            headers["Referer"] = self.referer

        try:
            response = await self.client.get(url, headers=headers, follow_redirects=True)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            # 宽松校验：要么声明为图片，要么二进制内容非空
            if content_type and not content_type.startswith("image/"):
                # 某些 CDN 不返回 content-type，只要内容非空也接受
                if not response.content:
                    return None
            with open(filepath, "wb") as f:
                f.write(response.content)
            logger.info("cached image: %s -> %s", url, filename)
            return filename
        except Exception as exc:
            logger.warning("failed to cache image %s: %s", url, exc)
            return None

    # ------------------------------------------------------------------
    # 文本替换
    # ------------------------------------------------------------------

    async def process_text(self, text: str) -> str:
        """提取文本中的图片 URL，下载缓存，并将原始 URL 替换为本地地址。"""
        if not text:
            return text

        urls = self._extract_image_urls(text)
        if not urls:
            return text

        # 并发下载所有图片
        results = await asyncio.gather(
            *(self._download_image(url) for url in urls)
        )

        # 构建原始 URL -> 本地 URL 的映射
        url_map: dict[str, str] = {}
        for url, result in zip(urls, results):
            if result:
                url_map[url] = self._local_url_for(result)

        if not url_map:
            return text

        # 依次替换：先 markdown，再 HTML img，最后裸 URL
        def replace_markdown(match: re.Match[str]) -> str:
            alt, url = match.group(1), match.group(2)
            local = url_map.get(url)
            return f"![{alt}]({local})" if local else match.group(0)

        def replace_html(match: re.Match[str]) -> str:
            url = match.group(1)
            local = url_map.get(url)
            if local:
                return match.group(0).replace(url, local)
            return match.group(0)

        text = MARKDOWN_IMG_RE.sub(replace_markdown, text)
        text = HTML_IMG_RE.sub(replace_html, text)
        for original, local in url_map.items():
            text = text.replace(original, local)

        return text

    # ------------------------------------------------------------------
    # 响应处理
    # ------------------------------------------------------------------

    async def _process_value(self, value: Any) -> Any:
        """递归处理任意嵌套结构中的字符串字段，缓存其中的图片 URL。"""
        if isinstance(value, str):
            return await self.process_text(value)
        if isinstance(value, list):
            return [await self._process_value(item) for item in value]
        if isinstance(value, dict):
            return {key: await self._process_value(item) for key, item in value.items()}
        return value

    async def process_response(
        self,
        response: ChatResponse,
        process_answer: bool = True,
        process_additional: bool = True,
    ) -> ChatResponse:
        """处理 ChatResponse 中的图片 URL，answer 与 additional_content 可分开控制。

        - process_answer=True：处理 answer / reasoning_content 正文中的图片
        - process_additional=True：处理 additional_content（任意嵌套结构，如
          reference_chunks[].content 中的 markdown 图片）
        - answer 中的 [数字] 引用标记始终会被清理
        """
        if process_answer:
            if response.answer:
                response.answer = strip_reference_marks(await self.process_text(response.answer))
            if response.reasoning_content:
                response.reasoning_content = await self.process_text(response.reasoning_content)
        if process_additional and response.additional_content:
            response.additional_content = await self._process_value(response.additional_content)
        return response

    async def process_stream_lines(
        self,
        lines: list[str],
        process_answer: bool = True,
        process_additional: bool = True,
    ) -> list[str]:
        """逐行解析 SSE 事件，按开关分别处理 answer 与 additional_content 中的图片。

        - answer 域：将所有事件的 content 片段拼接后提取图片 URL（跨块也能识别），
          下载后对各事件行内的 content 做替换
        - additional 域：对每个事件中的 additional_content 递归处理
        - 无变化的行原样保留，避免破坏 SSE 格式
        """
        # 解析所有 data 事件，收集 answer 域的 content 片段
        events: list[dict | None] = []
        answer_texts: list[str] = []
        for line in lines:
            if line.startswith("data:"):
                try:
                    event = json.loads(line[5:])
                except json.JSONDecodeError:
                    event = None
                if isinstance(event, dict):
                    events.append(event)
                    if isinstance(event.get("content"), str):
                        answer_texts.append(event["content"])
                    continue
            events.append(None)

        # answer 域：拼接提取 URL（可跨块识别）并并发下载
        answer_url_map: dict[str, str] = {}
        if process_answer and answer_texts:
            joined = "\n".join(answer_texts)
            urls = self._extract_image_urls(joined)
            if urls:
                results = await asyncio.gather(*(self._download_image(url) for url in urls))
                for url, result in zip(urls, results):
                    if result:
                        answer_url_map[url] = self._local_url_for(result)

        # 逐行重建：替换有变化的事件行
        new_lines: list[str] = []
        for index, line in enumerate(lines):
            event = events[index]
            if event is None:
                new_lines.append(line)
                continue
            changed = False
            # 引用标记清理：只要事件带 content 就执行，与图片缓存开关无关
            if isinstance(event.get("content"), str):
                content = event["content"]
                if process_answer:
                    for original, local in answer_url_map.items():
                        if original in content:
                            content = content.replace(original, local)
                            changed = True
                content = strip_reference_marks(content)
                if content != event["content"]:
                    event["content"] = content
                    changed = True
            if process_additional and "additional_content" in event:
                new_value = await self._process_value(event["additional_content"])
                if new_value != event["additional_content"]:
                    event["additional_content"] = new_value
                    changed = True
            if changed:
                new_lines.append("data:" + json.dumps(event, ensure_ascii=False))
            else:
                new_lines.append(line)
        return new_lines
