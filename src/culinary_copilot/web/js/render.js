/* Card renderers: pure functions (data, actions) -> HTMLElement.
 * Model and session text uses textContent only. Links go through
 * safeLink. Multi-line text uses the prewrap class. */

import { outcomeFor } from "./outcomes.js";

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined && text !== null) {
    node.textContent = String(text);
  }
  return node;
}

/* Anchor only for http: or https: URLs parsed with new URL();
 * anything else renders as plain text. */
export function safeLink(url, label) {
  const text = label === undefined || label === null ? url : label;
  let parsed = null;
  try {
    parsed = new URL(String(url));
  } catch {
    return el("span", null, text);
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return el("span", null, text);
  }
  const anchor = document.createElement("a");
  anchor.href = parsed.toString();
  anchor.textContent = String(text);
  anchor.target = "_blank";
  anchor.rel = "noopener noreferrer";
  return anchor;
}

export function hostOf(url) {
  try {
    return new URL(String(url)).host;
  } catch {
    return "";
  }
}

function badge(text, kind) {
  return el("span", "badge" + (kind ? " " + kind : ""), text);
}

function quantityText(item) {
  const parts = [item.ingredient || ""];
  const amount = [item.amount, item.unit].filter(Boolean).join(" ");
  if (amount) {
    parts.push("— " + amount);
  }
  return parts.join(" ");
}

function renderQuantities(list) {
  const wrap = el("div", null);
  wrap.appendChild(el("h3", null, "Quantities"));
  const ul = document.createElement("ul");
  for (const item of list || []) {
    const li = document.createElement("li");
    li.textContent = quantityText(item || {});
    ul.appendChild(li);
  }
  wrap.appendChild(ul);
  return wrap;
}

function renderAdaptations(list) {
  const wrap = el("div", null);
  wrap.appendChild(el("h3", null, "Adaptations"));
  const ul = document.createElement("ul");
  for (const item of list || []) {
    const li = document.createElement("li");
    li.textContent = String((item && item.description) || "");
    li.appendChild(badge("adaptation", "adaptation"));
    ul.appendChild(li);
  }
  wrap.appendChild(ul);
  return wrap;
}

function constraintText(entry) {
  const status = String(entry.status || "unknown");
  const value = String(entry.value || "");
  const terms = Array.isArray(entry.terms) ? entry.terms.filter(Boolean).map(String) : [];
  const unverified = Array.isArray(entry.unverified_terms)
    ? entry.unverified_terms.filter(Boolean).map(String)
    : [];
  let line = "tier " + status + ": " + value;
  const extras = [];
  if (terms.length > 0) {
    extras.push("terms: " + terms.join(", "));
  }
  if (unverified.length > 0) {
    extras.push("unverified: " + unverified.join(", "));
  }
  if (extras.length > 0) {
    line += " (" + extras.join("; ") + ")";
  }
  return line;
}

function renderConstraintCheck(list) {
  const wrap = el("div", null);
  wrap.appendChild(el("h3", null, "Constraint check"));
  const ul = document.createElement("ul");
  for (const entry of list || []) {
    const li = document.createElement("li");
    li.textContent = constraintText(entry || {});
    const disclaimer = entry && entry.disclaimer ? String(entry.disclaimer) : "";
    if (disclaimer) {
      const small = el("div", "small", disclaimer);
      li.appendChild(small);
    }
    ul.appendChild(li);
  }
  wrap.appendChild(ul);
  return wrap;
}

function renderEpicureLines(lines) {
  const wrap = el("div", null);
  wrap.appendChild(el("h3", null, "Epicure"));
  const ul = document.createElement("ul");
  for (const line of lines || []) {
    const li = document.createElement("li");
    li.textContent = String(line);
    ul.appendChild(li);
  }
  wrap.appendChild(ul);
  return wrap;
}

function renderNote(note, claims) {
  const wrap = el("div", null);
  const head = el("h3", null, "Note");
  if (claims === "verified" || claims === "unverified") {
    head.appendChild(badge(claims, claims));
  }
  wrap.appendChild(head);
  wrap.appendChild(el("p", "prewrap", note || ""));
  return wrap;
}

