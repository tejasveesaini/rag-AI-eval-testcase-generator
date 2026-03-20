const state = {
  selectedKey: "",
  activeSuite: "enriched",
  activeWorkspaceTab: "story",
  activeTestsTab: "generated",
  workspace: null,
  issue: null,
  evaluation: null,
  generationLatency: null,
  logs: [],
  history: [],
  selectedTests: [],
  suiteSummaryExpanded: false,
  isBusy: false,
  busyAction: null,
  busyLabel: "",
};

function runLabel(mode) {
  return "Context-backed suite";
}

async function requestJson(url, options = {}) {
  const { timeoutMs = 30000, ...fetchOptions } = options;
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(url, {
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      ...fetchOptions,
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.message || `Request failed with status ${response.status}`);
    }
    return data;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error(`Request timed out after ${Math.round(timeoutMs / 1000)} seconds.`);
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function buttonMatchesBusyAction(button, actionKey) {
  if (!actionKey) return false;
  if (button.dataset.action === actionKey) return true;
  return (
    (actionKey === "load" && button.id === "loadIssueButton")
    || (actionKey === "push" && button.id === "pushSelectedButton")
    || (actionKey === "history" && button.id === "refreshHistoryButton")
  );
}

function spinnerMarkup(label) {
  return `
    <span class="button-spinner" aria-hidden="true"></span>
    <span>${escapeHtml(label)}</span>
  `;
}

function rememberButtonMarkup(button) {
  if (!button.dataset.defaultHtml) {
    button.dataset.defaultHtml = button.innerHTML;
  }
}

function restoreButtonMarkup(button) {
  if (button.dataset.defaultHtml) {
    button.innerHTML = button.dataset.defaultHtml;
  }
  button.classList.remove("is-busy");
}

function renderBusyState() {
  document.body.classList.toggle("busy", state.isBusy);
  const issueInput = document.getElementById("issueKeyInput");
  if (issueInput) {
    issueInput.disabled = state.isBusy;
  }

  const managedButtons = document.querySelectorAll(
    "#loadIssueButton, #refreshHistoryButton, #pushSelectedButton, [data-action], [data-issue-select]",
  );

  managedButtons.forEach((button) => {
    rememberButtonMarkup(button);
    button.disabled = state.isBusy;
    if (state.isBusy && buttonMatchesBusyAction(button, state.busyAction)) {
      button.innerHTML = spinnerMarkup(state.busyLabel || "Working...");
      button.classList.add("is-busy");
    } else {
      restoreButtonMarkup(button);
    }
  });
}

function setBusy(isBusy, actionKey = null, busyLabel = "") {
  state.isBusy = isBusy;
  state.busyAction = isBusy ? actionKey : null;
  state.busyLabel = isBusy ? busyLabel : "";
  renderBusyState();
}

// ── Banner helpers ────────────────────────────────────────────────────────────
// bannerSteps: null | Array<{ label: string, status: "pending"|"active"|"done"|"error" }>

const _bannerState = { steps: null };

function showBanner(message, tone = "neutral") {
  _bannerState.steps = null;
  const banner = document.getElementById("actionBanner");
  banner.className = `action-banner ${tone}`;
  banner.innerHTML = `<span class="banner-message">${escapeHtml(message)}</span>`;
}

/**
 * Start a multi-step banner.
 * @param {string[]} stepLabels  — ordered list of step descriptions
 * @param {string}   tone        — "neutral" | "success" | "warning"
 */
function startBannerSteps(stepLabels, tone = "neutral") {
  _bannerState.steps = stepLabels.map((label) => ({ label, status: "pending" }));
  _renderBannerSteps(tone);
}

/**
 * Advance a step's status.
 * @param {number} index   — 0-based step index
 * @param {"active"|"done"|"error"} status
 * @param {string} [overrideLabel]  — optional updated label for the step
 * @param {string} tone
 */
function setBannerStep(index, status, overrideLabel = null, tone = "neutral") {
  if (!_bannerState.steps) return;
  if (overrideLabel !== null) _bannerState.steps[index].label = overrideLabel;
  _bannerState.steps[index].status = status;
  _renderBannerSteps(tone);
}

function _renderBannerSteps(tone) {
  const banner = document.getElementById("actionBanner");
  banner.className = `action-banner ${tone}`;
  const steps = _bannerState.steps;
  if (!steps) return;

  const icons = {
    pending: `<span class="step-icon pending" aria-hidden="true">·</span>`,
    active: `<span class="step-icon active"  aria-hidden="true"><span class="step-spinner"></span></span>`,
    done: `<span class="step-icon done"    aria-hidden="true">✓</span>`,
    info: `<span class="step-icon info"    aria-hidden="true">i</span>`,
    error: `<span class="step-icon error"   aria-hidden="true">✕</span>`,
  };

  const html = steps.map((s) =>
    `<li class="banner-step ${s.status}">${icons[s.status]}<span>${escapeHtml(s.label)}</span></li>`
  ).join("");

  banner.innerHTML = `<ol class="banner-steps" aria-label="Progress">${html}</ol>`;
}

function openPushSuccessModal({ issueKey, storyUrl, createdCount }) {
  const modal = document.getElementById("pushSuccessModal");
  const message = document.getElementById("pushSuccessMessage");
  const link = document.getElementById("openStoryInJiraLink");
  if (!modal || !message || !link) return;

  const countLabel = createdCount === 1 ? "1 test case was" : `${createdCount} test cases were`;
  message.textContent = `${countLabel} added to ${issueKey}. Open the Jira story to review the newly added test cases.`;
  link.href = storyUrl || "#";
  link.toggleAttribute("aria-disabled", !storyUrl);
  link.classList.toggle("is-disabled", !storyUrl);
  modal.hidden = false;
}

function closePushSuccessModal() {
  const modal = document.getElementById("pushSuccessModal");
  if (!modal) return;
  modal.hidden = true;
}

function pickActiveSuite(issue) {
  return "enriched";
}

function compactText(value, fallback = "None") {
  const text = Array.isArray(value) ? value.filter(Boolean).join("; ") : String(value ?? "").trim();
  return text || fallback;
}

function countSections(value) {
  return String(value ?? "")
    .split(/\n+/)
    .map((item) => item.trim())
    .filter(Boolean)
    .length;
}

function bulletListFromBlock(value, emptyMessage) {
  const items = String(value ?? "")
    .split(/\n+/)
    .map((item) => item.trim())
    .filter(Boolean);

  if (!items.length) {
    return `<div class="empty-state">${escapeHtml(emptyMessage)}</div>`;
  }

  return `
    <ul class="story-detail-list">
      ${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}
    </ul>
  `;
}

function acTagList(value, emptyMessage) {
  // Strip leading bullet/dash markers the same way bulletListFromBlock does,
  // then render each criterion as a tagged row: [AC-N chip] + text.
  const items = String(value ?? "")
    .split(/\n+/)
    .map((item) => item.replace(/^[\s\-\*•]+/, "").trim())
    .filter(Boolean);

  if (!items.length) {
    return `<div class="empty-state">${escapeHtml(emptyMessage)}</div>`;
  }

  return `
    <ul class="ac-tag-list">
      ${items.map((item, i) => `
        <li class="ac-tag-row">
          <span class="ac-tag-chip">AC-${i + 1}</span>
          <span class="ac-tag-text">${escapeHtml(item)}</span>
        </li>`).join("")}
    </ul>
  `;
}

function paragraphBlocks(value, emptyMessage) {
  const items = String(value ?? "")
    .split(/\n+/)
    .map((item) => item.trim())
    .filter(Boolean);

  if (!items.length) {
    return `<div class="empty-state">${escapeHtml(emptyMessage)}</div>`;
  }

  return `
    <div class="story-text-block">
      ${items.map((item) => `<p>${escapeHtml(item)}</p>`).join("")}
    </div>
  `;
}

function extractAcceptanceFromDescription(text) {
  const source = String(text ?? "").replace(/\r/g, "").trim();
  if (!source) {
    return null;
  }

  const headingPattern = /(?:^|\n)\s*(Acceptance Criteria|Acceptance)\s*:?\s*(?:\n|$)/i;
  const match = headingPattern.exec(source);
  if (!match) {
    return null;
  }

  const descriptionText = source.slice(0, match.index).trim();
  const acceptanceText = source.slice(match.index + match[0].length).trim();
  if (!acceptanceText) {
    return null;
  }

  return { descriptionText, acceptanceText };
}

function getStorySections(story) {
  const rawDescription = String(story?.description ?? "").replace(/\r/g, "").trim();
  const rawAcceptance = String(story?.acceptance_criteria ?? "").replace(/\r/g, "").trim();
  const extracted = extractAcceptanceFromDescription(rawDescription);

  if (rawAcceptance) {
    return {
      descriptionText: extracted?.descriptionText || rawDescription,
      acceptanceText: rawAcceptance,
    };
  }

  if (extracted) {
    return extracted;
  }

  return {
    descriptionText: rawDescription,
    acceptanceText: "",
  };
}

function compactList(items, emptyMessage) {
  if (!items?.length) {
    return `<div class="empty-state">${escapeHtml(emptyMessage)}</div>`;
  }

  return `
    <ul class="story-linked-list dense">
      ${items.map((item) => `<li>${item}</li>`).join("")}
    </ul>
  `;
}

function normalizeTitleKey(value) {
  return String(value ?? "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim()
    .replace(/\s+/g, " ");
}

function isTestLikeIssueType(issueType) {
  const normalized = String(issueType ?? "").toLowerCase().replace(/[\s_-]+/g, "");
  return normalized.includes("test") || normalized === "subtask";
}

function tokenizeForSimilarity(value) {
  return new Set(
    normalizeTitleKey(value)
      .split(" ")
      .filter((token) => token.length > 2),
  );
}

function jaccardSimilarity(left, right) {
  if (!left.size || !right.size) return 0;
  let overlap = 0;
  left.forEach((token) => {
    if (right.has(token)) overlap += 1;
  });
  const union = new Set([...left, ...right]).size;
  return union ? overlap / union : 0;
}

function getExistingStoryTests(issue) {
  const linked = issue?.story?.linked_issues || [];
  const seenKeys = new Set();
  return linked
    .filter((item) => item?.key && item?.summary && isTestLikeIssueType(item.issue_type))
    .filter((item) => {
      if (seenKeys.has(item.key)) return false;
      seenKeys.add(item.key);
      return true;
    });
}

function getVisibleGeneratedTests(issue, suite) {
  const existingTitles = getExistingStoryTests(issue)
    .map((item) => item.summary)
    .filter(Boolean);
  const existingTitleKeys = new Set(existingTitles.map((title) => normalizeTitleKey(title)).filter(Boolean));
  const existingTitleTokens = existingTitles.map((title) => tokenizeForSimilarity(title));

  return (suite?.tests || [])
    .map((test, index) => ({ test, index }))
    .filter(({ test }) => {
      const normalizedTitle = normalizeTitleKey(test.title);
      if (existingTitleKeys.has(normalizedTitle)) {
        return false;
      }

      const testTokens = tokenizeForSimilarity(test.title);
      return !existingTitleTokens.some((tokens) => jaccardSimilarity(testTokens, tokens) >= 0.55);
    });
}

function renderContextItems(items, emptyMessage) {
  return compactList(
    (items || []).map((item) => {
      const key = item.key ? `<strong>${escapeHtml(item.key)}</strong>` : "";
      const issueType = item.issue_type ? `<span class="context-inline-tag">${escapeHtml(item.issue_type)}</span>` : "";
      const summary = escapeHtml(item.summary || item.short_text || "No summary");
      const hint = item.relevance_hint ? `<span class="context-inline-note">${escapeHtml(item.relevance_hint)}</span>` : "";
      return `${key}${key ? " " : ""}${issueType} ${summary}${hint ? ` ${hint}` : ""}`.trim();
    }),
    emptyMessage,
  );
}

async function loadWorkspace() {
  const workspace = await requestJson("/api/workspace");
  state.workspace = workspace;
  renderWorkspace();
  renderBusyState();
}

async function loadIssue(issueKey) {
  if (!issueKey) return;
  state.selectedKey = issueKey.toUpperCase();
  document.getElementById("issueKeyInput").value = state.selectedKey;
  const issue = await requestJson(`/api/issue/${encodeURIComponent(state.selectedKey)}`);
  issue.suites = { enriched: null };
  if (issue.files) {
    issue.files.enriched = false;
  }
  state.issue = issue;
  state.activeSuite = pickActiveSuite(issue);
  state.activeTestsTab = "existing";
  state.selectedTests = [];
  state.evaluation = null;
  state.generationLatency = null;
  state.suiteSummaryExpanded = false;
  await refreshHistory();
  renderWorkspace();
  renderIssue();
  renderBusyState();
}

async function refreshLogs() {
  const data = await requestJson("/api/logs", { timeoutMs: 10000 });
  state.logs = data.logs || [];
  renderConsole();
}

async function refreshHistory() {
  const data = await requestJson("/api/push-history", { timeoutMs: 15000 });
  state.history = data.history || [];
  renderHistory();
}

async function runAction(path, payload, options = {}) {
  const result = await requestJson(path, {
    method: "POST",
    body: JSON.stringify(payload),
    timeoutMs: options.timeoutMs,
  });

  if (result.payload?.workspace) {
    state.workspace = result.payload.workspace;
  }
  if (result.payload?.issue) {
    state.issue = result.payload.issue;
    state.activeSuite = pickActiveSuite(state.issue);
  }
  // Only update evaluation state when the evaluate action ran — not on generate.
  // This prevents the Evaluate tab from showing stale/partial data after generation.
  if (path === "/api/actions/evaluate" && result.payload?.evaluation) {
    state.evaluation = result.payload.evaluation;
  }
  // Capture generation latency separately so it can be shown on the Tests tab
  // without polluting the Evaluate tab.
  if (path === "/api/actions/generate" && result.payload?.latency) {
    state.generationLatency = result.payload.latency;
  }
  if (result.payload?.history) {
    state.history = result.payload.history;
  }
  if (result.payload?.logs?.logs) {
    state.logs = result.payload.logs.logs;
  } else {
    await refreshLogs();
  }

  renderWorkspace();
  renderIssue();
  renderEvaluation();
  renderHistory();
  renderConsole();
  renderBusyState();

  // If a step-banner is active, let the caller manage the banner rather than
  // overwriting the progress list with the generic server message.
  if (!_bannerState.steps) {
    const tone = result.ok
      && !(result.payload?.evaluation && !result.payload.evaluation.passed)
      && !((result.payload?.failed || []).length)
      ? "success"
      : "warning";
    showBanner(result.message, tone);
  }
  return result;
}

async function runLoadFlow(issueKey) {
  setBusy(true, "load", "Loading story...");
  startBannerSteps([
    `Fetching story ${issueKey} from Jira`,
    "Collecting & indexing context (linked defects, related stories)",
    "Loading workspace",
  ]);
  try {
    state.activeWorkspaceTab = "story";
    renderWorkspaceTabs();

    setBannerStep(0, "active");
    await runAction("/api/actions/fetch", { issue_key: issueKey }, { timeoutMs: 60000 });
    setBannerStep(0, "done");

    setBannerStep(1, "active");
    try {
      await runAction("/api/actions/collect-context", { issue_key: issueKey }, { timeoutMs: 90000 });
      setBannerStep(1, "done");
    } catch (error) {
      setBannerStep(1, "error", `Context refresh failed: ${error.message}`);
    }

    setBannerStep(2, "active");
    await loadIssue(issueKey);
    setBannerStep(2, "done", "Workspace ready", "success");
    _renderBannerSteps("success");
  } catch (error) {
    const detail = error.message.includes("timed out")
      ? `${error.message} The backend may still be working. Open Console to inspect progress.`
      : error.message;
    showBanner(detail, "warning");
    await refreshLogs();
  } finally {
    setBusy(false);
  }
}

function renderWorkspace() {
  const workspace = state.workspace;
  if (!workspace) return;

  const envPill = document.getElementById("envPill");
  envPill.textContent = workspace.env_present ? ".env detected" : ".env missing";
  envPill.className = workspace.env_present ? "status-pill success" : "status-pill warning";

  const issueRail = document.getElementById("issueRail");
  if (!workspace.issues.length) {
    issueRail.innerHTML = `<div class="empty-state">No recent stories yet.</div>`;
    return;
  }

  issueRail.innerHTML = workspace.issues
    .map((issue) => {
      const activeClass = issue.key === state.selectedKey ? " issue-item-active" : "";

      return `
        <button class="issue-item${activeClass}" data-issue-select="${escapeHtml(issue.key)}">
          <span class="issue-key">${escapeHtml(issue.key)}</span>
          ${issue.last_updated ? `<span class="issue-updated">${escapeHtml(issue.last_updated)}</span>` : ""}
        </button>
      `;
    })
    .join("");

  document.querySelectorAll("[data-issue-select]").forEach((button) => {
    button.addEventListener("click", async () => {
      setBusy(true);
      try {
        await loadIssue(button.dataset.issueSelect);
        state.activeWorkspaceTab = "story";
        renderWorkspaceTabs();
        showBanner(`Loaded story ${button.dataset.issueSelect}.`, "neutral");
      } finally {
        setBusy(false);
      }
    });
  });
}

function renderIssue() {
  const issue = state.issue;
  const activeIssuePill = document.getElementById("activeIssuePill");
  if (!issue) {
    activeIssuePill.textContent = "No story loaded";
    return;
  }

  activeIssuePill.textContent = `Active story: ${issue.issue_key}`;
  document.getElementById("workspaceTitle").innerHTML = `
    <span class="workspace-title-token">${escapeHtml(issue.issue_key)}</span>
    <span class="workspace-title-separator">/</span>
    <span class="workspace-title-token">${escapeHtml(issue.summary || "Untitled Story")}</span>
  `;
  document.getElementById("workspaceSubtitle").textContent =
    "Story details, context, generation, evaluation, and push flow.";

  renderStory();
  renderSuite();
}

function renderStory() {
  const issue = state.issue;
  const storySummary = document.getElementById("storySummary");
  const signals = document.getElementById("storySignals");
  const contextPanel = document.getElementById("contextPanel");

  if (!issue || !issue.story) {
    storySummary.className = "summary-surface empty-state";
    storySummary.textContent = "Load a Jira story to populate story details.";
    signals.className = "content-body empty-state";
    signals.textContent = "Labels, components, and related story references will appear here.";
    contextPanel.className = "content-body empty-state";
    contextPanel.textContent = "No context collected yet.";
    return;
  }

  const story = issue.story;
  const context = issue.context;
  const suite = issue.suites?.[pickActiveSuite(issue)];
  const storySections = getStorySections(story);
  storySummary.className = "summary-surface";
  storySummary.innerHTML = `
    <div class="story-detail-grid">
      <div class="detail-block detail-block-wide">
        <div class="story-detail-row">
          <span class="detail-key">Description</span>
          <div class="detail-value">
            ${paragraphBlocks(storySections.descriptionText, "No story description available.")}
          </div>
        </div>
        <div class="story-detail-row">
          <span class="detail-key">Acceptance Criteria</span>
          <div class="detail-value">
            ${acTagList(storySections.acceptanceText, "No acceptance criteria extracted.")}
          </div>
        </div>
      </div>
    </div>
  `;

  const labels = (story.labels || []).map((item) => `<span>${escapeHtml(item)}</span>`).join("") || "<span>none</span>";
  const components = (story.components || []).map((item) => `<span>${escapeHtml(item)}</span>`).join("") || "<span>none</span>";
  const linkedIssues = (story.linked_issues || [])
    .map((item) => `<li><strong>${escapeHtml(item.key)}</strong> · ${escapeHtml(item.issue_type)} · ${escapeHtml(item.summary)}</li>`)
    .join("") || "<li>No linked stories</li>";

  signals.className = "content-body";
  signals.innerHTML = `
    <div class="story-meta-group">
      <span class="compact-label">Labels</span>
      <div class="story-chip-row">${labels}</div>
    </div>
    <div class="story-meta-group">
      <span class="compact-label">Components</span>
      <div class="story-chip-row">${components}</div>
    </div>
    <div class="story-meta-group">
      <span class="compact-label">Linked Stories</span>
      <ul class="story-linked-list">${linkedIssues}</ul>
    </div>
  `;

  if (!context) {
    contextPanel.className = "content-body empty-state";
    contextPanel.textContent = "No context collected yet.";
    return;
  }

  contextPanel.className = "content-body";
  contextPanel.innerHTML = `
    <div class="context-stat-strip">
      <div class="context-stat-chip"><strong>${(context.linked_defects || []).length}</strong><span>Linked defects</span></div>
      <div class="context-stat-chip"><strong>${(context.historical_tests || []).length}</strong><span>Historical tests</span></div>
      <div class="context-stat-chip"><strong>${(context.related_stories || []).length}</strong><span>Related stories</span></div>
    </div>
    <div class="context-section">
      <span class="compact-label">Coverage hints</span>
      ${compactList((context.coverage_hints || []).map((item) => escapeHtml(item)), "No coverage hints derived.")}
    </div>
    <div class="context-section">
      <span class="compact-label">Linked defects</span>
      ${renderContextItems(context.linked_defects, "No linked defects packaged.")}
    </div>
    <div class="context-section">
      <span class="compact-label">Historical tests</span>
      ${renderContextItems(context.historical_tests, "No historical tests packaged.")}
    </div>
    <div class="context-section">
      <span class="compact-label">Related stories</span>
      ${renderContextItems(context.related_stories, "No related stories packaged.")}
    </div>
  `;
}

function renderTestsSubtabs(generatedCount, existingCount) {
  const subtabbar = document.getElementById("testsSubtabbar");
  if (!subtabbar) return;

  subtabbar.querySelectorAll("[data-tests-tab]").forEach((button) => {
    const tab = button.dataset.testsTab;
    const count = tab === "existing" ? existingCount : generatedCount;
    const label = tab === "existing" ? "Existing" : "Generated";
    button.textContent = `${label} (${count})`;
    button.classList.toggle("active", tab === state.activeTestsTab);
  });

  const generatedPanel = document.getElementById("generatedTestsPanel");
  const existingPanel = document.getElementById("existingTestsPanel");
  generatedPanel?.classList.toggle("active", state.activeTestsTab === "generated");
  existingPanel?.classList.toggle("active", state.activeTestsTab === "existing");
}

function renderSuite() {
  const issue = state.issue;
  const suiteSummary = document.getElementById("suiteSummary");
  const suiteList = document.getElementById("suiteList");
  const existingTestsList = document.getElementById("existingTestsList");
  const runLabelEl = document.getElementById("suiteRunLabel");
  const selectAll = document.getElementById("testSelectAll");

  if (!issue) {
    suiteSummary.className = "summary-surface empty-state";
    suiteSummary.textContent = "No story selected.";
    state.suiteSummaryExpanded = false;
    suiteList.innerHTML = "";
    existingTestsList.innerHTML = `<div class="empty-state">No existing story tests found.</div>`;
    runLabelEl.textContent = "No generated suite selected yet.";
    if (selectAll) {
      selectAll.checked = false;
      selectAll.indeterminate = false;
    }
    renderTestsSubtabs(0, 0);
    updatePushButtonCount();
    return;
  }

  const existingTests = getExistingStoryTests(issue);
  const suite = issue.suites?.[state.activeSuite];
  runLabelEl.textContent = suite ? runLabel(state.activeSuite) : "No generated suite selected yet.";

  if (!suite) {
    suiteSummary.className = "summary-surface empty-state";
    suiteSummary.textContent = "Generate test cases to inspect them here.";
    state.suiteSummaryExpanded = false;
    suiteList.innerHTML = "";
    existingTestsList.innerHTML = existingTests.length
      ? existingTests
        .map((item) => `
          <div class="existing-test-row">
            <div class="existing-test-key">${escapeHtml(item.key)}</div>
            <div class="existing-test-copy">
              <strong>${escapeHtml(item.summary)}</strong>
              <p>${escapeHtml(item.issue_type)}</p>
            </div>
          </div>
        `)
        .join("")
      : `<div class="empty-state">No existing story tests found.</div>`;
    if (selectAll) {
      selectAll.checked = false;
      selectAll.indeterminate = false;
    }
    renderTestsSubtabs(0, existingTests.length);
    updatePushButtonCount();
    return;
  }

  const visibleTests = getVisibleGeneratedTests(issue, suite);

  const typeSummary = Object.entries(suite.type_counts || {})
    .map(([label, count]) => `<span>${escapeHtml(label)}: ${escapeHtml(count)}</span>`)
    .join("");

  const visibleIndices = visibleTests.map(({ index }) => index);
  const visibleIndexSet = new Set(visibleIndices);
  const selected = new Set(state.selectedTests.filter((index) => visibleIndexSet.has(index)));
  state.selectedTests = [...selected].sort((a, b) => a - b);
  const contextStatus = issue.context ? "Context ready" : "Context pending";
  const visibleCount = visibleTests.length;
  const hiddenExistingCount = suite.tests.length - visibleCount;
  const testLabel = visibleCount === 1 ? "test" : "tests";
  suiteSummary.className = "summary-surface suite-summary-shell";
  suiteSummary.innerHTML = `
    <details class="suite-summary-fold"${state.suiteSummaryExpanded ? " open" : ""}>
      <summary class="suite-summary-toggle">
        <div class="suite-overview-head">
          <div class="suite-overview-title">
            <strong>${visibleCount} ${testLabel}</strong>
            <span>${escapeHtml(runLabel(state.activeSuite))}</span>
          </div>
          <div class="suite-summary-meta">
            <div class="status-cluster">
              <span class="status-pill muted">${contextStatus}</span>
              ${typeSummary}
            </div>
            <span class="expand-indicator" title="Toggle suite summary" aria-hidden="true">
              <svg viewBox="0 0 16 16" fill="none">
                <path d="M6 3.5L10.5 8L6 12.5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"></path>
              </svg>
            </span>
          </div>
        </div>
      </summary>
      <div class="suite-summary-body">
        ${hiddenExistingCount > 0 ? `<div class="suite-note-inline">${hiddenExistingCount} generated test case${hiddenExistingCount === 1 ? "" : "s"} hidden because matching story tests already exist.</div>` : ""}
        ${suite.notes ? `<div class="suite-note-inline">${escapeHtml(suite.notes)}</div>` : ""}
      </div>
    </details>
  `;
  suiteSummary.querySelector(".suite-summary-fold")?.addEventListener("toggle", (event) => {
    state.suiteSummaryExpanded = event.currentTarget.open;
  });

  const testRows = visibleTests
    .map(({ test, index }, visiblePosition) => {
      const steps = (test.steps || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
      const preconditions = compactText(test.preconditions);
      return `
        <details class="test-card compact">
          <summary class="test-summary-row">
            <label class="test-select-wrap" onclick="event.stopPropagation()">
              <input type="checkbox" class="test-select-checkbox" data-index="${index}" ${selected.has(index) ? "checked" : ""}>
            </label>
            <span class="test-index">${visiblePosition + 1}</span>
            <div class="test-summary-copy">
              <strong>${escapeHtml(test.title)}</strong>
              <span>${escapeHtml(preconditions)}</span>
            </div>
            <div class="test-cell"><span class="table-chip">${escapeHtml(test.test_type)}</span></div>
            <div class="test-cell"><span class="table-chip">${escapeHtml(test.priority)}</span></div>
            <div class="test-meta">
              <span class="suite-inline-chip">${escapeHtml(test.coverage_tag || "untagged")}</span>
              <span class="expand-indicator" title="Toggle test details" aria-hidden="true">
                <svg viewBox="0 0 16 16" fill="none">
                  <path d="M6 3.5L10.5 8L6 12.5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"></path>
                </svg>
              </span>
            </div>
            <div class="test-result">${escapeHtml(test.expected_result || "No expected result provided.")}</div>
          </summary>
          <div class="test-detail-panel">
            <div class="test-detail-grid">
              <div>
                <h4>Preconditions</h4>
                <p>${escapeHtml(preconditions)}</p>
              </div>
              <div>
                <h4>Expected Result</h4>
                <p>${escapeHtml(test.expected_result)}</p>
              </div>
            </div>
            <div class="steps-block compact">
              <h4>Steps</h4>
              <ol>${steps}</ol>
            </div>
          </div>
        </details>
      `;
    })
    .join("");

  suiteList.innerHTML = `
    <div class="tests-table-head">
      <span></span>
      <span>#</span>
      <span>Test Case</span>
      <span>Type</span>
      <span>Priority</span>
      <span>Tag</span>
      <span class="column-expected">Expected Result</span>
    </div>
    ${testRows || `<div class="empty-state tests-empty-state">No new generated tests to show. Existing story tests are available in the Existing subtab.</div>`}
  `;

  existingTestsList.innerHTML = existingTests.length
    ? existingTests
      .map((item) => `
        <div class="existing-test-row">
          <div class="existing-test-key">${escapeHtml(item.key)}</div>
          <div class="existing-test-copy">
            <strong>${escapeHtml(item.summary)}</strong>
            <p>${escapeHtml(item.issue_type)}</p>
          </div>
        </div>
      `)
      .join("")
    : `<div class="empty-state">No existing story tests found.</div>`;

  suiteList.querySelectorAll(".test-select-checkbox").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      const index = Number.parseInt(checkbox.dataset.index, 10);
      const next = new Set(state.selectedTests);
      if (checkbox.checked) {
        next.add(index);
      } else {
        next.delete(index);
      }
      state.selectedTests = [...next].sort((a, b) => a - b);
      updatePushButtonCount();
    });
  });

  selectAll.checked = visibleIndices.length > 0 && state.selectedTests.length === visibleIndices.length;
  selectAll.indeterminate = state.selectedTests.length > 0 && state.selectedTests.length < visibleIndices.length;
  selectAll.disabled = visibleIndices.length === 0;
  renderTestsSubtabs(visibleCount, existingTests.length);
  updatePushButtonCount();

  // Update the generate button based on the current saved suite size.
  const hasTests = suite.tests.length > 0;
  const missingToTarget = Math.max(10 - suite.tests.length, 0);
  const generateLabel = document.getElementById("generateButtonLabel");
  if (generateLabel) {
    generateLabel.textContent = !hasTests
      ? "Generate Test Cases"
      : missingToTarget > 0
        ? `Fill to 10 (${missingToTarget} more)`
        : "Generate Next 10";
  }
  const generateBtn = document.getElementById("generateButton");
  if (generateBtn) {
    const icon = generateBtn.querySelector(".button-icon svg");
    if (icon) {
      icon.innerHTML = hasTests
        ? '<path d="M8 2.5v11"/><path d="M4.5 9.5 8 13l3.5-3.5"/>'
        : '<path d="M8 2.5v11"/><path d="M2.5 8h11"/>';
    }
  }
}

