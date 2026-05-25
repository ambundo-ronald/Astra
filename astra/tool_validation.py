import json


def validate_arguments(tool, arguments):
    schema = tool.get("input_schema") or {}
    if not isinstance(arguments, dict):
        return ["Arguments must be a JSON object."]

    required = schema.get("required") or []
    errors = [f"Missing required argument: {name}" for name in required if name not in arguments]

    properties = schema.get("properties") or {}
    for name, rules in properties.items():
        if name not in arguments or not isinstance(rules, dict):
            continue
        expected = rules.get("type")
        value = arguments.get(name)
        if expected and not _matches_type(value, expected):
            errors.append(f"Argument '{name}' should be {expected}.")

    return errors


def build_repair_prompt(tool_name, arguments, errors, schema):
    return (
        "Repair this tool call. Return only JSON in the shape "
        '{"tool_call":{"name":"tool_name","arguments":{}}}.\n'
        f"Tool: {tool_name}\n"
        f"Schema: {_json(schema)}\n"
        f"Current arguments: {_json(arguments)}\n"
        f"Errors: {'; '.join(errors)}"
    )


def _matches_type(value, expected):
    if isinstance(expected, list):
        return any(_matches_type(value, item) for item in expected)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    return True


def _json(value):
    return json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":"))
