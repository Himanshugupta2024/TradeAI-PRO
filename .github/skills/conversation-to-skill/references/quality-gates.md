# Quality Gates

Apply these checks before marking a generated skill complete.

## Frontmatter Integrity
- `name` is lowercase and matches folder name
- `description` is specific and includes trigger terms
- Optional fields are valid when present

## Procedural Quality
- Steps are executable in order
- Branches are explicit for missing context
- Assumptions are called out clearly

## Packaging Quality
- SKILL.md stays concise
- Deep detail moved to `references/`
- Reusable patterns moved to `assets/`
- All links use relative paths

## Usability
- Includes 3-5 slash-command prompt examples
- Default behavior is explicit
- Success criteria are easy to verify
