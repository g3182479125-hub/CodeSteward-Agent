from devpilot.user_intent import (
    capability_confirmation_matches,
    classify_user_intent,
    confirmation_matches,
)


def test_classifies_natural_language_confirmation_intents() -> None:
    assert classify_user_intent("把刚才的修改去掉，恢复原版本").intent == "rollback"
    assert classify_user_intent("按这个方案做，改完跑测试").intent == "apply_verify"
    assert classify_user_intent("这个项目可以信任").intent == "trust_project"
    assert classify_user_intent("启用这个插件吧").intent == "plugin_confirm"
    assert classify_user_intent("接受这次交付").intent == "none"


def test_explicit_negation_never_becomes_authorization() -> None:
    assert classify_user_intent("先别执行，暂时只看看").intent == "none"
    assert classify_user_intent("不要回滚，先保留当前版本").intent == "none"
    assert not confirmation_matches("先别执行", "apply")


def test_bare_acknowledgement_requires_single_pending_context() -> None:
    assert classify_user_intent("好").intent == "ambiguous"
    assert classify_user_intent("好", contextual_intent="rollback").intent == "rollback"


def test_capability_matching_keeps_internal_capability_boundary() -> None:
    assert capability_confirmation_matches("把修改去掉", "r1.rollback")
    assert capability_confirmation_matches("改完跑测试", "r1.apply")
    assert capability_confirmation_matches("改完跑测试", "r2.verify")
    assert not capability_confirmation_matches("验证一下", "r1.rollback")

