"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");
const vm = require("node:vm");
const composer = require("../web/composer.js");
const root = path.resolve(__dirname, "..");
const composerSource = readFileSync(path.join(root, "web/composer.js"), "utf8");
const appSource = readFileSync(path.join(root, "web/app.js"), "utf8");

const base = {
  document_type: "experience", style: "narrative", target_length: 800,
  institution: "한국테스트공사", target_job: "사무행정", experience_title: "민원 처리 기준 정비",
  organization: "고객지원팀 인턴", period_start: "2025-03", period_end: "2025-08",
  role: "민원 자료 분류와 개선안 초안 작성", situation: "담당자마다 분류 기준이 달라 집계가 지연되는",
  objective: "공통 분류 기준을 마련하는 것", judgment: "반복 민원을 먼저 수치로 확인해야 한다는 점",
  actions: ["민원 240건을 유형별로 분류", "담당자 검토를 받아 분류표를 보완"],
  tools: ["Excel 피벗테이블"], collaboration: "담당자에게 예외 사례를 확인",
  result: "월간 집계에 같은 분류표가 사용되는 변화", evidence: "최종 분류표와 월간 보고서",
  contribution: "초기 분류와 초안을 직접 담당하고 최종 승인은 팀장에게 받음",
  learning: "현장 예외까지 확인해야 기준이 정착된다는 점", facts_confirmed: true
};
const referenceBase = {
  reference_catalog_id: "a".repeat(64),
  reference_source_url: "https://www.data.go.kr/data/15083321/fileData.do",
  reference_name: "NCS 훈련기준 파일", reference_version: "2025-12-31"
};

function pythonResults(payloads) {
  const script = [
    "import json, sys",
    "from jobandkill.writer import compose, sanitize_draft, DraftValidationError",
    "results = []",
    "for payload in json.load(sys.stdin):",
    "    try:",
    "        results.append({'result': compose(payload), 'sanitized': sanitize_draft(payload)})",
    "    except DraftValidationError as error:",
    "        results.append({'errors': error.errors, 'message': str(error)})",
    "json.dump(results, sys.stdout)"
  ].join("\n");
  const child = spawnSync(process.env.PYTHON || "python", ["-c", script], {
    cwd: root, input: JSON.stringify(payloads), encoding: "utf8", maxBuffer: 16 * 1024 * 1024
  });
  assert.equal(child.status, 0, child.stderr || String(child.error));
  return JSON.parse(child.stdout);
}

test("browser compose matches the Python writer across formats, missing facts and normalization", () => {
  const variations = [
    {}, { target_length: 300 }, { target_length: 2000 }, { target_length: 9000 },
    { target_length: "8_00" }, { target_length: "800.5" }, { target_length: "  +500  " },
    { target_length: "８００" }, { target_length: "١٥٠٠" },
    { target_length: null }, { target_length: true }, { target_length: 450.9 },
    { period_start: "", period_end: "" }, { period_start: "" }, { period_end: "" },
    { matched_duty: "자료 관리", matched_skill: "정확한 분류", ncs_path: "미사용" },
    { ncs_path: "경영 · 회계 · 사무" },
    { target_job: "", organization: "", evidence: "", contribution: "", actions: [], result: "" },
    { actions: "분류;  검토 | 검토\n보고", tools: "Excel|Excel; SQL\n문서" },
    { actions: ["행동...", "둘.", "셋", "넷", "다섯"], collaboration: "협업...", learning: "판단..." },
    { actions: [...Array(15)].map((_, index) => `행동 ${index}`), tools: ["  Excel ", "Excel", null, "", false, 123] },
    { experience_title: "  민원\x00 정리\n\t개선  ", situation: "\u001c먼저\u0085확인\u2009수행\u3000완료\ufeff" },
    { organization: "테스트대학교", situation: "부모님과 고향", role: "만 ２５ 세 여성", contribution: "모든 결과 100% 전부" },
    { role: true, situation: 123, objective: null, actions: [false, 0, null, "검토"] },
    { role: 0.00001, situation: 1.234e-7, objective: -0.00002 },
    { organization: { name: "기관", active: true }, actions: [["검토", null], { item: "기록\n확인" }] },
    { experience_title: "😀".repeat(301), situation: "한😀".repeat(2100), actions: ["👍".repeat(1001)] },
    { experience_title: "😀".repeat(300), situation: "한😀".repeat(2000), actions: ["👍".repeat(1000)] },
    { situation: "010101-1234567" }, { actions: ["010101 2234567"] },
    { situation: "991010101123456799" }, { tools: ["１２３４５６-1１２３４５６"] },
    { experience_title: "<img src=x onerror=alert(1)>", result: "</textarea><script>alert('x')</script>&\"" }
  ];
  const payloads = [];
  for (const document_type of ["career", "experience"]) {
    for (const style of ["bullet", "narrative"]) {
      payloads.push({ document_type, style, facts_confirmed: true });
      for (const variation of variations) payloads.push({ ...base, document_type, style, ...variation });
    }
  }
  const expected = pythonResults(payloads);
  payloads.forEach((payload, index) => {
    const original = JSON.stringify(payload);
    if (expected[index].errors) {
      for (const operation of [composer.compose, composer.sanitizeDraft]) {
        assert.throws(() => operation(payload), error => {
          assert.deepEqual(error.errors, expected[index].errors, `validation parity case ${index}`);
          return true;
        });
      }
    } else {
      assert.deepEqual(composer.compose(payload), expected[index].result, `compose parity case ${index}`);
      assert.deepEqual(composer.sanitizeDraft(payload), expected[index].sanitized, `sanitize parity case ${index}`);
    }
    assert.equal(JSON.stringify(payload), original, "composition must not mutate the draft");
  });
});

