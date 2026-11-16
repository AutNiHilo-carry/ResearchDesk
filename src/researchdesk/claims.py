"""Conservative, source-anchored clause candidates; no model paraphrasing."""
import re
from .verification import CITATION, VerificationError

POLICY_VERSION = "clause-spans-v1"
V2 = "clause-spans-v2"
V3 = "clause-spans-v3"
TEMPORAL_ONLY = re.compile(r"只有[^，,；;。！？!?]*?(?:时|后|前)(?=[，,；;。！？!?]|$)")
# A deliberately narrow exception, not a general Chinese syntax parser.
SINGLE_RESOURCE = re.compile(r"只有\s*(?:一个|1个)\s*(?:WAL\s*文件|写入者|写者|读者|连接|线程|进程|副本)(?!时|后|情况下)", re.I)
CONDITIONAL_SUFFIX = re.compile(r"才|方可|就|时候|情况下")
PROTECTED = re.compile(
    r"https?://[^\s，。；！？<>]+|`[^`]*`|\[[^\]]*\]\([^)]*\)|" + CITATION.pattern)
CONDITION = re.compile(r"如果|若|仅当|只有|只要|当.+时|除非|否则|\b(?:if|unless|provided|only when)\b", re.I)
PAIRS = {"(": ")", "（": "）", "[": "]", "{": "}", "“": "”", "‘": "’", "「": "」", "『": "』"}
SEPARATORS = frozenset("；;。！？!?")


def _meaningful(text):
    return bool(re.search(r"[^\s#>*_`~\-。；！？;!?：:|]", CITATION.sub("", text)))


def partitions(text, *, policy_version=POLICY_VERSION):
    if policy_version not in {POLICY_VERSION, V2, V3}:
        raise VerificationError("Unsupported clause policy version.")
    condition_text = text
    if (policy_version in {V2, V3} and not CONDITIONAL_SUFFIX.search(text)
            and not (policy_version == V3 and TEMPORAL_ONLY.search(text))):
        # Mask only the recognized quantity prefix for the condition detector.
        # All returned spans still point into the untouched source text.
        condition_text = SINGLE_RESOURCE.sub(lambda m: " " * len(m[0]), text)
    # Keep conditional scope intact rather than guessing whether a dependency can be split.
    if CONDITION.search(condition_text):
        return [(0, len(text))], "conditional_scope_preserved"
    protected = set()
    for match in PROTECTED.finditer(text):
        protected.update(range(match.start(), match.end()))
    stack, boundaries = [], []
    in_ascii_quote = False
    for index, character in enumerate(text):
        if index in protected:
            continue
        if character == '"' and (index == 0 or text[index-1] != "\\"):
            in_ascii_quote = not in_ascii_quote
            continue
        if in_ascii_quote:
            continue
        if stack and character == stack[-1]:
            stack.pop()
        elif character in PAIRS:
            stack.append(PAIRS[character])
        elif character in SEPARATORS and not stack:
            boundaries.append(index + 1)
    if not boundaries or boundaries[-1] != len(text):
        boundaries.append(len(text))
    spans, start = [], 0
    for end in boundaries:
        if _meaningful(text[start:end]):
            spans.append((start, end))
            start = end
        elif spans:
            spans[-1] = (spans[-1][0], end)
            start = end
    if not spans:
        spans = [(0, len(text))]
    return spans, "punctuation_partition"


def split_clauses(parents, *, policy_version=POLICY_VERSION):
    if policy_version not in {POLICY_VERSION, V2, V3}:
        raise VerificationError("Unsupported clause policy version.")
    units = []
    for parent in parents:
        text = parent["text"]
        spans, reason = partitions(text, policy_version=policy_version)
        if "".join(text[start:end] for start, end in spans) != text:
            raise VerificationError("Clause partition omitted source characters.")
        matches = list(CITATION.finditer(text))
        final_start = spans[-1][0]
        trailing_pool = bool(matches) and all(match.start() >= final_start for match in matches)
        for index, (start, end) in enumerate(spans, 1):
            own = sorted(set(CITATION.findall(text[start:end])))
            if len(spans) == 1:
                ids, binding = list(parent["citations"]), "original_line"
            elif trailing_pool:
                # Parent-final citations have uncertain clause scope. Keep the pool explicit.
                ids, binding = list(parent["citations"]), "parent_final_candidate_pool"
            else:
                ids, binding = own, "explicit_span_only"
            units.append(dict(
                unit_id=f"{parent['unit_id']}.C{index}", line=parent["line"], text=text[start:end],
                citations=ids, direct_citations=own, parent_id=parent["unit_id"],
                source_line_text=text, span_start=start, span_end=end,
                citation_binding=binding, partition_reason=reason, unitization_version=policy_version))
    validate_anchors(units, parents)
    return units


def validate_anchors(units, parents=None):
    groups = {}
    originals = {row["unit_id"]: row for row in parents} if parents is not None else None
    for unit in units:
        text = unit["source_line_text"]
        start, end = unit["span_start"], unit["span_end"]
        if not 0 <= start < end <= len(text) or text[start:end] != unit["text"]:
            raise VerificationError("Claim span does not match source text.")
        groups.setdefault(unit["parent_id"], []).append(unit)
        if originals is not None:
            parent = originals.get(unit["parent_id"])
            if (parent is None or parent["text"] != text or parent["line"] != unit["line"]
                    or not set(unit["citations"]) <= set(parent["citations"])):
                raise VerificationError("Claim no longer matches its original report parent.")
    if originals is not None and set(groups) != set(originals):
        raise VerificationError("Clause partition omitted a whole source line.")
    for siblings in groups.values():
        siblings.sort(key=lambda row: row["span_start"])
        cursor = 0
        source = siblings[0]["source_line_text"]
        for unit in siblings:
            if unit["source_line_text"] != source or unit["span_start"] != cursor:
                raise VerificationError("Claim spans overlap or omit source text.")
            cursor = unit["span_end"]
        if cursor != len(source):
            raise VerificationError("Claim spans do not cover the full source line.")

