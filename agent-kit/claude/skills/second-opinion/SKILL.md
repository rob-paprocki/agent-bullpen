---
name: second-opinion
description: Get an independent, adversarial second opinion from a different model (Gemini through the gemini CLI) on a plan, an answer or a code change, then weigh it. Use when the user asks for a second opinion, a cross-check, "ask Gemini", or a review by another model.
---

# Second opinion

Have a different model attack the work, then judge its points yourself. The
other model only reads; it never edits files.

1. **Collect the material.** For a code change, capture `git diff` and
   `git diff --staged` (and name any new files). For a plan or an answer, use
   the text itself. Keep it under about 200 KB; summarise anything larger.

2. **Write a request file** in the system temp folder (not in the project),
   starting with:

   > You are an adversarial reviewer. Find real problems in the material below:
   > bugs, wrong facts, security issues, missed requirements, edge cases, and
   > anything that won't work on the target machine. Give each finding a
   > severity (critical / major / minor), where it is, and a concrete fix. If it
   > is solid, say so plainly; don't invent issues. Do not modify any files.

   then the user's goal in one or two sentences, then the material.

3. **Ask Gemini**, from the project folder, with the request on stdin:

   ```bash
   gemini --approval-mode default --output-format text < "$REQUEST_FILE"
   ```

   In PowerShell: `Get-Content $RequestFile -Raw | gemini --approval-mode default --output-format text`

   If the user asks for another of their Claude accounts instead, use that
   account's own command. The setup makes one for each extra account, named
   after its agent, for example
   `claude-work -p --permission-mode dontAsk < "$REQUEST_FILE"`.
   If you don't know which account they mean, ask.

4. **Weigh it.** Check each finding against the actual code or facts. Report
   to the user: the findings you agree with, the ones you reject and why, and
   what you would change. Don't apply fixes unless the user already asked you to.

5. Delete the request file.

<!-- managed-by: bullpen -->
