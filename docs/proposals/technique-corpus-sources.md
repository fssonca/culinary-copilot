# Technique corpus sources — proposal (PART 1)

**AI-drafted, pending owner approval. Do NOT fetch, ingest, or embed anything on the basis of this draft. Approval of the final list authorizes fetching exactly the approved URLs, and nothing else.**

- Branch: `main`, HEAD `c5b922e` (verified 2026-09-28).
- Scope basis: Checkpoint 0 decision 4 (30–60 technique documents, specific sources approved by the owner before anything is ingested); shared instructions in `docs/milestone-3-execution-plan.md`; actual code checked (see §4 citations).
- Phase 3 trajectories read: `evals/phase3_agent/trajectories.json` + `REVIEW.md`. Gaps addressed below.
- Nothing in this proposal was saved, downloaded, or committed except this file and the status line in `docs/milestone-3-execution-plan.md`. No DB changes, migrations, model or embedding calls, code changes, or `.env` edits were made.

## 1. Scope

Target **40 documents** (hard range 30–60). Spread across the phases the agent actually serves (**plan, cook, plate**) and the gaps visible in the Phase 3 trajectories:

- The offline packet covers recommend → select → plan, plus yogurt ask-and-resume, direct lookup, constraint conflict, empty retrieval, tool failure, budget/wall-clock stops, Epicure skip (`simple_technique_question`, e.g. "How do I boil an egg"), and no-progress. Plans contain mise en place, steps, and plating strings with no technique evidence behind them (`brown chicken`, `simmer 25 minutes`, `over rice in shallow bowls`, `in deep bowls with lemon`).
- There is currently no `search_techniques` corpus (stub returns `tool_not_configured`; see `src/culinary_copilot/tools/stub_tools.py`), so every technique claim in a plan/cook/plate step is unsupported. The corpus below is sized to back technique claims only — never to recommend a dish or stand in for a recipe source.

Planned count per topic (40 total):

| Topic | Count | Serves |
|---|---|---|
| Heat methods: sear, sauté, roast, braise, poach, steam, fry, grill/broil | 10 | cook, plan |
| Knife work and prep (incl. measuring, technique overview) | 3 | plan (mise en place), cook |
| Stocks, sauces, emulsions | 4 | cook, plan |
| Eggs | 3 | cook, plan (covers the "boil an egg" skip case with citable evidence) |
| Rice, grains, pasta, legumes | 5 | cook, plan |
| Basic dough and baking | 3 | cook, plan |
| Seasoning and balance (salt, acid, fat/browning, deglazing) | 3 | cook, plan |
| Food safety: internal temperatures, thawing, holding, storage, cross-contamination | 5 | cook, plan (safety-critical; authoritative pages only) |
| Common substitutions and why they work | 2 | plan, cook (covers the yogurt → coconut-milk adaptation pattern) |
| Basic plating | 2 | plate |
| **Total** | **40** | |

Reserves: **8** beyond the 40 (see §3, `R1`–`R8`), to use only if the owner strikes items. Reserves are not included in token/cost totals unless promoted.

## 2. Licence policy (read before the table)

- **Include only:** public domain US-government works (e.g. USDA FSIS, FDA food-safety pages), `CC0-1.0`, `CC-BY-4.0`, or `CC-BY-SA-4.0` (e.g. Wikibooks Cookbook, Wikipedia). This proposal uses only `CC-BY-SA-4.0` and US-government public domain — no `CC0`/`CC-BY` candidates were needed.
- **Share-alike flagged explicitly.** Every `CC-BY-SA-4.0` row is marked `Share-alike: YES`. If we ever **redistributed** derived text (e.g. published adapted excerpts outside the local DB), share-alike would oblige us to license those contributions under `CC-BY-SA-4.0` or later, give attribution (link/URL to the page + licence link), and indicate changes. **Local storage, chunking, embedding, and retrieval alone do not redistribute**; the owner decides whether any redistribution ever happens. No redistribution is proposed in part 1 or part 2.
- **Excluded:** all-rights-reserved cooking sites (magazines, blogs, recipe sites), even when freely readable. Link-only references are not corpus documents.
- **Fetch preference:** official APIs/dumps over HTML scraping. For Wikimedia: Action API (`action=query`, `prop=revisions|extracts`, `revids`/`oldid` for revision pinning) or REST API, serial requests, `maxlag` respected, gzip, caching, and a descriptive `User-Agent` with contact per `API:Etiquette` and the User-Agent policy (verified; links in §3 header). For FDA/FSIS: direct HTTPS page fetch at low rate, respect `robots.txt`, cache; there is no official content API for these pages.
- **Verification rule:** if a licence cannot be verified, it is marked **`unverified`**. Nothing below is guessed: `CC-BY-SA-4.0` is cited to the publisher copyright pages + footer observed on opened samples; FDA public-domain is cited to the opened website-policies page; FSIS pages could not be fetched from this environment (HTTP 403 on every `fsis.usda.gov` and `foodsafety.gov` attempt) so they are marked **`unverified`** with the statutory basis noted but not asserted as verified. The owner confirms FSIS URLs/licence via a normal browser before approval.
- Licence/terms links used (all opened except where noted):
  - Wikipedia copyrights: `https://en.wikipedia.org/wiki/Wikipedia:Copyrights` (opened; text dual `CC-BY-SA-4.0` + GFDL).
  - Wikibooks copyrights: `https://en.wikibooks.org/wiki/Wikibooks:Copyrights` (opened; text dual `CC-BY-SA-4.0` + GFDL).
  - `CC-BY-SA-4.0` deed: `https://creativecommons.org/licenses/by-sa/4.0/deed.en` (opened).
  - WMF Terms of Use: `https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use` (opened; licensing §7).
  - API etiquette: `https://www.mediawiki.org/wiki/API:Etiquette` (opened).
  - User-Agent policy: `https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy` (opened).
  - FDA website policies (public domain statement): `https://www.fda.gov/about-fda/about-website/website-policies` (opened; "contents … are not copyrighted … in the public domain … Credit … appreciated but not required").
  - 17 USC §105 (statutory basis for federal PD, not a per-page verification): `https://www.law.cornell.edu/uscode/text/17/105` (opened).
  - USDA policies page: `https://www.usda.gov/policies-and-links` (attempted; **403, not verified**).
  - FSIS/foodsafety.gov pages: all attempts **403, not verified** (see §5).

