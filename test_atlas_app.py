"""End-to-end test of the atlas integration (extract -> match -> apply).

Runs the real WorkerThread code on copies of the game data referenced by config.ini,
so the original game folder is never touched. Run: python test_atlas_app.py
"""
import json
import os
import shutil
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT)

from PIL import Image, ImageChops
from PyQt6.QtCore import Qt, QSettings
from PyQt6.QtWidgets import QApplication

import atlas as atlas_mod
import main as app_main

APP = QApplication.instance() or QApplication([])
CHECKS = 0
FAILURES = []
ATLASES = ("10p.atlas", "hina_cg1.atlas", "hina_cg4.atlas")


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


def load_piece(path):
    with Image.open(path) as img:
        return img.convert("RGBA")


def test_legacy_region_apply():
    """Old metadata (one item per region) must still apply into the original page.

    The per-region extraction mode was removed from the UI, but metadata.json files created
    by older versions still describe region items, so the apply path is kept and tested.
    """
    root = tempfile.mkdtemp(prefix="atlas_legacy_", dir=os.path.dirname(PROJECT))
    cwd = os.getcwd()
    try:
        orig_dir, mod_dir = prepare_pair(root)
        if not orig_dir or not mod_dir:
            print("  SKIP  game folders from config.ini not available")
            return
        os.chdir(root)

        atlas_name = "10p.atlas"
        items = atlas_mod.extract_atlas_regions(os.path.join(orig_dir, atlas_name),
                                               os.path.join(root, "regions"))
        check("legacy: region items extracted", len(items) > 10, len(items))
        check("legacy: items carry region metadata",
              all(t.get("atlas", {}).get("region") for t in items))
        check("legacy: region images exist", all(os.path.exists(t["save_path"]) for t in items[:20]))

        mod_items = atlas_mod.extract_atlas_regions(os.path.join(mod_dir, atlas_name),
                                                    os.path.join(root, "regions_mod"))
        by_name = {t["name"]: t for t in mod_items}
        result = None
        for item in items:
            twin = by_name.get(item["name"])
            if twin is None:
                continue
            if atlas_mod.piece_digest(load_piece(item["save_path"])) != \
               atlas_mod.piece_digest(load_piece(twin["save_path"])):
                result = {"original": item, "match_file": twin["save_path"], "match_pma": True,
                          "match_by": "atlas", "replace": True, "best_similarity": 0.5}
                break
        check("legacy: found a changed piece to apply", result is not None)
        if result is None:
            return

        page_path = result["original"]["atlas"]["page_file"]
        before = atlas_mod.load_page_image(page_path)
        data, error = run_worker(
            "apply", "run_apply",
            to_replace=[result], orig_dir=orig_dir, mod_dir=mod_dir,
            temp_orig=os.path.join(root, "t1"), temp_mod=os.path.join(root, "t2"),
            mode="Image", high_quality=False, save_as_original=True,
        )
        check("legacy: apply finished without error", error is None, error)
        check("legacy: apply reports success", bool(data and data.get("success")), data)

        after = atlas_mod.load_page_image(page_path)
        check("legacy: page size kept", after is not None and before is not None and after.size == before.size)
        region = atlas_mod.region_from_metadata(result["original"]["atlas"])
        if region is not None and after is not None:
            piece = load_piece(result["match_file"])
            want = atlas_mod.pack_region(piece, region)
            if region.pma and not result.get("match_pma"):
                want = atlas_mod.premultiply_alpha(want)
            if want.size != (region.packed_width, region.packed_height):
                want = want.resize((region.packed_width, region.packed_height), Image.Resampling.LANCZOS)
            x, y, w, h = region.rect
            got = after.crop((x, y, x + w, y + h))
            check("legacy: piece written at the original rect", images_equal(want, got))
    finally:
        os.chdir(cwd)
        remove_tree(root)


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


def make_small_atlas(root, side, colors):
    """Builds a tiny atlas page (2 pieces) so the assembly can be checked per pixel."""
    folder = os.path.join(root, side)
    os.makedirs(folder, exist_ok=True)
    page = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
    piece_a = Image.new("RGBA", (40, 40), colors["a"])
    piece_b = Image.new("RGBA", (60, 20), colors["b"])
    page.paste(piece_a, (0, 0))
    page.paste(piece_b, (40, 0))
    page_path = os.path.join(folder, "small.png")
    page.save(page_path, format="PNG")
    atlas_path = os.path.join(folder, "small.atlas")
    with open(atlas_path, "w", encoding="utf-8") as handle:
        handle.write("small.png\n"
                     "size:200,100\n"
                     "filter:Nearest,Nearest\n"
                     "pma:false\n"
                     "scale:1\n"
                     "piece_a\n"
                     "bounds:0,0,40,40\n"
                     "piece_b\n"
                     "bounds:40,0,60,20\n")
    parsed = atlas_mod.parse_atlas(atlas_path)
    page_image = atlas_mod.load_page_image(page_path)
    digests = {}
    if page_image is not None:
        digests = atlas_mod.page_piece_digests(page_image, parsed.pages[0].regions)
    meta = {
        "kind": "atlas-page",
        "atlas_file": atlas_path,
        "page_file": page_path,
        "pieces": len(digests),
        "pma": False,
        "digests": digests,
    }
    return meta