function updatePushButtonCount() {
  const button = document.getElementById("pushSelectedButton");
  const suite = state.issue?.suites?.[state.activeSuite];
  button.disabled = !suite || state.selectedTests.length === 0 || state.activeTestsTab !== "generated";
  button.dataset.defaultHtml = `
    <span class="button-icon">
      <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8h10"/><path d="M9.5 4.5 13 8l-3.5 3.5"/></svg>
    </span>
    <span>Push Selected (${state.selectedTests.length})</span>
  `;
  if (!state.isBusy || !buttonMatchesBusyAction(button, state.busyAction)) {
    button.innerHTML = button.dataset.defaultHtml;
  }
}

async function runLoadNextAction() {
  const issueKey = document.getElementById("issueKeyInput").value.trim().toUpperCase();
  if (!issueKey) {
    showBanner("Enter a story key first.", "warning");
    return;
  }
  const mode = state.activeSuite;
  const suite = state.issue?.suites?.[mode];
  const offset = suite?.tests?.length ?? 0;
  const needsTopUp = offset > 0 && offset < 5;
  const max_tests = needsTopUp ? 5 - offset : 5;
  const progressLabel = needsTopUp ? `Filling to 5 (${max_tests} more)...` : "Loading next 5...";

  setBusy(true, "load-next", progressLabel);
  try {
    const result = await requestJson("/api/actions/generate", {
      method: "POST",
      body: JSON.stringify({ issue_key: issueKey, mode, max_tests, offset }),
    });

    if (result.payload?.issue) {
      state.issue = result.payload.issue;
    }
    if (result.payload?.logs?.logs) {
      state.logs = result.payload.logs.logs;
    }
    renderSuite();
    renderWorkspace();
    showBanner(result.message, result.ok ? "success" : "warning");
  } catch (error) {
    showBanner(error.message, "warning");
    await refreshLogs();
  } finally {
    setBusy(false);
  }
}

