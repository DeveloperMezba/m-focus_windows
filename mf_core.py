"""M Focus (Windows) - Qt-free core. Replaces the Linux root service + pkexec scripts.

One exe, three roles:
  MFocus.exe                    the app window (normal user)
  MFocus.exe --ctl start <b64>  run elevated (UAC) by the app: validate + start a locked session
  MFocus.exe --ctl stop         run elevated (UAC) by the app: end the running session now
  MFocus.exe --enforcer         run as SYSTEM by a Task Scheduler task: every 2 s make the
                                system match state.json (hosts block + close listed apps)
"""
import base64
import ctypes
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

APP = "MFocus"
TASK = "MFocusEnforcer"
DATA = Path(os.environ.get("MFOCUS_DIR") or (Path(os.environ.get("ProgramData", r"C:\ProgramData")) / APP))
INSTALL = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / APP
STATE = DATA / "state.json"
HIST = DATA / "history.json"
ERRFILE = DATA / "ctl_error.txt"
LOG = DATA / "enforcer.log"
HOSTS = Path(os.environ.get("MFOCUS_HOSTS") or (Path(os.environ.get("SystemRoot", r"C:\Windows"))
                                                / "System32" / "drivers" / "etc" / "hosts"))

BEGIN = "# >>> M FOCUS BEGIN >>>"
END = "# <<< M FOCUS END <<<"
PREFIXES = ("www.", "m.", "mobile.")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9-]{1,63}\.)+[a-z]{2,63}$")
APP_RE = re.compile(r"^[A-Za-z0-9._+-]{1,15}$")
# never let a blocklist entry close the system itself (or this program)
NEVER_KILL = {"system", "smss", "csrss", "wininit", "winlogon", "services", "lsass", "svchost",
              "explorer", "dwm", "fontdrvhost", "mfocus", "taskhostw", "sihost", "ctfmon",
              "registry", "idle", "conhost", "msmpeng", "searchhost", "startmenuexperiencehost"}
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW
TESTING = bool(os.environ.get("MFOCUS_TEST"))


# ---------------------------------------------------------------- small helpers
def read_json(path, default):
    for _ in range(4):  # the enforcer may be swapping the file right now
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return default
        except (OSError, ValueError):
            time.sleep(0.05)
    return default


def write_atomic(path, text):
    path = str(path)
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".mf-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path, data):
    write_atomic(path, json.dumps(data))


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run(cmd, timeout=30):
    return subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)


# ---------------------------------------------------------------- hosts file
def read_hosts():
    try:
        with open(HOSTS, "rb") as f:
            raw = f.read().decode("utf-8", errors="surrogateescape")
    except OSError:
        return ""
    return "\n".join(raw.splitlines()) + "\n"


def strip_block(text):
    out, skip = [], False
    for line in text.splitlines():
        if line.strip() == BEGIN:
            skip = True
        elif line.strip() == END:
            skip = False
        elif not skip:
            out.append(line)
    return "\n".join(out).rstrip("\n") + "\n"


def build_block(domains):
    names = []
    for d in domains:
        names.append(d)
        if not d.startswith(PREFIXES):
            names += [p + d for p in PREFIXES]
    lines = [BEGIN]
    for n in dict.fromkeys(names):
        lines.append("0.0.0.0 " + n)
        lines.append("::1 " + n)
    lines.append(END)
    return "\n".join(lines) + "\n"


def write_hosts(text):
    try:
        os.chmod(str(HOSTS), stat.S_IWRITE | stat.S_IREAD)  # clear the read-only flag if set
    except Exception:
        pass
    with open(HOSTS, "wb") as f:
        f.write(text.replace("\n", "\r\n").encode("utf-8", errors="surrogateescape"))
    if not TESTING:
        try:
            run(["ipconfig", "/flushdns"], timeout=10)
        except Exception:
            pass


# ---------------------------------------------------------------- closing apps
def _norm_proc(name):
    n = str(name).strip().lower()
    return n[:-4] if n.endswith(".exe") else n


