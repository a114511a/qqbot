"""中二节奏底力分析 · 落雪（LXNS）API 客户端。

职责边界：
- 本模块只负责「跟落雪说话」：拼 URL、带鉴权、发请求、把 HTTP/网络错误翻译成
  可直接展示给用户的异常、把响应里的 ``data`` 信封拆掉。
- 不做任何业务计算（统计、拟合、文案、渲染都在 analysis.py），也不读写本地数据库。

为什么要独立出来：
原先这些代码和统计、渲染混在 analysis.py 里（2400 多行），新增一个落雪接口就得翻半天。
现在扩展落雪能力只需要动这一个文件——在"具体接口"一段加一个 ``async def fetch_xxx``，
调用方拿到的是已经过错误翻译的裸数据。

鉴权约定（和 OpenRouter 不一样，别改混了）：
- 落雪：``Authorization: <开发者 API 密钥>``，**不能**加 ``Bearer `` 前缀。
  实测带前缀会被判 401 unauthorized。
- 密钥来自项目 ``.env`` 的 ``LXNS_DEV_TOKEN``。

错误约定：
- 所有 HTTP 失败都收敛成 AnalysisError（或子类 ResourceNotFoundError），
  文案直接面向 QQ 用户，上层不必再包装。
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

# 本文件位于 <项目根>/src/plugins/chunithm_bot/lxns.py，向上 3 层即项目根目录。
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = PROJECT_ROOT / "data"
SONG_CACHE_PATH = DATA_DIR / "chunithm_songs_cache.json"

LXNS_BASE_URL = "https://maimai.lxns.net/api/v0"

# 曲目目录缓存有效期。曲目/谱面资料变化很慢，缓存能省下大量请求。
SONG_CACHE_MAX_AGE = 12 * 60 * 60

# 请求超时：成绩类接口要连续拉好几个端点，给宽一点。
DEFAULT_TIMEOUT = 22.0
PLAYER_TIMEOUT = 18.0

# 单页请求的超时。补充源用这个更短的值，避免一个慢接口拖住整份报告。
OPTIONAL_TIMEOUT = 15.0

TOKEN_ENV_NAME = "LXNS_DEV_TOKEN"
TOKEN_MISSING_MESSAGE = (
    f"还没有配置落雪 API 令牌。请在项目 .env 中填写 {TOKEN_ENV_NAME}，"
    "授予读取玩家资料及成绩权限后重启 NoneBot。"
)


class AnalysisError(RuntimeError):
    """可直接展示给用户的查询错误。

    定义在 API 层：所有落雪调用失败最终都收敛成这个异常（或其子类），
    上层指令可以直接把 ``str(exc)`` 发回 QQ。analysis.py 重新导出了它，
    所以 ``from .analysis import AnalysisError`` 的旧写法继续可用。
    """


class ResourceNotFoundError(AnalysisError):
    """落雪返回 404：资源不存在。

    落雪对两种「查不到」给了不同文案，靠它区分是哪一步挂了：
    - "player not found"：好友码没有对应的玩家
    - "user not found"：这个 QQ 没有在落雪账号里绑定
    """

    def __init__(self, message: str, api_message: str = "") -> None:
        super().__init__(message)
        self.api_message = api_message


# ---------------------------------------------------------------------------
# 配置与 HTTP 基础
# ---------------------------------------------------------------------------


def get_env(name: str, default: str = "") -> str:
    """读配置：优先进程环境变量，其次项目 .env。

    两者都读是为了兼容不同的启动方式——双击 .command 脚本时配置来自 .env，
    而测试或 echo 进环境变量启动时来自进程环境。
    """
    value = os.environ.get(name)
    if value is None:
        value = dotenv_values(PROJECT_ROOT / ".env").get(name)
    return str(value or default).strip()


def get_token() -> str:
    """取落雪开发者 API 密钥；没配置时抛出面向用户的错误。"""
    token = get_env(TOKEN_ENV_NAME)
    if not token:
        raise AnalysisError(TOKEN_MISSING_MESSAGE)
    return token


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _unwrap(payload: Any) -> Any:
    """落雪大部分响应包了一层 ``{"data": ...}``，这里统一拆掉。"""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _translate_http_error(status_code: int, response: httpx.Response) -> AnalysisError:
    """把落雪的各种错误码翻译成面向用户、指明排查方向的异常。

    401 / 403 / 404 / 429 / 5xx / 400 分别对应完全不同的原因，
    混成一句「查询失败」会让用户在错误的方向上乱试。
    """
    # 401 和 403 是两种完全不同的故障，分开提示。
    if status_code == 401:
        return AnalysisError(
            f"落雪 API 认证失败（HTTP 401）。请检查 .env 中的 {TOKEN_ENV_NAME}："
            "必须是落雪开发者面板里的「开发者 API 密钥」，且要原样填写、不要带 Bearer 前缀。"
        )
    if status_code == 403:
        return AnalysisError(
            "落雪 API 权限不足（HTTP 403）。开发者密钥本身有效，但缺少读取权限："
            "玩家资料需要 allow_third_party_fetch_player，成绩需要 allow_third_party_fetch_scores。"
            "请在被查询玩家的落雪账号里开启第三方读取授权。"
        )
    if status_code == 404:
        # 落雪的 message 字段能区分「玩家不存在」和「QQ 没登记」，直接用它定位问题。
        api_message = ""
        try:
            api_message = str(response.json().get("message") or "")
        except ValueError:
            pass
        if api_message == "user not found":
            return ResourceNotFoundError(
                "落雪没有登记这个 QQ 账号（user not found）。"
                "需要先在落雪查分器里把 QQ 绑定到你的账号，才能用 QQ 反查身份。",
                api_message,
            )
        if api_message == "player not found":
            return ResourceNotFoundError(
                "落雪没有这个好友码对应的玩家（player not found），请核对好友码是否填错。",
                api_message,
            )
        return ResourceNotFoundError("落雪没有查到对应资料（HTTP 404）。", api_message)
    if status_code == 429:
        return AnalysisError("落雪 API 当前请求次数较多，请稍后再试。")
    if status_code >= 500:
        return AnalysisError("落雪服务暂时不可用，请稍后重试。")
    if status_code == 400:
        # 实测：好友码位数不对（落雪是 15 位数字）会走到这里，而不是 404。
        return AnalysisError(
            "落雪 API 认为请求参数不合法（HTTP 400）。最常见的原因是好友码格式不对，"
            "落雪好友码是 15 位数字，请核对后重试。"
        )
    return AnalysisError(f"落雪 API 返回错误（HTTP {status_code}）。")


def _request(
    url: str,
    *,
    token: str = "",
    params: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> "_SyncResponse":
    """构造一次请求的惰性描述（不发送），由 ``_send`` 在协程里真正执行。

    这样做是因为 httpx 的同步客户端不能放进异步函数，而测试又想替换发送层；
    把「怎么发」和「发什么」拆开，替换/打桩都只改一处。
    """
    return _SyncResponse(url=url, token=token, params=params, timeout=timeout)


class _SyncResponse:
    """一次尚未发送的 HTTP 请求描述。

    内部承载请求参数，便于统一重试、日志与测试打桩；
    真正发送逻辑在 ``_send``，调用方不需要接触这个类型。
    """

    __slots__ = ("url", "token", "params", "timeout")

    def __init__(
        self,
        *,
        url: str,
        token: str,
        params: dict[str, Any] | None,
        timeout: float,
    ) -> None:
        self.url = url
        self.token = token
        self.params = params
        self.timeout = timeout


async def _send(request: "_SyncResponse") -> Any:
    """发送请求并返回拆掉信封后的 JSON；所有失败翻译成 AnalysisError。"""
    # 落雪（LXNS）的鉴权要求令牌直接放进 Authorization，不能加 "Bearer " 前缀。
    # 加了前缀会被判为 401 unauthorized（实测：带前缀 401，不带前缀鉴权通过）。
    headers = {"Authorization": request.token} if request.token else {}
    try:
        client = httpx.AsyncClient(timeout=request.timeout, follow_redirects=True)
    except (httpx.InvalidURL, ValueError) as exc:
        # 环境里的 NO_PROXY 若写成 "127.0.0.1,[::1]" 这种方括号 IPv6 形式，
        # httpx 在创建客户端时就会因为解析端口失败抛 InvalidURL，请求根本发不出去。
        # 这时退回一个不读代理环境的裸客户端，保证访问落雪不受本机代理配置影响。
        client = httpx.AsyncClient(
            timeout=request.timeout, follow_redirects=True, trust_env=False
        )
    try:
        async with client:
            response = await client.get(request.url, headers=headers, params=request.params)
    except httpx.TimeoutException as exc:
        raise AnalysisError("查询服务响应超时，请稍后重试。") from exc
    except httpx.HTTPError as exc:
        raise AnalysisError("暂时无法连接查询服务，请检查网络后重试。") from exc
    if response.is_error:
        raise _translate_http_error(response.status_code, response)
    try:
        return _unwrap(response.json())
    except ValueError as exc:
        raise AnalysisError("落雪 API 返回了无法识别的数据格式。") from exc


async def _get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    """带鉴权的 GET，返回拆掉信封的 JSON。"""
    return await _send(_request(url, token=get_token(), params=params, timeout=timeout))


async def _get_json_optional(url: str, *, timeout: float = OPTIONAL_TIMEOUT) -> Any | None:
    """取一个「有更好、没有也能凑合」的接口，失败时返回 None 而不是中断整个报告。

    用于 /scores、/recents 这类补充数据源：它们挂了不应该连累 /bests 已经拿到的成绩。
    """
    try:
        return await _get_json(url, timeout=timeout)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 具体接口
# ---------------------------------------------------------------------------


async def fetch_player(friend_code: str) -> dict[str, Any]:
    """按好友码读玩家资料（昵称、Rating、好友码等）。

    需要落雪开发者密钥具备 allow_third_party_fetch_player 权限。
    """
    result = await _get_json(
        f"{LXNS_BASE_URL}/chunithm/player/{friend_code}", timeout=PLAYER_TIMEOUT
    )
    if not isinstance(result, dict):
        raise AnalysisError("落雪返回的玩家资料格式不正确。")
    return result


async def fetch_player_by_qq(qq: str) -> dict[str, Any]:
    """按 QQ 号查落雪玩家资料。

    注意：绑定流程已不再使用它。落雪不保证登记了用户的 QQ，硬做 QQ 关联核对
    会把没绑 QQ 的玩家直接挡在门外（实测返回 404 user not found）。
    保留此函数仅供排查/将来需要时调用。
    """
    result = await _get_json(
        f"{LXNS_BASE_URL}/chunithm/player/qq/{qq}", timeout=PLAYER_TIMEOUT
    )
    if not isinstance(result, dict):
        raise AnalysisError("落雪没有返回与当前 QQ 账号关联的中二节奏资料。")
    return result


async def fetch_bests(friend_code: str) -> Any:
    """读 Best30 / Selection10 / New20。核心成绩来源，失败会抛出 AnalysisError。"""
    return await _get_json(f"{LXNS_BASE_URL}/chunithm/player/{friend_code}/bests")


async def fetch_scores(friend_code: str) -> Any | None:
    """读全部缓存成绩（**只有评级没有分数**）。覆盖最全，属于补充源。"""
    return await _get_json_optional(f"{LXNS_BASE_URL}/chunithm/player/{friend_code}/scores")


async def fetch_recents(friend_code: str) -> Any | None:
    """读最近 50 次游玩（带分数）。属于补充源，失败时静默降级。"""
    return await _get_json_optional(f"{LXNS_BASE_URL}/chunithm/player/{friend_code}/recents")


async def fetch_all_scores(friend_code: str) -> dict[str, Any]:
    """并发拉取玩家的 bests / scores / recents 原始响应。

    三个接口的差别（实测结论，改报告口径前务必先读一遍）：

    - /bests   Best30 / Selection10 / New20，**带具体分数与单曲 Rating**，但覆盖少
    - /scores  玩家缓存的所有最佳成绩，覆盖最全，但**只有评级没有分数**
    - /recents 最近 50 次游玩，带分数，能补上 /bests 之外的谱面

    返回 ``{"bests": ..., "scores": ..., "recents": ...}``。
    bests 失败会抛 AnalysisError；scores / recents 失败返回 None（静默降级）。
    ``gather`` 不会因为补充源返回 None 而取消其它任务。
    """
    bests, scores, recents = await asyncio.gather(
        fetch_bests(friend_code),
        fetch_scores(friend_code),
        fetch_recents(friend_code),
    )
    return {"bests": bests, "scores": scores, "recents": recents}


# ---------------------------------------------------------------------------
# 曲目目录（带本地缓存）
# ---------------------------------------------------------------------------
#
# 缓存是「读旧值也能用」的数据，格式改动时要考虑向后兼容：
# 判断只看 fetched_at 与 songs 字段，旧缓存读不出来会自动重新拉取。


def read_song_cache() -> dict[str, Any] | None:
    """读取未过期的曲目目录缓存；没有、过期或损坏都返回 None。"""
    try:
        content = json.loads(SONG_CACHE_PATH.read_text(encoding="utf-8"))
        age = _now_ts() - float(content.get("fetched_at", 0))
        if age < SONG_CACHE_MAX_AGE and isinstance(content.get("songs"), list):
            return content
    except (OSError, ValueError, TypeError):
        return None
    return None


def clear_song_cache() -> None:
    """删掉曲目目录缓存，下次调用会重新向落雪拉取。

    排查「曲目数据好像没更新」时可以先清缓存；不要与正在进行的取数并发调用。
    """
    try:
        SONG_CACHE_PATH.unlink()
    except OSError:
        pass


async def fetch_song_list() -> dict[str, Any]:
    """读落雪曲目/谱面目录（含每张谱的物量构成），带 12 小时本地缓存。

    返回 ``{"fetched_at": 时间戳, "songs": [...], "versions": [...]}``。
    缓存写入采用原子替换（先写临时文件再 rename），进程中途被杀也不会留下半个 JSON。
    """
    cached = read_song_cache()
    if cached:
        return cached
    raw = await _get_json(
        f"{LXNS_BASE_URL}/chunithm/song/list",
        params={"notes": "true"},
    )
    if not isinstance(raw, dict) or not isinstance(raw.get("songs"), list):
        raise AnalysisError("无法读取落雪曲目/谱面资料。")
    content = {
        "fetched_at": _now_ts(),
        "songs": raw["songs"],
        "versions": raw.get("versions", []),
    }
    _write_cache_atomically(content)
    return content


def _write_cache_atomically(content: dict[str, Any]) -> None:
    SONG_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp_path = SONG_CACHE_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(SONG_CACHE_PATH)
    try:
        SONG_CACHE_PATH.chmod(0o600)
    except OSError:
        pass