## 3. Candidate table

Conventions:

- `Size` is approximate words of main article text (nav/footer excluded), estimated from the two fully opened samples plus publisher-typical lengths; confirmed byte counts come at fetch time via the API. Not a commitment.
- `Licence` uses SPDX-style ids: `CC-BY-SA-4.0` for Wikimedia; `US-PD` (shorthand for US-federal public domain under 17 USC §105; not an SPDX licence id) for FDA; `unverified` for FSIS (expected `US-PD`, blocked from verification here).
- `Reuse` abbreviates: `A` = attribution text required; `SA` = share-alike on redistribution; `L` = storing full text locally + embedding permitted; `F` = fetch terms/restriction.
- Attribution text proposed for the manifest (part 2): Wikimedia rows — `"Title" — Wikipedia/Wikibooks contributors, CC BY-SA 4.0, via <URL> (oldid <id>, retrieved <UTC date>)`; FDA rows — `U.S. Food and Drug Administration, public domain, via <URL> (retrieved <UTC date>)`; FSIS rows (if approved+verified) — `U.S. Department of Agriculture, Food Safety and Inspection Service, public domain, via <URL> (retrieved <UTC date>)`.
- All Wikimedia fetches: Action/REST API with descriptive `User-Agent` (e.g. `culinary-copilot-technique-corpus/0.1 (contact: <owner-contact>; owner-approved fetch) requests`), serial, `maxlag`, gzip, cache; record `oldid`. No HTML scraping. No images/media ingested (text only; non-text licences differ per file).

