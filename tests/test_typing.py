"""The product code is typed, and its annotations resolve.

`from __future__ import annotations` makes every annotation a string, so a
typo in one is invisible until a checker reads it — or this test does.  It
fails on a missing annotation as well, which is what keeps "typed wherever
possible" true after today: a new function without one cannot be committed
green.
"""

import ast
import importlib
import inspect
import pathlib
import typing
import unittest

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "vigo"


def modules() -> list[str]:
    return [f"vigo.{path.stem}" for path in sorted(PACKAGE.glob("*.py"))]


def functions(path: pathlib.Path) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]


def gaps(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """The parameters and the return that carry no annotation."""
    arguments = node.args
    named = [item for item in arguments.posonlyargs + arguments.args + arguments.kwonlyargs
             if item.arg not in ("self", "cls")]
    if arguments.vararg:
        named.append(arguments.vararg)
    if arguments.kwarg:
        named.append(arguments.kwarg)
    missing = [item.arg for item in named if item.annotation is None]
    if node.returns is None:
        missing.append("return")
    return missing


# The names ruff moves out of `typing` (UP035): they live in `collections.abc`
# and nothing at runtime cares, so only the linter would have caught it.
ABC_NAMES = {
    "Awaitable", "Callable", "Collection", "Container", "Coroutine", "Generator",
    "Hashable", "ItemsView", "Iterable", "Iterator", "KeysView", "Mapping",
    "MutableMapping", "MutableSequence", "MutableSet", "Reversible", "Sequence",
    "Set", "Sized", "ValuesView",
}


class TypingTest(unittest.TestCase):
    def test_every_function_is_annotated(self):
        unannotated = []
        for path in sorted(PACKAGE.glob("*.py")):
            for node in functions(path):
                missing = gaps(node)
                if missing:
                    unannotated.append(f"{path.name}: {node.name} ({', '.join(missing)})")
        self.assertEqual(unannotated, [], "these need annotations")

    def test_abc_names_come_from_collections(self):
        """The rule ruff caught in CI, checked here so a push does not need it."""
        wrong = []
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "typing":
                    for name in node.names:
                        if name.name in ABC_NAMES:
                            wrong.append(f"{path.name}: {name.name}")
        self.assertEqual(wrong, [], "import these from collections.abc")

    def test_datetime_uses_the_utc_alias(self):
        """UP017: `datetime.UTC` rather than `datetime.timezone.utc`."""
        wrong = [
            str(path) for path in sorted(PACKAGE.glob("*.py"))
            if "datetime.timezone.utc" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(wrong, [])

    def test_every_annotation_resolves(self):
        """A name in an annotation that does not exist is a bug only a checker sees."""
        broken = []
        for name in modules():
            module = importlib.import_module(name)
            for attribute in vars(module).values():
                if inspect.isclass(attribute) and attribute.__module__ == name:
                    members = [attribute] + [
                        member for _member_name, member in vars(attribute).items()
                        if isinstance(member, (staticmethod, classmethod, property))
                    ]
                else:
                    members = [attribute]
                for member in members:
                    target = getattr(member, "__func__", member)
                    if not (inspect.isfunction(target) and target.__module__ == name):
                        continue
                    try:
                        typing.get_type_hints(target, vars(module))
                    except Exception as error:  # the message is the point
                        broken.append(f"{name}.{target.__name__}: {error}")
        self.assertEqual(broken, [], "these annotations do not resolve")
