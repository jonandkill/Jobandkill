from __future__ import annotations

import re
from typing import Any


MISSING = "[확인 필요]"
MAX_INPUT_LENGTH = 4_000

BLIND_PATTERNS: dict[str, re.Pattern[str]] = {
    "출신학교": re.compile(r"(?:대학교|대학원|고등학교|출신학교|학번)"),
    "가족관계": re.compile(r"(?:부모님|아버지|어머니|형제|자매|배우자|가족관계)"),
    "출신지역": re.compile(r"(?:출신지|고향|태어난 곳)"),
    "연령·성별": re.compile(r"(?:만\s*\d{2}\s*세|남성|여성|군필|미필)"),
}


class DraftValidationError(ValueError):
    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("입력값을 확인해 주세요.")
        self.errors = errors


def _text(value: Any, limit: int = MAX_INPUT_LENGTH) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\x00", " ").split())[:limit]


def _items(value: Any, maximum: int = 12) -> list[str]:
    if isinstance(value, list):
        values = value
    elif isinstance(value, str):
        values = re.split(r"\n+|\s*[;|]\s*", value)
    else:
        values = []
    result: list[str] = []
    for item in values:
        cleaned = _text(item, 1_000)
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result[:maximum]


def normalize_draft(payload: dict[str, Any]) -> dict[str, Any]:
    errors: dict[str, str] = {}
    document_type = _text(payload.get("document_type"), 30)
    style = _text(payload.get("style"), 30)
    if document_type not in {"career", "experience"}:
        errors["document_type"] = "경력기술서 또는 경험기술서를 선택해 주세요."
    if style not in {"bullet", "narrative"}:
        errors["style"] = "개조식 또는 스토리텔링을 선택해 주세요."
    if payload.get("facts_confirmed") is not True:
        errors["facts_confirmed"] = "입력한 내용이 실제 경험이라는 확인이 필요합니다."
    try:
        target_length = int(payload.get("target_length", 800))
    except (TypeError, ValueError):
        target_length = 800
    target_length = min(max(target_length, 300), 2_000)
    if errors:
        raise DraftValidationError(errors)
    return {
        "document_type": document_type,
        "style": style,
        "target_length": target_length,
        "institution": _text(payload.get("institution"), 300),
        "target_job": _text(payload.get("target_job"), 300),
        "ncs_path": _text(payload.get("ncs_path"), 1_000),
        "matched_duty": _text(payload.get("matched_duty"), 1_000),
        "matched_skill": _text(payload.get("matched_skill"), 1_000),
        "experience_title": _text(payload.get("experience_title"), 300),
        "organization": _text(payload.get("organization"), 300),
        "period_start": _text(payload.get("period_start"), 30),
        "period_end": _text(payload.get("period_end"), 30),
        "role": _text(payload.get("role"), 500),
        "situation": _text(payload.get("situation")),
        "objective": _text(payload.get("objective")),
        "judgment": _text(payload.get("judgment")),
        "actions": _items(payload.get("actions")),
        "tools": _items(payload.get("tools")),
        "collaboration": _text(payload.get("collaboration")),
        "result": _text(payload.get("result")),
        "evidence": _text(payload.get("evidence")),
        "contribution": _text(payload.get("contribution")),
        "learning": _text(payload.get("learning")),
    }


def _or_missing(value: str) -> str:
    return value or MISSING


def _period(draft: dict[str, Any]) -> str:
    start, end = draft["period_start"], draft["period_end"]
    if start and end:
        return f"{start}~{end}"
    return start or end or MISSING


def _job_link(draft: dict[str, Any]) -> str:
    target = draft["target_job"] or MISSING
    parts: list[str] = []
    if draft["matched_duty"]:
        parts.append(f"직무수행내용 ‘{draft['matched_duty']}’")
    if draft["matched_skill"]:
        parts.append(f"필요기술 ‘{draft['matched_skill']}’")
    if not parts and draft["ncs_path"]:
        parts.append(f"NCS 분야 ‘{draft['ncs_path']}’")
    basis = "과 연결됩니다" if not parts else f"의 {', '.join(parts)}와 연결됩니다"
    return f"이 경험은 {target} {basis}."


def _bullet(draft: dict[str, Any]) -> str:
    actions = draft["actions"] or [MISSING]
    action_lines = "\n".join(f"  - {item}" for item in actions)
    tools = ", ".join(draft["tools"]) or MISSING
    heading = "경력" if draft["document_type"] == "career" else "경험"
    return "\n".join(
        [
            f"[{heading} | {_or_missing(draft['experience_title'])}]",
            f"- 기간: {_period(draft)}",
            f"- 소속·역할: {_or_missing(draft['organization'])} / {_or_missing(draft['role'])}",
            f"- 상황: {_or_missing(draft['situation'])}",
            f"- 목표: {_or_missing(draft['objective'])}",
            f"- 판단 기준: {_or_missing(draft['judgment'])}",
            "- 수행:",
            action_lines,
            f"- 활용 도구: {tools}",
            f"- 협업: {_or_missing(draft['collaboration'])}",
            f"- 결과: {_or_missing(draft['result'])}",
            f"- 결과 근거: {_or_missing(draft['evidence'])}",
            f"- 본인 기여: {_or_missing(draft['contribution'])}",
            f"- 직무 연결: {_job_link(draft)}",
        ]
    )


