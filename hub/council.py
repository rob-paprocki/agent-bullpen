"""Multi-agent workflows ("councils").

A council is a parent job that the hub moves through a list of stages. Each
stage creates ordinary task jobs for runners; when the last job of a stage
finishes, the hub builds the next stage's prompts from the earlier results.

Two modes:

  debate   answer   -> every participant answers the request on its own
           critique -> every participant attacks the other answers
           revise   -> (optional) every participant fixes its own answer
           judge    -> one agent weighs everything and writes the final answer

  review   build    -> the builder agent does the work in a project (may edit files)
           review   -> reviewer agents adversarially review the builder's diff
           fix      -> (optional) the builder checks each finding and fixes the valid ones

Participants are anonymised as "Response A/B/C" in every prompt, so a model
can't go easy on (or pile on) a particular vendor. The dashboard shows the
real mapping.
"""

from __future__ import annotations

import logging
import string
import time
from collections.abc import Callable
from typing import Any

from db import Database

MAX_ANSWER_CHARS = 60_000
MAX_DIFF_CHARS = 150_000

# How big a council can be. The dashboard reads these from /api/state, so the form and the hub
# refuse the same thing. Every participant and reviewer is a job on some machine, and each one's
# prompt carries the other answers (or the whole diff), so the cost grows quickly with size.
MAX_DEBATE_PARTICIPANTS = 12   # labelled Response A to Response L
MAX_REVIEWERS = 10
# All the other agents' text in one prompt (answers, critiques, reviews), shared out between them,
# so a big council can't build a prompt larger than an agent can read.
MAX_PROMPT_CHARS = 400_000

DEBATE_STAGES = ["answer", "critique", "revise", "judge"]
REVIEW_STAGES = ["build", "review", "fix"]


