"""A custom op must reach replay, code export and streaming - not just the session
that registered it."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import warnings

import pandas as pd
import pytest

import cleanframe as cf
from cleanframe.ops import OP_REGISTRY

PLUGIN_SOURCE = textwrap.dedent(
    '''
    import re

    import cleanframe as cf

    _IBAN = re.compile(r"[^A-Za-z0-9]")


    def _codegen(params, column):
        return [f"df[{column!r}] = df[{column!r}].map(lambda v: re.sub('[^A-Za-z0-9]', '', v).upper() if isinstance(v, str) else v)"]


    @cf.register_op("normalize_iban_demo", codegen=_codegen, streamable=True)
    def normalize_iban_demo(series):
        """Strip separators and upper-case an IBAN."""
        return series.map(lambda v: _IBAN.sub("", v).upper() if isinstance(v, str) else v)


    @cf.detector("iban_demo")
    def detect_iban_demo(series, ctx):
        issues = cf.Issues()
        values = [v for v in series.dropna().astype(str) if re.fullmatch(r"[A-Z]{2}\\d{2}[ A-Za-z0-9]{10,30}", v.strip(), re.I)]
        if len(values) >= 2 and len(values) == series.notna().sum():
            issues.add(
                "iban_format",
                "IBAN values with separators or lower case",
                confidence=0.95,
                ops=[cf.Op("normalize_iban_demo", {})],
            )
        return issues
    '''
)

RECIPE = "version: 1\ncolumns:\n  iban:\n    ops: [normalize_iban_demo]\n"


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    (tmp_path / "iban_demo_plugin.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield tmp_path
    OP_REGISTRY.pop("normalize_iban_demo", None)
    from cleanframe.detectors.base import DETECTOR_REGISTRY
    from cleanframe.plugins import _LOADED

    DETECTOR_REGISTRY.pop("iban_demo", None)
    _LOADED.discard("iban_demo_plugin")
    sys.modules.pop("iban_demo_plugin", None)


def test_plugin_op_is_exported_and_streams(plugin, tmp_path):
    cf.load_plugins(["iban_demo_plugin"], discover=False)
    rec = cf.Recipe.from_yaml(RECIPE)
    df = pd.DataFrame({"iban": ["de89 3704 0044 0532 0130 00", "gb29-nwbk-6016-1331-9268-19"]})

    expected = cf.apply_recipe(df, rec, check_drift=False).dataframe["iban"].tolist()
    assert expected == ["DE89370400440532013000", "GB29NWBK60161331926819"]

    ns: dict = {}
    exec(compile(cf.generate_code(rec), "<generated>", "exec"), ns)
    assert ns["clean"](df)["iban"].tolist() == expected

    cf.check_streamable(rec)  # opted in with streamable=True
    src, out = tmp_path / "in.csv", tmp_path / "out.csv"
    df.to_csv(src, index=False)
    cf.stream_apply(rec, src, out, chunksize=1, check_drift=False)
    assert pd.read_csv(out)["iban"].tolist() == expected


def test_plugin_op_without_hooks_is_refused_not_mishandled():
    @cf.register_op("plain_plugin_op_demo")
    def plain(series):
        return series

    try:
        rec = cf.Recipe.from_yaml("version: 1\ncolumns:\n  a:\n    ops: [plain_plugin_op_demo]\n")
        with pytest.raises(cf.CleanFrameError, match="plain_plugin_op_demo"):
            cf.generate_code(rec)
        with pytest.raises(cf.CleanFrameError):
            cf.check_streamable(rec)
    finally:
        OP_REGISTRY.pop("plain_plugin_op_demo", None)


def test_a_detector_in_a_plugin_plans_end_to_end(plugin):
    cf.load_plugins(["iban_demo_plugin"], discover=False)
    df = pd.DataFrame({"iban": ["DE89 3704 0044 0532 0130 00", "gb29 nwbk 6016 1331 9268 19"]})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = cf.clean(df)
    assert "normalize_iban_demo" in result.recipe.to_yaml()
    assert result.dataframe["iban"].tolist() == ["DE89370400440532013000", "GB29NWBK60161331926819"]


def _cli(args, cwd, env_extra=None):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", **(env_extra or {})}
    return subprocess.run(
        [sys.executable, "-m", "cleanframe", *args],
        cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
    )


def test_cli_apply_needs_the_plugin_and_finds_it_by_flag_or_environment(plugin, tmp_path):
    (tmp_path / "in.csv").write_text("iban\nde89 3704 0044 0532 0130 00\n", encoding="utf-8")
    (tmp_path / "r.yaml").write_text(RECIPE, encoding="utf-8")
    base = ["apply", "in.csv", "--recipe", "r.yaml", "--no-drift-check", "--overwrite"]
    pypath = {"PYTHONPATH": str(plugin) + os.pathsep + os.environ.get("PYTHONPATH", "")}

    missing = _cli([*base, "--out", "a.csv"], tmp_path, pypath)
    assert missing.returncode == 1
    assert "--plugin" in missing.stderr

    by_flag = _cli([*base, "--out", "b.csv", "--plugin", "iban_demo_plugin"], tmp_path, pypath)
    assert by_flag.returncode == 0, by_flag.stderr
    assert "DE89370400440532013000" in (tmp_path / "b.csv").read_text(encoding="utf-8")

    by_env = _cli([*base, "--out", "c.csv"], tmp_path, {**pypath, "CLEANFRAME_PLUGINS": "iban_demo_plugin"})
    assert by_env.returncode == 0, by_env.stderr

    disabled = _cli(
        [*base, "--out", "d.csv"], tmp_path,
        {**pypath, "CLEANFRAME_PLUGINS": "iban_demo_plugin", "CLEANFRAME_NO_PLUGINS": "1"},
    )
    assert disabled.returncode == 1


def test_a_broken_plugin_names_itself(tmp_path, monkeypatch):
    (tmp_path / "broken_cf_plugin.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(cf.CleanFrameError, match="broken_cf_plugin.*boom"):
        cf.load_plugins(["broken_cf_plugin"], discover=False)
