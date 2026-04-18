"""CR-200B monitor — serial bridge + (later) web dashboard.

Step 1 scope: open the serial port, do an M115 handshake, log every raw
byte that crosses the wire to debug.log and stdout. No parsing, no HTTP,
no WebSocket — that lands in Step 2.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import serial
from serial.tools import list_ports


SCRIPT_DIR = Path(__file__).resolve().parent
DEBUG_LOG_PATH = SCRIPT_DIR / "debug.log"

DEFAULT_BAUD = 115200
FALLBACK_BAUD = 250000
BOOT_WAIT_SEC = 2.0
HANDSHAKE_TIMEOUT_SEC = 5.0


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class RawIOLogger:
    """Logs every byte sent and received to debug.log and stdout.

    Lines are written as: `<ISO-timestamp> <<|>> <data>`.
    Thread-safe — the reader thread and the main thread both call into it.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._fh = path.open("a", buffering=1, encoding="utf-8", errors="replace")
        self._stdout_log = logging.getLogger("cr200b.io")

    def log(self, direction: str, data: str) -> None:
        # Strip a trailing newline so the log line itself stays one line, but
        # keep any internal whitespace intact for debugging.
        clean = data.rstrip("\r\n")
        if not clean:
            return
        line = f"{_iso_now()} {direction} {clean}"
        with self._lock:
            self._fh.write(line + "\n")
        self._stdout_log.info("%s %s", direction, clean)

    def close(self) -> None:
        with self._lock:
            try:
                self._fh.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Serial helpers
# ---------------------------------------------------------------------------

def list_serial_ports() -> int:
    ports = sorted(list_ports.comports(), key=lambda p: p.device)
    if not ports:
        print("No serial ports found.", file=sys.stderr)
        return 1
    print(f"{'DEVICE':<20} {'VID:PID':<12} DESCRIPTION")
    for p in ports:
        vid_pid = ""
        if p.vid is not None and p.pid is not None:
            vid_pid = f"{p.vid:04x}:{p.pid:04x}"
        desc = p.description or ""
        if p.manufacturer and p.manufacturer not in desc:
            desc = f"{desc} ({p.manufacturer})".strip()
        print(f"{p.device:<20} {vid_pid:<12} {desc}")
    return 0


def open_serial(port: str, baud: int) -> serial.Serial:
    """Open the port or raise with a human-readable hint."""
    try:
        # Marlin resets on DTR toggle; we accept that — it's the only reliable
        # way to catch the boot banner. Caller waits BOOT_WAIT_SEC after.
        ser = serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.2,
            write_timeout=2.0,
        )
        return ser
    except serial.SerialException as e:
        msg = str(e).lower()
        if "permission denied" in msg and sys.platform.startswith("linux"):
            sys.exit(
                f"Permission denied opening {port}.\n"
                f"  On Linux, add yourself to the dialout group:\n"
                f"      sudo usermod -a -G dialout $USER\n"
                f"  Then log out and back in. Original error: {e}"
            )
        if "could not open port" in msg or "no such file" in msg:
            sys.exit(
                f"Cannot open {port}: {e}\n"
                f"  Run `python server.py --list-ports` to see available ports.\n"
                f"  Make sure no other program (slicer, Pronterface, OctoPrint, "
                f"another serial monitor) has the port open."
            )
        if "busy" in msg or "access is denied" in msg:
            sys.exit(
                f"{port} is busy: {e}\n"
                f"  Close any other program that has the port open."
            )
        sys.exit(f"Failed to open serial port {port}: {e}")


# ---------------------------------------------------------------------------
# Reader thread
# ---------------------------------------------------------------------------

class SerialReader(threading.Thread):
    """Pumps bytes from the serial port into the raw logger, line-by-line."""

    def __init__(self, ser: serial.Serial, io_log: RawIOLogger) -> None:
        super().__init__(name="serial-reader", daemon=True)
        self.ser = ser
        self.io_log = io_log
        self._stop = threading.Event()
        self._buf = bytearray()
        # Lines seen since the last get_recent() call — Step 2 hooks in here.
        self.recent: list[str] = []
        self._recent_lock = threading.Lock()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self.ser.read(256)
            except serial.SerialException as e:
                self.io_log.log("!!", f"serial read error: {e}")
                break
            if not chunk:
                continue
            self._buf.extend(chunk)
            while b"\n" in self._buf:
                raw_line, _, rest = self._buf.partition(b"\n")
                self._buf = bytearray(rest)
                try:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r")
                except Exception:
                    line = repr(bytes(raw_line))
                if not line:
                    continue
                self.io_log.log("<<", line)
                with self._recent_lock:
                    self.recent.append(line)
                    if len(self.recent) > 200:
                        del self.recent[: len(self.recent) - 200]

    def drain_recent(self) -> list[str]:
        with self._recent_lock:
            out = list(self.recent)
            self.recent.clear()
        return out


