"""Quality issues vs field availability vs capabilities (Workstream 1C)."""

from typing import Any, Literal

Severity = Literal["error", "warning", "info"]


def issue(code: str, severity: Severity, field: str, message: str = "") -> dict[str, Any]:
    item: dict[str, Any] = {"code": code, "severity": severity, "field": field}
    if message:
        item["message"] = message
    return item


def is_defective(issues: list[dict[str, Any]]) -> bool:
    """Presence metadata never counts; only warning/error issues are defective."""
    return any(i.get("severity") in ("warning", "error") for i in issues)


def capabilities_for(
    *,
    has_identity: bool,
    has_ingredients: bool,
    has_instructions: bool,
    quantities_validated: bool,
    servings_known: bool,
    durations_known: bool,
    structural_issue: bool,
) -> dict[str, bool]:
    """Five distinct capability indicators (never conflated).

    - searchable/discoverable: title+ingredients+instructions present.
    - evidence_usable: usable as contextual source evidence (same bar here).
    - quantities_validated: every quantity parsed AND units known per source.
    - complete_eligible: evidence + servings + durations + no structural issue
      + quantities validated. A parsed quantity alone never implies this.
    - scalable: complete + servings numeric + quantities validated.
    Source-reported durations are retained as-is; long CookTime stays a
    reported value, never reinterpreted as active time. Active/resting/
    chilling/freezing splits stay unknown unless the source supports them.
    """
    searchable = bool(has_identity and has_ingredients and has_instructions)
    complete = bool(
        searchable
        and servings_known
        and durations_known
        and not structural_issue
        and quantities_validated
    )
    scalable = bool(complete and servings_known and quantities_validated)
    return {
        "searchable": searchable,
        "evidence_usable": searchable,
        "quantities_validated": bool(searchable and quantities_validated),
        "complete_eligible": complete,
        "scalable": scalable,
    }
