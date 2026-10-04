/* Demo page: render every card from the synthetic fixtures.
 * This module makes no backend calls; it only reads the bundled
 * fixture file and reuses render.js. */

import { el, renderFinal, RENDERERS } from "./render.js";

function section(title) {
  const wrap = document.createElement("section");
  wrap.className = "demo-section";
  wrap.appendChild(el("h2", null, title));
  return wrap;
}

function demoActions() {
  return {
    onSelect: () => {},
    onRetry: () => {},
    onReload: () => {},
    onNewSession: () => {},
    onQuestionShown: () => {},
  };
}

async function init() {
  const root = document.getElementById("demo-root");
  const response = await fetch("fixtures/demo-finals.json");
  const fixtures = await response.json();

  const optionsSection = section("Options");
  optionsSection.appendChild(renderFinal(fixtures.options, demoActions()));
  root.appendChild(optionsSection);

  const planSection = section("Plan");
  planSection.appendChild(renderFinal(fixtures.plan, demoActions()));
  root.appendChild(planSection);

  const techniqueSection = section("Technique answer");
  techniqueSection.appendChild(renderFinal(fixtures.technique_answer, demoActions()));
  root.appendChild(techniqueSection);

  const webSection = section("Web answer");
  webSection.appendChild(renderFinal(fixtures.web_answer, demoActions()));
  root.appendChild(webSection);

  const questionSection = section("Question");
  questionSection.appendChild(renderFinal(fixtures.question, demoActions()));
  root.appendChild(questionSection);

  const outcomesSection = section("Outcomes");
  for (const outcome of fixtures.outcomes || []) {
    outcomesSection.appendChild(RENDERERS.outcome(outcome, demoActions()));
  }
  root.appendChild(outcomesSection);

  const errorSection = section("Error");
  errorSection.appendChild(RENDERERS.error(fixtures.error || {}, demoActions()));
  root.appendChild(errorSection);

  const unknownSection = section("Unsupported answer type");
  unknownSection.appendChild(renderFinal({ future_shape: true }, demoActions()));
  root.appendChild(unknownSection);
}

void init();
