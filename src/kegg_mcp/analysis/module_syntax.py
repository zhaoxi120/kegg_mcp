"""Lossless tokenizer and conservative parser for KEGG MODULE definitions.

The grammar implemented here is intentionally small and explicit::

    definition  := block (TOP_LEVEL_SPACE block)*
    block       := or_expression
    or_expression := and_expression ("," and_expression)*
    and_expression := factor (("+" | INNER_SPACE) factor | "-" factor)*
    factor      := K_NUMBER | M_NUMBER | unsupported | "(" or_expression ")"

Parentheses bind first, inner spaces and plus/minus connectors bind before commas,
and semantic spaces at the outermost level delimit required blocks. Whitespace next
to an explicit operator or parenthesis is retained lexically but is formatting rather
than another logical operator. Only ASCII space, tab, carriage return, and line feed
are grammar whitespace; every other Unicode whitespace character is unsupported.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Never

from kegg_mcp.analysis.contracts import (
    MODULE_PARSER_NAME,
    MODULE_PARSER_VERSION,
    ModuleDefinitionAst,
    ModuleDiagnosticCode,
    ModuleDiagnosticSeverity,
    ModuleExpression,
    ModuleExpressionKind,
    ModuleOperatorKind,
    ModuleOperatorOccurrence,
    ModuleParseDiagnostic,
    ModuleParseLimits,
    ModuleParseResult,
    ModuleToken,
    ModuleTokenKind,
    SourceSpan,
)

_EXPLICIT_LEXEMES: dict[str, ModuleTokenKind] = {
    "+": ModuleTokenKind.PLUS,
    ",": ModuleTokenKind.COMMA,
    "-": ModuleTokenKind.MINUS,
    "(": ModuleTokenKind.LEFT_PAREN,
    ")": ModuleTokenKind.RIGHT_PAREN,
}
_LOGICAL_WHITESPACE = frozenset({" ", "\t", "\r", "\n"})
_UNICODE_LINE_BREAKS = frozenset({"\n", "\u0085", "\u2028", "\u2029"})
_HASH_CHUNK_CODE_POINTS = 4_096
_FACTOR_STARTS = frozenset(
    {
        ModuleTokenKind.KO,
        ModuleTokenKind.MODULE_REFERENCE,
        ModuleTokenKind.LEFT_PAREN,
        ModuleTokenKind.UNSUPPORTED,
    }
)


@dataclass(frozen=True, slots=True)
class _Tokenization:
    tokens: tuple[ModuleToken, ...]
    definition_bytes: int
    definition_sha256: str
    source_span: SourceSpan
    definition_limit_exceeded: bool = False
    token_limit_exceeded: bool = False


@dataclass(frozen=True, slots=True)
class _SourceScan:
    definition_bytes: int
    definition_sha256: str
    source_span: SourceSpan


@dataclass(frozen=True, slots=True)
class _ParseFailure(Exception):
    code: ModuleDiagnosticCode
    message: str
    span: SourceSpan | None = None
    token_preview: str | None = None


def _advance_position(
    text: str,
    start_offset: int,
    end_offset: int,
    start_line: int,
    start_column: int,
) -> tuple[int, int]:
    """Advance a source position without allocating a per-code-point position map."""
    line = start_line
    column = start_column
    index = start_offset
    while index < end_offset:
        character = text[index]
        if character == "\r":
            line += 1
            column = 1
            index += 1
            if index < end_offset and text[index] == "\n":
                index += 1
            continue
        if character in _UNICODE_LINE_BREAKS:
            line += 1
            column = 1
        else:
            column += 1
        index += 1
    return line, column


def _scan_source(definition: str) -> _SourceScan:
    """Compute bounded-memory UTF-8 metadata before token allocation."""
    digest = hashlib.sha256()
    definition_bytes = 0
    for start in range(0, len(definition), _HASH_CHUNK_CODE_POINTS):
        chunk = definition[start : start + _HASH_CHUNK_CODE_POINTS]
        try:
            encoded = chunk.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("module definition must be valid UTF-8 text") from error
        definition_bytes += len(encoded)
        digest.update(encoded)

    end_line, end_column = _advance_position(definition, 0, len(definition), 1, 1)
    return _SourceScan(
        definition_bytes=definition_bytes,
        definition_sha256=digest.hexdigest(),
        source_span=SourceSpan(
            start_offset=0,
            end_offset=len(definition),
            start_line=1,
            start_column=1,
            end_line=end_line,
            end_column=end_column,
        ),
    )


def _classify_chunk(lexeme: str) -> ModuleTokenKind:
    if (
        len(lexeme) == 6
        and lexeme.startswith("K")
        and lexeme[1:].isascii()
        and lexeme[1:].isdigit()
    ):
        return ModuleTokenKind.KO
    if (
        len(lexeme) == 6
        and lexeme.startswith("M")
        and lexeme[1:].isascii()
        and lexeme[1:].isdigit()
    ):
        return ModuleTokenKind.MODULE_REFERENCE
    return ModuleTokenKind.UNSUPPORTED


def _is_unsupported_whitespace(character: str) -> bool:
    return character.isspace() and character not in _LOGICAL_WHITESPACE


def _token_extent(
    definition: str,
    start: int,
) -> tuple[int, ModuleTokenKind | None]:
    """Return a token end and a fixed kind, deferring identifier classification."""
    character = definition[start]
    index = start + 1
    if character in _LOGICAL_WHITESPACE:
        while index < len(definition) and definition[index] in _LOGICAL_WHITESPACE:
            index += 1
        return index, ModuleTokenKind.WHITESPACE
    if _is_unsupported_whitespace(character):
        while index < len(definition) and _is_unsupported_whitespace(definition[index]):
            index += 1
        return index, ModuleTokenKind.UNSUPPORTED
    if character in _EXPLICIT_LEXEMES:
        return index, _EXPLICIT_LEXEMES[character]
    while (
        index < len(definition)
        and definition[index] not in _LOGICAL_WHITESPACE
        and definition[index] not in _EXPLICIT_LEXEMES
        and not _is_unsupported_whitespace(definition[index])
    ):
        index += 1
    return index, None


def _bounded_tokens(
    definition: str,
    source_span: SourceSpan,
    max_tokens: int,
) -> tuple[tuple[ModuleToken, ...], bool]:
    """Build at most ``max_tokens`` objects and compact an over-limit remainder."""
    tokens: list[ModuleToken] = []
    index = 0
    line = 1
    column = 1
    while index < len(definition):
        start = index
        start_line = line
        start_column = column
        index, fixed_kind = _token_extent(definition, start)
        line, column = _advance_position(
            definition,
            start,
            index,
            start_line,
            start_column,
        )

        if len(tokens) == max_tokens - 1 and index < len(definition):
            tokens.append(
                ModuleToken(
                    kind=ModuleTokenKind.UNSUPPORTED,
                    lexeme=definition[start:],
                    span=SourceSpan(
                        start_offset=start,
                        end_offset=len(definition),
                        start_line=start_line,
                        start_column=start_column,
                        end_line=source_span.end_line,
                        end_column=source_span.end_column,
                    ),
                )
            )
            return tuple(tokens), True

        lexeme = definition[start:index]
        kind = fixed_kind if fixed_kind is not None else _classify_chunk(lexeme)
        tokens.append(
            ModuleToken(
                kind=kind,
                lexeme=lexeme,
                span=SourceSpan(
                    start_offset=start,
                    end_offset=index,
                    start_line=start_line,
                    start_column=start_column,
                    end_line=line,
                    end_column=column,
                ),
            )
        )
    return tuple(tokens), False


def _tokenize(definition: str, limits: ModuleParseLimits) -> _Tokenization:
    source = _scan_source(definition)
    if source.definition_bytes > limits.max_definition_bytes:
        tokens = (
            (
                ModuleToken(
                    kind=ModuleTokenKind.UNSUPPORTED,
                    lexeme=definition,
                    span=source.source_span,
                ),
            )
            if definition
            else ()
        )
        return _Tokenization(
            tokens=tokens,
            definition_bytes=source.definition_bytes,
            definition_sha256=source.definition_sha256,
            source_span=source.source_span,
            definition_limit_exceeded=True,
        )

    tokens, token_limit_exceeded = _bounded_tokens(
        definition,
        source.source_span,
        limits.max_tokens,
    )
    return _Tokenization(
        tokens=tokens,
        definition_bytes=source.definition_bytes,
        definition_sha256=source.definition_sha256,
        source_span=source.source_span,
        token_limit_exceeded=token_limit_exceeded,
    )


def tokenize_module_definition(
    definition: str,
    *,
    limits: ModuleParseLimits | None = None,
) -> tuple[ModuleToken, ...]:
    """Tokenize a definition without dropping source text.

    When a hard limit is exceeded, the unparsed remainder is represented by one
    ``UNSUPPORTED`` token. ``parse_module_definition`` additionally reports the
    corresponding fatal diagnostic.
    """
    effective_limits = limits if limits is not None else ModuleParseLimits()
    return _tokenize(definition, effective_limits).tokens


class _Parser:
    def __init__(
        self,
        tokens: tuple[ModuleToken, ...],
        source_span: SourceSpan,
        limits: ModuleParseLimits,
    ) -> None:
        self.tokens = tokens
        self.source_span = source_span
        self.limits = limits
        self.index = 0
        self.node_count = 0

    @property
    def current(self) -> ModuleToken | None:
        if self.index >= len(self.tokens):
            return None
        return self.tokens[self.index]

    def _consume(self) -> ModuleToken:
        token = self.current
        if token is None:  # pragma: no cover - callers guard the end of input
            raise RuntimeError("cannot consume beyond the token stream")
        self.index += 1
        return token

    def _skip_whitespace(self) -> None:
        while self.current is not None and self.current.kind is ModuleTokenKind.WHITESPACE:
            self.index += 1

    def _next_non_whitespace(self) -> ModuleToken | None:
        index = self.index
        while index < len(self.tokens) and self.tokens[index].kind is ModuleTokenKind.WHITESPACE:
            index += 1
        if index == len(self.tokens):
            return None
        return self.tokens[index]

    def _fail(
        self,
        code: ModuleDiagnosticCode,
        message: str,
        token: ModuleToken | None = None,
        *,
        span: SourceSpan | None = None,
    ) -> Never:
        preview = None if token is None else _preview(token.lexeme)
        raise _ParseFailure(
            code=code,
            message=message,
            span=span if span is not None else (None if token is None else token.span),
            token_preview=preview,
        )

    def _node(
        self,
        kind: ModuleExpressionKind,
        span: SourceSpan,
        *,
        value: str | None = None,
        children: tuple[ModuleExpression, ...] = (),
        operators: tuple[ModuleOperatorOccurrence, ...] = (),
    ) -> ModuleExpression:
        if self.node_count >= self.limits.max_ast_nodes:
            self._fail(
                ModuleDiagnosticCode.AST_NODE_LIMIT_EXCEEDED,
                "The MODULE definition exceeds the configured AST-node limit.",
                span=span,
            )
        self.node_count += 1
        return ModuleExpression(
            kind=kind,
            span=span,
            value=value,
            children=children,
            operators=operators,
        )

    def parse(self) -> ModuleDefinitionAst:
        self._skip_whitespace()
        first = self.current
        if first is None:  # pragma: no cover - handled before parser construction
            self._fail(
                ModuleDiagnosticCode.EMPTY_DEFINITION,
                "The MODULE definition contains no expression.",
            )
        if first.kind is ModuleTokenKind.RIGHT_PAREN:
            self._fail(
                ModuleDiagnosticCode.UNMATCHED_RIGHT_PARENTHESIS,
                "The MODULE definition contains an unmatched right parenthesis.",
                first,
            )

        blocks: list[ModuleExpression] = [self._parse_or(top_level=True, nesting_depth=0)]
        separators: list[ModuleOperatorOccurrence] = []
        while self.current is not None:
            token = self.current
            if token.kind is ModuleTokenKind.WHITESPACE:
                separator = self._consume()
                next_token = self._next_non_whitespace()
                if next_token is None:
                    self._skip_whitespace()
                    break
                if next_token.kind not in _FACTOR_STARTS:
                    self._fail(
                        ModuleDiagnosticCode.UNEXPECTED_TOKEN,
                        "A top-level block separator must be followed by an expression.",
                        next_token,
                    )
                separators.append(
                    ModuleOperatorOccurrence(kind=ModuleOperatorKind.SPACE, span=separator.span)
                )
                self._skip_whitespace()
                blocks.append(self._parse_or(top_level=True, nesting_depth=0))
                continue
            if token.kind is ModuleTokenKind.RIGHT_PAREN:
                self._fail(
                    ModuleDiagnosticCode.UNMATCHED_RIGHT_PARENTHESIS,
                    "The MODULE definition contains an unmatched right parenthesis.",
                    token,
                )
            self._fail(
                ModuleDiagnosticCode.UNEXPECTED_TOKEN,
                "The MODULE definition contains adjacent expressions without an operator.",
                token,
            )

        return ModuleDefinitionAst(
            span=self.source_span,
            required_blocks=tuple(blocks),
            block_separators=tuple(separators),
        )

    def _parse_or(self, *, top_level: bool, nesting_depth: int) -> ModuleExpression:
        children = [self._parse_and(top_level=top_level, nesting_depth=nesting_depth)]
        operators: list[ModuleOperatorOccurrence] = []
        while True:
            token = self.current
            if token is None or token.kind is not ModuleTokenKind.COMMA:
                break
            comma = self._consume()
            self._skip_whitespace()
            next_token = self.current
            if next_token is None or next_token.kind not in _FACTOR_STARTS:
                self._fail(
                    ModuleDiagnosticCode.MISSING_OPERAND,
                    "A comma operator must be followed by an alternative expression.",
                    comma,
                )
            operators.append(
                ModuleOperatorOccurrence(kind=ModuleOperatorKind.COMMA, span=comma.span)
            )
            children.append(self._parse_and(top_level=top_level, nesting_depth=nesting_depth))

        if len(children) == 1:
            return children[0]
        return self._node(
            ModuleExpressionKind.OR,
            _cover(children[0].span, children[-1].span),
            children=tuple(children),
            operators=tuple(operators),
        )

    def _parse_and(self, *, top_level: bool, nesting_depth: int) -> ModuleExpression:
        children = [self._parse_factor(nesting_depth=nesting_depth)]
        operators: list[ModuleOperatorOccurrence] = []
        while True:
            token = self.current
            if token is None:
                break

            if token.kind is ModuleTokenKind.WHITESPACE:
                next_token = self._next_non_whitespace()
                if next_token is None or next_token.kind in {
                    ModuleTokenKind.PLUS,
                    ModuleTokenKind.COMMA,
                    ModuleTokenKind.MINUS,
                    ModuleTokenKind.RIGHT_PAREN,
                }:
                    self._skip_whitespace()
                    continue
                if next_token.kind in _FACTOR_STARTS:
                    if top_level:
                        break
                    whitespace = self._consume()
                    self._skip_whitespace()
                    operators.append(
                        ModuleOperatorOccurrence(
                            kind=ModuleOperatorKind.SPACE,
                            span=whitespace.span,
                        )
                    )
                    children.append(self._parse_factor(nesting_depth=nesting_depth))
                    continue
                self._fail(
                    ModuleDiagnosticCode.UNEXPECTED_TOKEN,
                    "Whitespace is followed by an invalid MODULE operator.",
                    next_token,
                )

            if token.kind in {ModuleTokenKind.PLUS, ModuleTokenKind.MINUS}:
                operator = self._consume()
                self._skip_whitespace()
                next_token = self.current
                if next_token is None or next_token.kind not in _FACTOR_STARTS:
                    self._fail(
                        ModuleDiagnosticCode.MISSING_OPERAND,
                        "A MODULE connector must be followed by an expression.",
                        operator,
                    )
                child = self._parse_factor(nesting_depth=nesting_depth)
                if operator.kind is ModuleTokenKind.MINUS:
                    child = self._node(
                        ModuleExpressionKind.OPTIONAL,
                        _cover(operator.span, child.span),
                        children=(child,),
                    )
                    operator_kind = ModuleOperatorKind.MINUS
                else:
                    operator_kind = ModuleOperatorKind.PLUS
                operators.append(ModuleOperatorOccurrence(kind=operator_kind, span=operator.span))
                children.append(child)
                continue

            if token.kind in {ModuleTokenKind.COMMA, ModuleTokenKind.RIGHT_PAREN}:
                break
            if token.kind in _FACTOR_STARTS:
                self._fail(
                    ModuleDiagnosticCode.UNEXPECTED_TOKEN,
                    "Adjacent MODULE expressions require an explicit or whitespace operator.",
                    token,
                )
            self._fail(
                ModuleDiagnosticCode.UNEXPECTED_TOKEN,
                "The MODULE definition contains an unexpected operator.",
                token,
            )

        if len(children) == 1:
            return children[0]
        return self._node(
            ModuleExpressionKind.AND,
            _cover(children[0].span, children[-1].span),
            children=tuple(children),
            operators=tuple(operators),
        )

    def _parse_factor(self, *, nesting_depth: int) -> ModuleExpression:
        token = self.current
        if token is None:
            self._fail(
                ModuleDiagnosticCode.MISSING_OPERAND,
                "The MODULE definition is missing an expression operand.",
            )
        assert token is not None

        if token.kind in {
            ModuleTokenKind.PLUS,
            ModuleTokenKind.COMMA,
            ModuleTokenKind.MINUS,
        }:
            self._fail(
                ModuleDiagnosticCode.MISSING_OPERAND,
                "A MODULE operator appears without a left operand.",
                token,
            )
        if token.kind is ModuleTokenKind.RIGHT_PAREN:
            self._fail(
                ModuleDiagnosticCode.MISSING_OPERAND,
                "A parenthesized MODULE expression contains no operand.",
                token,
            )
        if token.kind is ModuleTokenKind.WHITESPACE:  # pragma: no cover - callers skip whitespace
            self._fail(
                ModuleDiagnosticCode.MISSING_OPERAND,
                "The MODULE definition is missing an expression operand.",
                token,
            )

        if token.kind is ModuleTokenKind.LEFT_PAREN:
            left = self._consume()
            next_depth = nesting_depth + 1
            if next_depth > self.limits.max_nesting_depth:
                self._fail(
                    ModuleDiagnosticCode.NESTING_LIMIT_EXCEEDED,
                    "The MODULE definition exceeds the configured parenthesis nesting limit.",
                    left,
                )
            self._skip_whitespace()
            first_inside = self.current
            if first_inside is None:
                self._fail(
                    ModuleDiagnosticCode.UNMATCHED_LEFT_PARENTHESIS,
                    "The MODULE definition contains an unmatched left parenthesis.",
                    left,
                )
            if first_inside.kind is ModuleTokenKind.RIGHT_PAREN:
                self._fail(
                    ModuleDiagnosticCode.MISSING_OPERAND,
                    "A parenthesized MODULE expression contains no operand.",
                    first_inside,
                )
            child = self._parse_or(top_level=False, nesting_depth=next_depth)
            right = self.tokens[self.index] if self.index < len(self.tokens) else None
            if right is None or right.kind is not ModuleTokenKind.RIGHT_PAREN:
                self._fail(
                    ModuleDiagnosticCode.UNMATCHED_LEFT_PARENTHESIS,
                    "The MODULE definition contains an unmatched left parenthesis.",
                    left,
                )
            self._consume()
            return self._node(
                ModuleExpressionKind.GROUP,
                _cover(left.span, right.span),
                children=(child,),
            )

        leaf = self._consume()
        leaf_kind = {
            ModuleTokenKind.KO: ModuleExpressionKind.KO,
            ModuleTokenKind.MODULE_REFERENCE: ModuleExpressionKind.MODULE_REFERENCE,
            ModuleTokenKind.UNSUPPORTED: ModuleExpressionKind.UNSUPPORTED,
        }.get(leaf.kind)
        if leaf_kind is None:  # pragma: no cover - all other kinds fail above
            self._fail(
                ModuleDiagnosticCode.UNEXPECTED_TOKEN,
                "The MODULE definition contains an unexpected token.",
                leaf,
            )
        assert leaf_kind is not None
        value = leaf.lexeme
        if leaf_kind is ModuleExpressionKind.UNSUPPORTED and len(value) > 1_000:
            value = f"{value[:997]}..."
        return self._node(leaf_kind, leaf.span, value=value)


def _cover(first: SourceSpan, last: SourceSpan) -> SourceSpan:
    return SourceSpan(
        start_offset=first.start_offset,
        end_offset=last.end_offset,
        start_line=first.start_line,
        start_column=first.start_column,
        end_line=last.end_line,
        end_column=last.end_column,
    )


def _preview(value: str) -> str:
    if len(value) <= 120:
        return value
    return f"{value[:117]}..."


def _diagnostic(
    code: ModuleDiagnosticCode,
    severity: ModuleDiagnosticSeverity,
    message: str,
    *,
    span: SourceSpan | None = None,
    token_preview: str | None = None,
) -> ModuleParseDiagnostic:
    return ModuleParseDiagnostic(
        code=code,
        severity=severity,
        message=message,
        span=span,
        token_preview=token_preview,
    )


def parse_module_definition(
    definition: str,
    *,
    limits: ModuleParseLimits | None = None,
) -> ModuleParseResult:
    """Parse one complete KEGG MODULE definition conservatively.

    Unsupported lexical content remains a warning-bearing AST leaf. Malformed
    structure or a configured hard-limit violation returns no AST and at least one
    error diagnostic.
    """
    effective_limits = limits if limits is not None else ModuleParseLimits()
    tokenization = _tokenize(definition, effective_limits)
    digest = tokenization.definition_sha256
    source_span = tokenization.source_span
    diagnostics: list[ModuleParseDiagnostic] = []

    if tokenization.definition_limit_exceeded:
        diagnostics.append(
            _diagnostic(
                ModuleDiagnosticCode.DEFINITION_LIMIT_EXCEEDED,
                ModuleDiagnosticSeverity.ERROR,
                "The MODULE definition exceeds the configured UTF-8 byte limit.",
                span=source_span,
            )
        )
        return _result(
            digest=digest,
            tokens=tokenization.tokens,
            ast=None,
            node_count=0,
            diagnostics=diagnostics,
            limits=effective_limits,
        )

    if tokenization.token_limit_exceeded:
        diagnostics.append(
            _diagnostic(
                ModuleDiagnosticCode.TOKEN_LIMIT_EXCEEDED,
                ModuleDiagnosticSeverity.ERROR,
                "The MODULE definition exceeds the configured token limit.",
                span=source_span,
            )
        )
        return _result(
            digest=digest,
            tokens=tokenization.tokens,
            ast=None,
            node_count=0,
            diagnostics=diagnostics,
            limits=effective_limits,
        )

    if not tokenization.tokens or all(
        token.kind is ModuleTokenKind.WHITESPACE for token in tokenization.tokens
    ):
        diagnostics.append(
            _diagnostic(
                ModuleDiagnosticCode.EMPTY_DEFINITION,
                ModuleDiagnosticSeverity.ERROR,
                "The MODULE definition contains no expression.",
                span=source_span,
            )
        )
        return _result(
            digest=digest,
            tokens=tokenization.tokens,
            ast=None,
            node_count=0,
            diagnostics=diagnostics,
            limits=effective_limits,
        )

    diagnostics.extend(
        _diagnostic(
            ModuleDiagnosticCode.UNSUPPORTED_TOKEN,
            ModuleDiagnosticSeverity.WARNING,
            "Unsupported MODULE content is retained as an explicit AST leaf.",
            span=token.span,
            token_preview=_preview(token.lexeme),
        )
        for token in tokenization.tokens
        if token.kind is ModuleTokenKind.UNSUPPORTED
    )

    parser = _Parser(
        tokens=tokenization.tokens,
        source_span=source_span,
        limits=effective_limits,
    )
    try:
        ast = parser.parse()
    except _ParseFailure as failure:
        diagnostics.append(
            _diagnostic(
                failure.code,
                ModuleDiagnosticSeverity.ERROR,
                failure.message,
                span=failure.span,
                token_preview=failure.token_preview,
            )
        )
        return _result(
            digest=digest,
            tokens=tokenization.tokens,
            ast=None,
            node_count=0,
            diagnostics=diagnostics,
            limits=effective_limits,
        )
    except RecursionError:
        diagnostics.append(
            _diagnostic(
                ModuleDiagnosticCode.NESTING_LIMIT_EXCEEDED,
                ModuleDiagnosticSeverity.ERROR,
                "The MODULE definition exceeds the parser's safe nesting bound.",
                span=source_span,
            )
        )
        return _result(
            digest=digest,
            tokens=tokenization.tokens,
            ast=None,
            node_count=0,
            diagnostics=diagnostics,
            limits=effective_limits,
        )

    return _result(
        digest=digest,
        tokens=tokenization.tokens,
        ast=ast,
        node_count=parser.node_count,
        diagnostics=diagnostics,
        limits=effective_limits,
    )


def _result(
    *,
    digest: str,
    tokens: tuple[ModuleToken, ...],
    ast: ModuleDefinitionAst | None,
    node_count: int,
    diagnostics: list[ModuleParseDiagnostic],
    limits: ModuleParseLimits,
) -> ModuleParseResult:
    return ModuleParseResult(
        definition_sha256=digest,
        parser_name=MODULE_PARSER_NAME,
        parser_version=MODULE_PARSER_VERSION,
        tokens=tokens,
        token_count=len(tokens),
        ast=ast,
        ast_node_count=node_count,
        diagnostics=tuple(diagnostics),
        is_valid=ast is not None
        and not any(
            diagnostic.severity is ModuleDiagnosticSeverity.ERROR for diagnostic in diagnostics
        ),
        limits=limits,
    )


__all__ = ["parse_module_definition", "tokenize_module_definition"]
