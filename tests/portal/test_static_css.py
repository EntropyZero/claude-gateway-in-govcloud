"""Regression gates for portal.css.

The stylesheet is data the templates depend on: the .wN ladder must map every
class to its own percentage, and generic single-class selectors must not
shadow the progress fills' ok/warn/danger status classes. A bare `.ok` rule
(the flash style) once leaked padding+border onto every green fill span,
inflating each bar by a constant 26px - so 0.4% rendered longer than 72%.
"""

import os
import re

CSS_PATH = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "docker", "portal", "portal",
    "static", "portal.css"))


def _css():
    with open(CSS_PATH) as f:
        return f.read()


def test_wn_ladder_is_complete_and_consistent():
    rules = dict(re.findall(r"\.w(\d+)\{--pct:(\d+)%\}", _css()))
    assert sorted(map(int, rules)) == list(range(101))
    for cls, pct in rules.items():
        assert cls == pct, ".w%s sets --pct:%s%%" % (cls, pct)


def test_status_classes_are_not_shadowed_by_generic_rules():
    # Any selector that is exactly `.ok` / `.warn` / `.danger` / `.err`
    # (no element or ancestor scoping) applies to the progress fills and
    # fill cells too - the flash-vs-fill collision this guards against.
    css = _css()
    selectors = []
    for rule in re.findall(r"(?:^|\})([^{}]+)\{", css):
        selectors.extend(s.strip() for s in rule.split(","))
    for status in (".ok", ".warn", ".danger", ".err"):
        assert status not in selectors, "bare %s rule shadows the fills" % status


def test_fill_consumers_read_the_pct_variable():
    css = _css()
    assert re.search(r"\.bar \.fill\{[^}]*width:var\(--pct", css)
    assert re.search(r"td\.fillcell\{[^}]*var\(--pct", css)
