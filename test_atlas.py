"""Functional tests for atlas.py (Spine / Live2D .atlas pages).

Run:  python test_atlas.py
Part 1 uses a synthetic fixture; part 2 runs against the folders stored in config.ini
([path] original / modified) and is skipped when they are not available.
"""
import os
import shutil
import sys
import tempfile

from PIL import Image, ImageChops

PROJECT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT)

import atlas as atlas_mod

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


def images_equal(a, b):
    if a is None or b is None or a.size != b.size:
        return False
    return ImageChops.difference(a.convert("RGBA"), b.convert("RGBA")).getbbox() is None


def build_fixture(root):
    """Creates a page + .atlas triple covering every rotate value and all layouts."""
    page = Image.new("RGBA", (64, 64), (0, 0, 0, 0))

    def block(color, w=16, h=8):
        img = Image.new("RGBA", (w, h), color)
        img.paste((255, 255, 255, 255), (0, 0, 4, 2))  # asymmetric marker
        return img

    page.paste(block((255, 0, 0, 255)), (0, 0))
    page.paste(block((0, 255, 0, 255)), (20, 0))
    page.paste(block((0, 0, 255, 255)), (0, 20))
    page.paste(block((255, 255, 0, 255)), (20, 20))
    page.save(os.path.join(root, "page.png"), format="PNG")

    flat = "\n".join([
        "page.png", "size:64,64", "filter:Linear,Linear", "format:RGBA8888", "pma:false",
        "r0", "bounds:0,0,16,8",
        "r90", "bounds:20,0,16,8", "rotate:90",
        "r180", "bounds:0,20,16,8", "rotate:180",
        "r270", "bounds:20,20,16,8", "rotate:270",
        "",
    ])
    with open(os.path.join(root, "flat.atlas"), "w", encoding="utf-8") as handle:
        handle.write(flat)

    indented = "\n".join([
        "page.png", "\tsize: 64, 64", "\tpma: true", "\tscale: 0.5",
        "r0", "\tbounds: 0, 0, 16, 8", "\toffsets: 1, 2, 20, 10",
        "r90", "\tbounds: 20, 0, 16, 8", "\trotate: 90",
        "",
    ])
    with open(os.path.join(root, "legacy.atlas"), "w", encoding="utf-8") as handle:
        handle.write(indented)

    legacy_xy = "\n".join([
        "page.png", "size: 64, 64",
        "r0", "xy: 0, 0", "size: 16, 8",
        "r90", "xy: 20, 0", "size: 16, 8", "rotate: true",
        "",
    ])
    with open(os.path.join(root, "xy.atlas"), "w", encoding="utf-8") as handle:
        handle.write(legacy_xy)


def test_parser(fixture):
    flat = atlas_mod.parse_atlas(os.path.join(fixture, "flat.atlas"))
    check("flat: single page", len(flat.pages) == 1, len(flat.pages))
    check("flat: page size", (flat.pages[0].width, flat.pages[0].height) == (64, 64),
          (flat.pages[0].width, flat.pages[0].height))
    names = [r.name for r in flat.pages[0].regions]
    check("flat: region names", names == ["r0", "r90", "r180", "r270"], names)
    rots = {r.name: r.rotate for r in flat.pages[0].regions}
    check("flat: rotations", rots == {"r0": 0, "r90": 90, "r180": 180, "r270": 270}, rots)
    r90 = next(r for r in flat.pages[0].regions if r.name == "r90")
    check("flat: footprint swapped for 90", (r90.packed_width, r90.packed_height) == (8, 16), r90.rect)
    check("flat: no pma", flat.pages[0].pma is False)

    indented = atlas_mod.parse_atlas(os.path.join(fixture, "legacy.atlas"))
    check("indented: pma parsed", indented.pages[0].pma is True)
    check("indented: scale parsed", abs(indented.pages[0].scale - 0.5) < 1e-6, indented.pages[0].scale)
    trimmed = indented.pages[0].regions[0]
    offsets = (trimmed.offset_x, trimmed.offset_y, trimmed.orig_width, trimmed.orig_height)
    check("indented: offsets parsed", offsets == (1, 2, 20, 10), offsets)

    legacy = atlas_mod.parse_atlas(os.path.join(fixture, "xy.atlas"))
    check("legacy xy: 2 regions", len(legacy.pages[0].regions) == 2, len(legacy.pages[0].regions))
    rotate_true = next(r for r in legacy.pages[0].regions if r.name == "r90").rotate
    check("legacy xy: rotate true -> 90", rotate_true == 90, rotate_true)


