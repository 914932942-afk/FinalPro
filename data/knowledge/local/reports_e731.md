<!-- rag: {"resource":"reports_db","codes":"E731","status":"active","version":"demo-2026-09"} -->
# 报表数据库 E731：只读副本延迟

适用范围：本 Demo 的报表数据库、报表库报错 E731。编号及规则为模拟内部知识。
E731 表示只读副本复制延迟，属于数据服务故障，与 ERP 同编号的结账期隔离不同。
处理结论：propose_action，动作 open_incident，资源 reports_db。登记故障并保留报错文字，无須追问历史权限。
不增加写入权限，不重置密码。Agent 仅登记，实际同步恢复由人工运维处理。
