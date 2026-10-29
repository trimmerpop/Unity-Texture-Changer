"""Functional tests for SUPPORTED_IMAGE_EXTS + 'Save as original format'."""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import sys, tempfile, shutil, traceback
import numpy as np
from PIL import Image

PROJECT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT)

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QSettings
from asset_manager import SUPPORTED_IMAGE_EXTS, IMAGE_SAVE_FORMATS, convert_image_format
from similarity import load_image
import main as app_main

APPLICATION = QApplication([])

FAILURES = []
CHECKS = 0


def check(name, condition, detail=""):
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


MAGIC = {
    ".png": b"\x89PNG\r\n\x1a\n", ".apng": b"\x89PNG\r\n\x1a\n",
    ".jpg": b"\xff\xd8", ".jpeg": b"\xff\xd8", ".jpe": b"\xff\xd8", ".jfif": b"\xff\xd8",
    ".bmp": b"BM", ".dib": b"BM", ".gif": b"GIF8",
    ".tif": b"II*\x00", ".tiff": b"II*\x00",
    ".dds": b"DDS ", ".qoi": b"qoif",
    ".ppm": b"P6", ".pgm": b"P6", ".pbm": b"P6", ".pnm": b"P6",
}


def make_rgba_source(path, color=(255, 0, 0, 255), size=(16, 16)):
    arr = np.zeros((size[1], size[0], 4), np.uint8)
    arr[..., 0], arr[..., 1], arr[..., 2], arr[..., 3] = color
    arr[0, 0] = (0, 0, 0, 0)  # transparent corner for alpha checks
    Image.fromarray(arr, "RGBA").save(path)
    return path


def test_constants():
    print("\n[1] Constants")
    check("webp/tga/dds/avif are supported",
          all(e in SUPPORTED_IMAGE_EXTS for e in (".webp", ".tga", ".dds", ".avif")))
    for bad in (".hdr", ".pfm", ".exr", ".psd", ".pcx", ".pdf", ".sgi", ".im", ".xbm"):
        check(f"{bad} excluded", bad not in SUPPORTED_IMAGE_EXTS)
    check("IMAGE_SAVE_FORMATS covers every extension",
          set(SUPPORTED_IMAGE_EXTS) == set(IMAGE_SAVE_FORMATS.keys()),
          str(set(SUPPORTED_IMAGE_EXTS) ^ set(IMAGE_SAVE_FORMATS.keys())))
    src = os.path.join(tempfile.mkdtemp(), "src.png")
    make_rgba_source(src)
    dst_dir = tempfile.mkdtemp()
    for ext in SUPPORTED_IMAGE_EXTS:
        dst = os.path.join(dst_dir, "out" + ext)
        if not convert_image_format(src, dst):
            check(f"convert png -> {ext}", False, "convert_image_format returned False")
            continue
        loaded = load_image(dst)
        with open(dst, "rb") as f:
            head = f.read(16)
        magic_ok = True
        if ext in MAGIC:
            magic_ok = head.startswith(MAGIC[ext])
        elif ext == ".tga":
            with open(dst, "rb") as f:
                f.seek(-18, os.SEEK_END)
                magic_ok = f.read(18).startswith(b"TRUEVISION-XFILE")
        elif ext == ".webp":
            magic_ok = head.startswith(b"RIFF") and head[8:12] == b"WEBP"
        elif ext in (".jp2", ".j2k", ".j2c", ".jpc"):
            magic_ok = head[:4] in (b"\x00\x00\x00\x0c", b"\x00\x00\x00\x20", b"\xff\x4f\xff\x51")
        elif ext == ".avif":
            magic_ok = head[4:8] == b"ftyp"
        check(f"convert png -> {ext} (magic + load)",
              magic_ok and loaded is not None and loaded.shape == (16, 16, 4),
              f"magic_ok={magic_ok} loaded={None if loaded is None else loaded.shape}")
        if loaded is None:
            continue
        alpha = int(loaded[0, 0, 3])
        if ext in (".png", ".webp", ".tga", ".tiff", ".avif"):
            check(f"{ext} keeps alpha channel", alpha < 10, f"corner alpha={alpha}")
        if ext in (".jpg", ".bmp", ".ppm", ".dds"):
            check(f"{ext} flattened to opaque RGB", alpha == 255, f"corner alpha={alpha}")
    keep = os.path.join(dst_dir, "keep.bin")
    with open(keep, "wb") as f:
        f.write(b"original")
    rejected = convert_image_format(src, keep) is False and open(keep, "rb").read() == b"original"
    check("unsupported dst ext rejected without touching the file", rejected)


def build_result(orig_path, match_path, unity=False):
    orig = {
        "name": "a", "save_path": orig_path,
        "size": os.path.getsize(orig_path), "width": 16, "height": 16,
    }
    if unity:
        orig["source_asset"] = r"C:\fake\sharedassets0.assets"
        orig["path_id"] = 12345
    return {
        "original": orig,
        "match_file": match_path,
        "best_similarity": 0.9,
        "candidates": [{"save_path": match_path, "name": "a", "width": 16, "height": 16,
                        "size": os.path.getsize(match_path), "similarity": 0.9,
                        "is_exact": True, "is_same_res": True}],
        "replace": True,
    }


