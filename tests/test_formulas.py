from io import BytesIO
from zipfile import ZipFile

import pytest
from docx import Document
from lxml import etree

from ucheba.formulas import MAX_DEPTH, MAX_LENGTH, add_formula, add_math_text

MATH = "http://schemas.openxmlformats.org/officeDocument/2006/math"
NS = {"m": MATH}


def test_export_contains_editable_word_equation():
    document = Document()
    paragraph = document.add_paragraph()
    add_math_text(paragraph, r"Формула: \(x_{0}^2=\frac{\alpha}{\sqrt[3]{y}}\).")
    output = BytesIO()
    document.save(output)
    with ZipFile(output) as package:
        xml = etree.fromstring(package.read("word/document.xml"))
    equation = xml.find(".//m:oMath", NS)
    assert equation is not None
    assert equation.findtext("m:sSubSup/m:e/m:r/m:t", namespaces=NS) == "x"
    assert equation.findtext("m:sSubSup/m:sub/m:r/m:t", namespaces=NS) == "0"
    assert equation.findtext("m:sSubSup/m:sup/m:r/m:t", namespaces=NS) == "2"
    assert equation.findtext("m:r/m:t", namespaces=NS) == "="
    assert equation.findtext("m:f/m:num/m:r/m:t", namespaces=NS) == "α"
    assert equation.findtext("m:f/m:den/m:rad/m:deg/m:r/m:t", namespaces=NS) == "3"
    assert equation.findtext("m:f/m:den/m:rad/m:e/m:r/m:t", namespaces=NS) == "y"
    assert Document(output).paragraphs[0].text == "Формула: ."


@pytest.mark.parametrize(
    "tex",
    [
        "",
        r"\input{secret}",
        r"\frac{x}",
        "x^{2",
        "x^2^3",
        "{" * (MAX_DEPTH + 1) + "x",
        "x" * (MAX_LENGTH + 1),
    ],
)
def test_invalid_or_unsupported_formula_leaves_paragraph_unchanged(tex):
    paragraph = Document().add_paragraph("Пример")
    before = paragraph._p.xml
    assert not add_formula(paragraph, tex)
    assert paragraph._p.xml == before


def test_mixed_delimiters_escaped_currency_and_unsupported_math():
    paragraph = Document().add_paragraph()
    add_math_text(
        paragraph,
        r"Цена \$50. $a_1$, $$b^2$$, \[\sqrt{x}\]. $\unknown{x}$ и $без конца.",
    )
    assert len(paragraph._p.xpath("./m:oMath")) == 3
    assert paragraph.text == r"Цена $50. , , . $\unknown{x}$ и $без конца."
