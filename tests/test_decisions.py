"""Typed decisions (``clean(decide=...)``): policy, privacy, determinism, backends."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pandas as pd
import pytest

import cleanframe as cf
from cleanframe.decisions import (
    CHOICE,
    DISTINCT,
    NO_MATCH,
    NOUL,
    Answer,
    DecisionError,
    DecisionResponse,
    LLMDecisionClient,
    Question,
    RulesDecisionClient,
    SystemOneClient,
    _resolve_aliases,
    get_decision_client,
)
from cleanframe.errors import CleanFrameError
from cleanframe.llm import LLMResponse
from cleanframe.recipe import Recipe

ALIASES = {"BLR": "Bangalore", "Bombay": "Mumbai"}


def _city_df() -> pd.DataFrame:
    cities = (
        ["Bangalore"] * 8 + ["BLR"] * 2 + ["Mumbai"] * 6 + ["Bombay"] * 2
        + ["Delhi"] * 4 + ["Banglore"]
    )
    middles = ["-", "K", "R", "-", "S", "T", "U", "V", "W", "X", "Y"] * 2 + ["Z"]
    return pd.DataFrame({"City": cities, "Middle": middles[: len(cities)]})


class Scripted:
    """A fake decision backend: answers each question kind with fixed probabilities."""

    calibrated = True

    def __init__(self, alias=0.97, merge=0.95, null=0.96, schema=None, model="fake-jev"):
        self.alias, self.merge, self.null = alias, merge, null
        self.schema = schema or {}
        self.model = model
        self.calls: list[tuple[str, list[str]]] = []

    def decide(self, state, questions):
        self.calls.append(
            (json.dumps(state, sort_keys=True), [json.dumps(q.to_wire(), sort_keys=True) for q in questions.values()])
        )
        answers = {}
        for name, q in questions.items():
            ctx = q.context
            kind = ctx["kind"]
            if kind == "category_alias":
                target = ALIASES.get(ctx["value"])
                answers[name] = _choice(q, target if target in q.labels else DISTINCT, self.alias)
            elif kind == "category_merge":
                answers[name] = Answer(NOUL, {"yes": self.merge, "no": 1 - self.merge})
            elif kind == "null_token":
                answers[name] = Answer(NOUL, {"yes": self.null, "no": 1 - self.null})
            elif kind == "schema_match":
                label, p = self.schema.get(ctx["target"], (NO_MATCH, 0.9))
                answers[name] = _choice(q, label, p)
        return DecisionResponse(answers, model=self.model, input_tokens=10, output_tokens=1)


def _choice(question: Question, label: str, p: float) -> Answer:
    rest = (1 - p) / max(1, len(question.labels) - 1)
    probs = {lbl: (p if lbl == label else rest) for lbl in question.labels}
    return Answer(CHOICE, probs, choice=label, confidence=p)


def _clean(df, **kw):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return cf.clean(df, **kw)


def _ops(recipe: Recipe, column: str) -> list:
    col = recipe.column(column)
    return [] if col is None else [(op.name, op.params) for op in col.ops]


# --------------------------------------------------------------------- invariants
def test_without_decide_nothing_changes_and_nothing_is_imported():
    code = (
        "import sys, warnings, pandas as pd, cleanframe as cf\n"
        "warnings.simplefilter('ignore')\n"
        "cf.clean(pd.DataFrame({'a': ['x', 'y', 'x']}))\n"
        "print('cleanframe.decisions' in sys.modules)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
    df = _city_df()
    assert _clean(df).recipe.to_yaml() == _clean(df, decide=None).recipe.to_yaml()


def test_alias_applied_only_above_the_bar_and_frozen_into_the_recipe():
    df = _city_df()
    result = _clean(df, decide=Scripted(), llm_exposure="sample")
    ops = _ops(result.recipe, "City")
    alias_ops = [p["map"] for name, p in ops if name == "normalize_values" and "BLR" in p["map"]]
    assert alias_ops and alias_ops[0]["BLR"] == "Bangalore" and alias_ops[0]["Bombay"] == "Mumbai"
    cities = set(result.dataframe["city"])
    assert cities == {"Bangalore", "Mumbai", "Delhi"}
    meta = result.recipe.meta["decisions"]
    assert meta["model"] == "fake-jev" and meta["calibrated"] is True
    applied = {r["subject"] for r in meta["records"] if r["kind"] == "category_alias" and r["outcome"] == "applied"}
    assert applied == {"BLR", "Bombay"}
    # Replay never calls the backend and reproduces the data.
    replay = cf.apply_recipe(df, result.recipe, check_drift=False).dataframe
    assert replay["city"].tolist() == result.dataframe["city"].tolist()


def test_alias_below_the_bar_is_a_suggestion_not_a_change():
    df = _city_df()
    result = _clean(df, decide=Scripted(alias=0.8), llm_exposure="sample")
    assert "BLR" in set(result.dataframe["city"])
    records = result.recipe.meta["decisions"]["records"]
    assert {r["subject"] for r in records if r["outcome"] == "suggested"} >= {"BLR", "Bombay"}
    assert any(i.kind == "decision_suggestion" and not i.has_fix for i in result.issues)


def test_mode_threshold_wins_when_higher_than_decision_threshold():
    df = _city_df()
    result = _clean(df, decide=Scripted(alias=0.6), llm_exposure="sample", decision_threshold=0.5)
    # review mode's own threshold is 0.5, so 0.6 is applied ...
    assert "BLR" not in set(result.dataframe["city"])
    strict = _clean(df, decide=Scripted(alias=0.6), llm_exposure="sample", decision_threshold=0.5, mode="strict")
    # ... but strict mode needs 0.85.
    assert "BLR" in set(strict.dataframe["city"])


def test_metadata_exposure_never_sends_category_values():
    df = _city_df()
    backend = Scripted()
    result = _clean(df, decide=backend)  # default exposure: metadata
    sent = " ".join(state + " ".join(qs) for state, qs in backend.calls)
    for value in ("BLR", "Bombay", "Bangalore", "Mumbai", "Banglore"):
        assert value not in sent
    assert any("llm_exposure='sample'" in n for n in result.recipe.meta["decisions"]["notes"])
    assert "BLR" in set(result.dataframe["city"])


def test_null_token_question_only_carries_vocabulary_tokens():
    df = _city_df()
    backend = Scripted()
    result = _clean(df, decide=backend)
    sent = " ".join(state + " ".join(qs) for state, qs in backend.calls)
    for value in ("K", "R", "S"):  # real values of the Middle column
        assert f"'{value}'" not in sent and f'"{value}"' not in sent
    assert result.dataframe["middle"].isna().sum() >= 2


def test_null_token_kept_when_the_model_says_it_is_real_data():
    df = _city_df()
    result = _clean(df, decide=Scripted(null=0.2))
    assert "-" in set(result.dataframe["middle"])
    kept = [r for r in result.recipe.meta["decisions"]["records"] if r["kind"] == "null_token"]
    assert kept and kept[0]["outcome"] == "kept"


def test_typo_merge_veto_keeps_values_apart():
    df = _city_df()
    result = _clean(df, decide=Scripted(merge=0.2), llm_exposure="sample")
    assert "Banglore" in set(result.dataframe["city"])
    records = result.recipe.meta["decisions"]["records"]
    assert any(r["kind"] == "category_merge" and r["outcome"] == "vetoed" for r in records)


def test_confirmed_typo_merge_survives_strict_mode():
    df = _city_df()
    plain_strict = _clean(df, mode="strict")
    assert "Banglore" in set(plain_strict.dataframe["city"])  # 0.7 fuzzy merge is gated out
    decided = _clean(df, decide=Scripted(), llm_exposure="sample", mode="strict")
    assert "Banglore" not in set(decided.dataframe["city"])


def test_schema_match_and_veto():
    df = pd.DataFrame(
        {
            "Amt (INR)": ["₹1,200", "₹300", "₹45"] * 3,
            "cust": ["Asha", "Ravi", "Mira"] * 3,
        }
    )
    schema = {"columns": {"revenue": {"dtype": "float"}, "customer_name": {"dtype": "string"}}}
    backend = Scripted(schema={"revenue": ("Amt (INR)", 0.95), "customer_name": ("cust", 0.97)})
    result = _clean(df, target_schema=schema, decide=backend)
    assert {"revenue", "customer_name"} <= set(result.dataframe.columns)
    # The schema question saw names and patterns, never the cell values.
    sent = " ".join(state + " ".join(qs) for state, qs in backend.calls)
    assert "Asha" not in sent and "1,200" not in sent
    vetoed = _clean(df, target_schema=schema, decide=Scripted(schema={}))
    assert "revenue" not in vetoed.dataframe.columns


def test_a_column_already_named_like_a_schema_field_is_never_offered():
    df = pd.DataFrame({"email": ["a@x.io", "b@y.io", "c@z.io"], "misc": ["1", "2", "3"]})
    schema = {"columns": {"email": {"dtype": "email"}, "contact": {"dtype": "email"}}}
    backend = Scripted(schema={"contact": ("email", 0.99)})
    result = _clean(df, target_schema=schema, decide=backend)
    assert "email" in result.dataframe.columns and "contact" not in result.dataframe.columns
    offered = [json.loads(q)["criteria"] for _state, qs in backend.calls for q in qs]
    assert offered and all("email" not in criteria for criteria in offered)


def test_two_targets_cannot_claim_one_source():
    df = pd.DataFrame({"Amt": ["1", "2", "3"], "other": ["a", "b", "c"]})
    schema = {"columns": {"revenue": {"dtype": "float"}, "income": {"dtype": "float"}}}
    backend = Scripted(schema={"revenue": ("Amt", 0.93), "income": ("Amt", 0.97)})
    result = _clean(df, target_schema=schema, decide=backend)
    assert "income" in result.dataframe.columns and "revenue" not in result.dataframe.columns


# --------------------------------------------------------------------- determinism
def test_shuffled_rows_ask_the_same_questions_and_plan_the_same_recipe():
    df = _city_df()
    shuffled = df.sample(frac=1.0, random_state=7).reset_index(drop=True)
    a, b = Scripted(), Scripted()
    ra = _clean(df, decide=a, llm_exposure="sample").recipe.to_dict()
    rb = _clean(shuffled, decide=b, llm_exposure="sample").recipe.to_dict()
    ra.pop("source_fingerprint"), rb.pop("source_fingerprint")
    assert ra == rb
    assert a.calls == b.calls


def test_decision_meta_round_trips_byte_identically():
    result = _clean(_city_df(), decide=Scripted(alias=0.9137), llm_exposure="sample")
    text = result.recipe.to_yaml()
    again = Recipe.from_yaml(text).to_yaml()
    assert again == text
    for record in result.recipe.meta["decisions"]["records"]:
        assert type(record["p"]) is float and record["p"] == round(record["p"], 3)


# --------------------------------------------------------------------- limits & failures
def test_max_decisions_bounds_the_questions():
    backend = Scripted()
    result = _clean(_city_df(), decide=backend, llm_exposure="sample", max_decisions=2)
    meta = result.recipe.meta["decisions"]
    assert meta["asked"] == 2 and meta["skipped"] >= 1
    assert any("max_decisions=2" in n for n in meta["notes"])


class Broken:
    model = "broken"
    calibrated = True

    def decide(self, state, questions):
        raise ConnectionError("boom")


def test_backend_failure_falls_back_to_rules_with_a_warning():
    df = _city_df()
    with pytest.warns(cf.CleanFrameWarning, match="decision backend failed"):
        result = cf.clean(df, decide=Broken(), llm_exposure="sample")
    plain = _clean(df).recipe.to_dict()
    got = result.recipe.to_dict()
    assert got["meta"].pop("decisions")["error"] == "boom"
    assert got == plain


def test_backend_failure_raises_without_fallback():
    with pytest.raises(DecisionError, match="boom"):
        _clean(_city_df(), decide=Broken(), llm_exposure="sample", llm_fallback=False)


def test_decide_with_llm_is_refused():
    with pytest.raises(CleanFrameError, match="cannot be combined"):
        cf.clean(_city_df(), decide=Scripted(), llm="openai/gpt-4o")


def test_exposure_none_asks_nothing():
    backend = Scripted()
    with pytest.warns(cf.CleanFrameWarning, match="decision backend was not called"):
        cf.clean(_city_df(), decide=backend, llm_exposure="none")
    assert backend.calls == []


@pytest.mark.parametrize("bad", [-0.1, 1.5, "high", True])
def test_bad_threshold_is_refused(bad):
    with pytest.raises(CleanFrameError, match="decision_threshold"):
        cf.clean(_city_df(), decide=Scripted(), decision_threshold=bad)


# --------------------------------------------------------------------- spec resolution
def test_specs_read_only_their_own_keys(monkeypatch):
    for var in ("TYPESAFE_API_KEY", "TYPESAFE_BASE_URL", "OPENROUTER_API_KEY", "SYSTEMONE_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-must-not-leak")
    ts = get_decision_client("typesafe/jev-1.14")
    assert isinstance(ts, SystemOneClient) and ts.base_url == "https://api.typesafe.ai"
    assert ts._api_key is None
    with pytest.raises(DecisionError, match="TYPESAFE_API_KEY"):
        ts.decide("s", {"q": Question.noul("ok?")})
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    orc = get_decision_client("openrouter/typesafe/jev-1.14")
    assert isinstance(orc, SystemOneClient)
    assert orc.base_url == "https://openrouter.ai/api" and orc.model == "typesafe/jev-1.14"
    assert orc._api_key == "or-key"
    with pytest.raises(DecisionError, match="SYSTEMONE_BASE_URL"):
        get_decision_client("systemone/imajev-4b")
    assert isinstance(get_decision_client("rules"), RulesDecisionClient)
    assert isinstance(get_decision_client("openai/gpt-4o-mini"), LLMDecisionClient)
    with pytest.raises(DecisionError):
        get_decision_client("jev")
    with pytest.raises(CleanFrameError):
        get_decision_client("nonsense/model")
    assert "or-key" not in repr(orc)


def test_plain_http_to_a_remote_host_warns():
    with pytest.warns(cf.CleanFrameWarning, match="unencrypted"):
        SystemOneClient("m", base_url="http://decisions.example.com")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        SystemOneClient("m", base_url="http://127.0.0.1:9")
        SystemOneClient("m", base_url="https://decisions.example.com")
    with pytest.raises(DecisionError):
        SystemOneClient("m", base_url="ftp://x")


# --------------------------------------------------------------------- the wire client
class _Server:
    """A local System One server that replays scripted (status, body, headers) replies."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests: list[dict] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", 0))
                server.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": json.loads(self.rfile.read(length))}
                )
                status, body, headers = server.replies.pop(0)
                payload = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


