"use strict";

const STORAGE_KEY = "jobandkill-draft-v1";
const STEP_KEY = "jobandkill-step-v1";
const CLIENT_KEY = "jobandkill-client-key-v1";
const FIELDS = [
  "document_type", "style", "target_length", "institution", "target_job", "ncs_path",
  "matched_duty", "matched_skill", "experience_title", "organization", "period_start",
  "period_end", "role", "situation", "objective", "judgment", "actions", "tools",
  "collaboration", "result", "evidence", "contribution", "learning", "facts_confirmed"
];
const defaults = {
  document_type: "career", style: "bullet", target_length: 800, institution: "", target_job: "",
  ncs_path: "", matched_duty: "", matched_skill: "", experience_title: "", organization: "",
  period_start: "", period_end: "", role: "", situation: "", objective: "", judgment: "",
  actions: [""], tools: [], collaboration: "", result: "", evidence: "", contribution: "",
  learning: "", facts_confirmed: false
};

let state = loadState();
let currentStep = Math.min(Math.max(Number(localStorage.getItem(STEP_KEY) || 0), 0), 7);
let authState = { status: "loading", user: null };
let syncState = {
  consent: false, draftId: null, revision: null, pendingServer: null, pendingMode: null,
  clientKey: getClientKey(), timer: null, saving: false, dirty: false
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

function loadState() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
    return cleanDraft(saved);
  } catch {
    return { ...defaults, actions: [""], tools: [] };
  }
}

function saveState() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  localStorage.setItem(STEP_KEY, String(currentStep));
  const label = $("#autosave-state");
  label.textContent = authState.status === "signed_in" && syncState.consent ? "계정 저장 대기 중…" : "이 브라우저에 자동 저장";
  scheduleServerSave();
}

function setField(name, value) {
  state[name] = value;
  saveState();
}

function escapeMarkup(value) {
  return String(value ?? "").replace(/[&<>'"]/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", "\"": "&quot;"
  }[char]));
}

function cleanDraft(saved) {
  const clean = { ...defaults };
  for (const key of FIELDS) {
    if (Object.hasOwn(saved || {}, key)) clean[key] = saved[key];
  }
  clean.actions = Array.isArray(clean.actions) && clean.actions.length ? clean.actions.slice(0, 12) : [""];
  clean.tools = Array.isArray(clean.tools) ? clean.tools.slice(0, 12) : [];
  clean.facts_confirmed = clean.facts_confirmed === true;
  return clean;
}

function getClientKey() {
  let value = localStorage.getItem(CLIENT_KEY);
  if (!value) {
    value = self.crypto?.randomUUID?.() || `draft_${Date.now()}_${Math.random().toString(36).slice(2)}`;
    localStorage.setItem(CLIENT_KEY, value);
  }
  return value;
}

function hasDraftContent(draft = state) {
  const textFields = FIELDS.filter(key => !["document_type", "style", "target_length", "actions", "tools", "facts_confirmed"].includes(key));
  return textFields.some(key => String(draft[key] || "").trim()) ||
    (draft.actions || []).some(Boolean) || (draft.tools || []).some(Boolean) || draft.facts_confirmed === true;
}

function sameDraft(left, right) {
  const project = value => Object.fromEntries(FIELDS.map(key => [key, cleanDraft(value)[key]]));
  return JSON.stringify(project(left)) === JSON.stringify(project(right));
}

function csrfToken() {
  const item = document.cookie.split(";").map(value => value.trim()).find(value => value.startsWith("jobandkill_csrf="));
  return item ? decodeURIComponent(item.split("=").slice(1).join("=")) : "";
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (options.csrf) headers.set("X-CSRF-Token", csrfToken());
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error?.message || "요청을 처리하지 못했습니다.");
    error.code = data.error?.code || "request_failed";
    error.status = response.status;
    throw error;
  }
  return data;
}

function updateAuthUI() {
  const signedIn = authState.status === "signed_in";
  $("#open-auth").textContent = signedIn ? "내 계정" : "로그인";
  $("#signed-out-panel").hidden = signedIn;
  $("#signed-in-panel").hidden = !signedIn;
  $("#account-email").textContent = signedIn ? authState.user.email : "";
  $("#privacy-copy").textContent = signedIn && syncState.consent
    ? "작성 내용은 동의 후 계정에도 저장되며, 확인하지 않은 정보는 만들지 않습니다."
    : "작성 내용은 이 브라우저에 임시 저장되며, 확인하지 않은 정보는 만들지 않습니다.";
  if (!signedIn) $("#autosave-state").textContent = "이 브라우저에 자동 저장";
}