function renderOptions(result, actions) {
  const wrap = el("div", "card");
  wrap.appendChild(el("h2", null, "Options"));
  const options = Array.isArray(result.options) ? result.options : [];
  options.forEach((option, index) => {
    const card = el("div", "card");
    card.appendChild(el("h3", null, option.title || "Option " + (index + 1)));
    card.appendChild(
      el("div", "small", (option.dataset_id || "") + " / " + (option.source_id || ""))
    );
    if (Array.isArray(option.quantities) && option.quantities.length > 0) {
      card.appendChild(renderQuantities(option.quantities));
    }
    if (Array.isArray(option.adaptations) && option.adaptations.length > 0) {
      card.appendChild(renderAdaptations(option.adaptations));
    }
    const checks = (Array.isArray(result.constraint_check) ? result.constraint_check : []).filter(
      (entry) => entry && (entry.index === index || String(entry.source_id || "") === String(option.source_id || ""))
    );
    if (checks.length > 0) {
      card.appendChild(renderConstraintCheck(checks));
    }
    card.classList.add("option-card");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "choose-button";
    button.textContent = "Choose this";
    button.addEventListener("click", async () => {
      if (!actions || typeof actions.onSelect !== "function") {
        return;
      }
      // Immediate feedback: mark this card, dim and lock the others;
      // restored when the selection request fails.
      const cards = Array.from(wrap.querySelectorAll(".option-card"));
      for (const other of cards) {
        other.classList.toggle("option-chosen", other === card);
        other.classList.toggle("option-dimmed", other !== card);
        const otherButton = other.querySelector(".choose-button");
        if (otherButton) {
          otherButton.disabled = true;
        }
      }
      button.setAttribute("aria-pressed", "true");
      button.replaceChildren(el("span", "spinner"), document.createTextNode("Preparing your plan…"));
      card.classList.add("option-busy");
      card.setAttribute("aria-busy", "true");
      const ok = await actions.onSelect(option.dataset_id, option.source_id);
      card.classList.remove("option-busy");
      card.removeAttribute("aria-busy");
      if (ok === false) {
        for (const other of cards) {
          other.classList.remove("option-chosen", "option-dimmed");
          const otherButton = other.querySelector(".choose-button");
          if (otherButton) {
            otherButton.disabled = false;
          }
        }
        button.textContent = "Choose this";
        button.removeAttribute("aria-pressed");
      } else {
        button.textContent = "Chosen ✓";
      }
    });
    card.appendChild(button);
    wrap.appendChild(card);
  });
  if (Array.isArray(result.epicure_lines) && result.epicure_lines.length > 0) {
    wrap.appendChild(renderEpicureLines(result.epicure_lines));
  }
  if (Array.isArray(result.dropped_options) && result.dropped_options.length > 0) {
    const names = result.dropped_options.map((item) =>
      String((item && item.title) || (item && item.source_id) || "an option")
    );
    wrap.appendChild(el("p", "small", "Dropped: " + names.join("; ")));
  }
  if (result.note) {
    wrap.appendChild(renderNote(result.note, result.note_claims));
  }
  return wrap;
}

