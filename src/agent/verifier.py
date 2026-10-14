"""Independent, evidence-bound final-answer verification."""
from dataclasses import dataclass
import re

VERIFIER_SYSTEM_PROMPT = """
# 角色定位
你是 Verifier，独立核验候选答案是否真正回答 question，而非仅找到相关实体。
# 工作内容
以 question 为最高目标，逐个检查 goals。只使用成功执行记录中的真实结果片段，计划、总结、记忆和候选答案均不是证据。
若 question 明确定义了括号缩写，引用片段须明确出现该目标术语；脚本名内的子串不是目标行为证据。
先独立从 question 确定必须证明的行为/属性/关系，再审查候选答案，不能以候选反推证明标准。
关系须由真实观测建立，不能仅凭脚本名、命令名或文件名猜测其功能；若只有启动/执行脚本的记录，没有目标行为的直接记录或已验证关联路径，必须拒绝。
区分线索、操作者、目标和受影响实体。脚本执行者不必然是实施目标行为的进程；相邻实体、同名字段或关键词命中不能替代所求关系。
confirmed=true 仅在所有目标均有证据建立所求关系且候选答案整体正确时使用。每个目标必须说明关系并引用 execution_id 和提供的 excerpt_id；跨记录关联须引用各端并核对连接键、时间和范围。截断结果不能证明穷尽性或不存在。
按题目要求的证据强度判断：询问尝试或关联对象，不要求证明行为成功；明确记录了目标行为/事件及其进程实体的同一条记录可直接支持关联。
证据不足则 confirmed=false，说明缺口和下一步需查明的关系，不猜测答案、不生成 SQL。即使任务树已完成也独立核验。
# 输出格式
仅输出 YAML，字段严格如下。gap、suggestion 各最多 120 字符；每目标 relation 最多 200 字符、最多 3 条证据，只引用提供的片段 ID，无需复制原文。
confirmed: false
goals: [] # 确认时覆盖全部目标：[{id: G1, relation: "证据如何建立所求关系", evidence: [{execution_id: E1, excerpt_id: "E1:0"}]}]
gap: "未被证实的关键关系；确认时为空"
suggestion: "需要补查的证据；确认时为空"
""".strip()

@dataclass(frozen=True)
class VerifierAgent:
    role_name: str = "_verifier"
    system_prompt: str = VERIFIER_SYSTEM_PROMPT


def validate_verification(value, goal_ids, executions, excerpts=None, question=""):
    if not isinstance(value, dict) or set(value) != {"confirmed", "goals", "gap", "suggestion"}:
        raise ValueError("Verifier requires confirmed, goals, gap, suggestion")
    if type(value['confirmed']) is not bool or not isinstance(value['goals'], list):
        raise ValueError("confirmed must be boolean; goals must be a list")
    for key in ('gap', 'suggestion'):
        if not isinstance(value[key], str) or len(value[key]) > 120:
            raise ValueError(f"{key} must be a string of at most 120 characters")
    if not value['confirmed']:
        if not value['gap'].strip() or not value['suggestion'].strip():
            raise ValueError("Rejected answers require gap and suggestion")
        if value['goals']:
            raise ValueError("Rejected verification must leave goals empty")
        return value
    if value['gap'] or value['suggestion']:
        raise ValueError("Confirmed verification must not contain gaps")
    records = {e.execution_id: e for e in executions if e.query_success and e.result_status == 'rows_returned'}
    seen = set()
    for goal in value['goals']:
        if not isinstance(goal, dict) or set(goal) != {'id', 'relation', 'evidence'}:
            raise ValueError("Each goal requires id, relation, evidence")
        ident = goal['id']
        if not isinstance(ident, str) or ident not in goal_ids or ident in seen:
            raise ValueError("Verifier must reference unique actual goal IDs")
        seen.add(ident)
        if not isinstance(goal['relation'], str) or not goal['relation'].strip() or len(goal['relation']) > 200:
            raise ValueError("Each goal requires a relationship explanation up to 200 characters")
        refs = goal['evidence']
        if not isinstance(refs, list) or not 1 <= len(refs) <= 3:
            raise ValueError("Each goal requires 1-3 evidence references")
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) not in ({'execution_id', 'quote'}, {'execution_id', 'excerpt_id'}):
                raise ValueError("Evidence requires execution_id and excerpt_id")
            if 'excerpt_id' in ref:
                source = (excerpts or {}).get(ref['excerpt_id']) if isinstance(ref['excerpt_id'], str) else None
                if source is None or source[0] != ref['execution_id']:
                    raise ValueError("Evidence must cite a provided excerpt from the same execution")
                ref['quote'] = source[1]
                del ref['excerpt_id']
            record = records.get(ref['execution_id']) if isinstance(ref['execution_id'], str) else None
            quote = ref['quote']
            if record is None or not isinstance(quote, str) or not quote.strip() or len(quote) > 500 or quote not in record.observation:
                raise ValueError("Evidence must quote an actual successful nonempty result")
    # Explicitly named target concepts cannot be established merely by a
    # substring inside a script/path. This is a generic evidence-anchor gate,
    # not an incident/process allowlist. Missing anchors require more evidence.
    anchors = re.findall(r"\(([A-Z][A-Z0-9-]{1,11})\)", question)
    if anchors:
        cited = "\n".join(ref["quote"] for goal in value['goals'] for ref in goal['evidence'])
        for anchor in anchors:
            full_name = re.search(r"((?:[A-Z][a-z]+\s+){1,5}[A-Z][a-z]+)\s*\(" + re.escape(anchor) + r"\)", question)
            named = full_name and full_name.group(1).lower() in cited.lower()
            if not named and not re.search(r"(?<![\w])" + re.escape(anchor) + r"(?![\w])", cited, re.I):
                raise ValueError(f"Cited evidence does not explicitly establish target concept {anchor}; a script-name substring is insufficient")
    if seen != set(goal_ids):
        raise ValueError("Confirmation must cover every investigation goal")
    return value

