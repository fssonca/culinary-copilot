/* State and wiring: sessions, streaming, composer, toggle. */

import {
  DIETS,
  createSession,
  getSession,
  isStaleRevision,
  postAnswer,
  postSelect,
  setPermission,
  shortId,
  streamAgent,
} from "./api.js";
import { stageLines } from "./outcomes.js";
import { el, renderFinal } from "./render.js";

const STORAGE_KEY = "culinary-copilot.session-id";
const TOGGLE_OFF_HELP = "answers use only the built-in recipe and technique library";
const TOGGLE_ON_HELP =
  "the assistant may run a limited number of web searches in this session; queries are minimized before sending";

const state = {
  sessionId: null,
  revision: null,
  phase: null,
  stepsRemaining: null,
  toolsRemaining: null,
  pendingQuestion: null,
  streaming: false,
};

const refs = {};

function readStoredSessionId() {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function writeStoredSessionId(id) {
  try {
    if (id) {
      window.localStorage.setItem(STORAGE_KEY, id);
    } else {
      window.localStorage.removeItem(STORAGE_KEY);
    }
  } catch {
    /* Storage is best-effort; the page works without it. */
  }
}

function setFormError(text) {
  refs.formError.textContent = text || "";
}

function setPhase(phase) {
  state.phase = phase;
  refs.phaseChip.textContent = "phase: " + (phase || "—");
}

function setBudgets(steps, tools) {
  if (steps !== undefined && steps !== null) {
    state.stepsRemaining = steps;
  }
  if (tools !== undefined && tools !== null) {
    state.toolsRemaining = tools;
  }
  const stepsText = state.stepsRemaining === null ? "—" : String(state.stepsRemaining);
  const toolsText = state.toolsRemaining === null ? "—" : String(state.toolsRemaining);
  refs.budgets.textContent = "steps " + stepsText + " · tool calls " + toolsText;
}

function setToggle(allowed) {
  refs.toggle.checked = Boolean(allowed);
  refs.toggleHelp.textContent = allowed ? TOGGLE_ON_HELP : TOGGLE_OFF_HELP;
}

function setSessionLabel() {
  refs.sessionLabel.textContent = "session " + (state.sessionId ? shortId(state.sessionId) : "—");
}

function setBusy(busy) {
  state.streaming = busy;
  refs.sendButton.disabled = busy;
  refs.composerInput.disabled = busy;
  refs.toggle.disabled = busy;
  refs.dietSelect.disabled = busy;
  for (const button of refs.answerOptions.querySelectorAll("button")) {
    button.disabled = busy;
  }
}

function applySession(body) {
  state.sessionId = body.id;
  state.revision = body.revision;
  setPhase(body.current_phase);
  setBudgets(body.steps_remaining, body.tool_calls_remaining);
  setToggle(body.internet_search_allowed);
  setDiet(body.constraints);
  setSessionLabel();
  const open = Array.isArray(body.unresolved_questions) ? body.unresolved_questions : [];
  state.pendingQuestion = open.length > 0 ? open[open.length - 1] : null;
  if (state.pendingQuestion) {
    enterAnswerMode(state.pendingQuestion);
  } else {
    exitAnswerMode();
  }
}

function setDiet(constraints) {
  const raw = constraints && constraints.dietary_constraints;
  const values = Array.isArray(raw) ? raw.map(String) : typeof raw === "string" ? [raw] : [];
  const diet = values.find((value) => DIETS.includes(value));
  refs.dietSelect.value = diet || "";
}

function notice(text) {
  const node = el("p", "notice", text);
  refs.transcript.appendChild(node);
  return node;
}

function enterAnswerMode(question) {
  state.pendingQuestion = question;
  refs.answerBox.hidden = false;
  refs.answerQuestion.textContent = String(question.question_text || "");
  refs.answerOptions.replaceChildren();
  const options = Array.isArray(question.options) ? question.options : [];
  for (const option of options) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary";
    button.textContent = String(option);
    button.disabled = state.streaming;
    button.addEventListener("click", () => answerQuestion(String(option)));
    refs.answerOptions.appendChild(button);
  }
  refs.composerLabel.textContent = "Your answer (or type below)";
  refs.composerInput.placeholder = "Type your answer, or pick an option above";
}

function exitAnswerMode() {
  state.pendingQuestion = null;
  refs.answerBox.hidden = true;
  refs.answerQuestion.textContent = "";
  refs.answerOptions.replaceChildren();
  refs.composerLabel.textContent = "Message";
  refs.composerInput.placeholder = "Ask for a meal, a plan, or a technique";
}

