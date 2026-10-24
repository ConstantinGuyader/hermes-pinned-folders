"""Path-safety tests for the devbox file explorer in dashboard/plugin_api.py.

Run: python3 tests/test_fs_api.py   (needs fastapi, or the stubs in
$HERMES_HOME/cache/scratch/pfstub: PYTHONPATH=<stubs> python3 tests/test_fs_api.py)
"""
import base64, importlib.util, os, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
try:
    import fastapi  # noqa: F401
except ImportError:
    stub = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "cache/scratch/pfstub"
    sys.path.insert(0, str(stub))


def load(home: Path):
    os.environ["HOME"] = str(home)
    os.environ["HERMES_HOME"] = str(home / ".hermes")
    spec = importlib.util.spec_from_file_location("pf_api", ROOT / "dashboard/plugin_api.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(self.tmp.name))
        self.home = base / "home"
        self.outside = base / "outside"
        for d in ("home/work/sub", "home/.ssh", "home/.config/devbox", "home/.config/gh", "home/.hermes", "home/.config/hermes-m365", "outside"):
            (base / d).mkdir(parents=True)
        (self.home / "work/a.txt").write_text("hello")
        (self.home / ".ssh/id_ed25519").write_text("SECRET")
        (self.home / ".config/devbox/secrets.env").write_text("K=V")
        (self.home / ".config/gh/hosts.yml").write_text("t")
        (self.home / ".config/hermes-m365/token.json").write_text("t")
        (self.home / ".hermes/.env").write_text("K=V")
        (self.home / ".hermes/auth.json").write_text("{}")
        (self.home / ".git-credentials").write_text("x")
        (self.home / ".netrc").write_text("x")
        (self.home / "work/cert.pem").write_text("x")
        (self.home / "work/my.key").write_text("x")
        (self.home / ".env.example").write_text("ok")
        (self.outside / "o.txt").write_text("out")
        os.symlink(self.outside, self.home / "work/escape")
        os.symlink(self.outside / "o.txt", self.home / "work/escfile")
        os.symlink(self.home / ".ssh", self.home / "work/sshlink")
        self.m = load(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def err(self, fn, *a, **k):
        with self.assertRaises(self.m.FsError) as c:
            fn(*a, **k)
        return c.exception

    def test_list_home_hides_secrets(self):
        r = self.m.fs_list(str(self.home))
        names = {e["name"] for e in r["entries"]}
        self.assertIn("work", names)
        self.assertIn(".env.example", names)
        for bad in (".ssh", ".git-credentials", ".netrc"):
            self.assertNotIn(bad, names)
        self.assertIsNone(r["parent"])
        self.assertNotIn("devbox", {e["name"] for e in self.m.fs_list(str(self.home / ".config"))["entries"]})
        hn = {e["name"] for e in self.m.fs_list(str(self.home / ".hermes"))["entries"]}
        self.assertNotIn(".env", hn); self.assertNotIn("auth.json", hn)
        wn = {e["name"] for e in self.m.fs_list(str(self.home / "work"))["entries"]}
        self.assertTrue(wn.isdisjoint({"cert.pem", "my.key", "escape", "escfile", "sshlink"}), wn)

    def test_traversal(self):
        for p in ("../outside/o.txt", str(self.home / "work/../../outside/o.txt"), "/etc/passwd", "/", str(self.home.parent)):
            self.assertEqual(self.err(self.m.fs_read, p).status, 403, p)
        self.assertEqual(self.err(self.m.fs_list, "/").status, 403)

    def test_symlink_escape(self):
        for p in ("work/escape", "work/escape/o.txt", "work/escfile", "work/sshlink/id_ed25519"):
            self.assertEqual(self.err(self.m.fs_read, p).status, 403, p)
        self.assertEqual(self.err(self.m.fs_write, "work/escape/new.txt", text="x").status, 403)
        self.assertEqual(self.err(self.m.fs_mkdir, "work/escape/d").status, 403)
        self.assertFalse((self.outside / "new.txt").exists())

    def test_denied_paths(self):
        for p in (".ssh", ".ssh/id_ed25519", ".config/devbox", ".config/devbox/secrets.env", ".config/gh/hosts.yml",
                  ".config/hermes-m365/token.json", ".hermes/.env", ".hermes/auth.json", ".git-credentials", ".netrc",
                  "work/cert.pem", "work/my.key"):
            self.assertEqual(self.err(self.m.fs_read, p).status, 403, p)
            self.assertEqual(self.err(self.m.fs_write, p, text="x", overwrite=True).status, 403, p)
            self.assertEqual(self.err(self.m.fs_delete, p).status, 403, p)
            self.assertEqual(self.err(self.m.fs_rename, p, "zz").status, 403, p)
            self.assertEqual(self.err(self.m.fs_download, p).status, 403, p)
        self.assertEqual(self.err(self.m.fs_write, "work/new.pem", text="x").status, 403)
        self.assertEqual(self.err(self.m.fs_rename, "work/a.txt", "x.key").status, 403)
        self.assertEqual((self.home / ".ssh/id_ed25519").read_text(), "SECRET")
        self.assertEqual(self.m.fs_read(".env.example")["text"], "ok")

    def test_cannot_move_or_delete_ancestor_of_secret(self):
        self.assertEqual(self.err(self.m.fs_delete, ".config").status, 403)
        self.assertEqual(self.err(self.m.fs_rename, ".config", "c2").status, 403)
        self.assertEqual(self.err(self.m.fs_delete, ".hermes").status, 403)
        self.assertEqual(self.err(self.m.fs_delete, str(self.home)).status, 403)
        self.assertTrue((self.home / ".config/devbox/secrets.env").exists())

    def test_read_write_roundtrip(self):
        r = self.m.fs_read("work/a.txt")
        self.assertEqual((r["kind"], r["text"]), ("text", "hello"))
        self.assertEqual(self.err(self.m.fs_write, "work/a.txt", text="n").status, 409)
        self.m.fs_write("work/a.txt", text="n", overwrite=True)
        self.assertEqual((self.home / "work/a.txt").read_text(), "n")
        self.m.fs_write("work/up.bin", b64=base64.b64encode(b"\0\1\2").decode())
        self.assertEqual(self.m.fs_read("work/up.bin")["kind"], "binary")
        self.assertEqual(self.m.fs_download("work/up.bin")["size"], 3)
        self.assertEqual(self.err(self.m.fs_write, "work/bad", b64="!!!").status, 400)

    def test_text_cap(self):
        (self.home / "work/big.txt").write_text("a" * (self.m.FS_TEXT_CAP + 10))
        r = self.m.fs_read("work/big.txt")
        self.assertTrue(r["truncated"]); self.assertEqual(len(r["text"]), self.m.FS_TEXT_CAP)

    def test_mkdir_rename_delete(self):
        self.m.fs_mkdir("work/new")
        self.assertEqual(self.err(self.m.fs_mkdir, "work/new").status, 409)
        self.m.fs_rename("work/new", "renamed")
        self.assertTrue((self.home / "work/renamed").is_dir())
        for bad in ("../x", "a/b", "..", ""):
            self.assertEqual(self.err(self.m.fs_rename, "work/renamed", bad).status, 400, bad)
        (self.home / "work/renamed/f").write_text("x")
        self.m.fs_delete("work/renamed")
        self.assertFalse((self.home / "work/renamed").exists())

    def test_delete_symlink_does_not_touch_target(self):
        # a link to an allowed dir inside home is removed, not followed
        os.symlink(self.home / "work/sub", self.home / "work/lnk")
        (self.home / "work/sub/keep").write_text("k")
        self.m.fs_delete("work/lnk")
        self.assertTrue((self.home / "work/sub/keep").exists())

    def test_open_falls_back(self):
        r = self.m.fs_open([str(self.home / "nope"), str(self.home / "work")])
        self.assertEqual(r["path"], str(self.home / "work"))
        self.assertEqual(self.m.fs_open(["/etc", ".ssh"])["path"], str(self.home))


if __name__ == "__main__":
    unittest.main()
