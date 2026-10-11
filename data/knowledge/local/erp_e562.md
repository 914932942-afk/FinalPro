<!-- rag: {"resource":"erp","codes":"E562","status":"active","version":"demo-2026-09"} -->
# ERP E562：缓存刷新任务故障

适用范围：本 Demo 的 ERP 报错 E562。编号及规则为模拟内部知识。
E562 表示 ERP 缓存刷新任务失败，不是账号凭据错误，也不要求补充使用历史。
处理结论：propose_action，动作 open_incident，资源 erp。保留报错文字并登记服务故障。
不通过重新开通权限或重置密码来处理；真实修复由下游人员完成。
