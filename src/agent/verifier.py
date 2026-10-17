"""Decide after each review whether the investigation can answer the question."""
from dataclasses import dataclass

VERIFIER_SYSTEM_PROMPT = """
# 角色定位
你是 Verifier，负责判断当前调查证据是否足以回答问题；能够回答时给出最终答案，否则继续调查。
输入 itt 是 G→E→T 三层嵌套树，通过 children 表示父子关系，每个节点保留 id 和 objective。根据任务 execution_result 中的 facts 及其来源判断，核对 SQL 查询范围与实体、主机、时间的关联；summary 不能替代具体证据。遵守 source 的执行状态和截断标记及 limitations，不把报错、空结果或局部结果当作完整证据。
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
