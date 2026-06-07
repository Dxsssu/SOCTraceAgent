from __future__ import annotations

from enum import StrEnum


class MessageType(StrEnum):
    """系统内部使用的结构化消息类型。"""

    USER_MESSAGE = "user_message"
    PLANNER_ANALYSIS_COMPLETED = "planner_analysis_completed"
    TTT_INITIALIZED = "ttt_initialized"
    TTT_UPDATED = "ttt_updated"
    OVERALL_ASSESSMENT_CREATED = "overall_assessment_created"
    LEAF_DISCOVERED = "leaf_discovered"
    LEAF_CLAIMED = "leaf_claimed"
    TOOL_SELECTED = "tool_selected"
    EXECUTION_STARTED = "execution_started"
    EXECUTION_COMPLETED = "execution_completed"
    EXECUTION_FAILED = "execution_failed"
    ROUND_REVIEW_STARTED = "round_review_started"
    ROUND_REVIEW_CREATED = "round_review_created"
    HANDOFF_TO_EXECUTOR = "handoff_to_executor"
    HANDOFF_TO_REVIEWER = "handoff_to_reviewer"
    HANDOFF_TO_PLANNER = "handoff_to_planner"
    SYSTEM_INFO = "system_info"
    SYSTEM_WARNING = "system_warning"
    SYSTEM_ERROR = "system_error"


class RoleName(StrEnum):
    """系统中的 Agent 角色名。"""

    USER = "user"
    PLANNER = "_planner"
    EXECUTOR = "_executor"
    REVIEWER = "_reviewer"
    SYSTEM = "system"


__all__ = ["MessageType", "RoleName"]