function showSyncNotice(mode, serverDraft = null, draftsMatch = false) {
  syncState.pendingMode = mode;
  syncState.pendingServer = serverDraft;
  const notice = $("#sync-notice");
  notice.hidden = false;
  if (mode === "upload") {
    $("#sync-notice-copy").textContent = "이 브라우저의 초안을 계정에도 저장할까요? 동의하기 전에는 서버로 보내지 않습니다.";
    $("#keep-local-draft").textContent = "계정에도 저장";
    $("#use-server-draft").hidden = true;
  } else if (mode === "conflict") {
    $("#sync-notice-copy").textContent = draftsMatch
      ? "이 브라우저와 연결된 계정 초안을 찾았습니다. 계정 저장을 계속하려면 직접 선택해 주세요."
      : "이 브라우저와 계정의 초안이 다릅니다. 사용할 내용을 직접 선택해 주세요.";
    $("#keep-local-draft").textContent = draftsMatch ? "계정 저장 계속" : "이 기기 내용 사용";
    $("#use-server-draft").hidden = draftsMatch;
  } else {
    $("#sync-notice-copy").textContent = "다른 기기에 저장된 계정 초안이 있습니다. 불러오거나 이 기기 내용을 새 초안으로 저장해 주세요.";
    $("#keep-local-draft").textContent = "이 기기 내용을 새로 저장";
    $("#use-server-draft").hidden = false;
  }
}

function hideSyncNotice() {
  $("#sync-notice").hidden = true;
  syncState.pendingMode = null;
  syncState.pendingServer = null;
}

function hydrateFromServer(serverDraft) {
  state = cleanDraft(serverDraft.payload);
  currentStep = Math.min(Math.max(Number(serverDraft.current_step || 0), 0), 7);
  syncState.draftId = serverDraft.id;
  syncState.revision = serverDraft.revision;
  syncState.clientKey = serverDraft.client_key;
  syncState.consent = true;
  localStorage.setItem(CLIENT_KEY, serverDraft.client_key);
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  localStorage.setItem(STEP_KEY, String(currentStep));
  hideSyncNotice();
  updateSelectedJob();
  if (!$("#workspace").hidden) renderStep();
  $("#autosave-state").textContent = "계정 내용을 불러옴";
  updateAuthUI();
}

async function reconcileDrafts() {
  const listing = await api("/api/user/drafts");
  const localClientKey = getClientKey();
  const summary = listing.items.find(item => item.client_key === localClientKey);
  syncState.clientKey = localClientKey;
  syncState.draftId = null;
  syncState.revision = null;
  syncState.consent = false;

  if (summary) {
    const serverDraft = await api(`/api/user/drafts/${summary.id}`);
    const serverStep = Math.min(Math.max(Number(serverDraft.current_step || 0), 0), 7);
    const draftsMatch = sameDraft(state, serverDraft.payload) && currentStep === serverStep;
    showSyncNotice("conflict", serverDraft, draftsMatch);
    $("#autosave-state").textContent = draftsMatch ? "계정 저장 동의 필요" : "저장 내용 선택 필요";
    updateAuthUI();
    return;
  }

  const otherSummary = listing.items[0];
  if (otherSummary) {
    const serverDraft = await api(`/api/user/drafts/${otherSummary.id}`);
    showSyncNotice("load", serverDraft);
    $("#autosave-state").textContent = "저장 내용 선택 필요";
  } else {
    showSyncNotice("upload");
    $("#autosave-state").textContent = "계정 저장 동의 필요";
  }
  updateAuthUI();
}

async function loadAuth() {
  try {
    const data = await api("/api/me");
    authState = data.authenticated ? { status: "signed_in", user: data.user } : { status: "signed_out", user: null };
    updateAuthUI();
    if (data.authenticated) await reconcileDrafts();
  } catch {
    authState = { status: "signed_out", user: null };
    updateAuthUI();
  }
}

function scheduleServerSave() {
  if (authState.status !== "signed_in" || !syncState.consent) return;
  syncState.dirty = true;
  window.clearTimeout(syncState.timer);
  syncState.timer = window.setTimeout(saveServerDraft, 900);
}

