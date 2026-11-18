"""Keep every automation Chrome window on another Windows virtual desktop, so nothing opens or flashes on the one the
user works on. The windows stay fully usable there: switch with Ctrl+Win+Right to watch them.

    python desk.py            # the daemon (README "Start"; live.py and the driver start it when it isn't running)
    python desk.py --status   # is it running, which desktop, how many windows it holds
    python desk.py --stop     # stop it: the windows come back to the desktop they were opened on

Which desktop: "desktop" in machine.json (2 = the second one in Task View). Without that key, or when that desktop
doesn't exist, the daemon exits and windows open where the user is, as before.

How, with documented APIs only: Windows lets a process move only its own windows between virtual desktops
(IVirtualDesktopManager::MoveWindowToDesktop), and an owned window always lives on its owner's desktop. So the daemon
makes one small window of its own ("aite automation windows"), moves it to the target desktop, and makes itself the
owner of every top-level window of the automation Chromes (the one on chrome-profile/, and the ones live.py starts,
found the same way watchdog.py finds them). It polls a few times a second, so a new window shows on the user's desktop
for a fraction of a second at most. A new window is hidden while it is moved and shown again
without being activated, so it never takes the keyboard focus with it (Windows switches desktops to follow the focused
window). The tools also open their windows without activating them.

hand.py's sign-in windows (--aite-hand) are left on the user's desktop, except the ones `hand.py park` lists under
"parked" in .hand.json: those are held here too, and handed back once they leave that list.

Owned windows are destroyed with their owner, so on exit (--stop, Ctrl+C, logoff) the daemon first hands every window
back. If it is killed hard, the Chrome windows it held close; the driver and live.py open new ones."""
import ctypes, ctypes.wintypes as wt, json, os, subprocess, sys, time, uuid, winreg

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, ".desk.json")
MACHINE = os.path.join(HERE, "machine.json")
PROFILE = os.path.join(HERE, "chrome-profile").lower()
LIVE_MARK = "--aite-live"
HAND = os.path.join(HERE, ".hand.json")  # hand.py: "parked" lists Chromes to hold here too
TITLE = "aite automation windows"
user32, kernel32, ole32 = ctypes.windll.user32, ctypes.windll.kernel32, ctypes.windll.ole32
GWLP_HWNDPARENT = -8
user32.SetWindowLongPtrW.restype = ctypes.c_void_p
user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_void_p]
user32.GetWindow.restype = wt.HWND
user32.GetWindow.argtypes = [wt.HWND, ctypes.c_uint]
user32.IsWindow.argtypes = [wt.HWND]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]


def wanted_desktop():
    """The 1-based desktop number from machine.json, or None."""
    try:
        n = json.load(open(MACHINE, encoding="utf-8")).get("desktop")
        return int(n) if n else None
    except Exception:
        return None


def desktop_ids():
    """GUIDs of the user's virtual desktops, in Task View order."""
    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\VirtualDesktops")
    raw = winreg.QueryValueEx(k, "VirtualDesktopIDs")[0]
    return [str(uuid.UUID(bytes_le=raw[i:i + 16])) for i in range(0, len(raw), 16)]


def _alive(pid):
    h = kernel32.OpenProcess(0x1000, False, int(pid))
    if not h:
        return False
    code = ctypes.c_ulong()
    kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    kernel32.CloseHandle(h)
    return code.value == 259


def state():
    """The running daemon's state ({pid, hwnd, desktop, held}), or None."""
    try:
        s = json.load(open(STATE, encoding="utf-8"))
        return s if _alive(s["pid"]) and user32.IsWindow(s["hwnd"]) else None
    except Exception:
        return None


def ensure():
    """Start the daemon if machine.json asks for a desktop and it isn't running. Returns its state or None. Never
    raises: without it, windows simply open on the current desktop."""
    try:
        n = wanted_desktop()
        if not n:
            return None
        s = state()
        if s:
            return s
        if len(desktop_ids()) < n:
            return None
        flags = {"creationflags": 0x00000008 | 0x00000200 | 0x08000000} if os.name == "nt" else {}
        log = open(os.path.join(HERE, "desk.out"), "a")
        subprocess.Popen([sys.executable, "-u", os.path.abspath(__file__)], cwd=HERE, stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, **flags)
        end = time.time() + 6
        while time.time() < end:
            s = state()
            if s:
                return s
            time.sleep(0.1)
    except Exception:
        pass
    return None