QUESTIONS = {
    "pick": Question.choice("Which?", {"a": None, "b": "the second"}),
    "yes": Question.noul("Is it?", true="it is"),
    "rate": Question.score("How bad?", ["fine", "bad", "awful"]),
}

GOOD = {
    "model": "jev-1.14.0",
    "answers": {
        "pick": {"type": "choice", "choice": "b", "confidence": 0.8, "probabilities": {"a": 0.1, "b": 0.9}},
        "yes": {"type": "noul", "noul": 0.97},
        "rate": {"type": "score", "score": 1.7, "confidence": 0.7, "legend": {}, "probabilities": {"0": 0.1, "1": 0.1, "2": 0.8}},
    },
    "usage": {"input_tokens": 120, "output_tokens": 3, "cost": 0.000005},
}


def test_wire_client_parses_all_answer_types_and_sends_the_contract():
    with _Server([(200, GOOD, None)]) as srv:
        client = SystemOneClient("jev-latest", base_url=srv.url, api_key="secret-key")
        resp = client.decide({"text": "x"}, QUESTIONS)
    sent = srv.requests[0]
    assert sent["path"] == "/v1/systemone"
    assert sent["headers"]["Authorization"] == "Bearer secret-key"
    assert sent["body"]["model"] == "jev-latest" and sent["body"]["state"] == {"text": "x"}
    assert sent["body"]["questions"]["yes"] == {"type": "noul", "instructions": "Is it?", "criteria": {"true": "it is"}}
    assert "context" not in json.dumps(sent["body"])
    assert resp.model == "jev-1.14.0" and resp.input_tokens == 120 and resp.cost == pytest.approx(5e-6)
    assert resp.answers["pick"].decision() == ("b", 0.8)  # min(probability, confidence)
    assert resp.answers["yes"].p_yes == pytest.approx(0.97)
    assert resp.answers["rate"].probabilities["2"] == pytest.approx(0.8)