async function saveServerDraft() {
  if (authState.status !== "signed_in" || !syncState.consent) return;
  if (syncState.saving) {
    syncState.dirty = true;
    return;
  }
  syncState.saving = true;
  syncState.dirty = false;
  $("#autosave-state").textContent = "계정에 저장 중…";
  const body = JSON.stringify({
    client_key: syncState.clientKey, title: state.experience_title || state.target_job || "제목 없는 초안",
    payload: state, current_step: currentStep, revision: syncState.revision
  });
  try {
    const saved = syncState.draftId
      ? await api(`/api/user/drafts/${syncState.draftId}`, { method: "PUT", body, csrf: true })
      : await api("/api/user/drafts", { method: "POST", body, csrf: true });
    syncState.draftId = saved.id;
    syncState.revision = saved.revision;
    syncState.clientKey = saved.client_key || syncState.clientKey;
    $("#autosave-state").textContent = "계정에 저장됨";
    updateAuthUI();
  } catch (error) {
    if (error.status === 401) {
      authState = { status: "signed_out", user: null };
      syncState.consent = false;
      updateAuthUI();
      $("#autosave-state").textContent = "로그인 만료 · 브라우저에는 저장됨";
    } else if (error.status === 409) {
      syncState.consent = false;
      $("#autosave-state").textContent = "저장 내용 선택 필요";
      try {
        const latest = syncState.draftId ? await api(`/api/user/drafts/${syncState.draftId}`) : null;
        showSyncNotice(latest ? "conflict" : "upload", latest);
      } catch {
        showSyncNotice("upload");
      }
    } else {
      $("#autosave-state").textContent = "브라우저에는 저장됨 · 다시 시도";
    }
  } finally {
    syncState.saving = false;
    if (syncState.dirty && syncState.consent) scheduleServerSave();
  }
}

async function keepLocalDraft() {
  const pendingMode = syncState.pendingMode;
  const serverDraft = syncState.pendingServer;
  if (pendingMode === "conflict" && serverDraft?.client_key === syncState.clientKey) {
    syncState.draftId = serverDraft.id;
    syncState.revision = serverDraft.revision;
    syncState.clientKey = serverDraft.client_key;
  } else {
    syncState.draftId = null;
    syncState.revision = null;
    syncState.clientKey = getClientKey();
  }
  syncState.consent = true;
  hideSyncNotice();
  updateAuthUI();
  await saveServerDraft();
}

function useServerDraft() {
  if (syncState.pendingServer) hydrateFromServer(syncState.pendingServer);
}

async function consumeLoginToken() {
  const values = new URLSearchParams(window.location.hash.slice(1));
  const token = values.get("login_token");
  if (!token) return false;
  history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
  $("#auth-dialog").showModal();
  $("#auth-status").textContent = "로그인 링크를 확인하고 있습니다…";
  try {
    const data = await api("/api/auth/verify", { method: "POST", body: JSON.stringify({ token }) });
    authState = { status: "signed_in", user: data.user };
    $("#auth-status").textContent = "로그인되었습니다.";
    updateAuthUI();
  } catch (error) {
    $("#auth-status").textContent = error.message;
  }
  return true;
}

async function requestLogin(event) {
  event.preventDefault();
  const button = $("#request-login");
  button.disabled = true;
  $("#auth-status").textContent = "로그인 링크를 보내고 있습니다…";
  $("#development-login-link").hidden = true;
  try {
    const data = await api("/api/auth/request", {
      method: "POST", body: JSON.stringify({ email: $("#login-email").value })
    });
    $("#auth-status").textContent = data.message;
    if (data.development_magic_link) {
      $("#development-login-link").href = data.development_magic_link;
      $("#development-login-link").hidden = false;
    }
  } catch (error) {
    $("#auth-status").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST", body: "{}", csrf: true });
  } catch (error) {
    $("#account-status").textContent = error.message;
    return;
  }
  if ($("#clear-local-on-logout").checked) {
    localStorage.removeItem(STORAGE_KEY);
    localStorage.removeItem(STEP_KEY);
    state = { ...defaults, actions: [""], tools: [] };
    currentStep = 0;
    updateSelectedJob();
    if (!$("#workspace").hidden) renderStep();
  }
  authState = { status: "signed_out", user: null };
  syncState = {
    consent: false, draftId: null, revision: null, pendingServer: null, pendingMode: null,
    clientKey: getClientKey(), timer: null, saving: false, dirty: false
  };
  updateAuthUI();
  $("#auth-status").textContent = "로그아웃되었습니다.";
}

async function resetDraft() {
  const scope = syncState.draftId && authState.status === "signed_in" ? "브라우저와 계정의" : "브라우저의";
  if (!window.confirm(`${scope} 작성 중인 내용을 모두 지우고 처음부터 시작할까요?`)) return;
  if (syncState.draftId && authState.status === "signed_in") {
    try {
      await api(`/api/user/drafts/${syncState.draftId}`, { method: "DELETE", csrf: true });
    } catch (error) {
      $("#form-message").textContent = `${error.message} 브라우저 내용은 지우지 않았습니다.`;
      return;
    }
  }
  state = { ...defaults, actions: [""], tools: [] };
  localStorage.removeItem(STORAGE_KEY);
  localStorage.removeItem(STEP_KEY);
  currentStep = 0;
  syncState.draftId = null;
  syncState.revision = null;
  updateSelectedJob();
  renderStep();
}

