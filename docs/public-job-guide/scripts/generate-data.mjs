import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const SOURCE_ROOT = path.join(ROOT, "data", "raw");
const SNAPSHOT = "2026-09-09";
const CLEAN_URL = "https://job.cleaneye.go.kr/user/ypRecruitment.do";

const jobInput = JSON.parse(await fs.readFile(path.join(SOURCE_ROOT, "jobalio_general_open_20260909.json"), "utf8"));
const cleanInput = JSON.parse(await fs.readFile(path.join(SOURCE_ROOT, "cleaneye_general_open_20260909.json"), "utf8"));
const detailInput = JSON.parse(await fs.readFile(path.join(SOURCE_ROOT, "jobalio_priority_details_20260909.json"), "utf8"));
const detailMap = new Map(detailInput.map((row) => [row.href, row.detail || {}]));

function compact(value, max = 900) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > max ? text.slice(0, max - 1) + "…" : text;
}

function jobDeadline(value) {
  const match = String(value || "").match(/(\d{2})\.(\d{2})\.(\d{2})/);
  return match ? "20" + match[1] + "-" + match[2] + "-" + match[3] : "";
}

function limitation(title, qualification = "") {
  const text = title + " " + qualification;
  if (/장애인?\s*(제한|전형)|장애인 제한경쟁|_장애|\(장애\)|장애 청년/.test(text)) return "장애 관련 제한경쟁";
  if (/보훈\s*(제한|전형)|\(보훈\)|보훈특별/.test(text)) return "보훈 관련 제한경쟁";
  if (/자립준비|고졸\s*(제한|전형)|지역제한|거주지 제한/.test(text)) return "기타 제한경쟁 가능";
  if (/제한경쟁|경력경쟁/.test(text)) return "제한경쟁";
  return "제한 문구 미탐지";
}

function track(title, employment, qualification = "") {
  if (limitation(title, qualification) !== "제한 문구 미탐지") return "제한경쟁·조건부";
  if (/원장|임원|감사 공개|전문의|진료의|교수|박사|변호사|회계사|약사|간호사|경력직|경력사원|전문직원|연구위원/.test(title)) return "경력·전문직";
  if (employment.includes("채용형")) return "신입·채용형 인턴";
  const regular = employment === "정규직" || employment.startsWith("일반정규직");
  if (regular && /신입|신규|대졸수준|일반직|신입행원/.test(title)) return "신입·정규직";
  if (regular) return "정규직(세부 확인)";
  if (employment.includes("체험형") || employment === "인턴" || /청년인턴|체험형 인턴/.test(title)) return "체험형·청년인턴";
  if (employment.includes("무기계약") || /공무직|무기계약/.test(title)) return "무기계약·공무직";
  if (/기간제|대체인력|단기|일용|비정규/.test(title + " " + employment)) return "기간제·비정규";
  return "기타 채용";
}

function fit(title, employment, limit, route, career = "") {
  if (limit !== "제한 문구 미탐지") return "조건 충족자만";
  if (/원장|임원|감사 공개|전문의|진료의|교수|박사|변호사|회계사|약사|간호사|경력직|경력사원|전문직원|연구위원/.test(title)) return "경력·전문 우선";
  if (route === "신입·정규직" || route === "신입·채용형 인턴") return "우선검토";
  if (route === "체험형·청년인턴" && !/장애|보훈|자립준비/.test(title)) return "우선검토";
  if (career.includes("신입") && !/비상임/.test(employment)) return "검토";
  return "세부조건 확인";
}

const jobRows = jobInput.map((raw) => {
  const c = raw.cells || [];
  const detail = detailMap.get(raw.href) || {};
  const meta = detail.meta || {};
  const qualification = detail.qualification || "";
  const limit = limitation(c[2] || "", qualification);
  const route = track(c[2] || "", c[5] || "", qualification);
  return {
    id: "JA-" + (c[1] || ""),
    source: "JOB-ALIO",
    institution: c[3] || "",
    title: c[2] || "",
    region: c[4] || "",
    employment: c[5] || "",
    recruitmentType: meta["채용구분"] || "상세공고 확인",
    regionRestriction: "상세공고 확인",
    start: c[6] || "",
    deadline: jobDeadline(c[7]),
    status: c[8] || "진행중",
    track: route,
    fit: fit(c[2] || "", c[5] || "", limit, route, meta["채용구분"] || ""),
    limitation: limit,
    verified: detailMap.has(raw.href) ? "상세 페이지 확인" : "포털 목록 확인",
    quality: "정상",
    qualification: compact(qualification || "상세공고 확인"),
    process: compact(detail.process || "상세공고 확인"),
    interview: compact(detail.interview_info || "상세공고 확인", 500),
    certificates: (detail.certificate_mentions || []).join(", ") || "상세공고 확인",
    url: raw.href,
  };
});