def test_wire_client_drops_answers_that_do_not_fit():
    body = {
        "answers": {
            "pick": {"type": "choice", "choice": "zzz", "confidence": 0.9, "probabilities": {"zzz": 1.0}},
            "yes": {"type": "choice", "choice": "a", "confidence": 1, "probabilities": {"a": 1}},
            "rate": {"type": "score", "abstained": True, "probabilities": {"0": 1}},
        },
        "usage": {},
    }
    with _Server([(200, body, None)]) as srv:
        resp = SystemOneClient("m", base_url=srv.url).decide("s", QUESTIONS)
    assert resp.answers == {}


def test_wire_client_retries_429_then_succeeds():
    with _Server([(429, {"error": "slow down"}, {"retry-after": "0"}), (200, GOOD, None)]) as srv:
        resp = SystemOneClient("m", base_url=srv.url, retry_backoff=0).decide("s", QUESTIONS)
    assert len(srv.requests) == 2 and "pick" in resp.answers


def test_wire_client_gives_up_after_retries_without_leaking_the_key():
    with _Server([(500, {"error": "down"}, None)] * 3) as srv:
        client = SystemOneClient("m", base_url=srv.url, api_key="secret-key", retry_backoff=0)
        with pytest.raises(DecisionError) as info:
            client.decide("s", QUESTIONS)
    assert "HTTP 500" in str(info.value) and "secret-key" not in str(info.value)
    assert len(srv.requests) == 3


