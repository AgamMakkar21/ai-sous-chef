"""Install the exact supplied USDA source at setup/build time, never at app startup."""

import hashlib
import os
import tempfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

REVISION = "2a313bc599a477bc2b2df6dcc43857e4002ab22f"
SOURCE_SHA256 = "1edc1d9d3fbc5458bb45020ed985c8cd14c3051633165ecaf8d382b393eda379"
SOURCE_URL = f"https://raw.githubusercontent.com/HemantaPatil/mcp_postgres/{REVISION}/main.py"
SOURCE_PATH = Path(__file__).parent / "_usda" / "main.py"
MAX_SOURCE_BYTES = 131_072


class UsdaSetupError(RuntimeError):
    """Source installation or verification failed; no unverified code may execute."""


def verify_source(data: bytes) -> bytes:
    if hashlib.sha256(data).hexdigest() != SOURCE_SHA256:
        raise UsdaSetupError("The USDA dependency checksum does not match the reviewed revision.")
    return data


def installed_source() -> Path:
    try:
        verify_source(SOURCE_PATH.read_bytes())
    except OSError:
        raise UsdaSetupError(
            "The pinned USDA dependency is missing. Run python -m sous_chef.usda_dependency "
            "during setup/build, then restart the app."
        ) from None
    return SOURCE_PATH.resolve()


def install() -> None:
    if SOURCE_PATH.exists():
        installed_source()
        return
    try:
        with urlopen(SOURCE_URL, timeout=30) as response:
            data = verify_source(response.read(MAX_SOURCE_BYTES + 1))
        SOURCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=SOURCE_PATH.parent, delete=False) as temporary:
            staging = Path(temporary.name)
            temporary.write(data)
        try:
            os.replace(staging, SOURCE_PATH)
        finally:
            staging.unlink(missing_ok=True)
    except (OSError, URLError):
        raise UsdaSetupError(
            "Could not install the pinned USDA dependency; check HTTPS access to "
            "raw.githubusercontent.com. No application or database credentials are required."
        ) from None


def main() -> None:
    try:
        install()
    except UsdaSetupError as error:
        raise SystemExit(str(error)) from None
    print(f"USDA dependency ready at reviewed revision {REVISION}.")


if __name__ == "__main__":
    main()