function appendActivityLines(list, lines) {
  for (const line of lines) {
    const item = document.createElement("li");
    item.textContent = line;
    list.appendChild(item);
  }
}

async function refetchSession() {
  const body = await getSession(state.sessionId);
  applySession(body);
  return body;
}

async function handleStale(error) {
  try {
    await refetchSession();
  } catch {
    /* Keep the stale state; the message below still guides the user. */
  }
  setFormError(
    "The session changed while the request was running (" +
      (error.message || "stale revision") +
      "). The latest state was reloaded; try again."
  );
}

function renderResult(container, result, extraActions) {
  const actions = {
    onSelect: (datasetId, sourceId) => chooseOption(datasetId, sourceId),
    onRetry: () => runStream(null),
    onReload: async () => {
      try {
        await refetchSession();
        setFormError("");
      } catch (error) {
        setFormError(error.message || "reload failed");
      }
    },
    onNewSession: () => {
      void createNewSession();
    },
    onQuestionShown: (question) => enterAnswerMode(question),
    ...(extraActions || {}),
  };
  container.replaceChildren();
  container.appendChild(renderFinal(result, actions));
}

async function runStream(message) {
  if (!state.sessionId || state.streaming) {
    return;
  }
  setBusy(true);
  setFormError("");
  const turn = document.createElement("div");
  turn.className = "turn";
  if (message) {
    const userNode = el("div", "user-message", message);
    turn.appendChild(userNode);
  }
  const details = document.createElement("details");
  details.className = "activity";
  details.open = true;
  const summary = document.createElement("summary");
  summary.textContent = "Activity";
  details.appendChild(summary);
  const log = document.createElement("ul");
  log.className = "activity-log";
  log.setAttribute("aria-live", "polite");
  details.appendChild(log);
  turn.appendChild(details);
  const resultBox = document.createElement("div");
  turn.appendChild(resultBox);
  refs.transcript.appendChild(turn);
  // The new turn can start below the fold (e.g. after "Choose this").
  turn.scrollIntoView({ behavior: "smooth", block: "nearest" });

  try {
    const final = await streamAgent(
      state.sessionId,
      { message: message || undefined, expectedRevision: state.revision },
      {
        onStage: (stage, detail) => {
          if (stage === "tool_step") {
            if (detail.steps_remaining !== undefined) {
              state.stepsRemaining = detail.steps_remaining;
            }
            if (detail.tool_calls_remaining !== undefined) {
              state.toolsRemaining = detail.tool_calls_remaining;
            }
            setBudgets(state.stepsRemaining, state.toolsRemaining);
          }
          appendActivityLines(log, stageLines(stage, detail));
        },
      }
    );
    state.revision = final.revision;
    setPhase(final.phase);
    details.open = false;
    if (final.stop_reason === "agent_needs_user_input" && final.result && final.result.question) {
      state.pendingQuestion = final.result.question;
      enterAnswerMode(state.pendingQuestion);
    } else if (final.result && final.result.question) {
      state.pendingQuestion = final.result.question;
      enterAnswerMode(state.pendingQuestion);
    } else if (final.stop_reason === "agent_sufficient_evidence") {
      exitAnswerMode();
    }
    if (final.result) {
      renderResult(resultBox, final.result);
    } else {
      renderResult(resultBox, {
        reason: final.stop_reason,
        stop_reason: final.stop_reason,
        message: "",
        next_action: null,
      });
    }
    try {
      await refetchSession();
    } catch {
      /* Budgets already updated from the stream; a failed refetch is non-fatal. */
    }
  } catch (error) {
    details.open = false;
    /* Refresh revision, phase, budgets and the pending question after
     * any stream error (best effort), so the next action does not hit
     * a spurious 409. This never retries the run itself. */
    try {
      await refetchSession();
    } catch {
      /* The error card below still guides the user. */
    }
    if (isStaleRevision(error)) {
      await handleStale(error);
      renderResult(resultBox, {
        reason: "stale_revision",
        message: error.message,
        next_action: "refetch_and_retry",
      });
    } else {
      renderResult(resultBox, {
        reason: error.reason || "internal_error",
        message: error.message,
        next_action: error.nextAction || null,
        status: error.status,
      });
    }
  } finally {
    setBusy(false);
  }
}

async function answerQuestion(answer) {
  if (!state.sessionId || state.streaming || !state.pendingQuestion) {
    return;
  }
  setBusy(true);
  setFormError("");
  try {
    const updated = await postAnswer(state.sessionId, {
      revision: state.revision,
      questionId: state.pendingQuestion.question_id,
      answer,
    });
    state.revision = updated.revision;
    exitAnswerMode();
    refs.composerInput.value = "";
  } catch (error) {
    if (isStaleRevision(error)) {
      await handleStale(error);
    } else {
      setFormError(error.message || "answer failed");
    }
    setBusy(false);
    return;
  }
  setBusy(false);
  await runStream(null);
}