def test_transforms(fixture):
    page = Image.open(os.path.join(fixture, "page.png")).convert("RGBA")
    parsed = atlas_mod.parse_atlas(os.path.join(fixture, "flat.atlas"))
    for region in parsed.pages[0].regions:
        piece = atlas_mod.unpack_region(page, region)
        check(f"{region.name}: upright size equals sprite size",
              piece.size == (region.width, region.height), (piece.size, (region.width, region.height)))
        repacked = atlas_mod.pack_region(piece, region)
        check(f"{region.name}: repacked size equals footprint",
              repacked.size == (region.packed_width, region.packed_height), repacked.size)
        transform = atlas_mod._UNPACK_TRANSFORM.get(region.rotate)
        restored = repacked.transpose(transform) if transform is not None else repacked
        check(f"{region.name}: unpack(pack(x)) == x", images_equal(piece, restored))

    copy = page.copy()
    for region in parsed.pages[0].regions:
        atlas_mod.composite_region(copy, atlas_mod.unpack_region(page, region), region,
                                   source_premultiplied=True)
    check("composite identity keeps the page pixel-identical", images_equal(page, copy))


def test_write_back(fixture):
    page_path = os.path.join(fixture, "page.png")
    parsed = atlas_mod.parse_atlas(os.path.join(fixture, "flat.atlas"))
    page = Image.open(page_path).convert("RGBA")
    region = next(r for r in parsed.pages[0].regions if r.name == "r90")
    new_piece = Image.new("RGBA", (region.width, region.height), (12, 34, 56, 255))

    out_path = os.path.join(fixture, "written.png")
    check("write_page_regions returns True",
          atlas_mod.write_page_regions(page_path, [(region, new_piece)], out_path=out_path))
    check("written page exists", os.path.exists(out_path))

    written = Image.open(out_path).convert("RGBA")
    check("written region matches the new piece",
          images_equal(atlas_mod.unpack_region(written, region), new_piece))
    others_ok = all(
        images_equal(atlas_mod.unpack_region(written, other), atlas_mod.unpack_region(page, other))
        for other in parsed.pages[0].regions if other.name != region.name
    )
    check("other regions are untouched", others_ok)

    # premultiplied page: straight-alpha input must be premultiplied on write
    pma_atlas = atlas_mod.parse_atlas(os.path.join(fixture, "legacy.atlas"))
    pma_region = pma_atlas.pages[0].regions[0]
    straight = Image.new("RGBA", (pma_region.width, pma_region.height), (200, 100, 50, 128))
    pma_page = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    atlas_mod.composite_region(pma_page, straight, pma_region, source_premultiplied=False)
    x, y, w, h = pma_region.rect
    pasted = pma_page.crop((x, y, x + w, y + h))
    transform = atlas_mod._UNPACK_TRANSFORM.get(pma_region.rotate)
    if transform is not None:
        pasted = pasted.transpose(transform)
    pixel = pasted.getpixel((0, 0))
    expected = (200 * 128 // 255, 100 * 128 // 255, 50 * 128 // 255, 128)
    check("pma page premultiplies straight-alpha art", pixel == expected, (pixel, expected))

    big = Image.new("RGBA", (region.width * 2, region.height * 2), (9, 9, 9, 255))
    packed = atlas_mod.pack_region(big, region)
    check("pack_region resizes to the region size",
          packed.size == (region.packed_width, region.packed_height), packed.size)


def test_trim_normalization(fixture):
    """compare_images(trim=True) must ignore transparent padding (atlas trim differences)."""
    import similarity

    folder = os.path.join(fixture, "trim")
    os.makedirs(folder, exist_ok=True)
    part = Image.new("RGBA", (20, 10), (10, 200, 30, 255))
    base = Image.new("RGBA", (40, 20), (0, 0, 0, 0))
    base.paste(part, (5, 5))
    padded = Image.new("RGBA", (70, 50), (0, 0, 0, 0))
    padded.paste(base, (15, 25))  # same artwork, different transparent border
    path_a = os.path.join(folder, "a.png")
    path_b = os.path.join(folder, "b.png")
    base.save(path_a, format="PNG")
    padded.save(path_b, format="PNG")

    raw = similarity.compare_images(path_a, path_b)
    trimmed = similarity.compare_images(path_a, path_b, trim=True)
    check("trim: raw comparison is penalised by the padding", raw < 0.99, raw)
    check("trim: normalized comparison is 1.0", trimmed >= 0.999, trimmed)

    hash_a = similarity.get_image_hash(path_a, trim=True)
    hash_b = similarity.get_image_hash(path_b, trim=True)
    check("trim: hashes match after trimming",
          hash_a is not None and hash_b is not None and similarity.hamming_similarity(hash_a, hash_b) == 1.0)


def test_page_digests(fixture):
    """Page level comparison: same pieces / all pieces, without decoding pages again."""
    page = Image.open(os.path.join(fixture, "page.png")).convert("RGBA")
    parsed = atlas_mod.parse_atlas(os.path.join(fixture, "flat.atlas"))
    regions = parsed.pages[0].regions
    digests = atlas_mod.page_piece_digests(page, regions)
    check("digests: one entry per region", len(digests) == len(regions), (len(digests), len(regions)))

    same = atlas_mod.compare_piece_digests(digests, digests, regions, regions)
    check("digests: identical pages -> ratio 1.0", same.ratio == 1.0 and same.changed == 0, same.to_dict())
    check("digests: summary text shows percent and (same/total)",
          "%" in same.summary() and f"({same.same}/{same.total})" in same.summary(), same.summary())

    changed_page = page.copy()
    changed_page.paste(Image.new("RGBA", (4, 4), (1, 2, 3, 255)), (21, 21))  # inside the r270 block
    changed = atlas_mod.page_piece_digests(changed_page, regions)
    diff = atlas_mod.compare_piece_digests(digests, changed, regions, regions)
    check("digests: one changed piece lowers the ratio", diff.changed == 1 and diff.ratio < 1.0, diff.to_dict())

    # trimming: the same piece with an extra transparent border must stay identical
    region = regions[0]
    piece = atlas_mod.unpack_region(page, region)
    padded = Image.new("RGBA", (piece.width + 6, piece.height + 6), (0, 0, 0, 0))
    padded.paste(piece, (3, 3))
    check("digests: transparent padding is ignored",
          atlas_mod.piece_digest(piece) == atlas_mod.piece_digest(padded))

    # a small mosaic patch must NOT be treated as unchanged (needs a textured piece)
    import numpy as np
    grid_y, grid_x = np.mgrid[0:40, 0:40]
    textured = Image.fromarray(np.dstack([
        (grid_x * 6 % 256).astype(np.uint8),
        (grid_y * 6 % 256).astype(np.uint8),
        ((grid_x + grid_y) * 3 % 256).astype(np.uint8),
        np.full((40, 40), 255, np.uint8),
    ]).astype(np.uint8), "RGBA")
    mosaic_src = textured.copy()
    box = mosaic_src.crop((10, 10, 26, 26))
    mosaic_src.paste(box.resize((4, 4), Image.Resampling.NEAREST).resize(box.size, Image.Resampling.NEAREST), (10, 10))
    check("digests: a mosaic edit changes the digest",
          atlas_mod.piece_digest(textured) != atlas_mod.piece_digest(mosaic_src))
    check("digests: a mosaic edit is not near-identical",
          not atlas_mod.pieces_near_identical(textured, mosaic_src))

    partial = dict(digests)
    partial.pop(next(iter(partial)))
    stats = atlas_mod.compare_piece_digests(digests, partial, regions, regions)
    check("digests: pieces missing in the other build are counted", stats.missing == 1, stats.to_dict())

    extra = dict(digests)
    extra["ghost"] = "0" * 32
    stats = atlas_mod.compare_piece_digests(digests, extra, regions, regions)
    check("digests: extra pieces are counted in the denominator",
          stats.extra == 1 and stats.total == len(digests) + 1, stats.to_dict())


def read_config_paths():
    paths = {}
    config_path = os.path.join(PROJECT, "config.ini")
    if not os.path.exists(config_path):
        return paths
    section = None
    with open(config_path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
            elif "=" in line and section == "path":
                key, value = line.split("=", 1)
                paths[key.strip()] = value.strip()
    return paths


def test_real_data():
    paths = read_config_paths()
    orig_root = paths.get("original")
    mod_root = paths.get("modified")
    if not orig_root or not mod_root or not os.path.isdir(orig_root) or not os.path.isdir(mod_root):
        print("  SKIP  real data (config.ini paths unavailable)")
        return

    orig_atlases = atlas_mod.find_atlas_files(orig_root)
    mod_atlases = atlas_mod.find_atlas_files(mod_root)
    print(f"  info  {len(orig_atlases)} .atlas in original, {len(mod_atlases)} in modified")
    check("real: atlases found in both folders", bool(orig_atlases) and bool(mod_atlases))
    if not orig_atlases or not mod_atlases:
        return

    bad_fit = []
    total_regions = 0
    parsed_cache = {}
    for root, atlas_files in ((orig_root, orig_atlases), (mod_root, mod_atlases)):
        for atlas_path in atlas_files:
            parsed = atlas_mod.parse_atlas(atlas_path)
            parsed_cache[(root, os.path.basename(atlas_path))] = parsed
            for page in parsed.pages:
                for region in page.regions:
                    total_regions += 1
                    if page.width and page.height and (
                        region.x + region.packed_width > page.width or region.y + region.packed_height > page.height
                    ):
                        bad_fit.append((os.path.basename(atlas_path), region.name, region.rect))
    print(f"  info  {total_regions} regions parsed, {len(bad_fit)} outside their page")
    check("real: every region fits inside its page", not bad_fit, bad_fit[:3])

    biggest = max(orig_atlases, key=os.path.getsize)
    parsed = atlas_mod.parse_atlas(biggest)
    page = parsed.pages[0]
    image = atlas_mod.load_page_image(parsed.page_path(page))
    check(f"real: page image readable ({os.path.basename(page.name)})", image is not None)
    if image is None:
        return
    copy = image.copy()
    for region in page.regions:
        atlas_mod.composite_region(copy, atlas_mod.unpack_region(image, region), region,
                                   source_premultiplied=True)
    check(f"real: round-trip identity on {os.path.basename(biggest)}", images_equal(image, copy))

    # pick the atlas that changed the most between the two builds
    best_name, best_pairs, best_moved = None, [], 0
    for name, orig_parsed in parsed_cache.items():
        if name[0] != orig_root or (mod_root, name[1]) not in parsed_cache:
            continue
        mod_parsed = parsed_cache[(mod_root, name[1])]
        orig_page, mod_page = orig_parsed.pages[0], mod_parsed.pages[0]
        mod_by_name = {atlas_mod.region_key(r.name): r for r in mod_page.regions}
        pairs, moved = [], 0
        for region in orig_page.regions:
            twin = mod_by_name.get(atlas_mod.region_key(region.name))
            if twin is None or (region.width, region.height) != (twin.width, twin.height):
                continue
            if (region.x, region.y, region.packed_width, region.packed_height, region.rotate) != \
               (twin.x, twin.y, twin.packed_width, twin.packed_height, twin.rotate):
                moved += 1
                pairs.append((region, twin))
        if moved > best_moved:
            best_name, best_pairs, best_moved = name[1], pairs, moved
    print(f"  info  {best_name}: {best_moved} regions moved/rotated between builds "
          f"(same sprite size)")
    check("real: a changed atlas candidate exists", best_name is not None and bool(best_pairs))
    if best_name is None or not best_pairs:
        return

    orig_parsed = parsed_cache[(orig_root, best_name)]
    mod_parsed = parsed_cache[(mod_root, best_name)]
    orig_page, mod_page = orig_parsed.pages[0], mod_parsed.pages[0]
    orig_image = atlas_mod.load_page_image(orig_parsed.page_path(orig_page))
    mod_image = atlas_mod.load_page_image(mod_parsed.page_path(mod_page))
    check("real: both pages readable for write-back", orig_image is not None and mod_image is not None)
    if orig_image is None or mod_image is None:
        return
    updates = [(region, atlas_mod.unpack_region(mod_image, twin)) for region, twin in best_pairs[:40]]
    out_path = os.path.join(tempfile.gettempdir(), f"atlas_writeback_{os.path.splitext(best_name)[0]}.png")
    ok = atlas_mod.write_page_regions(orig_parsed.page_path(orig_page), updates, out_path=out_path,
                                      source_premultiplied=mod_page.pma)
    check("real: write modified regions into the original page", ok, out_path)
    if not ok or not updates:
        return

    written = atlas_mod.load_page_image(out_path)
    check("real: written page re-readable", written is not None, out_path)
    if written is None:
        return
    pairs = best_pairs[:40]
    exact = sum(1 for region, twin in pairs
                if images_equal(atlas_mod.unpack_region(mod_image, twin), atlas_mod.unpack_region(written, region)))
    check("real: written regions read back pixel-identically", exact == len(updates), f"{exact}/{len(updates)}")

    replaced = {region.name for region, _ in updates}
    untouched_ok = all(
        images_equal(atlas_mod.unpack_region(written, region), atlas_mod.unpack_region(orig_image, region))
        for region in orig_page.regions if region.name not in replaced
    )
    check("real: regions not replaced keep the original pixels", untouched_ok)
    print(f"  info  written page: {out_path}")


def main():
    fixture = tempfile.mkdtemp(prefix="atlas_fixture_")
    try:
        build_fixture(fixture)
        print("== parser ==")
        test_parser(fixture)
        print("== transforms ==")
        test_transforms(fixture)
        print("== write back ==")
        test_write_back(fixture)
        print("== trim normalization ==")
        test_trim_normalization(fixture)
        print("== page digests ==")
        test_page_digests(fixture)
        print("== real data ==")
        test_real_data()
    finally:
        shutil.rmtree(fixture, ignore_errors=True)

    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print("FAILED:", FAILURES)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
