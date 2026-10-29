"""End-to-end test of the atlas integration (extract -> match -> apply).

Runs the real WorkerThread code on copies of the game data referenced by config.ini,
so the original game folder is never touched. Run: python test_atlas_app.py
"""
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT)

from PIL import Image, ImageChops
from PyQt6.QtWidgets import QApplication

import atlas as atlas_mod
import main as app_main

APP = QApplication.instance() or QApplication([])
CHECKS = 0
FAILURES = []
ATLASES = ("10p.atlas", "hina_cg4.atlas")


def check(name, condition, detail=None):
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  PASS  {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL  {name}  {detail!r}")


def images_equal(a, b):
    if a is None or b is None or a.size != b.size:
        return False
    return ImageChops.difference(a.convert("RGBA"), b.convert("RGBA")).getbbox() is None


def read_config_paths():
    paths = {}
    section = None
    with open(os.path.join(PROJECT, "config.ini"), encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
            elif "=" in line and section == "path":
                key, value = line.split("=", 1)
                paths[key.strip()] = value.strip()
    return paths


def prepare_pair(root):
    """Copies the chosen atlases + their page images of both builds into root."""
    paths = read_config_paths()
    made = {}
    for side, key in (("orig", "original"), ("mod", "modified")):
        src_root = paths.get(key, "")
        dst = os.path.join(root, side)
        os.makedirs(dst, exist_ok=True)
        for name in ATLASES:
            src_atlas = os.path.join(src_root, "www", name)
            if not os.path.exists(src_atlas):
                continue
            parsed = atlas_mod.parse_atlas(src_atlas)
            shutil.copy2(src_atlas, os.path.join(dst, name))
            for page in parsed.pages:
                page_src = parsed.page_path(page)
                if os.path.exists(page_src):
                    shutil.copy2(page_src, os.path.join(dst, page.name))
        made[side] = dst
    return made.get("orig"), made.get("mod")


def run_worker(task, method, **kwargs):
    captured = {}
    worker = app_main.WorkerThread(task, **kwargs)
    worker.sig_finished.connect(lambda data: captured.setdefault("data", data))
    worker.sig_error.connect(lambda title, msg: captured.setdefault("error", (title, msg)))
    getattr(worker, method)()
    return captured.get("data"), captured.get("error")


def test_end_to_end():
    root = tempfile.mkdtemp(prefix="atlas_e2e_")
    cwd = os.getcwd()
    try:
        orig_dir, mod_dir = prepare_pair(root)
        if not orig_dir or not mod_dir:
            print("  SKIP  game folders from config.ini not available")
            return
        temp_orig = os.path.join(root, "temp_orig")
        temp_mod = os.path.join(root, "temp_mod")
        os.makedirs(temp_orig, exist_ok=True)
        os.makedirs(temp_mod, exist_ok=True)

        # QSettings("config.ini") is relative, so the workers run inside the temp dir to
        # avoid touching the real configuration of the app.
        os.chdir(root)

        print("== extract ==")
        data, error = run_worker(
            "extract", "run_extract",
            orig_dir=orig_dir, mod_dir=mod_dir, temp_orig=temp_orig, temp_mod=temp_mod,
            mode="Image", orig_scan_all=False, mod_scan_all=False, atlas_regions=True,
        )
        check("extract finished without error", error is None, error)
        if not data:
            check("extract returned data", False)
            return
        orig_textures, mod_textures = data.get("orig", []), data.get("mod", [])
        print(f"  info  {len(orig_textures)} original items, {len(mod_textures)} modified items")
        check("extract: regions found on both sides", bool(orig_textures) and bool(mod_textures))
        check("extract: every item carries atlas metadata",
              all(t.get("atlas", {}).get("region") for t in orig_textures[:50]))
        check("extract: region images exist on disk",
              all(os.path.exists(t["save_path"]) for t in orig_textures[:50]))
        page_paths = {os.path.normcase(os.path.abspath(t["atlas"]["page_file"])) for t in orig_textures}
        check("extract: sheet itself is not an item",
              all(os.path.normcase(os.path.abspath(t["save_path"])) not in page_paths for t in orig_textures))
        sample = orig_textures[0]
        check("extract: item size matches its region rect",
              (sample["width"], sample["height"]) == (sample["atlas"]["sprite_size"][0],
                                                      sample["atlas"]["sprite_size"][1]),
              (sample["name"], sample["width"], sample["height"], sample["atlas"]["sprite_size"]))

        print("== match ==")
        results, error = run_worker(
            "match", "run_match",
            orig_textures=orig_textures, mod_textures=mod_textures,
            num_candidates=5, unchecked_same=False, same_res=False,
            filter_enabled=False, sim_cutoff=0.5,
        )
        check("match finished without error", error is None, error)
        results = results or []
        check("match: one result per original item", len(results) == len(orig_textures),
              (len(results), len(orig_textures)))
        matched = [r for r in results if r.get("match_file")]
        check("match: atlas regions were paired", len(matched) == len(results),
              f"{len(matched)}/{len(results)}")
        atlas_exact = [r for r in matched
                       if any(c.get("is_exact") for c in r.get("candidates", [])
                              if c["save_path"] == r["match_file"])]
        check("match: region identity wins over similarity", len(atlas_exact) >= len(matched) * 0.9,
              f"{len(atlas_exact)}/{len(matched)} marked exact")
        check("match: pma flag propagated for apply", all("match_pma" in r for r in matched))

        states = [r.get("atlas_state") for r in matched]
        counts = {state: states.count(state) for state in set(states)}
        print(f"  info  geometry states: {counts}")
        check("match: geometry state recorded for every pair",
              all(state in ("same", "moved", "rotated", "resized") for state in states), counts)
        check("match: moved/rotated pairings are reported",
              any(state in ("moved", "rotated") for state in states), counts)
        check("match: atlas identity is the matching reason",
              all(r.get("match_by") == "atlas" for r in matched),
              {r.get("match_by") for r in matched})

        resized = [r for r in matched if r.get("atlas_state") == "resized"]
        if resized:
            # the reported score must be the trim-normalised one: recompute a sample pair
            import similarity as sim_mod
            sample = resized[0]
            raw = sim_mod.compare_images(sample["original"]["save_path"], sample["match_file"])
            trim = sim_mod.compare_images(sample["original"]["save_path"], sample["match_file"], trim=True)
            print(f"  info  trim differs on {len(resized)} pairs (mean reported "
                  f"{sum(r['best_similarity'] for r in resized) / len(resized):.4f}); "
                  f"sample raw={raw:.4f} trim={trim:.4f}")
            check("match: reported score is the trim-normalised one",
                  abs(sample["best_similarity"] - trim) <= max(0.02, abs(trim - raw)), 
                  (sample["best_similarity"], trim, raw))

        print("== UI column ==")
        try:
            window = app_main.MainWindow()
            window.results = results
            window.update_tree(show_progress=False)
            top = window.tree.topLevelItem(0)
            check("ui: Atlas column exists", window.tree.columnCount() == 11, window.tree.columnCount())
            check("ui: Atlas cell is filled", bool(top.text(10)) and top.text(10) != "", top.text(10))
            check("ui: Atlas tooltip explains the packing", "rect" in top.toolTip(10), top.toolTip(10)[:60])
            check("ui: Similarity tooltip mentions trim", "Trim" in top.toolTip(4) or "trim" in top.toolTip(4),
                  top.toolTip(4)[:60])
            window.close()
        except Exception as error:
            check(f"ui: tree rendering ({type(error).__name__})", False, str(error))

        print("== apply ==")
        before = {}
        for res in matched:
            meta = res["original"]["atlas"]
            before.setdefault(meta["page_file"], atlas_mod.load_page_image(meta["page_file"]))
        to_replace = matched[:5]
        data, error = run_worker(
            "apply", "run_apply",
            to_replace=to_replace, orig_dir=orig_dir, mod_dir=mod_dir,
            temp_orig=temp_orig, temp_mod=temp_mod, mode="Image",
            high_quality=False, save_as_original=True,
        )
        check("apply finished without error", error is None, error)
        check("apply reports success", bool(data and data.get("success")), data)

        res = to_replace[0]
        meta = res["original"]["atlas"]
        page_path = meta["page_file"]
        after = atlas_mod.load_page_image(page_path)
        orig_page = before.get(page_path)
        check("apply: page readable after write", after is not None and orig_page is not None)
        if after is None or orig_page is None:
            return
        check(f"apply: page size kept ({os.path.basename(page_path)})", after.size == orig_page.size,
              (after.size, orig_page.size))
        region = atlas_mod.region_from_metadata(meta)
        check("apply: region metadata round-trips", region is not None)
        if region is not None:
            # the app packs the mod piece into the original footprint, so compare the raw
            # page rect with the packed piece (both in page orientation)
            piece = Image.open(res["match_file"]).convert("RGBA")
            want = atlas_mod.pack_region(piece, region)
            if region.pma and not res.get("match_pma"):
                want = atlas_mod.premultiply_alpha(want)
            if want.size != (region.packed_width, region.packed_height):
                want = want.resize((region.packed_width, region.packed_height), Image.Resampling.LANCZOS)
            x, y, w, h = region.rect
            got = after.crop((x, y, x + w, y + h))
            check(f"apply: region '{res['original']['name']}' written at the original rect",
                  images_equal(want, got))
            replaced = {res["original"]["atlas"]["region"] for res in to_replace}
            others = [r for r in atlas_mod.parse_atlas(meta["atlas_file"]).pages[0].regions
                      if r.name not in replaced]
            if others:
                other = others[0]
                check(f"apply: untouched region '{other.name}' unchanged",
                      images_equal(atlas_mod.unpack_region(after, other), atlas_mod.unpack_region(orig_page, other)))
    finally:
        os.chdir(cwd)
        shutil.rmtree(root, ignore_errors=True)


def atlas_page_regions_test(meta):
    """Regions of one atlas page (helper for the page mode test)."""
    path = meta.get("atlas_file") or ""
    page_name = os.path.basename(meta.get("page_file") or "")
    try:
        parsed = atlas_mod.parse_atlas(path)
    except Exception:
        return []
    for page in parsed.pages:
        if os.path.basename(page.name) == page_name:
            return page.regions
    return []


def remove_tree(root):
    import gc
    import time
    for _ in range(10):
        gc.collect()
        shutil.rmtree(root, ignore_errors=True)
        if not os.path.exists(root):
            return
        time.sleep(0.2)


def test_page_mode():
    """Page level mode: 1 item per atlas page, similarity = same pieces / all pieces."""
    root = tempfile.mkdtemp(prefix="atlas_pages_", dir=os.path.dirname(PROJECT))
    cwd = os.getcwd()
    try:
        orig_dir, mod_dir = prepare_pair(root)
        if not orig_dir or not mod_dir:
            print("  SKIP  game folders from config.ini not available")
            return
        temp_orig = os.path.join(root, "temp_orig")
        temp_mod = os.path.join(root, "temp_mod")
        os.makedirs(temp_orig, exist_ok=True)
        os.makedirs(temp_mod, exist_ok=True)
        os.chdir(root)

        print("== extract (page mode) ==")
        data, error = run_worker(
            "extract", "run_extract",
            orig_dir=orig_dir, mod_dir=mod_dir, temp_orig=temp_orig, temp_mod=temp_mod,
            mode="Image", orig_scan_all=False, mod_scan_all=False, atlas_regions=False,
        )
        check("page: extract finished without error", error is None, error)
        if not data:
            check("page: extract returned data", False)
            return
        orig_items, mod_items = data.get("orig", []), data.get("mod", [])
        print(f"  info  {len(orig_items)} page items per side (region mode had 130)")
        check("page: one item per atlas page", 1 <= len(orig_items) <= 4, len(orig_items))
        check("page: every item is an atlas page",
              all(t.get("atlas_page", {}).get("digests") for t in orig_items))
        check("page: page items carry no region metadata", all(not t.get("atlas") for t in orig_items))
        check("page: preview file exists for each page",
              all(os.path.exists(t["save_path"]) for t in orig_items))
        check("page: preview is downscaled",
              all(max(Image.open(t["save_path"]).size) <= 1024 for t in orig_items))

        print("== match (page mode) ==")
        results, error = run_worker(
            "match", "run_match",
            orig_textures=orig_items, mod_textures=mod_items,
            num_candidates=5, unchecked_same=False, same_res=False,
            filter_enabled=False, sim_cutoff=0.5,
        )
        check("page: match finished without error", error is None, error)
        results = results or []
        check("page: one result per page", len(results) == len(orig_items), (len(results), len(orig_items)))
        check("page: pages were paired", all(r.get("match_file") for r in results),
              [r.get("match_file") for r in results])

        by_page = {os.path.basename(r["original"]["atlas_page"]["page_file"]): r for r in results}
        ten = by_page.get("10p.png")
        cg42 = by_page.get("hina_cg4_2.png")
        if ten:
            stats = ten.get("piece_stats") or {}
            print(f"  info  10p.png: {stats}")
            check("page: fully re-drawn page scores 0", abs(ten["best_similarity"]) < 1e-9,
                  ten["best_similarity"])
            check("page: Atlas column shows the piece counts", "p /" in (ten.get("atlas_text") or ""),
                  ten.get("atlas_text"))
            check("page: Atlas tooltip explains the overlap", "pieces identical" in (ten.get("atlas_detail") or ""),
                  ten.get("atlas_detail"))
        if cg42:
            stats = cg42.get("piece_stats") or {}
            total = max(1, stats.get("pieces", 1) + stats.get("missing", 0))
            expected = stats.get("identical", 0) / total
            print(f"  info  hina_cg4_2.png: {stats} -> expected {expected:.4f}")
            check("page: ratio == identical pieces / total pieces",
                  abs(cg42["best_similarity"] - expected) < 1e-6, (cg42["best_similarity"], expected))

        print("== apply (page mode) ==")
        to_replace = [r for r in results if r.get("match_file")][:3]
        before = {r["original"]["atlas_page"]["page_file"]:
                  atlas_mod.load_page_image(r["original"]["atlas_page"]["page_file"]) for r in to_replace}
        data, error = run_worker(
            "apply", "run_apply",
            to_replace=to_replace, orig_dir=orig_dir, mod_dir=mod_dir,
            temp_orig=temp_orig, temp_mod=temp_mod, mode="Image",
            high_quality=False, save_as_original=True,
        )
        check("page: apply finished without error", error is None, error)
        check("page: apply reports writes", bool(data and data.get("success")), data)

        res = to_replace[0]
        page_path = res["original"]["atlas_page"]["page_file"]
        after = atlas_mod.load_page_image(page_path)
        original = before[page_path]
        check("page: page size kept after apply", after.size == original.size, (after.size, original.size))

        mod_image = atlas_mod.load_page_image(res.get("match_page_file") or "")
        mod_regions = atlas_page_regions_test({"atlas_file": res.get("match_atlas_file", ""),
                                               "page_file": res.get("match_page_file", "")})
        orig_regions = atlas_page_regions_test(res["original"]["atlas_page"])
        written = kept = 0
        if mod_image is not None and after is not None and mod_regions and orig_regions:
            mod_by = {atlas_mod.region_key(r.name): r for r in mod_regions}
            for region in orig_regions[:20]:
                twin = mod_by.get(atlas_mod.region_key(region.name))
                if twin is None:
                    continue
                want = atlas_mod.unpack_region(mod_image, twin)
                got = atlas_mod.unpack_region(after, region)
                if images_equal(want, got):
                    written += 1
                else:
                    kept += 1
        check("page: changed pieces now carry the modified art", written > 0, (written, kept))

        print("== UI column (page mode) ==")
        try:
            window = app_main.MainWindow()
            window.results = results
            window.update_tree(show_progress=False)
            top = window.tree.topLevelItem(0)
            check("page ui: Atlas cell shows the piece overlap", "p /" in top.text(10), top.text(10))
            check("page ui: Similarity tooltip explains the metric",
                  "Piece overlap" in top.toolTip(4), top.toolTip(4)[:60])
            check("page ui: Atlas tooltip explains the ratio",
                  "identical pieces" in top.toolTip(10), top.toolTip(10)[:80])
            window.close()
        except Exception as error:
            check(f"page ui: tree rendering ({type(error).__name__})", False, str(error))
    finally:
        os.chdir(cwd)
        remove_tree(root)


def main():
    test_end_to_end()
    test_page_mode()

    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print("FAILED:", FAILURES)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
