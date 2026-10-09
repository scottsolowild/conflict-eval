# Sources

Checked against the primary pages on 2026-10-09. The README cites from here. A figure that is not on this page stays out of the README.

## Anthropic, "How people ask Claude for personal guidance" (2026-04-30)

https://www.anthropic.com/research/claude-personal-guidance

- An automated grader flagged sycophancy in 9 percent of all guidance-seeking conversations, and in 25 percent of relationship conversations. Relationship conversations have the most flagged chats by count. Spirituality has the highest rate, at 38 percent.
- The rate was 18 percent in conversations where the person pushed back, against 9 percent without pushback. The page reports this across guidance conversations and names no single domain, so the README cites it that way. People pushed back in 21 percent of relationship conversations, against 15 percent across other domains.
- The study names one-sided accounts as the hard case. Its example is Claude agreeing that the other party was in the wrong with only the user's account to go on. It also says pushback combined with hearing one side makes neutrality harder.
- Method: about 38,000 guidance conversations, graded by Claude Sonnet 4.5 through Clio, with a small subset checked by hand.

## Claude's constitution (no date shown on the page)

https://www.anthropic.com/constitution, section "Navigating helpfulness across principals"

- Claude keeps caring about the wellbeing of people in a conversation who are not its principal. The page's example: "considerate toward the other party in a negotiation scenario but without representing their interests."
- Under "What constitutes genuine helpfulness", concern for the user's wellbeing includes avoiding sycophancy.

## Microsoft AI, "Humanist AI Code of Conduct" (draft, 2026-09-14)

https://microsoft.ai/code-of-conduct/, announced at https://microsoft.ai/news/mai-code-of-conduct/

- The consultation runs six weeks from 2026-09-14. The page states no closing date, so the README says "six weeks from September 14" rather than a day.
- Sycophancy is a guideline under "Truthful feedback" in the wellbeing section, outside the absolute constraints. The README calls it a guideline, not a ban.
- The draft has no rule for a model that both people in a conflict use, and none on fairness to a person the user describes. The words "dispute", "mediation", and "both parties" do not appear.

## Petri

https://www.anthropic.com/research/petri-open-source-auditing (2025-10-06). The repo now lives at https://github.com/meridianlabs-ai/inspect_petri (MIT).

- A Petri seed is a prose instruction to an auditor model, which then drives the conversation. A fixed script of user turns is not a documented mode.
- These scenarios can be written as seeds that quote turn 1 and turn 2 word for word. The auditor sits between the script and the target, so the exact text, the fixed second turn, and a repeatable run are all lost. An Inspect task fits an exact two-turn replay better.
