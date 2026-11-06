"""When an ask is a task, give the device a goal to work toward.

A goal is durable: the device keeps it, works toward it across turns, and says
it is met (`/goal <objective>` in the agent, and `opencode.goal` behind it).
A question is not a goal. "Why did CI fail?" wants an answer now, and made into
an objective it becomes work the agent keeps doing after the answer is given.
So the wording decides, with no option to say which it is:

- a request is a task: "can you fix the tests", "please rename this"
- a question is not: anything that ends with "?", or opens with a question word

The rule is deliberately small and dull: a clever guess would be wrong in ways
the person cannot see, and a goal the agent then chases is not a small mistake.
"""

from __future__ import annotations

# Two words that open a request.  They win over the "?" and over `QUESTIONS`:
# "can you fix this?" is work, not a question, and the "?" is only politeness.
REQUESTS = {
    ("can", "you"), ("can", "u"), ("could", "you"), ("could", "u"),
    ("would", "you"), ("will", "you"), ("do", "you"),
}

# One word that opens a request the same way.
OPENERS = {"please", "go", "lets", "let's"}

# Words that open a question.  "can", "could" and "will" are not here, because
# a request carries them and `REQUESTS` has already answered for those.
QUESTIONS = {
    "what", "why", "how", "who", "whom", "whose", "when", "where", "which",
    "is", "are", "was", "were", "am", "do", "does", "did", "shall",
    "may", "might", "should", "have", "has", "had",
}


def is_task(text: str) -> bool:
    """Whether this ask is work to be done rather than a question to answer."""
    said = (text or "").strip()
    if not said:
        return False
    words = [word.strip(".,:;!\"'()[]") for word in said.lower().split()]
    words = [word for word in words if word]
    if not words:
        return False
    if len(words) > 1 and tuple(words[:2]) in REQUESTS:
        return True
    if words[0] in OPENERS:
        return True
    if said.endswith("?"):
        return False
    return words[0] not in QUESTIONS