const stepDefinitions = [
  { title: "작성 형식을 골라주세요.", render: renderFormat },
  { title: "지원할 직무를 연결합니다.", render: renderTarget },
  { title: "경험의 기본 정보를 적어주세요.", render: renderBasics },
  { title: "어떤 상황, 어떤 목표였나요?", render: renderSituation },
  { title: "무엇을 판단하고 행동했나요?", render: renderAction },
  { title: "도구와 협업 방식을 알려주세요.", render: renderCollaboration },
  { title: "결과와 나의 기여를 나눠볼게요.", render: renderOutcome },
  { title: "사실을 확인하면 초안을 만듭니다.", render: renderReview }
];

function renderStep() {
  const definition = stepDefinitions[currentStep];
  $("#step-kicker").textContent = `STEP ${currentStep + 1} OF ${stepDefinitions.length}`;
  $("#builder-title").textContent = definition.title;
  $("#progress-bar").className = `progress-${currentStep + 1}`;
  $(".progress-track").setAttribute("aria-valuenow", String(currentStep + 1));
  $("#form-message").textContent = "";
  definition.render($("#step-content"));
  $("#previous-step").disabled = currentStep === 0;
  $("#previous-step").classList.toggle("is-invisible", currentStep === 0);
  $("#next-step").textContent = currentStep === stepDefinitions.length - 1 ? "초안 만들기 ↗" : "다음 →";
  bindInputs();
}

function renderFormat(container) {
  container.innerHTML = `
    <p class="step-intro">공고 양식에 맞게 문서 종류와 문체를 선택하세요. 언제든 다시 바꿀 수 있습니다.</p>
    <div class="choice-grid">
      ${choice("document_type", "career", "A", "경력기술서", "직무·프로젝트 단위로 기간과 성과를 정리합니다.", state.document_type)}
      ${choice("document_type", "experience", "B", "경험기술서", "특정 상황에서 한 판단과 행동을 중심으로 정리합니다.", state.document_type)}
      ${choice("style", "bullet", "01", "개조식", "채용담당자가 빠르게 읽도록 핵심을 항목별로 씁니다.", state.style)}
      ${choice("style", "narrative", "02", "스토리텔링", "내가 작성한 듯 자연스러운 경험담 문장으로 연결합니다.", state.style)}
    </div>
    <div class="field length-field">
      <label for="target_length">목표 분량</label>
      <select id="target_length" name="target_length">
        <option value="500" ${state.target_length === 500 ? "selected" : ""}>약 500자 — 짧은 문항</option>
        <option value="800" ${state.target_length === 800 ? "selected" : ""}>약 800자 — 표준</option>
        <option value="1000" ${state.target_length === 1000 ? "selected" : ""}>약 1,000자 — 상세</option>
        <option value="1500" ${state.target_length === 1500 ? "selected" : ""}>약 1,500자 — 경력 상세</option>
      </select>
    </div>`;
}

function choice(name, value, number, title, description, selected) {
  const id = `${name}-${value}`;
  return `<div class="choice-card">
    <input type="radio" id="${id}" name="${name}" value="${value}" ${selected === value ? "checked" : ""}>
    <label for="${id}"><small>${number}</small><strong>${title}</strong><span>${description}</span></label>
  </div>`;
}

