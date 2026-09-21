import asyncio
import uuid
import json
from memweft import AsyncMemory

async def main():
    # Initialize asynchronous memory (using in-memory SQLite for this demo)
    mem = AsyncMemory(in_memory=True)
    print("🧠 AsyncMemory initialized.")

    # Define a shared scope
    run_id = uuid.uuid4().hex
    scope = {
        "tenant_id": "demo",
        "user_id": "alice",
        "agent_id": "helper-bot",
        "session_id": "session-1",
        "run_id": run_id,
    }

    print(f"📂 Scope: {json.dumps(scope, indent=2)}")

    # 1. Concurrent Writes: Simulate a burst of events
    print("\n🚀 Simulating concurrent event ingestion...")
    events = []
    for i in range(10):
        event = {
            "event_id": str(uuid.uuid4()),
            "scope": scope,
            "kind": "message",
            "payload": {"role": "user", "content": f"Message {i}"},
            "tags": ["demo", "burst"],
        }
        events.append(event)

    # Use asyncio.gather to append events in parallel (handled by thread pool in Rust)
    await asyncio.gather(*(mem.append_event(e) for e in events))
    print(f"✅ Successfully appended {len(events)} events concurrently.")

    # 2. Async Read: List events
    print("\n🔍 Reading back events...")
    stored_events = await mem.list_events(scope)
    print(f"✅ Retrieved {len(stored_events)} events from store.")

    # 3. Build Memory Packet asynchronously
    print("\n📦 Building Memory Packet...")
    request = {
        "scope": scope,
        "purpose": "planner",
        "task_type": "generic",
        "budget": {"max_tokens": 1000}
    }
    packet = await mem.build_memory_packet(request)
    
    print(f"✅ Packet generated. Schema Version: {packet['meta']['schema_version']}")
    print(f"   Candidates: {len(packet['long_term']['facts'])} facts, {len(packet['long_term']['episodes'])} episodes")

if __name__ == "__main__":
    asyncio.run(main())
