"""插件级常量。

版本号只在这里定义一处，``/bdlstatus`` 直接读它；``metadata.yaml``、README
与 CHANGELOG 中的版本号与之手工同步（O6 计划用测试强制三者一致）。

之所以不把配置默认值也搬进来（O6 的完整形态）：``_conf_schema.json`` 是 AstrBot
运行时读取的静态文件，无法从 Python 常量生成，硬搬只会制造第二处需要同步的地方。
"""

from __future__ import annotations

PLUGIN_NAME = "astrbot_plugin_bilibilidownload"
VERSION = "0.2.1"

__all__ = ["PLUGIN_NAME", "VERSION"]
