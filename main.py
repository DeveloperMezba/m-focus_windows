"""M Focus for Windows - entry point.

  MFocus.exe                     open the app
  MFocus.exe --ctl start <b64>   (internal) elevated: start a locked session
  MFocus.exe --ctl stop          (internal) elevated: end the session now
  MFocus.exe --enforcer          (internal) SYSTEM task: keep the block applied
  MFocus.exe --selftest <file>   (internal) used by the GitHub build to smoke-test
"""
import sys


def main():
    args = sys.argv[1:]
    # Qt is imported lazily so the background modes start fast and need no display.
    if args and args[0] == "--enforcer":
        import mf_core
        return mf_core.enforcer_main()
    if args and args[0] == "--ctl":
        import mf_core
        return mf_core.ctl_main(args[1:])
    if args and args[0] == "--selftest":
        import mf_core
        import mfocus_ui
        out = args[1] if len(args) > 1 else "selftest.txt"
        assert "0.0.0.0 www.example.com" in mf_core.build_block(["example.com"])
        sys.argv = sys.argv[:1]
        return mfocus_ui.main(selftest_out=out)
    import mfocus_ui
    return mfocus_ui.main()


if __name__ == "__main__":
    sys.exit(main())
