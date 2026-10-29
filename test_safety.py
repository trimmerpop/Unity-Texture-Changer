"""Safety tests: swapping the source folder while a big asset is being processed.

Run: python test_safety.py
"""
import os
import shutil
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT)

from PIL import Image
from PyQt6.QtWidgets import QApplication

import asset_manager
import main as app_main
from asset_manager import AssetManager

APP = QApplication.instance() or QApplication([])
CHECKS = 0
FAILURES = []


def check(name, condition, detail=None):
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  PASS  {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL  {name}  {detail!r}")


def test_file_signature(root):
    path = os.path.join(root, "sig.bin")
    with open(path, "wb") as handle:
        handle.write(b"abc")
    first = AssetManager.file_signature(path)
    check("file_signature returns (size, mtime)", isinstance(first, tuple) and len(first) == 2, first)
    check("file_signature is None for a missing file",
          AssetManager.file_signature(os.path.join(root, "nope.bin")) is None)
    with open(path, "ab") as handle:
        handle.write(b"def")
    check("file_signature changes after a write", AssetManager.file_signature(path) != first,
          (first, AssetManager.file_signature(path)))


def test_apply_aborts_when_asset_changes(root):
    game_dir = os.path.join(root, "game")
    os.makedirs(game_dir, exist_ok=True)
    asset = make_asset(game_dir)
    if not asset:
        print("  SKIP  apply guard (test asset not available)")
        return

    am = AssetManager(game_dir, game_dir, os.path.join(root, "t1"), os.path.join(root, "t2"))
    # No extraction needed: just pick a texture that has readable dimensions
    import gc
    import UnityPy
    env = UnityPy.load(asset)
    target = None
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        try:
            data = obj.read()
            width = getattr(data, "m_Width", 0)
            height = getattr(data, "m_Height", 0)
        except Exception:
            continue
        if 0 < width < 4096 and 0 < height < 4096:
            target = {"path_id": obj.path_id, "width": width, "height": height}
            break
    del env
    gc.collect()
    if not target:
        print("  SKIP  apply guard (no usable Texture2D found)")
        return
    replacement = os.path.join(root, "replacement.png")
    Image.new("RGBA", (target["width"], target["height"]), (200, 10, 10, 255)).save(replacement)

    real_load = asset_manager.UnityPy.load
    logs = []

    def swapped_load(path, *args, **kwargs):
        env = real_load(path, *args, **kwargs)
        # simulate the folder/file being swapped while the asset is in memory
        with open(path, "ab") as handle:
            handle.write(b"\0")
        return env

    try:
        asset_manager.UnityPy.load = swapped_load
        ok = am.replace_textures_batch(asset, [(target["path_id"], replacement)],
                                       log_callback=logs.append)
    finally:
        asset_manager.UnityPy.load = real_load

    check("apply: refuses to write when the asset changed on disk", ok is False, ok)
    check("apply: explains why it refused",
          any("changed on disk" in line for line in logs), logs[-3:])


def test_extract_skips_changed_file(root):
    game_dir = os.path.join(root, "game2")
    os.makedirs(game_dir, exist_ok=True)
    asset = make_asset(game_dir)
    if not asset:
        print("  SKIP  extract guard (test asset not available)")
        return

    am = AssetManager(game_dir, game_dir, os.path.join(root, "t1"), os.path.join(root, "t2"))
    real_load = asset_manager.UnityPy.load
    logs = []

    def swapped_load(path, *args, **kwargs):
        env = real_load(path, *args, **kwargs)
        with open(path, "ab") as handle:
            handle.write(b"\0")
        return env

    try:
        asset_manager.UnityPy.load = swapped_load
        textures = am.extract_textures(game_dir, os.path.join(root, "extract2"), log_callback=logs.append)
    finally:
        asset_manager.UnityPy.load = real_load

    check("extract: skips an asset that changed while being read", len(textures) == 0, len(textures))
    check("extract: logs the skip", any("[SKIP]" in line and "changed on disk" in line for line in logs),
          logs[-3:])


class _FakeRunning:
    """Stand-in for a worker thread that is still running."""

    def isRunning(self):
        return True


def test_ui_guards(root):
    cwd = os.getcwd()
    os.chdir(root)
    try:
        window = app_main.MainWindow()
        window.results = [{"original": {"name": "x", "save_path": ""}, "candidates": [], "match_file": None}]
        setattr(window, "worker_thread", _FakeRunning())   # stand-in for a running job
        window.update_buttons()

        check("ui: path fields are locked while a task runs",
              not window.orig_path.isEnabled() and not window.mod_path.isEnabled(),
              (window.orig_path.isEnabled(), window.mod_path.isEnabled()))
        check("ui: browse buttons are locked too",
              not window.btn_orig_browse.isEnabled() and not window.btn_mod_browse.isEnabled())

        temp_dir = os.path.join(root, "temp_guard")
        os.makedirs(temp_dir, exist_ok=True)
        with open(os.path.join(temp_dir, "metadata.json"), "w", encoding="utf-8") as handle:
            handle.write("{}")
        with open(os.path.join(temp_dir, "keep.png"), "wb") as handle:
            handle.write(b"x")

        window.check_and_clear_temp(temp_dir)
        check("ui: temp folder is not wiped while a task runs",
              os.path.exists(os.path.join(temp_dir, "keep.png")))

        window.orig_path.setText(root)  # emits textChanged -> on_path_changed
        check("ui: path change is deferred while running", window._pending_path_change is True,
              window._pending_path_change)
        check("ui: results are kept until the task finishes", len(window.results) == 1, len(window.results))

        # simulate the worker finishing (cleanup must then hit our test folder)
        window.temp_dir_orig = temp_dir
        window.temp_dir_mod = temp_dir
        setattr(window, "worker_thread", None)
        window.on_finished({})
        check("ui: deferred path change is applied on finish", window._pending_path_change is False)
        check("ui: temp folder is cleaned after the task",
              not os.path.exists(os.path.join(temp_dir, "keep.png")))
        window.close()
    finally:
        os.chdir(cwd)


def make_asset(folder, name="sharedassets1.assets"):
    """Copies one real Unity asset file into folder (the .resS stream is not needed:
    both guards fire on the asset file itself)."""
    source = os.path.join(PROJECT, "test", "ST3192340", "HentaiKanojo_Data", name)
    target = os.path.join(folder, name)
    if not os.path.exists(source):
        return None
    shutil.copy2(source, target)
    return target


def remove_tree(root):
    """Windows keeps a folder busy while a reader handle lingers, so retry briefly."""
    import gc
    for _ in range(10):
        gc.collect()
        shutil.rmtree(root, ignore_errors=True)
        if not os.path.exists(root):
            return
        time.sleep(0.2)


def main():
    # keep the temp root on the project drive so the 200+ MB .resS can be hardlinked
    root = tempfile.mkdtemp(prefix="utc_safety_", dir=os.path.dirname(PROJECT))
    try:
        print("== file signature ==")
        test_file_signature(root)
        print("== apply guard ==")
        test_apply_aborts_when_asset_changes(root)
        print("== extract guard ==")
        test_extract_skips_changed_file(root)
        print("== ui guards ==")
        test_ui_guards(root)
    finally:
        remove_tree(root)

    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print("FAILED:", FAILURES)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
