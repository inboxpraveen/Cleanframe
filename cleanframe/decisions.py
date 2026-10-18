"""Typed decisions: let a decision model answer the judgment calls rules cannot make.

Some cleaning steps are judgment calls, not parsing. Is ``BLR`` another name for
``Bangalore``? Is ``Banglore`` a typo of ``Bangalore`` or a different place? Does
``none`` in an *allergies* column mean "no answer" or "no allergies"? Which source
column holds the target schema's ``amount_inr``? The rules answer these
conservatively or not at all. A *decision model* - TypeSafe's Jev, an open "System
One" model such as imajev, or any LLM through :class:`LLMDecisionClient` - answers
each one as a typed question (pick one option, or yes/no) with a probability.

CleanFrame asks those questions once, while planning, and freezes the accepted
answers into the recipe as ordinary ops (``normalize_values``, ``to_na``,
``rename_to``). Replay never calls the model again, and every answer is recorded in
``recipe.meta["decisions"]`` for review.

Policy (see CONTRIBUTING, invariants #2 and #4):

* **Asymmetric thresholds.** An answer that *changes data* - merge an alias, turn a
  token into a missing value, map a column onto a schema field - is applied only when
  its probability clears ``max(mode threshold, decision_threshold)`` (0.9 by
  default). Anything less is recorded as a suggestion and never executed, because
  ``cf.clean`` runs the recipe immediately. An answer that *undoes* a rules merge
  ("no, these are different categories") only has to be more likely than not:
  keeping two spellings apart cannot corrupt data.
* **Exposure.** Category questions send the column's distinct values, so they need
  ``llm_exposure="sample"``. Under ``"metadata"`` only schema questions (column names,
  types and value patterns) and null-token questions are asked; the token in a
  null-token question always comes from CleanFrame's fixed null vocabulary
  (``ops.DEFAULT_NA_TOKENS``), never from free-form data.
* **Deterministic questions.** Questions are built from sorted distinct values, so the
  same data always produces the same questions. Pin a model version for the same
  answers.
* **Dates and numbers stay deterministic.** Decision models are documented to be weak
  at arithmetic and dates, so those stay with CleanFrame's parsers.

The wire format is TypeSafe's System One contract (``POST /v1/systemone`` with
``model``, ``state`` and named ``questions``), implemented here with the standard
library so no SDK is required.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
import urllib.error
import urllib.request
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse

import pandas as pd

from ._version import __version__
from .errors import CleanFrameWarning, LLMError
from .issues import Issue, Issues, Proposal
from .types import LLMExposure, Mode, Op, Severity

logger = logging.getLogger(__name__)

CHOICE = "choice"
NOUL = "noul"
SCORE = "score"

#: Probability a data-changing answer must reach (unless the mode asks for more).
DEFAULT_DECISION_THRESHOLD = 0.9
#: Upper bound on questions per ``clean()`` call: a cost and latency cap.
DEFAULT_MAX_QUESTIONS = 200
#: Questions sent in one request. Open System One servers accept up to 8.
_MAX_QUESTIONS_PER_REQUEST = 8
#: Alias questions per column (the rarest values first).
_MAX_ALIAS_QUESTIONS_PER_COLUMN = 30
#: Category values listed in a question's state and offered as options.
_MAX_CATEGORY_OPTIONS = 60
#: Values longer than this are free text, not category labels worth an alias question.
_MAX_ALIAS_VALUE_LENGTH = 40
#: The extra option every alias / schema question offers.
DISTINCT = "__distinct__"
NO_MATCH = "__none__"

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_RETRY_WAIT = 10.0


class DecisionError(LLMError):
    """A decision backend is misconfigured, unreachable, or returned an unusable answer."""


# ---------------------------------------------------------------------------
# Questions and answers
# ---------------------------------------------------------------------------
@dataclass
class Question:
    """One typed question: ``choice`` (pick one label), ``noul`` (yes/no) or ``score``.

    ``criteria`` follows the System One contract: label -> description (or ``None``)
    for a choice, ``{"true": ..., "false": ...}`` for a noul, and an ordered list of
    level descriptions for a score. ``context`` is never sent anywhere; it carries the
    structured facts behind the question for offline backends and for applying the
    answer.
    """

    type: str
    instructions: str
    criteria: Any
    context: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def choice(
        cls,
        instructions: str,
        options: Mapping[str, str | None],
        context: dict[str, Any] | None = None,
    ) -> Question:
        if not options:
            raise DecisionError("A choice question needs at least one option.")
        return cls(CHOICE, instructions, dict(options), dict(context or {}))

    @classmethod
    def noul(
        cls,
        instructions: str,
        *,
        true: str | None = None,
        false: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> Question:
        return cls(NOUL, instructions, {"true": true, "false": false}, dict(context or {}))

    @classmethod
    def score(
        cls, instructions: str, levels: list[str], context: dict[str, Any] | None = None
    ) -> Question:
        if not levels:
            raise DecisionError("A score question needs at least one level.")
        return cls(SCORE, instructions, list(levels), dict(context or {}))

    @property
    def labels(self) -> list[str]:
        """The answer labels: choice names, ``yes``/``no``, or score levels ``0..n-1``."""
        if self.type == CHOICE:
            return list(self.criteria)
        if self.type == SCORE:
            return [str(i) for i in range(len(self.criteria))]
        return ["yes", "no"]

    def to_wire(self) -> dict[str, Any]:
        """The System One JSON form of this question (``context`` is left out)."""
        out: dict[str, Any] = {"type": self.type, "instructions": self.instructions}
        if self.type == NOUL:
            described = {k: v for k, v in self.criteria.items() if v is not None}
            if described:
                out["criteria"] = described
        else:
            out["criteria"] = self.criteria
        return out


@dataclass
class Answer:
    """A typed answer. ``probabilities`` always maps every known label to a float.

    For a noul the labels are ``yes``/``no``; for a score they are ``"0".."n-1"``.
    """

    type: str
    probabilities: dict[str, float]
    choice: str | None = None
    confidence: float | None = None
    score: float | None = None

    @property
    def p_yes(self) -> float:
        return self.probabilities.get("yes", 0.0)

    def decision(self) -> tuple[str, float]:
        """The chosen label and the probability CleanFrame gates on.

        For a choice that is the *smaller* of the label's probability and the
        model's reported confidence: either number being low is reason enough not to
        act on the answer.
        """
        if self.type == NOUL:
            p = self.p_yes
            return ("yes", p) if p >= 0.5 else ("no", 1.0 - p)
        label = self.choice
        if label is None:
            label = max(sorted(self.probabilities), key=lambda k: self.probabilities[k])
        p = self.probabilities.get(label, 0.0)
        if self.confidence is not None:
            p = min(p, self.confidence)
        return label, p


@dataclass
class DecisionResponse:
    """What one request returned. Unanswerable questions are simply absent."""

    answers: dict[str, Answer]
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None


class DecisionClient(Protocol):
    """Anything that answers typed questions about one shared ``state``."""

    model: str
    #: Whether the backend's probabilities are trained to be calibrated.
    calibrated: bool

    def decide(self, state: Any, questions: dict[str, Question]) -> DecisionResponse:
        """Answer ``questions`` (keyed by name) about ``state``."""


def _prob(value: Any) -> float | None:
    """A finite probability clamped to ``[0, 1]``, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    if not math.isfinite(f):
        return None
    return min(1.0, max(0.0, f))