test("validation matches Python and requires literal fact confirmation", () => {
  const payloads = [
    {}, { ...base, document_type: "other" }, { ...base, style: "html" },
    ...[false, null, 0, 1, "true", "false", [], {}].map(facts_confirmed => ({ ...base, facts_confirmed }))
  ];
  const expected = pythonResults(payloads);
  payloads.forEach((payload, index) => {
    assert.throws(() => composer.compose(payload), error => {
      assert.ok(error instanceof composer.DraftValidationError);
      assert.deepEqual(error.errors, expected[index].errors);
      assert.equal(error.message, expected[index].message);
      return true;
    });
  });
});

test("browser entrypoint composes with network, storage and DOM access forbidden", () => {
  const context = vm.createContext({});
  for (const forbidden of ["fetch", "XMLHttpRequest", "WebSocket", "navigator", "document", "localStorage", "sessionStorage"]) {
    Object.defineProperty(context, forbidden, { get() { throw new Error(`Unexpected ${forbidden} access`); } });
  }
  vm.runInContext(composerSource, context);
  const payload = { ...base, experience_title: "anonymous-private-experience" };
  assert.deepEqual(JSON.parse(JSON.stringify(context.JobAndKillComposer.compose(payload))), composer.compose(payload));
});

function composeUI(payload, api = composer) {
  function element() {
    return {
      textContent: "", disabled: false, children: [], focused: false, open: false,
      set innerHTML(_) { throw new Error("Draft data must never use innerHTML"); },
      replaceChildren() { this.children = []; },
      append(child) { this.children.push(child); },
      focus() { this.focused = true; },
      showModal() { this.open = true; }
    };
  }
  const elements = Object.fromEntries([
    "#form-message", "#facts_confirmed", "#next-step", "#draft-output", "#fact-coverage", "#warning-list", "#result-dialog"
  ].map(selector => [selector, element()]));
  const context = vm.createContext({
    state: payload, JobAndKillComposer: api, $: selector => elements[selector],
    document: { createElement: element },
    fetch() { throw new Error("Composition must never send a network request"); }
  });
  const start = appSource.indexOf("function composeDraft() {");
  const end = appSource.indexOf("\nfunction closeDialog(", start);
  assert.ok(start >= 0 && end > start, "existing compose UI function must be found");
  vm.runInContext(appSource.slice(start, end), context);
  vm.runInContext("composeDraft()", context);
  return elements;
}

test("compose UI uses plain text, preserves warning flow and performs no fetch", () => {
  const payload = { ...base, experience_title: "</pre><img src=x onerror=alert(1)>", evidence: "" };
  const expected = composer.compose(payload);
  const elements = composeUI(payload);
  assert.equal(elements["#draft-output"].textContent, expected.output);
  assert.equal(elements["#fact-coverage"].textContent, `${expected.fact_coverage}%`);
  assert.deepEqual(elements["#warning-list"].children.map(item => item.textContent), expected.warnings);
  assert.equal(elements["#result-dialog"].open, true);
  assert.equal(elements["#next-step"].disabled, false);
});

