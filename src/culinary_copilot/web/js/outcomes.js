/* OUTCOMES and STAGES tables for the minimal UI. */

export const OUTCOMES = {
  agent_max_steps: {
    title: "Step limit reached",
    body: "The assistant reached this session's step limit without finishing. Start a new session with a narrower request.",
    action: "new_session",
  },
  agent_tool_budget_exhausted: {
    title: "Tool budget exhausted",
    body: "The assistant used this session's tool-call budget. Start a new session to continue.",
    action: "new_session",
  },
  agent_token_budget_exhausted: {
    title: "Token budget exhausted",
    body: "The assistant reached this session's reading budget. Start a new session with a narrower request.",
    action: "new_session",
  },
  agent_wall_clock_exceeded: {
    title: "Run timed out",
    body: "The run hit its time limit. The work so far is kept in the session; try again.",
    action: "retry",
  },
  agent_no_progress: {
    title: "No progress",
    body: "The assistant repeated failing calls without new information and stopped. Rephrase your request.",
    action: "rephrase",
  },
  agent_validation_failed: {
    title: "Answer failed checks",
    body: "The assistant could not produce an answer that passes the source checks. Rephrase your request.",
    action: "rephrase",
  },
  tool_timeout: {
    title: "A tool timed out",
    body: "One lookup took too long and was skipped. Try again.",
    action: "retry",
  },
  tool_permission_denied: {
    title: "Web search is off",
    body: "web search is off for this session. Turn on internet search to let the assistant search the web.",
    action: "rephrase",
  },
  search_budget_exhausted: {
    title: "Web search limit reached",
    body: "This session has used all of its web searches. Start a new session for more searching, or keep going here without web search.",
    action: "new_session",
  },
  search_not_performed: {
    title: "Search did not run",
    body: "The web search was not performed. Try again.",
    action: "retry",
  },
  search_unverified: {
    title: "Search not verified",
    body: "The web search returned no source the assistant can cite. Rephrase your request.",
    action: "rephrase",
  },
  unknown_question: {
    title: "Question already answered",
    body: "That question is no longer open in this session. Continue with your next message.",
    action: "rephrase",
  },
  unknown_option: {
    title: "Option not offered",
    body: "That dish was not among the offered options. Pick one of the cards shown.",
    action: "rephrase",
  },
  stale_revision: {
    title: "Session changed",
    body: "The session changed while the request was running. The page reloaded the latest state; try again.",
    action: "reload",
  },
  unknown_session: {
    title: "Session not found",
    body: "This session no longer exists on the server. Start a new session.",
    action: "reload",
  },
  session_unavailable: {
    title: "Session store unavailable",
    body: "The server could not reach saved sessions. Try again in a moment.",
    action: "retry",
  },
  internal_error: {
    title: "Something went wrong",
    body: "The server hit an internal error. Contact the operator if it keeps happening.",
    action: "operator",
  },
};

/* Fallback when a reason has no OUTCOMES entry: derive the action from
 * next_action so the page never renders a dead end. */
export function actionForNextAction(nextAction) {
  if (nextAction === "retry") {
    return "retry";
  }
  if (nextAction === "change_request") {
    return "rephrase";
  }
  if (nextAction === "refetch_and_retry") {
    return "reload";
  }
  return "operator";
}

export function outcomeFor({ reason, nextAction, message }) {
  const key = String(reason || "internal_error");
  const entry = OUTCOMES[key];
  if (entry) {
    return { reason: key, title: entry.title, body: entry.body, action: entry.action, message: message || "" };
  }
  const action = actionForNextAction(nextAction);
  if (action === "retry") {
    return { reason: key, title: "Try again", body: message || "The request failed. Try again.", action, message: message || "" };
  }
  if (action === "rephrase") {
    return { reason: key, title: "Rephrase your request", body: message || "The same request fails the same way. Rephrase your request.", action, message: message || "" };
  }
  if (action === "reload") {
    return { reason: key, title: "Reload the session", body: message || "The session changed. Reload and try again.", action, message: message || "" };
  }
  return { reason: key, title: "Something went wrong", body: message || "Contact the operator if it keeps happening.", action: "operator", message: message || "" };
}

/* Friendly one-line reason for a single tool call in the activity log.
 * Search reasons reuse the OUTCOMES wording. */
export function toolCallLabel(tool, ok, reason) {
  const name = String(tool || "tool");
  if (ok) {
    return name + " ✓";
  }
  const key = String(reason || "unknown");
  if (key === "tool_permission_denied") {
    return name + " ✗ web search is off for this session";
  }
  const entry = OUTCOMES[key];
  if (entry) {
    return name + " ✗ " + entry.body;
  }
  return name + " ✗ " + key;
}

export const STAGES = {
  run_started(detail) {
    const phase = detail && detail.phase ? String(detail.phase) : "unknown";
    return ["run started (" + phase + ")"];
  },
  tool_step(detail) {
    const calls = detail && Array.isArray(detail.calls) ? detail.calls : [];
    return calls.map((call) => toolCallLabel(call.tool, call.ok, call.reason));
  },
  tool_excluded(detail) {
    const tool = detail && detail.tool ? String(detail.tool) : "a tool";
    return [tool + " excluded for this session"];
  },
  validation_reject(detail) {
    const errors = detail && Array.isArray(detail.errors) ? detail.errors : [];
    if (errors.length === 0) {
      return ["answer failed checks"];
    }
    return ["answer failed checks: " + errors.map((item) => String(item)).join("; ")];
  },
  provider_error(detail) {
    const reason = detail && detail.reason ? String(detail.reason) : "provider error";
    const entry = OUTCOMES[reason];
    if (entry) {
      return ["provider error: " + entry.body];
    }
    return ["provider error: " + reason];
  },
  question_asked(detail) {
    const id = detail && detail.question_id ? String(detail.question_id) : "";
    return ["question asked" + (id ? " (" + id + ")" : "")];
  },
  finished(detail) {
    const reason = detail && detail.stop_reason ? String(detail.stop_reason) : "finished";
    return ["finished (" + reason + ")"];
  },
};

/* Lines for one stage event. Unknown stages show their name
 * generically; recipe text and reasoning never travel in stage
 * details, so nothing is filtered here beyond naming. */
export function stageLines(stage, detail) {
  const render = STAGES[String(stage)];
  if (render) {
    try {
      return render(detail || {});
    } catch {
      return [String(stage)];
    }
  }
  return [String(stage)];
}
