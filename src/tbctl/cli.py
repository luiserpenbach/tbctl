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


def cmd_deploy_lua(args: argparse.Namespace) -> int:
    from tbctl.config.loader import load_channels
    from tbctl.hw.ljm_iface import LjmDevice
    from tbctl.hw.script_loader import ScriptLoadError, deploy_script, read_debug
    from tbctl.hw.watchdog import configure_watchdog

    chmap = load_channels(Path(args.config_dir) / "channels.yaml").model
    ip = args.ip or chmap.device.ip
    dev = LjmDevice(ip)
    print(f"connecting to T7 at {ip}…")
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

    p_dep = sub.add_parser(
        "deploy-lua", help="upload lua/sequencer.lua to the T7 and arm the hardware watchdog"
    )
    p_dep.add_argument("--config-dir", default="configs")
    p_dep.add_argument("--ip", default=None, help="override device IP from channels.yaml")
    p_dep.add_argument(
        "--no-flash", action="store_true", help="don't persist the script across power cycles"
    )
    p_dep.set_defaults(func=cmd_deploy_lua)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
