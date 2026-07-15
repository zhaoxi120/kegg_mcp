"""Unit tests for lossless KEGG MODULE tokenization and parsing."""

from unittest.mock import patch

import pytest

from kegg_mcp.analysis import module_syntax
from kegg_mcp.analysis.contracts import (
    ModuleDiagnosticCode,
    ModuleDiagnosticSeverity,
    ModuleExpression,
    ModuleExpressionKind,
    ModuleOperatorKind,
    ModuleParseLimits,
    ModuleParseResult,
    ModuleToken,
    ModuleTokenKind,
    SourceSpan,
)
from kegg_mcp.analysis.module_syntax import (
    parse_module_definition,
    tokenize_module_definition,
)


def _only_child(expression: ModuleExpression) -> ModuleExpression:
    assert len(expression.children) == 1
    return expression.children[0]


def test_tokenizer_is_lossless_and_uses_code_point_spans_for_utf8_and_crlf() -> None:
    definition = "\u03b1 K00001\r\nM00002"

    tokens = tokenize_module_definition(definition)

    assert "".join(token.lexeme for token in tokens) == definition
    assert [token.kind for token in tokens] == [
        ModuleTokenKind.UNSUPPORTED,
        ModuleTokenKind.WHITESPACE,
        ModuleTokenKind.KO,
        ModuleTokenKind.WHITESPACE,
        ModuleTokenKind.MODULE_REFERENCE,
    ]
    assert (tokens[0].span.start_offset, tokens[0].span.end_offset) == (0, 1)
    assert (tokens[-1].span.start_offset, tokens[-1].span.end_offset) == (10, 16)
    assert (tokens[-1].span.start_line, tokens[-1].span.start_column) == (2, 1)
    assert (tokens[-1].span.end_line, tokens[-1].span.end_column) == (2, 7)


@pytest.mark.parametrize(
    "definition",
    [
        "K00001suffix",
        "prefixK00001",
        "K00001/K00002",
        "k00001",
        "K\uff10\uff10\uff10\uff10\uff11",
    ],
)
def test_tokenizer_retains_each_malformed_delimiter_chunk_as_one_unsupported_token(
    definition: str,
) -> None:
    tokens = tokenize_module_definition(definition)

    assert len(tokens) == 1
    assert tokens[0].kind is ModuleTokenKind.UNSUPPORTED
    assert tokens[0].lexeme == definition


def test_non_ascii_whitespace_is_unsupported_instead_of_a_logical_separator() -> None:
    definition = "K00001\u00a0K00002"

    result = parse_module_definition(definition)

    assert [token.kind for token in result.tokens] == [
        ModuleTokenKind.KO,
        ModuleTokenKind.UNSUPPORTED,
        ModuleTokenKind.KO,
    ]
    assert result.tokens[1].lexeme == "\u00a0"
    assert "".join(token.lexeme for token in result.tokens) == definition
    assert result.ast is None
    assert _diagnostic_codes_for_result(result) == {
        ModuleDiagnosticCode.UNSUPPORTED_TOKEN,
        ModuleDiagnosticCode.UNEXPECTED_TOKEN,
    }


@pytest.mark.parametrize("definition", ["\u00a0", "\u2028", "\v", "\f"])
def test_unsupported_whitespace_only_input_is_not_treated_as_empty(definition: str) -> None:
    result = parse_module_definition(definition)

    assert result.is_valid is True
    assert result.ast is not None
    assert result.tokens[0].kind is ModuleTokenKind.UNSUPPORTED
    assert _diagnostic_codes_for_result(result) == {ModuleDiagnosticCode.UNSUPPORTED_TOKEN}


def test_parser_preserves_top_level_blocks_and_nested_or_and_precedence() -> None:
    result = parse_module_definition("(K00001,K00002+K00003) K00004")

    assert result.is_valid is True
    assert result.diagnostics == ()
    assert result.ast is not None
    assert len(result.ast.required_blocks) == 2
    assert len(result.ast.block_separators) == 1
    assert result.ast.block_separators[0].kind is ModuleOperatorKind.SPACE

    group, final_block = result.ast.required_blocks
    assert group.kind is ModuleExpressionKind.GROUP
    alternative = _only_child(group)
    assert alternative.kind is ModuleExpressionKind.OR
    assert [child.kind for child in alternative.children] == [
        ModuleExpressionKind.KO,
        ModuleExpressionKind.AND,
    ]
    complex_branch = alternative.children[1]
    assert [operator.kind for operator in complex_branch.operators] == [ModuleOperatorKind.PLUS]
    assert final_block.kind is ModuleExpressionKind.KO
    assert final_block.value == "K00004"