function renderPlan(result) {
  const plan = result.plan || {};
  const wrap = el("div", "card");
  wrap.appendChild(el("h2", null, "Cooking plan"));
  const source = plan.source || {};
  wrap.appendChild(
    el("div", "small", "source: " + (source.dataset_id || "") + " / " + (source.source_id || ""))
  );
  if (Array.isArray(plan.mise_en_place) && plan.mise_en_place.length > 0) {
    wrap.appendChild(el("h3", null, "Mise en place"));
    const ul = document.createElement("ul");
    for (const item of plan.mise_en_place) {
      const li = document.createElement("li");
      li.textContent = String(item);
      ul.appendChild(li);
    }
    wrap.appendChild(ul);
  }
  if (Array.isArray(plan.steps) && plan.steps.length > 0) {
    const head = el("h3", null, "Steps");
    if (plan.steps_source === "model_adaptation") {
      head.appendChild(badge("model adaptation", "unverified"));
    }
    wrap.appendChild(head);
    const ol = document.createElement("ol");
    for (const item of plan.steps) {
      const li = document.createElement("li");
      li.textContent = String(item);
      ol.appendChild(li);
    }
    wrap.appendChild(ol);
  }
  if (Array.isArray(plan.quantities) && plan.quantities.length > 0) {
    wrap.appendChild(renderQuantities(plan.quantities));
  }
  if (Array.isArray(plan.adaptations) && plan.adaptations.length > 0) {
    wrap.appendChild(renderAdaptations(plan.adaptations));
  }
  if (Array.isArray(plan.technique_refs) && plan.technique_refs.length > 0) {
    wrap.appendChild(el("h3", null, "Technique references"));
    const ul = document.createElement("ul");
    for (const ref of plan.technique_refs) {
      const li = document.createElement("li");
      li.textContent = String((ref && ref.doc_id) || "") + " · chunk " + String((ref && ref.chunk_id) ?? "");
      ul.appendChild(li);
    }
    wrap.appendChild(ul);
  }
  if (plan.plating) {
    wrap.appendChild(el("h3", null, "Plating"));
    wrap.appendChild(el("p", "prewrap", plan.plating));
  }
  return wrap;
}

function renderTechniqueAnswer(result) {
  const answer = result.technique_answer || {};
  const wrap = el("div", "card");
  wrap.appendChild(el("h2", null, "Technique answer"));
  wrap.appendChild(el("p", "prewrap", answer.text || ""));
  if (Array.isArray(answer.technique_refs) && answer.technique_refs.length > 0) {
    wrap.appendChild(el("h3", null, "References"));
    const ul = document.createElement("ul");
    for (const ref of answer.technique_refs) {
      const li = document.createElement("li");
      li.textContent = String((ref && ref.doc_id) || "") + " · chunk " + String((ref && ref.chunk_id) ?? "");
      ul.appendChild(li);
    }
    wrap.appendChild(ul);
  }
  const attribution = Array.isArray(answer.attribution) ? answer.attribution : [];
  if (attribution.length > 0) {
    wrap.appendChild(el("h3", null, "Attribution"));
    const ul = document.createElement("ul");
    for (const entry of attribution) {
      const li = document.createElement("li");
      li.textContent = String((entry && entry.attribution_text) || "");
      if (entry && entry.licence_url) {
        li.appendChild(document.createTextNode(" "));
        li.appendChild(safeLink(entry.licence_url, "licence"));
      }
      ul.appendChild(li);
    }
    wrap.appendChild(ul);
  }
  if (result.note) {
    wrap.appendChild(renderNote(result.note, result.note_claims));
  }
  return wrap;
}

const WEB_BANNER =
  "From the web — external sources, for discovery. Not verified by Culinary Copilot; check quantities and safety on the source pages.";

function renderWebAnswer(result) {
  const answer = result.web_answer || {};
  const wrap = el("div", "card");
  wrap.appendChild(el("div", "web-banner", WEB_BANNER));
  wrap.appendChild(el("h2", null, "Web answer"));
  wrap.appendChild(el("p", "prewrap", answer.text || ""));
  const refs = Array.isArray(answer.web_refs) ? answer.web_refs : [];
  if (refs.length > 0) {
    wrap.appendChild(el("h3", null, "Sources"));
    const ol = document.createElement("ol");
    ol.className = "citation-list";
    refs.forEach((ref) => {
      const item = document.createElement("li");
      item.appendChild(safeLink((ref && ref.url) || "", (ref && ref.title) || (ref && ref.url) || ""));
      const host = hostOf((ref && ref.url) || "");
      if (host) {
        const span = el("span", "host", " " + host);
        item.appendChild(span);
      }
      item.appendChild(document.createTextNode(" "));
      item.appendChild(badge(String((ref && ref.label) || "unclassified"), "source-label"));
      ol.appendChild(item);
    });
    wrap.appendChild(ol);
  }
  return wrap;
}