def test_assembled_preview_unit():
    """The assembled preview = original page with only the changed pieces replaced."""
    root = tempfile.mkdtemp(prefix="atlas_asm_", dir=os.path.dirname(PROJECT))
    try:
        red, blue, green = (220, 30, 30, 255), (30, 30, 220, 255), (30, 200, 30, 255)
        orig_meta = make_small_atlas(root, "orig", {"a": red, "b": green})
        mod_meta = make_small_atlas(root, "mod", {"a": blue, "b": green})

        out_dir = os.path.join(root, "assembled")
        before = atlas_mod.load_page_image(orig_meta["page_file"])
        assembled_path = app_main.assemble_atlas_preview(orig_meta, mod_meta, out_dir)
        check("assembly: preview file written", bool(assembled_path) and os.path.exists(assembled_path),
              assembled_path)
        if not assembled_path:
            return
        assembled = atlas_mod.load_page_image(assembled_path)
        check("assembly: page size kept",
              assembled is not None and before is not None and assembled.size == before.size,
              None if assembled is None else assembled.size)
        if assembled is None:
            return

        orig_regions = app_main.atlas_page_regions(orig_meta)
        mod_regions = app_main.atlas_page_regions(mod_meta)
        mod_by = dict(zip(atlas_mod.piece_keys(mod_regions), mod_regions))
        by_key = dict(zip(atlas_mod.piece_keys(orig_regions), orig_regions))
        mod_page = atlas_mod.load_page_image(mod_meta["page_file"])
        changed_piece = atlas_mod.unpack_region(assembled, by_key["piece_a"])
        same_piece = atlas_mod.unpack_region(assembled, by_key["piece_b"])
        check("assembly: changed piece carries the mod art",
              mod_page is not None
              and images_equal(changed_piece, atlas_mod.unpack_region(mod_page, mod_by["piece_a"])))
        check("assembly: unchanged piece keeps the original pixels",
              images_equal(same_piece, Image.new("RGBA", (60, 20), green)))
        check("assembly: cache is reused while nothing changed",
              app_main.assemble_atlas_preview(orig_meta, mod_meta, out_dir) == assembled_path)

        # a page where nothing changed shows the original preview (no diff noise)
        same_meta = make_small_atlas(root, "same", {"a": red, "b": green})
        same_meta["page_file"] = os.path.join(root, "same", "small.png")
        preview = os.path.join(root, "same", "small_preview.png")
        Image.open(same_meta["page_file"]).resize((100, 50)).save(preview, format="PNG")
        unmoved = app_main.assemble_atlas_preview(orig_meta, same_meta, out_dir,
                                                 orig_preview_path=preview)
        check("assembly: an unchanged page reuses the original preview",
              bool(unmoved) and images_equal(atlas_mod.load_page_image(unmoved),
                                             atlas_mod.load_page_image(preview)), unmoved)
    finally:
        remove_tree(root)


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
        # a plain image in the modified folder must still be offered as a candidate
        Image.new("RGBA", (256, 256), (40, 90, 160, 255)).save(
            os.path.join(mod_dir, "plain_candidate.png"), format="PNG")
        os.chdir(root)

        print("== extract (page mode) ==")
        data, error = run_worker(
            "extract", "run_extract",
            orig_dir=orig_dir, mod_dir=mod_dir, temp_orig=temp_orig, temp_mod=temp_mod,
            mode="Image", orig_scan_all=False, mod_scan_all=False,
        )
        check("page: extract finished without error", error is None, error)
        if not data:
            check("page: extract returned data", False)
            return
        orig_items, mod_items = data.get("orig", []), data.get("mod", [])
        # every page image is its own item: hina_cg1 -> hina_cg1, hina_cg1_2, hina_cg1_3
        expected_pages = []
        for atlas_name in ATLASES:
            expected_pages += [page.name for page in
                               atlas_mod.parse_atlas(os.path.join(orig_dir, atlas_name)).pages]
        item_pages = [os.path.basename(t.get("atlas_page", {}).get("page_file", "")) for t in orig_items]
        print(f"  info  {len(orig_items)} page items for {len(expected_pages)} page files")
        check("page: one item per atlas page file", len(orig_items) == len(expected_pages),
              (len(orig_items), expected_pages))
        check("page: every page file of a multi-page atlas is listed",
              sorted(item_pages) == sorted(expected_pages), (item_pages, expected_pages))
        check("page: a second page is its own row (hina_cg1_2)",
              any(name.startswith("hina_cg1_2") for name in item_pages), item_pages)
        check("page: rows are named after the page stem",
              all(t["name"] == os.path.splitext(os.path.basename(t["atlas_page"]["page_file"]))[0]
                  for t in orig_items), [t["name"] for t in orig_items])
        check("page: every item is an atlas page",
              all(t.get("atlas_page", {}).get("digests") for t in orig_items))
        check("page: page items carry no region metadata", all(not t.get("atlas") for t in orig_items))
        check("page: page position is recorded for the tooltip",
              all(t["atlas_page"].get("page_number") and t["atlas_page"].get("page_count")
                  for t in orig_items),
              [(t["name"], t["atlas_page"].get("page_number")) for t in orig_items[:3]])
        check("page: page count matches the .atlas file",
              all(t["atlas_page"]["page_count"] == len(atlas_mod.parse_atlas(
                  t["atlas_page"]["atlas_file"]).pages) for t in orig_items))
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
        plain_candidates = [c for r in results for c in (r.get("candidates") or [])
                            if c.get("name", "").startswith("plain_candidate")]
        check("page: plain images are still offered as candidates", bool(plain_candidates),
              [c.get("name") for r in results for c in (r.get("candidates") or [])][:6])
        check("page: plain candidates carry no atlas ratio",
              all(c.get("atlas_ratio") is None for c in plain_candidates),
              [c.get("atlas_ratio") for c in plain_candidates][:3])

        by_page = {os.path.basename(r["original"]["atlas_page"]["page_file"]): r for r in results}
        ten = by_page.get("10p.png")
        cg42 = by_page.get("hina_cg4_2.png")
        cg12 = by_page.get("hina_cg1_2.png")
        cg13 = by_page.get("hina_cg1_3.png")
        check("page: every page of hina_cg1 got its own result",
              bool(cg12) and bool(cg13), sorted(by_page))
        if cg12:
            # the page really differs between the builds, so the user must be able to see it
            print(f"  info  hina_cg1_2.png: {cg12.get('piece_stats')}")
            check("page: hina_cg1_2 is matched and reports its own overlap",
                  bool(cg12.get("match_file")) and "% (" in (cg12.get("atlas_text") or ""),
                  (cg12.get("match_file"), cg12.get("atlas_text")))
            check("page: hina_cg1_2 keeps its own piece statistics",
                  (cg12.get("piece_stats") or {}).get("pieces", 0) > 0,
                  cg12.get("piece_stats"))
        if ten:
            stats = ten.get("piece_stats") or {}
            print(f"  info  10p.png: {stats}")
            check("page: identity page is paired", ten.get("match_by") == "atlas", ten.get("match_by"))
            check("page: mostly unchanged page scores high", (ten.get("atlas_ratio") or 0) >= 0.85,
                  ten.get("atlas_ratio"))
            check("page: Atlas column shows percent and (same/total)",
                  "% (" in (ten.get("atlas_text") or ""), ten.get("atlas_text"))
            check("page: Atlas tooltip explains the overlap", "same pieces" in (ten.get("atlas_detail") or ""),
                  ten.get("atlas_detail"))
            check("page: similarity merges atlas and plain scores",
                  ten["best_similarity"] >= (ten.get("atlas_ratio") or 0) - 1e-9, ten["best_similarity"])
        if cg42:
            stats = cg42.get("piece_stats") or {}
            total = max(1, stats.get("pieces", 1) + stats.get("missing", 0))
            expected = stats.get("same", 0) / total
            print(f"  info  hina_cg4_2.png: {stats} -> expected {expected:.4f}")
            check("page: ratio == same pieces / total pieces",
                  abs((cg42.get("atlas_ratio") or 0) - expected) < 1e-6, (cg42.get("atlas_ratio"), expected))

        print("== highlight difference: assembled atlas preview ==")
        # hina_cg1_2 really differs, so the preview must show the mod art inside the ORIGINAL
        # layout instead of the re-packed mod sheet (that is what Apply writes). Pick a page
        # that was also re-packed, otherwise both comparisons look the same.
        def repacked(result):
            stats = result.get("piece_stats") or {}
            return stats.get("changed", 0) and (stats.get("moved", 0) or stats.get("rotated", 0))

        target = next((r for r in results if repacked(r)), cg12 or results[0])
        print(f"  info  assembled preview target: {target['original']['name']}")
        mod_match = next((t for t in mod_items
                          if t["save_path"] == target.get("match_file")), None)
        if mod_match is None:
            check("highlight: matched mod page found", False, target.get("match_file"))
        else:
            window = app_main.MainWindow()
            window.temp_dir_orig = temp_orig
            window.temp_dir_mod = temp_mod
            window.diff_check.setChecked(True)
            window.update_comparison_view(target["original"], mod_match)
            deadline = time.time() + 120
            while time.time() < deadline:
                APP.processEvents()
                current = window.comp_widget.current_img2_path or ""
                if current.endswith("__assembled.png"):
                    break
                time.sleep(0.05)
            assembled_path = window.comp_widget.current_img2_path or ""
            check("highlight: atlas preview shows the assembled page",
                  assembled_path.endswith("__assembled.png"), assembled_path)
            check("highlight: assembled file exists", os.path.exists(assembled_path), assembled_path)

            orig_preview = target["original"]["save_path"]
            mod_preview = mod_match["save_path"]
            assembled = Image.open(assembled_path) if os.path.exists(assembled_path) else None
            with Image.open(orig_preview) as handle:
                preview_size = handle.size
            check("highlight: assembled page uses the preview scale",
                  assembled is not None and max(assembled.size) <= 1024,
                  None if assembled is None else assembled.size)
            check("highlight: assembled page matches the original preview scale",
                  assembled is not None and assembled.size == preview_size,
                  (None if assembled is None else assembled.size, preview_size))

            from similarity import get_difference_mask
            import numpy as np

            def yellow_pixels(image):
                if image is None:
                    return 0
                return int(np.count_nonzero((image[:, :, 0] == 0) & (image[:, :, 1] == 255)
                                            & (image[:, :, 2] == 255)))

            raw_diff = yellow_pixels(get_difference_mask(orig_preview, mod_preview, grayscale_bg=True))
            asm_diff = yellow_pixels(get_difference_mask(orig_preview, assembled_path, grayscale_bg=True))
            print(f"  info  yellow pixels: raw mod sheet {raw_diff} -> assembled {asm_diff}")
            check("highlight: assembling removes the re-packing diff noise",
                  asm_diff < raw_diff * 0.5, (raw_diff, asm_diff))

            # ---- the real UI passes a candidate summary (no atlas_page data) ----
            # the page metadata has to come from the modified texture pool by path
            candidate = next((c for c in (target.get("candidates") or [])
                              if c.get("save_path") == target.get("match_file")), None)
            if candidate is None:
                check("highlight: candidate row found", False, target.get("match_file"))
            else:
                window.mod_textures = mod_items
                window.diff_check.setChecked(True)
                window.update_comparison_view(target["original"], candidate)
                deadline = time.time() + 120
                while time.time() < deadline:
                    APP.processEvents()
                    current = window.comp_widget.current_img2_path or ""
                    if current.endswith("__assembled.png"):
                        break
                    time.sleep(0.05)
                check("highlight: a candidate selection is assembled too",
                      (window.comp_widget.current_img2_path or "").endswith("__assembled.png"),
                      window.comp_widget.current_img2_path)
                check("highlight: mod page data is found for a candidate",
                      bool(window._mod_page_meta(candidate).get("page_file")),
                      list(window._mod_page_meta(candidate).keys()))

            # ---- highlight OFF: the mod image itself must be compared, no assembly ----
            window.diff_check.setChecked(False)
            window.update_comparison_view(target["original"], mod_match)
            deadline = time.time() + 120
            while time.time() < deadline:
                APP.processEvents()
                current = window.comp_widget.current_img2_path or ""
                if current == mod_preview:
                    break
                time.sleep(0.05)
            plain_path = window.comp_widget.current_img2_path or ""
            check("highlight off: the raw mod image is shown",
                  plain_path == mod_preview, (plain_path, mod_preview))
            check("highlight off: no assembled page is used",
                  not plain_path.endswith("__assembled.png"), plain_path)
            window.close()

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

        print("== recalc after apply ==")
        window = app_main.MainWindow()
        window.results = results
        window.mod_textures = mod_items
        window._recalculate_item_similarity(res)
        check("page: recalculated row keeps the atlas column",
              "% (" in (res.get("atlas_text") or "") and res.get("atlas_state") == "page",
              (res.get("atlas_text"), res.get("atlas_state")))
        window.close()

        print("== UI column (page mode) ==")
        try:
            window = app_main.MainWindow()
            window.results = results
            window.update_tree(show_progress=False)
            top = window.tree.topLevelItem(0)
            check("page ui: Atlas column sits next to Similarity",
                  window.tree.headerItem().text(4) == "Similarity"
                  and window.tree.headerItem().text(5) == "Atlas",
                  (window.tree.headerItem().text(4), window.tree.headerItem().text(5)))
            check("page ui: Atlas cell shows the overlap", "% (" in top.text(5), top.text(5))
            check("page ui: Similarity tooltip explains the metric",
                  "Piece overlap" in top.toolTip(4), top.toolTip(4)[:60])
            check("page ui: Atlas tooltip explains the ratio",
                  "same pieces" in top.toolTip(5), top.toolTip(5)[:80])
            # a page row must say which page of which atlas it is (hina_cg1_2 = page 2/3)
            window.tree.sortItems(1, Qt.SortOrder.AscendingOrder)
            names = [window.tree.topLevelItem(i).text(1)
                     for i in range(window.tree.topLevelItemCount())]
            idx = names.index("hina_cg1_2") if "hina_cg1_2" in names else 0
            page_tip = window.tree.topLevelItem(idx).toolTip(5)
            check("page ui: Atlas tooltip names the page and its atlas",
                  "page 2/3" in page_tip and "of hina_cg1.atlas" in page_tip,
                  (names[idx] if names else None, page_tip[-120:]))
            # the Atlas column must sort numerically, not alphabetically
            window.tree.sortItems(5, Qt.SortOrder.DescendingOrder)
            ratios = [window.tree.topLevelItem(i).data(5, Qt.ItemDataRole.UserRole)
                      for i in range(window.tree.topLevelItemCount())]
            numeric = [r for r in ratios if isinstance(r, (int, float))]
            check("page ui: Atlas column sorts by the numeric ratio",
                  numeric == sorted(numeric, reverse=True), numeric)
            window.close()
        except Exception as error:
            check(f"page ui: tree rendering ({type(error).__name__})", False, str(error))
    finally:
        os.chdir(cwd)
        remove_tree(root)