def test_wire_client_rejects_non_json_and_shapeless_responses():
    with _Server([(200, b"<html>oops</html>", None), (200, {"nope": 1}, None)]) as srv:
        client = SystemOneClient("m", base_url=srv.url, max_retries=0)
        with pytest.raises(DecisionError, match="not JSON"):
            client.decide("s", QUESTIONS)
        with pytest.raises(DecisionError, match="answers"):
            client.decide("s", QUESTIONS)


def test_cli_clean_with_a_local_system_one_server(tmp_path, monkeypatch):
    data = tmp_path / "in.csv"
    _city_df().to_csv(data, index=False)
    replies = []
    for _ in range(10):
        replies.append((200, {"answers": {f"null_{i}": {"type": "noul", "noul": 0.99} for i in range(3)}, "usage": {}}, None))
    with _Server(replies) as srv:
        monkeypatch.setenv("SYSTEMONE_BASE_URL", srv.url)
        out = subprocess.run(
            [sys.executable, "-m", "cleanframe", "clean", str(data), "--decide", "systemone/local",
             "--recipe", str(tmp_path / "r.yaml"), "--json"],
            capture_output=True, text=True, encoding="utf-8",
            env={**__import__("os").environ, "SYSTEMONE_BASE_URL": srv.url, "PYTHONIOENCODING": "utf-8"},
        )
    assert out.returncode == 0, out.stderr
    summary = json.loads(out.stdout)
    assert summary["decisions"]["model"] == "local" and summary["decisions"]["applied"] >= 1
    recipe = Recipe.load(tmp_path / "r.yaml")
    assert ["-"] in [op.params.get("tokens") for op in recipe.column("Middle").ops if op.name == "to_na"]


