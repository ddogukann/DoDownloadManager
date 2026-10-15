import os
import sys


def main():
    from . import i18n

    i18n.setup()
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_foreign("cairo")
    from .ui.app import DDMApp

    app = DDMApp()
    code = app.run(sys.argv)
    if getattr(app, "restart_requested", False):
        argv = [sys.executable, "-m", "ddm"]
        os.execv(sys.executable, argv)
    return code


if __name__ == "__main__":
    sys.exit(main())
