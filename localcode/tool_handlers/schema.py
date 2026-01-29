"""
Tool schema building: build_tools(), make_openai_tools(), feedback helpers.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

# Type aliases (matches localcode.py)
ToolTuple = Tuple[str, Dict[str, Any], Any, Dict[str, Any], Dict[str, Any]]
ToolsDict = Dict[str, ToolTuple]


def build_tools(
    tool_defs: Dict[str, Dict[str, Any]],
    handlers: Dict[str, Any],
    tool_order: List[str],
) -> ToolsDict:
    tools: ToolsDict = {}
    for name in tool_order:
        tool_def = tool_defs.get(name)
        if not tool_def:
            raise ValueError(f"Missing tool definition for '{name}'")
        description = tool_def.get("description")
        params = tool_def.get("parameters")
        schema: Dict[str, Any] = {}
        if "additionalProperties" in tool_def:
            schema["additionalProperties"] = bool(tool_def.get("additionalProperties"))
        feedback = tool_def.get("feedback") or {}
        if not isinstance(feedback, dict):
            feedback = {}
        handler_name = tool_def.get("handler", name)
        handler = handlers.get(handler_name)
        if description is None or params is None or handler is None:
            raise ValueError(f"Invalid tool definition: {name}")
        tools[name] = (description, params, handler, schema, feedback)
    return tools


def render_tool_description(desc: str, display_map: Optional[Dict[str, str]]) -> str:
    if not desc or not display_map:
        return desc

    def _replace(match: re.Match[str]) -> str:
        token = match.group(1).strip()
        if not token:
            return match.group(0)
        return display_map.get(token, token) if token in display_map else match.group(0)

    return re.sub(r"\{\{\s*tool:\s*([^}]+?)\s*\}\}", _replace, desc)


def make_openai_tools(
    tools_dict: ToolsDict,
    display_map: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for name, tool_info in tools_dict.items():
        display = display_map.get(name, name) if display_map else name
        desc = render_tool_description(
            tool_info[0],
            display_map,
        )
        params = tool_info[1]
        schema = tool_info[3] if len(tool_info) > 3 else {}
        properties = {}
        required = []
        for pn, pt in (params or {}).items():
            base_type = None
            is_optional = False
            description = None
            default = None
            default_set = False
            min_length = None
            if isinstance(pt, str):
                is_optional = pt.endswith("?")
                base_type = pt.rstrip("?")
            elif isinstance(pt, dict):
                type_val = pt.get("type")
                if not isinstance(type_val, str):
                    continue
                is_optional = bool(pt.get("optional", False))
                if type_val.endswith("?"):
                    is_optional = True
                    type_val = type_val.rstrip("?")
                base_type = type_val
                if isinstance(pt.get("description"), str):
                    description = pt["description"]
                if "default" in pt:
                    default_set = True
                    default = pt["default"]
                if isinstance(pt.get("minLength"), int):
                    min_length = pt["minLength"]
            if not base_type:
                continue
            if base_type == "array":
                prop = {"type": "array"}
                if isinstance(pt, dict) and isinstance(pt.get("items"), dict):
                    prop["items"] = pt["items"]
                elif isinstance(pt, dict) and isinstance(pt.get("items"), str):
                    prop["items"] = {"type": pt["items"]}
                else:
                    prop["items"] = {"type": "string"}
                if description:
                    prop["description"] = description
                properties[pn] = prop
                if not is_optional:
                    required.append(pn)
                continue
            if base_type == "object":
                prop = {"type": "object"}
                if isinstance(pt, dict) and isinstance(pt.get("properties"), dict):
                    prop["properties"] = pt["properties"]
                if isinstance(pt, dict) and "additionalProperties" in pt:
                    prop["additionalProperties"] = pt["additionalProperties"]
                if description:
                    prop["description"] = description
                properties[pn] = prop
                if not is_optional:
                    required.append(pn)
                continue
            if base_type in ("integer", "int"):
                json_type = "integer"
            else:
                json_type = "number" if base_type == "number" else base_type
            prop = {"type": json_type}
            if description:
                prop["description"] = description
            if default_set:
                prop["default"] = default
            if min_length is not None:
                prop["minLength"] = min_length
            if isinstance(pt, dict):
                if isinstance(pt.get("minimum"), (int, float)):
                    prop["minimum"] = pt["minimum"]
                if isinstance(pt.get("maximum"), (int, float)):
                    prop["maximum"] = pt["maximum"]
            properties[pn] = prop
            if not is_optional:
                required.append(pn)
        parameters = {
            "type": "object",
            "properties": properties,
            "required": required,
        }
        if "additionalProperties" in schema:
            parameters["additionalProperties"] = schema["additionalProperties"]
        out.append({
            "type": "function",
            "function": {
                "name": display,
                "description": desc,
                "parameters": parameters,
            }
        })
    return out


def get_tool_feedback_template(
    tools_dict: ToolsDict,
    tool_name: str,
    reason: str,
) -> Optional[str]:
    tool_info = tools_dict.get(tool_name)
    if not tool_info or len(tool_info) < 5:
        return None
    feedback = tool_info[4]
    if not isinstance(feedback, dict):
        return None
    template = feedback.get(reason)
    return template if isinstance(template, str) else None


def render_feedback_template(
    template: str,
    display_map: Optional[Dict[str, str]],
    values: Optional[Dict[str, Any]] = None,
) -> str:
    rendered = render_tool_description(template, display_map)
    if values:
        for key, value in values.items():
            rendered = rendered.replace(f"{{{{{key}}}}}", str(value))
    return rendered


def build_feedback_text(
    tools_dict: ToolsDict,
    display_map: Optional[Dict[str, str]],
    resolved_name: str,
    reason: str,
    fallback: str,
    values: Optional[Dict[str, Any]] = None,
) -> str:
    template = get_tool_feedback_template(tools_dict, resolved_name, reason)
    if template:
        return render_feedback_template(
            template,
            display_map,
            values,
        )
    return fallback
