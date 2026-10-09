"""H2 quantity self-check (read-only, reproducible evidence).

Builds plans from each recipe's own text and runs the ingredient-aware
quantity check (agent/plan_quantities.py via validate.py):

- source_text: mise = originals verbatim; steps = directions verbatim.
- of_form: mise = "AMOUNT UNIT of NAME" per mass/volume ingredient.
- model_style: mise = originals with ®/™ removed, "(such as|like|e.g. …)"
  removed, before first comma, collapsed, lowercased, kept only with a
  mass/volume amount.
- swap: model_style lines with one same-unit different-amount pair
  swapped (normalized text so ½/¾ are not garbled); skipped when another
  source line for the same ingredient (shared name token 4+ letters,
  including the partner) states the swapped amount.
- descriptor_swap: same swaps, but the swapped line is prefixed with a
  descriptor from the partner's name when it has one.

Read-only transaction, no writes. Prints counts and up to 20 misses per
variant. Source variants show source text passes, not that wrong
amounts are caught; swap variants measure swap detection.
"""

import re

from sqlalchemy import create_engine, text

from culinary_copilot.agent.plan_quantities import (
    DESCRIPTOR_WORDS,
    PROSE_MEASURE_UNITS,
    _normalize_prose_text,
    plan_prose_quantity_errors,
    prose_quantities,
    prose_quantities_with_spans,
)
from culinary_copilot.agent.validate import doc_directions
from culinary_copilot.config import Settings
from culinary_copilot.recipes.llm_validate import canonical_unit
from culinary_copilot.recipes.normalize import quantity


def _mass_volume(amt: object, unit: object) -> tuple[str, str] | None:
    if amt is None or unit is None:
        return None
    value = quantity(str(amt))
    uname = canonical_unit(str(unit))
    if value and uname in PROSE_MEASURE_UNITS:
        return value, uname
    return None


def _model_line(original: object) -> str:
    s = str(original or "")
    s = s.replace("®", "").replace("™", "")
    s = re.sub(r"\((such as|like|e\.g\.)[^)]*\)", "", s, flags=re.I)
    s = s.split(",")[0]
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _name_tokens(name: object) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", str(name or "").lower()) if t]


def _descriptor_of(name: object) -> str | None:
    for tok in _name_tokens(name):
        if tok in DESCRIPTOR_WORDS:
            return tok
    return None


def _swap_pair(lines: list[str], names: list[str]) -> tuple[int, int, str, str, str, str] | None:
    """First same-unit different-amount pair with normalized claims."""
    parsed: list[tuple[str, str, str]] = []
    for line in lines:
        norm = _normalize_prose_text(line)
        amounts = prose_quantities_with_spans(norm)
        if not amounts:
            continue
        claim, value, unit, _s, _e = amounts[0]
        parsed.append((claim, value, unit))
    for i in range(len(lines)):
        if i >= len(parsed):
            continue
        for j in range(i + 1, len(lines)):
            if j >= len(parsed):
                continue
            _, vi, ui = parsed[i]
            _, vj, uj = parsed[j]
            if ui == uj and vi != vj:
                ci, _, _ = parsed[i][0], parsed[i][1], parsed[i][2]
                cj, _, _ = parsed[j][0], parsed[j][1], parsed[j][2]
                return i, j, ci, cj, ui, uj
    return None


