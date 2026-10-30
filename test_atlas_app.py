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


def describe_atlas_digests(atlas_path):
    """piece key -> md5 (and page sizes) for a whole atlas, from the current page images."""
    try:
        parsed = atlas_mod.parse_atlas(atlas_path)
    except Exception:
        return {}, {}
    images, sizes = {}, {}
    for page in parsed.pages:
        image = atlas_mod.load_page_image(parsed.page_path(page))
        if image is not None:
            images[page.name] = image
            sizes[page.name] = image.size
    return atlas_mod.atlas_piece_digests(parsed, images), sizes


def test_atlas_item_mode():
    """Atlas level mode: 1 item per .atlas file, similarity = same pieces / all pieces."""
    root = tempfile.mkdtemp(prefix="atlas_items_", dir=os.path.dirname(PROJECT))
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

        print("== extract (atlas items) ==")
        data, error = run_worker(
            "extract", "run_extract",
            orig_dir=orig_dir, mod_dir=mod_dir, temp_orig=temp_orig, temp_mod=temp_mod,
            mode="Image", orig_scan_all=False, mod_scan_all=False,
        )
        check("item: extract finished without error", error is None, error)
        if not data:
            check("item: extract returned data", False)
            return
        orig_items, mod_items = data.get("orig", []), data.get("mod", [])
        print(f"  info  {len(orig_items)} items per side: {[t['name'] for t in orig_items]}")
        check("item: one item per .atlas file", len(orig_items) == len(ATLASES), len(orig_items))
        mod_atlas_items = [t for t in mod_items if t.get("atlas_item")]
        check("item: one item per .atlas file (mod)",
              len(mod_atlas_items) == len(ATLASES),
              [(t["name"], bool(t.get("atlas_item"))) for t in mod_items])
        check("item: every item is a whole atlas",
              all((t.get("atlas_item") or {}).get("digests") for t in orig_items))
        check("item: no per-page or per-region items are created",
              all(not t.get("atlas_page") and not t.get("atlas") for t in orig_items))
        check("item: item names are the atlas stems",
              all("__" not in t["name"] for t in orig_items), [t["name"] for t in orig_items])
        check("item: preview file exists for each atlas",
              all(os.path.exists(t["save_path"]) for t in orig_items))
        check("item: preview is downscaled",
              all(max(Image.open(t["save_path"]).size) <= 1024 for t in orig_items))

        multi = next((t for t in orig_items if len(t["atlas_item"]["pages"]) > 1), None)
        check("item: a multi-page atlas stays one item", multi is not None,
              [(t["name"], len(t["atlas_item"]["pages"])) for t in orig_items])
        if multi:
            pages = multi["atlas_item"]["pages"]
            print(f"  info  {multi['name']}: {len(pages)} pages, "
                  f"{multi['atlas_item']['pieces']} pieces")
            check("item: pieces of every page are counted",
                  multi["atlas_item"]["pieces"] == sum(p["pieces"] for p in pages),
                  (multi["atlas_item"]["pieces"], sum(p["pieces"] for p in pages)))
            check("item: page names are recorded (1p_2 style is a page, not a piece)",
                  all(p["page"].endswith(".png") for p in pages), [p["page"] for p in pages])
            check("item: no item is named like a second page",
                  all(not t["name"].endswith(("_2", "_3", "_4")) for t in orig_items),
                  [t["name"] for t in orig_items])

        print("== match (atlas items) ==")
        results, error = run_worker(
            "match", "run_match",
            orig_textures=orig_items, mod_textures=mod_items,
            num_candidates=5, unchecked_same=False, same_res=False,
            filter_enabled=False, sim_cutoff=0.5,
        )
        check("item: match finished without error", error is None, error)
        results = results or []
        check("item: one result per atlas", len(results) == len(orig_items),
              (len(results), len(orig_items)))
        check("item: atlases were paired", all(r.get("match_file") for r in results),
              [r.get("match_file") for r in results])
        plain_candidates = [c for r in results for c in (r.get("candidates") or [])
                            if c.get("name", "").startswith("plain_candidate")]
        check("item: plain images are still offered as candidates", bool(plain_candidates),
              [c.get("name") for r in results for c in (r.get("candidates") or [])][:6])
        check("item: plain candidates carry no atlas ratio",
              all(c.get("atlas_ratio") is None for c in plain_candidates),
              [c.get("atlas_ratio") for c in plain_candidates][:3])

        by_name = {r["original"]["name"]: r for r in results}
        ten = by_name.get("10p")
        cg4 = by_name.get("hina_cg4")
        if ten:
            stats = ten.get("piece_stats") or {}
            print(f"  info  10p: {stats}")
            check("item: identity atlas is paired", ten.get("match_by") == "atlas", ten.get("match_by"))
            check("item: mostly unchanged atlas scores high", (ten.get("atlas_ratio") or 0) >= 0.85,
                  ten.get("atlas_ratio"))
            check("item: Atlas column shows percent and (same/total)",
                  "% (" in (ten.get("atlas_text") or ""), ten.get("atlas_text"))
            check("item: Atlas tooltip explains the overlap",
                  "same pieces" in (ten.get("atlas_detail") or ""), ten.get("atlas_detail"))
            check("item: similarity merges atlas and plain scores",
                  ten["best_similarity"] >= (ten.get("atlas_ratio") or 0) - 1e-9, ten["best_similarity"])
        if cg4:
            stats = cg4.get("piece_stats") or {}
            total = max(1, stats.get("pieces", 1) + stats.get("missing", 0))
            expected = stats.get("same", 0) / total
            print(f"  info  hina_cg4: {stats} -> expected {expected:.4f}")
            check("item: ratio == same pieces / total pieces",
                  abs((cg4.get("atlas_ratio") or 0) - expected) < 1e-6,
                  (cg4.get("atlas_ratio"), expected))

        print("== apply (atlas items) ==")
        to_replace = [r for r in results if r.get("match_file")][:2]
        # prefer a multi-page atlas so the per-page write-back is exercised
        target = next((r for r in results
                       if len((r.get("original") or {}).get("atlas_item", {}).get("pages") or []) > 1),
                      to_replace[0])
        if target not in to_replace:
            to_replace = [target] + to_replace[:1]
        orig_atlas_file = target["original"]["atlas_item"]["atlas_file"]
        mod_atlas_file = (target.get("match_atlas_item") or {}).get("atlas_file", "")
        before_digests, before_sizes = describe_atlas_digests(orig_atlas_file)
        mod_digests, _ = describe_atlas_digests(mod_atlas_file)
        changed_keys = [k for k, v in before_digests.items()
                        if mod_digests.get(k) and mod_digests[k] != v]

        data, error = run_worker(
            "apply", "run_apply",
            to_replace=to_replace, orig_dir=orig_dir, mod_dir=mod_dir,
            temp_orig=temp_orig, temp_mod=temp_mod, mode="Image",
            high_quality=False, save_as_original=True,
        )
        check("item: apply finished without error", error is None, error)
        check("item: apply reports writes", bool(data and data.get("success")), data)

        after_digests, after_sizes = describe_atlas_digests(orig_atlas_file)
        check("item: every page keeps its size", after_sizes == before_sizes,
              (before_sizes, after_sizes))
        written = [k for k in changed_keys if after_digests.get(k) == mod_digests.get(k)]
        untouched = [k for k in before_digests if k not in changed_keys]
        kept = [k for k in untouched if after_digests.get(k) == before_digests.get(k)]
        print(f"  info  {len(changed_keys)} changed pieces -> {len(written)} written, "
              f"{len(kept)}/{len(untouched)} unchanged pieces untouched")
        check("item: changed pieces are written into their original pages", bool(written),
              (len(changed_keys), len(written)))
        check("item: unchanged pieces keep the original pixels", kept == untouched,
              (len(untouched), len(kept)))

        print("== recalc after apply ==")
        window = app_main.MainWindow()
        window.results = results
        window.mod_textures = mod_items
        window._recalculate_item_similarity(target)
        check("item: recalculated row keeps the atlas column",
              "% (" in (target.get("atlas_text") or "") and target.get("atlas_state") == "atlas",
              (target.get("atlas_text"), target.get("atlas_state")))
        window.close()

        print("== UI column (atlas items) ==")
        try:
            window = app_main.MainWindow()
            window.results = results
            window.update_tree(show_progress=False)
            top = window.tree.topLevelItem(0)
            check("item ui: Atlas column sits next to Similarity",
                  window.tree.headerItem().text(4) == "Similarity"
                  and window.tree.headerItem().text(5) == "Atlas",
                  (window.tree.headerItem().text(4), window.tree.headerItem().text(5)))
            check("item ui: Atlas cell shows the overlap", "% (" in top.text(5), top.text(5))
            check("item ui: Similarity tooltip explains the metric",
                  "Piece overlap" in top.toolTip(4), top.toolTip(4)[:60])
            check("item ui: Atlas tooltip explains the ratio",
                  "same pieces" in top.toolTip(5), top.toolTip(5)[:80])
            # the Atlas column must sort numerically, not alphabetically
            window.tree.sortItems(5, Qt.SortOrder.DescendingOrder)
            ratios = [window.tree.topLevelItem(i).data(5, Qt.ItemDataRole.UserRole)
                      for i in range(window.tree.topLevelItemCount())]
            numeric = [r for r in ratios if isinstance(r, (int, float))]
            check("item ui: Atlas column sorts by the numeric ratio",
                  numeric == sorted(numeric, reverse=True), numeric)
            window.close()
        except Exception as error:
            check(f"item ui: tree rendering ({type(error).__name__})", False, str(error))
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
        check("stale: atlas items produced", all(t.get("atlas_item") for t in fresh), len(fresh))

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
        check("stale: atlas items are rebuilt", all(t.get("atlas_item") for t in out.get("orig", [])))

        # 2) an old per-page extraction must trigger a re-extraction as well
        stripped = [{k: v for k, v in item.items() if k != "atlas_item"} for item in fresh[:1]]
        stripped[0]["atlas_page"] = {"kind": "atlas-page", "atlas_file": os.path.join(orig_dir, "10p.atlas"),
                                     "page_file": os.path.join(orig_dir, "10p.png"), "pieces": 4,
                                     "digests": {"a": "b"}}
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
        out = captured.get("d") or {}
        check("stale: page split metadata forces re-extraction",
              any("splits atlases into pages; re-extracting" in line for line in logs),
              [line for line in logs if "Stored" in line][:3])
        check("stale: atlas items replace the page items",
              all(t.get("atlas_item") for t in out.get("orig", [])), len(out.get("orig", [])))

        # 3) atlas metadata without piece data must be rebuilt
        stripped = json.loads(json.dumps(fresh[:1]))
        stripped[0]["atlas_item"]["digests"] = {}
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
        pages = [t for t in items if t.get("atlas_item")]
        print(f"  info  fallback scan: {len(items)} items, {len(pages)} atlas item(s) "
              f"-> {[t['name'] for t in pages]}")
        check("fallback: atlases are offered as items", bool(pages), len(pages))
        check("fallback: atlas items carry piece digests",
              all(p["atlas_item"].get("digests") for p in pages))
        check("fallback: sheet PNGs are not offered as plain images",
              all(not (t["name"] in ("10p", "10p_2", "hina_cg4_2") and not t.get("atlas_item"))
                  for t in items),
              [t["name"] for t in items if t["name"].startswith("10p")])
        ten = next((t for t in pages if t["name"] == "10p"), None)
        check("fallback: 10p is offered as one atlas item", ten is not None,
              [t["name"] for t in pages])
        check("fallback: atlas item points at the real page file",
              bool(ten and ten["atlas_item"].get("page_file")))
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
            orig_items += atlas_mod.extract_atlas_item(os.path.join(orig_dir, atlas_name),
                                                       os.path.join(temp_orig, "atlas"))
            mod_items += atlas_mod.extract_atlas_item(os.path.join(mod_dir, atlas_name),
                                                      os.path.join(temp_mod, "atlas"))
        print(f"  info  extracted {len(orig_items)} / {len(mod_items)} atlas items: "
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
        check("rebuild: row names are the atlas stems",
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


def main():
    test_legacy_region_apply()
    test_atlas_item_mode()
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
