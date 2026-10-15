import sys


def main():
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_foreign("cairo")
    from .ui.app import DDMApp

    return DDMApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
