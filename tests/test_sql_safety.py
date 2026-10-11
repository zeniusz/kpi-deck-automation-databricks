"""SQL building helpers: identifiers and literals must be escaped, staging names path-safe."""

import excel_ingestion as ei


def test_quote_ident_escapes_backticks():
    assert ei.quote_ident("Full Name") == "`Full Name`"
    assert ei.quote_ident("a`b") == "`a``b`"


def test_sql_literal_escapes_quotes_and_backslashes():
    assert ei.sql_literal(None) == "NULL"
    assert ei.sql_literal(5) == "5"
    assert ei.sql_literal(True) == "TRUE"
    assert ei.sql_literal("O'Neil") == "'O''Neil'"
    backslash = chr(92)
    assert ei.sql_literal(f"a{backslash}'; DROP") == f"'a{backslash}{backslash}''; DROP'"


def test_safe_stem_strips_path_and_quote_characters():
    assert ei.safe_stem("Analytics Community (2026.07.03).xlsx") == "Analytics_Community_2026.07.03"
    assert ei.safe_stem("x'); DROP--.xlsx") == "x_DROP--"
    assert ei.safe_stem("...xlsx") == "upload"