| # | Doc id | Title | Exact URL | Publisher | Licence | Licence/terms link | Reuse (A / SA / L / F) | ~Words | Topic | Why it belongs (one line) |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | tech-sear-01 | Searing | https://en.wikipedia.org/wiki/Searing | Wikimedia Foundation (English Wikipedia) | CC-BY-SA-4.0 | https://en.wikipedia.org/wiki/Wikipedia:Copyrights ; https://creativecommons.org/licenses/by-sa/4.0/deed.en | A: link to page + licence, indicate changes. SA: YES. L: yes. F: API + UA, serial, maxlag. | ~700 | heat: sear | Backs `brown chicken` crust/Maillard claims and corrects "seals in juices". |
| 2 | tech-saute-02 | Sautéing | https://en.wikipedia.org/wiki/Saut%C3%A9ing | Wikimedia Foundation (English Wikipedia) | CC-BY-SA-4.0 | (same Wikipedia pair as #1) | A/SA/L/F: same as #1. | ~800 | heat: sauté | Covers high-heat pan method behind curry/stir steps. |
| 3 | tech-roast-03 | Roasting | https://en.wikipedia.org/wiki/Roasting | Wikimedia Foundation (English Wikipedia) | CC-BY-SA-4.0 | (same Wikipedia pair) | A/SA/L/F: same as #1. | ~1200 | heat: roast | Covers oven-roast time/temp logic for the roast-pairing trajectory. |
| 4 | tech-braise-04 | Braising | https://en.wikipedia.org/wiki/Braising | Wikimedia Foundation (English Wikipedia) | CC-BY-SA-4.0 | (same Wikipedia pair) | A/SA/L/F: same as #1. | ~900 | heat: braise | Combination sear-then-moist method; supports plan steps that braise. |
| 5 | tech-poach-05 | Poaching (cooking) | https://en.wikipedia.org/wiki/Poaching_(cooking) | Wikimedia Foundation (English Wikipedia) | CC-BY-SA-4.0 | (same Wikipedia pair) | A/SA/L/F: same as #1. | ~700 | heat: poach | Low-moist-heat baseline for delicate proteins. |
| 6 | tech-steam-06 | Steaming | https://en.wikipedia.org/wiki/Steaming | Wikimedia Foundation (English Wikipedia) | CC-BY-SA-4.0 | (same Wikipedia pair) | A/SA/L/F: same as #1. | ~600 | heat: steam | Indirect-moist-heat method missing from recipe corpus. |
| 7 | tech-fry-07 | Frying | https://en.wikipedia.org/wiki/Frying | Wikimedia Foundation (English Wikipedia) | CC-BY-SA-4.0 | (same Wikipedia pair) | A/SA/L/F: same as #1. | ~1500 | heat: fry | Parent method for pan/shallow/deep fry distinctions. |
| 8 | tech-grill-08 | Grilling | https://en.wikipedia.org/wiki/Grilling | Wikimedia Foundation (English Wikipedia) | CC-BY-SA-4.0 | (same Wikipedia pair) | A/SA/L/F: same as #1. | ~1500 | heat: grill | Radiant-heat method; pairs with roast-trajectory gaps. |
| 9 | tech-broil-09 | Cookbook: Broiling | https://en.wikibooks.org/wiki/Cookbook:Broiling | Wikimedia Foundation (English Wikibooks / Cookbook) | CC-BY-SA-4.0 | https://en.wikibooks.org/wiki/Wikibooks:Copyrights ; https://creativecommons.org/licenses/by-sa/4.0/deed.en | A: link to module + licence, indicate changes. SA: YES. L: yes. F: API + UA, serial, maxlag. | ~400 | heat: broil | Practical broiler how-to to complement grill theory. |
| 10 | tech-roast-cb-10 | Cookbook: Roasting | https://en.wikibooks.org/wiki/Cookbook:Roasting | Wikimedia Foundation (English Wikibooks / Cookbook) | CC-BY-SA-4.0 | (same Wikibooks pair) | A/SA/L/F: same as #9. | ~600 | heat: roast | Hands-on roast procedure to pair with Wikipedia theory. |
| 11 | tech-knife-11 | Cookbook: Knife Skills | https://en.wikibooks.org/wiki/Cookbook:Knife_Skills | Wikimedia Foundation (English Wikibooks / Cookbook) | CC-BY-SA-4.0 | (same Wikibooks pair) | A/SA/L/F: same as #9. | ~800 | knife/prep | Backs `dice chicken/onion` mise en place claims. |
| 12 | tech-measure-12 | Cookbook: Measuring | https://en.wikibooks.org/wiki/Cookbook:Measuring | Wikimedia Foundation (English Wikibooks / Cookbook) | CC-BY-SA-4.0 | (same Wikibooks pair) | A/SA/L/F: same as #9. | ~500 | knife/prep | Supports `measure yogurt` and scale/convert-adjacent steps. |
| 13 | tech-overview-13 | Cookbook: Cooking Techniques | https://en.wikibooks.org/wiki/Cookbook:Cooking_Techniques | Wikimedia Foundation (English Wikibooks / Cookbook) | CC-BY-SA-4.0 | (same Wikibooks pair) | A/SA/L/F: same as #9. | ~900 | knife/prep (overview) | Index/orientation doc linking prep → moist/dry heat; plan-phase framing. |
| 14 | tech-stock-14 | Stock (food) | https://en.wikipedia.org/wiki/Stock_(food) | Wikimedia Foundation (English Wikipedia) | CC-BY-SA-4.0 | (same Wikipedia pair) | A/SA/L/F: same as #1. | ~1200 | stocks/sauces | Foundation for soups/sauces in lentil-soup trajectory. |
| 15 | tech-sauce-15 | Sauce | https://en.wikipedia.org/wiki/Sauce | Wikimedia Foundation (English Wikipedia) | CC-BY-SA-4.0 | (same Wikipedia pair) | A/SA/L/F: same as #1. | ~1000 | stocks/sauces | Mother-sauce taxonomy for gravy/sauce steps. |
| 16 | tech-emulsion-16 | Emulsion | https://en.wikipedia.org/wiki/Emulsion | Wikimedia Foundation (English Wikipedia) | CC-BY-SA-4.0 | (same Wikipedia pair) | A/SA/L/F: same as #1. Use the food-emulsion sections only. | ~1200 | emulsions | Explains why yogurt/coconut-milk breaks or holds. |
| 17 | tech-roux-17 | Cookbook: Roux | https://en.wikibooks.org/wiki/Cookbook:Roux | Wikimedia Foundation (English Wikibooks / Cookbook) | CC-BY-SA-4.0 | (same Wikibooks pair) | A/SA/L/F: same as #9. | ~400 | stocks/sauces | Thickening how-to for soups/sauces. |
| 18 | tech-egg-boil-18 | Boiled egg | https://en.wikipedia.org/wiki/Boiled_egg | Wikimedia Foundation (English Wikipedia) | CC-BY-SA-4.0 | (same Wikipedia pair) | A/SA/L/F: same as #1. | ~1000 | eggs | Direct evidence for the Epicure-skip "boil an egg" case. |
| 19 | tech-egg-sep-19 | Cookbook: Separating Eggs | https://en.wikibooks.org/wiki/Cookbook:Separating_Eggs | Wikimedia Foundation (English Wikibooks / Cookbook) | CC-BY-SA-4.0 | (same Wikibooks pair) | A/SA/L/F: same as #9. | ~300 | eggs | Prep prerequisite for egg dishes. |
| 20 | tech-egg-fried-20 | Fried egg | https://en.wikipedia.org/wiki/Fried_egg | Wikimedia Foundation (English Wikipedia) | CC-BY-SA-4.0 | (same Wikipedia pair) | A/SA/L/F: same as #1. | ~800 | eggs | Fat-heat egg method contrasting boiling. |
| 21 | tech-rice-boil-21 | Cookbook: Boiling Rice | https://en.wikibooks.org/wiki/Cookbook:Boiling_Rice | Wikimedia Foundation (English Wikibooks / Cookbook) | CC-BY-SA-4.0 | (same Wikibooks pair) | A/SA/L/F: same as #9. | ~500 | rice/grains | Backs `over rice` plating with a citable cook method. |
| 22 | tech-pasta-boil-22 | Cookbook: Boiling Pasta | https://en.wikibooks.org/wiki/Cookbook:Boiling_Pasta | Wikimedia Foundation (English Wikibooks / Cookbook) | CC-BY-SA-4.0 | (same Wikibooks pair) | A/SA/L/F: same as #9. | ~500 | pasta | Core pasta method absent from recipe text. |
| 23 | tech-rice-23 | Cooked rice | https://en.wikipedia.org/wiki/Cooked_rice | Wikimedia Foundation (English Wikipedia) | CC-BY-SA-4.0 | (same Wikipedia pair) | A/SA/L/F: same as #1. | ~800 | rice/grains | Rice varieties/methods overview. |
| 24 | tech-pasta-24 | Pasta | https://en.wikipedia.org/wiki/Pasta | Wikimedia Foundation (English Wikipedia) | CC-BY-SA-4.0 | (same Wikipedia pair) | A/SA/L/F: same as #1. | ~1500 | pasta | Shapes/sauces pairing context for plans. |
| 25 | tech-beans-25 | Cookbook: Soaking Beans | https://en.wikibooks.org/wiki/Cookbook:Soaking_Beans | Wikimedia Foundation (English Wikibooks / Cookbook) | CC-BY-SA-4.0 | (same Wikibooks pair) | A/SA/L/F: same as #9. | ~400 | legumes | Legume prep (rinse/soak/simmer) for lentil trajectory. |
| 26 | tech-baking-26 | Baking | https://en.wikipedia.org/wiki/Baking | Wikimedia Foundation (English Wikipedia) | CC-BY-SA-4.0 | (same Wikipedia pair) | A/SA/L/F: same as #1. | ~1200 | dough/baking | Dry-heat baking principles. |
| 27 | tech-bread-27 | Cookbook: Bread Making | https://en.wikibooks.org/wiki/Cookbook:Bread_Making | Wikimedia Foundation (English Wikibooks / Cookbook) | CC-BY-SA-4.0 | (same Wikibooks pair) | A/SA/L/F: same as #9. | ~800 | dough/baking | Hands-on dough/proof/bake flow. |
| 28 | tech-blind-28 | Cookbook: Blind-baking | https://en.wikibooks.org/wiki/Cookbook:Blind-baking | Wikimedia Foundation (English Wikibooks / Cookbook) | CC-BY-SA-4.0 | (same Wikibooks pair) | A/SA/L/F: same as #9. | ~300 | dough/baking | Niche pastry technique for completeness. |
| 29 | tech-season-29 | Seasoning | https://en.wikipedia.org/wiki/Seasoning | Wikimedia Foundation (English Wikipedia) | CC-BY-SA-4.0 | (same Wikipedia pair) | A/SA/L/F: same as #1. | ~600 | seasoning | Salt/herb/spice layering for balance claims. |
| 30 | tech-caramel-30 | Cookbook: Caramelization | https://en.wikibooks.org/wiki/Cookbook:Caramelization | Wikimedia Foundation (English Wikibooks / Cookbook) | CC-BY-SA-4.0 | (same Wikibooks pair) | A/SA/L/F: same as #9. | ~400 | seasoning/balance | Sugar-browning vs Maillard distinction (see #1). |
| 31 | tech-deglaze-31 | Cookbook: Deglazing | https://en.wikibooks.org/wiki/Cookbook:Deglazing | Wikimedia Foundation (English Wikibooks / Cookbook) | CC-BY-SA-4.0 | (same Wikibooks pair) | A/SA/L/F: same as #9. | ~400 | seasoning/balance | Acid/fond pan-sauce lift for sear/braise plans. |
| 32 | tech-fda-safe-32 | Safe Food Handling | https://www.fda.gov/food/buy-store-serve-safe-food/safe-food-handling | U.S. Food and Drug Administration | US-PD | https://www.fda.gov/about-fda/about-website/website-policies | A: none legally required; credit FDA appreciated. SA: none. L: yes. F: direct HTTPS, low rate, respect robots.txt; no API. | ~900 | food safety | Single authoritative clean/separate/cook/chill + temp table. |
| 33 | tech-fda-kitchen-33 | Food Safety in Your Kitchen | https://www.fda.gov/food/buy-store-serve-safe-food/food-safety-your-kitchen | U.S. Food and Drug Administration | US-PD | (same FDA policies page; page itself not opened here — publisher terms verified) | A: none required, credit appreciated. SA: none. L: yes. F: same as #32. | ~800 | food safety | Kitchen-level storage/holding/cross-contamination detail. |
| 34 | tech-fsis-temp-34 | Safe Temperature Chart | https://www.fsis.usda.gov/food-safety/safe-food-handling-and-preparation/food-safety-basics/safe-temperature-chart | U.S. Dept. of Agriculture, FSIS | unverified (expected US-PD; FSIS pages blocked 403 here — owner to confirm) | Attempted https://www.usda.gov/policies-and-links (403); statutory context https://www.law.cornell.edu/uscode/text/17/105 | A: if PD, none required (credit FSIS). SA: if PD, none. L: if PD, yes — pending verification. F: owner to confirm via browser; respect robots.txt; low rate. | ~600 | food safety | Canonical safe internal temperatures. |
| 35 | tech-fsis-basics-35 | Basics for Handling Food Safely | https://www.fsis.usda.gov/food-safety/safe-food-handling-and-preparation/food-safety-basics/basics-handling-food-safely | U.S. Dept. of Agriculture, FSIS | unverified (same as #34) | (same as #34) | A/SA/L/F: same as #34. | ~1200 | food safety | Thawing/holding/storage authority text. |
| 36 | tech-fsis-leftover-36 | Leftovers and Food Safety | https://www.fsis.usda.gov/food-safety/safe-food-handling-and-preparation/food-safety-basics/leftovers-and-food-safety | U.S. Dept. of Agriculture, FSIS | unverified (same as #34) | (same as #34) | A/SA/L/F: same as #34. | ~800 | food safety | Cooling/reheating/holding authority text. |
| 37 | tech-egg-sub-37 | Egg substitute | https://en.wikipedia.org/wiki/Egg_substitute | Wikimedia Foundation (English Wikipedia) | CC-BY-SA-4.0 | (same Wikipedia pair) | A/SA/L/F: same as #1. | ~700 | substitutions | Why egg subs work/fail (binding/leavening limits). |
| 38 | tech-sugar-sub-38 | Sugar substitute | https://en.wikipedia.org/wiki/Sugar_substitute | Wikimedia Foundation (English Wikipedia) | CC-BY-SA-4.0 | (same Wikipedia pair) | A/SA/L/F: same as #1. | ~1200 | substitutions | Why sweetness subs differ on bulk/browning; no dietary claim. |
| 39 | tech-plate-39 | Food presentation | https://en.wikipedia.org/wiki/Food_presentation | Wikimedia Foundation (English Wikipedia) | CC-BY-SA-4.0 | (same Wikipedia pair) | A/SA/L/F: same as #1. | ~600 | plating | Evidence for plating strings (`shallow bowls`, `deep bowls`). |
| 40 | tech-garnish-40 | Garnish (food) | https://en.wikipedia.org/wiki/Garnish_(food) | Wikimedia Foundation (English Wikipedia) | CC-BY-SA-4.0 | (same Wikipedia pair) | A/SA/L/F: same as #1. | ~500 | plating | Finishing/garnish technique for plate phase. |

