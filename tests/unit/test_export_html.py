"""HTML viewer export: embedded data matches the graph, neighborhood is bounded."""

import json
import re

from app.db.models import Edge, Node
from app.export import cmd_html


def _node(db, name, node_type="service"):
    n = Node(
        node_type=node_type, name=name, properties={}, source_system="github", external_id=name
    )
    db.add(n)
    return n


def _payload(path):
    html = path.read_text(encoding="utf-8")
    return json.loads(re.search(r"const data = (\{.*?\});\n", html, re.S).group(1)), html


def test_html_whole_graph(db, tmp_path):
    a = _node(db, "svc-a")
    b = _node(db, "svc-b")
    _node(db, "svc-lone")
    db.flush()
    db.add(Edge(source_id=a.id, target_id=b.id, relationship_type="REFERENCES"))
    db.flush()

    out = tmp_path / "graph.html"
    assert cmd_html(db, str(out), node_id=None, depth=2, max_nodes=2000) == 0
    data, html = _payload(out)
    names = {n["name"] for n in data["nodes"]}
    assert {"svc-a", "svc-b", "svc-lone"} <= names
    assert data["edges"][0]["rel"] == "REFERENCES"
    # Self-contained: a canvas app with no external scripts, styles, or links.
    assert "<canvas" in html and "src=" not in html and "href=" not in html


def test_html_neighborhood_is_bounded(db, tmp_path):
    a = _node(db, "svc-a")
    b = _node(db, "svc-b")
    faraway = _node(db, "svc-far")
    db.flush()
    db.add(Edge(source_id=a.id, target_id=b.id, relationship_type="REFERENCES"))
    db.add(Edge(source_id=b.id, target_id=faraway.id, relationship_type="REFERENCES"))
    db.flush()

    out = tmp_path / "hood.html"
    assert cmd_html(db, str(out), node_id=str(a.id), depth=1, max_nodes=2000) == 0
    data, _ = _payload(out)
    names = {n["name"] for n in data["nodes"]}
    assert names == {"svc-a", "svc-b"}


def test_html_rejects_bad_node(db, tmp_path):
    assert cmd_html(db, str(tmp_path / "x.html"), node_id="nope", depth=1, max_nodes=10) == 1