function renderEvaluation() {
  const summary = document.getElementById("evaluationSummary");
  const metrics = document.getElementById("deepEvalMetrics");
  const context = document.getElementById("evaluationContext");
  const runLabelEl = document.getElementById("evaluationRunLabel");
  const latencyStrip = document.getElementById("latencyStrip");
  const perTestSection = document.getElementById("perTestRelevancy");
  const perTestMeta = document.getElementById("perTestRelevancyMeta");
  const perTestList = document.getElementById("perTestRelevancyList");
  const acSection = document.getElementById("acCoverageSection");
  const acMeta = document.getElementById("acCoverageMeta");
  const acList = document.getElementById("acCoverageList");
  const hallucSection = document.getElementById("hallucinationSection");
  const hallucMeta = document.getElementById("hallucinationMeta");
  const hallucList = document.getElementById("hallucinationList");

  runLabelEl.textContent = `Current suite: ${runLabel(state.activeSuite)}`;

  const reset = () => {
    summary.className = "summary-surface empty-state";
    summary.textContent = "Run evaluation to inspect DeepEval metrics and context relevance.";
    metrics.innerHTML = "";
    context.innerHTML = "";
    latencyStrip.hidden = true;
    latencyStrip.innerHTML = "";
    perTestSection.hidden = true;
    acSection.hidden = true;
    hallucSection.hidden = true;
  };

  if (!state.evaluation) {
    reset();
    return;
  }

  const ev = state.evaluation;
  const deep = ev.deepeval;
  const latency = ev.latency || {};
  const acCov = ev.ac_coverage || {};
  const halluc = ev.hallucination || {};

  // ── Summary banner ─────────────────────────────────────────────────────────
  summary.className = ev.passed
    ? "summary-surface evaluation-summary pass"
    : "summary-surface evaluation-summary fail";
  summary.innerHTML = `
    <strong>${ev.passed ? "Evaluation passed" : "Evaluation needs attention"}</strong>
    <span>${escapeHtml(runLabel(ev.mode))}${deep?.judge_model ? ` · Judge: ${escapeHtml(deep.judge_model)}` : ""}</span>
  `;

  // ── Latency strip ──────────────────────────────────────────────────────────
  const genSec = latency.generation_seconds;
  const evalSec = latency.evaluation_seconds;
  if (genSec != null || evalSec != null) {
    const chips = [];
    if (genSec != null) chips.push(`<span class="latency-chip">⏱ Generation: <strong>${genSec.toFixed(1)}s</strong></span>`);
    if (evalSec != null) chips.push(`<span class="latency-chip">⏱ Evaluation: <strong>${evalSec.toFixed(1)}s</strong></span>`);
    if (latency.recorded_at) chips.push(`<span class="latency-chip muted">Recorded ${escapeHtml(latency.recorded_at.replace("T", " ").slice(0, 19))} UTC</span>`);
    latencyStrip.innerHTML = chips.join("");
    latencyStrip.hidden = false;
  } else {
    latencyStrip.hidden = true;
  }

  // ── Suite-level DeepEval metrics ───────────────────────────────────────────
  if (deep) {
    metrics.innerHTML = (deep.metrics || [])
      .map((metric) => `
        <article class="metric-card-detail ${metric.passed ? "pass" : metric.skipped ? "skipped" : "fail"}">
          <div class="metric-card-head">
            <strong>${escapeHtml(metric.name)}</strong>
            <span>${metric.passed ? "PASS" : metric.skipped ? "SKIPPED" : "FAIL"}</span>
          </div>
          <p>Score: ${escapeHtml(metric.score?.toFixed ? metric.score.toFixed(3) : String(metric.score ?? "—"))} / threshold ${escapeHtml(String(metric.threshold))}</p>
          <p>${escapeHtml(metric.reason)}</p>
        </article>
      `)
      .join("");
  } else {
    metrics.innerHTML = "";
  }

  // ── Per-test relevancy ─────────────────────────────────────────────────────
  const ptr = deep?.per_test_relevancy;
  if (ptr && ptr.results && ptr.results.length) {
    perTestMeta.textContent =
      `${ptr.pass_count} pass / ${ptr.fail_count} fail · avg score ${ptr.avg_score?.toFixed(3) ?? "—"} · threshold ${ptr.threshold}`;
    perTestList.innerHTML = ptr.results.map((r) => `
      <div class="per-test-row ${r.passed ? "pass" : "fail"}">
        <div class="per-test-score">${r.score?.toFixed ? r.score.toFixed(2) : "—"}</div>
        <div class="per-test-body">
          <strong class="per-test-title">${escapeHtml(r.title)}</strong>
          ${r.reason ? `<p class="per-test-reason">${escapeHtml(r.reason)}</p>` : ""}
        </div>
        <div class="per-test-badge ${r.passed ? "pass" : "fail"}">${r.passed ? "PASS" : "REVIEW"}</div>
      </div>
    `).join("");
    perTestSection.hidden = false;
  } else {
    perTestSection.hidden = true;
  }

  // ── AC Coverage ────────────────────────────────────────────────────────────
  const phantomCallout = document.getElementById("phantomCallout");
  if (acCov && acCov.items && acCov.items.length) {
    const ratio = acCov.coverage_ratio != null
      ? `${(acCov.coverage_ratio * 100).toFixed(0)}%`
      : "—";
    const phantomCount = acCov.phantom_tags?.length || 0;
    const verdictLabel = acCov.verdict?.toUpperCase() ?? "";
    acMeta.textContent = `${acCov.covered_ac}/${acCov.total_ac} AC covered (${ratio}) · ${verdictLabel}`;
    acMeta.className = `eval-section-meta verdict-${(acCov.verdict || "pass").toLowerCase()}`;

    // Real AC rows only — one row per story AC
    acList.innerHTML = acCov.items.map((item) => `
      <div class="ac-row ${item.covered ? "pass" : "fail"}">
        <span class="ac-label">${escapeHtml(item.ac_label)}</span>
        <span class="ac-status">${item.covered ? "✅ Covered" : "❌ Not covered"}</span>
        <span class="ac-text">${escapeHtml(item.ac_text)}</span>
        ${item.covering_tests && item.covering_tests.length
        ? `<ul class="ac-covering-tests">${item.covering_tests.map((t) => `<li>${escapeHtml(t)}</li>`).join("")}</ul>`
        : ""}
      </div>
    `).join("");

    // Phantom tags — shown as a separate callout below the AC table, not mixed in
    if (phantomCallout) {
      if (phantomCount) {
        const tagList = acCov.phantom_tags.map((p) =>
          `<li><span class="phantom-tag-label">${escapeHtml(p.tag)}</span> — ${escapeHtml(p.test)}</li>`
        ).join("");
        phantomCallout.innerHTML = `
          <span class="phantom-callout-icon">⚠</span>
          <div class="phantom-callout-body">
            <strong>${phantomCount} test${phantomCount > 1 ? "s reference" : " references"} an AC tag not found in this story.</strong>
            <p class="phantom-callout-hint">The model invented these tags. Tests are still valid but should be re-tagged to AC-1 or a real acceptance criterion.</p>
            <ul class="phantom-tag-list">${tagList}</ul>
          </div>`;
        phantomCallout.hidden = false;
      } else {
        phantomCallout.hidden = true;
      }
    }

    acSection.hidden = false;
  } else if (acCov.summary) {
    acMeta.textContent = acCov.summary;
    acMeta.className = "eval-section-meta";
    acList.innerHTML = `<p class="empty-state">${escapeHtml(acCov.summary)}</p>`;
    acSection.hidden = false;
  } else {
    acSection.hidden = true;
  }

  // ── Hallucination ──────────────────────────────────────────────────────────
  if (halluc && typeof halluc.flagged_count === "number") {
    hallucMeta.textContent = halluc.summary || `${halluc.flagged_count} test(s) flagged`;
    hallucMeta.className = `eval-section-meta ${halluc.flagged_count > 0 ? "verdict-warn" : "verdict-pass"}`;
    if (halluc.flags && halluc.flags.length) {
      hallucList.innerHTML = halluc.flags.map((flag) => `
        <div class="halluc-row">
          <strong>${escapeHtml(flag.title)}</strong>
          <p>${escapeHtml(flag.reason)}</p>
          <ul class="halluc-values">${(flag.hallucinated_values || []).map((v) => `<li><code>${escapeHtml(v)}</code></li>`).join("")}</ul>
        </div>
      `).join("");
    } else {
      hallucList.innerHTML = `<p class="empty-state">No hallucinated specific values detected.</p>`;
    }
    hallucSection.hidden = false;
  } else {
    hallucSection.hidden = true;
  }

  // ── Context preview (retrieval) ────────────────────────────────────────────
  if (deep) {
    const retrievalPreview = (deep.retrieval_context || []).slice(0, 5)
      .map((entry) => `<li>${escapeHtml(entry)}</li>`)
      .join("");

    context.innerHTML = `
      <div class="context-preview-grid">
        <details class="context-preview">
          <summary><span>Story input sent to DeepEval</span></summary>
          <pre>${escapeHtml(deep.story_input || "")}</pre>
        </details>
        <details class="context-preview">
          <summary><span>Generated output evaluated</span></summary>
          <pre>${escapeHtml(deep.actual_output || "")}</pre>
        </details>
      </div>
      ${(deep.retrieval_context || []).length ? `
        <details class="context-preview">
          <summary><span>Retrieval context (${(deep.retrieval_context || []).length} items)</span></summary>
          <ul>${retrievalPreview}</ul>
        </details>
      ` : ""}
    `;
  } else {
    context.innerHTML = "";
  }
}

