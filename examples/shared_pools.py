"""Two Agent handles with private notes and shared facts; no model service needed."""
from memweft import Memory


def main():
    config = {
        "read_pools": [
            {"pool_id": "private", "access": "read_write"},
            {"pool_id": "project", "access": "read_write"},
        ],
        "default_write_pool": "private",
        "conflict_policy": "private_first",
    }
    with Memory(in_memory=True) as memory:
        planner = memory.user("alice", agent_id="planner", memory_config=config)
        executor = memory.user("alice", agent_id="executor", memory_config=config)
        planner.remember("先列出计划，再执行", key="work_style")
        shared = planner.remember(8002, key="service_port", pool_id="project", expected_revision=0)
        assert [r["fact_key"] for r in executor.memories()] == ["service_port"]
        print("执行 Agent 读取共享事实：")
        print(executor.session("deploy").context(query="service_port").text)
        planner.session("deploy").add_message("user", "仅供规划 Agent 的会话内容")
        assert executor.session("deploy").messages() == []
        executor.remember(9000, key="service_port")
        assert executor.memories()[0]["value"] == 9000
        executor.forget("service_port")
        assert executor.memories()[0]["value"] == 8002  # shared fallback
        executor.remember(8003, key="service_port", pool_id="project", expected_revision=shared["revision"])
        assert planner.memories(pool_id="project")[0]["value"] == 8003
        print("共享更新已对规划 Agent 可见；私有事实和会话保持隔离。")


if __name__ == "__main__":
    main()
