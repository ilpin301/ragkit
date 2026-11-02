#!/usr/bin/env python3
"""
drive_upload_status.py - did Google Drive for Desktop really upload this file?
Uses only the Python standard library (Windows). Read-only on Google's databases:
the metadata_sqlite_db (+ -wal, -shm) of an account is COPIED to a temp dir and the copy is opened.

  drive_upload_status.py version
  drive_upload_status.py check PATH [--wait SEC] [--interval SEC]
  drive_upload_status.py selftest [--canary-dir DIR] [--timeout SEC]
  drive_upload_status.py --self-test            (pure logic, no Drive needed)

check prints one line: UPLOADED / PENDING / NOT_FOUND / UNKNOWN.
Exit codes: 0 UPLOADED, 1 PENDING, 2 NOT_FOUND, 3 UNKNOWN (also: Drive not running).

How a file is judged UPLOADED (found empirically on DriveFS 118.0.1.0, see selftest):
  - item resolved by walking the folder chain from the account's root (title alone is not unique),
  - items.id is a real cloud id (a new file starts as 'local-<n>'),
  - items.file_size == local size and the item's local-content-modified-date == local mtime
    (an overwrite keeps the OLD id/size/revision until the upload finishes, but Drive records the
    new mtime in local-content-modified-date and adds a 'local-content-size' property),
  - no 'local-content-size' property and no queued operation that mentions the item id.
Known limit: an overwrite with identical size AND identical mtime cannot be told apart from no change.
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime

DRIVEFS = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "DriveFS")
VERIFIED = os.path.join(os.environ.get("LOCALAPPDATA", ""), "drive-upload-status", "verified.json")
DEFAULT_CANARY = r"J:\My Drive\LLM_WIKI\.status-canary"
MTIME_TOL_MS = 2
EXIT = {"UPLOADED": 0, "PENDING": 1, "NOT_FOUND": 2, "UNKNOWN": 3}
DB_FILES = ("metadata_sqlite_db", "metadata_sqlite_db-wal", "metadata_sqlite_db-shm")


# ---- pure logic -------------------------------------------------------------

def classify(item, props, ops_hit, size, mtime_ms):
    """item: {'id','file_size','modified_date'}; props: item_properties dict; ops_hit: a queued
    operation mentions the item. Returns (state, detail)."""
    why = []
    if str(item["id"]).startswith("local-"):
        why.append("not in cloud yet (id %s)" % item["id"])
    if item["file_size"] != size:
        why.append("cloud size %s != local %s" % (item["file_size"], size))
    if "local-content-size" in props:
        why.append("content upload pending (local-content-size=%s)" % props["local-content-size"])
    if ops_hit:
        why.append("queued operation for this item")
    lcm = props.get("local-content-modified-date")
    if lcm is None and not why:
        return "UNKNOWN", "item has no local-content-modified-date, cannot verify the revision"
    if lcm is not None and not any(
            v is not None and abs(v - mtime_ms) <= MTIME_TOL_MS for v in (lcm, item.get("modified_date"))):
        why.append("Drive has not picked up the latest local write (mtime differs by %d ms)" % abs(lcm - mtime_ms))
    if why:
        return "PENDING", "; ".join(why)
    return "UPLOADED", "id=%s size=%s" % (item["id"], item["file_size"])


def split_path(path):
    """'J:\\My Drive\\a\\b.txt' -> ('J', ['My Drive', 'a', 'b.txt']); None if no drive letter."""
    d, rest = os.path.splitdrive(os.path.abspath(path))
    if len(d) != 2 or d[1] != ":":
        return None
    return d[0].upper(), [p for p in rest.replace("/", "\\").split("\\") if p]


def run_logic_selftest():
    it = lambda i="1Q-A", s=100, m=5: {"id": i, "file_size": s, "modified_date": m}
    ok = {"local-content-modified-date": 1000}
    assert classify(it(), ok, False, 100, 1000)[0] == "UPLOADED"
    assert classify(it(), ok, False, 100, 1002)[0] == "UPLOADED"
    assert classify(it("local-7", 0), ok, False, 100, 1000)[0] == "PENDING"            # new file
    assert classify(it(), dict(ok, **{"local-content-size": 300}), False, 300, 1000)[0] == "PENDING"
    assert classify(it(s=200), ok, False, 300, 1000)[0] == "PENDING"                   # size differs
    assert classify(it(), {"local-content-modified-date": 900}, False, 100, 1000)[0] == "PENDING"  # not noticed yet
    assert classify(it(), ok, True, 100, 1000)[0] == "PENDING"                         # queued op
    assert classify(it(), {}, False, 100, 1000)[0] == "UNKNOWN"                        # cannot verify
    assert classify(it(), {}, False, 999, 1000)[0] == "PENDING"                        # pending beats unknown
    assert split_path(r"j:\My Drive\a\b.tgz") == ("J", ["My Drive", "a", "b.tgz"])
    assert split_path("J:/My Drive/a") == ("J", ["My Drive", "a"])
    print("self-test OK")


# ---- Drive state ------------------------------------------------------------

def drive_version():
    """Running DriveFS version(s) via PowerShell, or None when Drive is not running."""
    ps = ("Get-Process GoogleDriveFS -ErrorAction SilentlyContinue | Where-Object { $_.Path } | "
          "ForEach-Object { [System.Diagnostics.FileVersionInfo]::GetVersionInfo($_.Path).FileVersion } | "
          "Sort-Object -Unique")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    vers = [v.strip() for v in out.splitlines() if v.strip()]
    return "+".join(vers) if vers else None


def mount_accounts(letter):
    """Account folder names whose Drive is mounted at <letter>:. Falls back to every account
    folder if the registry mapping is unreadable (then the folder chain decides)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Google\DriveFS") as k:
            prefs = json.loads(winreg.QueryValueEx(k, "PerAccountPreferences")[0])["per_account_preferences"]
        return [p["key"] for p in prefs if p["value"].get("mount_point_path", "").upper().rstrip(":\\") == letter]
    except (OSError, ValueError, KeyError):
        return [d for d in os.listdir(DRIVEFS)
                if d.isdigit() and os.path.exists(os.path.join(DRIVEFS, d, DB_FILES[0]))]