def send_line(ser: serial.Serial, io_log: RawIOLogger, line: str) -> None:
    """Send a single G-code line (newline appended) and log it."""
    if not line.endswith("\n"):
        line = line + "\n"
    payload = line.encode("ascii", errors="replace")
    try:
        ser.write(payload)
        ser.flush()
    except serial.SerialException as e:
        io_log.log("!!", f"serial write error: {e}")
        raise
    io_log.log(">>", line.rstrip("\r\n"))


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------

def wait_for_boot(reader: SerialReader, io_log: RawIOLogger) -> None:
    """Block BOOT_WAIT_SEC so Marlin's boot banner can land in the log."""
    io_log.log("##", f"waiting {BOOT_WAIT_SEC:.1f}s for boot banner")
    time.sleep(BOOT_WAIT_SEC)
    reader.drain_recent()  # boot lines are already in the log; clear the buffer


def handshake(ser: serial.Serial, reader: SerialReader,
              io_log: RawIOLogger, baud: int) -> Optional[str]:
    """Send M115, wait up to HANDSHAKE_TIMEOUT_SEC for any response.

    Returns the firmware string if we saw something that looks like one,
    else None. Step 2 will properly parse this; here we just want to know
    the printer is talking back.
    """
    send_line(ser, io_log, "M115")
    deadline = time.monotonic() + HANDSHAKE_TIMEOUT_SEC
    seen: list[str] = []
    fw_line: Optional[str] = None
    while time.monotonic() < deadline:
        time.sleep(0.1)
        new = reader.drain_recent()
        if new:
            seen.extend(new)
            for line in new:
                if "FIRMWARE_NAME" in line or "Marlin" in line:
                    fw_line = line
            # Stop early once we've seen an "ok" after some firmware data.
            if fw_line and any(line.strip().lower().startswith("ok") for line in seen):
                break
    if not seen:
        print(
            f"\nNo response from printer at {baud} baud within "
            f"{HANDSHAKE_TIMEOUT_SEC:.0f}s.",
            file=sys.stderr,
        )
        if baud != FALLBACK_BAUD:
            print(
                f"  Try: --baud {FALLBACK_BAUD}\n"
                f"  Some CR-200B firmware builds use 250000 instead of 115200.",
                file=sys.stderr,
            )
        else:
            print(
                "  Both common baud rates have failed. Check the USB cable, "
                "driver (CH340/CH341 on Windows), and that the printer is on.",
                file=sys.stderr,
            )
        return None
    return fw_line


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="cr200b-monitor",
        description="Live monitor for a Creality CR-200B over USB serial.",
    )
    p.add_argument("--serial-port", help="e.g. /dev/ttyUSB0 or COM3")
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD,
                   help=f"baud rate (default {DEFAULT_BAUD}; try {FALLBACK_BAUD} if no response)")
    p.add_argument("--http-port", type=int, default=8080,
                   help="HTTP/WebSocket port (default 8080) — used in Step 2+")
    p.add_argument("--poll-interval", type=float, default=2.0,
                   help="seconds between status polls (default 2.0) — used in Step 2+")
    p.add_argument("--list-ports", action="store_true",
                   help="print available serial ports with VID/PID and exit")
    p.add_argument("--stream-file", help="(Step 6) .gcode file to host-stream")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_level)
    log = logging.getLogger("cr200b")

    if args.list_ports:
        return list_serial_ports()

    if not args.serial_port:
        print("Error: --serial-port is required (or use --list-ports).",
              file=sys.stderr)
        return 2

    log.info("debug.log → %s", DEBUG_LOG_PATH)
    io_log = RawIOLogger(DEBUG_LOG_PATH)
    io_log.log("##", f"=== session start, port={args.serial_port} baud={args.baud} ===")

    ser = open_serial(args.serial_port, args.baud)
    log.info("opened %s @ %d baud", args.serial_port, args.baud)

    reader = SerialReader(ser, io_log)
    reader.start()

    stop_event = threading.Event()

    def _on_signal(signum, _frame):
        log.info("signal %s received, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _on_signal)

    try:
        wait_for_boot(reader, io_log)
        fw = handshake(ser, reader, io_log, args.baud)
        if fw:
            log.info("firmware response: %s", fw)
        else:
            log.warning("no firmware response — see hint above and debug.log")

        # Step 1 main loop: just keep the reader pumping and let raw I/O flow
        # to the log. Ctrl-C to exit. Step 2 will replace this with the real
        # state machine + WebSocket push.
        log.info("idle — passively logging raw serial I/O. Ctrl-C to quit.")
        while not stop_event.is_set():
            time.sleep(0.5)

    finally:
        log.info("stopping reader and closing port")
        reader.stop()
        reader.join(timeout=2.0)
        try:
            ser.close()
        except Exception:
            pass
        io_log.log("##", "=== session end ===")
        io_log.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