def run_worker(task_type, **kwargs):
    logs, result = [], {}
    worker = app_main.WorkerThread(task_type, **kwargs)
    worker.sig_log.connect(lambda m: logs.append(m))
    worker.sig_finished.connect(lambda d: result.setdefault("data", d))
    if task_type == "apply":
        worker.run_apply()
    elif task_type == "copy":
        worker.run_copy()
    elif task_type == "match":
        worker.run_match()
    return logs, result.get("data")


def setup_case(tmp_root):
    orig_dir = os.path.join(tmp_root, "orig")
    mod_dir = os.path.join(tmp_root, "mod")
    os.makedirs(orig_dir, exist_ok=True)
    os.makedirs(mod_dir, exist_ok=True)
    orig_path = os.path.join(orig_dir, "a.png")
    make_rgba_source(orig_path, color=(0, 0, 255, 255))
    match_path = os.path.join(mod_dir, "a.webp")
    arr = np.zeros((16, 16, 4), np.uint8)
    arr[..., 0], arr[..., 3] = 255, 255
    Image.fromarray(arr, "RGBA").save(match_path, lossless=True)
    return orig_dir, mod_dir, orig_path, match_path


def test_apply(tmp_root):
    print("\n[2] Apply (direct files)")
    orig_dir, mod_dir, orig_path, match_path = setup_case(os.path.join(tmp_root, "a1"))
    logs, summary = run_worker("apply", to_replace=[build_result(orig_path, match_path)],
                               orig_dir=orig_dir, mod_dir=mod_dir,
                               temp_orig=os.path.join(tmp_root, "t1"), temp_mod=os.path.join(tmp_root, "t2"),
                               mode="Image", high_quality=False, save_as_original=True)
    head = open(orig_path, "rb").read(8)
    pix = load_image(orig_path)[8, 8]
    check("save_as_original=True writes PNG bytes", head.startswith(b"\x89PNG"), str(head))
    check("save_as_original=True writes the matched pixels",
          int(pix[2]) > 200 and int(pix[0]) < 60, f"pixel={pix}")
    check("apply summary reports 1 success", bool(summary) and summary.get("success") == 1, str(summary))

    orig_dir2, mod_dir2, orig_path2, match_path2 = setup_case(os.path.join(tmp_root, "a2"))
    run_worker("apply", to_replace=[build_result(orig_path2, match_path2)],
               orig_dir=orig_dir2, mod_dir=mod_dir2,
               temp_orig=os.path.join(tmp_root, "t3"), temp_mod=os.path.join(tmp_root, "t4"),
               mode="Image", high_quality=False, save_as_original=False)
    check("save_as_original=False keeps raw webp bytes", open(orig_path2, "rb").read(4) == b"RIFF")

    orig_dir3, mod_dir3, orig_path3, match_path3 = setup_case(os.path.join(tmp_root, "a3"))
    logs3, summary3 = run_worker("apply", to_replace=[build_result(orig_path3, match_path3)],
                                 orig_dir=orig_dir3, mod_dir=mod_dir3,
                                 temp_orig=os.path.join(tmp_root, "t5"), temp_mod=os.path.join(tmp_root, "t6"),
                                 mode="Unity", high_quality=True, save_as_original=True)
    joined = "\n".join(logs3)
    check("BC7 exception is announced", "High Quality (BC7) is enabled" in joined, joined[-300:])
    check("BC7 exception: direct file still converted to PNG",
          open(orig_path3, "rb").read(4) == b"\x89PNG")
    check("BC7 run still applies 1 file", bool(summary3) and summary3.get("success") == 1, str(summary3))

    # conversion failure must leave the destination untouched (no wrong-format bytes)
    orig_dir4, mod_dir4, orig_path4, match_path4 = setup_case(os.path.join(tmp_root, "a4"))
    before = open(orig_path4, "rb").read()
    original_convert = app_main.convert_image_format
    app_main.convert_image_format = lambda s, d: False
    try:
        logs4, summary4 = run_worker("apply", to_replace=[build_result(orig_path4, match_path4)],
                                     orig_dir=orig_dir4, mod_dir=mod_dir4,
                                     temp_orig=os.path.join(tmp_root, "t7"), temp_mod=os.path.join(tmp_root, "t8"),
                                     mode="Image", high_quality=False, save_as_original=True)
    finally:
        app_main.convert_image_format = original_convert
    check("failed conversion leaves the destination untouched", open(orig_path4, "rb").read() == before)
    check("failed conversion is not reported as success",
          bool(summary4) and summary4.get("success") == 0, str(summary4))
    check("failed conversion is logged", any("Could not convert" in l for l in logs4))


