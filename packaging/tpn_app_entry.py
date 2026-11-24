"""PyInstaller entry point for the windowed tpn tray application."""


def main() -> None:
    from tpn.gui.app import main as app_main

    app_main()


if __name__ == "__main__":
    main()
