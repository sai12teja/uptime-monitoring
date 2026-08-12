"""Shared helpers for the card/grid self-checks.

Exists so those tests assert on STRUCTURE BY CLASS rather than by child
index. Positional assertions (card.children[2]) broke every time the card
markup was reshaped -- e.g. when the meta row moved inside <summary> so a
collapsed <details> would actually render it -- even though the behaviour
under test never changed.
"""


def find_all(node, predicate, out=None):
    """Every node in the tree matching `predicate`, depth-first."""
    out = [] if out is None else out
    if predicate(node):
        out.append(node)
    children = getattr(node, "children", None)
    if isinstance(children, (list, tuple)):
        for child in children:
            find_all(child, predicate, out)
    elif children is not None and hasattr(children, "children"):
        find_all(children, predicate, out)
    return out


def by_class(node, class_name):
    """Nodes whose className contains `class_name` as a whole token."""
    return find_all(
        node,
        lambda n: class_name in (getattr(n, "className", None) or "").split(),
    )


def one_by_class(node, class_name):
    """The single node with `class_name`; asserts there is exactly one."""
    found = by_class(node, class_name)
    assert len(found) == 1, f"expected exactly one .{class_name}, got {len(found)}"
    return found[0]


def tcard_ids(node):
    """Every pattern-matching {"type": "tcard", "index": N} id in the tree --
    the click targets Dash's callback binds to."""
    matches = find_all(
        node,
        lambda n: isinstance(getattr(n, "id", None), dict)
        and getattr(n, "id").get("type") == "tcard",
    )
    return [n.id["index"] for n in matches]
