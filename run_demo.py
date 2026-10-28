"""新手建议先读这个文件：它展示整个项目怎样拼在一起运行。

运行：在项目根目录执行 `py run_demo.py`，无需先启动 API。
输入：脚本中写好的两张示例工单；输出：终端里的三段 JSON。
副作用：在配置的数据库路径保存演示工单、审批单和节点事件。
本脚本自动模拟审批同意，所以不需要你中途输入文字。
"""

# 标准库 json 用来打印字典，ensure_ascii=False 让中文保持可读。
import json

# import 只是取得其他模块里的定义，真正组装对象的代码在 main() 中。
from app.audit import AuditStore
from app.knowledge import KnowledgeBase
from app.model_adapter import build_model_from_env
from app.tools import EnterpriseTools
from app.workflow import EnterpriseAgent
from app.config import get_settings
from app.auth import IdentityStore
from app.service import TicketService


def main() -> None:
    """按顺序组装依赖、运行两张工单、模拟批准并打印结果。"""
    # 第 1 步：创建存储对象。它会准备 data/agent.db（或环境变量指定的文件）。
    settings = get_settings()
    audit = AuditStore(settings.db_path, timeout=settings.sqlite_timeout)
    # 第 2 步：从最内层往外看：
    # KnowledgeBase(KB_DIR) 读取文档 -> EnterpriseTools(...) 装好工具 -> Agent 组装完成。
    # audit 传给工具和 Agent，是为了让两者使用同一份存储。
    # build_model_from_env 会按 AGENT_LLM_MODE 选择 DeepSeek 或离线模型。
    # 有 DEEPSEEK_API_KEY 时，这两张工单会真实调用 DeepSeek；没有 Key 时仍可演示。
    agent = EnterpriseAgent(
        EnterpriseTools(KnowledgeBase(settings.kb_dir, settings=settings), audit),
        audit,
        model=build_model_from_env(settings),
        settings=settings,
    )
    # 演示脚本由本机开发者运行，读取初始化时生成的三种身份凭据。
    identities = IdentityStore(settings.auth_file)
    keys_path = settings.auth_file.with_name(settings.auth_file.stem + ".keys.json")
    keys = json.loads(keys_path.read_text(encoding="utf-8"))
    service = TicketService(agent)
    alice = identities.authenticate(keys["alice"])
    bob = identities.authenticate(keys["bob"])
    manager = identities.authenticate(keys["manager"])
    password = service.create(alice, title="无法登录", description="员工忘记密码，需要重置")
    access = service.create(bob, title="申请报表库权限", description="需要报表数据库只读权限")
    # 第 4 步：观察两份 AgentState。第一份 closed，第二份 awaiting_approval。
    print("=== 低风险自动处理 ===")
    # to_dict 把对象转字典；json.dumps 把字典转文本；print 才真正显示到终端。
    print(json.dumps(password.to_dict(), ensure_ascii=False, indent=2))
    print("=== 高风险进入审批 ===")
    print(json.dumps(access.to_dict(), ensure_ascii=False, indent=2))
    if access.approval:
        # 使用已验证经理身份；service 检查部门和禁止自审。
        approved = service.approve(manager, access.approval.id, True)
        print("=== 审批后继续执行 ===")
        print(json.dumps(approved.to_dict(), ensure_ascii=False, indent=2))


# 这个入口判断保证：直接运行本文件才执行 main，导入它不会自动跑两张工单。
if __name__ == "__main__":
    main()