def test_atlas_disabled():
    """Check Atlas OFF: .atlas files are ignored, page images behave like plain images."""
    root = tempfile.mkdtemp(prefix="atlas_off_", dir=os.path.dirname(PROJECT))
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

        data, error = run_worker(
            "extract", "run_extract",
            orig_dir=orig_dir, mod_dir=mod_dir, temp_orig=temp_orig, temp_mod=temp_mod,
            mode="Image", orig_scan_all=False, mod_scan_all=False,
            atlas_enabled=False,
        )
        check("off: extract finished without error", error is None, error)
        orig_items = (data or {}).get("orig", [])
        print(f"  info  {len(orig_items)} items without atlas handling: {[t['name'] for t in orig_items]}")
        check("off: no atlas metadata on the items",
              all(not t.get("atlas_page") and not t.get("atlas") for t in orig_items))
        check("off: atlas page images become ordinary items",
              any(t["name"].startswith("10p") for t in orig_items), [t["name"] for t in orig_items])

        print("== UI checkbox ==")
        window = app_main.MainWindow()
        check("ui: 'Check Atlas' exists and defaults to on",
              window.chk_atlas.text() == "Check Atlas" and window.chk_atlas.isChecked(),
              (window.chk_atlas.text(), window.chk_atlas.isChecked()))
        check("ui: 'Per region' option is gone", not hasattr(window, "chk_atlas_region"))
        window.chk_atlas.setChecked(False)
        check("ui: 'Check Atlas' can be turned off", window.chk_atlas.isChecked() is False)
        window.close()
    finally:
        os.chdir(cwd)
        remove_tree(root)


