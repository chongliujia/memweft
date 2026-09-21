import json
import os
from pathlib import Path

from memweft import Memory
from memweft.adapters.langchain import MemWeftChatMessageHistory, MemWeftContextInjector

try:
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import HumanMessage, SystemMessage
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "This example requires langchain-openai and langchain-core. "
        "Install with: pip install langchain-openai langchain-core"
    ) from exc


def load_dotenv() -> None:
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[1] / ".env",
        Path(__file__).resolve().parents[1] / "python" / ".env",
    ]
    for path in candidates:
        if path.exists():
            _load_env_file(path)
            break


def _load_env_file(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def main() -> None:
    load_dotenv()
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit("Set DEEPSEEK_API_KEY before running this example.")

    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    memory = Memory(path="data/memweft.db")
    scope = {
        "tenant_id": "default",
        "user_id": "u1",
        "agent_id": "a1",
        "session_id": "s1",
        "run_id": "r1",
    }

    history = MemWeftChatMessageHistory(memory, scope, limit=10)
    injector = MemWeftContextInjector(memory, scope)

    user_text = "请用一句话总结 MemWeft 的核心目标。"
    history.add_message(HumanMessage(content=user_text))

    packet = injector.build_packet(purpose="planner", task_type="summary")
    packet_text = json.dumps(packet, ensure_ascii=True)

    system_prompt = (
        "You are an assistant that uses MemoryPacket to answer.\n"
        f"MemoryPacket:\n{packet_text}"
    )

    llm = ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=0.2,
    )

    response = llm.invoke([SystemMessage(content=system_prompt), HumanMessage(content=user_text)])
    history.add_message(response)

    print(response.content)


if __name__ == "__main__":
    main()