function renderTarget(container) {
  const selectedText = state.target_job
    ? `<strong>${escapeMarkup(state.institution || "기관 미지정")} · ${escapeMarkup(state.target_job)}</strong>`
    : `<strong>축적된 직무를 검색하거나 직접 입력하세요.</strong>`;
  container.innerHTML = `
    <p class="step-intro">공식 직무를 선택하면 해당 공고의 업무·기술을 연결합니다. 검색 결과가 없어도 직접 입력할 수 있습니다.</p>
    <div class="target-preview">${selectedText}<button type="button" data-open-job-search>직무 아카이브 열기</button></div>
    <div class="form-grid">
      <div class="field"><label for="institution">지원 기관</label><input id="institution" name="institution" value="${escapeMarkup(state.institution)}" placeholder="예: 한국○○공사"></div>
      <div class="field"><label for="target_job">지원 직무</label><input id="target_job" name="target_job" value="${escapeMarkup(state.target_job)}" placeholder="예: 사무행정"></div>
      <div class="field field-full"><label for="ncs_path">NCS 분류 또는 채용분야</label><input id="ncs_path" name="ncs_path" value="${escapeMarkup(state.ncs_path)}" placeholder="예: 경영·회계·사무 > 총무·인사 > 일반사무"></div>
      <div class="field"><label for="matched_duty">연결할 직무수행내용</label><textarea id="matched_duty" name="matched_duty" placeholder="선택한 직무에서 가져오거나 직접 입력">${escapeMarkup(state.matched_duty)}</textarea></div>
      <div class="field"><label for="matched_skill">연결할 필요기술</label><textarea id="matched_skill" name="matched_skill" placeholder="예: 자료 분석 및 문서 작성 능력">${escapeMarkup(state.matched_skill)}</textarea></div>
    </div>`;
  $("[data-open-job-search]", container).addEventListener("click", openJobDialog);
}

function renderBasics(container) {
  container.innerHTML = `
    <p class="step-intro">여기에는 지원 직무가 아니라, 실제로 수행했던 경험의 정보를 적습니다.</p>
    <div class="form-grid">
      ${inputField("experience_title", "경험·업무명", "예: 민원 처리 절차 개선", state.experience_title, true)}
      ${inputField("organization", "소속·활동기관", "예: ○○공단 체험형 인턴", state.organization)}
      ${inputField("period_start", "시작", "2025.03", state.period_start, false, "month")}
      ${inputField("period_end", "종료", "2025.08", state.period_end, false, "month")}
      <div class="field field-full"><label for="role">내 역할과 책임 범위</label><textarea id="role" name="role" placeholder="직책보다 실제로 맡은 책임을 써주세요. 예: 민원 데이터 정리와 개선안 초안 작성">${escapeMarkup(state.role)}</textarea><small>‘팀원’ 대신 내가 책임진 대상과 범위를 적으면 더 정확해집니다.</small></div>
    </div>`;
}

function inputField(name, label, placeholder, value, full = false, type = "text") {
  return `<div class="field ${full ? "field-full" : ""}"><label for="${name}">${label}</label><input id="${name}" name="${name}" type="${type}" value="${escapeMarkup(value)}" placeholder="${placeholder}"></div>`;
}

function renderSituation(container) {
  container.innerHTML = `
    <p class="step-intro">배경 설명은 짧게, 해결해야 했던 문제와 기준은 구체적으로 적어주세요.</p>
    <div class="form-grid">
      <div class="field field-full"><label for="situation">상황·문제</label><textarea id="situation" name="situation" placeholder="언제, 무엇이 잘 되지 않았고, 누구에게 어떤 영향이 있었나요?">${escapeMarkup(state.situation)}</textarea><small>예: 접수 기준이 담당자마다 달라 같은 민원도 처리 시간이 달라지는 상황이었습니다.</small></div>
      <div class="field field-full"><label for="objective">내가 세운 목표</label><textarea id="objective" name="objective" placeholder="무엇을 어느 수준까지 바꾸려고 했나요?">${escapeMarkup(state.objective)}</textarea><small>실제로 측정한 수치가 없다면 숫자를 새로 만들지 마세요.</small></div>
    </div>`;
}

function renderAction(container) {
  container.innerHTML = `
    <p class="step-intro">먼저 왜 그런 선택을 했는지 적고, 실제 행동은 시간 순서대로 나눠주세요.</p>
    <div class="field"><label for="judgment">판단 기준·이유</label><textarea id="judgment" name="judgment" placeholder="여러 방법 중 이 방법을 선택한 이유와 고려한 기준">${escapeMarkup(state.judgment)}</textarea></div>
    <div class="field field-spaced"><span class="field-label">구체적인 행동</span><span class="field-help">‘노력했다’보다 조사·분석·설계·협의처럼 확인 가능한 동사로 시작하세요.</span><div class="action-list" id="action-list"></div><button class="add-action" id="add-action" type="button">+ 행동 한 단계 추가</button></div>`;
  renderActionRows();
  $("#add-action").addEventListener("click", () => {
    if (state.actions.length >= 12) return;
    state.actions.push("");
    saveState();
    renderActionRows();
  });
}