# ---- the daemon ----

class GUID(ctypes.Structure):
    _fields_ = [("d1", wt.DWORD), ("d2", wt.WORD), ("d3", wt.WORD), ("d4", ctypes.c_ubyte * 8)]

    def __init__(self, s=None):
        super().__init__()
        if s:
            ole32.CLSIDFromString(ctypes.c_wchar_p("{" + s.strip("{}") + "}"), ctypes.byref(self))


def _vdm():
    """(manager, GetWindowDesktopId, MoveWindowToDesktop) of the documented IVirtualDesktopManager, or None."""
    ole32.CoInitialize(None)
    clsid, iid, mgr = GUID("aa509086-5ca9-4c25-8f95-589d3c07b48a"), GUID("a5cd92ff-29be-454c-8d04-d82879fb3f1b"), ctypes.c_void_p()
    if ole32.CoCreateInstance(ctypes.byref(clsid), None, 1 | 4, ctypes.byref(iid), ctypes.byref(mgr)):
        return None
    vt = ctypes.cast(ctypes.cast(mgr, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    get_desktop = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wt.HWND, ctypes.POINTER(GUID))(vt[4])
    move = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wt.HWND, ctypes.POINTER(GUID))(vt[5])
    return mgr, get_desktop, move


def window_desktop(hwnd):
    """1-based virtual desktop (Task View order) that a window is on, or None. Read-only; works for any window."""
    try:
        vdm = _vdm()
        if not vdm:
            return None
        g = GUID()
        if vdm[1](vdm[0], hwnd, ctypes.byref(g)) != 0:
            return None
        for i, d in enumerate(desktop_ids()):
            if bytes(GUID(d)) == bytes(g):
                return i + 1
    except Exception:
        pass
    return None


def parked_pids():
    """Browser pids hand.py asked to hold on the automation desktop (`hand.py park`)."""
    try:
        return {int(p) for p in json.load(open(HAND, encoding="utf-8")).get("parked", [])}
    except Exception:
        return set()


def automation_pids():
    """Browser processes of the automation Chromes (not their renderer/gpu children: those own no windows)."""
    import psutil
    out = set()
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if (p.info["name"] or "").lower() != "chrome.exe":
                continue
            cmd = p.info["cmdline"] or []
            if any(a.startswith("--type=") for a in cmd):
                continue
            line = " ".join(cmd)
            if PROFILE in line.lower() or LIVE_MARK in line:
                out.add(p.pid)
        except Exception:
            pass
    return out


def chrome_windows(pids):
    """Visible top-level, unowned-or-ours windows of these processes."""
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(h, _):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(h):
            found.append(h)
        return True
    user32.EnumWindows(cb, 0)
    return found


