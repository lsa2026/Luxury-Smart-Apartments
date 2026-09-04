"""Turn a raw Hostaway review body into the parts a guest should read.

Hostaway concatenates the channel's structured answers into one string:

    An optional headline
    Positive: what the guest liked
    Negative: what they did not

Those two English markers reach an Arabic page unchanged, and a guest who had
nothing negative to say often types a placeholder such as "....." which then
renders as an empty complaint.

Nothing here edits stored data. The review row keeps exactly what the channel
sent; this is a reading of it, applied at display time.
"""

import re
from dataclasses import dataclass
from typing import Final

# A section needs this many letters or digits before it is worth showing. Two
# keeps a terse "ok" while dropping "...", "-- --" and "     ".
MIN_MEANINGFUL_CHARACTERS: Final = 2

_SECTION = re.compile(r"^\s*(positive|negative)\s*[:：]\s*", re.IGNORECASE)
_MEANINGFUL = re.compile(r"[^\W_]", re.UNICODE)


@dataclass(frozen=True)
class ReviewBody:
    headline: str
    positive: str
    negative: str

    @property
    def has_sections(self) -> bool:
        return bool(self.positive or self.negative)

    @property
    def is_empty(self) -> bool:
        return not (self.headline or self.positive or self.negative)


def is_meaningful(text: str) -> bool:
    """True when a fragment carries content rather than punctuation or spacing."""
    return len(_MEANINGFUL.findall(text or "")) >= MIN_MEANINGFUL_CHARACTERS


def parse_review_body(raw: str) -> ReviewBody:
    """Split a review into its headline and its two labelled sections."""
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return ReviewBody("", "", "")

    current = "headline"
    buckets: dict[str, list[str]] = {"headline": [], "positive": [], "negative": []}

    for line in text.split("\n"):
        match = _SECTION.match(line)
        if match:
            current = match.group(1).lower()
            remainder = line[match.end() :]
            if remainder.strip():
                buckets[current].append(remainder)
            continue
        buckets[current].append(line)

    def collect(name: str) -> str:
        value = "\n".join(buckets[name]).strip()
        # A section of dots or dashes is dropped rather than shown as an empty
        # complaint; the underlying row is untouched.
        return value if is_meaningful(value) else ""

    return ReviewBody(
        headline=collect("headline"),
        positive=collect("positive"),
        negative=collect("negative"),
    )