Reserves (use only if the owner strikes items above; same licence/fetch rules):

| # | Doc id | Title | Exact URL | Publisher | Licence | ~Words | Topic | Why it is a good reserve |
|---|---|---|---|---|---|---|---|---|
| R1 | tech-fry-deep-R1 | Cookbook: Deep Fat Frying | https://en.wikibooks.org/wiki/Cookbook:Deep_Fat_Frying | Wikibooks / Cookbook | CC-BY-SA-4.0 | ~500 | heat: fry | Deep-fry safety/procedure if #7 needs a practical companion. |
| R2 | tech-simmer-R2 | Cookbook: Simmering | https://en.wikibooks.org/wiki/Cookbook:Simmering | Wikibooks / Cookbook | CC-BY-SA-4.0 | ~400 | heat: poach/simmer | Gentle-heat companion to #5. |
| R3 | tech-blanch-R3 | Cookbook: Blanching | https://en.wikibooks.org/wiki/Cookbook:Blanching | Wikibooks / Cookbook | CC-BY-SA-4.0 | ~400 | prep/heat | Veg prep technique bridging knife work and heat. |
| R4 | tech-marinate-R4 | Cookbook: Marinating | https://en.wikibooks.org/wiki/Cookbook:Marinating | Wikibooks / Cookbook | CC-BY-SA-4.0 | ~400 | prep/seasoning | Time-based flavor prep if seasoning section is struck. |
| R5 | tech-yeast-R5 | Cookbook: Preparing Yeast | https://en.wikibooks.org/wiki/Cookbook:Preparing_Yeast | Wikibooks / Cookbook | CC-BY-SA-4.0 | ~400 | dough/baking | Yeast handling if #27/#28 are struck. |
| R6 | tech-freeze-R6 | Cookbook: Freezing | https://en.wikibooks.org/wiki/Cookbook:Freezing | Wikibooks / Cookbook | CC-BY-SA-4.0 | ~500 | food safety (storage) | Storage companion; Wikibooks alternative if an FSIS item is struck. |
| R7 | tech-foodsafety-R7 | Food safety | https://en.wikipedia.org/wiki/Food_safety | Wikipedia | CC-BY-SA-4.0 | ~2000 | food safety | Cross-contamination overview; reserve because authority pages (#32–36) take precedence. |
| R8 | tech-umami-R8 | Umami | https://en.wikipedia.org/wiki/Umami | Wikipedia | CC-BY-SA-4.0 | ~1200 | seasoning | Savory-balance depth if seasoning section is struck. |

