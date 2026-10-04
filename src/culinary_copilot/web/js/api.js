/* API client: fetch wrappers, SSE parser, revision helpers. */

export class ApiError extends Error {
  constructor({ status, reason, message, nextAction, body }) {
    super(message || reason || "request failed");
    this.name = "ApiError";
    this.status = status ?? null;
    this.reason = reason ?? "unknown";
    this.nextAction = nextAction ?? null;
    this.body = body ?? null;
  }
}

function detailOf(body, fallbackMessage) {
  const detail = body && typeof body === "object" ? body.detail : null;
  if (detail && typeof detail === "object") {
    return {
      reason: detail.reason || body.reason || "unknown",
      message: detail.message || body.message || fallbackMessage,
      nextAction: detail.next_action || body.next_action || null,
    };
  }
  return {
    reason: (body && body.reason) || "unknown",
    message: (body && body.message) || fallbackMessage,
    nextAction: (body && (body.next_action || body.nextAction)) || null,
  };
}

export async function requestJson(path, { method, body } = {}) {
  const response = await fetch(path, {
    method: method || "GET",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let parsed = null;
  try {
    parsed = await response.json();
  } catch {
    parsed = null;
  }
  if (!response.ok) {
    const info = detailOf(parsed, "request failed " + response.status);
    throw new ApiError({
      status: response.status,
      reason: info.reason,
      message: info.message,
      nextAction: info.nextAction,
      body: parsed,
    });
  }
  return parsed;
}

export function createSession() {
  return requestJson("/api/v1/sessions", {
    method: "POST",
    body: { internet_search_allowed: false },
  });
}

export function getSession(sessionId) {
  return requestJson("/api/v1/sessions/" + encodeURIComponent(sessionId));
}

export function setPermission(sessionId, revision, allowed) {
  return requestJson("/api/v1/sessions/" + encodeURIComponent(sessionId) + "/permission", {
    method: "POST",
    body: { revision, allowed },
  });
}

export function postAnswer(sessionId, { revision, questionId, answer }) {
  return requestJson("/api/v1/sessions/" + encodeURIComponent(sessionId) + "/answers", {
    method: "POST",
    body: { revision, question_id: questionId, answer },
  });
}

export function postSelect(sessionId, { revision, datasetId, sourceId }) {
  return requestJson("/api/v1/sessions/" + encodeURIComponent(sessionId) + "/select", {
    method: "POST",
    body: { revision, dataset_id: datasetId, source_id: sourceId },
  });
}

/* Parse one SSE block (split on blank lines by the caller) into
 * { event, data } using the event: and data: lines. */
export function parseSseBlock(block) {
  let event = "";
  const lines = [];
  for (const raw of block.split("\n")) {
    const line = raw.endsWith("\r") ? raw.slice(0, -1) : raw;
    if (line.startsWith("event:")) {
      event = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      lines.push(line.slice("data:".length).trimStart());
    }
  }
  if (!event || lines.length === 0) {
    return null;
  }
  let data = null;
  try {
    data = JSON.parse(lines.join("\n"));
  } catch {
    return null;
  }
  return { event, data };
}

/* POST the agent stream and dispatch stage/final/error events.
 * Returns the final payload. Throws ApiError (or an SSE error object
 * converted to ApiError) for error events and HTTP failures. */
export async function streamAgent(sessionId, { message, expectedRevision } = {}, { onStage } = {}) {
  const body = {};
  if (message !== undefined && message !== null && String(message).length > 0) {
    body.message = message;
  }
  if (expectedRevision !== undefined && expectedRevision !== null) {
    body.expected_revision = expectedRevision;
  }
  const response = await fetch(
    "/api/v1/sessions/" + encodeURIComponent(sessionId) + "/agent/stream",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }
  );
  if (!response.ok) {
    let parsed = null;
    try {
      parsed = await response.json();
    } catch {
      parsed = null;
    }
    const info = detailOf(parsed, "stream failed " + response.status);
    throw new ApiError({
      status: response.status,
      reason: info.reason,
      message: info.message,
      nextAction: info.nextAction,
      body: parsed,
    });
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split("\n\n");
    buffer = blocks.pop();
    for (const block of blocks) {
      const parsed = parseSseBlock(block);
      if (!parsed) {
        continue;
      }
      if (parsed.event === "stage") {
        if (onStage) {
          onStage(parsed.data.stage, parsed.data.detail || {});
        }
      } else if (parsed.event === "final") {
        return parsed.data;
      } else if (parsed.event === "error") {
        throw new ApiError({
          status: parsed.data.status ?? null,
          reason: parsed.data.reason || "unknown",
          message: parsed.data.message || "stream error",
          nextAction: parsed.data.next_action || null,
          body: parsed.data,
        });
      }
    }
  }
  buffer += decoder.decode();
  const tail = parseSseBlock(buffer);
  if (tail) {
    if (tail.event === "final") {
      return tail.data;
    }
    if (tail.event === "error") {
      throw new ApiError({
        status: tail.data.status ?? null,
        reason: tail.data.reason || "unknown",
        message: tail.data.message || "stream error",
        nextAction: tail.data.next_action || null,
        body: tail.data,
      });
    }
  }
  throw new ApiError({ status: null, reason: "internal_error", message: "stream ended without a final event", nextAction: "retry" });
}

export function isStaleRevision(error) {
  return Boolean(error && error.reason === "stale_revision");
}

export function shortId(sessionId) {
  const text = String(sessionId || "");
  return text.length <= 12 ? text : text.slice(0, 8);
}