def _narrative(draft: dict[str, Any]) -> str:
    title = draft["experience_title"] or "해당 경험"
    if draft["document_type"] == "career":
        opening = (
            f"{_or_missing(draft['organization'])}에서 {_period(draft)} 동안 ‘{title}’ 업무를 담당했습니다. "
            f"주요 책임은 {_or_missing(draft['role'])}이었습니다."
        )
    else:
        opening = (
            f"{_or_missing(draft['organization'])}에서 {_period(draft)} 동안 "
            f"‘{title}’의 {_or_missing(draft['role'])} 역할을 맡았습니다."
        )
    situation = (
        f"당시 {_or_missing(draft['situation'])} 상황에서 {_or_missing(draft['objective'])}을 목표로 삼았습니다. "
        f"저는 {_or_missing(draft['judgment'])}을 기준으로 접근 방향을 정했습니다."
    )
    if draft["actions"]:
        labels = ("먼저", "다음으로", "이어서", "마지막으로")
        action_sentences = []
        for index, action in enumerate(draft["actions"]):
            label = labels[index] if index < len(labels) else "또한"
            action_sentences.append(f"{label} {action.rstrip('.')}했습니다.")
        actions = " ".join(action_sentences)
    else:
        actions = f"구체적인 행동은 {MISSING}입니다."
    if draft["tools"]:
        actions += f" 이 과정에서 {', '.join(draft['tools'])}을 활용했습니다."
    collaboration = (
        f" 협업 과정에서는 {draft['collaboration'].rstrip('.')}했습니다."
        if draft["collaboration"] else f" 협업 방식은 {MISSING}입니다."
    )
    outcome = (
        f"그 결과 {_or_missing(draft['result'])}을 만들었습니다. "
        f"이를 확인할 수 있는 근거는 {_or_missing(draft['evidence'])}입니다. "
        f"팀의 전체 결과와 구분해 제가 직접 책임진 범위는 {_or_missing(draft['contribution'])}입니다."
    )
    learning = f" 이 경험을 통해 {draft['learning'].rstrip('.')}을 배웠습니다." if draft["learning"] else ""
    return " ".join((opening, situation, actions + collaboration, outcome, _job_link(draft) + learning))


def _warnings(draft: dict[str, Any], output: str) -> tuple[list[str], list[str]]:
    required = {
        "experience_title": "경험·업무명",
        "organization": "소속",
        "role": "본인 역할",
        "situation": "상황",
        "objective": "목표",
        "judgment": "판단 기준",
        "actions": "구체적 행동",
        "result": "결과",
        "evidence": "결과 근거",
        "contribution": "본인 기여 범위",
    }
    missing = [label for key, label in required.items() if not draft[key]]
    warnings: list[str] = []
    if missing:
        warnings.append("비어 있는 사실은 문서에 [확인 필요]로 표시했습니다. 수치나 성과를 임의로 만들지 않았습니다.")
    combined = " ".join(
        value for value in draft.values() if isinstance(value, str)
    )
    blind_hits = [label for label, pattern in BLIND_PATTERNS.items() if pattern.search(combined)]
    if blind_hits:
        warnings.append("블라인드 채용에서 제한될 수 있는 정보가 감지되었습니다: " + ", ".join(blind_hits))
    if draft["result"] and not draft["evidence"]:
        warnings.append("결과를 뒷받침하는 수치·문서·피드백 등 확인 근거를 추가해 주세요.")
    if draft["contribution"] and re.search(r"(?:전부|모든|100%)", draft["contribution"]):
        warnings.append("팀 성과 전체와 본인이 직접 수행한 범위를 다시 구분해 주세요.")
    target = draft["target_length"]
    if len(output) > target:
        warnings.append(f"현재 초안은 {len(output):,}자입니다. 목표 {target:,}자에 맞추려면 세부 내용을 줄여 주세요.")
    elif len(output) < target * 0.55:
        warnings.append(f"현재 초안은 {len(output):,}자입니다. 목표 분량에 맞게 판단 이유와 행동 근거를 더 적어 주세요.")
    return warnings, missing


def compose(payload: dict[str, Any]) -> dict[str, Any]:
    draft = normalize_draft(payload)
    output = _bullet(draft) if draft["style"] == "bullet" else _narrative(draft)
    warnings, missing = _warnings(draft, output)
    completed = 10 - len(missing)
    return {
        "output": output,
        "warnings": warnings,
        "missing_fields": missing,
        "fact_coverage": max(0, round(completed / 10 * 100)),
        "character_count": len(output),
        "requested_character_count": draft["target_length"],
        "policy": "입력된 사실만 재구성하며, 없는 사실은 [확인 필요]로 남깁니다.",
    }