def list_processes():
    """[(pid, exe name)] via the Toolhelp API."""
    from ctypes import wintypes as wt

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD),
                    ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    out = []
    if not snap or snap == ctypes.c_void_p(-1).value:
        return out
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(pe)
        ok = k32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            out.append((int(pe.th32ProcessID), pe.szExeFile))
            ok = k32.Process32NextW(snap, ctypes.byref(pe))
    finally:
        k32.CloseHandle(snap)
    return out


def terminate_process(pid):
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
    if not h:
        return False
    try:
        return bool(k32.TerminateProcess(h, 1))
    finally:
        k32.CloseHandle(h)


def kill_apps(apps, procs=None, terminate=None):
    """Close every running process whose exe name matches one of `apps` ('discord' = Discord.exe)."""
    want = {_norm_proc(a) for a in apps} - NEVER_KILL
    killed = []
    if not want:
        return killed
    try:
        plist = procs() if procs else list_processes()
    except Exception:
        return killed
    for pid, name in plist:
        if pid in (0, 4) or pid == os.getpid():
            continue
        if _norm_proc(name) in want:
            try:
                if (terminate or terminate_process)(pid):
                    killed.append(name)
            except Exception:
                pass
    return killed


# ---------------------------------------------------------------- phases / history
def current_phase(state, now):
    for i, ph in enumerate(state["phases"]):
        if ph["start"] <= now < ph["end"]:
            return i, ph
    return None, None


def record(state, j, now):
    p = state["phases"][j]
    minutes = int(round(state.get("acc", {}).get(str(j), 0.0) / 60))
    if minutes < 1:
        return
    hist = read_json(HIST, [])
    hist.append({"date": datetime.fromtimestamp(p["start"]).strftime("%Y-%m-%d"),
                 "minutes": minutes, "start": p["start"],
                 "end": min(p["end"], now), "label": state.get("label", "")})
    write_json(HIST, hist[-5000:])


class Enforcer:
    def __init__(self):
        self.last = time.time()
        self.n = 0
        self.key = None      # which session the in-memory counters belong to
        self.acc = {}        # observed focus seconds per phase index
        self.rec = []        # phases already written to history

    def tick(self, now=None, kill=None):
        """One pass. Returns True while a session is still active."""
        now = time.time() if now is None else now
        dt = max(0.0, min(now - self.last, 5.0))
        self.last = now
        state = read_json(STATE, None)
        text = read_hosts()
        if not isinstance(state, dict) or not isinstance(state.get("phases"), list):
            if BEGIN in text:
                write_hosts(strip_block(text))
            return False

        if state.get("start") != self.key:   # new session (or enforcer restarted)
            self.key = state.get("start")
            self.acc = dict(state.get("acc", {}))
            self.rec = list(state.get("recorded", []))
        state["acc"], state["recorded"] = self.acc, self.rec
        i, ph = current_phase(state, now)
        blocking = bool(ph and ph["kind"] == "focus")
        dirty = False
        if blocking:
            self.acc[str(i)] = self.acc.get(str(i), 0.0) + dt
            self.n += 1
            dirty = self.n % 15 == 0          # flush progress every ~30 s
        for j, p in enumerate(state["phases"]):
            if p["kind"] == "focus" and p["end"] <= now and j not in self.rec:
                record(state, j, now)
                self.rec.append(j)
                dirty = True

        if blocking:
            want = strip_block(text) + "\n" + build_block(state.get("domains", []))
        else:
            want = strip_block(text) if BEGIN in text else text
        if want != text:
            write_hosts(want)
        if blocking:
            kill_apps(state.get("apps", []), terminate=kill)

        if state.get("end", 0) <= now:
            try:
                os.remove(str(STATE))
            except FileNotFoundError:
                pass
            return False
        if dirty:
            write_json(STATE, state)
        return True