class Snapshot:
    """Copy of one account's metadata DB, refreshed only when the live files changed."""

    def __init__(self, account):
        self.src = os.path.join(DRIVEFS, account)
        self.tmp = tempfile.mkdtemp(prefix="dus_")
        self.sig = None
        self.con = None

    def _sig(self):
        out = []
        for f in DB_FILES:
            try:
                s = os.stat(os.path.join(self.src, f))
                out.append((s.st_size, s.st_mtime_ns))
            except OSError:
                out.append(None)
        return tuple(out)

    def refresh(self):
        """True if a consistent copy is open. The live files are only ever read."""
        for _ in range(4):
            sig = self._sig()
            if sig == self.sig and self.con:
                return True
            if self.con:
                self.con.close()
                self.con = None
            try:
                for f in DB_FILES:
                    dst = os.path.join(self.tmp, f)
                    if os.path.exists(os.path.join(self.src, f)):
                        shutil.copyfile(os.path.join(self.src, f), dst)
                    elif os.path.exists(dst):
                        os.remove(dst)
                if self._sig() != sig:      # Drive wrote while we copied: take another copy
                    time.sleep(0.3)
                    continue
                con = sqlite3.connect(os.path.join(self.tmp, DB_FILES[0]))
                con.execute("select count(*) from items").fetchone()
                self.con, self.sig = con, sig
                return True
            except (OSError, sqlite3.Error):
                time.sleep(0.5)
        return False

    def close(self):
        if self.con:
            self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def find(self, parts):
        """Walk parts from the account root. Returns ('ok', stable_id, row) / ('missing', None, why) /
        ('dup', None, why)."""
        con = self.con
        r = con.execute("select value from properties where property='root_id'").fetchone()
        root = con.execute("select stable_id, local_title from items where id=?",
                           (r[0].decode() if isinstance(r[0], bytes) else r[0],)).fetchone() if r else None
        if not root:
            return "missing", None, "account root not found"
        if parts[0].casefold() not in ("my drive", (root[1] or "").casefold()):
            return "missing", None, "not under My Drive"
        cur = root[0]
        for n, name in enumerate(parts[1:], 1):
            kids = [k for k in con.execute(
                "select i.stable_id, i.local_title, i.id, i.file_size, i.modified_date from stable_parents p "
                "join items i on i.stable_id=p.item_stable_id where p.parent_stable_id=? "
                "and i.trashed=0 and i.is_tombstone=0", (cur,)) if (k[1] or "").casefold() == name.casefold()]
            if not kids:
                return "missing", None, "'%s' not in Drive" % name
            if len(kids) > 1:
                return "dup", None, "%d items named '%s' in the same folder" % (len(kids), name)
            cur = kids[0][0]
        k = kids[0]
        props = {key: val for key, val in con.execute(
            "select key, value from item_properties where item_stable_id=?", (cur,))}
        ops = con.execute("select proto from operations").fetchall()
        return "ok", cur, ({"id": k[2], "file_size": k[3], "modified_date": k[4]}, props, ops)