Counts: main 40 = 21 Wikipedia `CC-BY-SA-4.0` + 14 Wikibooks `CC-BY-SA-4.0` + 2 FDA `US-PD` + 3 FSIS `unverified`. Reserves add 2 Wikipedia + 6 Wikibooks `CC-BY-SA-4.0`. No `CC0`/`CC-BY` candidates used. Every share-alike row is flagged above; all 35 (+8 reserves) Wikimedia rows are share-alike.

## 4. Ingestion design sketch (proposal only — nothing implemented in part 1)

### a) Provenance per document

Store per document (in DB row + manifest): `doc_id`, `title`, `url` (exact approved URL), `publisher`, `licence` (SPDX-style id as in §3), `attribution_text` (per §3), `retrieval_date_utc` (ISO-8601), `revision_id` where the source has one (Wikimedia `oldid` from the API response; FDA/FSIS pages have no revision id — record `etag`/`last-modified` if sent, else `none`), and `sha256_raw` (fetched raw bytes: API JSON/wikitext payload) plus `sha256_normalized` (exact text that was chunked/embedded). Re-fetch or re-normalization that changes either hash invalidates embeddings for that doc (same identity discipline as `embeddings/rendering.py::embedding_identity`).

### b) Storage

- Raw (`<doc_id>.raw.json`: API payload + headers) and normalized text (`<doc_id>.txt` + `<doc_id>.meta.json` with §4a fields) go under `data/technique-corpus/`, which stays out of Git (extend `.gitignore`; same rule as `data/recipe-import/`, local datasets, and model responses per `AGENTS.md`).
- Commit a manifest with no document text, e.g. `evals/technique_corpus/manifest.json` (plus a short `README.md`): one entry per doc with `doc_id`, `title`, `url`, `publisher`, `licence`, `attribution_text`, `retrieval_date_utc`, `revision_id`, `sha256_raw`, `sha256_normalized`, word/byte counts. The manifest is the reviewable provenance record; text never enters Git.

