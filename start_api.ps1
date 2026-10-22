# PowerShell 启动脚本，用来少打一条切换目录命令。
# $PSScriptRoot 是 PowerShell 内置变量，表示本脚本所在的目录。
# Set-Location 切换当前工作目录，因此 Python 能找到 app 包。
Set-Location $PSScriptRoot
# python 使用当前已激活的虚拟环境；-m 表示以模块方式执行 app/api.py。
# 这条命令会持续运行，直到在终端按 Ctrl+C。
# 如果 PowerShell 不允许执行 .ps1，可手动进入项目目录运行下面的 Python 命令。
python -m app.api
