"""astrbot_plugin_bilibilidownload 内部模块。

AstrBot 以 ``data.plugins.<插件目录名>.main`` 的形式导入插件（star_manager
使用 ``__import__(path, fromlist=["main"])``），所以 main.py 属于一个包，
这里可以被 ``from .core.xxx import ...`` 相对导入。
"""