def _weight(value: Any) -> float | None:
    """A finite, non-negative weight (an LLM may answer 60 for 60%)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) and f >= 0 else None


def _normalise(raw: Any, labels: list[str], *, weights: bool = False) -> dict[str, float] | None:
    """Turn a label -> probability mapping into a distribution over ``labels``.

    Unknown labels are dropped and missing ones count as 0. With ``weights`` the
    numbers are relative weights rather than probabilities (an LLM may answer in
    percent). Returns ``None`` when no usable probability mass remains.
    """
    if not isinstance(raw, dict):
        return None
    read = _weight if weights else _prob
    probs = {label: (read(raw.get(label)) or 0.0) for label in labels}
    total = sum(probs.values())
    if total <= 0:
        return None
    return {label: p / total for label, p in probs.items()}


# ---------------------------------------------------------------------------
# Backend: the System One wire protocol (TypeSafe, OpenRouter, open models)
# ---------------------------------------------------------------------------
def _is_local(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")


class SystemOneClient:
    """Answer questions over ``POST {base_url}/v1/systemone``.

    Works with TypeSafe's API, OpenRouter's System One route and self-hosted
    Jev-compatible models (imajev, Laya servers). Uses only the standard library.
    The API key is sent as a bearer token and never appears in errors or logs.
    """

    calibrated = True

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str | None = None,
        key_env: str | None = None,
        provider: str = "systemone",
        timeout: float = 30.0,
        max_retries: int = 2,
        retry_backoff: float = 1.0,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._key_env = key_env
        self.provider = provider
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff = max(0.0, float(retry_backoff))
        self._extra_headers = dict(extra_headers or {})
        scheme = urlparse(self.base_url).scheme
        if scheme not in ("http", "https"):
            raise DecisionError(
                f"Decision backend URL must start with http:// or https://, got {base_url!r}."
            )
        if scheme == "http" and not _is_local(self.base_url):
            warnings.warn(
                f"CleanFrame: the decision backend {self.base_url} is plain http on a "
                "non-local host - questions (and any values they carry) travel unencrypted.",
                CleanFrameWarning,
                stacklevel=2,
            )

    def __repr__(self) -> str:  # the key must never show up in a repr
        return f"SystemOneClient(model={self.model!r}, base_url={self.base_url!r})"

    def decide(self, state: Any, questions: dict[str, Question]) -> DecisionResponse:
        if not questions:
            raise DecisionError("At least one question is required.")
        if self._key_env and not self._api_key:
            raise DecisionError(
                f"No API key for the {self.provider} decision backend: set {self._key_env}."
            )
        body = {
            "model": self.model,
            "state": state,
            "questions": {name: q.to_wire() for name, q in questions.items()},
        }
        raw = self._post(json.dumps(body, ensure_ascii=False).encode("utf-8"))
        return parse_system_one_response(raw, questions, default_model=self.model)

    def _post(self, data: bytes) -> dict:
        url = self.base_url + "/v1/systemone"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"cleanframe/{__version__}",
            **self._extra_headers,
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        attempt = 0
        while True:
            request = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:  # noqa: S310 - scheme checked in __init__
                    payload = resp.read()
                break
            except urllib.error.HTTPError as exc:
                status = exc.code
                detail = _error_detail(exc)
                if status in _RETRY_STATUSES and attempt < self.max_retries:
                    time.sleep(self._wait(attempt, exc.headers.get("retry-after") if exc.headers else None))
                    attempt += 1
                    continue
                raise DecisionError(
                    f"The {self.provider} decision backend returned HTTP {status}{detail}."
                ) from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < self.max_retries:
                    time.sleep(self._wait(attempt, None))
                    attempt += 1
                    continue
                reason = getattr(exc, "reason", exc)
                raise DecisionError(
                    f"Could not reach the {self.provider} decision backend at {self.base_url}: {reason}"
                ) from None
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DecisionError(
                f"The {self.provider} decision backend returned a response that is not JSON."
            ) from None
        if not isinstance(decoded, dict):
            raise DecisionError(f"The {self.provider} decision backend returned an unexpected shape.")
        return decoded

    def _wait(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(_MAX_RETRY_WAIT, max(0.0, float(retry_after)))
            except ValueError:
                pass
        return min(_MAX_RETRY_WAIT, self.retry_backoff * (2**attempt))


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001 - the detail is best-effort
        return ""
    if not body:
        return ""
    return f": {body[:200]}" + ("…" if len(body) > 200 else "")


def parse_system_one_response(
    raw: dict, questions: dict[str, Question], *, default_model: str = ""
) -> DecisionResponse:
    """Parse a System One response. Answers that do not fit their question are dropped.

    Accepts the extra fields open servers add (``abstained``, ``unknown_probability``):
    an abstention is treated as no answer.
    """
    answers_raw = raw.get("answers")
    if not isinstance(answers_raw, dict):
        raise DecisionError("The decision backend's response has no 'answers' object.")
    answers: dict[str, Answer] = {}
    for name, question in questions.items():
        entry = answers_raw.get(name)
        if not isinstance(entry, dict) or entry.get("type") != question.type:
            continue
        if entry.get("abstained") is True:
            continue
        answer = _parse_answer(entry, question)
        if answer is not None:
            answers[name] = answer
    usage_raw = raw.get("usage")
    usage: dict[str, Any] = usage_raw if isinstance(usage_raw, dict) else {}
    cost = _prob_free_float(usage.get("cost"))
    return DecisionResponse(
        answers=answers,
        model=str(raw.get("model") or default_model),
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        cost=cost,
    )


def _parse_answer(entry: dict, question: Question) -> Answer | None:
    labels = question.labels
    if question.type == NOUL:
        p = _prob(entry.get("noul"))
        if p is None:
            return None
        return Answer(NOUL, {"yes": p, "no": 1.0 - p})
    probs = _normalise(entry.get("probabilities"), labels)
    if probs is None:
        return None
    confidence = _prob(entry.get("confidence"))
    if question.type == CHOICE:
        label = entry.get("choice")
        if label not in probs:
            return None
        return Answer(CHOICE, probs, choice=str(label), confidence=confidence)
    score = entry.get("score")
    score_f = float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else None
    return Answer(SCORE, probs, confidence=confidence, score=score_f)


def _int(value: Any) -> int:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _prob_free_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


# ---------------------------------------------------------------------------
# Backend: any LLM, through the same typed interface
# ---------------------------------------------------------------------------
_LLM_DECISION_PROMPT = """You answer typed questions about data-cleaning decisions. \
You never write prose. For every question, give a probability for each of its options.