# --------------------------------------------------------------------- other backends
class FakeLLM:
    model = "fake-llm"

    def __init__(self, text):
        self.text = text

    def complete(self, system, user, *, max_tokens=2048):
        self.user = user
        return LLMResponse(self.text, 50, 5)


def test_llm_adapter_normalises_probabilities_and_is_uncalibrated():
    reply = json.dumps(
        {"answers": {"pick": {"probabilities": {"a": 2, "b": 6, "junk": 9}}, "yes": {"probabilities": {"yes": 0.7}}}}
    )
    client = LLMDecisionClient(FakeLLM("Sure! " + reply))
    resp = client.decide("state", QUESTIONS)
    assert client.calibrated is False and resp.model == "llm:fake-llm"
    assert resp.answers["pick"].choice == "b" and resp.answers["pick"].probabilities["b"] == pytest.approx(0.75)
    assert resp.answers["yes"].p_yes == pytest.approx(1.0)  # "no" omitted counts as 0
    assert "rate" not in resp.answers
    with pytest.raises(DecisionError):
        LLMDecisionClient(FakeLLM("no json here")).decide("state", QUESTIONS)


def test_rules_backend_answers_like_the_detectors():
    client = RulesDecisionClient()
    q_merge = Question.noul("same?", context={"kind": "category_merge", "variant": "Banglore", "canonical": "Bangalore"})
    q_alias = Question.choice(
        "alias?", {"Bengaluru": None, "Mumbai": None, DISTINCT: None},
        context={"kind": "category_alias", "value": "Bangalore", "options": ["Bengaluru", "Mumbai"]},
    )
    resp = client.decide("s", {"m": q_merge, "a": q_alias})
    assert resp.answers["m"].p_yes == pytest.approx(0.7)  # the rules' own typo-merge confidence
    assert resp.answers["a"].choice == DISTINCT  # rules never map aliases


