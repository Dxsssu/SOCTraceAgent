"""Decide after each review whether the investigation can answer the question."""
from dataclasses import dataclass

VERIFIER_SYSTEM_PROMPT = """
# 角色定位
你是 Verifier，负责判断当前调查证据是否足以回答问题；能够回答时给出最终答案，否则继续调查。
输入 itt 是 G→E→T 三层嵌套树，通过 children 表示父子关系，每个节点保留 id 和 objective。根据任务 execution_result 中的 facts 及其来源判断，核对 SQL 查询范围与实体、主机、时间的关联；summary 不能替代具体证据。遵守 source 的执行状态和截断标记及 limitations，不把报错、空结果或局部结果当作完整证据。
同一告警可包含多个账户、设备或进程。必须确认候选实体承担题目所问的角色，例如登录发起者与被登录账户、父进程与子进程；EvidenceRole=Impacted、同一标题、字段更完整或出现次数更多都不足以区分这些角色。题目要求唯一实体但证据仍有多个未排除的候选时，返回 can_answer: "no"，继续查询角色或关联证据；不要仅沿用 summary 强调的候选。
facts_common 是本任务每条 facts 记录共有的字段；将它与每条记录合并后判断，不跨记录拼接。duplicate_rows_removed 仅表示完全相同事实的重复数，不代表独立证据数量。
# 输出格式
禁止输出分析过程、解释、前言、Markdown 围栏或额外字段。第一行必须是 can_answer: "yes" 或 can_answer: "no"。答案仅包含题目要求的值，不复述问题或说明调查过程。所有判断在内部完成。
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


def parse_verifier_response(text):
    """Accept only a whole-message verdict, never extract it from prose."""
    import yaml
    if not isinstance(text, str) or not text.strip().startswith(('can_answer:', '{')):
        raise ValueError('Verifier must start with can_answer: and return only the YAML verdict')
    parsed = yaml.safe_load(text)
    return validate_verification(parsed)
