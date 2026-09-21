import os
from openai import OpenAI
from memweft import Memory

# 配置
API_KEY = os.getenv("DEEPSEEK_API_KEY", "your_api_key_here")
BASE_URL = "https://api.deepseek.com"

def task_planning_demo():
    print("🛠 Agent 任务规划与状态跟踪演示 (AI 决策版)")
    
    if API_KEY == "your_api_key_here":
        print("❌ 错误: 请先设置环境变量 export DEEPSEEK_API_KEY='你的key'")
        return

    client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
    mem = Memory(in_memory=True)
    scope = {"tenant_id": "t", "user_id": "u", "agent_id": "planner_agent", "session_id": "s", "run_id": "r"}

    # 1. 初始化任务状态
    print("\n--- 1. 初始化 Agent 状态 ---")
    mem.patch_working_state(scope, {
        "goal": "策划一场去日本京都的 3 天旅行",
        "plan": [
            "1. 调研酒店",
            "2. 查看新干线时刻表",
            "3. 预订餐厅"
        ],
        "state_version": 1
    })

    # 2. 模拟执行了一部分任务，并记录决策
    print("\n--- 2. 模拟任务执行并更新状态 ---")
    # 存入一条工具执行结果
    mem.append_event({
        "event_id": "evt_tool_1", "scope": scope, "kind": "tool_result",
        "payload": {"tool": "hotel_search", "result": "发现：The Thousand Kyoto 酒店评分极高，靠近车站。"}
    })

    # 更新进度和决定
    mem.patch_working_state(scope, {
        "plan": [
            "[已完成] 1. 调研酒店",
            "[进行中] 2. 查看新干线时刻表",
            "3. 预订餐厅"
        ],
        "decisions": ["选择了 'The Thousand Kyoto' 酒店，因为交通便利"],
        "state_version": 2
    })

    # 3. 让 DeepSeek 根据 MemWeft 维护的状态做出决策
    print("\n--- 3. 正在请求 DeepSeek 决定下一步行动 ---")
    
    packet = mem.build_memory_packet({"scope": scope, "purpose": "planner"})
    ws = packet['short_term']['working_state']
    
    system_prompt = f"""
    你是一个任务规划专家。以下是 Agent 当前的工作记忆：
    【目标】: {ws['goal']}
    【当前计划】: {ws['plan']}
    【已做决定】: {ws['decisions']}
    """
    
    user_query = "根据目前的进度和已有的决定，请告诉我 Agent 下一步最应该执行的具体动作是什么？"

    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_query}
            ]
        )
        print(f"\n🤖 DeepSeek 决策建议:\n{response.choices[0].message.content}")
    except Exception as e:
        print(f"\n❌ 调用失败: {e}")

if __name__ == "__main__":
    task_planning_demo()
