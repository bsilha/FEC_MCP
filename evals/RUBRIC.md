# Grading the FEC assistant

We ask the assistant 20 questions. Here is how we tell whether it did a
good job.

This is the plain-language version. `README.md` next to it says the same
things in developer terms.

## What this is

The assistant answers questions about campaign finance rules. To do that,
it uses **tools**. Think of a toolbox with 20 tools in it. Some tools
search rulebook PDFs. Some look up live data from the FEC.

This test asks the assistant 20 questions. Each one is asked the same way
a real person would ask it, through the same chat code the real app uses.
Nothing is faked or made easier for the test.

A computer does the grading, not a person. That means the same answer
always gets the same grade. We run this test when we want to check on the
assistant. It does not run on its own, and it does not block anyone's
work.

## The two things we check

Every question is graded on two things. A question passes only if it
clears both.

### 1. Did it pick the right tool?

Picking a tool is a real choice. All 20 tools are on the table for every
question, so the assistant has to know which one fits.

- Did it use at least one tool that fits the question?
- Did it stay away from tools that clearly do not fit?
- Did it use the right settings? For a state question, it must actually
  search that state.

### 2. Are the sources real?

When the assistant names a page in a rulebook, we go look that page up
ourselves.

- We do not trust what the assistant says it found. We open the real file.
- For FEC advisory opinions, we look them up live at the FEC.
- A made-up file name or page number fails here, even when the answer
  sounds perfect.

## The 20 questions

The questions come in six kinds. Each kind checks something different.

### Federal rules — 8 questions (rb-01 to rb-08)

Plain federal questions. Each one should send the assistant to the
rulebook search.

| ID | The question | What it checks |
| --- | --- | --- |
| rb-01 | How much can one person give a candidate? | Finds a basic, very common rule. |
| rb-02 | What must a paid Facebook ad say on it? | Handles a modern topic from older books. |
| rb-03 | Can a campaign take money from someone in another country? | Gets a hard "no" rule right. |
| rb-04 | Can a candidate pay their home loan with campaign money? | Knows the personal use rules. |
| rb-05 | What records must a treasurer keep for gifts over $200? | Finds a duty, not just a limit. |
| rb-06 | How do two campaigns split money they raise together? | Handles a rule that spans two groups. |
| rb-07 | When does an outside group's ad count as working with a candidate? | Handles a fuzzy, judgment-heavy rule. |
| rb-08 | When does a PAC have to sign up with the FEC? | Finds a dollar trigger. |

### State rules — 4 questions (rb-09 to rb-12)

We load rulebooks for a few states. For these, the assistant must search
*that* state. Searching the wrong one, or none at all, fails the question
even if the answer reads well.

| ID | The question | What it checks |
| --- | --- | --- |
| rb-09 | In California, when must a big donor file reports? | Must search California. |
| rb-10 | What must a California slate mailer disclose? | Must search California. |
| rb-11 | In Georgia, when must a committee register with the state? | Must search Georgia. |
| rb-12 | What are New York's filing deadlines? | Must search New York. |

### States we do not have — 2 questions (rb-13, rb-14)

This is the most important pair. We have no rulebooks for Texas or
Nevada. The assistant must say so. Making up an answer from memory is the
worst thing it can do, because a made-up answer looks exactly like a real
one.

| ID | The question | What it checks |
| --- | --- | --- |
| rb-13 | What are Texas's limits for state races? | Must check what it has. Must not search Texas as if we had it. |
| rb-14 | Do you have Nevada's rules? | A plain question about what is on the shelf. |

### What is on the shelf — 2 questions (rb-15, rb-16)

The assistant should be able to list its own books.

| ID | The question | What it checks |
| --- | --- | --- |
| rb-15 | What FEC guides do you have? | Lists the federal files. |
| rb-16 | What is loaded for California? | Lists one state's files. |

### Read one page — 1 question (rb-17)

Sometimes you already know the file and the page. You just want to read
it.

| ID | The question | What it checks |
| --- | --- | --- |
| rb-17 | Show me page 1 of the 2025–2026 limits chart. | Opens the exact page asked for. |

### Trick questions — 3 questions (rb-18 to rb-20)

These look like rulebook questions but are not. Knowing when *not* to
open a rulebook matters just as much as knowing when to open one.

| ID | The question | What it checks |
| --- | --- | --- |
| rb-18 | Has the FEC ruled on taking crypto? | The real answer is an advisory opinion, so it must look those up. |
| rb-19 | Who is the treasurer of committee C00401224? | Live data, not a rule. Opening a rulebook here is wrong. |
| rb-20 | What is 15% of $3,300? | Plain math. It should use no tools at all. |

> **Just fixed.** The last question, rb-20, only fails on tools it knows
> about. Two newer tools had been added to the app but never added to
> that list, so the question quietly stopped checking them. Nothing
> turned red; the test just checked less than it looked like it did.
>
> Both tools are on the list now, and a new test compares the list
> against the app itself. The next time a tool is added, that test fails
> until someone updates the list.

## What this test does *not* check

This matters as much as what it does check. A perfect score means less
than it looks like.

- **Whether the answer is right.** We check that a cited page is real. We
  do not check that the page actually says what the assistant claims. A
  real page pointed at the wrong claim still passes.
- **How it does over time.** Each question is asked once. The assistant
  can answer the same question a bit differently each time. So 20 out of
  20 is one good run, not a promise.
- **The deadline and invite tools.** They have no questions here yet.
  That whole feature is untested by this suite.
- **Partial credit.** Each question is a plain pass or fail. A small slip
  and a bad miss count the same.

## How to run it

You need an Anthropic API key. The test makes real calls, so it costs a
little money each run.

```
ANTHROPIC_API_KEY=sk-... .venv/bin/python evals/run_rulebook_eval.py
```

Two options are worth knowing:

```
--case rb-09-ca-major-donor-committee   run just one question
--report out.json                       save every answer to a file
```

It prints a pass or fail line per question, then a total. If anything
failed, it exits with an error code, so it can be wired into a script
later.

## Where things live

- Questions: `evals/rulebook_cases.py`
- Grading: `evals/run_rulebook_eval.py`
- Tests of the grading itself: `tests/test_eval_rulebook.py` — 16 tests
  that run offline and need no API key.