function renderActionRows() {
  const list = $("#action-list");
  list.replaceChildren();
  state.actions.forEach((action, index) => {
    const row = document.createElement("div");
    row.className = "action-row";
    const marker = document.createElement("span");
    marker.className = "action-index";
    marker.textContent = String(index + 1).padStart(2, "0");
    const input = document.createElement("input");
    input.value = action;
    input.placeholder = index === 0 ? "예: 최근 3개월의 민원 유형과 처리 시간을 분류" : "다음 행동";
    input.setAttribute("aria-label", `${index + 1}번째 행동`);
    input.addEventListener("input", event => {
      state.actions[index] = event.target.value;
      saveState();
    });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove-action";
    remove.setAttribute("aria-label", `${index + 1}번째 행동 삭제`);
    remove.textContent = "×";
    remove.disabled = state.actions.length === 1;
    remove.addEventListener("click", () => {
      state.actions.splice(index, 1);
      saveState();
      renderActionRows();
    });
    row.append(marker, input, remove);
    list.append(row);
  });
}

function renderCollaboration(container) {
  container.innerHTML = `
    <p class="step-intro">도구 이름만 나열하지 말고, 누구와 어떤 정보를 주고받았는지 함께 정리합니다.</p>
    <div class="form-grid">
      <div class="field field-full"><label for="tools">사용한 도구·기법</label><input id="tools" name="tools" value="${escapeMarkup(state.tools.join(", "))}" placeholder="예: Excel 피벗테이블, VOC 분류표, 주간 회의"><small>쉼표로 구분해 주세요.</small></div>
      <div class="field field-full"><label for="collaboration">협업·의사소통 방식</label><textarea id="collaboration" name="collaboration" placeholder="누구와 무엇을 확인하고, 의견 차이를 어떻게 조정했나요?">${escapeMarkup(state.collaboration)}</textarea></div>
    </div>`;
}

function renderOutcome(container) {
  container.innerHTML = `
    <p class="step-intro">팀 전체 성과, 확인 가능한 근거, 내가 직접 기여한 부분을 분리하면 과장 없이도 강해집니다.</p>
    <div class="form-grid">
      <div class="field field-full"><label for="result">결과·변화</label><textarea id="result" name="result" placeholder="업무 시간, 오류, 이용자 반응, 산출물 등 실제로 달라진 점">${escapeMarkup(state.result)}</textarea></div>
      <div class="field"><label for="evidence">결과를 확인한 근거</label><textarea id="evidence" name="evidence" placeholder="예: 월간 처리 통계, 담당자 검토 의견, 최종 보고서">${escapeMarkup(state.evidence)}</textarea></div>
      <div class="field"><label for="contribution">내가 직접 기여한 범위</label><textarea id="contribution" name="contribution" placeholder="직접 작성·분석·제안·실행한 부분과 도움받은 부분">${escapeMarkup(state.contribution)}</textarea></div>
    </div>`;
}

function renderReview(container) {
  const review = [
    ["목표 직무", state.target_job], ["경험·업무명", state.experience_title], ["역할", state.role],
    ["상황", state.situation], ["판단", state.judgment], ["행동", state.actions.filter(Boolean).join(" / ")],
    ["결과", state.result], ["본인 기여", state.contribution]
  ];
  container.innerHTML = `
    <p class="step-intro">빠진 항목은 초안에서 [확인 필요]로 남습니다. 생성 후에도 돌아와 수정할 수 있습니다.</p>
    <ul class="review-list">${review.map(([label, value]) => `<li><span>${label}</span><b class="${value ? "" : "missing"}">${value ? "입력됨" : "확인 필요"}</b></li>`).join("")}</ul>
    <div class="field field-review"><label for="learning">배운 점·다음 적용</label><textarea id="learning" name="learning" placeholder="이 경험 이후 달라진 업무 방식이나 지원 직무에서 적용할 점">${escapeMarkup(state.learning)}</textarea></div>
    <div class="fact-check"><input id="facts_confirmed" name="facts_confirmed" type="checkbox" ${state.facts_confirmed ? "checked" : ""}><label for="facts_confirmed">위 내용은 내가 실제로 수행한 경험입니다.<span>팀 성과를 내 개인 성과로 바꾸거나, 확인하지 않은 수치를 입력하지 않았습니다.</span></label></div>`;
}

function bindInputs() {
  $$('input[name], textarea[name], select[name]', $("#step-content")).forEach(input => {
    if (input.closest("#action-list")) return;
    const eventName = input.type === "radio" || input.type === "checkbox" || input.tagName === "SELECT" ? "change" : "input";
    input.addEventListener(eventName, event => {
      const target = event.target;
      if (target.type === "radio" && !target.checked) return;
      if (target.name === "facts_confirmed") setField(target.name, target.checked);
      else if (target.name === "target_length") setField(target.name, Number(target.value));
      else if (target.name === "tools") setField("tools", target.value.split(",").map(item => item.trim()).filter(Boolean));
      else setField(target.name, target.value);
    });
  });
}

