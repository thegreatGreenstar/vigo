"""The GitHub work an answer names, said before the prose that describes it.

A report of GitHub work reads best the way this project's maintainers write it
on GitHub: the verdict on the first line, then the facts, each with its number.
The agent writes that, because only the agent knows what it did.  This module
adds the one part a machine does better: the artifacts themselves, in one line
at the top, in the order they were made, so a reader who reads one line knows
where the work is and can follow it.

Nothing here asks GitHub anything.  A project on a device may be private, the
device may have no `gh`, and a fetched link describes a state the work has left.
Only what the answer already names appears, so the line cannot claim more than
the answer does, and a link the answer puts first is not repeated below it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A link to the work itself.  A repository, a tree, a blob and a compare page
# name a place rather than work, so they stay in the prose and out of the line.
LINK = re.compile(
    r"https?://(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/"
    r"(?P<kind>pull|issues|discussions|commit)/(?P<what>[A-Za-z0-9]+)",
    re.I,
)

# `owner/repo#123`, the way a person writes it in a commit body or a comment.
REF = re.compile(
    r"(?<![\w/.-])(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)#(?P<what>\d+)"
)

# The word that says what an artifact is, before its number.  A reference of the
# `owner/repo#123` shape carries its repository instead, so it has no word.
WORDS = {
    "pull": "PR ",
    "issue": "issue ",
    "discussion": "discussion ",
    "commit": "commit ",
    "ref": "",
}

# How many artifacts one line holds.  Discord takes 2000 characters and a reader
# takes six; past that the count says how many were left, and the prose has them.
MOST = 6


@dataclass(frozen=True)
class Artifact:
    """One piece of work, as the answer names it."""

    kind: str
    key: str
    url: str

    @property
    def label(self) -> str:
        return WORDS[self.kind] + self.key

    def marked(self) -> str:
        """The artifact as a link that shows what it is: `[PR #1251](…)`."""
        return f"[{self.label}]({self.url})"


def _hits(said: str) -> list[tuple[int, Artifact]]:
    """Each artifact with where it starts, so the line keeps the order of the text."""
    found: list[tuple[int, Artifact]] = []
    for match in LINK.finditer(said):
        kind = match.group("kind").lower()
        what = match.group("what")
        if kind == "issues":
            kind = "issue"
        elif kind == "discussions":
            kind = "discussion"
        # A commit is named by seven characters, as git names it; a number is
        # what GitHub shows and what the person can search for.
        key = what[:7] if kind == "commit" else f"#{what}"
        found.append((match.start(), Artifact(kind, key, match.group(0))))
    for match in REF.finditer(said):
        where = match.group("owner") + "/" + match.group("repo")
        number = match.group("what")
        found.append((match.start(), Artifact(
            "ref", f"{where}#{number}",
            f"https://github.com/{where}/issues/{number}",
        )))
    return found


def _identity(artifact: Artifact) -> str:
    """What makes two mentions one piece of work.

    A number belongs to its repository, and the issues, pull requests and
    discussions of one repository share their numbering, so `#1251` in one
    repository is one thing however the text points at it.
    """
    return re.sub(r"/(?:pull|discussions)/", "/issues/", artifact.url).lower()


def artifacts(said: str) -> list[Artifact]:
    """The GitHub work this text names, in order, each piece once."""
    seen: set[str] = set()
    kept: list[Artifact] = []
    for _, artifact in sorted(_hits(said), key=lambda hit: hit[0]):
        identity = _identity(artifact)
        if identity in seen:
            continue
        seen.add(identity)
        kept.append(artifact)
    return kept


def _first_line(said: str) -> str:
    for line in said.split("\n"):
        if line.strip():
            return line.strip().lower()
    return ""


def _named(artifact: Artifact, line: str) -> bool:
    """Whether this line already names the artifact, in any of its forms.

    A line can hold the link, the short reference, or the bare number, and each
    of them names the work.  Where the number alone appears, the line is the
    author's own summary and the link is theirs to write.
    """
    if artifact.url.lower() in line or artifact.key.lower() in line:
        return True
    cut = artifact.key.rsplit("#", 1)
    return len(cut) == 2 and f"#{cut[1]}" in line


def mark(said: str, most: int = MOST) -> str:
    """The line that puts the work first, or "" when the answer already does.

    An artifact the answer names on its first line is left out, so the line is
    always an addition and never a repetition.  An answer whose first line holds
    all of them gets no line at all.
    """
    first = _first_line(said)
    rest = [artifact for artifact in artifacts(said) if not _named(artifact, first)]
    if not rest:
        return ""
    shown = " · ".join(artifact.marked() for artifact in rest[:most])
    if len(rest) > most:
        shown += f" · and {len(rest) - most} more"
    return f"GitHub: {shown}"