test("compose UI handles missing confirmation, invalid fields and recoverable errors locally", () => {
  let elements = composeUI({ ...base, facts_confirmed: false });
  assert.equal(elements["#facts_confirmed"].focused, true);
  assert.equal(elements["#result-dialog"].open, false);
  elements = composeUI({ ...base, style: "invalid" });
  assert.equal(elements["#form-message"].textContent, "입력값을 확인해 주세요.");
  assert.equal(elements["#next-step"].disabled, false);
  elements = composeUI(base, { compose() { throw new Error("로컬 작성 오류"); } });
  assert.equal(elements["#form-message"].textContent, "로컬 작성 오류");
  assert.equal(elements["#next-step"].disabled, false);
});

test("page loads the composer before app initialization and has no compose API reference", () => {
  const markup = readFileSync(path.join(root, "web/index.html"), "utf8");
  const composerIndex = markup.search(/<script src="\/composer\.js(?:\?[^"]+)?" defer>/u);
  const appIndex = markup.search(/<script src="\/app\.js(?:\?[^"]+)?" defer>/u);
  assert.ok(composerIndex >= 0 && appIndex > composerIndex);
  assert.equal(appSource.includes("/api/drafts/compose"), false);
});

function catalogUIContext(extra = {}) {
  const elements = {};
  function element() {
    return {
      children: [], hidden: false, listeners: {}, textContent: "", disabled: false,
      set innerHTML(_) { throw new Error("Catalog content must use plain text"); },
      append(...children) { this.children.push(...children); },
      replaceChildren() { this.children = []; this.textContent = ""; },
      focus() {},
      addEventListener(event, callback) { this.listeners[event] = callback; }
    };
  }
  const context = vm.createContext({
    URL, state: {}, saved: 0, closed: 0,
    JobAndKillComposer: composer,
    $: selector => elements[selector] ||= element(),
    document: { createElement: element },
    saveState() { context.saved += 1; },
    updateSelectedJob() {}, renderStep() {}, showWorkspace() {},
    closeDialog() { context.closed += 1; },
    api() { throw new Error("Unexpected network request"); },
    ...extra
  });
  const start = appSource.indexOf('let archiveScope = "catalog";');
  const end = appSource.indexOf("\nasync function searchJobs(", start);
  assert.ok(start >= 0 && end > start);
  vm.runInContext(appSource.slice(start, end), context);
  return { context, elements };
}

test("NCS application fills only empty reference fields and preserves institution and every experience", () => {
  const payload = { ...base, ncs_path: "", matched_duty: "", matched_skill: "내가 선택한 기술" };
  const { context, elements } = catalogUIContext({ state: payload });
  const item = { id: "a".repeat(64), source_slug: "ncs-training-2025", source_url: referenceBase.reference_source_url,
    source_updated_at: "2025-12-31", job_title: "공통 직무", ncs_path: "분류 > 세분류", summary: "공식 수행내용", institution_name: "잘못된 기관", skills: ["공통 기술"] };
  const additions = context.catalogDraftAdditions(item, payload);
  assert.deepEqual(JSON.parse(JSON.stringify(additions)), { ncs_path: "분류 > 세분류", matched_duty: "공식 수행내용" });
  assert.equal(context.saved, 0, "viewing/calculating references must not save a draft");
  context.applyCatalogReference(item);
  assert.equal(context.saved, 1);
  assert.equal(context.closed, 1);
  for (const [key, value] of Object.entries(base)) assert.deepEqual(payload[key], value, `${key} must be preserved`);
  assert.equal(payload.matched_skill, "내가 선택한 기술");
  assert.equal(payload.ncs_path, "분류 > 세분류");
  for (const [key, value] of Object.entries(referenceBase)) assert.equal(payload[key], value);
  assert.match(elements["#form-message"].textContent, /지원 기관과 작성한 경험은 변경하지 않았습니다/);
});

test("NCS application leaves absent and oversized official fields empty without truncation or generation", () => {
  const { context } = catalogUIContext();
  for (const item of [{}, { job_title: "", summary: null }, { job_title: "가".repeat(301), ncs_path: "가".repeat(1001), summary: "가".repeat(1001) }]) {
    assert.deepEqual(JSON.parse(JSON.stringify(context.catalogDraftAdditions(item, {}))), {});
  }
  const item = { job_title: "😀".repeat(300), summary: "공식 내용", raw: { institution: "악성 기관", result: "가짜 성과" } };
  const additions = JSON.parse(JSON.stringify(context.catalogDraftAdditions(item, {})));
  assert.equal(additions.target_job, item.job_title);
  assert.equal(additions.matched_duty, "공식 내용");
  assert.deepEqual(Object.keys(additions), ["target_job", "matched_duty"]);
});

