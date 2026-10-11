<!-- rag: {"resource":"vpn","codes":"E218","status":"active","version":"demo-2026-09"} -->
# VPN E218：解析连接器故障

适用范围：本 Demo 的 VPN、内网报错 E218。编号及规则为模拟内部知识。
E218 表示 VPN 的地址解析连接器不可用，与报表数据库的同编号策略复核不同。
处理结论：propose_action，动作 open_incident，资源 vpn。记录错误并登记故障，无须询问首次使用或已有权限。
不申请新权限、不重置密码；登记不等于修复完成。
