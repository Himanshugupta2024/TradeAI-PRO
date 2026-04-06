# Workflow Map

Use this map when converting conversation context into a reusable skill.

## Extraction Flow
1. Scan conversation for repeated steps.
2. Convert repeated steps into ordered procedure items.
3. Identify decision points where path changes.
4. Attach completion checks to each major phase.
5. Separate long details into references.

## Decision Branches
- Clear workflow exists:
  - Convert directly into procedure and preserve sequence.
- Partial workflow exists:
  - Draft best-effort flow, mark assumptions, ask targeted clarifications.
- No workflow exists:
  - Ask for outcome, scope, and default format before finalizing.

## Scope Defaults
- Default scope: personal
- Override to workspace if explicitly requested

## Finalization Gate
- `name` and folder match
- Trigger-rich `description`
- Procedure includes branches and quality gates
- Companion references linked from SKILL.md
