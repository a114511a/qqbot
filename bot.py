"""NoneBot2 启动入口。"""

import os
from pathlib import Path

import nonebot
from nonebot.adapters.onebot.v11 import Adapter


PROJECT_ROOT = Path(__file__).resolve().parent

# pyproject.toml 里的 plugin_dirs（src/plugins）是相对路径，按当前工作目录解析。
# 这里统一切到项目根目录，双击启动脚本、从 Finder 启动、或从别处调用都能加载到插件。
os.chdir(PROJECT_ROOT)

# 显式从项目根目录的 .env 读取 NoneBot 与插件配置（含 API Keys）。
nonebot.init(_env_file=PROJECT_ROOT / ".env")


driver = nonebot.get_driver()
driver.register_adapter(Adapter)
nonebot.load_from_toml(str(PROJECT_ROOT / "pyproject.toml"))


if __name__ == "__main__":
    nonebot.run()
