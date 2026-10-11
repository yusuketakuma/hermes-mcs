"""Exit 0 when a pyproject's [project] requires-python admits the running interpreter."""
import re
import sys
import tomllib

_CLAUSE = re.compile(r"(>=|<=|==|!=|>|<)\s*([0-9]+(?:\.[0-9]+)*)")


def admits(spec, version) -> bool:
    """Plain >=,>,<=,<,==,!= clauses only; anything else is not proven."""
    clauses = [c.strip() for c in spec.split(",")] if isinstance(spec, str) else []
    if not clauses or not all(clauses):
        return False
    for clause in clauses:
        m = _CLAUSE.fullmatch(clause)
        if not m:
            return False
        want = tuple(int(x) for x in m[2].split("."))
        width = max(len(want), len(version))
        have = tuple(version) + (0,) * (width - len(version))
        want = want + (0,) * (width - len(want))
        if not {">=": have >= want, "<=": have <= want, "==": have == want,
                "!=": have != want, ">": have > want, "<": have < want}[m[1]]:
            return False
    return True


def main(argv) -> int:
    # mcs-requires-python-admits (the installer test stub delegates on this name)
    try:
        with open(argv[1], "rb") as stream:
            project = tomllib.load(stream).get("project")
    except (IndexError, OSError, ValueError, RecursionError):
        return 1
    spec = project.get("requires-python") if isinstance(project, dict) else None
    return 0 if admits(spec, tuple(sys.version_info[:3])) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