async function chooseOption(datasetId, sourceId) {
  if (!state.sessionId || state.streaming) {
    return false;
  }
  setBusy(true);
  setFormError("");
  try {
    const updated = await postSelect(state.sessionId, {
      revision: state.revision,
      datasetId,
      sourceId,
    });
    state.revision = updated.revision;
    if (updated.current_phase) {
      setPhase(updated.current_phase);
    }
  } catch (error) {
    if (isStaleRevision(error)) {
      await handleStale(error);
    } else {
      setFormError(error.message || "select failed");
    }
    setBusy(false);
    return false;
  }
  setBusy(false);
  await runStream(null);
  return true;
}

async function sendComposer() {
  if (state.pendingQuestion) {
    const text = refs.composerInput.value.trim();
    if (!text) {
      setFormError("Type your answer or pick an option.");
      return;
    }
    await answerQuestion(text);
    return;
  }
  const text = refs.composerInput.value.trim();
  if (!text) {
    setFormError("Type a message first.");
    return;
  }
  refs.composerInput.value = "";
  await runStream(text);
}

async function createNewSession() {
  setFormError("");
  setBusy(true);
  try {
    const body = await createSession(refs.dietSelect.value);
    writeStoredSessionId(body.id);
    refs.transcript.replaceChildren();
    applySession(body);
  } catch (error) {
    setFormError(error.message || "could not create a session");
  } finally {
    setBusy(false);
  }
}

async function restoreOrCreate() {
  const stored = readStoredSessionId();
  let replacedSpent = false;
  if (stored) {
    try {
      const body = await getSession(stored);
      const spent = Number(body.steps_remaining) <= 0 || Number(body.tool_calls_remaining) <= 0;
      if (!spent) {
        applySession(body);
        notice("Earlier answers in this session are not shown after reload");
        return;
      }
      // A restored session with no budget left cannot run: start a new
      // one (it gets the server's current budgets) and say so.
      replacedSpent = true;
    } catch (error) {
      if (!(error && (error.reason === "unknown_session" || error.status === 404))) {
        setFormError(error.message || "could not load the session");
        return;
      }
    }
  }
  try {
    const body = await createSession(refs.dietSelect.value);
    writeStoredSessionId(body.id);
    applySession(body);
    if (replacedSpent) {
      notice("The previous session had used its budget, so a new session was started");
    }
  } catch (error) {
    setFormError(error.message || "could not create a session");
  }
}

async function onToggleChange() {
  if (!state.sessionId || state.streaming) {
    return;
  }
  const next = refs.toggle.checked;
  refs.toggle.disabled = true;
  setFormError("");
  try {
    const body = await setPermission(state.sessionId, state.revision, next);
    applySession(body);
  } catch (error) {
    setToggle(!next);
    if (isStaleRevision(error)) {
      await handleStale(error);
    } else {
      setFormError(error.message || "permission update failed");
    }
  } finally {
    refs.toggle.disabled = state.streaming;
  }
}

export function init() {
  refs.phaseChip = document.getElementById("phase-chip");
  refs.budgets = document.getElementById("budgets");
  refs.sessionLabel = document.getElementById("session-label");
  refs.newSession = document.getElementById("new-session");
  refs.toggle = document.getElementById("internet-toggle");
  refs.toggleHelp = document.getElementById("toggle-help");
  refs.dietSelect = document.getElementById("diet-select");
  refs.transcript = document.getElementById("transcript");
  refs.answerBox = document.getElementById("answer-box");
  refs.answerQuestion = document.getElementById("answer-question");
  refs.answerOptions = document.getElementById("answer-options");
  refs.composerInput = document.getElementById("composer-input");
  refs.composerLabel = document.getElementById("composer-label");
  refs.sendButton = document.getElementById("send-button");
  refs.formError = document.getElementById("form-error");

  refs.sendButton.addEventListener("click", () => {
    void sendComposer();
  });
  refs.composerInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void sendComposer();
    }
  });
  refs.newSession.addEventListener("click", () => {
    void createNewSession();
  });
  refs.toggle.addEventListener("change", () => {
    void onToggleChange();
  });
  refs.dietSelect.addEventListener("change", () => {
    if (!state.streaming) {
      void createNewSession();
    }
  });
  setToggle(false);
  setBudgets(null, null);
  void restoreOrCreate();
}

init();