def test_stale_metadata():
    """An extraction stored by an older version must be re-extracted, not reused."""
    root = tempfile.mkdtemp(prefix="atlas_stale_", dir=os.path.dirname(PROJECT))
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

        data, error = run_worker(
            "extract", "run_extract",
            orig_dir=orig_dir, mod_dir=mod_dir, temp_orig=temp_orig, temp_mod=temp_mod,
            mode="Image", orig_scan_all=False, mod_scan_all=False, atlas_enabled=True,
        )
        check("stale: first extraction ok", error is None and bool(data), error)
        fresh = (data or {}).get("orig", [])
        check("stale: page items produced", all(t.get("atlas_page") for t in fresh), len(fresh))

        # 1) legacy per-region metadata must trigger a re-extraction
        legacy = [{"name": "old piece", "file": "old.png", "save_path": os.path.join(temp_orig, "old.png"),
                   "atlas": {"kind": "atlas", "region": "old piece", "atlas_file": os.path.join(orig_dir, "10p.atlas"),
                             "page_file": os.path.join(orig_dir, "10p.png"), "rect": [0, 0, 8, 8],
                             "sprite_size": [8, 8], "rotate": 0, "pma": True}}]
        with open(os.path.join(temp_orig, "metadata.json"), "w", encoding="utf-8") as handle:
            json.dump(legacy, handle)
        logs = []
        worker = app_main.WorkerThread(
            "extract", orig_dir=orig_dir, mod_dir=mod_dir, temp_orig=temp_orig, temp_mod=temp_mod,
            mode="Image", orig_scan_all=False, mod_scan_all=False, atlas_enabled=True)
        worker.sig_log.connect(logs.append)
        captured = {}
        worker.sig_finished.connect(lambda d: captured.setdefault("d", d))
        worker.run_extract()
        out = captured.get("d") or {}
        check("stale: legacy metadata is not reused",
              any("old per-region layout; re-extracting" in line for line in logs),
              [line for line in logs if "Stored" in line][:3])
        check("stale: page items are rebuilt", all(t.get("atlas_page") for t in out.get("orig", [])))

        # 2) page metadata without digests must trigger a re-extraction as well
        stripped = [{k: v for k, v in item.items() if k != "atlas_page"} for item in fresh[:1]]
        stripped[0]["atlas_page"] = {"kind": "atlas-page", "atlas_file": "", "page_file": "", "pieces": 0}
        with open(os.path.join(temp_orig, "metadata.json"), "w", encoding="utf-8") as handle:
            json.dump(stripped, handle)
        logs = []
        worker = app_main.WorkerThread(
            "extract", orig_dir=orig_dir, mod_dir=mod_dir, temp_orig=temp_orig, temp_mod=temp_mod,
            mode="Image", orig_scan_all=False, mod_scan_all=False, atlas_enabled=True)
        worker.sig_log.connect(logs.append)
        captured = {}
        worker.sig_finished.connect(lambda d: captured.setdefault("d", d))
        worker.run_extract()
        check("stale: missing piece data forces re-extraction",
              any("lacks atlas piece data; re-extracting" in line for line in logs),
              [line for line in logs if "Stored" in line][:3])

        # 4) metadata that merged every page into one atlas item must be re-extracted too,
        #    otherwise the single page files (hina_cg1_2, ...) would stay hidden
        merged = json.loads(json.dumps(fresh[:1]))
        merged[0].pop("atlas_page", None)
        merged[0]["atlas_item"] = {"kind": "atlas", "atlas_file": os.path.join(orig_dir, "10p.atlas"),
                                   "page_file": os.path.join(orig_dir, "10p.png"),
                                   "pages": [], "pieces": 0, "digests": {}}
        with open(os.path.join(temp_orig, "metadata.json"), "w", encoding="utf-8") as handle:
            json.dump(merged, handle)
        logs = []
        worker = app_main.WorkerThread(
            "extract", orig_dir=orig_dir, mod_dir=mod_dir, temp_orig=temp_orig, temp_mod=temp_mod,
            mode="Image", orig_scan_all=False, mod_scan_all=False, atlas_enabled=True)
        worker.sig_log.connect(logs.append)
        captured = {}
        worker.sig_finished.connect(lambda d: captured.setdefault("d", d))
        worker.run_extract()
        out = captured.get("d") or {}
        check("stale: merged atlas metadata forces re-extraction",
              any("merges atlas pages; re-extracting" in line for line in logs),
              [line for line in logs if "Stored" in line][:3])
        check("stale: pages are items again after the re-extraction",
              all(t.get("atlas_page") and not t.get("atlas_item") for t in out.get("orig", [])),
              [(t.get("name"), bool(t.get("atlas_page"))) for t in out.get("orig", [])][:4])
    finally:
        os.chdir(cwd)
        remove_tree(root)


