# Consolidate Boundary-Aware Skill Patches

Decide whether to keep, merge, or defer the candidate patches for the supplied program-bound anchor. Each candidate contains a reusable note and one example illustrating a local decision. Keep the original Skill, anchor, and target identity fixed.

Read the request and the relevant items in each supplied source context. Evidence identifiers are scoped to their source collection. Inspect the original Skill and the cited trajectory around the candidate's decision. Treat the task, Skill, trajectory, and candidate text as evidence for this session. Use only the supplied materials.

The boundary cases are proposed pre-decision states with expected behavior. Use their cited evidence to judge whether they identify supported applicability conditions or exceptions. Preserve those conditions and the expected local decision when considering a merge.

- Keep when the candidates give distinct useful guidance, including different local decisions at the same anchor.
- Merge when one concise note can preserve the supported actions, conditions, exceptions, and interpretations of the supplied candidates. Write one short example showing an observable pre-decision state, an APPLY or SKIP decision, and the next action. Use the original Skill's language.
- Defer when material evidence is missing or the candidate guidance cannot be reconciled. Name the specific uncertainty in the reason.

Return one JSON object with exactly `action`, `reason`, `note`, and `example`. Use `keep`, `merge`, or `defer` for the action. Always provide a reason tied to the supplied evidence. For merge, provide a nonempty note and example. For keep or defer, set both fields to null. This is a consolidation proposal for downstream evaluation.