def test_copy(tmp_root):
    print("\n[3] Copy to...")
    orig_dir, mod_dir, orig_path, match_path = setup_case(os.path.join(tmp_root, "c1"))
    target = os.path.join(tmp_root, "c1_out")
    os.makedirs(target, exist_ok=True)
    logs, summary = run_worker("copy", to_replace=[build_result(orig_path, match_path)],
                               target_dir=target, use_uabea_format=False, save_as_original=True)
    png_out = os.path.join(target, "a.png")
    check("copy exports original extension (.png)", os.path.exists(png_out), str(os.listdir(target)))
    if os.path.exists(png_out):
        check("copied file is a real PNG", open(png_out, "rb").read(4) == b"\x89PNG")
    check("copy summary ok", bool(summary) and summary.get("success") == 1, str(summary))

    target2 = os.path.join(tmp_root, "c2_out")
    os.makedirs(target2, exist_ok=True)
    run_worker("copy", to_replace=[build_result(orig_path, match_path)],
               target_dir=target2, use_uabea_format=False, save_as_original=False)
    check("copy keeps matched extension when option OFF",
          os.path.exists(os.path.join(target2, "a.webp")), str(os.listdir(target2)))


def test_match_e2e(tmp_root):
    print("\n[4] Matching with mixed formats")
    work = os.path.join(tmp_root, "m1")
    os.makedirs(work, exist_ok=True)
    grad = np.tile(np.linspace(0, 255, 32), (32, 1)).astype(np.uint8)
    rgba = np.dstack([grad, grad, grad, np.full_like(grad, 255)])
    orig_png = os.path.join(work, "tex.png")
    Image.fromarray(rgba, "RGBA").save(orig_png)
    mod_webp = os.path.join(work, "tex.webp")
    Image.fromarray(rgba, "RGBA").save(mod_webp, lossless=True)
    mod_tga = os.path.join(work, "tex2.tga")
    Image.fromarray(rgba, "RGBA").save(mod_tga)

    orig_tex = [{"name": "tex", "save_path": orig_png, "width": 32, "height": 32,
                 "size": os.path.getsize(orig_png)}]
    mod_tex = [
        {"name": "tex", "save_path": mod_webp, "width": 32, "height": 32, "size": os.path.getsize(mod_webp)},
        {"name": "tex2", "save_path": mod_tga, "width": 32, "height": 32, "size": os.path.getsize(mod_tga)},
    ]
    logs, results = run_worker("match", orig_textures=orig_tex, mod_textures=mod_tex,
                               num_candidates=5, unchecked_same=False, same_res=False,
                               filter_enabled=False, sim_cutoff=0.5)
    check("match returns one result per original",
          isinstance(results, list) and len(results) == 1, str(results)[:200])
    if isinstance(results, list) and results:
        r = results[0]
        check("webp candidate matched with similarity 1.0",
              (r.get("match_file") or "").lower().endswith(".webp") and r.get("best_similarity", 0) >= 0.999,
              f"{r.get('match_file')} sim={r.get('best_similarity')}")
        check("tga appears among candidates",
              any(c["save_path"].lower().endswith(".tga") for c in r.get("candidates", [])))


def test_main_window(tmp_root):
    print("\n[5] UI: checkbox + settings + scanning")
    scan_dir = os.path.join(tmp_root, "scan")
    os.makedirs(scan_dir, exist_ok=True)
    make_rgba_source(os.path.join(scan_dir, "p.webp"))
    make_rgba_source(os.path.join(scan_dir, "q.tga"))
    make_rgba_source(os.path.join(scan_dir, "r.png"))
    with open(os.path.join(scan_dir, "notes.txt"), "w") as f:
        f.write("not an image")

    cwd = os.getcwd()
    os.chdir(tmp_root)  # keep config.ini out of the project folder
    try:
        window = app_main.MainWindow()
        try:
            check("checkbox exists and defaults to True", window.chk_save_original.isChecked())
            check("label text", window.chk_save_original.text() == "Save as original format",
                  window.chk_save_original.text())
            check("has_images finds webp/tga folder", window.has_images(scan_dir))
            found = window.scan_image_folder(scan_dir)
            names = sorted(os.path.basename(t["save_path"]) for t in found)
            check("scan_image_folder picks up webp/tga/png", names == ["p.webp", "q.tga", "r.png"], str(names))
            window.chk_save_original.setChecked(False)
            window.save_settings()
            settings = QSettings("config.ini", QSettings.Format.IniFormat)
            check("setting persisted to config.ini",
                  str(settings.value("options/save_as_original")).lower() == "false",
                  str(settings.value("options/save_as_original")))
        finally:
            window.close()
        window2 = app_main.MainWindow()
        try:
            check("setting restored on next start", window2.chk_save_original.isChecked() is False)
        finally:
            window2.close()
    finally:
        os.chdir(cwd)


def main():
    tmp_root = tempfile.mkdtemp(prefix="utc_tests_")
    print(f"work dir: {tmp_root}")
    try:
        test_constants()
        test_apply(tmp_root)
        test_copy(tmp_root)
        test_match_e2e(tmp_root)
        test_main_window(tmp_root)
    except Exception:
        traceback.print_exc()
        FAILURES.append("unexpected exception")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
    passed = CHECKS - len(FAILURES)
    print(f"\n=== {passed}/{CHECKS} checks passed ===")
    if FAILURES:
        print("FAILED: " + ", ".join(FAILURES))
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
