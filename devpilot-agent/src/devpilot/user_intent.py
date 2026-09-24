"""Natural-language intent recognition at user-confirmation boundaries.

This module deliberately returns an intent, not an authorization. Callers must
still bind the result to the current ChangeSet, project, signature and
capability before doing anything irreversible.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

UserIntent = Literal[
    "apply",
    "apply_verify",
    "verify",
    "rollback",
    "stop",
    "trust_project",
    "plugin_confirm",
    "none",
    "ambiguous",
]
IntentConfidence = Literal["explicit", "contextual", "none"]


@dataclass(frozen=True, slots=True)
class IntentResult:
    intent: UserIntent
    confidence: IntentConfidence
    reason: str = ""

    @property
    def explicit(self) -> bool:
        return self.intent not in {"none", "ambiguous"}


_ALLOWED_INTENTS = frozenset(
    {
        "apply",
        "apply_verify",
        "verify",
        "rollback",
        "stop",
        "trust_project",
        "plugin_confirm",
    }
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).casefold())


def _contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def classify_user_intent(
    text: str,
    *,
    allowed_intents: set[str] | frozenset[str] | None = None,
    contextual_intent: UserIntent | None = None,
) -> IntentResult:
    """Classify a user's natural-language confirmation.

    The classifier is intentionally conservative. It handles common Chinese
    and English confirmations, rejects explicit negation before action words,
    and only interprets a bare acknowledgement when the caller supplies a
    single pending contextual intent.
    """

    normalized = _normalize(text)
    allowed = set(allowed_intents or _ALLOWED_INTENTS)
    if not normalized:
        return IntentResult("none", "none", "empty")

    # Negation is checked before positive keywords so “不要回滚” can never
    # accidentally become a rollback confirmation.
    if _contains_any(
        normalized,
        (
            "先别执行",
            "暂时别执行",
            "不要执行",
            "不执行",
            "先不要执行",
            "不要改",
            "先别改",
            "暂时不要改",
            "只看看不要改",
            "只是看看",
            "不要验证",
            "不要仅应用",
            "不要只应用",
            "先不要仅应用",
            "先不要只应用",
            "不要回滚",
            "不要撤回",
            "不要撤销",
            "donotapply",
            "dontapply",
            "donotrollback",
            "dontrollback",
        ),
    ):
        return IntentResult("none", "none", "explicit_negation")

    # These phrases intentionally describe removal of the current change,
    # rather than negating a rollback request.
    if _contains_any(
        normalized,
        (
            "把这次修改去掉",
            "把修改去掉",
            "去掉刚才的修改",
            "去除这次修改",
            "恢复原样",
            "恢复原版本",
            "退回原版本",
            "回到修改前",
            "撤回这次修改",
            "撤销这次修改",
            "回滚这次修改",
            "revertthischange",
            "rollbackthischange",
        ),
    ):
        candidates = {"rollback"}
    else:
        candidates: set[str] = set()
        if _contains_any(
            normalized,
            (
                "改完跑测试",
                "修改并验证",
                "修好并验证",
                "执行并验证",
                "直接修复并确认",
                "applyandverify",
                "fixandtest",
                "执行",
                "直接执行",
            ),
        ):
            candidates.add("apply_verify")
        if _contains_any(
            normalized,
            (
                "撤回",
                "撤销",
                "回滚",
                "rollback",
                "revert",
            ),
        ):
            candidates.add("rollback")
        if _contains_any(
            normalized,
            (
                "验证一下",
                "跑测试",
                "跑一下测试",
                "测试确认",
                "检查修复结果",
                "验证修复",
                "verify",
                "run test",
            ),
        ):
            candidates.add("verify")
        if _contains_any(
            normalized,
            (
                "可以改",
                "仅应用",
                "只应用",
                "按这个方案做",
                "开始修复",
                "开始修改",
                "应用这个改动",
                "apply",
            ),
        ):
            candidates.add("apply")
        if _contains_any(normalized, ("停止交付", "停止", "不继续了", "先到这里", "stop")):
            candidates.add("stop")
        if _contains_any(
            normalized,
            ("这个项目可以信任", "允许这个项目", "信任项目", "trust", "trustproject"),
        ):
            candidates.add("trust_project")
        if _contains_any(
            normalized,
            (
                "启用这个插件",
                "可以试用这个插件",
                "确认试用",
                "确认启用",
                "确认插件",
                "执行试用",
                "confirmplugin",
                "approveplugin",
            ),
        ):
            candidates.add("plugin_confirm")

    candidates &= allowed
    if "apply_verify" in candidates:
        if "rollback" in candidates:
            return IntentResult("ambiguous", "none", "conflicting_intents")
        candidates = {"apply_verify"}
    if len(candidates) > 1:
        # apply_verify is a deliberate combination, not an accidental conflict
        # between separate apply and verify phrases.
        if candidates == {"apply", "verify", "apply_verify"}:
            return IntentResult("apply_verify", "explicit", "combined_action")
        return IntentResult("ambiguous", "none", "conflicting_intents")
    if candidates:
        return IntentResult(next(iter(candidates)), "explicit", "matched_natural_language")  # type: ignore[arg-type]

    if normalized in {"好", "行", "可以", "好的", "yes", "ok", "confirmed"}:
        if contextual_intent in allowed and contextual_intent not in {"none", "ambiguous"}:
            return IntentResult(contextual_intent, "contextual", "single_pending_action")
        return IntentResult("ambiguous", "none", "bare_acknowledgement")

    return IntentResult("none", "none", "no_matching_intent")


def confirmation_matches(
    text: str,
    required_intent: str,
    *,
    contextual_intent: UserIntent | None = None,
) -> bool:
    """Return whether text explicitly confirms the required action."""

    if required_intent not in _ALLOWED_INTENTS:
        return False
    if _normalize(text) == "confirmed":
        return True
    allowed = {required_intent}
    # The historical chat command “执行” represented the bounded
    # apply-and-verify path.  Let that combined intent pass through the
    # compatibility boundary for either legacy apply or verify callers; the
    # capability-specific control plane still binds the final action.
    if required_intent in {"apply", "verify"}:
        allowed.add("apply_verify")
    result = classify_user_intent(
        text,
        allowed_intents=allowed,
        contextual_intent=contextual_intent,
    )
    if result.intent == required_intent:
        return True
    # “执行” historically meant the bounded apply-and-verify path. Keep that
    # compatibility while allowing the newer, more descriptive phrases.
    return required_intent in {"apply", "verify"} and result.intent == "apply_verify"


def capability_confirmation_matches(
    text: str,
    capability: str,
    *,
    contextual_intent: UserIntent | None = None,
) -> bool:
    """Map a control-plane capability to the allowed user intents."""

    expected = {
        "r1.apply": {"apply", "apply_verify"},
        "r2.verify": {"verify", "apply_verify"},
        "r1.rollback": {"rollback"},
    }.get(capability)
    if expected is None:
        return False
    if _normalize(text) == "confirmed":
        return True
    result = classify_user_intent(
        text,
        allowed_intents=expected,
        contextual_intent=contextual_intent,
    )
    return result.intent in expected