test("catalog source links allow only credential-free HTTP(S)", () => {
  const { context } = catalogUIContext();
  for (const value of ["javascript:alert(1)", "data:text/html,test", "/relative", "https://secret@data.go.kr/file", null, ""]) {
    assert.equal(context.safeSourceLink(value), null);
  }
  assert.equal(context.safeSourceLink("https://www.data.go.kr/data/15150267/openapi.do"), "https://www.data.go.kr/data/15150267/openapi.do");
});

test("catalog details show missing KSA and provenance as plain text without mutating the draft", async () => {
  const payload = { ...base };
  const original = JSON.stringify(payload);
  const item = {
    id: "a".repeat(64), job_title: "<img src=x onerror=alert(1)>", summary: "공식 설명",
    source_slug: "ncs-common", source_url: "javascript:alert(1)", knowledge: [], skills: [], attitudes: [], performance_criteria: [],
    raw: { secret: "RAW_MUST_NOT_APPEAR" }
  };
  const { context, elements } = catalogUIContext({ state: payload, api: async () => ({ item }) });
  await context.showCatalogDetail(item.id);
  assert.equal(JSON.stringify(payload), original);
  assert.equal(context.saved, 0);
  const children = elements["#catalog-detail"].children;
  assert.equal(children[0].textContent, item.job_title);
  const fields = children.find(child => child.children.length === 10);
  assert.ok(fields);
  assert.equal(fields.children.filter(child => child.textContent.includes("미제공 또는 미수집")).length, 4);
  const rendered = JSON.stringify(children, (key, value) => typeof value === "function" ? undefined : value);
  assert.equal(rendered.includes("RAW_MUST_NOT_APPEAR"), false);
  assert.equal(rendered.includes("javascript:alert"), false);
  assert.ok(children.some(child => child.textContent === "비어 있는 항목에 참고 자료 적용"));
});

test("catalog reference survives browser/server sanitization and is attributed separately in both writing styles", () => {
  const payloads = ["bullet", "narrative"].map(style => ({ ...base, ...referenceBase, style }));
  const expected = pythonResults(payloads);
  payloads.forEach((payload, index) => {
    const result = composer.compose(payload);
    assert.deepEqual(result, expected[index].result);
    assert.deepEqual(composer.sanitizeDraft(payload), expected[index].sanitized);
    for (const [key, value] of Object.entries(referenceBase)) assert.equal(expected[index].sanitized[key], value);
    const [body, footer] = result.output.split("\n\n[NCS 참고자료 — 기관별 채용요건 아님]");
    assert.equal(body.includes(referenceBase.reference_source_url), false);
    assert.ok(footer.includes(referenceBase.reference_source_url));
    assert.ok(footer.includes("한국산업인력공단"));
    assert.ok(footer.includes("2025-12-31"));
    assert.ok(footer.includes("사용자의 경험·성과를 증명하는 자료가 아닙니다"));
  });
  const fields = appSource.slice(appSource.indexOf("const FIELDS = ["), appSource.indexOf("let sessionStorageAvailable"));
  for (const key of Object.keys(referenceBase)) assert.ok(fields.includes(key), `${key} must survive cleanDraft and storage`);
  const hashWithDigits = { ...base, ...referenceBase, reference_catalog_id: "a" + "0101011234567" + "a".repeat(50) };
  assert.deepEqual(composer.sanitizeDraft(hashWithDigits), pythonResults([hashWithDigits])[0].sanitized);
});

