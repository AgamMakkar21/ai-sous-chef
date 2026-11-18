import base64
import hashlib
import re
from functools import cache
from html import escape
from importlib.resources import files


@cache
def render_login_page() -> tuple[str, str]:
    templates = files("sous_chef").joinpath("templates")
    assets = templates.joinpath("assets")
    document = (
        templates.joinpath("login.html")
        .read_text(encoding="utf-8")
        .replace(
            "__JERSEY_FONT__",
            base64.b64encode(assets.joinpath("jersey-10-kitchen.woff2").read_bytes()).decode(
                "ascii"
            ),
        )
        .replace(
            "__FONT_LICENSE__", escape(assets.joinpath("Jersey-10-OFL.txt").read_text("utf-8"))
        )
    )

    def hashes(tag: str) -> str:
        # Only the trusted template's exact inline blocks may execute or style the page.
        return " ".join(
            f"'sha256-{base64.b64encode(hashlib.sha256(block.encode()).digest()).decode()}'"
            for block in re.findall(rf"<{tag}>(.*?)</{tag}>", document, flags=re.DOTALL)
        )

    policy = (
        "default-src 'none'; "
        f"style-src {hashes('style')}; script-src {hashes('script')}; "
        "font-src data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )
    return document, policy