def test_parser_handles_nested_space_and_inside_an_or_branch() -> None:
    definition = "(K00001+(K00002,K00003 K00004),K00005+K00006) K00007"

    result = parse_module_definition(definition)

    assert result.is_valid is True
    assert result.ast is not None
    outer_or = _only_child(result.ast.required_blocks[0])
    assert outer_or.kind is ModuleExpressionKind.OR
    first_branch = outer_or.children[0]
    assert first_branch.kind is ModuleExpressionKind.AND
    nested_group = first_branch.children[1]
    nested_or = _only_child(nested_group)
    assert nested_or.kind is ModuleExpressionKind.OR
    nested_and = nested_or.children[1]
    assert nested_and.kind is ModuleExpressionKind.AND
    assert [operator.kind for operator in nested_and.operators] == [ModuleOperatorKind.SPACE]


def test_parser_preserves_consecutive_optional_terms_and_optional_groups() -> None:
    definition = "K00001+K00002-K00003-(K00004,K00005) K00006"

    result = parse_module_definition(definition)

    assert result.is_valid is True
    assert result.ast is not None
    first_block = result.ast.required_blocks[0]
    assert first_block.kind is ModuleExpressionKind.AND
    assert [operator.kind for operator in first_block.operators] == [
        ModuleOperatorKind.PLUS,
        ModuleOperatorKind.MINUS,
        ModuleOperatorKind.MINUS,
    ]
    first_optional, grouped_optional = first_block.children[2:]
    assert first_optional.kind is ModuleExpressionKind.OPTIONAL
    assert _only_child(first_optional).value == "K00003"
    assert grouped_optional.kind is ModuleExpressionKind.OPTIONAL
    optional_group = _only_child(grouped_optional)
    assert optional_group.kind is ModuleExpressionKind.GROUP
    assert _only_child(optional_group).kind is ModuleExpressionKind.OR
    assert grouped_optional.span.start_offset == definition.index("-(")


def test_parser_treats_whitespace_around_explicit_operators_as_formatting() -> None:
    definition = "K00001\n +\n K00002,\n K00003"

    result = parse_module_definition(definition)

    assert result.is_valid is True
    assert result.ast is not None
    assert len(result.ast.required_blocks) == 1
    alternative = result.ast.required_blocks[0]
    assert alternative.kind is ModuleExpressionKind.OR
    assert alternative.children[0].kind is ModuleExpressionKind.AND
    assert alternative.children[0].operators[0].kind is ModuleOperatorKind.PLUS
    assert "".join(token.lexeme for token in result.tokens) == definition


def test_parser_uses_inner_whitespace_as_and_but_outer_whitespace_as_blocks() -> None:
    result = parse_module_definition("(K00001 K00002) K00003")

    assert result.ast is not None
    assert len(result.ast.required_blocks) == 2
    inner = _only_child(result.ast.required_blocks[0])
    assert inner.kind is ModuleExpressionKind.AND
    assert inner.operators[0].kind is ModuleOperatorKind.SPACE


def test_unsupported_operand_remains_a_valid_ast_leaf_with_warning() -> None:
    result = parse_module_definition("K00001+R00001")

    assert result.is_valid is True
    assert result.ast is not None
    assert result.ast.required_blocks[0].children[1].kind is ModuleExpressionKind.UNSUPPORTED
    assert [(item.code, item.severity) for item in result.diagnostics] == [
        (ModuleDiagnosticCode.UNSUPPORTED_TOKEN, ModuleDiagnosticSeverity.WARNING)
    ]
    assert result.diagnostics[0].token_preview == "R00001"


def test_long_unsupported_chunk_is_lossless_in_tokens_and_bounded_in_the_ast() -> None:
    definition = "x" * 1_001

    result = parse_module_definition(definition)

    assert result.is_valid is True
    assert result.ast is not None
    leaf = result.ast.required_blocks[0]
    assert leaf.kind is ModuleExpressionKind.UNSUPPORTED
    assert leaf.value == f"{'x' * 997}..."
    assert result.tokens[0].lexeme == definition


@pytest.mark.parametrize(
    ("definition", "expected_code"),
    [
        ("", ModuleDiagnosticCode.EMPTY_DEFINITION),
        (" \r\n\t", ModuleDiagnosticCode.EMPTY_DEFINITION),
        ("(K00001", ModuleDiagnosticCode.UNMATCHED_LEFT_PARENTHESIS),
        ("K00001)", ModuleDiagnosticCode.UNMATCHED_RIGHT_PARENTHESIS),
        (")K00001", ModuleDiagnosticCode.UNMATCHED_RIGHT_PARENTHESIS),
        ("K00001+", ModuleDiagnosticCode.MISSING_OPERAND),
        ("K00001,,K00002", ModuleDiagnosticCode.MISSING_OPERAND),
        ("K00001(K00002)", ModuleDiagnosticCode.UNEXPECTED_TOKEN),
    ],
)
def test_structural_errors_return_no_ast(
    definition: str,
    expected_code: ModuleDiagnosticCode,
) -> None:
    result = parse_module_definition(definition)

    assert result.is_valid is False
    assert result.ast is None
    assert result.ast_node_count == 0
    assert expected_code in {diagnostic.code for diagnostic in result.diagnostics}
    assert "".join(token.lexeme for token in result.tokens) == definition