def test_match_fallback_scan():
    """Match without extraction (e.g. right after Clear temp) must still use atlas pages."""
    root = tempfile.mkdtemp(prefix="atlas_fallback_", dir=os.path.dirname(PROJECT))
    cwd = os.getcwd()
    try:
        orig_dir, mod_dir = prepare_pair(root)
        if not orig_dir or not mod_dir:
            print("  SKIP  game folders from config.ini not available")
            return
        temp_dir = os.path.join(root, "temp")
        os.makedirs(temp_dir, exist_ok=True)
        os.chdir(root)

        window = app_main.MainWindow()
        items = window.scan_image_folder(orig_dir, temp_dir)
        pages = [t for t in items if t.get("atlas_page")]
        print(f"  info  fallback scan: {len(items)} items, {len(pages)} atlas page(s) "
              f"-> {[t['name'] for t in pages]}")
        check("fallback: atlas pages are expanded into page items", bool(pages), len(pages))
        check("fallback: page items carry piece digests",
              all(p["atlas_page"].get("digests") for p in pages))
        check("fallback: sheet PNGs are not offered as plain images",
              all(not (t["name"] == "10p" and not t.get("atlas_page")) for t in items),
              [t["name"] for t in items if t["name"] == "10p"])
        ten = next((t for t in pages if t["name"] == "10p"), None)
        check("fallback: 10p is offered as an atlas page", ten is not None,
              [t["name"] for t in pages])
        check("fallback: page item points at the real page file",
              bool(ten and ten["atlas_page"].get("page_file")))
        window.close()
    finally:
        os.chdir(cwd)
        remove_tree(root)


