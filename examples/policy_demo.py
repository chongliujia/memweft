import json
from memweft import Memory

def main():
    mem = Memory(in_memory=True)
    scope = {
        "tenant_id": "demo",
        "user_id": "charlie",
        "agent_id": "planner",
        "session_id": "s1",
        "run_id": "r1",
    }

    # 1. Seed Memory with excessive data
    print("🌱 Seeding memory with 50 facts...")
    for i in range(50):
        mem.upsert_fact(scope, {
            "fact_id": f"fact-{i}",
            "fact_key": f"data.point.{i}",
            "value": i,
            "status": "active",
            "confidence": 0.5
        })

    # 2. Define a Strict Policy
    # We restrict the planner to only see top 5 facts to save context window
    strict_policy = {
        "max_facts": 5,
        "max_episodes": 2,
        "max_total_candidates": 10
    }

    # 3. Define a Budget
    # We also enforce a token budget for the 'facts' section specifically
    budget = {
        "max_tokens": 2000,
        "per_section": {
            "facts": 100  # Very tight budget for facts
        }
    }

    print("\n👮‍♀️ Requesting packet with Strict Policy + Budget...")
    request = {
        "scope": scope,
        "purpose": "planner",
        "policy": strict_policy,
        "budget": budget,
        "policy_id": "strict-v1"
    }

    packet = mem.build_memory_packet(request)

    # 4. Analyze the Result
    facts = packet["long_term"]["facts"]
    report = packet["budget_report"]
    explain = packet["explain"]

    print(f"\n📊 Result Analysis:")
    print(f"   Facts returned: {len(facts)} (Policy limit: 5)")
    print(f"   Total Facts in DB: 50")
    
    print(f"\n💰 Budget Report:")
    print(f"   Used Tokens (Est): {report['used_tokens_est']}")
    print(f"   Omissions: {len(report['omissions'])} items dropped")
    if report['omissions']:
        print(f"   Example omission: {report['omissions'][0]}")

    print(f"\n🧠 Explainability:")
    print(f"   Candidate Limits Used: {json.dumps(explain.get('candidate_limits'), indent=2)}")

if __name__ == "__main__":
    main()
