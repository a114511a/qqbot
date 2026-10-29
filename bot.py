"""NoneBot2 启动入口。"""

import os
from pathlib import Path

import nonebot
from nonebot.adapters.onebot.v11 import Adapter


PROJECT_ROOT = Path(__file__).resolve().parent

# pyproject.toml 里的 plugin_dirs（src/plugins）是相对路径，按当前工作目录解析。
# 这里统一切到项目根目录，双击启动脚本、从 Finder 启动、或从别处调用都能加载到插件。
os.chdir(PROJECT_ROOT)


def _sanitize_no_proxy() -> None:
    """修正 NO_PROXY 里的方括号 IPv6 写法。

    macOS 某些环境会把 NO_PROXY 设成 ``...,::1,[::1]`` 这种带方括号的形式，
    httpx 在创建客户端时会把 ``[::1]`` 当成「主机:端口」解析，直接抛
    ``InvalidURL: Invalid port: ':1]'``。这个异常发生在客户端构造阶段，
    请求根本发不出去，表现为插件导入即崩溃。

    这里把方括号去掉并去重，保持语义不变（仍然是本机地址不走代理），
    但不再触发 httpx 的解析 bug。
    """
    for key in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(key)
        if not raw:
            continue
        items: list[str] = []
        for part in raw.split(","):
            part = part.strip().replace("[", "").replace("]", "")
            if part and part not in items:
                items.append(part)
        os.environ[key] = ",".join(items)


_sanitize_no_proxy()

# 显式从项目根目录的 .env 读取 NoneBot 与插件配置（含 API Keys）。
nonebot.init(_env_file=PROJECT_ROOT / ".env")


driver = nonebot.get_driver()
driver.register_adapter(Adapter)
nonebot.load_from_toml(str(PROJECT_ROOT / "pyproject.toml"))


if __name__ == "__main__":
    nonebot.run()