def test_preview_rebuild():
    """Missing previews (Clear temp) are rebuilt and the right panel works again."""
    root = tempfile.mkdtemp(prefix="atlas_rebuild_", dir=os.path.dirname(PROJECT))
    cwd = os.getcwd()
    try:
        orig_dir, mod_dir = prepare_pair(root)
        if not orig_dir or not mod_dir:
            print("  SKIP  game folders from config.ini not available")
            return
        os.chdir(root)
        temp_orig = os.path.join(root, "t_orig")
        temp_mod = os.path.join(root, "t_mod")
        orig_items = []
        mod_items = []
        for atlas_name in ATLASES:
            orig_items += atlas_mod.extract_atlas_pages(os.path.join(orig_dir, atlas_name),
                                                        os.path.join(temp_orig, "atlas"))
            mod_items += atlas_mod.extract_atlas_pages(os.path.join(mod_dir, atlas_name),
                                                       os.path.join(temp_mod, "atlas"))
        print(f"  info  extracted {len(orig_items)} / {len(mod_items)} page items: "
              f"{[t['name'] for t in orig_items]}")

        # simulate "Clear temp Folders": every preview file disappears
        for item in orig_items + mod_items:
            if os.path.exists(item["save_path"]):
                os.remove(item["save_path"])
        check("rebuild: previews are gone",
              all(not os.path.exists(t["save_path"]) for t in orig_items + mod_items))

        window = app_main.MainWindow()
        window.temp_dir_orig = temp_orig
        window.temp_dir_mod = temp_mod
        window.orig_path.setText(orig_dir)
        window.mod_path.setText(mod_dir)
        window.orig_textures = orig_items
        window.mod_textures = mod_items
        window.match()

        deadline = time.time() + 120
        while time.time() < deadline:
            APP.processEvents()
            if window.worker_thread is None and window.results:
                break
            time.sleep(0.1)

        check("rebuild: previews were rebuilt",
              all(os.path.exists(t["save_path"]) for t in orig_items + mod_items),
              [t["name"] for t in orig_items if not os.path.exists(t["save_path"])])
        check("rebuild: match produced results", bool(window.results), len(window.results))
        four = next((r for r in window.results if r["original"]["name"].startswith("10p")), None)
        check("rebuild: 10p row kept its atlas info",
              bool(four and "% (" in (four.get("atlas_text") or "")),
              None if four is None else four.get("atlas_text"))
        check("rebuild: row names are the page stems",
              all("__" not in r["original"]["name"] for r in window.results),
              [r["original"]["name"] for r in window.results][:5])
        window.close()
    finally:
        os.chdir(cwd)
        remove_tree(root)


