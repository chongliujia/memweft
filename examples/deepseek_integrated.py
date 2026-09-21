import asyncio
import os
from openai import OpenAI
from memweft import AsyncMemory

# 读取环境变量
API_KEY = os.getenv("DEEPSEEK_API_KEY", "your_api_key_here")
BASE_URL = "https://api.deepseek.com"

async def test_integrated_recall():
    if API_KEY == "your_api_key_here":
        print("❌ 错误: 请先设置环境变量 export DEEPSEEK_API_KEY='你的key'")
        return

    # 1. 初始化
    mem = AsyncMemory(in_memory=True)
    client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
    
    scope = {"tenant_id": "t", "user_id": "u", "agent_id": "a", "session_id": "s", "run_id": "r"}

    print("🚀 正在存入 100 条混合记忆（包含重要事实和琐碎闲聊）...")
    
    # 存入一些干扰信息（闲聊）
    for i in range(90):
        await mem.append_event({
            "event_id": f"chat_{i}", "scope": scope, "kind": "message",
            "payload": {"role": "user", "content": f"这是第 {i} 条无关紧要的闲聊内容，应该被裁剪掉。"}
        })

    # 存入几条非常关键的核心事实
    important_facts = [
        "核心事实 A：用户的真实姓名是 Jiachong，他住在上海。",
        "核心事实 B：用户正在使用 Rust 语言开发一个名为 MemWeft 的项目。",
        "核心事实 C：用户对系统的响应延迟极其敏感，目标是 10ms 以内。"
    ]
    for i, fact in enumerate(important_facts):
        await mem.upsert_fact(scope, {
            "fact_id": f"imp_{i}", 
            "fact_key": f"key_info_{i}", 
            "value": fact, 
            "confidence": 1.0 # 高置信度，MemWeft 会优先保留
        })

    # 2. 核心：设置极小的预算，强制 MemWeft 剔除那 90 条闲聊，只保留重要事实
    print("\n⚖️  设置 Token 预算为 600 (强制触发 MemWeft 智能裁剪)...")
    packet = await mem.build_memory_packet({
        "scope": scope,
        "purpose": "responder",
        "budget": {"max_tokens": 600} 
    })

    # 提取最终留下的事实
    final_facts = [f['value'] for f in packet['long_term']['facts']]
    print(f"📊 MemWeft 最终保留了 {len(final_facts)} 条事实送往 DeepSeek。")

    # 3. 让 DeepSeek 验证结果
    print("\n🤖 正在请求 DeepSeek 进行总结验证...")
    
    system_prompt = f"""
    你是一个具备长期记忆的助手。
    由于上下文长度限制，我们对记忆进行了自动筛选。
    以下是筛选后保留的记忆片段：
    {chr(10).join([f'- {f}' for f in final_facts])}
    """
    
    user_query = "请告诉我，关于用户你现在知道哪些核心信息？（请验证是否包含了姓名、项目名和性能目标）"

    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_query}
            ]
        )
        print(f"\n✅ DeepSeek 的反馈:\n{response.choices[0].message.content}")
    except Exception as e:
        print(f"\n❌ 模型调用失败: {e}")

if __name__ == "__main__":
    asyncio.run(test_integrated_recall())
