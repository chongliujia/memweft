import asyncio
import os
from openai import OpenAI
from memweft import AsyncMemory

# ==========================================
# CONFIGURATION
# ==========================================
# 优先从环境变量读取
API_KEY = os.getenv("DEEPSEEK_API_KEY", "your_deepseek_api_key")
BASE_URL = "https://api.deepseek.com"

if API_KEY == "your_deepseek_api_key":
    print("⚠️  警告: 未检测到环境变量 DEEPSEEK_API_KEY，将使用硬编码的占位符。")
else:
    print(f"✅ 已检测到 API Key: {API_KEY[:6]}******")

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

async def chat_with_memory():
    db_path = "examples/deepseek_agent.db"
    # Clean up previous run for demo purposes
    if os.path.exists(db_path):
        os.remove(db_path)

    # Initialize MemWeft (Rust Backend)
    print(f"🧠 Initializing MemWeft at {db_path}...")
    mem = AsyncMemory(path=db_path)
    
    # Define Context Scope
    scope = {
        "tenant_id": "demo_tenant",
        "user_id": "user_jiachong",
        "agent_id": "deepseek_assistant",
        "session_id": "session_001",
        "run_id": "run_001"
    }

    print("\n--- Step 1: User Input ---")
    user_input = "Hello! My name is Jiachong. I'm a Rust developer and I prefer strict typing."
    print(f"User: {user_input}")
    
    # 1. Store event in Short-term Memory
    await mem.append_event({
        "event_id": "evt_1",
        "scope": scope,
        "kind": "message",
        "payload": {"role": "user", "content": user_input}
    })

    print("\n--- Step 2: Memory Consolidation (Simulation) ---")
    # 2. Simulate extracting Facts into Long-term Memory
    # In a real agent, this would be done by an LLM analyzing the conversation in background
    print("📝 Storing Fact: User is a Rust developer")
    await mem.upsert_fact(scope, {
        "fact_id": "fact_1",
        "fact_key": "user.job",
        "value": "Rust Developer",
        "confidence": 1.0
    })
    
    print("📝 Storing Fact: User prefers strict typing")
    await mem.upsert_fact(scope, {
        "fact_id": "fact_2",
        "fact_key": "user.preference.coding",
        "value": "Prefers strict typing",
        "confidence": 0.9
    })

    print("\n--- Step 3: Context Retrieval ---")
    # 3. Build Memory Packet
    # This triggers the Rust 'Composer' which filters, ranks, and fits data into budget
    packet = await mem.build_memory_packet({
        "scope": scope,
        "purpose": "responder",
        "budget": {"max_tokens": 1000} # DeepSeek context budget
    })
    
    facts = [f['value'] for f in packet['long_term']['facts']]
    print(f"📚 Retrieved {len(facts)} relevant facts: {facts}")

    print("\n--- Step 4: LLM Generation ---")
    # 4. Construct System Prompt with MemWeft Context
    system_prompt = f"""
    You are a helpful assistant.
    
    ## Memory Context
    The following information is retrieved from your long-term memory about the user:
    {json_bullet_points(facts)}
    
    ## Instructions
    Answer the user's question using the context provided.
    """

    user_query = "What language should I use for my next high-performance project?"
    print(f"User Query: {user_query}")

    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_query}
            ],
            stream=False
        )
        print(f"\n🤖 DeepSeek: {response.choices[0].message.content}")
    except Exception as e:
        print(f"\n❌ API Call Failed (Did you set the API Key?): {e}")

def json_bullet_points(items):
    return "\n".join([f"- {item}" for item in items])

if __name__ == "__main__":
    asyncio.run(chat_with_memory())
