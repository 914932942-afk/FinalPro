"""告诉 Python：app 是一个可以导入的包（模块的集合）。

一个 .py 文件通常叫一个“模块”；把相关模块放进目录，就能组成一个“包”。
例如 `from app.models import Ticket` 是从 app 包的 models 模块取出 Ticket 类。

本文件没有启动程序的代码，也没有创建 Agent。
运行演示请从项目根目录执行 `py run_demo.py`。
运行接口请执行 `py -m app.api`，-m 表示按模块方式运行，方便解析相对导入。
"""