function showWorkspace() {
  const workspace = $("#workspace");
  workspace.hidden = false;
  renderStep();
  updateSelectedJob();
  workspace.scrollIntoView({ behavior: "smooth", block: "start" });
}

function updateSelectedJob() {
  $("#selected-job-title").textContent = state.target_job || "직무를 아직 선택하지 않았습니다";
  $("#selected-job-institution").textContent = state.target_job ? (state.institution || "기관 미지정") : "직접 입력해도 작성할 수 있어요.";
  const tags = $("#selected-job-tags");
  tags.replaceChildren();
  [state.ncs_path, state.matched_skill].filter(Boolean).slice(0, 2).forEach(value => {
    const tag = document.createElement("span");
    tag.textContent = value.length > 35 ? `${value.slice(0, 35)}…` : value;
    tags.append(tag);
  });
}

async function loadHealth() {
  try {
    const response = await fetch("/api/health");
    if (!response.ok) throw new Error();
    const data = await response.json();
    const stats = data.stats;
    $("#stat-institutions").textContent = Number(stats.institutions).toLocaleString("ko-KR");
    $("#stat-postings").textContent = Number(stats.postings).toLocaleString("ko-KR");
    $("#stat-profiles").textContent = Number(stats.profiles).toLocaleString("ko-KR");
    $("#status-label").textContent = stats.last_sync_at ? "데이터 동기화됨" : "수집 연결 대기";
    $("#open-sources").classList.toggle("is-live", Boolean(stats.last_sync_at));
  } catch {
    $("#status-label").textContent = "서버 연결 확인";
    ["institutions", "postings", "profiles"].forEach(key => $(`#stat-${key}`).textContent = "0");
  }
}

async function openJobDialog() {
  const dialog = $("#job-dialog");
  dialog.showModal();
  $("#job-search").focus();
  await searchJobs("");
}

async function searchJobs(query) {
  const stateLabel = $("#job-search-state");
  const results = $("#job-results");
  results.replaceChildren();
  stateLabel.textContent = "축적된 직무를 검색하고 있습니다…";
  try {
    const response = await fetch(`/api/jobs?q=${encodeURIComponent(query)}&limit=30`);
    if (!response.ok) throw new Error();
    const data = await response.json();
    if (!data.items.length) {
      stateLabel.textContent = query ? "일치하는 직무가 없습니다. 직접 입력하거나 다른 단어로 찾아보세요." : "아직 동기화된 직무가 없습니다. 직접 입력해 먼저 작성할 수 있습니다.";
      return;
    }
    stateLabel.textContent = `${data.items.length.toLocaleString("ko-KR")}개 직무를 찾았습니다.`;
    data.items.forEach(profile => results.append(createJobResult(profile)));
  } catch {
    stateLabel.textContent = "직무 데이터를 불러오지 못했습니다. 직접 입력으로 계속 진행할 수 있습니다.";
  }
}

function createJobResult(profile) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "job-result";
  const content = document.createElement("div");
  const title = document.createElement("strong");
  title.textContent = profile.job_title;
  const institution = document.createElement("small");
  institution.textContent = profile.institution_name;
  const tags = document.createElement("div");
  tags.className = "result-tags";
  [profile.ncs_path, ...(profile.skills || []).slice(0, 2)].filter(Boolean).slice(0, 3).forEach(value => {
    const tag = document.createElement("span");
    tag.textContent = value.length > 45 ? `${value.slice(0, 45)}…` : value;
    tags.append(tag);
  });
  const arrow = document.createElement("span");
  arrow.textContent = "→";
  content.append(title, institution, tags);
  button.append(content, arrow);
  button.addEventListener("click", () => selectJob(profile));
  return button;
}

function selectJob(profile) {
  state.institution = profile.institution_name || "";
  state.target_job = profile.job_title || "";
  state.ncs_path = profile.ncs_path || (profile.ncs_categories || []).join(" · ");
  state.matched_duty = (profile.duties || [])[0] || profile.summary || "";
  state.matched_skill = (profile.skills || [])[0] || "";
  saveState();
  updateSelectedJob();
  $("#job-dialog").close();
  if (!$("#workspace").hidden) {
    currentStep = 1;
    renderStep();
  } else {
    showWorkspace();
  }
}

