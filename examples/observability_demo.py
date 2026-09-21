import logging
import sys
from memweft import Memory

# Configure standard Python logging
# Rust logs (via pyo3-log) will be forwarded here.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout
)

# You can set specific levels for the Rust crate
logging.getLogger("memweft_store").setLevel(logging.DEBUG)

def main():
    print("🕵️‍♂️  Initializing Memory with Tracing enabled...\n")
    
    # Initialize synchronous memory
    mem = Memory(in_memory=True)
    
    scope = {
        "tenant_id": "demo",
        "user_id": "bob",
        "agent_id": "debugger",
        "session_id": "s1",
        "run_id": "r1",
    }

    # Add some dummy data to trigger logic
    print("📝 Adding facts and episodes...")
    mem.upsert_fact(scope, {
        "fact_id": "f1",
        "fact_key": "user.preference",
        "value": "loves observability",
        "confidence": 1.0,
        "status": "active"
    })
    
    mem.append_episode(scope, {
        "episode_id": "ep1",
        "time_range": {"start": "2023-01-01T00:00:00Z"},
        "summary": "First interaction",
        "tags": ["intro"],
        "compression_level": "raw"
    })

    print("\n🚀 Building Memory Packet (Watch the logs below!)")
    print("-" * 60)
    
    # This call will emit INFO/DEBUG logs from Rust showing:
    # - How many candidates were loaded
    # - Budget trimming decisions
    request = {
        "scope": scope,
        "purpose": "planner",
        "cues": {"tags": ["intro"]},
        "budget": {"max_tokens": 500}
    }
    
    packet = mem.build_memory_packet(request)
    
    print("-" * 60)
    print("\n✅ Build complete.")
    print(f"Packet Explain: {packet.get('explain', {})}")

if __name__ == "__main__":
    main()
