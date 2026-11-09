import os
import secrets
from pathlib import Path

target = Path(".env")
if target.exists():
    raise SystemExit(".env already exists; preserve it and set missing local-only values manually.")
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    f.write("# Generated local synthetic-demo credentials. Never commit.\n")
    for key in ("POSTGRES_PASSWORD", "APP_DB_PASSWORD", "SOURCE_DB_PASSWORD"):
        f.write(key + "=" + secrets.token_hex(24) + "\n")
print("Generated private local .env. Values are not printed.")