async function loadSources() {
  const list = $("#source-list");
  list.replaceChildren();
  const loading = document.createElement("p");
  loading.className = "loading-line";
  loading.textContent = "출처를 확인하고 있습니다…";
  list.append(loading);
  try {
    const response = await fetch("/api/sources");
    if (!response.ok) throw new Error();
    const data = await response.json();
    list.replaceChildren();
    data.sources.forEach(source => {
      const item = document.createElement("article");
      item.className = "source-item";
      const body = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = source.name;
      const provider = document.createElement("small");
      provider.textContent = `${source.provider} · ${source.sync_mode.toUpperCase()}`;
      const note = document.createElement("p");
      note.textContent = source.license_note;
      body.append(title, provider, note);
      const badge = document.createElement("span");
      badge.className = `source-badge ${source.enabled ? "enabled" : ""}`;
      badge.textContent = source.enabled ? "수집 사용" : "검토 대기";
      item.append(body, badge);
      list.append(item);
    });
    $("#copyright-policy").href = data.copyright_policy_url;
  } catch {
    list.textContent = "출처 정보를 불러오지 못했습니다.";
  }
}

async function composeDraft() {
  if (!state.facts_confirmed) {
    $("#form-message").textContent = "실제 경험 확인란에 동의해야 초안을 만들 수 있습니다.";
    $("#facts_confirmed")?.focus();
    return;
  }
  const button = $("#next-step");
  button.disabled = true;
  button.textContent = "초안 작성 중…";
  try {
    const response = await fetch("/api/drafts/compose", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state)
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || "초안을 만들지 못했습니다.");
    $("#draft-output").textContent = data.output;
    $("#fact-coverage").textContent = `${data.fact_coverage}%`;
    const warnings = $("#warning-list");
    warnings.replaceChildren();
    data.warnings.forEach(message => {
      const item = document.createElement("p");
      item.className = "warning";
      item.textContent = message;
      warnings.append(item);
    });
    $("#result-dialog").showModal();
  } catch (error) {
    $("#form-message").textContent = error.message || "초안을 만들지 못했습니다. 잠시 후 다시 시도해 주세요.";
  } finally {
    button.disabled = false;
    button.textContent = "초안 만들기 ↗";
  }
}

function closeDialog(id) {
  const dialog = document.getElementById(id);
  if (dialog?.open) dialog.close();
}

function bindPage() {
  ["hero-start", "header-start"].forEach(id => $(`#${id}`).addEventListener("click", showWorkspace));
  $("#open-auth").addEventListener("click", () => {
    $("#auth-dialog").showModal();
    window.setTimeout(() => authState.status === "signed_in" ? $("#logout").focus() : $("#login-email").focus(), 20);
  });
  $("#login-form").addEventListener("submit", requestLogin);
  $("#logout").addEventListener("click", logout);
  $("#keep-local-draft").addEventListener("click", keepLocalDraft);
  $("#use-server-draft").addEventListener("click", useServerDraft);
  $("#browse-jobs").addEventListener("click", openJobDialog);
  $("#change-job").addEventListener("click", openJobDialog);
  $("#open-sources").addEventListener("click", () => { $("#source-dialog").showModal(); loadSources(); });
  $("#footer-sources").addEventListener("click", () => { $("#source-dialog").showModal(); loadSources(); });
  $("#job-search-form").addEventListener("submit", event => { event.preventDefault(); searchJobs($("#job-search").value); });
  $("#use-manual-job").addEventListener("click", () => {
    closeDialog("job-dialog");
    if ($("#workspace").hidden) showWorkspace();
    currentStep = 1;
    renderStep();
    window.setTimeout(() => $("#target_job")?.focus(), 50);
  });
  $("#previous-step").addEventListener("click", () => {
    if (currentStep > 0) { currentStep -= 1; saveState(); renderStep(); }
  });
  $("#next-step").addEventListener("click", () => {
    if (currentStep < stepDefinitions.length - 1) {
      currentStep += 1;
      saveState();
      renderStep();
      $("#builder-title").focus?.();
    } else composeDraft();
  });
  $("#reset-draft").addEventListener("click", resetDraft);
  $("#copy-draft").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("#draft-output").textContent);
      $("#copy-draft").textContent = "복사 완료 ✓";
      window.setTimeout(() => { $("#copy-draft").textContent = "초안 복사"; }, 1600);
    } catch {
      $("#draft-output").focus();
    }
  });
  $$('[data-close-dialog]').forEach(button => button.addEventListener("click", () => closeDialog(button.dataset.closeDialog)));
  $$("dialog").forEach(dialog => dialog.addEventListener("click", event => {
    if (event.target === dialog) dialog.close();
  }));
}

bindPage();
loadHealth();
(async () => {
  await consumeLoginToken();
  await loadAuth();
})();
