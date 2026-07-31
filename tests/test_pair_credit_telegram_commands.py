import sys
import types as module_types

if "google.genai" not in sys.modules:
    google_mod = module_types.ModuleType("google")
    genai_mod = module_types.ModuleType("google.genai")
    genai_types = module_types.SimpleNamespace(
        Tool=object,
        Schema=lambda **kwargs: kwargs,
        FunctionDeclaration=lambda **kwargs: kwargs,
    )
    genai_mod.types = genai_types
    google_mod.genai = genai_mod
    sys.modules.setdefault("google", google_mod)
    sys.modules.setdefault("google.genai", genai_mod)

from main import BlitzTrader


def test_pair_credit_exit_parser_accepts_natural_pair_variants():
    assert BlitzTrader._extract_pair_credit_exit_serial("exit #1") == 1
    assert BlitzTrader._extract_pair_credit_exit_serial("exit pair #1") == 1
    assert BlitzTrader._extract_pair_credit_exit_serial("exit position 2") == 2
    assert BlitzTrader._extract_pair_credit_exit_serial("exit spread #3") == 3
    assert BlitzTrader._extract_pair_credit_exit_serial("exit serial 4") == 4
    assert BlitzTrader._extract_pair_credit_exit_serial("close pair #5") == 5
    assert BlitzTrader._extract_pair_credit_exit_serial("square off pair #6") == 6


def test_pair_credit_exit_parser_ignores_non_exit_status_text():
    assert BlitzTrader._extract_pair_credit_exit_serial("pnl?") is None
    assert BlitzTrader._extract_pair_credit_exit_serial("positions") is None