def daemon():
    n = wanted_desktop()
    if not n:
        print("desk: no \"desktop\" in machine.json; nothing to do", flush=True)
        return 0
    if state():
        print("desk: already running", flush=True)
        return 0
    ids = desktop_ids()
    if len(ids) < n:
        print(f"desk: virtual desktop {n} doesn't exist ({len(ids)} desktops); create it in Task View", flush=True)
        return 1
    import win32api, win32con, win32gui
    vdm = _vdm()
    if not vdm:
        print("desk: IVirtualDesktopManager not available", flush=True)
        return 1
    mgr, get_desktop, move = vdm

    wc = win32gui.WNDCLASS()
    wc.lpfnWndProc, wc.lpszClassName, wc.hInstance = win32gui.DefWindowProc, "AiteDesktopAnchor", win32api.GetModuleHandle(None)
    win32gui.RegisterClass(wc)
    # A regular app window: Windows only tracks those per desktop (a tool or no-activate window is on every desktop).
    anchor = win32gui.CreateWindowEx(win32con.WS_EX_APPWINDOW, wc.lpszClassName, TITLE, win32con.WS_OVERLAPPEDWINDOW,
                                     40, 40, 320, 80, 0, 0, wc.hInstance, None)
    win32gui.ShowWindow(anchor, win32con.SW_SHOWMINNOACTIVE)
    target = GUID(ids[n - 1])

    def on_target():
        g = GUID()
        return get_desktop(mgr, anchor, ctypes.byref(g)) == 0 and bytes(g) == bytes(target)

    end = time.time() + 5
    while not on_target() and time.time() < end:
        win32gui.PumpWaitingMessages()
        move(mgr, anchor, ctypes.byref(target))
        time.sleep(0.15)
    if not on_target():
        print("desk: could not move the anchor window to desktop", n, flush=True)
        win32gui.DestroyWindow(anchor)
        return 1
    held = set()

    def release():
        for h in list(held):
            if user32.IsWindow(h) and user32.GetWindow(h, 4) == anchor:
                user32.SetWindowLongPtrW(h, GWLP_HWNDPARENT, None)
        held.clear()

    def save():
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "hwnd": anchor, "desktop": n, "held": len(held), "parks": 1,
                       "at": time.time()}, f)
        os.replace(tmp, STATE)

    save()
    print(f"desk: holding automation windows on desktop {n}", flush=True)
    stop_flag = STATE + ".stop"
    pids, tick, parked, hand_held = set(), 0, set(), {}  # hand_held: hwnd -> pid of windows held because parked
    try:
        while True:
            win32gui.PumpWaitingMessages()
            if os.path.exists(stop_flag):
                os.remove(stop_flag)
                break
            if tick % 8 == 0:
                parked = parked_pids()
                pids = automation_pids() | parked
                for h, p in list(hand_held.items()):  # unparked (or its Chrome closed): hand the window back
                    if p not in parked:
                        if user32.IsWindow(h) and user32.GetWindow(h, 4) == anchor:
                            user32.SetWindowLongPtrW(h, GWLP_HWNDPARENT, None)
                        held.discard(h)
                        del hand_held[h]
            if tick % 20 == 0 and not on_target():  # the user dragged the anchor to another desktop: follow them
                g = GUID()
                if get_desktop(mgr, anchor, ctypes.byref(g)) != 0:
                    move(mgr, anchor, ctypes.byref(target))
            n_before = len(held)
            for h in chrome_windows(pids):
                if user32.GetWindow(h, 4) in (None, 0):  # GW_OWNER: a top-level window nobody owns yet
                    # Windows follows the focused window: moving it while it has the focus switches the user to the
                    # other desktop. Hiding it first gives the focus back to what the user was in; it is then moved
                    # and shown again without being activated.
                    user32.ShowWindow(h, 0)                    # SW_HIDE
                    user32.SetWindowLongPtrW(h, GWLP_HWNDPARENT, anchor)
                    user32.ShowWindow(h, 4)                    # SW_SHOWNOACTIVATE
                    held.add(h)
                    wp = wt.DWORD()
                    user32.GetWindowThreadProcessId(h, ctypes.byref(wp))
                    if wp.value in parked:
                        hand_held[h] = wp.value
            held.intersection_update({h for h in held if user32.IsWindow(h)})
            if len(held) != n_before or tick % 40 == 0:
                save()
            tick += 1
            time.sleep(0.12)
    except KeyboardInterrupt:
        pass
    finally:
        release()
        try:
            os.remove(STATE)
        except OSError:
            pass
        win32gui.DestroyWindow(anchor)
    print("desk: stopped, windows handed back", flush=True)
    return 0


if __name__ == "__main__":
    if "--status" in sys.argv:
        s = state()
        print(f"desk: running (pid {s['pid']}), holding {s['held']} windows on desktop {s['desktop']}" if s
              else f"desk: not running (machine.json desktop: {wanted_desktop()})")
        sys.exit(0)
    if "--stop" in sys.argv:
        s = state()
        if not s:
            print("desk: not running")
            sys.exit(0)
        open(STATE + ".stop", "w").close()
        end = time.time() + 5
        while state() and time.time() < end:
            time.sleep(0.1)
        print("desk: stopped, windows handed back" if not state() else "desk: did not stop")
        sys.exit(0)
    sys.exit(daemon())
