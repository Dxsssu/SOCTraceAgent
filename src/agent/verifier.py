"""Decide after each review whether the investigation can answer the question."""
from dataclasses import dataclass

VERIFIER_SYSTEM_PROMPT = """
# 角色定位
你是 Verifier，负责判断当前调查证据是否足以回答问题；能够回答时给出最终答案，否则继续调查。
# 输出格式
仅输出 YAML。can_answer 只能为 "yes" 或 "no"；只有 "yes" 时输出 answer。
能够回答：
can_answer: "yes"
answer: "提交的最终答案"
不能回答：
can_answer: "no"
""".strip()


@dataclass(frozen=True)
class VerifierAgent:
    role_name: str = "_verifier"
    system_prompt: str = VERIFIER_SYSTEM_PROMPT


def validate_verification(value):
    if not isinstance(value, dict) or "can_answer" not in value:
        raise ValueError("Verifier requires can_answer")
    decision = value["can_answer"]
    # YAML 1.1 resolves unquoted yes/no to booleans; normalize the read model.
    if type(decision) is bool:
        decision = "yes" if decision else "no"
    if decision not in ("yes", "no"):
        raise ValueError('can_answer must be "yes" or "no"')
    if decision == "no":
        if set(value) != {"can_answer"}:
            raise ValueError('When can_answer is "no", omit answer and all other fields')
        return {"can_answer": "no"}
    if set(value) != {"can_answer", "answer"}:
        raise ValueError('When can_answer is "yes", output only can_answer and answer')
    answer = value["answer"]
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("answer must be a nonempty string")
    return {"can_answer": "yes", "answer": answer.strip()}
