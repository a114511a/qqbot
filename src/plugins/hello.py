"""示例插件：发送 /hello 或 /你好 进行测试。

用于确认链路是否通畅：能收到回复就说明 QQ ↔ NapCat ↔ NoneBot 三段都通了。
"""

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message

hello = on_command("hello", aliases={"你好"}, priority=10, block=True)


@hello.handle()
async def handle_hello() -> None:
    await hello.finish(Message("傻逼，喊我干啥。"))