def check_once(path, snaps):
    """One look at the DB. snaps caches Snapshot objects per account. Returns (state, detail)."""
    sp = split_path(path)
    if not os.path.isfile(path):
        return "NOT_FOUND", "no such local file: %s" % path
    st = os.stat(path)
    size, mtime_ms = st.st_size, st.st_mtime_ns // 1_000_000
    if not sp or len(sp[1]) < 2:
        return "NOT_FOUND", "!not under a Drive mount"
    accounts = mount_accounts(sp[0])
    if not accounts:
        return "NOT_FOUND", "!%s: is not a Drive mount" % sp[0]
    hits, notes = [], []
    for acc in accounts:
        if not os.path.exists(os.path.join(DRIVEFS, acc, DB_FILES[0])):
            continue
        if acc not in snaps:
            snaps[acc] = Snapshot(acc)
        snap = snaps[acc]
        if not snap.refresh():
            notes.append("account %s: DB copy unreadable" % acc)
            continue
        try:
            kind, _, res = snap.find(sp[1])
        except sqlite3.Error as e:
            return "UNKNOWN", "account %s: DB query failed: %s" % (acc, e)
        if kind == "ok":
            hits.append((acc, res))
        else:
            notes.append("account %s: %s" % (acc, res))
            if kind == "dup":
                return "UNKNOWN", notes[-1]
    if len(hits) > 1:
        return "UNKNOWN", "path resolves in %d accounts: %s" % (len(hits), ", ".join(h[0] for h in hits))
    if not hits:
        unreadable = any("unreadable" in n for n in notes)
        return ("UNKNOWN" if unreadable else "NOT_FOUND"), "; ".join(notes) or "no Drive account for this mount"
    acc, (item, props, ops) = hits[0]
    ops_hit = any(item["id"].encode() in (o[0] or b"") for o in ops)
    state, detail = classify(item, props, ops_hit, size, mtime_ms)
    return state, "%s [account %s]" % (detail, acc)


def check(path, wait=0, interval=5, quiet=False):
    """Poll until UPLOADED or timeout. Returns (state, detail)."""
    if drive_version() is None:
        return "UNKNOWN", "Google Drive for Desktop is not running"
    snaps, t0 = {}, time.time()
    try:
        while True:
            state, detail = check_once(path, snaps)
            # a just-copied file may not be in the DB yet: keep waiting while it exists locally
            retry = state == "PENDING" or (state == "NOT_FOUND" and os.path.isfile(path) and not detail.startswith("!"))
            if state == "UPLOADED" or time.time() - t0 >= wait or not retry:
                return state, detail.lstrip("!")
            time.sleep(interval)
    finally:
        for s in snaps.values():
            s.close()


# ---- verified.json + selftest -----------------------------------------------