### c) Migration 006

- New tables (names illustrative): `technique_documents` (one row per approved doc: `doc_id PK`, `title`, `url`, `publisher`, `licence`, `attribution_text`, `retrieval_date_utc`, `revision_id`, `sha256_raw`, `sha256_normalized`) and `technique_chunks` (`doc_id FK`, `chunk_id`, `section`, `chunk_text`, `tsvector`, ordering), plus `technique_embeddings` (chunk embeddings reusing the 004 pattern: `doc_id`, `chunk_id`, `model`, `dimension`, `renderer_version`, `chunking_version`, `embedded_text_hash`, `embedding vector`, ledger columns).
- Full-text via `tsvector` on chunks; vector via pgvector reusing the 004 column discipline (`vector` type, dimension CHECK, `vector_dims` check).
- **Stock-`postgres:17` question:** yes, the vector part must be split or guarded exactly as 005/004 are. Verified in code: `004_recipe_embeddings.sql` carries a `requires-extension: vector` header and `import_data.py::apply_migrations` (`_migration_required_extension`, `_extension_available`) **skips** vector-tagged migrations entirely — never partially — on stock `postgres:17`, preserving full-text-only operation; `005_sessions.sql` is plain PostgreSQL with no extension requirement and applies on both images. A single 006 that mixes `tsvector` tables with `vector` objects and carries the header would be skipped wholesale on stock PG (losing full-text too); without the header it would fail on stock PG. So 006 must **not** bundle both: propose `006_technique_corpus.sql` (plain PG: docs + chunks + `tsvector`, applies everywhere) and `007_technique_embeddings.sql` (`requires-extension: vector`, pgvector only), or equivalently one migration number with two files behind the same guard — but the ordered-migration convention (`001`–`005` + runner glob) favors two numbers. Either way the full-text corpus works on stock `postgres:17` and vectors activate only on the pgvector image.
- `001`–`005` stay byte-for-byte unchanged (runner checksums enforce this; violation raises `Applied migration … changed`).

### d) Chunking

- New separate renderer + chunking version in code (e.g. `TECHNIQUE_RENDER_VERSION`, `TECHNIQUE_CHUNK_VERSION` in a new `embeddings/technique_rendering.py` or equivalent). **Do not reuse** `EMBED_DOCUMENT_VERSION` / `CHUNKING_VERSION` in `embeddings/rendering.py` (currently both `"1"`, single-chunk `EMBED_MAX_CHARS 7000` truncation per recipe).
- Section-aware chunks: split normalized text on source sections (Wikimedia headings; FDA/FSIS headings), then pack to a **target ~400 tokens, hard max ~800 tokens** per chunk, one chunk never crossing a section boundary, with `section` recorded per chunk. Short sections stay whole; over-long sections split on paragraph boundaries.
- Why: recipe rendering is one truncated blob per recipe (identity = whole-recipe text); technique docs are expository with independent sections (e.g. FDA clean/separate/cook/chill; sear theory vs reverse-sear). Retrieval must cite a section-sized excerpt (safe temp, thaw rule), not a whole article. Separate versions keep technique re-chunking from invalidating the 004 recipe vectors and vice versa.

### e) `search_techniques`

- Keeps the Phase 2 schema (`query` 1–500, `limit?` 1–10 default 5; see `tools/stub_tools.py::SearchTechniquesArgs`) and gains an optional `mode` (`fulltext | vector`, default from a new setting e.g. `TECHNIQUE_RETRIEVAL_MODE` defaulting to `fulltext`). This mirrors the `search_recipes` mode decision (Checkpoint 0 decision 2; `tools.md`: `mode` arg wins, omitted → `RETRIEVAL_MODE`, vector cutoff, `mode_ran` logged, vector-without-embeddings returns `unavailable` with no silent fallback).
- Returns chunk excerpts: `doc_id`, `chunk_id`, `section`, `title`, `url`, `licence`, `attribution_text`, excerpt (bounded chars), and `mode_ran`. Cost class stays `free` for fulltext, `paid` when vector runs (one query embedding).
- Evidence discipline: technique references are a **separate evidence type** from recipe identities (`dataset_id, source_id`). They may support a technique claim in a `plan`/`cook` step (e.g. "sear at >150 °C for Maillard crust" citing tech-sear-01). They can **never** be a recommended option and never stand in for a recipe source.
- Validator changes implied in `agent/validate.py`: `validate_options` must reject any option lacking an exact `(dataset_id, source_id)` recipe resolution (technique ids are not options); `validate_plan` keeps quantity/plan-source checks unchanged and gains an optional `technique_refs` check (new `validate_technique_refs`: each ref resolves to a known technique `doc_id`/`chunk_id`, carries `url`+`licence`, and is cited to a plan/cook step — never to quantities or dish identity); hard-constraint handling unchanged. `tests/test_next_action.py` coverage must add the new reasons.

### f) Evaluation

