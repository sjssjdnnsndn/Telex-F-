"""Production entry point for the uploaded Telegram bot.

This file is a runtime copy of the uploaded source. It is generated during
setup from the original attachment and then receives only deployment-safe
changes (configuration validation and health server integration).
"""

from pathlib import Path
import runpy


SOURCE = Path(__file__).resolve().parent / "source_bot.py"

if __name__ == "__main__":
    runpy.run_path(str(SOURCE), run_name="__main__")