def read_verified():
    try:
        with open(VERIFIED, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def selftest(canary_dir=DEFAULT_CANARY, timeout=180, interval=3):
    ver = drive_version()
    if ver is None:
        print("SELFTEST FAIL: Google Drive for Desktop is not running")
        return False
    os.makedirs(canary_dir, exist_ok=True)
    name = "canary_%d.bin" % os.getpid()
    dst = os.path.join(canary_dir, name)
    tmpd = tempfile.mkdtemp(prefix="dus_canary_")
    res = []

    def make(n, size):
        p = os.path.join(tmpd, n)
        with open(p, "wb") as f:
            f.write(os.urandom(size))
        return p

    def step(label, ok, detail):
        res.append(ok)
        print("  %-34s %s  %s" % (label, "PASS" if ok else "FAIL", detail))

    try:
        print("selftest on DriveFS %s, canary %s" % (ver, dst))
        shutil.copy2(make("a1.bin", 200_000), dst)
        t = time.time()
        s, d = check(dst, wait=timeout, interval=interval)
        step("new file -> UPLOADED", s == "UPLOADED", "%s (%.0fs) %s" % (s, time.time() - t, d))
        if s == "UPLOADED":
            shutil.copy2(make("a2.bin", 300_000), dst)      # same call Copy-Item -Force makes
            s0, d0 = check(dst, wait=0)
            step("overwrite not UPLOADED at once", s0 != "UPLOADED", "%s %s" % (s0, d0))
            t = time.time()
            s, d = check(dst, wait=timeout, interval=interval)
            step("overwrite -> UPLOADED", s == "UPLOADED", "%s (%.0fs) %s" % (s, time.time() - t, d))
        else:
            step("overwrite case", False, "skipped, new-file case failed")
        s, d = check(os.path.join(canary_dir, "does_not_exist_%d.bin" % os.getpid()), wait=0)
        step("missing path -> NOT_FOUND", s == "NOT_FOUND", s)
    finally:
        try:
            os.remove(dst)
        except OSError:
            pass
        shutil.rmtree(tmpd, ignore_errors=True)
    ok = len(res) == 4 and all(res)
    if ok:
        os.makedirs(os.path.dirname(VERIFIED), exist_ok=True)
        with open(VERIFIED, "w", encoding="utf-8") as f:
            json.dump({"version": ver, "date": datetime.now().isoformat(timespec="seconds"), "result": "pass"}, f)
    print("SELFTEST %s (DriveFS %s)" % ("PASS" if ok else "FAIL", ver))
    return ok


# ---- CLI --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true", help="assert the pure logic and exit")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("version")
    c = sub.add_parser("check")
    c.add_argument("path")
    c.add_argument("--wait", type=float, default=0, help="poll up to SEC seconds for UPLOADED")
    c.add_argument("--interval", type=float, default=5)
    c.add_argument("--canary-dir", default=DEFAULT_CANARY)
    c.add_argument("--selftest-timeout", type=float, default=180, help="per-case timeout of the automatic selftest")
    s = sub.add_parser("selftest")
    s.add_argument("--canary-dir", default=DEFAULT_CANARY)
    s.add_argument("--timeout", type=float, default=180)
    a = ap.parse_args()

    if a.self_test:
        run_logic_selftest()
        return 0
    if a.cmd == "version":
        v = drive_version()
        print(v or "Google Drive for Desktop is not running")
        return 0 if v else 3
    if a.cmd == "selftest":
        return 0 if selftest(a.canary_dir, a.timeout) else 1
    if a.cmd == "check":
        ver = drive_version()
        if ver is None:
            print("UNKNOWN Google Drive for Desktop is not running")
            return EXIT["UNKNOWN"]
        old = read_verified()
        if old.get("version") != ver or old.get("result") != "pass":
            print("Google Drive for Desktop version changed: %s -> %s, re-validating" % (old.get("version") or "none", ver))
            if not selftest(a.canary_dir, a.selftest_timeout):
                print("UNKNOWN status check not trusted on this version - check the tray icon")
                return EXIT["UNKNOWN"]
        state, detail = check(a.path, a.wait, a.interval)
        print("%s %s - %s" % (state, a.path, detail))
        return EXIT[state]
    ap.print_help()
    return 3


if __name__ == "__main__":
    sys.exit(main())