@pytest.mark.parametrize("exposure", ["metadata", "sample"])
def test_decide_rules_plans_what_plain_rules_plan(exposure):
    frames = [
        (_city_df(), None),
        (
            pd.DataFrame({"Dept": ["HR", "IT", "HR"] * 3, "Emp Code": ["E1", "E2", "E3"] * 3}),
            {"columns": {"department": {"dtype": "string"}, "employee_id": {"dtype": "string"}}},
        ),
    ]
    for df, schema in frames:
        plain = _clean(df, target_schema=schema).recipe.to_dict()
        ruled = _clean(df, target_schema=schema, decide="rules", llm_exposure=exposure).recipe.to_dict()
        assert ruled["meta"].pop("decisions")["model"] == "rules"
        assert ruled == plain


def test_alias_groups_take_the_most_frequent_spelling_whatever_the_direction():
    counts = {"Bangalore": 10, "BLR": 3, "Bengaluru": 2}
    edges = [(0.99, "Bangalore", "BLR"), (0.95, "BLR", "Bengaluru")]
    assert _resolve_aliases(edges, counts) == {"BLR": "Bangalore", "Bengaluru": "Bangalore"}
    # A cycle (A -> B and B -> A) is one group, not a loop.
    assert _resolve_aliases([(0.9, "A", "B"), (0.9, "B", "A")], {"A": 1, "B": 5}) == {"A": "B"}


def test_question_wire_form_never_contains_context():
    q = Question.noul("x?", context={"secret": "value"})
    assert q.to_wire() == {"type": "noul", "instructions": "x?"}
    with pytest.raises(DecisionError):
        Question.choice("x?", {})