VERIFIER_CHALLENGE_PROMPT = """
# 角色定位
你是 Verifier 的独立反证审查。前一判断可能把候选角色误当成目标角色，默认寻找不能提交的理由。
# 工作内容
只根据 question 和原始执行记录判断，不信任候选答案及先前解释。逐个 G 写出证据建立的准确关系。
严格区分“运行了名字像目标行为的脚本”和“被证实实施目标行为的实体”。仅有脚本/命令名或启动记录不能证明脚本功能、行为实施者或题目要求的身份；即使文件名暗示目标行为也不能据此补全关系。
如果换一个可能具有相同上下文关系的候选也能使用相同理由得到确认，说明尚未排除角色混淆，应拒绝。
只有真实行为记录、明确的事件实体关联或经过验证的关系路径能支持确认。不要要求无关的额外调查，但不能把存在候选当作已确认答案。
不能提高题目的证明标准：问“尝试”不要求证明成功；问行为或告警的关联实体，同一事件记录明确列出行为描述与实体即可，不必再证明机制或获得令牌。
例如只有“脚本启动A”不能确定行为主体；若事件记录同时写明目标行为与进程B，则可确认B与该行为的关系。
证据仅引用提供的 excerpt_id，最多每目标3个，不复制或编造原文。
# 输出格式
仅输出 YAML：confirmed、goals、gap、suggestion，格式与限制如下。
confirmed: false
goals: [] # 确认时覆盖全部目标，每项为 {id: G1, relation: "200字符以内", evidence: [{execution_id: E1, excerpt_id: "E1:0"}]}
gap: "拒绝时说明缺口，最多120字符；确认时为空"
suggestion: "拒绝时说明补查关系，最多120字符；确认时为空"
""".strip()


def evidence_excerpts(executions, question, candidate):
    """Bound input size; every excerpt is an exact substring of a real result."""
    terms = set(re.findall(r"[a-zA-Z][a-zA-Z0-9_.-]{2,}", question.lower())) - {
        "the", "and", "this", "that", "can", "you", "name", "process", "identify", "relation"}
    candidate_terms = set(re.findall(r"[a-zA-Z][a-zA-Z0-9_.-]{2,}", candidate.lower()))
    rows, ledger = [], {}
    for e in executions:
        if not e.query_success or e.result_status != 'rows_returned':
            continue
        text = e.observation
        starts = list(range(0, len(text), 350))
        def score(start):
            fragment = text[start:start+480].lower()
            return sum(t in fragment for t in terms) + 3 * sum(t in fragment for t in candidate_terms)
        selected = sorted(set([0] + sorted(starts, key=lambda i: (-score(i), i))[:8]))
        fragments = []
        for start in selected:
            fragment = text[start:start+480]
            if not fragment: continue
            ident = f"{e.execution_id}:{start}"
            ledger[ident] = (e.execution_id, fragment)
            fragments.append({"excerpt_id": ident, "text": fragment})
        rows.append({"execution_id": e.execution_id, "sql": e.sql,
                     "observation_truncated": getattr(e, 'observation_truncated', False),
                     "excerpts_only": len(selected) < len(starts), "excerpts": fragments})
    return rows, ledger
