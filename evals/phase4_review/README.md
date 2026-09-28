# Phase 4 owner review (streaming)

The owner checked that streamed progress is understandable and that
nothing before `final` can be mistaken for the recommendation.

- `sse_transcript.txt`: the exact transcript the owner reviewed. It has
  three streams: success, a validation rejection, and a mid-stream edit.
- `capture_sse.py`: regenerates an equivalent transcript offline. Group
  IDs are random, so the bytes differ.
- `owner_review.json`: the answers, their attribution, and the open
  finding P4-REV-01.

The review uses an offline transcript because curl shows only a 503
while generation is disabled (see the
[walkthrough](../../docs/phase4-streaming-walkthrough.md)).