function renderHistory() {
  const container = document.getElementById("historyList");
  const totalLabel = document.getElementById("historyTotalLabel");
  const totalCount = state.history.length;
  if (totalLabel) {
    totalLabel.textContent = `${totalCount} total pushed`;
  }

  if (!state.history.length) {
    container.innerHTML = `<div class="empty-state">No pushed test cases yet.</div>`;
    return;
  }

  container.innerHTML = state.history
    .map((entry) => `
      <div class="history-row">
        <a class="jira-chip" href="${escapeHtml(entry.url || "#")}" target="_blank" rel="noopener noreferrer">${escapeHtml(entry.jira_key)}</a>
        <div class="history-copy">
          <strong class="history-title">${escapeHtml(entry.test_title)}</strong>
          <p class="history-meta-line">${escapeHtml(entry.issue_key || "Unknown story")} · ${escapeHtml(entry.timestamp?.slice(0, 16).replace("T", " ") || "")}</p>
        </div>
      </div>
    `)
    .join("");
}

function renderConsole() {
  const output = document.getElementById("consoleOutput");
  const visibleLogs = state.logs.filter((entry) => entry.level?.toLowerCase() !== "trace");

  if (!visibleLogs.length) {
    output.textContent = "No logs recorded yet.";
    return;
  }

  output.textContent = visibleLogs
    .map((entry) => `[${entry.timestamp}] ${entry.level.toUpperCase()} ${entry.message}`)
    .join("\n");
}