function renderQuestion(result, actions) {
  const question = result.question || {};
  const wrap = el("div", "card");
  wrap.appendChild(el("h2", null, "Question"));
  wrap.appendChild(el("p", "prewrap", question.question_text || ""));
  const options = Array.isArray(question.options) ? question.options : [];
  if (options.length > 0) {
    const ul = document.createElement("ul");
    for (const option of options) {
      const li = document.createElement("li");
      li.textContent = String(option);
      ul.appendChild(li);
    }
    wrap.appendChild(ul);
  }
  if (actions && typeof actions.onQuestionShown === "function") {
    actions.onQuestionShown(question);
  }
  return wrap;
}

function renderOutcomeLike(result, actions, kind) {
  const outcome = outcomeFor({
    reason: result.reason || result.stop_reason,
    nextAction: result.next_action || result.nextAction,
    message: result.message,
  });
  const wrap = el("div", "card");
  wrap.appendChild(el("h2", null, outcome.title));
  wrap.appendChild(el("p", "prewrap", outcome.body));
  if (outcome.message) {
    wrap.appendChild(el("p", "small prewrap", outcome.message));
  }
  if (outcome.action === "retry" && actions && typeof actions.onRetry === "function") {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Try again";
    button.addEventListener("click", () => actions.onRetry());
    wrap.appendChild(button);
  } else if (outcome.action === "reload" && actions && typeof actions.onReload === "function") {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Reload session";
    button.addEventListener("click", () => actions.onReload());
    wrap.appendChild(button);
  } else if (outcome.action === "new_session" && actions && typeof actions.onNewSession === "function") {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Start a new session";
    button.addEventListener("click", () => actions.onNewSession());
    wrap.appendChild(button);
  } else if (outcome.action === "rephrase") {
    wrap.appendChild(el("p", "small", "Rephrase your request and send it again."));
  }
  if (kind === "error") {
    wrap.setAttribute("role", "alert");
  }
  return wrap;
}

function renderUnsupported(result) {
  const keys = Object.keys(result || {}).filter((key) =>
    ["options", "plan", "technique_answer", "web_answer", "question"].includes(key)
  );
  const wrap = el("div", "card");
  wrap.appendChild(el("h2", null, "Unsupported answer type"));
  const named = keys.length > 0 ? keys.join(", ") : Object.keys(result || {}).join(", ");
  wrap.appendChild(el("p", null, "The server returned an answer this page does not render: " + (named || "unknown") + "."));
  return wrap;
}

/* Dispatch on the final's result keys. Unknown keys render a neutral
 * card that names the key and never throws. */
export function renderFinal(result, actions) {
  const data = result && typeof result === "object" ? result : {};
  try {
    if (data.options !== undefined && data.options !== null) {
      return RENDERERS.options(data, actions);
    }
    if (data.plan !== undefined && data.plan !== null) {
      return RENDERERS.plan(data, actions);
    }
    if (data.technique_answer !== undefined && data.technique_answer !== null) {
      return RENDERERS.technique_answer(data, actions);
    }
    if (data.web_answer !== undefined && data.web_answer !== null) {
      return RENDERERS.web_answer(data, actions);
    }
    if (data.question !== undefined && data.question !== null) {
      return RENDERERS.question(data, actions);
    }
    if (data.stop_reason || data.reason) {
      return RENDERERS.outcome(data, actions);
    }
    return renderUnsupported(data);
  } catch {
    return renderUnsupported(data);
  }
}

export const RENDERERS = {
  options: renderOptions,
  plan: renderPlan,
  technique_answer: renderTechniqueAnswer,
  web_answer: renderWebAnswer,
  question: renderQuestion,
  outcome: (data, actions) => renderOutcomeLike(data, actions, "outcome"),
  error: (data, actions) => renderOutcomeLike(data, actions, "error"),
};
