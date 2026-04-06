---
name: conversation-to-skill
description: 'Create a reusable SKILL.md from chat history. Use when a user wants to turn a repeated workflow, checklist, or implementation pattern into an on-demand skill with clear steps, branching logic, and quality checks.'
argument-hint: 'Describe the workflow to capture, target scope (workspace or personal), and preferred format (quick checklist or full workflow).'
user-invocable: true
disable-model-invocation: false
---

# Conversation To Skill

## Outcome
Produce a complete skill bundle that includes SKILL.md and companion references:
- captures a repeatable workflow from conversation context
- includes decision points and branching logic
- includes completion criteria and quality checks
- is ready to invoke as a slash command
- includes at least one companion reference file for details the core skill should not inline

## When To Use
- user asks to create a new skill
- user has repeated a process across messages and wants reuse
- user wants a checklist or step-by-step workflow packaged as a skill

## Inputs To Collect
- desired outcome the skill should produce
- scope: workspace skill or personal skill
- format: quick checklist or full multi-step workflow
- any required tools, constraints, and quality bar

## Procedure
1. Read the conversation and extract a candidate workflow.
2. Identify:
- ordered steps
- decision points (if/then branches)
- quality gates (what defines done)
3. If no clear workflow exists, ask for missing inputs:
- target outcome
- scope (workspace or personal)
- format (checklist or full workflow)
4. Draft SKILL.md with:
- valid frontmatter (`name`, `description`, optional fields)
- clear triggers in `description` for discoverability
- actionable procedure and completion checks
5. Create companion files as needed, for example:
- `references/workflow-map.md` for expanded decision logic
- `references/quality-gates.md` for acceptance checks
- `assets/skill-template.md` for reusable skeletons
6. Save the draft skill bundle in the correct location.
7. Review ambiguity in the draft and ask focused follow-up questions.
8. Finalize the skill and provide example prompts to invoke it.

## Branching Logic
- If repeated steps are explicit in chat: preserve them as the core procedure.
- If steps are implied but incomplete: draft best-effort structure, then ask targeted clarifications.
- If scope is unspecified: default to personal scope and flag this assumption.
- If format is unspecified: default to full workflow and include an optional quick checklist section.

## Completion Checklist
- SKILL.md path and folder naming are valid
- `name` matches folder name
- `description` contains concrete trigger phrases
- body contains clear steps and decision points
- includes explicit done criteria
- includes companion reference file(s) for overflow detail
- includes 3-5 example prompts users can run

## Example Prompt Starters
- /conversation-to-skill Create a skill from this debugging workflow and save it as workspace-scoped.
- /conversation-to-skill Turn our PR review checklist into a reusable skill.
- /conversation-to-skill Convert this release process into a quick checklist skill.
- /conversation-to-skill Build a skill for triaging flaky tests with decision branches.

## Notes
- Keep SKILL.md concise and procedural.
- Move long references into companion files and link with relative paths when needed.

## Companion Files
- [Workflow Map](./references/workflow-map.md)
- [Quality Gates](./references/quality-gates.md)
- [Skill Template](./assets/skill-template.md)