# ---------------------------------------------------------------- tamper resistance
def protect_process(pid=None):
    """Deny terminate / suspend / inject / change-permissions on a process to everybody, so
    Task Manager's "End task" says "Access is denied". (A debugger-level tool run by an admin
    can still get around this - nothing on Windows can stop a determined admin.)"""
    try:
        adv, k32 = ctypes.windll.advapi32, ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.GetCurrentProcess() if pid is None else k32.OpenProcess(0x40000, False, pid)  # WRITE_DAC
        if not h:
            return False
        psd = ctypes.c_void_p()
        # deny TERMINATE|CREATE_THREAD|VM_OPERATION|VM_WRITE|SUSPEND_RESUME|WRITE_DAC|WRITE_OWNER
        sddl = "D:(D;;0xC082B;;;WD)(A;;0x1FFFFF;;;WD)"
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(psd), None):
            return False
        adv.SetKernelObjectSecurity.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
        ok = adv.SetKernelObjectSecurity(h, 0x4, psd)  # DACL_SECURITY_INFORMATION
        if pid is not None:
            k32.CloseHandle(h)
        return bool(ok)
    except Exception as e:
        log("protect_process failed: %r" % (e,))
        return False


def _lock_down_data_dir():
    """Only Administrators/SYSTEM may write to the data dir; everyone may read."""
    try:
        run(["icacls", str(DATA), "/inheritance:r",
             "/grant:r", "*S-1-5-18:(OI)(CI)F",
             "/grant:r", "*S-1-5-32-544:(OI)(CI)F",
             "/grant:r", "*S-1-5-32-545:(OI)(CI)RX"])
    except Exception as e:
        log("icacls failed: %r" % (e,))


# ---------------------------------------------------------------- scheduled task
def _task_xml(command, watchdog=True):
    exe, _, args = command.partition('" ')
    exe = exe.lstrip('"')
    args = args.strip()
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # watchdog: every minute during a session, (re)start the enforcer if someone killed it.
    # "IgnoreNew" below makes this a no-op while the enforcer is already running.
    wd = ("""
    <TimeTrigger><StartBoundary>2024-01-01T00:00:00</StartBoundary>
      <Repetition><Interval>PT1M</Interval></Repetition><Enabled>true</Enabled></TimeTrigger>"""
          if watchdog else "")
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>M Focus enforcer - keeps the block active during a focus session.</Description></RegistrationInfo>
  <Triggers>
    <BootTrigger><Enabled>true</Enabled></BootTrigger>%s
  </Triggers>
  <Principals><Principal id="Author"><UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>6</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author"><Exec><Command>%s</Command><Arguments>%s</Arguments></Exec></Actions>
