"""Offline validation of the supplied MVP documents and focused contract fixtures."""

import argparse
import hashlib
import json
import re
import shutil
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote, urlsplit

import jsonschema
import pymupdf
from markdown_it import MarkdownIt


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "contracts"
PDF_HASH = "b4ce91fa507e33605600d112b9eb6544e37ab653abaf331a87663600667feba1"
CHECKS = 0


def require(condition, message):
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(message)


def reject_constant(value):
    raise ValueError(f"Nonfinite JSON number: {value}")


def load_json(text):
    return json.loads(text, parse_float=Decimal, parse_constant=reject_constant)


def validate_schemas():
    document = load_json((CONTRACTS / "schemas.json").read_text(encoding="utf-8"))
    fixtures = load_json((CONTRACTS / "api-fixtures.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(document)

    def check(name, value, expected=True):
        schema = {
            "$schema": document["$schema"],
            "$defs": document["$defs"],
            "$ref": f"#/$defs/{name}",
        }
        validator = jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker()
        )
        errors = list(validator.iter_errors(value))
        require(bool(errors) != expected, f"{name}: expected valid={expected}: {errors}")

    require(not jsonschema.Draft202012Validator(document).is_valid({}),
            "Root schema must reject unspecified contracts")
    for case in fixtures["schemaCases"]:
        check(case["schema"], case["value"], case["valid"])
    for operation in fixtures["operations"]:
        if "input" in operation:
            require(operation["input"] in document["$defs"], "Unknown operation input")

    outline = (CONTRACTS / "api-and-schemas.md").read_text(encoding="utf-8")
    routes = set(re.findall(r"\| `(GET|POST|PUT|PATCH|DELETE) ([^`]+)` \|", outline))
    require(routes == {(op["method"], op["path"]) for op in fixtures["operations"]},
            "API fixtures must exactly cover the documented routes")
    require(len(routes) == len(fixtures["operations"]) == 19, "Duplicate/missing routes")

    for case in fixtures["learningCases"]:
        confidence = (Decimal(3) + case["supportUnits"]) / (
            Decimal(6) + case["supportUnits"] + case["oppositionUnits"]
        )
        eligible = (
            case["supportingCookedCount"] >= 2
            and confidence >= Decimal("0.65")
            and not case.get("explicitConflict", False)
            and case.get("currentEvidence", True)
        )
        require(eligible == case["eligible"], f"Learning boundary: {case['name']}")

    for name, field, limit, value in [
        ("groceryInput", "name", 100, {"name": ""}),
        ("sendInput", "content", 4000, {"content": ""}),
        ("feedbackInput", "note", 2000, {"rating": "liked"}),
        ("reasonInput", "reason", 2000, {}),
    ]:
        value[field] = "\U0001f34b" * limit
        check(name, value)
        value[field] += "x"
        check(name, value, False)
    for field, maximum in [
        ("allergies", 50), ("dietaryRestrictions", 20), ("likes", 50),
        ("dislikes", 50), ("preferredCuisines", 20), ("nutritionGoals", 20),
    ]:
        check("preferencePatch", {field: ["x"] * maximum})
        check("preferencePatch", {field: ["x"] * (maximum + 1)}, False)
        check("preferencePatch", {field: ["x" * 101]}, False)
    for quantity in [Decimal("0.001"), Decimal("1.001"), Decimal("999999.999")]:
        check("groceryPatch", {"quantity": quantity})
    for raw in ["NaN", "Infinity", "-Infinity"]:
        try:
            load_json(raw)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Accepted nonfinite number {raw}")

    uid = "11111111-1111-4111-8111-111111111111"
    other = "22222222-2222-4222-8222-222222222222"
    timestamp = "2026-10-07T20:00:00Z"
    base = {"id": uid, "userId": "synthetic-user", "schemaVersion": 1, "createdAt": timestamp}
    preferences = next(
        c["value"] for c in fixtures["schemaCases"]
        if c["name"] == "complete explicitly confirmed onboarding"
    )
    candidate = {
        "insightKey": "citrus", "insight": "prefers citrus-forward sauces",
        "confidence": Decimal("0.75"), "evidenceCount": 2, "supportingCookedCount": 2,
        "supportUnits": 6, "oppositionUnits": 0, "source": "recipe_feedback",
        "lastUpdated": timestamp, "latestEvidenceAt": timestamp, "policyVersion": "1",
    }
    check("learnedCandidate", candidate)
    profile = dict(base, updatedAt=timestamp, onboardingCompleted=True,
                   groceries=[], explicitPreferences=preferences, learnedPreferences=[candidate])
    check("usersProfile", profile)
    check("usersProfile", dict(profile, learnedPreferences=[candidate] * 51), False)
    grocery = dict(groceryId=other, name="Lime", quantity=None, unit=None, updatedAt=timestamp)
    check("usersProfile", dict(profile, groceries=[grocery] * 200))
    check("usersProfile", dict(profile, groceries=[grocery] * 201), False)
    check("usersProfile", dict(profile, schemaVersion=2), False)
    check("chatConversation", dict(base, conversationId=other, title="Dinner",
                                  nextTurnIndex=0, updatedAt=timestamp))

    user_message = dict(messageId=uid, role="user", entityId="synthetic-user",
                        content="Dinner with lime", timestamp=timestamp)
    assistant_message = dict(messageId=other, role="assistant", entityId="orchestrator",
                             content="Validated recipe follows.", timestamp=timestamp)
    turn = dict(base, conversationId=other, turnIndex=0, status="pending",
                messages=[user_message], updatedAt=timestamp)
    for status in ["pending", "failed", "outcomeUnknown"]:
        check("chatTurn", dict(turn, status=status))
        check("chatTurn", dict(turn, status=status, messages=[user_message, assistant_message]), False)
    check("chatTurn", dict(turn, status="completed"), False)
    check("chatTurn", dict(turn, status="completed", messages=[user_message, assistant_message]))
    check("chatTurn", dict(turn, status="completed", messages=[assistant_message, user_message]), False)
    check("chatTurn", dict(turn, status="completed",
                          messages=[user_message, assistant_message, assistant_message]), False)

    # These are structural projections, not claims of safe or nutritionally complete recipes.
    recipe = {
        "generatedRecipeId": uid, "occasionId": other, "title": "Synthetic recipe",
        "servings": 1, "minutes": 10, "skill": "beginner", "fitReasons": [],
        "ingredients": [], "instructions": [], "shoppingItems": [],
        "nutrition": {"status": "unavailable"}, "sources": [], "safetyProfileRevision": "etag-1",
    }
    check("recipeSnapshot", recipe)
    check("recipeSnapshot", dict(recipe, cookedAt=timestamp), False)
    for field, maximum in [("ingredients", 30), ("instructions", 30), ("sources", 12)]:
        check("recipeSnapshot", dict(recipe, **{field: [{}] * maximum}))
        check("recipeSnapshot", dict(recipe, **{field: [{}] * (maximum + 1)}), False)
    for field, valid, invalid in [("servings", 20, 21), ("minutes", 1440, 1441)]:
        check("recipeSnapshot", dict(recipe, **{field: valid}))
        check("recipeSnapshot", dict(recipe, **{field: invalid}), False)
        check("recipeSnapshot", dict(recipe, **{field: 0}), False)
    saved = dict(base, historyId=uid, occasionId=other, generatedRecipeId=uid, snapshot=recipe,
                 accepted=True, savedAt=timestamp, cookedAt=None, rating=None, note=None,
                 feedbackRevision=0, processingStatus="notEligible", updatedAt=timestamp)
    check("recipeHistory", saved)
    cooked = dict(saved, cookedAt=timestamp)
    check("recipeHistory", cooked)
    check("recipeHistory", dict(saved, rating="liked", feedbackRevision=1), False)
    check("recipeHistory", dict(cooked, rating="liked", feedbackRevision=0), False)
    check("recipeHistory", dict(cooked, rating="liked", feedbackRevision=1, processingStatus="pending"))
    check("recipeHistory", dict(cooked, feedbackRevision=1), False)
    check("feedbackRevision", dict(base, historyId=uid, occasionId=other,
                                  rating="fine", note="Too salty.", feedbackRevision=1))
    check("feedbackRevision", dict(base, historyId=uid, occasionId=other,
                                  rating="fine", note=None, feedbackRevision=0), False)
    check("behavior", dict(base, occasionId=other, generatedRecipeId=uid,
                          kind="saved", behaviorRevision=1))
    evidence = dict(historyId=uid, occasionId=other, insightKey="salt",
                    feedbackRevision=1, excerpt="Too salty.")
    check("evidenceAssertion", evidence)
    check("evidenceAssertion", dict(evidence, behaviorRevision=1), False)
    check("evidenceAssertion", dict(generatedRecipeId=uid, occasionId=other,
                                   insightKey="citrus", behaviorRevision=1))

    # Resolve every local schema reference, including entries not used by a fixture yet.
    def references(node):
        if isinstance(node, dict):
            if "$ref" in node:
                require(node["$ref"].startswith("#/$defs/"), "Unexpected remote schema reference")
                require(node["$ref"].split("/")[-1] in document["$defs"], "Broken schema reference")
            for value in node.values():
                references(value)
        elif isinstance(node, list):
            for value in node:
                references(value)

    references(document)
    print(f"Schema fixtures: {len(fixtures['schemaCases'])}; learning boundaries: "
          f"{len(fixtures['learningCases'])}; documented operations: {len(routes)}")


def heading_slug(text):
    return re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")


def validate_markdown(render_dir):
    parser = MarkdownIt("commonmark").enable("table")
    paths = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    parsed = {}
    anchors = {}
    table_count = 0
    json_count = 0
    link_count = 0
    for path in paths:
        text = path.read_text(encoding="utf-8")
        tokens = parser.parse(text)
        parsed[path] = (text, tokens)
        require(sum(t.type == "heading_open" and t.tag == "h1" for t in tokens) == 1,
                f"{path}: expected exactly one H1")
        slugs = set()
        used = {}
        for index, token in enumerate(tokens):
            if token.type == "heading_open":
                inline = tokens[index + 1]
                plain = "".join(c.content for c in inline.children if c.type in ("text", "code_inline"))
                slug = heading_slug(plain)
                count = used.get(slug, 0)
                used[slug] = count + 1
                slug = f"{slug}-{count}" if count else slug
                slugs.add(slug)
                token.attrSet("id", slug)
            if token.type == "table_open":
                table_count += 1
            if token.type == "fence":
                closing = text.splitlines()[token.map[1] - 1].strip()
                require(closing == token.markup, f"{path}: unclosed code fence")
                require(token.info.strip() in {"json", "text", "sh"}, f"{path}: unexpected fence language")
                if token.info.strip() == "json":
                    load_json(token.content)
                    json_count += 1
        anchors[path] = slugs
        if render_dir:
            for token in tokens:
                for child in token.children or []:
                    attribute = "href" if child.type == "link_open" else (
                        "src" if child.type == "image" else None
                    )
                    target = child.attrGet(attribute) if attribute else None
                    if not target:
                        continue
                    url = urlsplit(target)
                    if url.scheme or url.netloc or not url.path:
                        continue
                    source = (path.parent / unquote(url.path)).resolve()
                    require(source.is_relative_to(ROOT) and source.exists(),
                            f"{path}: invalid preview asset {target}")
                    if source.suffix == ".md":
                        child.attrSet(attribute, url.path[:-3] + ".html" + (
                            "#" + url.fragment if url.fragment else ""
                        ))
                    else:
                        asset = render_dir / source.relative_to(ROOT)
                        asset.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(source, asset)
        html = parser.renderer.render(tokens, parser.options, {})
        require("<h1" in html, f"{path}: missing rendered title")
        require(html.count("<table>") == sum(t.type == "table_open" for t in tokens),
                f"{path}: tables failed to render")
        if render_dir:
            destination = render_dir / path.relative_to(ROOT).with_suffix(".html")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                '<!doctype html><html lang="en"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>MVP contracts preview</title>'
                '<style>body{font:16px system-ui;max-width:1100px;margin:2em auto;padding:1em}'
                'table{border-collapse:collapse}td,th{border:1px solid #aaa;padding:.5em}'
                'pre{overflow:auto;background:#eee;padding:1em}img{max-width:100%}</style>'
                '<body>' + html + "</body></html>", encoding="utf-8"
            )
    for path, (text, _) in parsed.items():
        tokens = parser.parse(text)
        for token in tokens:
            for child in token.children or []:
                target = child.attrGet("href") if child.type == "link_open" else (
                    child.attrGet("src") if child.type == "image" else None
                )
                if target is None:
                    continue
                url = urlsplit(target)
                if url.scheme or url.netloc:
                    continue
                link_count += 1
                resolved = (path.parent / unquote(url.path)).resolve() if url.path else path
                require(resolved.is_relative_to(ROOT), f"{path}: link escapes repository: {target}")
                require(resolved.exists(), f"{path}: missing link target {target}")
                if url.fragment:
                    require(unquote(url.fragment) in anchors.get(resolved, set()),
                            f"{path}: broken anchor {target}")
    print(f"Markdown rendered: {len(paths)} documents, {table_count} tables, "
          f"{json_count} JSON fences, {link_count} local links/anchors")