test("unregistered credential-bearing or partial attribution is rejected identically by Python and browser", () => {
  const variations = [
    { reference_source_url: "https://other.example/reference" },
    { reference_source_url: referenceBase.reference_source_url + "?serviceKey=DO_NOT_STORE" },
    { reference_source_url: referenceBase.reference_source_url + "#SECRET" },
    { reference_source_url: "https://SECRET@www.data.go.kr/data/15083321/fileData.do" },
    { reference_source_url: "http://www.data.go.kr/data/15083321/fileData.do" },
    { reference_source_url: "https://www.data.go.kr:443/data/15083321/fileData.do" },
    { reference_source_url: [referenceBase.reference_source_url] },
    { reference_catalog_id: "a".repeat(64) + "\n" }, { reference_catalog_id: "not-a-record" },
    { reference_name: "임의 출처 이름" }, { reference_version: "secret_token_123" },
    { reference_version: "24v1\n" }, { reference_version: "24v1\u2028" },
    { reference_version: null }, { reference_catalog_id: false }
  ];
  const payloads = variations.map(value => ({ ...base, ...referenceBase, ...value }));
  const expected = pythonResults(payloads);
  payloads.forEach((payload, index) => {
    assert.ok(expected[index].errors);
    assert.throws(() => composer.sanitizeDraft(payload), error => {
      assert.deepEqual(error.errors, expected[index].errors);
      return true;
    });
  });
});

test("second catalog cannot overwrite attribution or mix sources into an existing draft", () => {
  const payload = { ...base, ...referenceBase, ncs_path: "", matched_duty: "" };
  const original = JSON.stringify(payload);
  const { context, elements } = catalogUIContext({ state: payload });
  context.applyCatalogReference({ id: "b".repeat(64), source_slug: "ncs-common", job_title: "다른 자료", summary: "다른 업무" });
  assert.equal(JSON.stringify(payload), original);
  assert.equal(context.saved, 0);
  assert.equal(context.closed, 0);
  assert.match(elements["#catalog-apply-status"].textContent, /기존 참고 출처와 작성 내용을 유지/);
});

test("view-only and unusable catalog references never create or overwrite provenance", () => {
  const item = { id: "a".repeat(64), source_slug: "ncs-training-2025", source_url: referenceBase.reference_source_url,
    source_updated_at: "2025-12-31", job_title: "공통 직무", ncs_path: "분류", summary: "설명" };
  const payload = { ...base, ncs_path: "내 분류", matched_duty: "내 업무" };
  const original = JSON.stringify(payload);
  let ui = catalogUIContext({ state: payload });
  ui.context.applyCatalogReference(item);
  assert.equal(JSON.stringify(payload), original);
  assert.equal(ui.context.saved, 0);
  ui = catalogUIContext({ state: { ...base, ncs_path: "", matched_duty: "" } });
  ui.context.applyCatalogReference({ ...item, source_url: item.source_url + "?secret=x" });
  assert.equal(ui.context.state.ncs_path, "");
  assert.equal(ui.context.saved, 0);
  assert.match(ui.elements["#catalog-apply-status"].textContent, /공식 출처 기록을 확인하지 못해 적용하지 않았습니다/);
});

test("stale catalog searches and details cannot replace newer selections", async () => {
  const pending = [];
  const { context, elements } = catalogUIContext({ api: () => new Promise(resolve => pending.push(resolve)) });
  const first = context.searchCatalog("옛 검색");
  const second = context.searchCatalog("새 검색");
  pending[1]({ items: [{ id: "b", job_title: "새 자료" }] });
  await second;
  pending[0]({ items: [{ id: "a", job_title: "오래된 자료" }] });
  await first;
  assert.equal(elements["#job-results"].children[0].children[0].children[0].textContent, "새 자료");
  const details = context.showCatalogDetail("a");
  context.clearCatalogDetail();
  pending[2]({ item: { job_title: "늦게 도착한 자료" } });
  await details;
  assert.equal(elements["#catalog-detail"].hidden, true);
  assert.equal(elements["#catalog-detail"].children.length, 0);
});

test("coverage distinguishes source restrictions and missing official fields from zero collected records", async () => {
  const { context, elements } = catalogUIContext({ api: async () => ({
    catalog: { catalog_records: 0, notes: [] },
    sources: [{ name: "직업정보", rights: "restricted", not_provided: ["knowledge", "skills"], note: "허락 없이 상용 수집 제외" }]
  }) });
  await context.loadCatalogCoverage();
  assert.match(elements["#catalog-coverage-message"].textContent, /저장된 직무능력 자료 0건/);
  assert.match(elements["#catalog-coverage-message"].textContent, /정부 전체 자료의 총수가 아닙니다/);
  const row = elements["#catalog-source-list"].children[0];
  assert.match(row.children[1].textContent, /이용권한 제한/);
  assert.match(row.children[3].textContent, /지식 · 기술/);
});