</Task>
""" % (wd, esc(exe), esc(args))


def _register_task(command, watchdog):
    xml_path = DATA / "task.xml"
    with open(xml_path, "w", encoding="utf-16") as f:
        f.write(_task_xml(command, watchdog))
    r = run(["schtasks", "/Create", "/TN", TASK, "/XML", str(xml_path), "/F"])
    if r.returncode != 0:
        raise RuntimeError("Could not create the scheduled task: "
                           + (r.stderr or r.stdout).decode("mbcs", "replace").strip())


def _start_task():
    r = run(["schtasks", "/Run", "/TN", TASK])
    if r.returncode != 0:
        raise RuntimeError("Could not start the scheduled task: "
                           + (r.stderr or r.stdout).decode("mbcs", "replace").strip())


def _self_command():
    return '"%s" --enforcer' % sys.executable if getattr(sys, "frozen", False) else None


def _install_copy():
    """Copy ourselves to Program Files (writable only by admins) so the SYSTEM task never runs
    a file that a normal user could swap out. Returns the task command line."""
    INSTALL.mkdir(parents=True, exist_ok=True)
    if getattr(sys, "frozen", False):
        src = Path(sys.executable)
        dst = INSTALL / "MFocus.exe"
        try:
            if not dst.exists() or dst.stat().st_size != src.stat().st_size \
                    or int(dst.stat().st_mtime) != int(src.stat().st_mtime):
                shutil.copy2(str(src), str(dst))
        except PermissionError:
            if not dst.exists():
                raise
        return '"%s" --enforcer' % dst
    here = Path(__file__).resolve().parent  # running from source (testing)
    for name in ("mf_core.py", "main.py"):
        shutil.copy2(str(here / name), str(INSTALL / name))
    return '"%s" "%s" --enforcer' % (sys.executable, INSTALL / "main.py")


# ---------------------------------------------------------------- enforcer main (SYSTEM)
def enforcer_main():
    DATA.mkdir(parents=True, exist_ok=True)
    protect_process()
    if getattr(sys, "frozen", False):  # one-file exe: also protect the launcher parent process
        protect_process(os.getppid())
    enf = Enforcer()
    while True:
        try:
            active = enf.tick()
        except Exception as e:  # never die in the middle of a session
            log("tick failed: %r" % (e,))
            active = True
        if not active:
            st = read_json(STATE, None)  # a new session may have been started just now
            if isinstance(st, dict) and st.get("end", 0) > time.time():
                continue
            break
        time.sleep(2)
    cmd = _self_command()  # session over: drop the once-a-minute watchdog trigger again
    if cmd and not isinstance(read_json(STATE, None), dict):
        try:
            _register_task(cmd, watchdog=False)
        except Exception as e:
            log("could not reset task: %r" % (e,))
    return 0


# ---------------------------------------------------------------- privileged start / stop (admin)
def build_state(req, now):
    """Validate a request. Everything is checked here because this runs with admin rights."""
    if not isinstance(req, dict):
        raise ValueError("bad request")
    raw = req.get("phases")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 48:
        raise ValueError("bad phases")
    phases, t = [], now
    for item in raw:
        try:
            kind, mins = item[0], item[1]
        except (TypeError, IndexError, KeyError):
            raise ValueError("bad phase")
        if kind not in ("focus", "break") or not isinstance(mins, int) or isinstance(mins, bool):
            raise ValueError("bad phase")
        if not 1 <= mins <= (720 if kind == "focus" else 120):
            raise ValueError("phase length out of range")
        phases.append({"kind": kind, "start": t, "end": t + mins * 60})
        t += mins * 60
    while phases and phases[-1]["kind"] == "break":
        phases.pop()
    if not phases or phases[0]["kind"] != "focus":
        raise ValueError("a session must start with focus")
    if phases[-1]["end"] - now > 24 * 3600:
        raise ValueError("session longer than 24 h")
    domains = req.get("domains", [])
    apps = req.get("apps", [])
    if not isinstance(domains, list) or not isinstance(apps, list) or len(domains) > 600 or len(apps) > 100:
        raise ValueError("bad lists")
    if not all(isinstance(d, str) and DOMAIN_RE.match(d) for d in domains):
        raise ValueError("bad domain")
    if not all(isinstance(a, str) and APP_RE.match(a) for a in apps):
        raise ValueError("bad app name")
    bad = sorted({a for a in apps if _norm_proc(a) in NEVER_KILL})
    if bad:
        raise ValueError("not allowed to close: " + ", ".join(bad))
    if not domains and not apps:
        raise ValueError("nothing to block")
    label = req.get("label", "")
    label = "".join(c for c in str(label) if c.isprintable()).strip()[:60]
    return {"version": 2, "start": now, "end": phases[-1]["end"], "label": label,
            "domains": sorted(set(domains)), "apps": sorted(set(apps)), "phases": phases}


def truncate(state, now):
    keep = []
    for p in state["phases"]:
        if p["start"] >= now:
            continue
        p["end"] = min(p["end"], now)
        keep.append(p)
    state["phases"], state["end"] = keep, now
    return state


def ctl_start(b64):
    if not is_admin() and not TESTING:
        raise RuntimeError("must run as administrator")
    try:
        req = json.loads(base64.urlsafe_b64decode(b64.encode("ascii")).decode("utf-8"))
        state = build_state(req, int(time.time()))
    except (ValueError, TypeError) as e:
        raise RuntimeError("rejected: %s" % e)
    DATA.mkdir(parents=True, exist_ok=True)
    if not TESTING:
        _lock_down_data_dir()
    old = read_json(STATE, {})
    if isinstance(old, dict) and old.get("end", 0) > time.time():
        raise RuntimeError("a session is already running")
    if TESTING:
        write_json(STATE, state)
        return
    command = _install_copy()
    write_json(STATE, state)
    try:
        _register_task(command, watchdog=True)
        _start_task()
    except Exception:
        try:
            os.remove(str(STATE))
        except FileNotFoundError:
            pass
        raise


def ctl_stop():
    if not is_admin() and not TESTING:
        raise RuntimeError("must run as administrator")
    state = read_json(STATE, None)
    if not isinstance(state, dict) or not isinstance(state.get("phases"), list):
        raise RuntimeError("no session running")
    write_json(STATE, truncate(state, int(time.time())))
    if not TESTING:
        try:
            _start_task()  # no-op if the enforcer is running (it is); makes sure it picks this up
        except Exception as e:
            log("stop: could not start task: %r" % (e,))


def ctl_main(argv):
    """argv = ['start', <base64 json>] or ['stop']. Exit code 0 ok, 1 error (message in ERRFILE)."""
    try:
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            if ERRFILE.exists():
                os.remove(str(ERRFILE))
        except Exception:
            pass
        if len(argv) == 2 and argv[0] == "start":
            ctl_start(argv[1])
        elif len(argv) == 1 and argv[0] == "stop":
            ctl_stop()
        else:
            raise RuntimeError("usage: --ctl start <b64> | --ctl stop")
        return 0
    except Exception as e:
        try:
            ERRFILE.write_text(str(e), encoding="utf-8")
        except Exception:
            pass
        log("ctl error: %r" % (e,))
        return 1


# ---------------------------------------------------------------- UAC elevation (used by the app)
def run_elevated_and_wait(args, pump=None):
    """Re-launch this program with UAC elevation, wait for it, return its exit code.
    Returns None if the user cancelled the UAC prompt (or it could not be launched)."""
    from ctypes import wintypes as wt

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wt.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wt.HWND),
            ("lpVerb", wt.LPCWSTR), ("lpFile", wt.LPCWSTR), ("lpParameters", wt.LPCWSTR),
            ("lpDirectory", wt.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wt.HINSTANCE),
            ("lpIDList", ctypes.c_void_p), ("lpClass", wt.LPCWSTR), ("hkeyClass", wt.HKEY),
            ("dwHotKey", wt.DWORD), ("hIconOrMonitor", wt.HANDLE), ("hProcess", wt.HANDLE),
        ]

    if getattr(sys, "frozen", False):
        exe, params = sys.executable, subprocess.list2cmdline(["--ctl"] + list(args))
    else:
        exe = sys.executable
        params = subprocess.list2cmdline([str(Path(__file__).resolve().parent / "main.py"),
                                          "--ctl"] + list(args))
    if len(params) > 30000:
        raise RuntimeError("Too many sites/apps selected for one session.")

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = exe
    info.lpParameters = params
    info.nShow = 0  # SW_HIDE
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        return None
    k32 = ctypes.windll.kernel32
    k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wt.DWORD]
    k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    while k32.WaitForSingleObject(info.hProcess, 100) == 0x102:  # WAIT_TIMEOUT
        if pump:
            pump()
    code = wt.DWORD()
    k32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    k32.CloseHandle(info.hProcess)
    return int(code.value)


def encode_request(req):
    return base64.urlsafe_b64encode(json.dumps(req).encode("utf-8")).decode("ascii")


def read_ctl_error():
    try:
        return ERRFILE.read_text(encoding="utf-8").strip()
    except Exception:
        return ""