def validate_diagram():
    folder = ROOT / "docs" / "references"
    pdf_path = folder / "architecture-diagram.pdf"
    require(hashlib.sha256(pdf_path.read_bytes()).hexdigest() == PDF_HASH, "PDF hash differs from attachment")
    adr = (ROOT / "docs" / "adr" / "0001-mvp-contracts.md").read_text(encoding="utf-8")
    require(PDF_HASH in adr, "ADR diagram hash mismatch")
    with pymupdf.open(pdf_path) as pdf:
        require(len(pdf) == 1, "Expected single-page supplied PDF")
        page = pdf[0]
        expected = page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False)
        actual = pymupdf.Pixmap(str(folder / "architecture-diagram.png"))
        require((actual.width, actual.height) == (expected.width, expected.height), "Preview dimensions differ")
        require(actual.samples == expected.samples, "Preview pixels differ from PDF rendering")
        for label in ["GPT-5.4-mini", "GPT-5.4", "GPT-5.1 nano", "Foundry IQ", "Web IQ"]:
            require(label in page.get_text(), f"Missing diagram label: {label}")
        print(f"Diagram: 1 PDF page; SHA-256 {PDF_HASH}; PNG {actual.width}x{actual.height}, pixel match")


def main():
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("--render-dir", type=Path, help="Optional HTML output directory outside the repository")
    args = arguments.parse_args()
    if args.render_dir:
        require(not args.render_dir.resolve().is_relative_to(ROOT), "Keep rendered artifacts outside the repo")
    validate_schemas()
    validate_markdown(args.render_dir)
    validate_diagram()
    print(f"PASS: {CHECKS} offline contract/document assertions (not application or live-service tests)")


if __name__ == "__main__":
    main()
