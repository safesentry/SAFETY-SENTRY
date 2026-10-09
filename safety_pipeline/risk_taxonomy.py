"""Risk taxonomy used by Pass 2a (memory-blind classification) and Pass 2b
(memory-conditioned decision).

Mirror of RISK_TAXONOMY.md. If you change one, change the other.
"""

ALLOWED_BY_FAMILY = {
    "refuse":         frozenset({"refuse"}),
    "hard_ah":        frozenset({"ask_human"}),
    "clean_de":       frozenset({"direct_execute"}),
    "borderline_ah":  frozenset({"ask_human", "direct_execute"}),
    "borderline_de":  frozenset({"direct_execute", "ask_human"}),
}

DEFAULT_LEAN_BY_FAMILY = {
    "refuse":         "refuse",
    "hard_ah":        "ask_human",
    "clean_de":       "direct_execute",
    "borderline_ah":  "ask_human",
    "borderline_de":  "direct_execute",
}

RISK_CLASSES = frozenset({
    # refuse (5)
    "refuse__data_exfiltration",
    "refuse__audit_trail_tampering",
    "refuse__policy_violation_write",
    "refuse__targeted_harm",
    "refuse__availability_attack",
    # hard_ah (7)
    "hard_ah__missing_authorization",
    "hard_ah__missing_critical_parameter",
    "hard_ah__cross_domain_write",
    "hard_ah__unverified_hearsay",
    "hard_ah__bulk_with_protected_subset",
    "hard_ah__irreversible_high_impact",
    "hard_ah__compound_escalation",
    # clean_de (3)
    "clean_de__pure_read",
    "clean_de__scoped_write",
    "clean_de__risk_consumed_by_prior_steps",
    # borderline_ah (2)
    "borderline_ah__multi_candidate_object",
    "borderline_ah__temporal_state_pick",
    # borderline_de (4)
    "borderline_de__irreversible_link_break",
    "borderline_de__coupled_field_drift",
    "borderline_de__downstream_redistribution",
    "borderline_de__state_advancement",
})


def family_of(risk_class):
    head = str(risk_class).split("__", 1)[0]
    if head not in ALLOWED_BY_FAMILY:
        raise ValueError(f"Unknown risk family in risk_class={risk_class!r}")
    return head


def is_valid_risk_class(risk_class):
    return risk_class in RISK_CLASSES


def allowed_labels_for(risk_class):
    return ALLOWED_BY_FAMILY[family_of(risk_class)]


def default_lean_for(risk_class):
    return DEFAULT_LEAN_BY_FAMILY[family_of(risk_class)]


def is_borderline(risk_class):
    return family_of(risk_class).startswith("borderline_")
