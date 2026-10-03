"""Launch an isolated PostgreSQL, run the real migration/race tests, and clean up."""

import os
import shutil
import socket
import subprocess  # noqa: S404
import sys
import tempfile
from pathlib import Path


def main() -> None:
    binary = os.environ.get("DAILY_TEST_POSTGRES_BIN")
    if binary is None:
        found = shutil.which("initdb")
        if found is None:
            raise SystemExit("Set DAILY_TEST_POSTGRES_BIN to the PostgreSQL bin directory")
        binary = str(Path(found).parent)
    pg = Path(binary)
    with tempfile.TemporaryDirectory(prefix="academy-daily-pg-") as temp:
        root = Path(temp)
        data = root / "data"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        subprocess.run(  # noqa: S603
            [str(pg / "initdb"), "-D", str(data), "-U", "academy_daily_test", "--auth=trust", "--no-locale"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        try:
            subprocess.run(  # noqa: S603
                [
                    str(pg / "pg_ctl"),
                    "-D",
                    str(data),
                    "-l",
                    str(root / "server.log"),
                    "-o",
                    f"-h 127.0.0.1 -k {temp} -p {port}",
                    "-w",
                    "start",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            env = dict(os.environ, DATABASE_URL=f"postgresql+asyncpg://academy_daily_test@127.0.0.1:{port}/postgres")
            subprocess.run([sys.executable, "-m", "tests.daily_limit_postgres"], env=env, check=True)  # noqa: S603
        finally:
            if (data / "postmaster.pid").exists():
                subprocess.run(  # noqa: S603
                    [str(pg / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"],
                    check=True,
                    stdout=subprocess.DEVNULL,
                )


if __name__ == "__main__":
    main()