def test_parser_retains_the_exact_utf8_definition() -> None:
    definition = " K00001\r\n"

    result = parse_module_definition(definition)

    assert "".join(token.lexeme for token in result.tokens) == definition
    assert result.ast is not None
    assert (result.ast.span.start_offset, result.ast.span.end_offset) == (0, len(definition))
    assert (result.ast.span.start_line, result.ast.span.start_column) == (1, 1)
    assert (result.ast.span.end_line, result.ast.span.end_column) == (2, 1)


def test_definition_byte_limit_is_fatal_and_keeps_the_source_lossless() -> None:
    definition = "K00001"
    limits = ModuleParseLimits(max_definition_bytes=5)

    result = parse_module_definition(definition, limits=limits)

    assert result.ast is None
    assert _diagnostic_codes_for_result(result) == {ModuleDiagnosticCode.DEFINITION_LIMIT_EXCEEDED}
    assert "".join(token.lexeme for token in result.tokens) == definition
    assert len(result.tokens) == 1
    assert result.tokens[0].kind is ModuleTokenKind.UNSUPPORTED


def test_byte_limit_constructs_one_token_and_one_span_for_a_large_input() -> None:
    definition = "x" * 100_000
    limits = ModuleParseLimits(max_definition_bytes=5)
    scan_source = module_syntax._scan_source  # pyright: ignore[reportPrivateUsage]

    with (
        patch.object(module_syntax, "ModuleToken", wraps=ModuleToken) as token_constructor,
        patch.object(module_syntax, "SourceSpan", wraps=SourceSpan) as span_constructor,
        patch.object(
            module_syntax,
            "_scan_source",
            wraps=scan_source,
        ) as source_scanner,
    ):
        result = parse_module_definition(definition, limits=limits)

    assert token_constructor.call_count == 1
    assert span_constructor.call_count == 1
    assert source_scanner.call_count == 1
    assert result.tokens[0].lexeme == definition


def test_token_limit_compacts_only_the_remainder_and_is_fatal() -> None:
    definition = "K00001+K00002"
    limits = ModuleParseLimits(max_tokens=2)

    result = parse_module_definition(definition, limits=limits)

    assert result.ast is None
    assert _diagnostic_codes_for_result(result) == {ModuleDiagnosticCode.TOKEN_LIMIT_EXCEEDED}
    assert len(result.tokens) == 2
    assert result.tokens[0].lexeme == "K00001"
    assert result.tokens[1].kind is ModuleTokenKind.UNSUPPORTED
    assert result.tokens[1].lexeme == "+K00002"
    assert "".join(token.lexeme for token in result.tokens) == definition


def test_token_limit_never_constructs_the_discarded_token_population() -> None:
    definition = "+".join(["K00001"] * 1_000)
    limits = ModuleParseLimits(max_tokens=3)

    with patch.object(module_syntax, "ModuleToken", wraps=ModuleToken) as constructor:
        result = parse_module_definition(definition, limits=limits)

    assert constructor.call_count == limits.max_tokens
    assert result.token_count == limits.max_tokens
    assert result.tokens[-1].kind is ModuleTokenKind.UNSUPPORTED
    assert "".join(token.lexeme for token in result.tokens) == definition
    assert _diagnostic_codes_for_result(result) == {ModuleDiagnosticCode.TOKEN_LIMIT_EXCEEDED}


def test_ast_node_limit_is_fatal() -> None:
    result = parse_module_definition(
        "K00001+K00002",
        limits=ModuleParseLimits(max_ast_nodes=2),
    )

    assert result.ast is None
    assert _diagnostic_codes_for_result(result) == {ModuleDiagnosticCode.AST_NODE_LIMIT_EXCEEDED}


def test_parenthesis_nesting_limit_is_fatal() -> None:
    result = parse_module_definition(
        "((K00001))",
        limits=ModuleParseLimits(max_nesting_depth=1),
    )

    assert result.ast is None
    assert _diagnostic_codes_for_result(result) == {ModuleDiagnosticCode.NESTING_LIMIT_EXCEEDED}


def test_extreme_allowed_nesting_fails_closed_instead_of_leaking_recursion_error() -> None:
    definition = "(" * 500 + "K00001" + ")" * 500

    result = parse_module_definition(
        definition,
        limits=ModuleParseLimits(max_nesting_depth=512, max_ast_nodes=2_000),
    )

    assert result.ast is None
    assert _diagnostic_codes_for_result(result) == {ModuleDiagnosticCode.NESTING_LIMIT_EXCEEDED}


def _diagnostic_codes_for_result(result: ModuleParseResult) -> set[ModuleDiagnosticCode]:
    return {diagnostic.code for diagnostic in result.diagnostics}
