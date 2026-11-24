"""Преобразование ограниченного подмножества TeX в редактируемые формулы Word."""

from __future__ import annotations

import re

from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from lxml.etree import _Element

MAX_LENGTH = 1024
MAX_DEPTH = 16
TOKENS = re.compile(r"\\[A-Za-z]+|\\[^\r\n]|\s+|.", re.DOTALL)
MARKERS = re.compile(r"\\[\\$()\[\]]|\$\$?")
PAIRS = {"$": "$", "$$": "$$", r"\(": r"\)", r"\[": r"\]"}
ESCAPED_DOLLAR = re.compile(r"\\[\\$]")

SYMBOLS = dict(
    zip(
        (
            "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi "
            "omicron pi rho sigma tau upsilon phi chi psi omega"
        ).split(),
        "αβγδεζηθικλμνξοπρστυφχψω",
        strict=True,
    )
)
SYMBOLS.update(
    {
        "Gamma": "Γ",
        "Delta": "Δ",
        "Theta": "Θ",
        "Lambda": "Λ",
        "Xi": "Ξ",
        "Pi": "Π",
        "Sigma": "Σ",
        "Upsilon": "Υ",
        "Phi": "Φ",
        "Psi": "Ψ",
        "Omega": "Ω",
        "varepsilon": "ϵ",
        "vartheta": "ϑ",
        "varphi": "ϕ",
        "varrho": "ϱ",
        "varpi": "ϖ",
        "varsigma": "ς",
        "cdot": "·",
        "times": "×",
        "div": "÷",
        "pm": "±",
        "mp": "∓",
        "le": "≤",
        "leq": "≤",
        "ge": "≥",
        "geq": "≥",
        "neq": "≠",
        "ne": "≠",
        "approx": "≈",
        "equiv": "≡",
        "infty": "∞",
        "to": "→",
        "partial": "∂",
        "nabla": "∇",
        "degree": "°",
        ",": "\u2009",
        ";": "\u2005",
        "quad": "\u2003",
        "qquad": "\u2003\u2003",
        " ": " ",
        "{": "{",
        "}": "}",
        "%": "%",
        "$": "$",
        "_": "_",
        "#": "#",
        "&": "&",
    }
)
OPERATORS = "+-=*/:.,!?()[]|<>·×÷±∓≤≥≠≈≡∞→∂∇°−′"


class UnsupportedFormula(ValueError):
    """Конструкция не входит в поддерживаемое подмножество."""


def _node(name: str, children: list[_Element] | None = None) -> _Element:
    element = OxmlElement(f"m:{name}")
    if children is not None:
        element.extend(children)
    return element


def _run(value: str) -> _Element:
    text = _node("t")
    text.text = value
    if value.isspace():
        text.set(qn("xml:space"), "preserve")
    return _node("r", [text])


class _Parser:
    def __init__(self, tex: str) -> None:
        self.tokens = [token for token in TOKENS.findall(tex) if not token.isspace()]
        self.index = 0

    def peek(self) -> str | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def take(self) -> str:
        token = self.peek()
        if token is None:
            raise UnsupportedFormula
        self.index += 1
        return token

    def sequence(self, depth: int, stop: str | None = None) -> list[_Element]:
        if depth > MAX_DEPTH:
            raise UnsupportedFormula
        result = []
        while self.peek() is not None and self.peek() != stop:
            base = self.atom(depth)
            scripts: dict[str, list[_Element]] = {}
            while self.peek() in {"_", "^"}:
                marker = self.take()
                if marker in scripts:
                    raise UnsupportedFormula
                scripts[marker] = self.atom(depth + 1)
            if scripts:
                name = {"_": "sSub", "^": "sSup", "_^": "sSubSup"}[
                    "".join(sorted(scripts, reverse=True))
                ]
                children = [_node("e", base)]
                children.extend(
                    _node(tag, scripts[marker])
                    for marker, tag in (("_", "sub"), ("^", "sup"))
                    if marker in scripts
                )
                base = [_node(name, children)]
            result.extend(base)
        if stop is not None and self.take() != stop:
            raise UnsupportedFormula
        if not result:
            raise UnsupportedFormula
        return result

    def atom(self, depth: int) -> list[_Element]:
        if depth > MAX_DEPTH:
            raise UnsupportedFormula
        token = self.take()
        if token == "{":
            return self.sequence(depth + 1, "}")
        if token in {r"\frac", r"\dfrac", r"\tfrac"}:
            numerator = _node("num", self.atom(depth + 1))
            denominator = _node("den", self.atom(depth + 1))
            return [_node("f", [numerator, denominator])]
        if token == r"\sqrt":
            degree = []
            if self.peek() == "[":
                self.take()
                degree = self.sequence(depth + 1, "]")
            base = _node("e", self.atom(depth + 1))
            hidden = _node("degHide")
            hidden.set(qn("m:val"), "0" if degree else "1")
            return [_node("rad", [_node("radPr", [hidden]), _node("deg", degree), base])]
        if token.startswith("\\") and token[1:] in SYMBOLS:
            return [_run(SYMBOLS[token[1:]])]
        if len(token) == 1 and (token.isalnum() or token in OPERATORS):
            return [_run(token)]
        raise UnsupportedFormula


def add_formula(paragraph: Paragraph, tex: str) -> bool:
    """Добавить формулу без разделителей. При неподдерживаемом TeX вернуть False."""
    if len(tex) > MAX_LENGTH:
        return False
    try:
        equation = _node("oMath", _Parser(tex).sequence(0))
    except UnsupportedFormula:
        return False
    paragraph._p.append(equation)
    return True


def _plain(paragraph: Paragraph, text: str) -> None:
    if text:
        paragraph.add_run(
            ESCAPED_DOLLAR.sub(lambda match: "$" if match.group() == r"\$" else match.group(), text)
        )


def add_math_text(paragraph: Paragraph, text: str) -> None:
    """Добавить текст, заменяя явно размеченные поддерживаемые формулы на OMML."""
    position = 0
    opener: re.Match[str] | None = None
    for marker in MARKERS.finditer(text):
        token = marker.group()
        if opener is None:
            if token in PAIRS and (
                token != "$" or not text[marker.end() : marker.end() + 1].isspace()
            ):
                opener = marker
            continue
        if token != PAIRS[opener.group()]:
            continue
        if token == "$" and (
            text[marker.start() - 1 : marker.start()].isspace()
            or text[marker.end() : marker.end() + 1].isdigit()
        ):
            continue
        _plain(paragraph, text[position : opener.start()])
        tex = text[opener.end() : marker.start()]
        if not add_formula(paragraph, tex):
            paragraph.add_run(text[opener.start() : marker.end()])
        position = marker.end()
        opener = None
    _plain(paragraph, text[position:])