function renderWorkspaceTabs() {
  document.querySelectorAll("[data-workspace-tab]").forEach((button) => {
    button.classList.toggle("active", button.dataset.workspaceTab === state.activeWorkspaceTab);
  });

  document.querySelectorAll(".workspace-tab").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `${state.activeWorkspaceTab}Tab`);
  });
}

async function runPushAction() {
  if (!state.selectedTests.length) {
    showBanner("Select at least one generated test to push.", "warning");
    return;
  }

  setBusy(true, "push", "Pushing...");
  try {
    const result = await runAction("/api/actions/push", {
      issue_key: state.selectedKey,
      mode: state.activeSuite,
      indices: state.selectedTests,
    }, { timeoutMs: 120000 });
    state.selectedTests = [];
    renderSuite();
    if ((result.payload?.created || []).length) {
      openPushSuccessModal({
        issueKey: state.selectedKey,
        storyUrl: result.payload?.story_url || "",
        createdCount: result.payload.created.length,
      });
    }
  } catch (error) {
    showBanner(error.message, "warning");
  } finally {
    setBusy(false);
  }
}

function bindEvents() {
  document.getElementById("loadIssueButton").addEventListener("click", async () => {
    const issueKey = document.getElementById("issueKeyInput").value.trim().toUpperCase();
    if (!issueKey) {
      showBanner("Enter a story key first.", "warning");
      return;
    }
    await runLoadFlow(issueKey);
  });

  document.getElementById("refreshLogsButton").addEventListener("click", async () => {
    try {
      await refreshLogs();
      showBanner("Console refreshed.", "neutral");
    } finally {
    }
  });

  document.getElementById("refreshHistoryButton").addEventListener("click", async () => {
    setBusy(true, "history", "Refreshing...");
    try {
      await refreshHistory();
      showBanner("History refreshed.", "neutral");
    } finally {
      setBusy(false);
    }
  });

  document.getElementById("pushSelectedButton").addEventListener("click", runPushAction);
  document.getElementById("closePushSuccessModal").addEventListener("click", closePushSuccessModal);
  document.querySelectorAll("[data-modal-close='push-success']").forEach((node) => {
    node.addEventListener("click", closePushSuccessModal);
  });
  document.getElementById("openStoryInJiraLink").addEventListener("click", () => {
    closePushSuccessModal();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closePushSuccessModal();
    }
  });

  document.getElementById("testSelectAll").addEventListener("change", (event) => {
    const suite = state.issue?.suites?.[state.activeSuite];
    if (!suite) {
      return;
    }
    const visibleIndices = getVisibleGeneratedTests(state.issue, suite).map(({ index }) => index);
    state.selectedTests = event.target.checked ? visibleIndices : [];
    renderSuite();
  });

  document.querySelectorAll("[data-tests-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      state.activeTestsTab = button.dataset.testsTab;
      renderSuite();
    });
  });

  document.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      const issueKey = document.getElementById("issueKeyInput").value.trim().toUpperCase();
      if (!issueKey) {
        showBanner("Enter a story key first.", "warning");
        return;
      }

      const action = button.dataset.action;

      // Smart generate: if suite already has tests, delegate to load-next (which has its own busy state)
      if (action === "generate") {
        const existingSuite = state.issue?.suites?.[state.activeSuite];
        const hasTests = existingSuite?.tests?.length > 0;
        if (hasTests) {
          await runLoadNextAction();
          return;
        }
      }

      const actionLabelMap = {
        "collect-context": "Refreshing context...",
        "generate": "Generating...",
        "evaluate-current": "Evaluating...",
      };
      setBusy(true, action, actionLabelMap[action] || "Working...");
      try {
        if (action === "collect-context") {
          startBannerSteps([`Collecting context for ${issueKey} (linked defects, related stories, history)`]);
          setBannerStep(0, "active");
          await runAction("/api/actions/collect-context", { issue_key: issueKey }, { timeoutMs: 90000 });
          setBannerStep(0, "done", "Context collected & indexed", "success");
          _renderBannerSteps("success");
          return;
        }
        if (action === "generate") {
          const needsContext = !state.issue?.context;
          const steps = needsContext
            ? [
              `Collecting context for ${issueKey}`,
              "Generating test cases with context (thinking model)",
            ]
            : ["Generating test cases with context (thinking model)"];
          startBannerSteps(steps);
          let genIdx = 0;

          if (needsContext) {
            setBannerStep(0, "active");
            await runAction("/api/actions/collect-context", { issue_key: issueKey }, { timeoutMs: 90000 });
            setBannerStep(0, "done");
            genIdx = 1;
          }

          state.activeSuite = "enriched";
          state.activeWorkspaceTab = "tests";
          state.activeTestsTab = "generated";
          renderWorkspaceTabs();

          setBannerStep(genIdx, "active");
          await runAction("/api/actions/generate", { issue_key: issueKey, mode: state.activeSuite, max_tests: 5, offset: 0 }, { timeoutMs: 150000 });
          setBannerStep(genIdx, "done", "Test cases generated", "success");
          _renderBannerSteps("success");
          return;
        }
        if (action === "evaluate-current") {
          startBannerSteps([
            "Running answer relevancy (per-test scores)",
            "Running faithfulness & context relevance",
            "Analysing AC coverage",
            "Scanning for hallucinations",
          ]);
          // All checks run server-side in one call — show them all as active while waiting
          [0, 1, 2, 3].forEach((i) => setBannerStep(i, "active"));
          state.activeWorkspaceTab = "evaluate";
          renderWorkspaceTabs();
          const evalResult = await runAction("/api/actions/evaluate", { issue_key: issueKey, mode: state.activeSuite }, { timeoutMs: 150000 });
          const ev = evalResult?.payload?.evaluation;

          // Step 0: per-test answer relevancy
          const perTest = ev?.deepeval?.per_test_relevancy;
          const relevancyOk = perTest ? perTest.fail_count === 0 : true;
          setBannerStep(0, relevancyOk ? "done" : "info");

          // Step 1: faithfulness & context relevance (deepeval.metrics array)
          const deepMetrics = ev?.deepeval?.metrics || [];
          const faithFail = deepMetrics.some((m) => !m.passed && !m.skipped);
          setBannerStep(1, faithFail ? "info" : "done");

          // Step 2: AC coverage
          const acVerdict = ev?.ac_coverage?.verdict;
          setBannerStep(2, acVerdict === "FAIL" ? "info" : "done");

          // Step 3: hallucination
          const hallucFlags = ev?.hallucination?.flags?.length || 0;
          setBannerStep(3, hallucFlags > 0 ? "info" : "done");

          const overallOk = ev?.passed !== false;
          _renderBannerSteps(overallOk ? "success" : "warning");
        }
      } catch (error) {
        const detail = error.message.includes("timed out")
          ? `${error.message} Open Console to inspect whether the backend is still running the action.`
          : error.message;
        showBanner(detail, "warning");
        await refreshLogs();
      } finally {
        setBusy(false);
      }
    });
  });

  document.querySelectorAll("[data-workspace-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      state.activeWorkspaceTab = button.dataset.workspaceTab;
      renderWorkspaceTabs();
    });
  });
}

async function boot() {
  bindEvents();
  renderWorkspaceTabs();
  try {
    await loadWorkspace();
    await refreshLogs();
    renderIssue();
    renderEvaluation();
    renderBusyState();
    showBanner("Workflow ready.", "success");
    window.setInterval(refreshLogs, 4000);
  } catch (error) {
    showBanner(error.message, "warning");
  }
}

boot();
