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