const keyOf = (row) => [row.institution, row.title, row.start_date, row.end_date].join("|");
const counts = new Map();
for (const row of cleanInput) counts.set(keyOf(row), (counts.get(keyOf(row)) || 0) + 1);

const cleanRows = cleanInput.map((raw, index) => {
  const limit = limitation(raw.title || "", "");
  const route = track(raw.title || "", raw.employment || "", "");
  let quality = "정상";
  if (raw.end_date && raw.end_date < SNAPSHOT) quality = "상태 불일치(마감일 경과)";
  else if ((raw.start_date && raw.start_date < "2025-09-09") || (raw.end_date && raw.end_date > "2027-12-31")) quality = "장기 미마감(원문 확인)";
  else if ((counts.get(keyOf(raw)) || 1) > 1) quality = "포털 중복 " + counts.get(keyOf(raw)) + "건";
  return {
    id: "CE-" + String(index + 1).padStart(3, "0"),
    source: "Cleaneye Job+",
    institution: raw.institution || "",
    title: raw.title || "",
    region: raw.region || "",
    employment: raw.employment || "",
    recruitmentType: raw.career_type || "",
    regionRestriction: raw.region_restriction || "명시 없음(원문 확인)",
    start: raw.start_date || "",
    deadline: raw.end_date || "",
    status: raw.status || "모집중",
    track: route,
    fit: fit(raw.title || "", raw.employment || "", limit, route, raw.career_type || ""),
    limitation: limit,
    verified: "포털 목록 확인",
    quality,
    qualification: "기관 공고 원문 확인",
    process: "기관 공고 원문 확인",
    interview: "기관 공고 원문 확인",
    certificates: "기관 공고 원문 확인",
    url: CLEAN_URL,
  };
});

const all = [...jobRows, ...cleanRows];
const fields = [
  "id", "source", "institution", "title", "region", "employment", "recruitmentType",
  "regionRestriction", "start", "deadline", "status", "track", "fit", "limitation",
  "verified", "quality", "qualification", "process", "interview", "certificates", "url",
];
const csvCell = (value) => '"' + String(value ?? "").replace(/"/g, '""') + '"';
const csv = [fields.join(","), ...all.map((row) => fields.map((field) => csvCell(row[field])).join(","))].join("\n") + "\n";

await fs.mkdir(path.join(ROOT, "data"), { recursive: true });
await fs.writeFile(path.join(ROOT, "data/recruitments-2026-09-09.csv"), csv);
await fs.writeFile(path.join(ROOT, "data/recruitments-2026-09-09.json"), JSON.stringify(all, null, 2));
await fs.writeFile(path.join(ROOT, "site/assets/recruitments.js"), "window.PUBLIC_JOB_DATA=" + JSON.stringify(all) + ";\n");
await fs.writeFile(path.join(ROOT, "data/summary.json"), JSON.stringify({
  snapshot: SNAPSHOT,
  totalRows: all.length,
  jobAlioRows: jobRows.length,
  jobAlioInstitutions: new Set(jobRows.map((row) => row.institution)).size,
  cleaneyeRows: cleanRows.length,
  cleaneyeInstitutions: new Set(cleanRows.map((row) => row.institution)).size,
  cleaneyeUnique: new Set(cleanInput.map(keyOf)).size,
  detailVerified: detailInput.length,
  qualityReview: cleanRows.filter((row) => row.quality !== "정상").length,
}, null, 2));

console.log(JSON.stringify({ rows: all.length, csvBytes: Buffer.byteLength(csv), detailVerified: detailInput.length }));
