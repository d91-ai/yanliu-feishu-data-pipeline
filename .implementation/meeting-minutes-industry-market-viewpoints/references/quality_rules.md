# Industry and Market Viewpoint Quality Rules

Use accuracy and readability as the two quality dimensions. Apply these rules
before rendering the review Markdown or artifact JSON.

## Accuracy

### Coverage

- Read the complete source before drafting claims.
- Inventory eligible market and industry judgments before selecting claims.
- Preserve distinct speakers and materially different judgments.
- Recheck the complete source after drafting to detect omitted eligible claims.

### Source representation

- Before compressing, identify the primary object, expected outcome, material
  time scope, material conditions or limitations, and supporting fragments.
- Render the viewpoint only after this source representation is complete.
- Require source support for every factual, directional, temporal, and causal
  element retained in the viewpoint.

### Scope

- Keep each claim focused on a market, industry, sector, industrial chain, or
  cross-company theme.
- Exclude facts without judgment, directionless event watches, meeting process,
  and judgments whose only object is one security.
- Do not infer a broader market or industry conclusion from a collection of
  security-level statements unless the source states that broader conclusion.
- Keep one primary judgment object in each claim. Supporting context may remain
  only when it directly explains that object and does not introduce an
  independent conclusion.
- Route independently supported market and industry conclusions to their
  corresponding scopes. Remove secondary cross-scope content that cannot stand
  as an eligible claim.
- Keep event-related content only when the source derives a directional market
  or industry judgment from it. Exclude unresolved outcome lists and
  directionless event or flow observations.

### Subject normalization

- Identify the substantive object before naming the subject.
- Prefer a conventional, stable, reusable market or industry label.
- Remove event, time, direction, conclusion, catalyst, and trading-action
  language from the subject.
- Reuse the same subject for the same substantive concept.
- Separate subjects only when their substantive objects differ.
- Create a new subject only when existing labels would cause material semantic
  loss and the new label can be reused beyond the current statement.
- Do not force distinct objects into a broad parent label merely to reduce the
  number of subjects.

### Direction and modality

- Classify direction from the expected outcome, not from the strength of modal
  wording.
- Use bullish for a positive expected outcome, bearish for a negative expected
  outcome, and neutral only when direction is unresolved or materially
  balanced.
- Probability and uncertainty normally affect wording rather than direction.
- Compression may simplify non-material modality, but must not convert an
  expectation into an observed fact, a possibility into a certainty, or one
  time scope into another.
- Retain any condition whose removal would materially change the object,
  direction, time scope, or conclusion.
- Retain a limitation when it materially changes the status, maturity,
  applicability, timing, or practical meaning of the judgment. Non-material
  uncertainty may be compressed without changing direction.

### Merge and split

- Prefer merging source-adjacent judgments from the same speaker when they have
  the same subject and direction and jointly form one complete rationale.
- Keep judgments separate when their object, direction, material time scope,
  speaker, or reasoning is different.
- Do not merge distant passages solely because their subjects are similar.
- Do not split one complete rationale into fragments that lack independent
  information.
- Split parallel conclusions when they have independently meaningful objects.
  Do not split supporting reasons that only explain the primary conclusion.

## Readability

### Standalone meaning

- State the viewpoint object explicitly.
- Use complete sentences that remain understandable without surrounding source
  text.
- Resolve references whose antecedents are outside the card.
- Remove transitions that depend on omitted preceding text.

### Information design

- Lead with the core judgment.
- Retain only the most useful reason, time boundary, or limitation needed to
  understand that judgment.
- Remove verbal filler, meeting process, redundant reasoning, and secondary
  detail.
- Avoid vague conclusions, unexplained abbreviations, excessive jargon, and
  text that is too compressed to convey useful information.
- Keep each viewpoint to one or two concise sentences.
- Preserve an explicit eligible direction even when the source provides little
  rationale. Keep the result concise and do not invent supporting reasons.

## Final review

Before output, verify that:

- all material eligible judgments were considered;
- each subject matches its viewpoint object and is reusable;
- each direction matches the source's expected outcome;
- each claim has one primary object and remains in the correct scope;
- no claim materially expands or changes the source conclusion;
- material conditions and limitations remain represented;
- event-only and unsupported secondary conclusions are excluded;
- merging and splitting preserve meaningful distinctions without fragmentation;
- every viewpoint is independently understandable and information-complete.