def main() -> None:
    engine = create_engine(Settings().database_url.get_secret_value())
    with engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        rows = list(
            conn.execute(
                text(
                    "SELECT dataset_id, source_id, document FROM recipes "
                    "ORDER BY dataset_id, source_id"
                )
            ).all()
        )
    for variant in ("source_text", "of_form", "model_style", "swap", "descriptor_swap"):
        passes = 0
        examined = 0
        caught = 0
        tried = 0
        failures: list[tuple[str, str, str | None, str]] = []
        for dataset_id, source_id, doc in rows:
            dirs = doc_directions(doc)
            steps = list(dirs) if dirs else ["Serve hot."]
            step_sources = list(range(len(dirs))) if dirs else [None]
            if variant == "source_text":
                mise = [
                    str(ing.get("original") or "").strip()
                    for ing in doc.get("ingredients", [])
                    if str(ing.get("original") or "").strip()
                ]
                plan = {
                    "mise_en_place": mise,
                    "steps": steps,
                    "step_sources": step_sources,
                    "plating": "Serve hot.",
                }
                errors = plan_prose_quantity_errors(plan, doc)
                if not errors:
                    passes += 1
                elif len(failures) < 20:
                    failures.append((dataset_id, source_id, doc.get("title"), errors[0][:400]))
                continue
            if variant == "of_form":
                mise = []
                for ing in doc.get("ingredients", []):
                    amt = ing.get("amount_text") or ing.get("amount")
                    unit = ing.get("unit")
                    name = ing.get("canonical") or ing.get("name")
                    if not (amt and unit and name):
                        continue
                    if _mass_volume(amt, unit) is None:
                        continue
                    mise.append(f"{amt} {unit} of {name}".strip())
                plan = {
                    "mise_en_place": mise,
                    "steps": steps,
                    "step_sources": step_sources,
                    "plating": "Serve hot.",
                }
                errors = plan_prose_quantity_errors(plan, doc)
                if not errors:
                    passes += 1
                elif len(failures) < 20:
                    failures.append((dataset_id, source_id, doc.get("title"), errors[0][:400]))
                continue
            # model_style, swap, descriptor_swap share model lines.
            model_entries: list[tuple[str, str]] = []
            for ing in doc.get("ingredients", []):
                line = _model_line(ing.get("original"))
                if not line or not prose_quantities(line):
                    continue
                name = str(ing.get("canonical") or ing.get("name") or "")
                model_entries.append((line, name))
            if not model_entries:
                continue
            if variant == "model_style":
                examined += 1
                plan = {
                    "mise_en_place": [line for line, _ in model_entries],
                    "steps": steps,
                    "step_sources": step_sources,
                    "plating": "Serve hot.",
                }
                errors = plan_prose_quantity_errors(plan, doc)
                if not errors:
                    passes += 1
                elif len(failures) < 20:
                    failures.append((dataset_id, source_id, doc.get("title"), errors[0][:400]))
                continue
            # Swap variants: one same-unit different-amount pair per recipe.
            lines = [line for line, _ in model_entries]
            names = [name for _, name in model_entries]
            pair = _swap_pair(lines, names)
            if pair is None:
                continue
            i, j, ci, cj, _ui, _uj = pair
            norm_lines = [_normalize_prose_text(line) for line in lines]

            # Skip when another line for the same ingredient states it.
            def _states_elsewhere(idx: int, value_unit: tuple[str, str]) -> bool:
                toks_idx = {t for t in _name_tokens(names[idx]) if len(t) >= 4}
                for k, (line_k, _name_k) in enumerate(model_entries):
                    if k == idx:
                        continue
                    toks_k = {t for t in _name_tokens(names[k]) if len(t) >= 4}
                    if not (toks_idx & toks_k):
                        continue
                    for _, v, u in prose_quantities(_normalize_prose_text(line_k)):
                        if (v, u) == value_unit:
                            return True
                return False

            # Values/units for the pair (normalized claims).
            norm_i = _normalize_prose_text(lines[i])
            norm_j = _normalize_prose_text(lines[j])
            amt_i = prose_quantities_with_spans(norm_i)
            amt_j = prose_quantities_with_spans(norm_j)
            if not amt_i or not amt_j:
                continue
            _cli, vi, ui, _si, _ei = amt_i[0]
            _clj, vj, uj, _sj, _ej = amt_j[0]
            if ui != uj or vi == vj:
                continue
            if _states_elsewhere(i, (vj, uj)) or _states_elsewhere(j, (vi, ui)):
                continue
            tried += 1
            new_i = norm_i.replace(ci, cj, 1)
            new_j = norm_j.replace(cj, ci, 1)
            if variant == "descriptor_swap":
                desc_j = _descriptor_of(names[j])
                if desc_j:
                    # Prefix swapped A with partner B's descriptor.
                    amt_new_i = prose_quantities_with_spans(new_i)
                    if amt_new_i:
                        _c, _v, _u, s, e = amt_new_i[0]
                        new_i = new_i[:e] + f" {desc_j}" + new_i[e:]
                desc_i = _descriptor_of(names[i])
                if desc_i:
                    amt_new_j = prose_quantities_with_spans(new_j)
                    if amt_new_j:
                        _c, _v, _u, s, e = amt_new_j[0]
                        new_j = new_j[:e] + f" {desc_i}" + new_j[e:]
            swapped = list(norm_lines)
            swapped[i] = new_i
            swapped[j] = new_j
            plan = {
                "mise_en_place": swapped,
                "steps": steps,
                "step_sources": step_sources,
                "plating": "Serve hot.",
            }
            errors = plan_prose_quantity_errors(plan, doc)
            if errors:
                caught += 1
            elif len(failures) < 20:
                failures.append(
                    (
                        dataset_id,
                        source_id,
                        doc.get("title"),
                        f"missed swap {lines[i]!r} <-> {lines[j]!r}",
                    )
                )
        if variant in ("swap", "descriptor_swap"):
            print(f"{variant}: TRIED {tried} CAUGHT {caught} MISSED {tried - caught}")
            for dataset_id, source_id, title, err in failures:
                print(f"MISS {dataset_id} {source_id} {title!r} :: {err}")
        else:
            total = examined if variant == "model_style" else len(rows)
            print(f"{variant}: TOTAL {total} PASSES {passes} FAILS {total - passes}")
            for dataset_id, source_id, title, err in failures:
                print(f"FAIL {dataset_id} {source_id} {title!r} :: {err}")


if __name__ == "__main__":
    main()
