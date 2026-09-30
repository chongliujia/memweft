"""Run twice to verify persistence. No API key or framework required."""
from memweft import Memory

memory = Memory("data/quickstart.db")
alice = memory.user("alice")
alice.remember("喜欢简短、直接的回答", key="reply_style")
chat = alice.session("chat-001")
question = "帮我解释 Rust 的所有权"
chat.add_message("user", question, event_id="question-1")
context = chat.context(query=question, max_tokens=1000)
print(context.text)
print("Context warnings:", context.explain()["warnings"])
print("Stored messages:", len(chat.messages()))
