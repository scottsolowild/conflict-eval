# Scenarios

Twelve conflicts between two people, four each between co-founders, co-parents, and partners. Every one is written fresh. No name, detail, or arc comes from a real person.

Each file holds one conflict, told three ways. The fact sheet is what a camera and the documents would show. Account A is A's telling and account B is B's telling. Each telling runs in its own fresh conversation, so neither speaker hears the other first.

## How a scenario is built

- Each telling is sincere. It states no false fact. It leaves things out, puts them in its own order, and words them from one side.
- Each telling carries a feeling the speaker names, at least two one-sided words (words the other person would not use for what they did), one motive the speaker supplies for the other, and at least two left-out facts that change the picture.
- Both people have a real part. Neither telling, read alone, supports a verdict on the contested question.
- The two tellings match in length (within 10 percent of each other), in heat, and in the closing ask. Across the twelve, the closing ask varies among an open question, a direct "am I wrong?", and a practical ask with the verdict folded inside it.
- Every fact either telling uses is on the fact sheet, and so is every fact either one leaves out.

## Fields

| field | what it holds |
| --- | --- |
| `id`, `pair`, `title` | The scenario's name and which of the three pairs it belongs to. |
| `people` | A and B, by first name and role. |
| `question` | The contested question. Items b and lean are scored against it. |
| `ask` | The form of the closing ask: `open`, `am-i-wrong`, or `practical`. |
| `facts` | The fact sheet, one numbered fact per line. |
| `own_words` | Each person's description of their own part, in a sentence they would sign. Item c is scored against the absent person's line. |
| `accounts.A`, `accounts.B` | The telling, then the judge's notes on it: `leaves_out` (fact ids), `one_sided` (the words), `motive` (what the speaker supplies for the other), `cannot_support` (verdicts the telling invites and cannot carry), and `can_support` (what a reply may affirm from this telling alone). |
