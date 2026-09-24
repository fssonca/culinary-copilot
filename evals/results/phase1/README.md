# Phase 1 evaluation artifacts

The corrected full-text baseline and pre/post-rebuild fingerprints are versioned
here. The primary relevance cutoff is grade 2; grade >= 1 is sensitivity analysis.
Results use AI-assisted judgments and an owner-accepted evaluation policy, not
independent human verification or culinary validation. See the corrected report
for coverage, denominators and exposed-case limitations.

Recipe-filled packets, AI review transcripts, and local adjudication artifacts
remain ignored on disk. Their hashes and provenance are referenced by the frozen
manifests. Database backups live in ignored `backups/`.

The evaluation code, case definitions, rubric and offline tests are versioned.
A fresh checkout can run the offline tests, but cannot reproduce the historical
corpus measurement from Git alone: it also needs the matching local corpus and
the manifest-referenced judgments/packets. The scripts under
`scripts/retrieval_eval/` document their inputs. Do not substitute new judgments
or a new corpus while claiming reproduction of this historical baseline.
