"""`tbctl` CLI: validate / codegen (compile + simulate arrive with the sequencer)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def cmd_validate(args: argparse.Namespace) -> int:
    from tbctl.config.loader import ConfigError, load_config_dir
    from tbctl.config.models import dio_number

    config_dir = Path(args.config_dir)
    try:
        bundle = load_config_dir(config_dir)
    except ConfigError as e:
        print(f"INVALID\n{e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"INVALID\n{e}", file=sys.stderr)
        return 1

    ch = bundle.channels
    chmap = ch.model
    print(f"OK {ch.path}  sha256={ch.sha256[:16]}…")
    print(
        f"   device={chmap.device.ip}  channels={len(chmap.channels)} "
        f"({len(chmap.inputs())} in / {len(chmap.outputs())} out)"
    )
    print(f"   safe_word=0x{chmap.safe_word():04X}  dio_inhibit=0x{chmap.dio_inhibit():04X}")
    for c in chmap.outputs():
        print(
            f"     bit {dio_number(c.hw):2d}  {c.hw:5s}  {c.tag}  ({c.kind}, fail_safe={c.fail_safe})"
        )
    for name, s in bundle.sequences.items():
        print(
            f"OK {s.path}  sha256={s.sha256[:16]}…  "
            f"({len(s.model.steps)} steps, {len(s.model.abort)} abort)"
        )
        if s.model.allow_unsafe_end:
            print(f"   WARNING: {name} sets allow_unsafe_end=true")
    for _name, d in bundle.dashboards.items():
        print(f"OK {d.path}  sha256={d.sha256[:16]}…  ({len(d.model.panels)} panels)")
    print("all configs valid")
    return 0


def cmd_codegen(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo_root / "shared"))
    import codegen  # type: ignore[import-not-found]

    return codegen.generate(check=args.check)


def cmd_probe(args: argparse.Namespace) -> int:
    """Minimal connectivity check: find the T7, print identity, check the script."""
    from tbctl.config.loader import load_channels
    from tbctl.hw.ljm_iface import LjmDevice
    from tbctl.hw.registers_gen import INTERP_VERSION, REG_NAME

    chmap = load_channels(Path(args.config_dir) / "channels.yaml").model
    conn, ident = ("USB", "ANY") if args.usb else chmap.device.ljm_open_args()
    if args.ip:
        conn, ident = "ETHERNET", args.ip
    print(f"opening T7 via {conn} ({ident})…")
    dev = LjmDevice(ident, conn)
    try:
        dev.open()
    except Exception as e:
        print(f"FAILED to open device: {e}", file=sys.stderr)
        print(
            "hints: is Kipling (or another process) holding the device? "
            "is the LJM library installed? try: python -c "
            "\"from labjack import ljm; print(ljm.listAllS('T7','ANY'))\"",
            file=sys.stderr,
        )
        return 1
    try:
        serial = int(dev.read_name("SERIAL_NUMBER"))
        fw = dev.read_name("FIRMWARE_VERSION")
        temp_k = dev.read_name("TEMPERATURE_DEVICE_K")
        print(
            f"connected: T7 serial {serial}, firmware {fw:.4f}, device temp {temp_k - 273.15:.1f} degC"
        )
        ain0 = dev.read_name("AIN0")
        print(f"AIN0 reads {ain0:.4f} V")
        v = int(dev.read_name(REG_NAME["INTERP_VERSION"]))
        if v == INTERP_VERSION:
            print(f"sequencer.lua is running (interp_version {v}) — ready")
        elif v == 0:
            print("sequencer.lua NOT running — run `tbctl deploy-lua` before sequencing")
        else:
            print(
                f"WARNING: script reports interp_version {v}, host expects {INTERP_VERSION} — redeploy"
            )
        return 0
    finally:
        dev.close()


def cmd_soak(args: argparse.Namespace) -> int:
    """§10.4 soak: record against a running node for N hours, then verify
    sample-count accounting (expected = rate x duration per group, within
    tolerance) and report node memory growth if it is local."""
    import json
    import time
    import urllib.request

    def api(path: str, body: dict | None = None, method: str | None = None) -> dict:
        req = urllib.request.Request(
            args.url + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method=method or ("GET" if body is None else "POST"),
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    status = api("/api/status")
    rate = status["acquisition"]["scan_rate_hz"]
    print(
        f"node: sim={status['device']['sim']} scan_rate={rate:.0f} Hz, "
        f"soaking for {args.hours:.2f} h"
    )
    run = api(
        "/api/runs/start",
        {"name": "soak", "operator": args.operator, "notes": f"{args.hours} h soak (§10.4)"},
    )
    print(f"recording run {run['run_id']}")
    t0 = time.time()
    end = t0 + args.hours * 3600.0
    try:
        while time.time() < end:
            time.sleep(min(60.0, end - time.time()))
            st = api("/api/status")
            if not st["recording"]["active"]:
                print("FAIL: recording stopped unexpectedly", file=sys.stderr)
                return 1
            elapsed = time.time() - t0
            print(
                f"  {elapsed / 3600.0:5.2f} h  samples={st['acquisition']['sample_count']}",
                flush=True,
            )
    except KeyboardInterrupt:
        print("interrupted — stopping run and checking what we have")
    result = api("/api/runs/stop", method="POST")
    duration = time.time() - t0
    ok = True
    meta = api("/api/channels")
    for group, count in result["sample_counts"].items():
        group_rate = meta["rate_groups"][group]
        expected = duration * group_rate
        loss = 1.0 - count / expected if expected else 0.0
        verdict = "OK" if abs(loss) < 0.01 else "FAIL"
        ok &= verdict == "OK"
        print(
            f"group {group}: {count} samples, expected ~{expected:.0f} "
            f"({loss * 100:+.2f}% vs nominal) {verdict}"
        )
    print("soak PASS" if ok else "soak FAIL")
    return 0 if ok else 1


def cmd_deploy_lua(args: argparse.Namespace) -> int:
    from tbctl.config.loader import load_channels
    from tbctl.hw.ljm_iface import LjmDevice
    from tbctl.hw.script_loader import ScriptLoadError, deploy_script, read_debug
    from tbctl.hw.watchdog import configure_watchdog

    chmap = load_channels(Path(args.config_dir) / "channels.yaml").model
    if args.usb:
        conn, ident = "USB", "ANY"
    else:
        conn, ident = chmap.device.ljm_open_args()
    if args.ip:
        conn, ident = "ETHERNET", args.ip
    dev = LjmDevice(ident, conn)
    print(f"connecting to T7 via {conn} ({ident})…")
    dev.open()
    try:
        version = deploy_script(dev, save_to_flash=not args.no_flash)
        print(f"sequencer.lua deployed and running (interp_version {version})")
        applied = configure_watchdog(dev, chmap)
        print(f"hardware watchdog armed: timeout {applied['WATCHDOG_TIMEOUT_S_DEFAULT']:.0f} s")
        return 0
    except ScriptLoadError as e:
        print(f"deploy failed: {e}", file=sys.stderr)
        debug = read_debug(dev)
        if debug:
            print(f"LUA_DEBUG:\n{debug}", file=sys.stderr)
        return 1
    finally:
        dev.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tbctl", description="testbench control CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_val = sub.add_parser("validate", help="validate a config directory")
    p_val.add_argument("--config-dir", default="configs")
    p_val.set_defaults(func=cmd_validate)

    p_gen = sub.add_parser("codegen", help="regenerate registers_gen.{py,lua} from registers.yaml")
    p_gen.add_argument(
        "--check", action="store_true", help="verify outputs are in sync, don't write"
    )
    p_gen.set_defaults(func=cmd_codegen)

    p_soak = sub.add_parser(
        "soak", help="§10.4 soak: record via a running node, verify sample accounting"
    )
    p_soak.add_argument("--url", default="http://localhost:8000")
    p_soak.add_argument("--hours", type=float, default=8.0)
    p_soak.add_argument("--operator", default="soak")
    p_soak.set_defaults(func=cmd_soak)

    p_probe = sub.add_parser("probe", help="check T7 connectivity and script status")
    p_probe.add_argument("--config-dir", default="configs")
    p_probe.add_argument("--ip", default=None, help="override: connect via Ethernet to this IP")
    p_probe.add_argument("--usb", action="store_true", help="override: connect via USB")
    p_probe.set_defaults(func=cmd_probe)

    p_dep = sub.add_parser(
        "deploy-lua", help="upload lua/sequencer.lua to the T7 and arm the hardware watchdog"
    )
    p_dep.add_argument("--config-dir", default="configs")
    p_dep.add_argument("--ip", default=None, help="override device IP from channels.yaml")
    p_dep.add_argument("--usb", action="store_true", help="connect over USB instead of Ethernet")
    p_dep.add_argument(
        "--no-flash", action="store_true", help="don't persist the script across power cycles"
    )
    p_dep.set_defaults(func=cmd_deploy_lua)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