- 15–20 technique retrieval cases with document-level relevance labels (which `doc_id`s are relevant per query), AI-drafted and **marked as such** in the case file (same honesty rule as the Phase 1 "AI-assisted, owner-accepted" labels; see `docs/retrieval.md`).
- Freeze the case file with a `sha256` recorded inside it **before any vector run** (same freeze discipline as `evals/results/phase1/phase2_inputs_freeze.json`).
- Metrics: **HitRate@5 and MRR** (document-level, k=5), full-text baseline vs vector. Reuse `scripts/retrieval_eval/metrics.py` (`recall_at_k`, `reciprocal_rank`) where it fits — note it currently computes topical Recall@k/MRR plus joint suitability for recipes; technique eval reuses the topical half (grade ≥1 = relevant) and skips joint suitability (no dietary/constraint layer for technique). Add a thin wrapper if the recipe-specific `joint_verdict`/aggregate-group logic does not map.
- Owner spot-checks requested: all 5 food-safety labels (temps/thawing/holding must be exact), the sear-vs-sauté-vs-braise discriminations, both substitution cases (why-substitution wording vs dietary claims), and any case where the top-1 is a short Wikibooks module (thin docs are the main false-positive risk).

### g) Cost

Pricing/model cited in code (not re-verified here; re-verify before any live run per registry docs):

- Embedding registry: `src/culinary_copilot/embeddings/registry.py` (`EMBED_PRICING_VERSION "2026-09-24-embed-v1"`): `text-embedding-3-small`, 1536 dims, **$0.02 / 1M input tokens** (Standard). `estimate_cost_usd` = `tokens/1e6 * input_per_1m`.
- Settings: `src/culinary_copilot/config.py` + `.env.example`: `EMBEDDING_MODEL=text-embedding-3-small`, `EMBEDDING_DIMENSION=1536`, `EMBED_MAX_RETRIES=1` (retry multiplier ×2), `EMBED_BUDGET_USD` (stop condition), `EMBEDDINGS_ENABLED=false` default.
- CLI: `scripts/embeddings/embed.py`: reservation = `sum(est_tokens) × (retries+1)` with `est_tokens` = `estimate_tokens_bytes` (`len(utf-8 bytes)+8`, an upper bound; see `embeddings/rendering.py`); `--ceiling-usd` refuses when reservation exceeds ceiling; `--live` additionally requires key + budget + `--yes` and is not authorized by any prior phase.

Estimate (40 docs + 18-query eval set; reserves excluded):

| Item | Basis | Tokens |
|---|---|---|
| Corpus text | ~31k words main text (§3 sizes sum) ≈ 1.3 tok/word | ~41,000 |
| Conservative upper bound (bytes rule) | ~200k UTF-8 bytes → tokens ≤ bytes | ≤200,000 |
| Query set (18 queries × ~15 words) | ~270 words | ~400 |
| **Planning figure (expected)** | word-based | **~41,500** |
| **Retry-inclusive reservation (×2)** | `EMBED_MAX_RETRIES=1` | **~83,000 (expected) / ≤400,000 (upper bound)** |

- Expected cost: `83,000/1e6 × $0.02 ≈ $0.0017`.
- Upper-bound reservation cost: `400,000/1e6 × $0.02 ≈ $0.008`.
- **Proposed dollar bound: $0.01** (covers the byte-upper-bound plus re-runs of dropped/stale chunks; ~6× expected).
- Share of the **$1.00 M3 ceiling**: `$0.01 = 1.0%` (expected spend ~0.2%). Of that ceiling, **$0.15 is proposed for the Phase 3 live run and not yet approved** (`evals/phase3_agent/LIVE_PLAN.md`: 8 sessions × 2 attempts × $0.009 = $0.15); technique embeddings would leave ~$0.84 for Phases 5/7 even at the bound.
- **Stop condition: `EMBED_BUDGET_USD`** (plus the CLI `--ceiling-usd`; the run refuses when the retry-inclusive reservation exceeds it; per-run ledger `embedding_runs` tracks reserved/used tokens).

### h) Go-ahead points after approval

1. Approval of this list authorizes fetching **exactly the approved URLs, and nothing else** (pinned `oldid` for Wikimedia at fetch time; no substitutes, no extra pages, no media).
2. Applying migration(s) to the **application DB needs a separate package and owner approval, as 005 did** (disposable-DB rehearsal + backup/restore + exact commands + rollback; do not apply on proposal approval).
3. The **paid embedding run needs its own go-ahead** (§4g bound, pricing re-verification with `EMBED_PRICING_VERSION` bump if changed, target DB guard, `EMBED_BUDGET_USD` + `--ceiling-usd` + `--yes`).

## 5. Fetch log (honest record)

Opened (11 successes; content not saved anywhere):

1. `https://en.wikipedia.org/wiki/Wikipedia:Copyrights` — confirms `CC-BY-SA-4.0` + GFDL dual licence for Wikipedia text.
2. `https://en.wikibooks.org/wiki/Wikibooks:Copyrights` — confirms `CC-BY-SA-4.0` + GFDL dual licence for Wikibooks text.
3. `https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use` — licensing §7 (attribution + share-alike on redistribution).
4. `https://creativecommons.org/licenses/by-sa/4.0/deed.en` — share/adapt freedoms; attribution + share-alike terms.
5. `https://www.mediawiki.org/wiki/API:Etiquette` — serial requests, pipe batching, gzip, `maxlag`, descriptive `User-Agent`.
6. `https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy` — informative UA string with contact required; generic agents may be blocked (403).
7. `https://en.wikipedia.org/wiki/Searing` — sample content page; footer confirms `CC-BY-SA-4.0`; ~700-word scale reference.
8. `https://en.wikibooks.org/wiki/Cookbook:Cooking_Techniques` — index confirming the Cookbook module URLs used in §3 exist; footer confirms `CC-BY-SA-4.0`.
9. `https://www.fda.gov/about-fda/about-website/website-policies` — FDA contents public domain ("republished, reprinted and otherwise used freely … without permission"; credit appreciated).
10. `https://www.fda.gov/food/buy-store-serve-safe-food/safe-food-handling` — sample content page (clean/separate/cook/chill + temp table); ~900-word scale reference.
11. `https://www.law.cornell.edu/uscode/text/17/105` — statutory PD basis for federal works (context only, not a per-page verification).

