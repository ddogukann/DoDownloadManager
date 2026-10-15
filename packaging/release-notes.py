import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
version = sys.argv[1].lstrip("v")
text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
match = re.search(rf"^## {re.escape(version)}\b.*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
notes = match.group(1).strip() if match else ""
print(notes or f"DoDownloadManager {version}")
print()
print("### Install")
print()
print(f"Download `dodownloadmanager_{version}_all.deb` below and open it, or run:")
print()
print("```bash")
print(f"sudo apt install ./dodownloadmanager_{version}_all.deb")
print("```")
print()
print("Ubuntu 24.04 or newer. Settings and download history are kept when updating.")