def test_restart_restore():
    """Restart after Match: results are restored and the right panel still shows the mod image."""
    root = tempfile.mkdtemp(prefix="atlas_restart_", dir=os.path.dirname(PROJECT))
    cwd = os.getcwd()
    try:
        orig_dir, mod_dir = prepare_pair(root)
        if not orig_dir or not mod_dir:
            print("  SKIP  game folders from config.ini not available")
            return
        os.chdir(root)
        temp_orig = os.path.join(root, "t_orig")
        temp_mod = os.path.join(root, "t_mod")
        os.makedirs(temp_orig, exist_ok=True)
        os.makedirs(temp_mod, exist_ok=True)

        # --- first session: extract + match, then close (saves metadata) ---
        first = app_main.MainWindow()
        first.temp_dir_orig = temp_orig
        first.temp_dir_mod = temp_mod
        first.orig_path.setText(orig_dir)
        first.mod_path.setText(mod_dir)
        data, error = run_worker(
            "extract", "run_extract",
            orig_dir=orig_dir, mod_dir=mod_dir, temp_orig=temp_orig, temp_mod=temp_mod,
            mode="Image", orig_scan_all=False, mod_scan_all=False, atlas_enabled=True,
        )
        check("restart: extraction ok", error is None and bool(data), error)
        first.orig_textures = (data or {}).get("orig", [])
        first.mod_textures = (data or {}).get("mod", [])
        first.results = []
        worker = app_main.WorkerThread("match", orig_textures=first.orig_textures,
                                       mod_textures=first.mod_textures, num_candidates=5,
                                       unchecked_same=False, same_res=False,
                                       filter_enabled=False, sim_cutoff=0.5)
        captured = {}
        worker.sig_finished.connect(lambda d: captured.setdefault("d", d))
        worker.run_match()
        first.results = captured.get("d") or []
        check("restart: first session produced results", bool(first.results), len(first.results))
        first.close()  # closeEvent -> save_settings + _sync_metadata_to_disk

        previews = [r["match_file"] for r in first.results if r.get("match_file")]
        check("restart: mod previews exist before restart", all(os.path.exists(p) for p in previews))

        # a stale "last_extract" entry must not wipe the cached previews anymore
        settings = QSettings("config.ini", QSettings.Format.IniFormat)
        settings.setValue("last_extract/modified", os.path.join(root, "somewhere_else"))
        settings.sync()

        # --- second session: restart ---
        second = app_main.MainWindow()
        second.temp_dir_orig = temp_orig
        second.temp_dir_mod = temp_mod
        second.orig_path.setText(orig_dir)
        second.mod_path.setText(mod_dir)
        second.restore_matching_results()   # temp dirs are overridden after __init__
        APP.processEvents()

        check("restart: mod previews survived the restart",
              all(os.path.exists(p) for p in previews),
              [p for p in previews if not os.path.exists(p)][:3])
        check("restart: results were restored", bool(second.results), len(second.results))
        if not second.results:
            second.close()
            return

        item = second.tree.topLevelItem(0)
        check("restart: atlas cell restored",
              bool(item and ("%" in item.text(5) or "pcs" in item.text(5))),
              None if item is None else item.text(5))
        item.setSelected(True)
        second.tree.setCurrentItem(item)
        deadline = time.time() + 30
        while time.time() < deadline:
            APP.processEvents()
            pix = second.comp_widget.matched_label.main_pixmap
            if pix is not None and not pix.isNull():
                break
            time.sleep(0.05)
        matched = second.comp_widget.matched_label.main_pixmap
        check("restart: right panel shows the mod image",
              matched is not None and not matched.isNull(),
              None if matched is None else matched.size())
        second.close()
    finally:
        os.chdir(cwd)
        remove_tree(root)


def test_thumbnail_viewport_frame():
    """The thumbnail frame must stand out on a grayscale + yellow difference image."""
    from PyQt6.QtGui import QImage, QPixmap
    import numpy as np

    window = app_main.MainWindow()
    try:
        # a grayscale background with a yellow difference band (the diff thumbnail look)
        image = QImage(200, 200, QImage.Format.Format_RGBA8888)
        image.fill(0)
        data = np.zeros((200, 200, 4), dtype=np.uint8)
        data[:, :, 0] = 110      # gray
        data[:, :, 1] = 110
        data[:, :, 2] = 110
        data[:, :, 3] = 255
        data[60:120, :] = (255, 255, 0, 255)   # yellow band (RGBA)
        image = QImage(data.tobytes(), 200, 200, 200 * 4, QImage.Format.Format_RGBA8888).copy()

        window.current_orig_img = "thumb"
        window.current_pix = QPixmap.fromImage(image)
        window.thumbnail_preview.setFixedSize(200, 200)
        window.last_viewport = (0.25, 0.25, 0.5, 0.5)
        window.draw_thumbnail_viewport()

        frame = window.thumbnail_preview.pixmap()
        check("frame: thumbnail pixmap drawn", frame is not None and not frame.isNull())
        if frame is None or frame.isNull():
            return
        rgba = frame.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        probe_dir = tempfile.mkdtemp(prefix="frame_probe_", dir=os.path.dirname(PROJECT))
        probe_png = os.path.join(probe_dir, "frame.png")
        check("frame: thumbnail pixmap saved", frame.save(probe_png, "PNG"))
        try:
            with Image.open(probe_png) as handle:
                arr = np.asarray(handle.convert("RGBA"))
        finally:
            remove_tree(probe_dir)

        magenta = int(np.count_nonzero((arr[:, :, 0] > 200) & (arr[:, :, 1] < 60)
                                       & (arr[:, :, 2] > 200)))
        dark = int(np.count_nonzero(arr[:, :, :3].max(axis=2) < 40))
        check("frame: the viewport frame is drawn in magenta", magenta > 100, magenta)
        check("frame: a dark halo makes it readable on yellow", dark > 100, dark)
        # the frame must be a rectangle around the viewport, not a filled block
        x, y, w, h = window.last_viewport
        inner = arr[int((y + 0.05) * arr.shape[0]):int((y + h - 0.05) * arr.shape[0]),
                    int((x + 0.05) * arr.shape[1]):int((x + w - 0.05) * arr.shape[1])]
        yellow_left = int(np.count_nonzero((inner[:, :, 0] > 240) & (inner[:, :, 1] > 240)
                                          & (inner[:, :, 2] < 60)))
        check("frame: the area inside the frame is left untouched", yellow_left > 100, yellow_left)
    finally:
        window.close()


