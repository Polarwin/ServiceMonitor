"""Watchdog for our own systemd services.

Discovers enabled services whose WorkingDirectory or ExecStart points into
/home/justin (i.e. the projects we deploy ourselves) and, every
CHECK_INTERVAL_SECONDS, looks for the failure modes systemd itself cannot
see:

- the unit is enabled but no longer active (gave up after StartLimitBurst,
  or was never restarted),
- the main process is alive but degraded: file-descriptor usage near
  LimitNOFILE, or fatal resource errors ("Too many open files", "thread
  failed to start", ...) in its recent journal output.

On a problem the service is restarted via systemctl and a Telegram message
is sent. Restarts are rate-limited per service so a crash-looping service
alerts once instead of flapping forever.

State (journal watermarks, restart timestamps, alert cooldowns) lives in
monitor_state.json next to this script. Must run as root (needs journalctl,
systemctl restart, and /proc access); install_monitor.sh sets that up.
Run with --dry-run to report what it would do without restarting anything.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

# Telegram credentials: this project's own .env first, then the stockticker
# project's .env (current single source of truth for the bot token).
for _env in (
    Path(__file__).with_name(".env"),
    Path("/home/justin/Projects/stockticker/.env"),
):
    if _env.exists():
        load_dotenv(_env, override=False)


def send_telegram(message: str) -> None:
    """Send a message to the configured Telegram chat.

    Prints a warning and returns if Telegram credentials are missing.
    Prints/raises a clear error if the API request fails.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print(
            "Warning: TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID not set; "
            "skipping Telegram notification.",
            file=sys.stderr,
        )
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}

    try:
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"Error: failed to send Telegram message: {exc}", file=sys.stderr)
        raise SystemExit(1)


HOME_PREFIX = "/home/justin"
SELF_UNIT = "stockticker-monitor.service"
CHECK_INTERVAL_SECONDS = 60
DISCOVERY_TTL_SECONDS = 900
STATE_PATH = Path(__file__).with_name("monitor_state.json")

# Restart when the main process holds at least this fraction of LimitNOFILE.
FD_RESTART_FRACTION = 0.8
# Journal lines containing any of these mean the service is degraded even
# though the process is still running.
FATAL_PATTERNS = (
    "Too many open files",
    "thread failed to start",
    "Cannot allocate memory",
    "Out of memory",
)
# Per-service flap protection and alert throttling.
MAX_RESTARTS_PER_HOUR = 3
ALERT_COOLDOWN_SECONDS = 3600


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=30, check=False
    )


def discover_services() -> list[str]:
    """Enabled services that run something under HOME_PREFIX, minus ourselves."""
    result = run(
        "systemctl", "list-unit-files", "*.service",
        "--state=enabled", "--no-legend", "--no-pager",
    )
    units = [line.split()[0] for line in result.stdout.splitlines() if line.strip()]
    found = []
    for unit in units:
        if unit == SELF_UNIT:
            continue
        show = run("systemctl", "show", unit, "-p", "WorkingDirectory,ExecStart", "--value")
        if HOME_PREFIX in show.stdout:
            found.append(unit)
    return sorted(found)


def show_value(unit: str, prop: str) -> str:
    return run("systemctl", "show", unit, "-p", prop, "--value").stdout.strip()


def is_active(unit: str) -> bool:
    return run("systemctl", "is-active", "--quiet", unit).returncode == 0


def fd_usage(unit: str) -> tuple[int, int] | None:
    """(open FDs, limit) of the main process, or None when not running."""
    try:
        pid = int(show_value(unit, "MainPID"))
    except ValueError:
        return None
    if pid <= 0:
        return None
    try:
        open_fds = len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        return None
    try:
        limit = int(show_value(unit, "LimitNOFILE"))
    except ValueError:
        return None
    return open_fds, limit


def recent_fatal_line(unit: str, since: float) -> str | None:
    """First recent journal line matching FATAL_PATTERNS, or None."""
    result = run(
        "journalctl", "-u", unit, "--since", f"@{int(since)}",
        "-o", "cat", "--no-pager",
    )
    for line in result.stdout.splitlines():
        if any(pattern in line for pattern in FATAL_PATTERNS):
            return line.strip()[:200]
    return None


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=1))


def notify(message: str) -> None:
    print(message, flush=True)
    try:
        send_telegram(message)
    except BaseException as exc:
        # send_telegram raises SystemExit on API failure; never let that
        # kill the watchdog loop.
        print(f"Warning: Telegram send failed ({exc})", file=sys.stderr, flush=True)


def alert(state: dict, unit: str, key: str, message: str) -> None:
    """Send a Telegram alert, throttled per (unit, key)."""
    alerts = state.setdefault("alerts", {})
    now = time.time()
    if now - alerts.get(f"{unit}:{key}", 0) < ALERT_COOLDOWN_SECONDS:
        print(f"(alert throttled) {message}", flush=True)
        return
    alerts[f"{unit}:{key}"] = now
    notify(message)


def restart_service(state: dict, unit: str, reason: str, dry_run: bool) -> None:
    """Restart a unit with flap protection; always reports to Telegram."""
    now = time.time()
    restarts = [t for t in state.setdefault("restarts", {}).get(unit, [])
                if now - t < 3600]
    state["restarts"][unit] = restarts
    if len(restarts) >= MAX_RESTARTS_PER_HOUR:
        alert(
            state, unit, "giveup",
            f"🚨 {unit}: {reason} — but already restarted "
            f"{MAX_RESTARTS_PER_HOUR}x in the last hour; not restarting again. "
            "Manual check needed.",
        )
        return
    if dry_run:
        print(f"[dry-run] would restart {unit}: {reason}", flush=True)
        return
    result = run("systemctl", "restart", unit)
    restarts.append(now)
    if result.returncode == 0:
        notify(f"🔧 Restarted {unit}: {reason}")
    else:
        notify(f"🚨 Failed to restart {unit} ({reason}): {result.stderr.strip()}")


def check_service(state: dict, unit: str, dry_run: bool) -> None:
    units = state.setdefault("units", {})

    if not is_active(unit):
        restart_service(state, unit, "service is enabled but not active", dry_run)
        return

    usage = fd_usage(unit)
    if usage is not None:
        open_fds, limit = usage
        if limit > 0 and open_fds >= limit * FD_RESTART_FRACTION:
            restart_service(
                state, unit,
                f"file descriptors at {open_fds}/{limit} "
                f"({100 * open_fds // limit}%)",
                dry_run,
            )
            return

    watermark = units.get(unit)
    units[unit] = time.time()
    if watermark is None:
        # First sight of this unit: only set the watermark so historical
        # journal errors don't trigger a restart of a healthy service.
        return
    line = recent_fatal_line(unit, watermark)
    if line is not None:
        restart_service(state, unit, f"fatal error in journal: {line}", dry_run)


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    state = load_state()
    discovered_at = 0.0
    services: list[str] = []
    while True:
        now = time.time()
        if now - discovered_at > DISCOVERY_TTL_SECONDS:
            try:
                services = discover_services()
                discovered_at = now
                print(f"Monitoring: {', '.join(services) or '(none found)'}", flush=True)
            except Exception as exc:
                print(f"Warning: service discovery failed ({exc})", file=sys.stderr, flush=True)
        for unit in services:
            try:
                check_service(state, unit, dry_run)
            except Exception as exc:
                print(f"Warning: check of {unit} failed ({exc})", file=sys.stderr, flush=True)
        try:
            save_state(state)
        except OSError as exc:
            print(f"Warning: could not save state ({exc})", file=sys.stderr, flush=True)
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