Attempted but blocked (4 attempts, all HTTP 403 via this fetch path; not verified):

- `https://www.fsis.usda.gov/policy/freedom-information-act/foia-frequently-asked-questions`
- `https://www.usda.gov/policies-and-links`
- `https://www.foodsafety.gov/about`
- `https://www.fsis.usda.gov/food-safety/safe-food-handling-and-preparation/food-safety-basics/safe-temperature-chart`

Not opened: the remaining 37 main-list content pages and all 8 reserve pages (§3). Their licences rest on the publisher terms verified above (Wikimedia/FDA) or are marked `unverified` (FSIS). Word counts for unopened pages are estimates to be confirmed at fetch time.

No paid web search was used. No content was saved, bulk-downloaded, or embedded. No DB, migration, model, code, commit, or `.env` actions were taken.

## 6. Decisions needed from the owner

1. **Approve / strike / substitute** each of the 40 rows + 8 reserves (reply with the approved `doc_id` list; anything not listed is not fetched).
2. **Confirm the 3 FSIS rows** (#34–36): verify URLs + public-domain status via a normal browser (this environment got 403s) or replace them with FDA equivalents.
3. **Confirm share-alike acceptance** for the 35 (+8 reserves) `CC-BY-SA-4.0` docs under the §2 policy (local storage/retrieval only; no redistribution proposed).
4. **Confirm the fetch method**: Wikimedia Action/REST API with a `culinary-copilot-technique-corpus/0.1 (contact: <owner-contact>)` User-Agent, serial + `maxlag`; direct low-rate HTTPS for FDA/FSIS with `robots.txt` respected.
5. **Confirm the ingestion sketch** (§4c split `006` plain + `007` vector; §4d separate technique renderer versions + 400/800-token section chunks; §4e `search_techniques` mode design; §4f 15–20 AI-drafted frozen cases + owner spot-check list; §4g $0.01 bound with `EMBED_BUDGET_USD` stop) — or correct it before part 2.
6. **Note the deferred go-aheads** (§4h): no fetching, migration apply, or paid embedding run happens until each is separately approved with its own package/bound.

## 7. Owner decisions (2026-09-28)

Owner answers were given in conversation with AI assistance (the options
were AI-drafted, and the owner chose them). They are recorded here verbatim
in effect.

1. **Approved list: 40 documents.** Only these doc ids may be fetched:
   - heat: `tech-sear-01`, `tech-saute-02`, `tech-roast-03`,
     `tech-braise-04`, `tech-poach-05`, `tech-steam-06`, `tech-fry-07`,
     `tech-grill-08`, `tech-broil-09`, `tech-roast-cb-10`;
   - prep: `tech-knife-11`, `tech-measure-12`, `tech-overview-13`,
     `tech-blanch-R3`, `tech-marinate-R4`;
   - stocks and sauces: `tech-stock-14`, `tech-sauce-15`, `tech-roux-17`,
     `tech-simmer-R2`;
   - eggs: `tech-egg-boil-18`, `tech-egg-sep-19`, `tech-egg-fried-20`;
   - rice, pasta and legumes: `tech-rice-boil-21`, `tech-pasta-boil-22`,
     `tech-rice-23`, `tech-beans-25`;
   - baking: `tech-baking-26`, `tech-bread-27`, `tech-blind-28`;
   - seasoning: `tech-season-29`, `tech-caramel-30`, `tech-deglaze-31`;
   - food safety: `tech-fda-safe-32`, `tech-fda-kitchen-33`,
     `tech-fsis-temp-34`, `tech-fsis-leftover-36`, `tech-freeze-R6`;
   - substitutions: `tech-egg-sub-37`;
   - plating: `tech-plate-39`, `tech-garnish-40`.

   URLs are exactly as listed in §3.
   - **Struck:**
     - `tech-emulsion-16` (mostly non-food chemistry);
     - `tech-pasta-24` (mostly history; `tech-pasta-boil-22` covers the
       method);
     - `tech-sugar-sub-38` (health and nutrition content; out of scope);
     - `tech-fsis-basics-35` (the owner's browser check returned 404).
   - **Unused reserves:** R1, R5, R7, R8. They are not approved for fetching.
2. **FSIS.** On 2026-09-28 the owner opened the pages in a browser:
   - `tech-fsis-temp-34` returned 200;
   - `tech-fsis-leftover-36` returned 200;
   - `tech-fsis-basics-35` returned 404, so it was struck.

   The two FSIS pages that load are accepted as US public domain. If the
   automated fetch is still blocked, drop them and report it. Do not work
   around the block.
3. **Share-alike (CC BY-SA 4.0) accepted.** Condition: every excerpt shown
   by the API or UI carries its attribution text and a licence link.
   Nothing is republished.
4. **Design §4 approved as proposed:**
   - `006` plain full-text and `007` vector (with `requires-extension:
     vector`);
   - separate technique renderer and chunking versions, section-aware
     chunks of about 400 tokens, 800 at most;
   - `search_techniques` with a `mode` argument, default `fulltext`;
   - 15–20 AI-drafted cases, frozen before **any** retrieval run (full-text
     included);
   - a $0.01 cap on the embedding run;
   - the User-Agent contact is the repository URL
     `https://github.com/fssonca/culinary-copilot`, not an email address.
5. **Fetch-time rule:** a page that does not exist, redirects to a
   different topic, or has fewer than ~150 words of normalized text is
   dropped and reported. No automatic substitutes.
