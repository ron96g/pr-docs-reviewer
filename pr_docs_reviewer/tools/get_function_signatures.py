"""Tool: get_function_signatures — extracts function/class signatures from source."""

import ast
import re

from google.adk.tools import ToolContext

from .read_file_contents import read_file_contents


def get_function_signatures(
    file_path: str,
    tool_context: ToolContext,
    ref: str = "HEAD",
) -> dict:
    """
    Extracts function and class signatures from a source file in the repository.

    The repository is automatically determined from the PR being analyzed
    (stored in session state).

    Args:
        file_path: Path to the file within the repo.
        ref: Git ref to read from.

    Returns:
        dict with key "signatures" containing a list of:
            - name: str (function/class name)
            - type: "function" | "class" | "method"
            - signature: str (full signature line including params)
            - line_number: int
            - docstring_summary: str or null
    """
    # Fetch the file content
    file_result = read_file_contents(file_path, tool_context, ref=ref)
    if file_result["status"] == "error":
        return file_result

    content = file_result["content"]

    # Use AST for Python files, regex fallback for others
    if file_path.endswith(".py"):
        return _extract_python_signatures(content)
    else:
        return _extract_regex_signatures(content, file_path)


def _extract_python_signatures(source: str) -> dict:
    """Use Python's AST to extract precise signatures from Python source."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return {"status": "error", "error_message": f"Python syntax error: {e}"}

    signatures = []
    source_lines = source.splitlines()

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sig = _format_function_sig(node, source_lines)
            # Determine if it's a method (inside a class) or standalone
            sig_type = "function"
            for parent in ast.walk(tree):
                if isinstance(parent, ast.ClassDef):
                    for child in ast.iter_child_nodes(parent):
                        if child is node:
                            sig_type = "method"
                            sig["name"] = f"{parent.name}.{node.name}"
                            break

            sig["type"] = sig_type
            signatures.append(sig)

        elif isinstance(node, ast.ClassDef):
            docstring = ast.get_docstring(node)
            signatures.append({
                "name": node.name,
                "type": "class",
                "signature": f"class {node.name}" + _format_bases(node),
                "line_number": node.lineno,
                "docstring_summary": _first_line(docstring),
            })

    # Sort by line number for stable output
    signatures.sort(key=lambda s: s["line_number"])

    return {"signatures": signatures}


def _format_function_sig(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    source_lines: list[str],
) -> dict:
    """Format a function signature from an AST node."""
    # Reconstruct signature from AST for accuracy
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    params = _format_params(node.args)
    returns = ""
    if node.returns:
        returns = f" -> {ast.unparse(node.returns)}"

    signature = f"{prefix} {node.name}({params}){returns}"
    docstring = ast.get_docstring(node)

    return {
        "name": node.name,
        "type": "function",  # may be overwritten to "method"
        "signature": signature,
        "line_number": node.lineno,
        "docstring_summary": _first_line(docstring),
    }


def _format_params(args: ast.arguments) -> str:
    """Format function parameters from AST arguments node."""
    parts = []

    # Count positional args without defaults
    num_no_default = len(args.args) - len(args.defaults)

    for i, arg in enumerate(args.args):
        param = arg.arg
        if arg.annotation:
            param += f": {ast.unparse(arg.annotation)}"
        # Add default value if this arg has one
        default_idx = i - num_no_default
        if default_idx >= 0 and default_idx < len(args.defaults):
            param += f" = {ast.unparse(args.defaults[default_idx])}"
        parts.append(param)

    if args.vararg:
        va = f"*{args.vararg.arg}"
        if args.vararg.annotation:
            va += f": {ast.unparse(args.vararg.annotation)}"
        parts.append(va)
    elif args.kwonlyargs:
        parts.append("*")

    for i, arg in enumerate(args.kwonlyargs):
        param = arg.arg
        if arg.annotation:
            param += f": {ast.unparse(arg.annotation)}"
        if i < len(args.kw_defaults) and args.kw_defaults[i] is not None:
            param += f" = {ast.unparse(args.kw_defaults[i])}"
        parts.append(param)

    if args.kwarg:
        kw = f"**{args.kwarg.arg}"
        if args.kwarg.annotation:
            kw += f": {ast.unparse(args.kwarg.annotation)}"
        parts.append(kw)

    return ", ".join(parts)


def _format_bases(node: ast.ClassDef) -> str:
    """Format class base classes."""
    if not node.bases:
        return ""
    bases = [ast.unparse(b) for b in node.bases]
    return f"({', '.join(bases)})"


def _first_line(docstring: str | None) -> str | None:
    """Return the first non-empty line of a docstring."""
    if not docstring:
        return None
    for line in docstring.strip().splitlines():
        line = line.strip()
        if line:
            return line
    return None


def _extract_regex_signatures(source: str, file_path: str) -> dict:
    """Regex-based fallback for non-Python files."""
    signatures = []

    # Language-specific patterns
    patterns = [
        # JavaScript/TypeScript
        (r"(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)", "function"),
        (r"(?:export\s+)?class\s+(\w+)(?:\s+extends\s+\w+)?", "class"),
        (r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(([^)]*)\)\s*=>", "function"),
        # Go
        (r"func\s+(?:\(\w+\s+\*?(\w+)\)\s+)?(\w+)\s*\(([^)]*)\)", "function"),
        # Rust
        (r"(?:pub\s+)?fn\s+(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)", "function"),
        (r"(?:pub\s+)?struct\s+(\w+)", "class"),
        (r"(?:pub\s+)?enum\s+(\w+)", "class"),
    ]

    for line_num, line in enumerate(source.splitlines(), 1):
        for pattern, sig_type in patterns:
            match = re.search(pattern, line)
            if match:
                name = match.group(1) or match.group(2) if match.lastindex >= 2 else match.group(1)
                signatures.append({
                    "name": name,
                    "type": sig_type,
                    "signature": line.strip(),
                    "line_number": line_num,
                    "docstring_summary": None,
                })
                break  # one match per line

    return {"signatures": signatures}
