"""Task 12 — XSS guard: no untrusted value may reach an HTML sink.

Scans the frontend (src/) and extension (extension/src/) sources for
HTML sinks (``innerHTML`` assignments, ``outerHTML``,
``document.write``, ``insertAdjacentHTML``) and proves, for every
``innerHTML`` assignment, that every value interpolated or
concatenated into the markup is either:

* wrapped in an approved escaper (``escapeHtml`` / ``safe`` /
  ``linkify`` / ``renderMarkdown`` / ``formatResultHtml`` /
  ``formatSummary``) — the escaper neutralises the value, so its
  argument is never inspected;
* produced by a trusted builder (``tag`` / ``button`` escape their
  data arguments internally; ``artifactUrl`` builds a URL from
  encoded parts; ``actionIcon`` returns only a constant emoji);
* a compile-time constant, a number, a boolean, a ``.length`` count,
  or a formatted date — values that cannot carry markup;
* selected from a constant map by untrusted data (``names[p.provider]``)
  — the data only selects, it is never rendered.

Anything else fails the test with the file and the offending
expression, so a new sink — or a new interpolation into an existing
one — cannot land without a human justifying it here.

The analyser is deliberately syntactic and conservative: it parses
template literals (including nested ``${}``), string concatenation,
ternaries, ``||``/``&&`` chains, and ``.map(...).join('')`` over
constant arrays. If it cannot prove a value safe, it fails closed.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = [REPO_ROOT / "src", REPO_ROOT / "extension" / "src"]
SCAN_FILES = [REPO_ROOT / "agent.html", REPO_ROOT / "index.html"]

# Calls whose return value is safe HTML by construction.
ESCAPERS = {"escapeHtml", "safe", "linkify", "renderMarkdown",
            "formatResultHtml", "formatSummary"}
# Calls that escape their data arguments internally (see their
# definitions: controlViews.ts `tag`/`button`; sanitize.ts
# `artifactUrl` builds from encoded parts; taskconsole.ts /
# agentApp.ts `actionIcon` returns one constant emoji).
TRUSTED_BUILDERS = {"tag", "button", "artifactUrl", "actionIcon"}
# Passthrough builders: their return is safe only if every argument is.
PASSTHROUGH_BUILDERS = {"card", "empty"}
# Identifiers whose values are constants / numbers / already-safe HTML,
# per file. Keep this list short and justified — it is the only place
# a bare identifier is trusted.
SAFE_IDENTIFIERS: dict[str, set[str]] = {
    "src/controlCenter.ts": {"views", "icons", "i", "v"},
    "src/agentApp.ts": {
        "WORKING_MISSION_HTML", "START_MISSION_HTML",  # constant templates
        "badgeCls",  # class built only from a whitelisted format token
        "sizeStr",  # formatBytes() numeric string ("1.2 KB")
        "tabInfo", "childInfo",  # number-derived strings (" · Tab #3")
        "isOk",  # boolean
    },
    "src/controlViews.ts": {
        "modeBadge", "metricsHtml", "controlsHtml", "subtabsHtml",
        # ^ constant templates / card() output built from safe() data
        "names", "guides",  # constant lookup maps (data only selects)
        "active",  # missions.filter(...).length — a number, not text
        # Task 12 Privacy Center cards: each is card() output whose
        # dynamic values all pass through safe() (provider/model,
        # tracker note/hosts, DPIA summary/url) or are compile-time
        # constants ('On'/'Off', the 11 audit event names).
        "plannerCard", "auditCard", "trackerCard", "dpiaCard",
        # Task 14 connectors card: card() output whose only dynamic
        # value is a boolean-derived constant ('Configured' /
        # 'Not connected') — no user or API data reaches it.
        "connectorsCard",
    },
    "src/taskconsole.ts": set(),
    "extension/src/companion/companionDrawer.ts": set(),
    "extension/src/companion/companionElement.ts": set(),
}

JS_KEYWORDS = {
    "true", "false", "null", "undefined", "this", "typeof", "instanceof",
    "in", "of", "new", "void", "delete",
}


# ---------------------------------------------------------------------------
# Minimal JS scanner: finds .innerHTML assignments and their RHS text,
# respecting strings, template literals (with nested ${}), and comments.
# ---------------------------------------------------------------------------

def _scan_expr(text: str, i: int, mode: str = "code"):
    """Scan a JS expression starting at ``i``.

    Returns (end_index, interpolations) where interpolations are the
    raw texts of every ${...} found at any nesting depth inside the
    scanned expression. Stops at the ';' that ends the statement.

    ``mode`` is the starting scanner mode: "code" for a statement, or
    "bt" when scanning the body of a template literal (the caller has
    already consumed the opening backtick). Starting a template body
    in "code" mode misses every ${...} inside it — that was a real
    hole: the guard passed template interpolations sight unseen
    (review, Task 12).
    """
    n = len(text)
    depth = 0
    interps: list[str] = []
    j = i
    while j < n:
        c = text[j]
        nxt = text[j + 1] if j + 1 < n else ""
        if mode == "code":
            if c == "'":
                mode = "sq"
            elif c == '"':
                mode = "dq"
            elif c == "`":
                mode = "bt"
            elif c == "/" and nxt == "/":
                mode = "lc"
            elif c == "/" and nxt == "*":
                mode = "bc"
            elif c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
                if depth < 0:
                    return j, interps
            elif c == ";" and depth == 0:
                return j, interps
        elif mode == "sq":
            if c == "\\":
                j += 1
            elif c == "'":
                mode = "code"
        elif mode == "dq":
            if c == "\\":
                j += 1
            elif c == '"':
                mode = "code"
        elif mode == "bt":
            if c == "\\":
                j += 1
            elif c == "`":
                mode = "code"
            elif c == "$" and nxt == "{":
                end, _inner = _scan_braced(text, j + 2)
                # Only the outermost interpolation is recorded. A
                # nested ${...} lives inside this interpolation's own
                # expression, and classifying the outer interpolation
                # recurses into it (or the escaper/trusted-builder
                # call wrapping it neutralises it). Recording nested
                # ones separately double-counts them without their
                # context — e.g. artifactUrl(`${API}...`) flagged the
                # inner API/artId as unescaped (review, Task 12).
                interps.append(text[j + 2:end])
                j = end
        elif mode == "lc":
            if c == "\n":
                mode = "code"
        elif mode == "bc":
            if c == "*" and nxt == "/":
                mode = "code"
                j += 1
        j += 1
    return n, interps


def _scan_braced(text: str, i: int):
    """Scan from just after '${' to its matching '}'. Returns
    (end_index_just_past_'}', nested_interpolations)."""
    n = len(text)
    depth = 1
    interps: list[str] = []
    mode = "code"
    j = i
    while j < n:
        c = text[j]
        nxt = text[j + 1] if j + 1 < n else ""
        if mode == "code":
            if c == "'":
                mode = "sq"
            elif c == '"':
                mode = "dq"
            elif c == "`":
                mode = "bt"
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return j, interps
        elif mode == "sq":
            if c == "\\":
                j += 1
            elif c == "'":
                mode = "code"
        elif mode == "dq":
            if c == "\\":
                j += 1
            elif c == '"':
                mode = "code"
        elif mode == "bt":
            if c == "\\":
                j += 1
            elif c == "`":
                mode = "code"
            elif c == "$" and nxt == "{":
                end, _inner = _scan_braced(text, j + 2)
                # As in _scan_expr: only the outermost interpolation
                # is recorded; nested ones are covered by recursion.
                interps.append(text[j + 2:end])
                j = end
        j += 1
    return n, interps


def _find_innerhtml_sites(text: str):
    """Yield (line_number, rhs_text, interpolations) for each
    `.innerHTML = ...` / `.innerHTML += ...` assignment."""
    sites = []
    for m in re.finditer(r"\.innerHTML\s*\+?=", text):
        line_no = text.count("\n", 0, m.start()) + 1
        end, interps = _scan_expr(text, m.end())
        sites.append((line_no, text[m.end():end], interps))
    return sites


# ---------------------------------------------------------------------------
# Expression classifier: is this value provably safe to put into HTML?
# ---------------------------------------------------------------------------

def _split_top_level(expr: str, sep: str) -> list[str]:
    """Split on `sep` at depth 0, respecting strings/templates/parens."""
    parts: list[str] = []
    depth = 0
    mode = "code"
    cur: list[str] = []
    i = 0
    n = len(expr)
    while i < n:
        c = expr[i]
        nxt = expr[i + 1] if i + 1 < n else ""
        if mode == "code":
            if c == "'":
                mode = "sq"
                cur.append(c)
            elif c == '"':
                mode = "dq"
                cur.append(c)
            elif c == "`":
                mode = "bt"
                cur.append(c)
            elif c in "([{":
                depth += 1
                cur.append(c)
            elif c in ")]}":
                depth -= 1
                cur.append(c)
            elif depth == 0 and expr.startswith(sep, i):
                parts.append("".join(cur))
                cur = []
                i += len(sep)
                continue
            else:
                cur.append(c)
        elif mode == "sq":
            cur.append(c)
            if c == "\\" and i + 1 < n:
                cur.append(expr[i + 1])
                i += 1
            elif c == "'":
                mode = "code"
        elif mode == "dq":
            cur.append(c)
            if c == "\\" and i + 1 < n:
                cur.append(expr[i + 1])
                i += 1
            elif c == '"':
                mode = "code"
        elif mode == "bt":
            cur.append(c)
            if c == "\\" and i + 1 < n:
                cur.append(expr[i + 1])
                i += 1
            elif c == "`":
                mode = "code"
            elif c == "$" and nxt == "{":
                cur.append(nxt)
                i += 1
        i += 1
    parts.append("".join(cur))
    return parts


def _strip_parens(expr: str) -> str:
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        inner = expr[1:-1]
        # Only strip if the parens really wrap the whole expression.
        if _split_top_level(inner, "+") or True:
            depth = 0
            balanced = True
            for ch in inner:
                if ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    depth -= 1
                    if depth < 0:
                        balanced = False
                        break
            if balanced and depth == 0:
                expr = inner.strip()
                continue
        break
    return expr


def _outermost_call(expr: str):
    """Split `expr` into (receiver, method, args) for its outermost call.

    Returns None when `expr` is not a call. `receiver` is '' for a bare
    function call like ``safe(x)``; otherwise it is the text before
    the final ``.method``.
    """
    expr = expr.strip()
    if not expr.endswith(")"):
        return None
    # Find the '(' matching the final ')'.
    depth = 0
    mode = "code"
    i = len(expr) - 1
    open_idx = -1
    while i >= 0:
        c = expr[i]
        if mode == "code":
            if c in ("'", '"'):
                mode = "sq" if c == "'" else "dq"
            elif c == "`":
                mode = "bt"
            elif c == ")":
                depth += 1
            elif c == "(":
                depth -= 1
                if depth == 0:
                    open_idx = i
                    break
        elif mode in ("sq", "dq"):
            if c == "\\":
                i -= 1
            elif (mode == "sq" and c == "'") or (mode == "dq" and c == '"'):
                mode = "code"
        elif mode == "bt":
            if c == "\\":
                i -= 1
            elif c == "`":
                mode = "code"
        i -= 1
    if open_idx < 0:
        return None
    callee = expr[:open_idx].strip()
    args = expr[open_idx + 1:-1]
    if not callee:
        return None
    if "." in callee:
        receiver, _, method = callee.rpartition(".")
        if not re.fullmatch(r"[A-Za-z_$][\w$]*", method):
            return None
        return receiver.strip(), method, args
    if re.fullmatch(r"[A-Za-z_$][\w$]*", callee):
        return "", callee, args
    return None


def _classify_block_body(body: str, safe_ids: set[str], context: str) -> str | None:
    """Classify a block-bodied arrow callback: `const` bindings first,
    then the `return` expression. A binding is trusted only if its own
    right-hand side is proven safe."""
    body = body.strip()
    if body.startswith("{") and body.endswith("}"):
        body = body[1:-1]
    scope = set(safe_ids)
    # const NAME = <expr>; bindings, in order.
    for m in re.finditer(r"\bconst\s+([A-Za-z_$][\w$]*)\s*=\s*", body):
        end, _ = _scan_expr(body, m.end())
        rhs = body[m.end():end]
        if _classify(rhs, scope, context) is None:
            scope.add(m.group(1))
    # return <expr>;
    m = re.search(r"\breturn\s+", body)
    if not m:
        return "block-bodied callback without a return"
    end, _ = _scan_expr(body, m.end())
    return _classify(body[m.end():end], scope, context)


def _map_callback_params(args: str) -> tuple[set[str], set[str], str]:
    """Split map/filter callback args into (bound params, index params, body).

    The SECOND callback parameter of .map() is always the numeric
    index, whatever the receiver is — so it is safe on its own. The
    first parameter is only a constant when the receiver is.
    """
    arrow = args.find("=>")
    if arrow == -1:
        return set(), set(), ""
    params_text = args[:arrow]
    body = args[arrow + 2:]
    params = set()
    for tok in re.findall(r"[A-Za-z_$][\w$]*", params_text):
        if tok not in ("any", "string", "number", "boolean", "unknown", "void"):
            params.add(tok)
    ordered = [tok for tok in re.findall(r"[A-Za-z_$][\w$]*", params_text)
               if tok not in ("any", "string", "number", "boolean", "unknown", "void")]
    index_params = {ordered[1]} if len(ordered) > 1 else set()
    return params, index_params, body


def _is_literal(expr: str) -> bool:
    expr = expr.strip()
    if expr in ("true", "false", "null", "undefined"):
        return True
    if re.fullmatch(r"[0-9][0-9_]*(?:\.[0-9]+)?", expr):
        return True
    # A SINGLE quoted literal: the whole expression must be one string.
    # The old first-char/last-char check also matched concatenations
    # like '<p>' + x + '</p>' (starts and ends with a quote) and waved
    # the unescaped value through — that was a real hole (review,
    # Task 12). An escape-aware fullmatch rejects those.
    if re.fullmatch(r"'(?:[^'\\]|\\.)*'", expr, re.S):
        return True
    if re.fullmatch(r'"(?:[^"\\]|\\.)*"', expr, re.S):
        return True
    return False


def _matching_bracket(text: str, i: int) -> int | None:
    """Index of the bracket closing the one at ``i``, or None.

    Respects strings, template literals and comments, so a ']' inside
    a string literal does not end the scan.
    """
    open_ch = text[i]
    close_ch = {"[": "]", "(": ")", "{": "}"}[open_ch]
    depth = 0
    mode = "code"
    j = i
    n = len(text)
    while j < n:
        c = text[j]
        nxt = text[j + 1] if j + 1 < n else ""
        if mode == "code":
            if c == "'":
                mode = "sq"
            elif c == '"':
                mode = "dq"
            elif c == "`":
                mode = "bt"
            elif c == "/" and nxt == "/":
                mode = "lc"
            elif c == "/" and nxt == "*":
                mode = "bc"
            elif c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    return j
        elif mode == "sq":
            if c == "\\":
                j += 1
            elif c == "'":
                mode = "code"
        elif mode == "dq":
            if c == "\\":
                j += 1
            elif c == '"':
                mode = "code"
        elif mode == "bt":
            if c == "\\":
                j += 1
            elif c == "`":
                mode = "code"
            elif c == "$" and nxt == "{":
                end, _ = _scan_braced(text, j + 2)
                j = end
        elif mode == "lc":
            if c == "\n":
                mode = "code"
        elif mode == "bc":
            if c == "*" and nxt == "/":
                mode = "code"
                j += 1
        j += 1
    return None


def _is_literal_array(expr: str) -> bool:
    """True for an array literal whose every element is a literal
    (e.g. ['auth_failure', 'allowlist_denied']). Values drawn from
    such an array are compile-time constants, so a .map() callback
    over it binds constant params."""
    expr = expr.strip()
    if not expr.startswith("["):
        return False
    end = _matching_bracket(expr, 0)
    if end is None or expr[end + 1:].strip():
        return False
    inner = expr[1:end]
    if not inner.strip():
        return True
    return all(_is_literal(p) for p in _split_top_level(inner, ","))


def _is_numeric(expr: str, safe_ids: set[str]) -> bool:
    """Numbers, counts, arithmetic on numbers — cannot carry markup."""
    expr = expr.strip()
    # Strip a leading `new Date(...)` formatted date.
    if re.match(r"^new Date\(.*\)\.toLocaleString\(\)$", expr, re.S):
        return True
    tokens = re.findall(r"[A-Za-z_$][\w$]*", expr)
    for tok in tokens:
        if tok in JS_KEYWORDS or tok in ("Math", "round", "length"):
            continue
        if tok in safe_ids:
            continue
        return False
    # No string literals, no other calls.
    if re.search(r"['\"`]", expr):
        return False
    if re.search(r"[A-Za-z_$][\w$]*\s*\(", expr) and "Math.round" not in expr:
        return False
    return True


def _classify(expr: str, safe_ids: set[str], context: str) -> str | None:
    """Return None if provably safe, else a reason string."""
    expr = _strip_parens(expr.strip())
    if not expr:
        return "empty interpolation"
    if _is_literal(expr):
        return None
    # Template literal: every interpolation inside must be safe.
    # Scan the body in "bt" mode (the opening backtick is consumed):
    # scanning in "code" mode misses every ${...} (review, Task 12).
    if expr.startswith("`"):
        _, interps = _scan_expr(expr, 1, mode="bt")
        for inner in interps:
            reason = _classify(inner, safe_ids, context)
            if reason:
                return reason
        return None
    # Ternary: the condition cannot inject; both branches must be safe.
    tern = _split_top_level(expr, "?")
    if len(tern) == 2:
        branches = _split_top_level(tern[1], ":")
        if len(branches) == 2:
            for branch in branches:
                reason = _classify(branch, safe_ids, context)
                if reason:
                    return reason
            return None
    # || / && / ??: every value operand must be safe.
    for op in ("||", "&&", "??"):
        parts = _split_top_level(expr, op)
        if len(parts) > 1:
            for part in parts:
                reason = _classify(part, safe_ids, context)
                if reason:
                    return reason
            return None
    # String concatenation: every operand must be safe on its own.
    # (String literals were already handled above; this covers
    # '...' + safe(x) + '...' and numeric a + b.)
    parts = _split_top_level(expr, "+")
    if len(parts) > 1:
        for part in parts:
            reason = _classify(part, safe_ids, context)
            if reason:
                return reason
        return None
    # Arithmetic: every operand must be safe on its own. As with '+',
    # an operand that is a number, a .length count, an escaped value,
    # or a map index is safe; anything unescaped fails. (A '-' split
    # also covers `receipts.length - i`.)
    for op in ("-", "*", "/", "%"):
        parts = _split_top_level(expr, op)
        if len(parts) > 1:
            for part in parts:
                if not part.strip():
                    continue  # leading unary minus, e.g. '-x'
                reason = _classify(part, safe_ids, context)
                if reason:
                    return reason
            return None
    call = _outermost_call(expr)
    if call is not None:
        receiver, method, args = call
        if not receiver:
            if method in ESCAPERS:
                return None  # escaper neutralises its argument
            if method in TRUSTED_BUILDERS:
                return None  # escapes its data args internally
            if method in PASSTHROUGH_BUILDERS:
                for arg in _split_top_level(args, ","):
                    reason = _classify(arg, safe_ids, context)
                    if reason:
                        return reason
                return None
            if method == "encodeURIComponent":
                return None
            if method == "String":
                return "String() does not escape — wrap in escapeHtml/safe"
            return f"untrusted call: {expr[:80]}"
        # Method call on a receiver.
        # .map(cb): the receiver is never rendered — only the callback's
        # return value is. When the receiver is a compile-time constant
        # (e.g. `views`), the callback params are constants too and are
        # safe inside the body; when the receiver is runtime data, the
        # params are NOT added — the body must prove safety itself with
        # safe()/escapeHtml() calls.
        if method == "map":
            params, index_params, body = _map_callback_params(args)
            if body:
                # The callback params are constants when the receiver
                # is a constant (safe_ids) OR a literal array of
                # literals — e.g. ['auth_failure', ...].map(e => ...).
                # The index param is always a number, whatever the
                # receiver is.
                scope = safe_ids | index_params | (
                    params if (
                        receiver.strip() in safe_ids or _is_literal_array(receiver)
                    ) else set()
                )
                body_stripped = body.strip()
                if body_stripped.startswith("{"):
                    # Block body: prove the const bindings, then the return.
                    reason = _classify_block_body(body_stripped, scope, context)
                else:
                    reason = _classify(body_stripped, scope, context)
                if reason:
                    return reason
                return None
            return f"map without arrow callback: {expr[:80]}"
        # .join('') / .flat(): the receiver's (already proven) value.
        if method in ("join", "flat"):
            return _classify(receiver, safe_ids, context)
        # .slice(...) then .map(...): handled by the map branch above
        # when map is outermost; a bare slice renders the receiver.
        if method == "slice":
            return _classify(receiver, safe_ids, context)
        # A formatted date is a number-derived string, never markup:
        # new Date(<timestamp> * 1000).toLocale{String,DateString,
        # TimeString}() yields locale date text (or "Invalid Date"
        # for garbage input) — it cannot carry markup.
        if method in ("toLocaleString", "toLocaleDateString", "toLocaleTimeString") and receiver.strip().startswith("new Date("):
            return None
        # Math.round/floor/ceil coerce their argument to a number
        # (or NaN), so the rendered result is numeric text, never
        # markup — whatever the argument is.
        if receiver.strip() == "Math" and method in ("round", "floor", "ceil"):
            return None
        return f"untrusted method call: {expr[:80]}"
    # Constant-map lookup: data only selects, never renders.
    m = re.fullmatch(r"([A-Za-z_$][\w$]*)\s*\[.*\]", expr, re.S)
    if m and m.group(1) in safe_ids:
        return None
    # Literal-array indexing: ['a', 'b'][index] — the array is
    # constant, so the index only selects a compile-time string.
    if expr.startswith("["):
        end = _matching_bracket(expr, 0)
        if end is not None and _is_literal_array(expr[: end + 1]):
            rest = expr[end + 1:].strip()
            if rest.startswith("["):
                idx_end = _matching_bracket(rest, 0)
                if idx_end is not None and not rest[idx_end + 1:].strip():
                    return None
    # Bare identifier / property access on a safe identifier.
    m = re.fullmatch(r"([A-Za-z_$][\w$]*)((?:\.[A-Za-z_$][\w$]*)*)", expr)
    if m:
        if m.group(1) in safe_ids or m.group(1) in JS_KEYWORDS:
            return None
        # `.length` counts are numeric and safe.
        if m.group(2).endswith(".length") and not re.search(r"\.\w+\(", expr):
            return None
        return f"unescaped identifier: {expr[:80]}"
    if _is_numeric(expr, safe_ids):
        return None
    return f"unclassified value: {expr[:80]}"


def _check_rhs(rhs: str, safe_ids: set[str], context: str) -> list[str]:
    """Check one innerHTML RHS. Returns a list of violation reasons."""
    violations: list[str] = []
    # Concatenation operands at the top level.
    for operand in _split_top_level(rhs, "+"):
        operand = operand.strip()
        if not operand:
            continue
        if _is_literal(operand):
            continue
        # A template literal operand: check its interpolations.
        # Scan the body in "bt" mode (opening backtick consumed):
        # scanning in "code" mode misses every ${...} (review, Task 12).
        if operand.startswith("`"):
            _, interps = _scan_expr(operand, 1, mode="bt")
            for inner in interps:
                reason = _classify(inner, safe_ids, context)
                if reason:
                    violations.append(reason)
            continue
        reason = _classify(operand, safe_ids, context)
        if reason:
            violations.append(reason)
    return violations


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_no_outer_html_document_write_or_insert_adjacent_html():
    bad = []
    for base in SCAN_DIRS:
        for path in sorted(base.rglob("*.ts")):
            text = path.read_text()
            for pat in ("outerHTML", "document.write", "insertAdjacentHTML"):
                if pat in text:
                    bad.append(f"{path.relative_to(REPO_ROOT)}: {pat}")
    for path in SCAN_FILES:
        text = path.read_text()
        for pat in ("outerHTML", "document.write", "insertAdjacentHTML"):
            if pat in text:
                bad.append(f"{path.relative_to(REPO_ROOT)}: {pat}")
    assert not bad, "forbidden HTML sinks present:\n" + "\n".join(bad)


def test_every_innerhtml_assignment_is_proven_safe():
    violations: list[str] = []
    sites = 0
    for base in SCAN_DIRS:
        for path in sorted(base.rglob("*.ts")):
            rel = str(path.relative_to(REPO_ROOT))
            safe_ids = SAFE_IDENTIFIERS.get(rel, set())
            text = path.read_text()
            for line_no, rhs, _ in _find_innerhtml_sites(text):
                sites += 1
                ctx = f"{rel}:{line_no}"
                for reason in _check_rhs(rhs, safe_ids, ctx):
                    violations.append(f"{ctx}: {reason}")
    for path in SCAN_FILES:
        text = path.read_text()
        for line_no, rhs, _ in _find_innerhtml_sites(text):
            sites += 1
            ctx = f"{path.relative_to(REPO_ROOT)}:{line_no}"
            for reason in _check_rhs(rhs, set(), ctx):
                violations.append(f"{ctx}: {reason}")
    assert sites > 0, "scanner found no innerHTML sites — it is broken, not clean"
    assert not violations, (
        f"{len(violations)} unescaped value(s) reach innerHTML:\n" + "\n".join(violations)
    )

# ---------------------------------------------------------------------------
# REVIEW (Task 12 audit) — regression tests for two holes the guard had.
#
# 1. _is_literal used a first-char/last-char check, so a concatenation
#    like '<p>' + x + '</p>' was treated as one string literal and the
#    unescaped value waved through.
# 2. Template literals were scanned starting in "code" mode, so no
#    ${...} inside a backtick template was ever inspected — including
#    the companion-drawer timeline sink this audit was supposed to
#    guard (reverting that fix did NOT fail the guard).
# Both are fixed above; these tests pin the fix. They also pin the
# narrow sound rules added while triaging the 9 sites the fixed
# guard newly surfaced (all false positives, verified by reading
# each site): literal-array .map() params, literal-array indexing,
# Math.round/floor/ceil, toLocaleDateString/toLocaleTimeString, and
# .map() index params in arithmetic.
# ---------------------------------------------------------------------------

def test_guard_rejects_quote_bounded_concatenation():
    # The old _is_literal passed this: starts and ends with a quote.
    safe_ids: set[str] = set()
    assert _classify("'<p>' + e.summary + '</p>'", safe_ids, "t") is not None
    assert _classify('"<p>" + e.summary + "</p>"', safe_ids, "t") is not None
    assert _classify("card('T', '<p>' + e.summary + '</p>')", safe_ids, "t") is not None
    # But a genuine single literal is still safe.
    assert _classify("'<p>hello</p>'", safe_ids, "t") is None
    assert _classify("'it\\'s fine'", safe_ids, "t") is None


def test_guard_rejects_template_literal_interpolation():
    safe_ids: set[str] = set()
    assert _classify("`<p>${e.summary}</p>`", safe_ids, "t") is not None
    assert _classify("`<li>${step}</li>`", safe_ids, "t") is not None
    # Escaped interpolations still pass.
    assert _classify("`<p>${safe(e.summary)}</p>`", safe_ids, "t") is None
    assert _classify("`<p>${escapeHtml(e.summary)}</p>`", safe_ids, "t") is None


def test_guard_catches_reverted_drawer_timeline_sink():
    # The exact pre-Task-12 drawer line: a page-derived narrative
    # interpolated into innerHTML. The guard must fail on it.
    rhs = "snapshot.timeline.map(step => `<li>${step}</li>`).join(\"\")"
    violations = _check_rhs(rhs, set(), "companionDrawer.ts:159")
    assert violations, "guard missed the reverted drawer sink"
    assert any("step" in v for v in violations)


def test_guard_accepts_constant_array_map_and_indexing():
    safe_ids: set[str] = set()
    # .map() over a literal array of literals binds constant params.
    assert _classify(
        "['auth_failure', 'allowlist_denied'].map(e => '<option value=\"' + e + '\">' + e + '</option>').join('')",
        safe_ids, "t") is None
    # Indexing a literal array only selects a compile-time string.
    assert _classify("['a', 'b'][index]", safe_ids, "t") is None
    # But .map() over runtime data with a raw param still fails.
    assert _classify(
        "info.events.map(e => '<p>' + e.summary + '</p>').join('')",
        safe_ids, "t") is not None


def test_guard_accepts_numeric_renderings():
    safe_ids: set[str] = set()
    assert _classify("Math.round((m.unnecessary_confidence || 0) * 100)", safe_ids, "t") is None
    assert _classify("new Date(f.expires_at * 1000).toLocaleDateString()", safe_ids, "t") is None
    assert _classify("new Date(a.created_at * 1000).toLocaleTimeString()", safe_ids, "t") is None
    # Map index param in arithmetic: receipts.length - i.
    assert _classify(
        "receipts.map((receipt, i) => '<small>' + (receipts.length - i) + '</small>').join('')",
        safe_ids, "t") is None
    # A raw runtime value in arithmetic still fails.
    assert _classify("receipts.length - e.summary", safe_ids, "t") is not None
