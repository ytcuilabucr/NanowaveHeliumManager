"""Show the Helium Recovery console in a desktop window (pywebview).

    uv run python main.py

This window only DISPLAYS the console. The monitoring itself runs in the
"HeliumRecovery" Windows service (installed with scripts/install_service.ps1),
which keeps running without anyone logged in and starts again by itself after
a Windows Update restart. Closing this window stops nothing.

If the service is not running, the window waits and opens the console as soon
as it answers. For a one-off run without the service (acquisition + window in
this process): ``uv run heliumrecovery desktop``.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))  # also runnable with a plain interpreter, without installing
os.chdir(ROOT)  # so the .env next to this script is read (HR_UI_PORT)

from heliumrecovery.__main__ import main as cli_main  # noqa: E402


def main() -> None:
    cli_main(["view", *sys.argv[1:]])


if __name__ == "__main__":
    main()