Return ONLY a JSON object (no markdown fences) of this exact shape:
{"answers": {"<question name>": {"probabilities": {"<option>": <0..1>, ...}}, ...}}

Rules:
- Use the option labels exactly as given. Options you leave out count as 0.
- The probabilities of a question should sum to 1.
- yes/no questions have the options "yes" and "no".
- Be calibrated: say 0.6 when you are unsure, not 0.99. When the evidence does not \
settle the question, prefer the option that keeps values distinct or unchanged."""


class LLMDecisionClient:
    """Adapter: any :class:`~cleanframe.llm.LLMClient` answers the same typed questions.

    An LLM's stated probabilities are not trained to be calibrated, so this backend
    reports ``calibrated = False`` and that flag is recorded in the recipe.
    """

    calibrated = False

    def __init__(self, client: Any, *, max_output_tokens: int = 4096) -> None:
        if not hasattr(client, "complete"):
            raise DecisionError("LLMDecisionClient needs an object with a .complete() method.")
        self.client = client
        self.model = f"llm:{getattr(client, 'model', 'custom')}"
        self.max_output_tokens = max_output_tokens

    def decide(self, state: Any, questions: dict[str, Question]) -> DecisionResponse:
        from .llm import _extract_json_object

        payload = {
            "state": state,
            "questions": {
                name: {
                    "type": "yes/no" if q.type == NOUL else q.type,
                    "instructions": q.instructions,
                    "options": _llm_options(q),
                }
                for name, q in questions.items()
            },
        }
        user = json.dumps(payload, ensure_ascii=False, indent=2)
        response = self.client.complete(_LLM_DECISION_PROMPT, user, max_tokens=self.max_output_tokens)
        try:
            data = json.loads(_extract_json_object(response.text))
        except (LLMError, json.JSONDecodeError) as exc:
            raise DecisionError(f"The LLM did not return usable JSON answers: {exc}") from exc
        answers_raw = data.get("answers", data) if isinstance(data, dict) else {}
        answers: dict[str, Answer] = {}
        for name, question in questions.items():
            entry = answers_raw.get(name) if isinstance(answers_raw, dict) else None
            raw_probs = entry.get("probabilities") if isinstance(entry, dict) else None
            probs = _normalise(raw_probs, question.labels, weights=True)
            if probs is None:
                continue
            if question.type == NOUL:
                answers[name] = Answer(NOUL, probs)
                continue
            # Highest probability wins; ties go to the earlier option, deterministically.
            best = max(question.labels, key=lambda label: (probs[label], -question.labels.index(label)))
            if question.type == CHOICE:
                answers[name] = Answer(CHOICE, probs, choice=best)
            else:
                expected = sum(int(label) * p for label, p in probs.items())
                answers[name] = Answer(SCORE, probs, score=expected)
        return DecisionResponse(
            answers=answers,
            model=self.model,
            input_tokens=int(getattr(response, "input_tokens", 0) or 0),
            output_tokens=int(getattr(response, "output_tokens", 0) or 0),
        )


def _llm_options(question: Question) -> dict[str, Any]:
    if question.type == CHOICE:
        return dict(question.criteria)
    if question.type == SCORE:
        return {str(i): level for i, level in enumerate(question.criteria)}
    return {
        "yes": question.criteria.get("true") or "yes",
        "no": question.criteria.get("false") or "no",
    }


# ---------------------------------------------------------------------------
# Backend: CleanFrame's own heuristics (offline baseline, benchmarks, dry runs)
# ---------------------------------------------------------------------------
class RulesDecisionClient:
    """Answers the way CleanFrame's rules already decide - no network, no model.

    It reads each question's ``context`` and mirrors the detectors: a typo merge the
    rules made is a "yes" at the detector's own confidence (0.7), an alias is always
    "distinct" (rules never map abbreviations), an ambiguous null token is "no", and a
    schema field gets the detector's own best name match. So ``decide="rules"`` plans
    what plain rules plan: it is the zero-cost baseline in the benchmark and a safe dry
    run of a decision pass. Its probabilities are heuristic scores, not calibrated.
    """

    calibrated = False
    model = "rules"

    def decide(self, state: Any, questions: dict[str, Question]) -> DecisionResponse:
        answers: dict[str, Answer] = {}
        for name, question in questions.items():
            answer = self._answer(question)
            if answer is not None:
                answers[name] = answer
        return DecisionResponse(answers=answers, model=self.model)

    def _answer(self, question: Question) -> Answer | None:
        from ._util import similarity

        ctx = question.context
        kind = ctx.get("kind")
        if kind == "category_merge":
            # Only merges the rules made are asked about; they carry confidence 0.7.
            return Answer(NOUL, {"yes": 0.7, "no": 0.3})
        if kind == "category_alias":
            return _one_hot_choice(question, DISTINCT, 0.9)
        if kind == "schema_match":
            # The schema detector's own rule: best name similarity, kept from 0.4 up.
            target = str(ctx["target"])
            scored = sorted(
                ((similarity(target, str(opt)), str(opt)) for opt in ctx.get("options", [])),
                key=lambda t: (-t[0], t[1]),
            )
            if scored and scored[0][0] >= 0.4:
                return _one_hot_choice(question, scored[0][1], scored[0][0])
            return _one_hot_choice(question, NO_MATCH, 0.6)
        if kind == "null_token":
            # The rules never convert an ambiguous token: it may be real data.
            return Answer(NOUL, {"yes": 0.45, "no": 0.55})
        return None


def _one_hot_choice(question: Question, label: str, p: float) -> Answer | None:
    if label not in question.labels:
        return None
    rest = (1.0 - p) / max(1, len(question.labels) - 1)
    probs = {lbl: (p if lbl == label else rest) for lbl in question.labels}
    return Answer(CHOICE, probs, choice=label, confidence=p)


# ---------------------------------------------------------------------------
# Resolving a spec string
# ---------------------------------------------------------------------------
_TYPESAFE_DEFAULT_URL = "https://api.typesafe.ai"
_OPENROUTER_URL = "https://openrouter.ai/api"


def get_decision_client(spec: Any) -> DecisionClient:
    """Resolve ``decide=`` into a client.

    * ``"typesafe/jev-latest"`` - TypeSafe's API; reads only ``TYPESAFE_API_KEY``
      (and ``TYPESAFE_BASE_URL``, as TypeSafe's own SDK does).
    * ``"openrouter/typesafe/jev-1.14"`` - Jev through OpenRouter; reads only
      ``OPENROUTER_API_KEY``.
    * ``"systemone/<model>"`` - any self-hosted System One server at
      ``SYSTEMONE_BASE_URL`` (key ``SYSTEMONE_API_KEY``, optional).
    * ``"rules"`` - CleanFrame's own heuristics, offline.
    * any LLM spec (``"anthropic/claude-sonnet-4-6"``, ``"ollama/qwen3"``, ...) - the
      LLM answers through :class:`LLMDecisionClient` (uncalibrated).
    * an object with a ``.decide()`` method is used as is.
    """
    if not isinstance(spec, str):
        if hasattr(spec, "decide"):
            return spec
        raise DecisionError(
            "decide must be a backend spec string (e.g. 'typesafe/jev-latest') or an object "
            f"with a .decide() method, got {type(spec).__name__}."
        )
    text = spec.strip()
    if text.lower() == "rules":
        return RulesDecisionClient()
    if "/" not in text:
        raise DecisionError(
            f"decide must be 'provider/model' (e.g. 'typesafe/jev-latest') or 'rules', got {spec!r}."
        )
    provider, model = text.split("/", 1)
    provider = provider.lower()
    if not model:
        raise DecisionError(f"decide spec {spec!r} names no model.")
    if provider == "typesafe":
        return SystemOneClient(
            model,
            base_url=os.environ.get("TYPESAFE_BASE_URL", "").strip() or _TYPESAFE_DEFAULT_URL,
            api_key=os.environ.get("TYPESAFE_API_KEY", "").strip() or None,
            key_env="TYPESAFE_API_KEY",
            provider="TypeSafe",
        )
    if provider == "openrouter" and model.lower().startswith("typesafe/"):
        return SystemOneClient(
            model,
            base_url=_OPENROUTER_URL,
            api_key=os.environ.get("OPENROUTER_API_KEY", "").strip() or None,
            key_env="OPENROUTER_API_KEY",
            provider="OpenRouter",
            extra_headers={
                "HTTP-Referer": "https://github.com/inboxpraveen/Cleanframe",
                "X-Title": "CleanFrame",
            },
        )
    if provider == "systemone":
        base_url = os.environ.get("SYSTEMONE_BASE_URL", "").strip()
        if not base_url:
            raise DecisionError(
                "decide='systemone/...' needs SYSTEMONE_BASE_URL (e.g. http://127.0.0.1:8765)."
            )
        return SystemOneClient(
            model,
            base_url=base_url,
            api_key=os.environ.get("SYSTEMONE_API_KEY", "").strip() or None,
            provider="System One",
        )
    from .llm import get_client

    return LLMDecisionClient(get_client(text))


# ---------------------------------------------------------------------------
# The decision pass
# ---------------------------------------------------------------------------
@dataclass
class DecisionRecord:
    """One answered question and what CleanFrame did with it."""

    kind: str
    column: str | None
    subject: str
    answer: str
    probability: float
    #: applied | suggested | vetoed | confirmed | kept | rejected
    outcome: str

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind}
        if self.column is not None:
            out["column"] = self.column
        out.update(
            subject=self.subject,
            answer=self.answer,
            p=round(float(self.probability), 3),
            outcome=self.outcome,
        )
        return out


@dataclass
class DecisionLog:
    """Everything one decision pass asked, answered and changed."""

    model: str
    calibrated: bool
    threshold: float
    records: list[DecisionRecord] = field(default_factory=list)
    asked: int = 0
    answered: int = 0
    skipped: int = 0
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    #: Columns whose distinct values were sent off the machine.
    values_sent: list[str] = field(default_factory=list)

    def count(self, outcome: str) -> int:
        return sum(1 for r in self.records if r.outcome == outcome)

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "model": self.model,
            "calibrated": self.calibrated,
            "threshold": round(float(self.threshold), 3),
            "asked": self.asked,
            "answered": self.answered,
            "applied": self.count("applied"),
            "suggested": self.count("suggested"),
            "vetoed": self.count("vetoed"),
        }
        if self.skipped:
            out["skipped"] = self.skipped
        if self.cost is not None:
            out["cost_usd"] = round(float(self.cost), 6)
        if self.error:
            out["error"] = self.error
        return out

    def to_meta(self) -> dict[str, Any]:
        """The compact, YAML-safe block stored in ``recipe.meta['decisions']``."""
        meta = self.summary()
        if self.notes:
            meta["notes"] = list(self.notes)
        recorded = [r.to_dict() for r in self.records if r.outcome != "rejected"]
        if recorded:
            meta["records"] = recorded
        return meta


@dataclass
class _Batch:
    kind: str
    column: str | None
    state: Any
    questions: dict[str, Question]


class _Asker:
    """Sends batches within the question budget and keeps the log's counters."""

    def __init__(self, client: DecisionClient, log: DecisionLog, max_questions: int, fallback: bool):
        self.client = client
        self.log = log
        self.remaining = max(0, int(max_questions))
        self.fallback = fallback
        self.dead = False

    def ask(self, batch: _Batch) -> dict[str, Answer]:
        names = list(batch.questions)
        if self.dead:
            self.log.skipped += len(names)
            return {}
        if len(names) > self.remaining:
            self.log.skipped += len(names) - self.remaining
            names = names[: self.remaining]
        answers: dict[str, Answer] = {}
        for start in range(0, len(names), _MAX_QUESTIONS_PER_REQUEST):
            chunk = {n: batch.questions[n] for n in names[start : start + _MAX_QUESTIONS_PER_REQUEST]}
            try:
                response = self.client.decide(batch.state, chunk)
            except Exception as exc:  # noqa: BLE001 - every backend failure takes one path
                if not self.fallback:
                    if isinstance(exc, DecisionError):
                        raise
                    raise DecisionError(f"Decision request failed: {exc}") from exc
                self.dead = True
                self.log.error = str(exc)
                unasked = len(names) - start
                self.log.skipped += unasked
                warnings.warn(
                    f"CleanFrame: the decision backend failed ({exc}); continuing with the "
                    "rules-only plan for the remaining questions. See recipe.meta['decisions'].",
                    CleanFrameWarning,
                    stacklevel=4,
                )
                return answers
            self.remaining -= len(chunk)
            self.log.asked += len(chunk)
            self.log.requests += 1
            self.log.input_tokens += response.input_tokens
            self.log.output_tokens += response.output_tokens
            if response.cost is not None:
                self.log.cost = (self.log.cost or 0.0) + response.cost
            if response.model and response.model != self.log.model:
                self.log.model = response.model
            for name in chunk:
                if name in response.answers:
                    answers[name] = response.answers[name]
            self.log.answered += sum(1 for name in chunk if name in response.answers)
        return answers


def refine_issues(
    df: pd.DataFrame,
    profile: Any,
    issues: Issues,
    *,
    client: DecisionClient,
    schema: Any | None = None,
    mode: Mode | str = Mode.REVIEW,
    exposure: LLMExposure | str = LLMExposure.METADATA,
    decision_threshold: float = DEFAULT_DECISION_THRESHOLD,
    max_questions: int = DEFAULT_MAX_QUESTIONS,
    options: dict[str, Any] | None = None,
    fallback: bool = True,
) -> tuple[Issues, DecisionLog]:
    """Ask ``client`` the judgment calls in ``issues`` and return refined issues.

    The returned issues feed the ordinary planner: accepted answers become fixes whose
    confidence is the answer's probability, everything else becomes an informational
    suggestion. ``issues`` itself is not modified.
    """
    from .llm import coerce_exposure
    from .planner import MODE_THRESHOLDS

    mode = Mode.coerce(mode)
    exposure = coerce_exposure(exposure)
    if not 0.0 <= float(decision_threshold) <= 1.0:
        raise DecisionError(f"decision_threshold must be between 0 and 1, got {decision_threshold!r}.")
    accept = max(MODE_THRESHOLDS[mode], float(decision_threshold))
    log = DecisionLog(
        model=str(getattr(client, "model", "custom")),
        calibrated=bool(getattr(client, "calibrated", False)),
        threshold=accept,
    )
    out = Issues(list(issues))
    if exposure is LLMExposure.NONE:
        log.notes.append("llm_exposure='none': no decision questions were asked")
        return out, log

    asker = _Asker(client, log, max_questions, fallback)
    options = options or {}
    sample = exposure is LLMExposure.SAMPLE

    out = _decide_schema(df, profile, out, schema, asker, log, accept, sample)
    out = _decide_null_tokens(df, profile, out, asker, log, accept, sample)
    if sample:
        out = _decide_categories(df, profile, out, asker, log, accept, options)
    elif _has_category_columns(df, profile):
        log.notes.append(
            "category alias / typo questions need llm_exposure='sample' (they send the "
            "column's distinct values); they were not asked"
        )
    if log.skipped and not log.error:
        log.notes.append(
            f"{log.skipped} question(s) were not asked: max_decisions={max_questions} was reached"
        )
    if log.values_sent:
        log.values_sent = sorted(set(log.values_sent))
        warnings.warn(
            "CleanFrame: llm_exposure='sample' sent cell values to the decision backend "
            "(distinct category values verbatim; example values redacted as for the LLM "
            f"planner) for column(s): {', '.join(log.values_sent)}.",
            CleanFrameWarning,
            stacklevel=3,
        )
    logger.info(
        "decisions: %d asked, %d answered, %d applied (%s)",
        log.asked, log.answered, log.count("applied"), log.model,
    )
    return out, log


# -- schema mapping ----------------------------------------------------------
def _examples(cp: Any, series: pd.Series, k: int) -> list[str]:
    """Up to ``k`` redacted example values that do not depend on row order.

    The most frequent distinct values (ties alphabetical), redacted exactly as the
    LLM planner's SAMPLE exposure redacts them, so shuffled rows ask the same
    questions.
    """
    from ._util import sample_non_null
    from .llm import _anonymize_value

    counts: dict[str, int] = {}
    for v in sample_non_null(series, cap=10_000):
        key = str(v)
        counts[key] = counts.get(key, 0) + 1
    out: list[str] = []
    for value in sorted(counts, key=lambda v: (-counts[v], v)):
        anon = _anonymize_value(value, cp.semantic_type)
        if anon not in out:
            out.append(anon)
        if len(out) >= k:
            break
    return out


def _column_overview(df: pd.DataFrame, profile: Any, names: list[str], sample: bool) -> list[dict]:
    from .llm import value_sketches

    overview = []
    for name in names:
        cp = profile.column(name)
        entry: dict[str, Any] = {
            "column": name,
            "semantic_type": cp.semantic_type if cp else "unknown",
            "value_patterns": value_sketches(df[name]),
        }
        if sample and cp is not None:
            entry["example_values"] = _examples(cp, df[name], k=3)
        overview.append(entry)
    return overview


def _decide_schema(df, profile, issues: Issues, schema, asker: _Asker, log: DecisionLog,
                   accept: float, sample: bool) -> Issues:
    if schema is None:
        return issues
    weak = {str(i.evidence.get("target")): i for i in issues if i.kind == "weak_schema_match"}
    missing = {str(i.evidence.get("target")): i for i in issues if i.kind == "missing_schema_column"}
    targets = [c for c in schema.columns if c.name in weak or c.name in missing]
    if not targets:
        return issues
    # Sources already matched - by a strong rename, or by carrying a schema field's exact
    # name (which raises no issue at all) - are never offered for another target.
    schema_names = {c.name for c in schema.columns}
    strong_sources = {
        i.column for i in issues if i.kind == "schema_mapping" and i.column is not None
    } | {str(c) for c in df.columns if str(c) in schema_names}
    # A target already named in the data needs no question.
    candidates = sorted(str(c) for c in df.columns if str(c) not in strong_sources)
    if not candidates:
        return issues
    overview = _column_overview(df, profile, candidates, sample)
    patterns = {e["column"]: e for e in overview}
    state = {
        "task": "Map the columns of a messy data file onto a target schema.",
        "source_columns": overview,
    }
    questions: dict[str, Question] = {}
    for i, scol in enumerate(targets):
        option_desc = {
            src: f"semantic type {patterns[src]['semantic_type']}; values look like "
            + (", ".join(patterns[src]["value_patterns"]) or "(empty)")
            for src in candidates
        }
        option_desc[NO_MATCH] = "None of these columns holds this field."
        questions[f"map_{i}"] = Question.choice(
            f"Which source column holds the target field {scol.name!r} "
            f"(expected type: {scol.dtype})?",
            option_desc,
            context={
                "kind": "schema_match",
                "target": scol.name,
                "dtype": scol.dtype,
                "options": candidates,
            },
        )
    answers = asker.ask(_Batch("schema_match", None, state, questions))
    if sample and answers:
        log.values_sent.extend(candidates)

    # Resolve: a source may serve one target; the more probable claim wins.
    claims: list[tuple[float, str, str]] = []
    vetoed_targets: set[str] = set()
    suggestions: list[Issue] = []
    for name, question in questions.items():
        answer = answers.get(name)
        if answer is None:
            continue
        target = question.context["target"]
        label, p = answer.decision()
        if label == NO_MATCH:
            outcome = "vetoed" if p >= 0.5 and target in weak else "kept"
            if outcome == "vetoed":
                vetoed_targets.add(target)
            log.records.append(DecisionRecord("schema_match", None, target, "none", p, outcome))
        elif p >= accept:
            claims.append((p, target, label))
        elif p >= 0.5:
            log.records.append(DecisionRecord("schema_match", label, target, label, p, "suggested"))
            suggestions.append(
                _suggestion(label, f"Decision model suggests mapping {label!r} → {target!r} "
                            f"(p={p:.2f}, below the {accept:.2f} bar)", {"target": target})
            )
        else:
            log.records.append(DecisionRecord("schema_match", label, target, label, p, "rejected"))

    used: set[str] = set()
    mapped: dict[str, str] = {}
    for p, target, source in sorted(claims, key=lambda t: (-t[0], t[1], t[2])):
        if source in used:
            log.records.append(DecisionRecord("schema_match", source, target, source, p, "suggested"))
            continue
        used.add(source)
        mapped[target] = source
        log.records.append(DecisionRecord("schema_match", source, target, source, p, "applied"))

    if not mapped and not vetoed_targets and not suggestions:
        return issues
    probability = {t: p for p, t, _ in claims}
    result = Issues()
    for issue in issues:
        target = str(issue.evidence.get("target"))
        if issue.kind == "weak_schema_match" and (target in mapped or target in vetoed_targets):
            continue  # replaced by the decision (or vetoed by it)
        if issue.kind == "weak_schema_match" and issue.column in used:
            continue  # its source now belongs to another target
        if issue.kind == "missing_schema_column" and target in mapped:
            continue
        if issue.kind == "extra_source_column" and issue.column in used:
            continue
        result.append(issue)
    for target, source in sorted(mapped.items()):
        result.append(
            Issue(
                kind="schema_mapping",
                message=f"Map {source!r} → {target!r} (decision, p={probability[target]:.2f})",
                severity=Severity.INFO,
                column=source,
                # rank 0 in the planner's rename resolution, like a strong name match
                detector="schema_mapping",
                confidence=round(probability[target], 3),
                evidence={"target": target, "decided_by": log.model},
                proposal=Proposal(rename_to=target),
            )
        )
    result.extend(suggestions)
    return result


# -- null tokens ---------------------------------------------------------------
def _decide_null_tokens(df, profile, issues: Issues, asker: _Asker, log: DecisionLog,
                        accept: float, sample: bool) -> Issues:
    from .llm import value_sketches
    from .ops import DEFAULT_NA_TOKENS

    vocabulary = {t.strip().casefold() for t in DEFAULT_NA_TOKENS if t}
    targets = [
        i for i in issues
        if i.kind == "disguised_nulls" and i.evidence.get("ambiguous") and i.column is not None
    ]
    if not targets:
        return issues
    replaced: dict[int, list[Issue]] = {}
    for issue in sorted(targets, key=lambda i: str(i.column)):
        column = str(issue.column)
        tokens = sorted(
            str(t) for t in (issue.evidence.get("tokens") or {})
            if str(t).strip().casefold() in vocabulary  # invariant #2: vocabulary only
        )
        if not tokens or column not in df.columns:
            continue
        cp = profile.column(column)
        state: dict[str, Any] = {
            "task": "Decide whether placeholder-looking values in a data column mean 'missing'.",
            "column": column,
            "semantic_type": cp.semantic_type if cp else "unknown",
            "null_fraction": round(float(cp.null_fraction), 3) if cp else None,
            "value_patterns": value_sketches(df[column]),
        }
        if sample and cp is not None:
            state["example_values"] = _examples(cp, df[column], k=5)
        questions = {
            f"null_{k}": Question.noul(
                f"In the data column {column!r}, does the value {token!r} stand for a missing "
                "or unknown value, rather than a real answer?",
                true=f"{token!r} is a placeholder for missing or unknown data.",
                false=f"{token!r} is a meaningful value in this column.",
                context={"kind": "null_token", "column": column, "token": token},
            )
            for k, token in enumerate(tokens)
        }
        answers = asker.ask(_Batch("null_token", column, state, questions))
        if sample and answers:
            log.values_sent.append(column)
        accepted: list[tuple[str, float]] = []
        for name, question in questions.items():
            answer = answers.get(name)
            if answer is None:
                continue
            token = question.context["token"]
            p_yes = answer.p_yes
            if p_yes >= accept:
                accepted.append((token, p_yes))
                log.records.append(DecisionRecord("null_token", column, token, "yes", p_yes, "applied"))
            elif p_yes >= 0.5:
                log.records.append(DecisionRecord("null_token", column, token, "yes", p_yes, "suggested"))
            else:
                log.records.append(DecisionRecord("null_token", column, token, "no", 1 - p_yes, "kept"))
        if not accepted:
            continue
        counts = issue.evidence.get("tokens") or {}
        accepted_tokens = [t for t, _ in accepted]
        remaining = [t for t in tokens if t not in accepted_tokens]
        new: list[Issue] = [
            Issue(
                kind="disguised_nulls",
                message=f"{sum(int(counts.get(t, 0)) for t in accepted_tokens)} value(s) are "
                f"placeholders for missing data ({', '.join(map(repr, accepted_tokens))}; decision)",
                severity=Severity.WARNING,
                column=column,
                detector=issue.detector,
                confidence=round(min(p for _, p in accepted), 3),
                evidence={
                    "count": sum(int(counts.get(t, 0)) for t in accepted_tokens),
                    "tokens": {t: counts.get(t, 0) for t in accepted_tokens},
                    "decided_by": log.model,
                },
                proposal=Proposal(ops=[Op("to_na", {"tokens": accepted_tokens})]),
            )
        ]
        if remaining:
            new.append(
                Issue(
                    kind=issue.kind,
                    message=issue.message,
                    severity=issue.severity,
                    column=column,
                    detector=issue.detector,
                    confidence=issue.confidence,
                    evidence={**issue.evidence, "tokens": {t: counts.get(t, 0) for t in remaining}},
                    proposal=Proposal(ops=[Op("to_na", {"tokens": remaining})]),
                )
            )
        replaced[id(issue)] = new
    if not replaced:
        return issues
    result = Issues()
    for issue in issues:
        for item in replaced.get(id(issue), [issue]):
            result.append(item)
    return result


# -- categories ----------------------------------------------------------------
def _category_column_counts(series: pd.Series, cp: Any) -> dict[str, int] | None:
    """Spelling counts for a column worth category questions, else ``None``.

    Beyond the category detector's own test the values must repeat (a profiled
    ``categorical`` column, or on average two rows per spelling): a column of
    mostly-unique names gains nothing from alias questions, and asking would send
    those values off the machine for no benefit.
    """
    from .detectors.categories import category_counts, is_category_column

    if not is_category_column(series, cp):
        return None
    counts = category_counts(series)
    if len(counts) < 2:
        return None
    if cp.semantic_type != "categorical" and 2 * len(counts) > sum(counts.values()):
        return None
    return counts


def _has_category_columns(df: pd.DataFrame, profile: Any) -> bool:
    return any(
        _category_column_counts(df[column], profile.column(str(column))) is not None
        for column in df.columns
    )


def _decide_categories(df, profile, issues: Issues, asker: _Asker, log: DecisionLog,
                       accept: float, options: dict[str, Any]) -> Issues:
    from .detectors.categories import _cluster, _cluster_groups

    seed_maps = options.get("category_map") or {}
    variant_issue = {
        str(i.column): i for i in issues if i.kind == "category_variants" and i.column is not None
    }
    replacements: dict[int, Issue | None] = {}
    additions: list[Issue] = []

    for column in sorted(str(c) for c in df.columns):
        counts_or_none = _category_column_counts(df[column], profile.column(column))
        if counts_or_none is None:
            continue
        counts = counts_or_none
        seed = seed_maps.get(column) if isinstance(seed_maps, dict) else None
        sent = False

        # 1) Check each fuzzy (typo) merge the rules made.
        groups, canon_of_group, cluster_rep = _cluster_groups(counts)
        merges = sorted(
            {
                (canon_of_group[key], canon_of_group[rep])
                for key, rep in cluster_rep.items()
                if rep != key
            }
        )
        forbidden: set[tuple[str, str]] = set()
        confirmed_all = bool(merges)
        if merges:
            state = _category_state(column, counts)
            questions = {
                f"merge_{k}": Question.noul(
                    f"In the data column {column!r}, are {variant!r} and {canonical!r} the same "
                    "category - one a misspelling or variant spelling of the other?",
                    true="They mean the same thing; one is a variant spelling of the other.",
                    false="They are different categories that only look alike.",
                    context={"kind": "category_merge", "column": column,
                             "variant": variant, "canonical": canonical},
                )
                for k, (variant, canonical) in enumerate(merges)
            }
            answers = asker.ask(_Batch("category_merge", column, state, questions))
            sent = bool(answers)
            for name, question in questions.items():
                variant = question.context["variant"]
                canonical = question.context["canonical"]
                answer = answers.get(name)
                if answer is None:
                    confirmed_all = False
                    continue
                p_yes = answer.p_yes
                subject = f"{variant} -> {canonical}"
                if p_yes < 0.5:
                    forbidden.add((variant, canonical))
                    confirmed_all = False
                    log.records.append(
                        DecisionRecord("category_merge", column, subject, "no", 1 - p_yes, "vetoed")
                    )
                else:
                    if p_yes < accept:
                        confirmed_all = False
                    log.records.append(
                        DecisionRecord("category_merge", column, subject, "yes", p_yes, "confirmed")
                    )

        mapping = _cluster(counts, seed, frozenset(forbidden) if forbidden else None)
        existing = variant_issue.get(column)
        if existing is not None and (forbidden or confirmed_all):
            replacements[id(existing)] = _rebuild_variant_issue(
                existing, mapping, counts, log.model, confirmed_all and not forbidden
            )

        # 2) Ask whether a remaining value is another name for another value.
        canon_counts: dict[str, int] = {}
        for value, n in counts.items():
            target = mapping.get(value, value)
            canon_counts[target] = canon_counts.get(target, 0) + n
        if len(canon_counts) < 2:
            if sent:
                log.values_sent.append(column)
            continue
        ranked = [v for v in sorted(canon_counts, key=lambda v: (-canon_counts[v], v)) if v.strip()]
        seeded = set((seed or {}).keys()) | set((seed or {}).values())
        # The most common value is the natural canonical; ask about every other one,
        # rarest first.
        subjects = [
            v for v in sorted(canon_counts, key=lambda v: (canon_counts[v], v))
            if v != ranked[0] and v not in seeded and _alias_candidate(v)
        ][:_MAX_ALIAS_QUESTIONS_PER_COLUMN]
        if not subjects:
            if sent:
                log.values_sent.append(column)
            continue
        state = _category_state(column, canon_counts)
        questions = {}
        for k, value in enumerate(subjects):
            opts = [v for v in ranked if v != value][:_MAX_CATEGORY_OPTIONS]
            criteria: dict[str, str | None] = {v: None for v in opts}
            criteria[DISTINCT] = f"{value!r} means something different from every listed value."
            questions[f"alias_{k}"] = Question.choice(
                f"In the data column {column!r}, is {value!r} another name, abbreviation, code "
                "or spelling of one of the listed values? Pick that value, or "
                f"{DISTINCT!r} if it is a distinct value.",
                criteria,
                context={"kind": "category_alias", "column": column, "value": value, "options": opts},
            )
        answers = asker.ask(_Batch("category_alias", column, state, questions))
        if answers or sent:
            log.values_sent.append(column)
        edges: list[tuple[float, str, str]] = []
        suggestions: list[str] = []
        for name, question in questions.items():
            answer = answers.get(name)
            if answer is None:
                continue
            value = question.context["value"]
            label, p = answer.decision()
            if label == DISTINCT:
                continue
            if p >= accept:
                edges.append((p, value, label))
            elif p >= 0.5:
                suggestions.append(f"{value!r} → {label!r} (p={p:.2f})")
                log.records.append(DecisionRecord("category_alias", column, value, label, p, "suggested"))
            else:
                log.records.append(DecisionRecord("category_alias", column, value, label, p, "rejected"))
        aliases = _resolve_aliases(edges, canon_counts)
        for p, value, label in edges:
            log.records.append(
                DecisionRecord("category_alias", column, value, label, p, "applied")
            )
        if aliases:
            applied = [p for p, _value, _label in edges]
            alias_map = {
                original: aliases[mapping.get(original, original)]
                for original in counts
                if mapping.get(original, original) in aliases
            }
            additions.append(
                Issue(
                    kind="category_alias",
                    message=f"{len(aliases)} value(s) are other names for existing categories "
                    f"({', '.join(f'{k!r} → {v!r}' for k, v in sorted(aliases.items())[:3])}"
                    + (", …" if len(aliases) > 3 else "") + "; decision)",
                    severity=Severity.WARNING,
                    column=column,
                    detector="decisions",
                    confidence=round(min(applied), 3),
                    evidence={"aliases": dict(sorted(aliases.items())), "decided_by": log.model},
                    proposal=Proposal(ops=[Op("normalize_values", {"map": dict(sorted(alias_map.items()))})]),
                )
            )
        if suggestions:
            additions.append(
                _suggestion(column, "Decision model suggests these are the same category "
                            f"(below the {accept:.2f} bar): {', '.join(suggestions[:5])}", {})
            )

    if not replacements and not additions:
        return issues
    result = Issues()
    for issue in issues:
        if id(issue) in replacements:
            new = replacements[id(issue)]
            if new is not None:
                result.append(new)
        else:
            result.append(issue)
    result.extend(additions)
    return result


def _alias_candidate(value: str) -> bool:
    from .detectors.categories import _skeleton

    if not value or len(value) > _MAX_ALIAS_VALUE_LENGTH:
        return False
    # Numbers and codes made of digits are values, not names.
    return not (_skeleton(value) and _skeleton(value) == value.replace(" ", ""))


def _category_state(column: str, counts: dict[str, int]) -> dict[str, Any]:
    ranked = sorted(counts, key=lambda v: (-counts[v], v))[:_MAX_CATEGORY_OPTIONS]
    return {
        "task": "Decide which spellings in a data column name the same category.",
        "column": column,
        "values": [{"value": v, "count": int(counts[v])} for v in ranked],
    }


def _rebuild_variant_issue(issue: Issue, mapping: dict[str, str], counts: dict[str, int],
                           model: str, confirmed: bool) -> Issue | None:
    if not mapping:
        return None
    distinct_after = len({mapping.get(v, v) for v in counts})
    confidence = max(issue.confidence, 0.9) if confirmed else issue.confidence
    return Issue(
        kind=issue.kind,
        message=f"{len(mapping)} value(s) are variants of other categories "
        f"({len(counts)} → {distinct_after} distinct; typo merges checked by decision)",
        severity=issue.severity,
        column=issue.column,
        detector=issue.detector,
        confidence=confidence,
        evidence={**issue.evidence, "mapping": mapping, "distinct_after": distinct_after,
                  "decided_by": model},
        proposal=Proposal(ops=[Op("normalize_values", {"map": mapping})]),
    )


def _resolve_aliases(
    edges: list[tuple[float, str, str]], counts: dict[str, int]
) -> dict[str, str]:
    """Fold accepted "``value`` is another name for ``label``" answers into a map.

    Each answer joins two values into one group. A group's canonical value follows
    the category detector's rule (most frequent, then nicest casing, then
    alphabetical), so the answers' direction - or their order - can never pick an
    abbreviation over the common spelling. Returns ``{value: canonical}`` for every
    value that changes.
    """
    from .detectors.categories import _canonical

    parent: dict[str, str] = {}

    def find(v: str) -> str:
        while parent.get(v, v) != v:
            v = parent[v]
        return v

    for _p, value, label in sorted(edges, key=lambda t: (-t[0], t[1], t[2])):
        a, b = find(value), find(label)
        if a != b:
            parent[max(a, b)] = min(a, b)  # any deterministic union; canonical is chosen below
    members: dict[str, list[str]] = {}
    for v in sorted({v for _p, value, label in edges for v in (value, label)}):
        members.setdefault(find(v), []).append(v)
    aliases: dict[str, str] = {}
    for group in members.values():
        canonical = _canonical({v: counts.get(v, 0) for v in group})
        for v in group:
            if v != canonical:
                aliases[v] = canonical
    return dict(sorted(aliases.items()))


def _suggestion(column: str | None, message: str, evidence: dict[str, Any]) -> Issue:
    return Issue(
        kind="decision_suggestion",
        message=message,
        severity=Severity.INFO,
        column=column,
        detector="decisions",
        confidence=0.0,
        evidence=evidence,
        proposal=None,
    )


__all__ = [
    "Answer",
    "DecisionClient",
    "DecisionError",
    "DecisionLog",
    "DecisionRecord",
    "DecisionResponse",
    "LLMDecisionClient",
    "Question",
    "RulesDecisionClient",
    "SystemOneClient",
    "get_decision_client",
    "parse_system_one_response",
    "refine_issues",
    "DEFAULT_DECISION_THRESHOLD",
    "DEFAULT_MAX_QUESTIONS",
]
