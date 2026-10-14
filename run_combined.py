"""Run VK/Gmail and MAX->Telegram in one Bothost container.

MAX adapter is pinned to an upstream Git commit by the Docker build.
Only the VK/Gmail process consumes Telegram getUpdates; MAX is send-only.
"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

LOG = logging.getLogger("vk-max-supervisor")
ROOT = Path(__file__).resolve().parent
MAX_ROOT = Path("/opt/max2tg")
STOP = False


def _stop(_signum, _frame):
    global STOP
    STOP = True


def prepare_environment() -> dict[str, str]:
    env = dict(os.environ)
    # The upstream MAX tool should NEVER start a second Telegram getUpdates
    # consumer: gmail_bot.py is the sole command receiver.
    env["REPLY_ENABLED"] = "false"
    env["DEBUG"] = "false"
    env["TG_BOT_TOKEN"] = env.get("TG_BOT_TOKEN") or env.get("BOT_TOKEN", "")
    env.setdefault("LOG_DIR", "/tmp/max2tg-logs")

    required = ("TG_BOT_TOKEN", "TG_CHAT_ID", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")
    missing = [key for key in required if not env.get(key)]
    if missing:
        raise RuntimeError("Missing Bothost environment variables: " + ", ".join(missing))

    # Bothost's /app/data persists across redeploys. Preserve older state
    # when the previous Gmail bot wrote its DB into the project root.
    if not env.get("DB_FILE"):
        db = ROOT / "data" / "vk_mail_state.sqlite3"
        db.parent.mkdir(parents=True, exist_ok=True)
        legacy = ROOT / "vk_mail_state.sqlite3"
        if not db.exists() and legacy.is_file():
            shutil.copy2(legacy, db)
            LOG.info("Copied existing Gmail state into persistent data folder")
        env["DB_FILE"] = str(db)
    return env


def service_specs(env: dict[str, str]) -> dict[str, tuple[list[str], Path]]:
    result = {"gmail": ([sys.executable, "-u", str(ROOT / "gmail_bot.py")], ROOT)}
    max_token, device = env.get("MAX_TOKEN", "").strip(), env.get("MAX_DEVICE_ID", "").strip()
    if bool(max_token) != bool(device):
        LOG.warning("MAX disabled: both MAX_TOKEN and MAX_DEVICE_ID are required")
    elif max_token and device:
        if not (MAX_ROOT / "app" / "main.py").is_file():
            raise RuntimeError("MAX client not installed in Docker image")
        result["max"] = ([sys.executable, "-u", "-m", "app.main"], MAX_ROOT)
    else:
        LOG.info("MAX disabled until both credentials are configured")
    return result


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [supervisor] %(levelname)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    try:
        env = prepare_environment()
        specs = service_specs(env)
    except Exception as exc:
        LOG.error("Cannot initialize services: %s", exc)
        return 2

    jobs = {name: {"cmd": cmd, "cwd": cwd, "proc": None,
                   "delay": 3, "next": 0.0} for name, (cmd, cwd) in specs.items()}

    try:
        while not STOP:
            now = time.monotonic()
            for name, job in jobs.items():
                proc = job["proc"]
                if proc is not None and proc.poll() is not None:
                    LOG.warning("%s exited (%s), restarting with delay",
                                name, proc.returncode)
                    job["proc"] = None
                    job["next"] = now + job["delay"]
                    job["delay"] = min(job["delay"] * 2, 120)
                if job["proc"] is None and now >= job["next"]:
                    # MAX upstream can log private message previews at INFO.
                    # Suppress MAX stdout/stderr; its own rotating debug logs
                    # are kept ONLY in container /tmp, never in Git or Bothost logs.
                    output = subprocess.DEVNULL if name == "max" else None
                    job["proc"] = subprocess.Popen(
                        job["cmd"], cwd=str(job["cwd"]), env=env,
                        stdout=output, stderr=subprocess.STDOUT if output is not None else None,
                    )
                    LOG.info("%s process started (pid=%s)", name, job["proc"].pid)
                    # A process that remains up for a minute gets fresh retry budget.
                    job["started"] = now
                if job["proc"] is not None and proc is not None and now - job.get("started", now) > 60:
                    job["delay"] = 3
            time.sleep(2)
    finally:
        for job in jobs.values():
            proc = job["proc"]
            if proc is not None and proc.poll() is None:
                proc.terminate()
        for job in jobs.values():
            proc = job["proc"]
            if proc is not None:
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
