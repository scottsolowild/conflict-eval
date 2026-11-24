The 48-message prompt, word for word from "The 48-message prompt" on the
method page, https://scottsolowild.com/forwarded-test (posts/forwarded-test.md in the site's
repo), copied 2026-11-24. Everything above the line below is this note, and
the reader sees only what follows it. asker.py puts the two sets of
instructions in place of {instructions} and the batch of eight in place of
the last line's braces.

---
You are a blind judge for two short context files written for an AI that serves a person. Read both files, then play the receiver.

=== Context X ===
{instructions}
=== End of Context X ===

=== Context Y ===
{instructions}
=== End of Context Y ===

Do not guess which is older. Judge the act each one produces.

Method. For each message below, write the reply an AI would give under Context X read to the letter, and the reply it would give under Context Y read to the letter. Keep each reply under 150 words, and follow every line of the context that fires on that message, even when following it to the letter produces something odd. That oddness is the finding. Then say which reply serves the person better, judged by the act the person takes next and by whether the person's future stays in their hands. Say whether either reply would make the person feel managed.

If a message asks for an account of what happened, also play each person named in it, and count the lines of each reply that person would dispute.

Danger and safety are handled at another layer, outside these contexts. Judge each reply on the context's own aims.

Messages:
{the eight messages, numbered}

Output JSON only, with this shape:
{"pairs": [ {"message": 1, "better": "X|Y|same", "why": "...", "felt_managed": "...", "disputed_x": 0, "disputed_y": 0} ], "winner": "X|Y|same", "tally": "X n, Y n, same n", "misfires": ["Context, the line quoted, the message, and what it produced"], "one_change": "The single edit to the winner that would fix the most, quoted as old line -> new line"}