def _clip(text: str | None, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[... truncated {len(text) - limit} characters ...]"


def _block(title: str, body: str | None, limit: int = MAX_ANSWER_CHARS) -> str:
    return f"{title}:\n<<<\n{_clip(body, limit).strip() or '(empty)'}\n>>>\n"


def _share(count: int, limit: int = MAX_ANSWER_CHARS) -> int:
    """The limit for each of `count` blocks in one prompt: at most `limit`, at least 4,000 characters."""
    return max(4_000, min(limit, MAX_PROMPT_CHARS // max(1, count)))


def labels(n: int) -> list[str]:
    if n > len(string.ascii_uppercase):
        raise ValueError(f"at most {len(string.ascii_uppercase)} participants can be labelled")
    return [f"Response {string.ascii_uppercase[i]}" for i in range(n)]


# --------------------------------------------------------------------- prompts
READ_ONLY_NOTE = "Do not create, edit or delete any files in this step."


def prompt_answer(question: str) -> str:
    return (
        "You are one of several independent AI agents answering the same request. "
        "The other agents are different models or accounts working separately. Afterwards, "
        "every answer will be critiqued adversarially by the others, so be accurate, state "
        "your assumptions, show your reasoning where it matters, and say plainly when you are unsure.\n"
        f"{READ_ONLY_NOTE}\n\n" + _block("REQUEST", question)
    )


def prompt_critique(question: str, others: list[tuple[str, str]]) -> str:
    parts = [
        "You are an adversarial reviewer. Below is a request and the response(s) other AI agents "
        "wrote for it. Your job is to find what is wrong with them, not to be polite.\n\n"
        "For each response:\n"
        "1. List factual errors, bugs, logical gaps, risky or unsafe suggestions and missing "
        "considerations. Quote the part you are criticising.\n"
        "2. Mark each issue critical, major or minor.\n"
        "3. Note anything it got right.\n"
        "Finish with two or three sentences on which response is strongest and why.\n"
        "If you can check a claim yourself (by reading files in this workspace, searching, or "
        "running a harmless read-only command), do it and say that you checked. "
        "Don't invent problems: if a response is solid, say so.\n"
        f"{READ_ONLY_NOTE}\n\n",
        _block("REQUEST", question),
    ]
    each = _share(len(others))
    for label, text in others:
        parts.append("\n" + _block(label.upper(), text, each))
    return "".join(parts)


def prompt_revise(question: str, own_label: str, own_answer: str, critiques: list[str]) -> str:
    parts = [
        f"Earlier you answered the request below; your answer is called '{own_label}' in the "
        "critiques. Other AI agents have critiqued it. For each criticism of your answer, decide "
        "whether it is valid. Briefly say why you reject any you disagree with, then write your "
        "improved final answer in full.\n"
        f"{READ_ONLY_NOTE}\n\n",
        _block("REQUEST", question),
        "\n" + _block(f"YOUR ANSWER ({own_label})", own_answer),
    ]
    each = _share(len(critiques))
    for i, critique in enumerate(critiques, 1):
        parts.append("\n" + _block(f"CRITIQUE {i}", critique, each))
    return "".join(parts)


def prompt_judge(question: str, answers: list[tuple[str, str]], critiques: list[str],
                 revised: list[tuple[str, str]]) -> str:
    parts = [
        "You are the judge of a structured debate between AI agents. Below are the request, the "
        "competing responses (anonymised), the adversarial critiques"
        + (", and each agent's revised answer" if revised else "")
        + ". Check disputed claims yourself where you can.\n\n"
        "Write your ruling with exactly these sections:\n"
        "## Verdict\nWhich response is best (or 'none is adequate') and why, in a few sentences.\n"
        "## Where they agree\n"
        "## Where they disagree\nFor each disagreement: who is right, and why.\n"
        "## Final answer\nThe best possible answer to the request: keep the strongest parts, fix "
        "every error the critiques exposed, and say what is still uncertain.\n"
        f"Be decisive. {READ_ONLY_NOTE}\n\n",
        _block("REQUEST", question),
    ]
    each = _share(len(answers) + len(critiques) + len(revised), MAX_ANSWER_CHARS // 2)
    for label, text in answers:
        parts.append("\n" + _block(label.upper(), text, each))
    for i, critique in enumerate(critiques, 1):
        parts.append("\n" + _block(f"CRITIQUE {i}", critique, each))
    for label, text in revised:
        parts.append("\n" + _block(f"REVISED {label.upper()}", text, each))
    return "".join(parts)


def prompt_build(task: str) -> str:
    return (
        f"{task.strip()}\n\n"
        "When you are done, end your reply with a short section headed 'Summary' that lists what "
        "you changed, why, how you checked it, and anything you could not finish. Another AI "
        "agent will review your work adversarially."
    )


def prompt_review(task: str, summary: str, diff: str) -> str:
    return (
        "You are an adversarial code reviewer. Another AI agent was given the task below and "
        "made the changes in the diff. Find real problems: bugs, regressions, security issues, "
        "missed requirements, unhandled edge cases, and anything that won't work on the machine "
        "it runs on (it may be Windows). Don't nitpick style.\n"
        "For each finding give: severity (critical/major/minor), file and line, what is wrong, "
        "and a concrete fix. If you find nothing serious, say so plainly; don't invent issues.\n"
        f"{READ_ONLY_NOTE}\n\n"
        + _block("TASK", task)
        + "\n" + _block("BUILDER'S SUMMARY", summary)
        + "\n" + _block("DIFF", diff or "(the builder reported no file changes)", MAX_DIFF_CHARS)
    )


def prompt_fix(task: str, reviews: list[str]) -> str:
    parts = [
        "Other AI agents reviewed the changes you made for the task below. Their findings follow. "
        "Some findings may be wrong: check each one against the actual code before acting. Fix "
        "the valid ones, say briefly why you reject any you don't fix, and end with a 'Summary' "
        "section.\n\n",
        _block("TASK", task),
    ]
    each = _share(len(reviews))
    for i, review in enumerate(reviews, 1):
        parts.append("\n" + _block(f"REVIEW {i}", review, each))
    return "".join(parts)


# ------------------------------------------------------------------ the engine
log = logging.getLogger("hub.council")


class CouncilEngine:
    """`on_finish(council_id, status)` is called when a council finishes ("done" or "error"),
    not when it is cancelled. The hub sends its notification from there."""

    def __init__(self, db: Database, on_finish: Callable[[int, str], None] | None = None):
        self.db = db
        self.on_finish = on_finish

    # -- creation -------------------------------------------------------------
    def create(self, spec: dict[str, Any]) -> int:
        mode = spec["mode"]
        typed = (spec.get("title") or "").strip()
        title = typed or spec["prompt"].strip().splitlines()[0][:80]
        meta: dict[str, Any] = {
            "mode": mode,
            "prompt": spec["prompt"],
            "stage": None,
            "revise": bool(spec.get("revise")),
            "fix": bool(spec.get("fix", True)),
        }
        if mode == "debate":
            meta["participants"] = spec["participants"]
            meta["judge"] = spec["judge"]
            meta["labels"] = labels(len(spec["participants"]))
        elif mode == "review":
            meta["builder"] = spec["builder"]
            meta["reviewers"] = spec["reviewers"]
            meta["autonomy"] = spec.get("autonomy", "edits")
        else:
            raise ValueError(f"unknown council mode {mode!r}")

        council_id = self.db.create_job(
            kind="council", title=title, status="running", started_at=time.time(),
            prompt=spec["prompt"], meta=meta,
            # title_typed: the owner typed the title, so a notification may show it.
            options={"timeout_min": spec.get("timeout_min", 60), **({"title_typed": True} if typed else {})},
        )
        self._start_stage(council_id, meta, "answer" if mode == "debate" else "build")
        return council_id

    # -- progress -------------------------------------------------------------
    def advance(self, council_id: int) -> None:
        """Called whenever one of the council's steps finishes. Steps can finish at the same moment
        (in parallel requests), so the whole check-and-start runs under the database lock: otherwise
        two callers could both start the next stage, or one could see the next stage before its steps
        exist and stop the council with a false "every step failed"."""
        with self.db.lock:
            self._advance(council_id)

    def _advance(self, council_id: int) -> None:
        council = self.db.job(council_id)
        if not council or council["status"] != "running":
            return
        meta = council["meta"]
        stage = meta["stage"]
        steps = [c for c in self.db.children(council_id) if c["stage"] == stage]
        if any(s["status"] in ("queued", "running") for s in steps):
            return  # this stage is still going

        ok = [s for s in steps if s["status"] == "done"]
        if not ok:
            self._finish(council_id, "error", None,
                         f"Every step of the '{stage}' stage failed, so the council stopped.")
            return

        nxt = self._next_stage(meta)
        if nxt is None:
            self._finish(council_id, "done", self._final_result(council_id, meta), None)
        else:
            self._start_stage(council_id, meta, nxt)

    def cancel(self, council_id: int) -> None:
        with self.db.lock:
            self._cancel(council_id)

    def _cancel(self, council_id: int) -> None:
        for child in self.db.children(council_id):
            if child["status"] == "queued":
                self.db.update_job(child["id"], status="cancelled", finished_at=time.time())
            elif child["status"] == "running":
                self.db.request_cancel(child["id"])  # also revokes its job tools at once
        self._finish(council_id, "cancelled", None, "Cancelled from the dashboard.")

    # -- internals ------------------------------------------------------------
    def _next_stage(self, meta: dict) -> str | None:
        order = DEBATE_STAGES if meta["mode"] == "debate" else REVIEW_STAGES
        i = order.index(meta["stage"]) + 1
        while i < len(order):
            stage = order[i]
            if stage == "revise" and not meta.get("revise"):
                i += 1
                continue
            if stage == "fix" and not meta.get("fix"):
                i += 1
                continue
            return stage
        return None

    def _finish(self, council_id: int, status: str, result: str | None, error: str | None) -> None:
        self.db.update_job(council_id, status=status, result=result, error=error,
                           finished_at=time.time())
        if status in ("done", "error") and self.on_finish is not None:
            try:
                self.on_finish(council_id, status)
            except Exception as err:  # noqa: BLE001 - a notification problem must never stop a council
                log.warning("council %s finished, but on_finish failed: %s", council_id, err)

    def _results(self, council_id: int, stage: str) -> list[dict]:
        return [c for c in self.db.children(council_id) if c["stage"] == stage]

    def _step(self, council_id: int, who: dict, stage: str, prompt: str, label: str,
              autonomy: str = "readonly", **extra: Any) -> None:
        """Queue one step for a seat: a machine by name, or (a tag seat) the first free machine
        with the accepted tag, the agent on the seat's account key, and what the step needs."""
        council = self.db.job(council_id)
        options = {"timeout_min": (council["options"] or {}).get("timeout_min", 60)}
        options.update(extra.pop("options", {}))
        runner = who.get("runner") or None
        tag = None if runner else who.get("tag")
        if autonomy == "ask":
            options["needs"] = ["approvals"]
        if tag:
            options["account"] = who.get("account")
        self.db.create_job(
            kind="task", parent_id=council_id, stage=stage, label=label,
            title=f"{stage}: {label}", runner=runner, target_tag=tag, agent=who["agent"],
            project=who.get("project") or "chat", autonomy=autonomy, prompt=prompt,
            model=who.get("model"),
            options=options, **extra,
        )

    def _start_stage(self, council_id: int, meta: dict, stage: str) -> None:
        meta["stage"] = stage
        self.db.update_job(council_id, meta=meta)
        question = meta["prompt"]

        if stage == "answer":
            for who, label in zip(meta["participants"], meta["labels"]):
                self._step(council_id, who, stage, prompt_answer(question), label)

        elif stage == "critique":
            answers = {s["label"]: s["result"] for s in self._results(council_id, "answer")
                       if s["status"] == "done"}
            for who, label in zip(meta["participants"], meta["labels"]):
                others = [(lbl, txt) for lbl, txt in answers.items() if lbl != label]
                if not others:
                    continue
                self._step(council_id, who, stage, prompt_critique(question, others),
                           f"{label} critiques")

        elif stage == "revise":
            answers = {s["label"]: s["result"] for s in self._results(council_id, "answer")
                       if s["status"] == "done"}
            critiques = [s for s in self._results(council_id, "critique") if s["status"] == "done"]
            for who, label in zip(meta["participants"], meta["labels"]):
                if label not in answers:
                    continue
                about_me = [c["result"] for c in critiques if not c["label"].startswith(label + " ")]
                self._step(council_id, who, stage,
                           prompt_revise(question, label, answers[label], about_me),
                           f"{label} revised")

        elif stage == "judge":
            answers = [(s["label"], s["result"]) for s in self._results(council_id, "answer")
                       if s["status"] == "done"]
            critiques = [s["result"] for s in self._results(council_id, "critique")
                         if s["status"] == "done"]
            revised = [(s["label"].replace(" revised", ""), s["result"])
                       for s in self._results(council_id, "revise") if s["status"] == "done"]
            self._step(council_id, meta["judge"], stage,
                       prompt_judge(question, answers, critiques, revised), "Judge")

        elif stage == "build":
            self._step(council_id, meta["builder"], stage, prompt_build(question), "Builder",
                       autonomy=meta.get("autonomy", "edits"),
                       options={"capture_diff": True})

        elif stage == "review":
            build = self._results(council_id, "build")[-1]
            for i, who in enumerate(meta["reviewers"], 1):
                who = dict(who)
                # A reviewer on the machine that built (the one that took the build step, also when
                # the builder's seat was a tag) can also open the real files.
                if who.get("runner") and who.get("runner") == build["runner"] and not who.get("project"):
                    who["project"] = meta["builder"].get("project")
                self._step(council_id, who, stage,
                           prompt_review(question, build["result"], build.get("diff") or ""),
                           f"Reviewer {i}")

        elif stage == "fix":
            build = self._results(council_id, "build")[-1]
            reviews = [s["result"] for s in self._results(council_id, "review") if s["status"] == "done"]
            # The session to continue lives on the machine that built, with the agent that actually
            # built (the machine's credits agent, when the build ran on platform credits): the fix
            # goes there by name, to that agent.
            builder = {**meta["builder"], "runner": build["runner"], "tag": None, "agent": build["agent"]}
            moved = {"named_agent": build["named_agent"], "credits": build["credits"]} \
                if build.get("named_agent") else {}
            self._step(council_id, builder, stage, prompt_fix(question, reviews),
                       "Builder (fixes)", autonomy=meta.get("autonomy", "edits"),
                       resume_session=build.get("session_id"),
                       options={"capture_diff": True}, **moved)

    @staticmethod
    def _where(step: dict | None, seat: dict | None = None) -> str:
        """`alpha`: the machine a step ran on, or its seat's machine; `any machine tagged gpu` for a
        tag seat whose step no machine took."""
        runner = (step or {}).get("runner") or (seat or {}).get("runner")
        if runner:
            return f"`{runner}`"
        tag = (step or {}).get("target_tag") or (seat or {}).get("tag")
        return f"`any machine tagged {tag}`" if tag else "`?`"

    @staticmethod
    def _on_credits(step: dict | None) -> str:
        """' (ran on platform credits: `claude-api`)' for a step that ran on its machine's credits agent."""
        if not step or not step.get("named_agent"):
            return ""
        return f" (ran on platform credits: `{step.get('agent')}`)"

    def _final_result(self, council_id: int, meta: dict) -> str:
        if meta["mode"] == "debate":
            judge = [s for s in self._results(council_id, "judge") if s["status"] == "done"]
            answered = {s["label"]: s for s in self._results(council_id, "answer")}
            mapping = "\n".join(
                f"- **{label}** = `{p['agent']}`{' (' + p['model'] + ')' if p.get('model') else ''} on "
                f"{self._where(answered.get(label), p)}{self._on_credits(answered.get(label))}"
                for p, label in zip(meta["participants"], meta["labels"])
            )
            body = judge[-1]["result"] if judge else "(the judge step failed; see the steps below)"
            return f"{body}\n\n---\n**Who was who**\n{mapping}\n"

        out = []
        for stage, heading in (("build", "Builder's summary"), ("review", "Review findings"),
                               ("fix", "Builder's response to the reviews")):
            for step in self._results(council_id, stage):
                if step["status"] == "done":
                    out.append(f"## {heading} — {step['label']} (`{step['agent']}` on {self._where(step)}"
                               f"{', on platform credits' if step.get('named_agent') else ''})\n\n"
                               f"{(step['result'] or '').strip()}\n")
        return "\n".join(out) or "(no results)"
