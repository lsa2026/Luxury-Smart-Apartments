"""Read-only rendering reference for authenticated DB context, never API caller context.

No network, source authority, admission, persistence or provider operation here.
The exact original B02 output hash is required before returning a wire message.
"""

import hashlib
import json
import re


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_trusted_website_source(context, expected_renderer_sha):
    def hold(reason):
        return {"ok": False, "reason": reason, "dispatch_allowed": False}

    c = context
    if (
        not isinstance(c, dict)
        or c.get("contract_version") != "yc20-website-render-source-1"
        or c.get("scenario") not in ("07", "08")
        or c.get("dispatch_allowed") is not False
        or c.get("language") not in ("ar", "en", "fr")
        or c.get("renderer_sha256") != expected_renderer_sha
    ):
        return hold("render_context_not_verified")
    text = c.get("source_template_text")
    variables = c.get("variables")
    if (
        not isinstance(text, str)
        or _sha(text) != c.get("source_template_sha256")
        or not isinstance(variables, dict)
    ):
        return hold("source_template_or_variables_invalid")

    def replace(match):
        key = match.group(1)
        if key not in variables or not (variables[key] is None or isinstance(variables[key], str)):
            raise ValueError("invalid source variable")
        return variables[key] if variables[key] is not None else ""

    try:
        text = re.sub(r"\{\{\s*(\w+)\s*\}\}", replace, text, flags=re.ASCII)
    except ValueError:
        return hold("source_variable_missing")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</?(?:b|strong)>", "*", text, flags=re.I)
    text = re.sub(r"</?(?:i|em)>", "_", text, flags=re.I)
    text = re.sub(r"<[^>]*>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    if c["language"] == "ar":
        text = "\u200f" + text.replace("\n", "\n\u200f")
    if (
        not text
        or len(text.encode("utf-16-le")) // 2 > 4096
        or _sha(text) != c.get("stored_body_sha256")
    ):
        return hold("persisted_source_render_mismatch")
    if c.get("media_required") is not False:
        return hold("media_contract_required")
    message = {"mode": "text", "text": text}
    return {
        "ok": True,
        "message": message,
        "content_digest": _sha(
            json.dumps([c["language"], message], ensure_ascii=False, separators=(",", ":"))
        ),
        "render_policy_ref": "yc20-web-render-v1:"
        + _sha(c["renderer_sha256"] + ":" + c["source_template_sha256"]),
        "source_template_sha256": c["source_template_sha256"],
        "renderer_sha256": c["renderer_sha256"],
        "dispatch_allowed": False,
    }
