from functools import partial
from pathlib import Path

from nonebot import logger

from ..browser import get_browser, get_page_semaphore
from ..constants import USER_AGENT

_BASE_MAI_RESOURCE_URL = "https://assets2.lxns.net/maimai"
_BASE_CHUNITHM_RESOURCE_URL = "https://assets2.lxns.net/chunithm"


async def download_resource(
    file_id: str,
    file_type: str,
    postfix: str = ".png",
    save_dir: str = "./static/mai/cover",
    base_dir: str = _BASE_MAI_RESOURCE_URL,
) -> str:
    """
    下载游戏资源文件

    :param file_id: 资源文件 ID
    :param file_type: 资源文件类型，包括 `icon`, `plate`, `frame`, `jacket`, `music`, `trophy`
    :param postfix: 资源文件后缀，默认为 `.png`
    :param save_dir: 资源文件保存目录，默认为 `./static/mai/cover`
    :param base_dir: LXNS 资源路径

    assets2.lxns.net 绝大多数资源可以直连拿图片，不需要浏览器 challenge。
    旧实现每次都启动一个 Chromium context 且全局串行（信号量=1），
    玩家成绩里有几十首新曲时就要排队几分钟，超时后渲染成灰白方块。
    这里改成先直连 HTTP，只有确实遇到 challenge（返回 HTML）才回退到 Playwright。
    """
    url = f"{base_dir}/{file_type}/{file_id}{postfix}"
    save_path = Path(save_dir) / f"{file_id}{postfix}"

    if save_path.exists():
        return str(save_path.resolve())

    content = await _download_direct(url)
    if content is None:
        content = await _download_via_browser(url)

    if not content:
        raise RuntimeError(f"下载失败：{url} 未返回有效内容")

    save_path.parent.mkdir(parents=True, exist_ok=True)
    save_path.write_bytes(content)
    return str(save_path.resolve())


def _is_image(data: bytes) -> bool:
    """粗校验响应体确实是图片（PNG / JPEG / WebP），排除 HTML challenge 和 XML 错误页。"""
    if not data or len(data) < 256:
        return False
    return (
        data[:8] == b"\x89PNG\r\n\x1a\n"
        or data[:3] == b"\xff\xd8\xff"
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
    )


async def _download_direct(url: str) -> bytes | None:
    """直连下载。trust_env=False 绕过进程里的代理变量（否则 httpx 会连不上）。"""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0), trust_env=False) as client:
            for attempt in range(2):
                try:
                    resp = await client.get(
                        url,
                        headers={"User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.8"},
                    )
                    if resp.status_code == 200 and _is_image(resp.content):
                        return resp.content
                    if resp.status_code == 404:
                        # WORLDS_END 曲目 8000+ 的封面实际按 id-8000 存放
                        return await _download_we_fallback(client, url)
                    return None
                except Exception:
                    if attempt == 0:
                        continue
                    return None
    except Exception as e:
        logger.debug(f"直连下载失败，回退浏览器: {url} -> {e}")
    return None


async def _download_we_fallback(client, url: str) -> bytes | None:
    """8000~8999 是 WORLDS_END 曲目，LXNS 用原曲封面（id-8000）。"""
    import re

    m = re.search(r"/(\d+)(\.png|\.jpg|\.webp)$", url)
    if not m:
        return None
    orig = int(m.group(1)) - 8000
    if orig < 0:
        return None
    alt = url[: m.start(1)] + str(orig) + m.group(2)
    try:
        resp = await client.get(alt, headers={"User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.8"})
        if resp.status_code == 200 and _is_image(resp.content):
            return resp.content
    except Exception:
        pass
    return None


async def _download_via_browser(url: str) -> bytes | None:
    """兜底：走 Cloudflare challenge（直连返回 HTML 时才会用到）。"""
    page_semaphore = await get_page_semaphore()
    async with page_semaphore:
        browser = await get_browser()
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="zh-CN",
            extra_http_headers={"Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"},
        )
        try:
            page = await context.new_page()
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            if resp is not None:
                ct = resp.headers.get("content-type", "")
                if resp.status < 400 and ("image" in ct or "octet-stream" in ct):
                    return await resp.body()

            api_resp = await context.request.get(
                url, headers={"Accept": "image/*,*/*;q=0.8"}, timeout=30_000
            )
            if api_resp.status >= 400:
                return None
            ct = api_resp.headers.get("content-type", "")
            if "html" in ct.lower():
                return None
            content = await api_resp.body()
            return content if _is_image(content) else None
        finally:
            await context.close()


download_mai_icon = partial(download_resource, file_type="icon", postfix=".png", save_dir="./static/mai/icon")
download_mai_plate = partial(download_resource, file_type="plate", postfix=".png", save_dir="./static/mai/plate")
download_mai_frame = partial(download_resource, file_type="frame", postfix=".png", save_dir="./static/mai/frame")
download_mai_jacket = partial(download_resource, file_type="jacket", postfix=".png", save_dir="./static/mai/cover")
download_mai_music = partial(download_resource, file_type="music", postfix=".mp3", save_dir="./static/mai/music")

download_chu_jacket = partial(
    download_resource,
    file_type="jacket",
    postfix=".png",
    save_dir="./static/chu/cover",
    base_dir=_BASE_CHUNITHM_RESOURCE_URL,
)
download_chu_icon = partial(
    download_resource,
    file_type="character",
    postfix=".png",
    save_dir="./static/chu/icon",
    base_dir=_BASE_CHUNITHM_RESOURCE_URL,
)
download_chu_trophy = partial(
    download_resource,
    file_type="trophy",
    postfix=".png",
    save_dir="./static/chu/trophy",
    base_dir=_BASE_CHUNITHM_RESOURCE_URL,
)
download_chu_plate = partial(
    download_resource,
    file_type="plate",
    postfix=".png",
    save_dir="./static/chu/plate",
    base_dir=_BASE_CHUNITHM_RESOURCE_URL,
)
