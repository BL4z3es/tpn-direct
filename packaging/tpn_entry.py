"""PyInstaller entry point for the console executable and Windows service host."""

import sys


def main() -> None:
    args = sys.argv[1:]
    if args == ["service-host"]:
        from tpn.winservice import dispatch

        dispatch()
    elif args == ["service-install"]:
        from tpn.winservice import install

        install(sys.executable)
        print("Installed the tpn service (automatic delayed start).")
    elif args == ["service-uninstall"]:
        from tpn.winservice import uninstall

        uninstall()
        print("Removed the tpn service.")
    else:
        from tpn.cli import main as cli_main

        cli_main()


if __name__ == "__main__":
    main()