def make_unity_page(root, tag, colors, positions, path_id, size=(200, 100)):
    """Builds a Unity-style page item: one texture + inline sprite rects (no .atlas file)."""
    import unity_atlas

    path = os.path.join(root, f"{tag}.png")
    page = Image.new("RGBA", size, (0, 0, 0, 0))
    pieces = []
    for name, color, (x, y) in (("piece_a", colors["a"], positions["a"]),
                                ("piece_b", colors["b"], positions["b"])):
        width, height = (40, 40) if name == "piece_a" else (60, 20)
        page.paste(Image.new("RGBA", (width, height), color), (x, y))
        pieces.append({
            "kind": "atlas", "region": name, "atlas_file": "", "page_file": path,
            "rect": [x, y, width, height], "sprite_size": [width, height], "rotate": 0,
            "index": -1, "offset": [0, 0], "orig_size": [width, height], "pma": False,
            "scale": 1.0,
        })
    page.save(path, format="PNG")
    meta = unity_atlas.page_metadata(pieces, path, size, page_name="atlas_page")
    meta["digests"] = atlas_mod.page_piece_digests(page, atlas_mod.regions_from_metadata(pieces))
    return {
        "name": "atlas_page",
        "file": os.path.basename(path),
        "save_path": path,
        "width": size[0],
        "height": size[1],
        "size": os.path.getsize(path),
        "path_id": path_id,
        "source_asset": os.path.join(root, "fake.assets"),
        "atlas_page": meta,
    }


def test_unity_page_items():
    """Unity style pages (inline regions, no .atlas file): match, preview and apply."""
    root = tempfile.mkdtemp(prefix="unity_pages_", dir=os.path.dirname(PROJECT))
    try:
        red, blue, green = (220, 30, 30, 255), (30, 30, 220, 255), (30, 200, 30, 255)
        orig = make_unity_page(root, "orig_page", {"a": red, "b": green},
                               {"a": (0, 0), "b": (40, 0)}, path_id=111)
        # the mod build repacked the sprites and changed piece_a
        mod = make_unity_page(root, "mod_page", {"a": blue, "b": green},
                              {"a": (100, 40), "b": (10, 60)}, path_id=222)
        temp_dir = os.path.join(root, "temp")
        os.makedirs(temp_dir, exist_ok=True)

        check("unity: page regions are read from metadata",
              len(app_main.atlas_page_regions(orig["atlas_page"])) == 2)
        check("unity: identity key uses the texture name",
              app_main.atlas_keys(orig) == ("unity|atlas_page", "atlas_page"),
              app_main.atlas_keys(orig))

        results, error = run_worker("match", "run_match", orig_textures=[orig],
                                    mod_textures=[mod], num_candidates=5,
                                    unchecked_same=False, same_res=False,
                                    filter_enabled=False, sim_cutoff=0.5)
        check("unity: match finished without error", error is None, error)
        results = results or []
        check("unity: one result", len(results) == 1, len(results))
        if not results:
            return
        res = results[0]
        stats = res.get("piece_stats") or {}
        print(f"  info  unity page: {stats}")
        check("unity: paired by atlas identity", res.get("match_by") == "atlas",
              res.get("match_by"))
        check("unity: piece overlap is computed", res.get("atlas_state") == "page",
              res.get("atlas_state"))
        check("unity: one piece changed, both repacked",
              stats.get("changed") == 1 and stats.get("moved") == 2, stats)
        check("unity: ratio is same pieces / all pieces",
              abs((res.get("atlas_ratio") or 0) - 0.5) < 1e-6, res.get("atlas_ratio"))

        assembled = app_main.assemble_atlas_preview(
            orig["atlas_page"], mod["atlas_page"], os.path.join(root, "asm"),
            orig_preview_path=orig["save_path"])
        check("unity: assembled preview built", bool(assembled) and os.path.exists(assembled),
              assembled)
        if assembled:
            page = atlas_mod.load_page_image(assembled)
            regions = app_main.atlas_page_regions(orig["atlas_page"])
            if page is None:
                check("unity: assembled page readable", False)
                return
            changed = atlas_mod.unpack_region(page, next(r for r in regions
                                                         if r.name == "piece_a"))
            kept = atlas_mod.unpack_region(page, next(r for r in regions
                                                      if r.name == "piece_b"))
            check("unity: assembled page carries the mod piece",
                  images_equal(changed, Image.new("RGBA", (40, 40), blue)))
            check("unity: assembled page keeps the unchanged piece",
                  images_equal(kept, Image.new("RGBA", (60, 20), green)))

        # apply: the page is composited in place, then queued as a texture replacement
        data, error = run_worker("apply", "run_apply", to_replace=[res],
                                 orig_dir=root, mod_dir=root,
                                 temp_orig=temp_dir, temp_mod=temp_dir, mode="Unity",
                                 high_quality=False, save_as_original=True)
        check("unity: apply finished without error", error is None, error)
        check("unity: apply wrote the changed piece", bool(data and data.get("success")), data)
        page = atlas_mod.load_page_image(orig["save_path"])
        regions = app_main.atlas_page_regions(orig["atlas_page"])
        if page is None:
            check("unity: applied page readable", False)
            return
        piece_a = atlas_mod.unpack_region(page, next(r for r in regions if r.name == "piece_a"))
        check("unity: page file now holds the mod art",
              images_equal(piece_a, Image.new("RGBA", (40, 40), blue)))
        check("unity: page keeps its full resolution",
              page.size == (orig["width"], orig["height"]), page.size)
    finally:
        remove_tree(root)


def main():
    test_legacy_region_apply()
    test_assembled_preview_unit()
    test_thumbnail_viewport_frame()
    test_unity_page_items()
    test_page_mode()
    test_atlas_disabled()
    test_stale_metadata()
    test_match_fallback_scan()
    test_preview_rebuild()
    test_restart_restore()

    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print("FAILED:", FAILURES)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
