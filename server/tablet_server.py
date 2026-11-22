#!/usr/bin/env python3
"""
Phone-as-Graphics-Tablet -- Windows server

Double-click / run with no arguments  -> small window (GUI)
    python tablet_server.py --cli      -> console only
    python tablet_server.py --dry-run  -> test pattern + printed input (any OS, no GUI)

Options: --port 5555  --monitor 1  --fps 30  --quality 70  --mouse
         --pin 123456 | --no-pin
"""
import argparse
import sys
import time

import common
from server_core import TabletServer, local_ips


def parse_args():
    p = argparse.ArgumentParser(description="Phone graphics tablet server")
    p.add_argument("--cli", action="store_true", help="no window, console only")
    p.add_argument("--dry-run", action="store_true", help="test pattern, print input (implies --cli)")
    p.add_argument("--port", type=int)
    p.add_argument("--monitor", type=int, help="monitor to stream first (1 = primary)")
    p.add_argument("--fps", type=int)
    p.add_argument("--quality", type=int, help="JPEG quality 1-95")
    p.add_argument("--mouse", action="store_true", help="force mouse mode (no pen)")
    p.add_argument("--pin", help="6-digit PIN the phone must enter")
    p.add_argument("--no-pin", action="store_true", help="do not ask for a PIN")
    return p.parse_args()


def settings_from(args, cfg):
    """CLI flags override the saved config; returns TabletServer keyword arguments."""
    if args.no_pin:
        pin = ""
    elif args.pin:
        pin = "".join(ch for ch in args.pin if ch.isdigit())[:6]
    else:
        pin = cfg.get("pin", "") if "pin" in cfg else common.new_pin()
    return dict(
        port=args.port or cfg.get("port", common.DEFAULT_PORT),
        pin=pin,
        monitor=args.monitor or cfg.get("monitor", 1),
        fps=args.fps or cfg.get("fps", 30),
        quality=args.quality or cfg.get("quality", 70),
        mouse=args.mouse or cfg.get("mouse", False),
        dry_run=args.dry_run,
    )


def run_cli(args, cfg):
    kw = settings_from(args, cfg)
    if not args.dry_run and not args.pin and not args.no_pin:
        cfg["pin"] = kw["pin"]
        common.save_config(cfg)
    server = TabletServer(**kw)
    try:
        server.start()
    except OSError as exc:
        sys.exit(f"Cannot listen on port {kw['port']}: {exc}")
    print()
    for ip in local_ips():
        print(f"  Phone address: {ip}   (the phone can also find this PC automatically)")
    print(f"  PIN: {kw['pin'] or 'none'}")
    print(f"  USB: run  adb reverse tcp:{server.port} tcp:{server.port}  then use 127.0.0.1")
    print("Press Ctrl+C to stop.\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.stop()


def main():
    args = parse_args()
    cfg = common.load_config()
    if args.cli or args.dry_run:
        return run_cli(args, cfg)
    try:
        import gui
    except ImportError:
        print("tkinter is not available; running in console mode.")
        return run_cli(args, cfg)
    gui.run(args, cfg, settings_from(args, cfg))


if __name__ == "__main__":
    main()
