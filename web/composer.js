"use strict";

// Deterministic browser counterpart of jobandkill/writer.py. This module has
// no network, storage or DOM dependencies; output is always plain text.
(function () {
  const MISSING = "[확인 필요]";
  const PYTHON_SPACE = "[\\u0009-\\u000d\\u001c-\\u0020\\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000]";
  const SPACE_RUN = new RegExp(`${PYTHON_SPACE}+`, "u");
  const ITEM_SEPARATOR = new RegExp(`\\n+|${PYTHON_SPACE}*[;|]${PYTHON_SPACE}*`, "u");
  const TEXT_LIMITS = {
    institution: 300, target_job: 300, ncs_path: 1000, matched_duty: 1000, matched_skill: 1000,
    reference_catalog_id: 64, reference_source_url: 200, reference_name: 100, reference_version: 30,
    experience_title: 300, organization: 300, period_start: 30, period_end: 30, role: 500,
    situation: 4000, objective: 4000, judgment: 4000, collaboration: 4000, result: 4000,
    evidence: 4000, contribution: 4000, learning: 4000
  };
  const REFERENCE_SOURCES = {
    "https://www.data.go.kr/data/15150267/openapi.do": "NCS 능력단위 공통정보",
    "https://www.data.go.kr/data/15083321/fileData.do": "NCS 훈련기준 파일"
  };
  const REFERENCE_FIELDS = ["reference_catalog_id", "reference_source_url", "reference_name", "reference_version"];
  const REFERENCE_VERSION = /^(?:[0-9]{1,4}(?:v[0-9]{1,3})?|[0-9]{4}-[0-9]{2}-[0-9]{2}|미제공)$/u;
  const RESIDENT_REGISTRATION_NUMBER = /(?<!\p{Decimal_Number})\p{Decimal_Number}{6}\s*-?\s*[1-8]\p{Decimal_Number}{6}(?!\p{Decimal_Number})/u;
  const BLIND_PATTERNS = [
    ["출신학교", /(?:대학교|대학원|고등학교|출신학교|학번)/u],
    ["가족관계", /(?:부모님|아버지|어머니|형제|자매|배우자|가족관계)/u],
    ["출신지역", /(?:출신지|고향|태어난 곳)/u],
    ["연령·성별", new RegExp(`(?:만${PYTHON_SPACE}*\\p{Decimal_Number}{2}${PYTHON_SPACE}*세|남성|여성|군필|미필)`, "u")]
  ];

  class DraftValidationError extends Error {
    constructor(errors) {
      super("입력값을 확인해 주세요.");
      this.name = "DraftValidationError";
      this.errors = errors;
    }
  }

  // Python's writer counts Unicode code points, not JavaScript UTF-16 units.
  const characters = value => Array.from(value);
  const countCharacters = value => characters(value).length;
  const formatCount = value => String(value).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const orMissing = value => value || MISSING;

  function pythonString(value, nested = false) {
    if (value === null || value === undefined) return "None";
    if (value === true) return "True";
    if (value === false) return "False";
    if (typeof value === "string") {
      if (!nested) return value;
      const quote = value.includes("'") && !value.includes('"') ? '"' : "'";
      const escaped = characters(value).map(character => {
        if (character === "\\" || character === quote) return "\\" + character;
        if (character === "\n") return "\\n";
        if (character === "\r") return "\\r";
        if (character === "\t") return "\\t";
        if (character !== " " && /[\p{C}\p{Z}]/u.test(character)) {
          const point = character.codePointAt(0);
          return point <= 255 ? "\\x" + point.toString(16).padStart(2, "0")
            : point <= 65535 ? "\\u" + point.toString(16).padStart(4, "0")
              : "\\U" + point.toString(16).padStart(8, "0");
        }
        return character;
      }).join("");
      return quote + escaped + quote;
    }
    if (Array.isArray(value)) return "[" + value.map(item => pythonString(item, true)).join(", ") + "]";
    if (typeof value === "object") {
      return "{" + Object.entries(value).map(([key, item]) => `${pythonString(key, true)}: ${pythonString(item, true)}`).join(", ") + "}";
    }
    if (typeof value === "number" && value !== 0 && Math.abs(value) < 0.0001) {
      return value.toExponential().replace(/e([+-])(\d)$/u, "e$10$2");
    }
    return String(value);
  }

  function text(value, limit = 4000) {
    if (value === null || value === undefined) return "";
    const normalized = pythonString(value).replace(/\x00/g, " ").split(SPACE_RUN).filter(Boolean).join(" ");
    return characters(normalized).slice(0, limit).join("");
  }

  function itemValues(value) {
    return Array.isArray(value) ? value : typeof value === "string" ? value.split(ITEM_SEPARATOR) : [];
  }

  function items(value, maximum = 12) {
    const values = itemValues(value);
    const result = [];
    for (const item of values) {
      const cleaned = text(item, 1000);
      if (cleaned && !result.includes(cleaned)) result.push(cleaned);
    }
    return result.slice(0, maximum);
  }

  function targetLength(value) {
    let parsed = 800;
    if (typeof value === "number" && Number.isFinite(value)) parsed = Math.trunc(value);
    else if (typeof value === "boolean") parsed = Number(value);
    else if (typeof value === "string") {
      // Python int accepts Unicode decimal digits, including full-width forms.
      const decimal = value.replace(/\p{Decimal_Number}/gu, character => {
        const point = character.codePointAt(0);
        let zero = point;
        while (/\p{Decimal_Number}/u.test(String.fromCodePoint(zero - 1))) zero -= 1;
        return String((point - zero) % 10);
      });
      const trimmed = decimal.split(SPACE_RUN).filter(Boolean);
      if (trimmed.length === 1 && /^[+-]?\d(?:_?\d)*$/u.test(trimmed[0])) {
        parsed = Number(trimmed[0].replace(/_/g, ""));
      }
    }
    return Math.min(Math.max(parsed, 300), 2000);
  }

  function sanitizeDraft(payload, requireConfirmation = false) {
    const errors = {};
    const documentType = text(payload.document_type, 30);
    const style = text(payload.style, 30);
    if (!["career", "experience"].includes(documentType)) errors.document_type = "경력기술서 또는 경험기술서를 선택해 주세요.";
    if (!["bullet", "narrative"].includes(style)) errors.style = "개조식 또는 스토리텔링을 선택해 주세요.";
    if (requireConfirmation && payload.facts_confirmed !== true) errors.facts_confirmed = "입력한 내용이 실제 경험이라는 확인이 필요합니다.";
    for (const [field, limit] of Object.entries(TEXT_LIMITS)) {
      if (countCharacters(text(payload[field], Infinity)) > limit) errors[field] = `${field} 입력은 ${formatCount(limit)}자 이하여야 합니다.`;
    }
    const reference = Object.fromEntries(REFERENCE_FIELDS.map(field => [field, payload[field] ?? ""]));
    if (Object.values(reference).some(value => value !== "")) {
      if (Object.values(reference).some(value => typeof value !== "string" || !value)) {
        errors.reference_catalog = "NCS 참고자료의 출처 정보를 함께 확인해 주세요.";
      } else {
        if (reference.reference_catalog_id.length !== 64 || !/^[a-f0-9]{64}$/u.test(reference.reference_catalog_id)) errors.reference_catalog_id = "NCS 참고자료 기록이 올바르지 않습니다.";
        const sourceName = Object.hasOwn(REFERENCE_SOURCES, reference.reference_source_url) ? REFERENCE_SOURCES[reference.reference_source_url] : null;
        if (!sourceName) errors.reference_source_url = "등록된 정부 자료의 공식 출처 주소만 사용할 수 있습니다.";
        else if (reference.reference_name !== sourceName) errors.reference_name = "NCS 참고자료 이름과 공식 출처가 일치하지 않습니다.";
        if (!REFERENCE_VERSION.test(reference.reference_version) || /\s/u.test(reference.reference_version)) errors.reference_version = "NCS 참고자료의 기준 버전·일자를 확인해 주세요.";
      }
    }
    for (const field of ["actions", "tools"]) {
      const values = itemValues(payload[field]);
      if (values.length > 12) errors[field] = `${field} 항목은 최대 12개까지 입력할 수 있습니다.`;
      else if (values.some(item => countCharacters(text(item, Infinity)) > 1000)) errors[field] = `${field}의 각 항목은 1,000자 이하여야 합니다.`;
    }
    const sensitiveText = [
      ...Object.keys(TEXT_LIMITS).filter(field => field !== "reference_catalog_id").map(field => text(payload[field], Infinity)),
      ...["actions", "tools"].flatMap(field => itemValues(payload[field]).map(item => text(item, Infinity)))
    ].join(" ");
    if (RESIDENT_REGISTRATION_NUMBER.test(sensitiveText.normalize("NFKC"))) errors.personal_information = "주민등록번호는 입력하거나 저장할 수 없습니다.";
    if (Object.keys(errors).length) throw new DraftValidationError(errors);
    return {
      document_type: documentType, style, target_length: targetLength(payload.target_length),
      institution: text(payload.institution, 300), target_job: text(payload.target_job, 300),
      ncs_path: text(payload.ncs_path, 1000), matched_duty: text(payload.matched_duty, 1000),
      matched_skill: text(payload.matched_skill, 1000), experience_title: text(payload.experience_title, 300),
      ...Object.fromEntries(REFERENCE_FIELDS.map(field => [field, text(payload[field], TEXT_LIMITS[field])])),
      organization: text(payload.organization, 300), period_start: text(payload.period_start, 30),
      period_end: text(payload.period_end, 30), role: text(payload.role, 500),
      situation: text(payload.situation), objective: text(payload.objective), judgment: text(payload.judgment),
      actions: items(payload.actions), tools: items(payload.tools), collaboration: text(payload.collaboration),
      result: text(payload.result), evidence: text(payload.evidence), contribution: text(payload.contribution),
      learning: text(payload.learning), facts_confirmed: payload.facts_confirmed === true
    };
  }

  function period(draft) {
    return draft.period_start && draft.period_end ? `${draft.period_start}~${draft.period_end}`
      : draft.period_start || draft.period_end || MISSING;
  }

  function jobLink(draft) {
    const target = draft.target_job || MISSING;
    const parts = [];
    if (draft.matched_duty) parts.push(`직무수행내용 ‘${draft.matched_duty}’`);
    if (draft.matched_skill) parts.push(`필요기술 ‘${draft.matched_skill}’`);
    if (!parts.length && draft.ncs_path) parts.push(`NCS 분야 ‘${draft.ncs_path}’`);
    const basis = parts.length ? `의 ${parts.join(", ")}와 연결됩니다` : "과 연결됩니다";
    return `이 경험은 ${target} ${basis}.`;
  }

  function bullet(draft) {
    const actions = draft.actions.length ? draft.actions : [MISSING];
    const heading = draft.document_type === "career" ? "경력" : "경험";
    return [
      `[${heading} | ${orMissing(draft.experience_title)}]`,
      `- 기간: ${period(draft)}`,
      `- 소속·역할: ${orMissing(draft.organization)} / ${orMissing(draft.role)}`,
      `- 상황: ${orMissing(draft.situation)}`,
      `- 목표: ${orMissing(draft.objective)}`,
      `- 판단 기준: ${orMissing(draft.judgment)}`,
      "- 수행:", actions.map(item => `  - ${item}`).join("\n"),
      `- 활용 도구: ${draft.tools.join(", ") || MISSING}`,
      `- 협업: ${orMissing(draft.collaboration)}`,
      `- 결과: ${orMissing(draft.result)}`,
      `- 결과 근거: ${orMissing(draft.evidence)}`,
      `- 본인 기여: ${orMissing(draft.contribution)}`,
      `- 직무 연결: ${jobLink(draft)}`
    ].join("\n");
  }

  function narrative(draft) {
    const title = draft.experience_title || "해당 경험";
    const opening = draft.document_type === "career"
      ? `${orMissing(draft.organization)}에서 ${period(draft)} 동안 ‘${title}’ 업무를 담당했습니다. 주요 책임은 ${orMissing(draft.role)}이었습니다.`
      : `${orMissing(draft.organization)}에서 ${period(draft)} 동안 ‘${title}’의 ${orMissing(draft.role)} 역할을 맡았습니다.`;
    const situation = `당시 ${orMissing(draft.situation)} 상황에서 ${orMissing(draft.objective)}을 목표로 삼았습니다. 저는 ${orMissing(draft.judgment)}을 기준으로 접근 방향을 정했습니다.`;
    const labels = ["먼저", "다음으로", "이어서", "마지막으로"];
    let actions = draft.actions.length
      ? draft.actions.map((action, index) => `${labels[index] || "또한"} ${action.replace(/\.+$/u, "")}했습니다.`).join(" ")
      : `구체적인 행동은 ${MISSING}입니다.`;
    if (draft.tools.length) actions += ` 이 과정에서 ${draft.tools.join(", ")}을 활용했습니다.`;
    const collaboration = draft.collaboration
      ? ` 협업 과정에서는 ${draft.collaboration.replace(/\.+$/u, "")}했습니다.` : ` 협업 방식은 ${MISSING}입니다.`;
    const outcome = `그 결과 ${orMissing(draft.result)}을 만들었습니다. 이를 확인할 수 있는 근거는 ${orMissing(draft.evidence)}입니다. 팀의 전체 결과와 구분해 제가 직접 책임진 범위는 ${orMissing(draft.contribution)}입니다.`;
    const learning = draft.learning ? ` 이 경험을 통해 ${draft.learning.replace(/\.+$/u, "")}을 배웠습니다.` : "";
    return [opening, situation, actions + collaboration, outcome, jobLink(draft) + learning].join(" ");
  }

  function warnings(draft, output) {
    const required = {
      experience_title: "경험·업무명", organization: "소속", role: "본인 역할", situation: "상황", objective: "목표",
      judgment: "판단 기준", actions: "구체적 행동", result: "결과", evidence: "결과 근거", contribution: "본인 기여 범위"
    };
    const missing = Object.entries(required).filter(([key]) => !draft[key].length).map(([, label]) => label);
    const messages = [];
    if (missing.length) messages.push("비어 있는 사실은 문서에 [확인 필요]로 표시했습니다. 수치나 성과를 임의로 만들지 않았습니다.");
    const combined = Object.values(draft).filter(value => typeof value === "string").join(" ");
    const blindHits = BLIND_PATTERNS.filter(([, pattern]) => pattern.test(combined)).map(([label]) => label);
    if (blindHits.length) messages.push("블라인드 채용에서 제한될 수 있는 정보가 감지되었습니다: " + blindHits.join(", "));
    if (draft.result && !draft.evidence) messages.push("결과를 뒷받침하는 수치·문서·피드백 등 확인 근거를 추가해 주세요.");
    if (draft.contribution && /(?:전부|모든|100%)/u.test(draft.contribution)) messages.push("팀 성과 전체와 본인이 직접 수행한 범위를 다시 구분해 주세요.");
    const count = countCharacters(output);
    if (count > draft.target_length) messages.push(`현재 초안은 ${formatCount(count)}자입니다. 목표 ${formatCount(draft.target_length)}자에 맞추려면 세부 내용을 줄여 주세요.`);
    else if (count < draft.target_length * 0.55) messages.push(`현재 초안은 ${formatCount(count)}자입니다. 목표 분량에 맞게 판단 이유와 행동 근거를 더 적어 주세요.`);
    return { messages, missing };
  }

  function compose(payload) {
    const draft = sanitizeDraft(payload, true);
    delete draft.facts_confirmed;
    let output = draft.style === "bullet" ? bullet(draft) : narrative(draft);
    if (draft.reference_catalog_id) output += "\n\n" + [
      "[NCS 참고자료 — 기관별 채용요건 아님]",
      `제공: 한국산업인력공단 / 자료: ${draft.reference_name} / 기준 버전·일자: ${draft.reference_version}`,
      `출처: ${draft.reference_source_url}`,
      `참고 기록: ${draft.reference_catalog_id}`,
      "직무 이해를 위한 공통 기준이며, 사용자의 경험·성과를 증명하는 자료가 아닙니다."
    ].join("\n");
    const { messages, missing } = warnings(draft, output);
    return {
      output, warnings: messages, missing_fields: missing, fact_coverage: (10 - missing.length) * 10,
      character_count: countCharacters(output), requested_character_count: draft.target_length,
      policy: "입력된 사실만 재구성하며, 없는 사실은 [확인 필요]로 남깁니다."
    };
  }

  function catalogReference(item) {
    const expected = { "ncs-common": "NCS 능력단위 공통정보", "ncs-training-2025": "NCS 훈련기준 파일" };
    if (!Object.hasOwn(expected, item.source_slug) || REFERENCE_SOURCES[item.source_url] !== expected[item.source_slug]) return null;
    const proposed = String(item.version || "");
    const sourceDate = String(item.source_updated_at || "").slice(0, 10);
    const version = REFERENCE_VERSION.test(proposed) && !/\s/u.test(proposed) ? proposed
      : /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/u.test(sourceDate) ? sourceDate : "미제공";
    const reference = {
      reference_catalog_id: item.id, reference_source_url: item.source_url,
      reference_name: expected[item.source_slug], reference_version: version
    };
    try {
      sanitizeDraft({ document_type: "career", style: "bullet", ...reference });
      return reference;
    } catch { return null; }
  }

  const api = Object.freeze({ compose, sanitizeDraft, catalogReference, DraftValidationError });
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else globalThis.JobAndKillComposer = api;
})();
