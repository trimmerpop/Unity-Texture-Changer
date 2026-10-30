"""Atlas (.atlas) page support for Spine / Live2D style texture pages.

One page image packs many small sprites and a plain-text .atlas file describes where
each region lives (`bounds`/`xy`, `offsets`, `rotate`, `pma`, `scale`). Region level
access has two big benefits for this app:

* comparison: a 4000x3300 sheet is useless for similarity matching, a 120x290 region
  is not, and the region name is a near-unique key across builds;
* writing: replacements are composited back into the **original** page at the
  **original** rect/rotation, so a mod whose atlas was repacked differently (other
  positions, other rotations, other page sizes) still lands exactly where the
  original build expects it.

The format is the libGDX/Spine text atlas. Both indented and non-indented variants
are accepted, because game builds contain both:

    Hina.png                    10p.png
    \tsize: 2045, 1935          size:3969,3377
    \tpma: true                 pma:true
    L arm                       BackgroundForImport
    \tbounds: 800, 1262, 186, 262   bounds:3094,365,40,40
    \trotate: 90                L bang
                                bounds:3246,377,168,357

`rotate: 90` means the region is stored rotated 90 degrees counter-clockwise inside
the page, so it is restored with ROTATE_270 (a clockwise quarter turn). This was
verified against real builds: unpacking orig/mod with that direction makes the same
region pixel-identical (MSE 0.0) in 142 of 147 rotation-mismatch cases.
"""
import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from PIL import Image, ImageChops
import numpy as np

PAGE_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tga")
_PAGE_KEY_RE = re.compile(r"^([A-Za-z]+)\s*[:=]\s*(.*)$")

# rotate (degrees inside the page) -> transform that restores the upright sprite
_UNPACK_TRANSFORM = {
    90: Image.Transpose.ROTATE_270,   # stored CCW 90 -> rotate CW 90 to restore
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}
# inverse of _UNPACK_TRANSFORM: upright piece -> orientation used inside the page
_PACK_TRANSFORM = {
    90: Image.Transpose.ROTATE_90,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_270,
}


@dataclass
class AtlasRegion:
    """One sprite inside an atlas page."""
    name: str
    page: str
    x: int = 0
    y: int = 0
    width: int = 0           # upright sprite size (before trimming)
    height: int = 0
    rotate: int = 0          # 0 / 90 / 180 / 270 degrees inside the page
    index: int = -1
    offset_x: int = 0        # trim offset (Spine "offsets")
    offset_y: int = 0
    orig_width: int = 0      # untrimmed size
    orig_height: int = 0
    pma: bool = False        # page uses premultiplied alpha
    scale: float = 1.0

    @property
    def packed_width(self) -> int:
        """Width of the footprint on the page (swapped when rotated by 90/270)."""
        return self.height if self.rotate in (90, 270) else self.width

    @property
    def packed_height(self) -> int:
        return self.width if self.rotate in (90, 270) else self.height

    @property
    def rect(self) -> Tuple[int, int, int, int]:
        return (self.x, self.y, self.packed_width, self.packed_height)

    def to_metadata(self, atlas_path: str = "", page_path: str = "") -> dict:
        """JSON-friendly description stored next to the extracted region image."""
        return {
            "kind": "atlas",
            "region": self.name,
            "atlas_file": atlas_path,
            "page_file": page_path or self.page,
            "rect": list(self.rect),
            "sprite_size": [self.width, self.height],
            "rotate": self.rotate,
            "index": self.index,
            "offset": [self.offset_x, self.offset_y],
            "orig_size": [self.orig_width, self.orig_height],
            "pma": self.pma,
            "scale": self.scale,
        }


@dataclass
class AtlasPage:
    name: str
    width: int = 0
    height: int = 0
    pma: bool = False
    scale: float = 1.0
    format: str = ""
    regions: List[AtlasRegion] = field(default_factory=list)


@dataclass
class AtlasFile:
    path: str
    pages: List[AtlasPage] = field(default_factory=list)

    @property
    def regions(self) -> List[AtlasRegion]:
        return [r for page in self.pages for r in page.regions]

    def page_path(self, page: AtlasPage) -> str:
        return os.path.join(os.path.dirname(self.path), page.name)


def _parse_int_list(value: str) -> List[int]:
    return [int(part.strip()) for part in value.split(",") if part.strip()]


def _rotate_value(value: str) -> int:
    low = value.strip().lower()
    if low in ("true", "90"):
        return 90
    if low in ("180", "270"):
        return int(low)
    return 0


def parse_atlas(path: str) -> AtlasFile:
    """Parses a libGDX/Spine style .atlas file (indented or flat key layout)."""
    atlas = AtlasFile(path=path)
    page: Optional[AtlasPage] = None
    region: Optional[AtlasRegion] = None
    base_dir = os.path.dirname(path)

    with open(path, encoding="utf-8", errors="replace") as handle:
        text = handle.read().lstrip("\ufeff")

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _PAGE_KEY_RE.match(line)
        if match:
            key, value = match.group(1).lower(), match.group(2).strip()
            if page is None:
                continue
            if key == "size" and region is None:
                dims = _parse_int_list(value)
                if len(dims) >= 2:
                    page.width, page.height = dims[0], dims[1]
                continue
            if region is None:
                if key == "pma":
                    page.pma = value.lower() == "true"
                elif key == "scale":
                    try: page.scale = float(value)
                    except ValueError: pass
                elif key == "format":
                    page.format = value
                continue
            try:
                if key == "bounds":
                    nums = _parse_int_list(value)
                    if len(nums) >= 4:
                        region.x, region.y, region.width, region.height = nums[:4]
                elif key == "xy":
                    nums = _parse_int_list(value)
                    if len(nums) >= 2:
                        region.x, region.y = nums[0], nums[1]
                elif key == "size":
                    nums = _parse_int_list(value)
                    if len(nums) >= 2:
                        region.width, region.height = nums[0], nums[1]
                elif key == "offsets":
                    nums = _parse_int_list(value)
                    if len(nums) >= 4:
                        region.offset_x, region.offset_y = nums[0], nums[1]
                        region.orig_width, region.orig_height = nums[2], nums[3]
                elif key == "orig":
                    nums = _parse_int_list(value)
                    if len(nums) >= 2:
                        region.orig_width, region.orig_height = nums[0], nums[1]
                elif key == "index":
                    region.index = int(value)
                elif key == "rotate":
                    region.rotate = _rotate_value(value)
            except ValueError:
                continue
            continue

        # Non-key line: a page image name or a region name.
        is_page_image = line.lower().endswith(PAGE_IMAGE_EXTS) and (
            os.path.exists(os.path.join(base_dir, line)) or page is None or page.regions or page.width
        )
        if is_page_image:
            page = AtlasPage(name=line)
            atlas.pages.append(page)
            region = None
        elif page is not None:
            region = AtlasRegion(name=line, page=page.name, pma=page.pma, scale=page.scale)
            page.regions.append(region)

    for atlas_page in atlas.pages:
        for item in atlas_page.regions:
            item.pma = atlas_page.pma
            item.scale = atlas_page.scale
            _fit_region_to_page(atlas_page, item)
    return atlas


def _fit_region_to_page(page: AtlasPage, region: AtlasRegion) -> None:
    """Falls back to the swapped size when a legacy atlas stores packed dimensions.

    Legacy spines (`xy` + `size` + `rotate: true`) sometimes describe the already
    rotated size. Rather than trusting one convention blindly, the footprint is
    checked against the page and the interpretation that fits is kept.
    """
    if not page.width or not page.height or not region.width or not region.height:
        return
    fits_upright = region.x + region.packed_width <= page.width and region.y + region.packed_height <= page.height
    if fits_upright:
        return
    if region.rotate in (90, 270):
        swapped_fits = region.x + region.width <= page.width and region.y + region.height <= page.height
        if swapped_fits:
            region.width, region.height = region.height, region.width


def find_atlas_files(root: str, recursive: bool = True) -> List[str]:
    """Lists .atlas files below root (sorted, stable order)."""
    found: List[str] = []
    if not root or not os.path.isdir(root):
        return found
    if recursive:
        for current, _dirs, files in os.walk(root):
            for name in files:
                if name.lower().endswith(".atlas"):
                    found.append(os.path.join(current, name))
    else:
        for name in os.listdir(root):
            if name.lower().endswith(".atlas"):
                found.append(os.path.join(root, name))
    return sorted(found)


def premultiply_alpha(image: Image.Image) -> Image.Image:
    """Premultiplies RGB by alpha (atlas pages are often stored with `pma: true`)."""
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    channels = image.split()
    alpha = channels[3]
    # ImageChops.multiply works per channel, which keeps this pure PIL
    premultiplied = [ImageChops.multiply(channel, alpha) for channel in channels[:3]]
    return Image.merge("RGBA", (*premultiplied, alpha))


def unpack_region(page_image: Image.Image, region: AtlasRegion) -> Image.Image:
    """Crops region out of the page image and restores its upright orientation."""
    x, y, width, height = region.rect
    piece = page_image.crop((x, y, x + width, y + height))
    transform = _UNPACK_TRANSFORM.get(region.rotate)
    if transform is not None:
        piece = piece.transpose(transform)
    return piece


def pack_region(piece: Image.Image, region: AtlasRegion, resize: bool = True) -> Image.Image:
    """Turns an upright piece back into the orientation/size stored in the page."""
    if piece.mode != "RGBA":
        piece = piece.convert("RGBA")
    target = (region.width, region.height)
    if target[0] > 0 and target[1] > 0 and piece.size != target:
        piece = piece.resize(target, Image.Resampling.LANCZOS) if resize else piece.resize(target)
    transform = _PACK_TRANSFORM.get(region.rotate)
    if transform is not None:
        piece = piece.transpose(transform)
    return piece


def alpha_bbox(image: Image.Image) -> Optional[Tuple[int, int, int, int]]:
    """Bounds of the visible pixels of a piece (None when fully transparent)."""
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    mask = np.asarray(image)[:, :, 3] > 10
    if not mask.any():
        return None
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    return int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1


def align_piece_to_region(piece: Image.Image, source: AtlasRegion,
                          target: AtlasRegion) -> Image.Image:
    """Fits a source piece into the target sprite box so the artwork overlaps.

    Both builds trim and pack the same sprite differently: the visible content can be a few
    pixels wider/shorter and its offset inside the sprite box can differ. Stretching the raw
    crop to the target rect then smears the art, so:
      - the resolution follows the ORIGINAL: the content is scaled only by the ratio of the
        untrimmed sprite sizes (usually 1.0, so no resampling at all),
      - the content is centred on the target's content, which removes the offset difference
        and makes the shapes overlap,
      - everything the art does not cover stays transparent (transparent art stays
        transparent instead of being filled with a background).
    Returns an image with the target's packed footprint, ready for pasting.
    """
    if piece.mode != "RGBA":
        piece = piece.convert("RGBA")
    if target.width <= 0 or target.height <= 0:
        return pack_region(piece, target)

    # Fast path: both builds describe the sprite identically -> keep the pixels untouched
    # (no trim/resample at all, so an unchanged piece stays byte identical). A different
    # rotation is fine: pack_region rotates the piece to the target orientation.
    if ((source.width, source.height) == (target.width, target.height)
            and (source.offset_x, source.offset_y) == (target.offset_x, target.offset_y)
            and (source.orig_width, source.orig_height) == (target.orig_width, target.orig_height)):
        return pack_region(piece, target)

    bounds = alpha_bbox(piece)
    art = piece.crop(bounds) if bounds else piece

    scale_x = scale_y = 1.0
    if source.orig_width > 0 and target.orig_width > 0 and source.orig_height > 0 and target.orig_height > 0:
        scale_x = target.orig_width / source.orig_width
        scale_y = target.orig_height / source.orig_height
    # Guard against nonsense metadata (missing orig sizes, wildly different builds)
    if not (0.125 <= scale_x <= 8 and 0.125 <= scale_y <= 8):
        scale_x = scale_y = 1.0

    width = max(1, int(round(art.width * scale_x)))
    height = max(1, int(round(art.height * scale_y)))
    if (width, height) != art.size:
        art = art.resize((width, height), Image.Resampling.LANCZOS)
    # safety net: when the art ended up far larger than the sprite, fall back to fitting it
    if width > target.width * 2 or height > target.height * 2:
        return pack_region(piece, target)

    canvas = Image.new("RGBA", (target.width, target.height), (0, 0, 0, 0))
    canvas.paste(art, ((target.width - width) // 2, (target.height - height) // 2))

    transform = _PACK_TRANSFORM.get(target.rotate)
    if transform is not None:
        canvas = canvas.transpose(transform)
    if canvas.size != (target.packed_width, target.packed_height):
        canvas = canvas.resize((target.packed_width, target.packed_height),
                               Image.Resampling.LANCZOS)
    return canvas


def composite_region(page_image: Image.Image, piece: Image.Image, region: AtlasRegion,
                     source_premultiplied: bool = False,
                     source_region: Optional[AtlasRegion] = None) -> None:
    """Writes piece into page_image at the region's original rect (in place).

    source_premultiplied tells whether the piece already matches the page's alpha
    convention: region images extracted by this module keep the page pixels as-is
    (so pass True), while plain replacement art is straight alpha and gets
    premultiplied for `pma` pages.

    source_region is the region the piece was taken from. When it is given, the artwork is
    aligned into the target box (see align_piece_to_region) so a source build with another
    trim/offset still overlaps the original instead of being stretched.
    """
    if source_region is not None:
        packed = align_piece_to_region(piece, source_region, region)
    else:
        packed = pack_region(piece, region)
    if region.pma and not source_premultiplied:
        packed = premultiply_alpha(packed)
    if packed.size != (region.packed_width, region.packed_height):
        packed = packed.resize((region.packed_width, region.packed_height), Image.Resampling.LANCZOS)
    # plain paste on purpose: the piece covers the whole region rect, so its alpha must
    # replace the page pixels instead of being blended into them
    page_image.paste(packed, (region.x, region.y))


def region_from_metadata(meta: dict) -> Optional[AtlasRegion]:
    """Rebuilds an AtlasRegion from a metadata.json "atlas" block.

    Used when applying, so an original region can be restored even if its .atlas file
    is no longer readable (rect/rotation/sprite size/pma are all in the metadata).
    """
    if not meta:
        return None
    rect = meta.get("rect") or []
    sprite = meta.get("sprite_size") or []
    if len(rect) < 4:
        return None
    region = AtlasRegion(name=meta.get("region", ""), page=os.path.basename(meta.get("page_file", "") or ""),
                         x=int(rect[0]), y=int(rect[1]), rotate=int(meta.get("rotate", 0)),
                         index=int(meta.get("index", -1)), pma=bool(meta.get("pma", False)),
                         scale=float(meta.get("scale", 1.0)))
    # metadata stores the page footprint; the sprite size is whatever was recorded
    if len(sprite) >= 2 and sprite[0] and sprite[1]:
        region.width, region.height = int(sprite[0]), int(sprite[1])
    elif region.rotate in (90, 270):
        region.width, region.height = int(rect[3]), int(rect[2])
    else:
        region.width, region.height = int(rect[2]), int(rect[3])
    offset = meta.get("offset") or []
    if len(offset) >= 2:
        region.offset_x, region.offset_y = int(offset[0]), int(offset[1])
    orig = meta.get("orig_size") or []
    if len(orig) >= 2:
        region.orig_width, region.orig_height = int(orig[0]), int(orig[1])
    return region


def regions_from_metadata(entries) -> List[AtlasRegion]:
    """Rebuilds page regions from inline metadata entries (Unity sprite pages).

    A page taken from a Unity Texture2D has no .atlas file: its rects are stored in the
    metadata itself, so the page logic can work without one.
    """
    regions: List[AtlasRegion] = []
    for entry in entries or []:
        region = region_from_metadata(entry)
        if region is not None:
            regions.append(region)
    return regions


def find_region(atlas: AtlasFile, region_name: str,
                page_name: str = "") -> Optional[AtlasRegion]:
    """Looks a region up by name (optionally restricted to one page)."""
    key = region_key(region_name)
    page_key = os.path.basename(page_name).lower() if page_name else ""
    for page in atlas.pages:
        if page_key and os.path.basename(page.name).lower() != page_key:
            continue
        for region in page.regions:
            if region_key(region.name) == key:
                return region
    return None


AtlasUpdate = Union[Tuple[AtlasRegion, Image.Image], Tuple[AtlasRegion, Image.Image, bool]]


def write_page_regions(page_path: str, updates: Sequence[AtlasUpdate],
                       out_path: Optional[str] = None, source_premultiplied: bool = False) -> bool:
    """Composites every region update into the page and saves it.

    Each update is (region, piece) or (region, piece, source_premultiplied), so callers
    can mix pieces that already match the page's alpha convention with straight-alpha
    art in one write. The page is written through a temporary file, so a failure never
    leaves a broken texture behind, and regions that are not updated keep their pixels.
    """
    out_path = out_path or page_path
    tmp_path = out_path + ".tmp"
    try:
        with Image.open(page_path) as handle:
            page_image = handle.convert("RGBA")
        for update in updates:
            region, piece = update[0], update[1]
            premultiplied = bool(update[2]) if len(update) > 2 else source_premultiplied
            # optional 4th element: the region the piece was taken from (alignment)
            from_region = update[3] if len(update) > 3 else None
            composite_region(page_image, piece, region, source_premultiplied=premultiplied,
                             source_region=from_region)
        page_image.save(tmp_path, format="PNG")
        os.replace(tmp_path, out_path)
        return True
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False


def region_key(name: str) -> str:
    """Normalises a region name so builds with cosmetic differences still match."""
    return re.sub(r"\s+", " ", (name or "")).strip().lower()


PIECE_TOLERANCE = {"mae": 2.0, "changed_pct": 1.0, "peak": 48.0}
"""Pixel tolerance for "same piece" when the trimmed md5 differs.

Calibrated on real builds: unchanged pieces are byte identical, real edits (censoring,
mosaic, redraw) show a peak difference far above 48, while a re-export with another
compression stays below it.
"""


def trim_alpha(image: Image.Image) -> Image.Image:
    """Crops the fully transparent border (Pillow image)."""
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    mask = np.asarray(image)[:, :, 3] > 10
    if not mask.any():
        return image
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    return image.crop((int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1))


def piece_digest(image: Image.Image) -> str:
    """md5 of the visible (alpha trimmed) pixels of a piece.

    Trimming first is what makes the digest meaningful: the same sprite packed with a
    different amount of transparent padding must still compare equal.
    """
    return hashlib.md5(np.ascontiguousarray(np.asarray(trim_alpha(image))).tobytes()).hexdigest()


def piece_keys(regions: Sequence[AtlasRegion]) -> List[str]:
    """Stable key per region of ONE page ("name", plus "#2" when a name repeats).

    Region names are unique inside a page in practice; the suffix keeps two sprites that
    really share a name apart instead of silently dropping one of them.
    """
    keys: List[str] = []
    seen: Dict[str, int] = {}
    for region in regions:
        name = region_key(region.name)
        count = seen.get(name, 0) + 1
        seen[name] = count
        keys.append(name if count == 1 else f"{name}#{count}")
    return keys


def page_piece_digests(page_image: Image.Image, regions: Sequence[AtlasRegion]) -> Dict[str, str]:
    """Maps piece key -> trimmed md5 digest for every piece of a page."""
    digests: Dict[str, str] = {}
    for key, region in zip(piece_keys(regions), regions):
        if not region.width or not region.height:
            continue
        try:
            digests[key] = piece_digest(unpack_region(page_image, region))
        except Exception:
            continue
    return digests


def pieces_near_identical(a: Image.Image, b: Image.Image, tolerance: Optional[dict] = None) -> bool:
    """Pixel tolerance check for pieces whose trimmed digest differs.

    Absorbs re-encode noise but not real edits. Every colour channel is compared (not just
    the luminance): a re-colour with the same brightness, which is a common edit, still has a
    large peak difference and is therefore reported as changed.
    """
    tol = tolerance or PIECE_TOLERANCE
    ta = np.asarray(trim_alpha(a))
    tb = np.asarray(trim_alpha(b))
    if ta.shape != tb.shape:
        return False
    diff = np.abs(ta[:, :, :3].astype(np.int16) - tb[:, :, :3].astype(np.int16))
    per_pixel = diff.max(axis=2)
    return (float(diff.mean()) <= tol["mae"]
            and float((per_pixel > 8).mean() * 100) <= tol["changed_pct"]
            and float(per_pixel.max()) <= tol["peak"])


@dataclass
class PageStats:
    """Piece level comparison of two atlas pages."""
    pieces: int = 0
    identical: int = 0
    similar: int = 0
    changed: int = 0
    missing: int = 0
    extra: int = 0
    moved: int = 0
    rotated: int = 0
    trimmed: int = 0

    @property
    def same(self) -> int:
        """Pieces counted as the same sprite (exact or near identical)."""
        return self.identical + self.similar

    @property
    def total(self) -> int:
        """Denominator for the overlap ratio (the bigger of both pages)."""
        return max(self.pieces + self.missing, self.pieces + self.extra)

    @property
    def ratio(self) -> float:
        """same pieces / total pieces, i.e. how much of the page is unchanged."""
        if self.total <= 0:
            return 0.0
        return self.same / self.total

    def to_dict(self) -> dict:
        return {"pieces": self.pieces, "identical": self.identical, "similar": self.similar,
                "same": self.same, "changed": self.changed,
                "missing": self.missing, "extra": self.extra, "moved": self.moved,
                "rotated": self.rotated, "trimmed": self.trimmed,
                "ratio": round(self.ratio, 4)}

    def summary(self) -> str:
        """Short text for the Atlas column: percentage plus (same/total)."""
        return f"{self.ratio * 100:.1f}% ({self.same}/{self.total})"

    def detail(self) -> str:
        """Tooltip text."""
        lines = [f"same pieces: {self.same} / {self.total} ({self.ratio * 100:.1f}%)",
                 f"  identical pixels: {self.identical}",
                 f"  near-identical (re-encoded): {self.similar}",
                 f"  changed: {self.changed}"]
        if self.missing:
            lines.append(f"only in original: {self.missing}")
        if self.extra:
            lines.append(f"only in modified: {self.extra}")
        if self.moved or self.rotated or self.trimmed:
            lines.append(f"re-packed: moved {self.moved}, rotated {self.rotated}, trimmed {self.trimmed}")
        return "\n".join(lines)


def compare_piece_digests(orig_digests: Dict[str, str], mod_digests: Dict[str, str],
                          orig_regions: Sequence[AtlasRegion] = (),
                          mod_regions: Sequence[AtlasRegion] = ()) -> PageStats:
    """Fast pass: compares two pages by their trimmed md5 digests (no page decoding)."""
    stats = PageStats()
    orig_by = dict(zip(piece_keys(orig_regions), orig_regions))
    mod_by = dict(zip(piece_keys(mod_regions), mod_regions))
    for key, digest in orig_digests.items():
        twin = mod_digests.get(key)
        if twin is None:
            stats.missing += 1
            continue
        stats.pieces += 1
        if digest == twin:
            stats.identical += 1
        else:
            stats.changed += 1
        a, b = orig_by.get(key), mod_by.get(key)
        if a and b:
            if (a.width, a.height) != (b.width, b.height):
                stats.trimmed += 1
            elif a.rotate != b.rotate:
                stats.rotated += 1
            elif (a.x, a.y) != (b.x, b.y):
                stats.moved += 1
    for key in mod_digests:
        if key not in orig_digests:
            stats.extra += 1
    return stats


def compare_pages_pixels(orig_page_image: Image.Image, mod_page_image: Image.Image,
                         orig_regions: Sequence[AtlasRegion], mod_regions: Sequence[AtlasRegion],
                         tolerance: Optional[dict] = None) -> PageStats:
    """Exact pass: decodes both pages and classifies every piece by its visible pixels.

    identical = trimmed pixels equal, similar = within the re-encode tolerance,
    changed = a real edit (a small mosaic patch also lands here). Use it when the fast
    digest pass reported differences, so re-encoded art is not counted as changed.
    """
    stats = PageStats()
    mod_by = dict(zip(piece_keys(mod_regions), mod_regions))
    orig_by = dict(zip(piece_keys(orig_regions), orig_regions))
    for key, region in orig_by.items():
        twin = mod_by.get(key)
        if twin is None:
            stats.missing += 1
            continue
        stats.pieces += 1
        try:
            piece_a = unpack_region(orig_page_image, region)
            piece_b = unpack_region(mod_page_image, twin)
        except Exception:
            stats.changed += 1
            continue
        if piece_digest(piece_a) == piece_digest(piece_b):
            stats.identical += 1
        elif pieces_near_identical(piece_a, piece_b, tolerance):
            stats.similar += 1
        else:
            stats.changed += 1
        if (region.width, region.height) != (twin.width, twin.height):
            stats.trimmed += 1
        elif region.rotate != twin.rotate:
            stats.rotated += 1
        elif (region.x, region.y) != (twin.x, twin.y):
            stats.moved += 1
    for key in mod_by:
        if key not in orig_by:
            stats.extra += 1
    return stats


def load_page_image(page_path: str) -> Optional[Image.Image]:
    """Loads an atlas page as RGBA, or None when it cannot be read."""
    try:
        with Image.open(page_path) as handle:
            return handle.convert("RGBA")
    except Exception:
        return None


def extract_atlas_regions(atlas_path: str, out_dir: str, log_callback=None) -> List[dict]:
    """Extracts every region of one .atlas file into out_dir as PNGs.

    Returns texture metadata entries (same shape the Unity extractor produces, with an
    extra "atlas" block), ready to be stored in metadata.json by the caller.
    """
    def log(message):
        if log_callback:
            log_callback(message)

    entries: List[dict] = []
    try:
        atlas = parse_atlas(atlas_path)
    except Exception as error:
        log(f"  [ATLAS] Failed to parse {os.path.basename(atlas_path)}: {error}")
        return entries

    if not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    for page in atlas.pages:
        page_path = atlas.page_path(page)
        page_image = load_page_image(page_path)
        if page_image is None:
            log(f"  [ATLAS] Page image missing/unreadable: {page.name}")
            continue
        if page.width and page.height and (page_image.width, page_image.height) != (page.width, page.height):
            log(f"  [ATLAS] Page {page.name} is {page_image.width}x{page_image.height}, atlas says "
                f"{page.width}x{page.height}; using the image size")
            page.width, page.height = page_image.width, page_image.height

        for region in page.regions:
            if not region.width or not region.height:
                continue
            try:
                piece = unpack_region(page_image, region)
            except Exception as error:
                log(f"  [ATLAS] Could not crop {region.name}: {error}")
                continue
            safe_name = re.sub(r'[\\/:*?"<>|]', "_", region.name).strip() or "region"
            file_name = f"{safe_name}_{region.index if region.index >= 0 else region.x}_{region.y}.png"
            save_path = os.path.join(out_dir, file_name)
            try:
                piece.save(save_path, format="PNG")
            except Exception as error:
                log(f"  [ATLAS] Could not save {region.name}: {error}")
                continue
            entries.append({
                "name": region.name,
                "file": file_name,
                "save_path": save_path,
                "width": piece.width,
                "height": piece.height,
                "size": os.path.getsize(save_path),
                "source_asset": atlas_path,
                "atlas": region.to_metadata(atlas_path, page_path),
            })
        log(f"  [ATLAS] {os.path.basename(atlas_path)}: {len(page.regions)} regions on {page.name}")
    return entries


def extract_atlas_pages(atlas_path: str, out_dir: str, preview_max: int = 1024,
                        log_callback=None, used_names: Optional[set] = None) -> List[dict]:
    """One metadata entry per atlas page (no per-piece files).

    The page is offered as a single item, and the pieces are compared internally through
    their digests: `similarity = identical pieces / total pieces`, computed by
    compare_piece_digests() without decoding the pages again. The item name is the page
    stem ("3p", "3p_2"), falling back to "<atlas>__<page>" when the folder already has a page
    with that name so two atlases cannot produce ambiguous rows.
    """
    def log(message):
        if log_callback:
            log_callback(message)

    entries: List[dict] = []
    try:
        atlas = parse_atlas(atlas_path)
    except Exception as error:
        log(f"  [ATLAS] Failed to parse {os.path.basename(atlas_path)}: {error}")
        return entries
    if not atlas.pages:
        return entries
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    stem = os.path.splitext(os.path.basename(atlas_path))[0]
    page_count = len(atlas.pages)
    for page_number, page in enumerate(atlas.pages, start=1):
        page_path = atlas.page_path(page)
        page_image = load_page_image(page_path)
        if page_image is None:
            log(f"  [ATLAS] Page image missing/unreadable: {page.name}")
            continue
        page.width, page.height = page_image.size
        digests = page_piece_digests(page_image, page.regions)

        preview = page_image
        if max(preview.size) > preview_max:
            preview = preview.copy()
            preview.thumbnail((preview_max, preview_max), Image.Resampling.BOX)
        page_stem = os.path.splitext(os.path.basename(page.name))[0]
        safe_stem = re.sub(r'[\\/:*?"<>|]', "_", page_stem).strip() or "page"
        if used_names is None:
            name = safe_stem
        else:
            name = safe_stem if safe_stem.lower() not in used_names else f"{stem}__{safe_stem}"
            used_names.add(name.lower())
        file_name = f"{re.sub(r'[\\/:*?\"<>|]', '_', name)}.png"
        save_path = os.path.join(out_dir, file_name)
        try:
            preview.save(save_path, format="PNG")
        except Exception as error:
            log(f"  [ATLAS] Could not write preview for {page.name}: {error}")
            continue
        entries.append({
            "name": name,
            "file": file_name,
            "save_path": save_path,
            "width": page.width,
            "height": page.height,
            # the item's Size column shows the real page file, not the small preview
            "size": os.path.getsize(page_path) if os.path.exists(page_path) else os.path.getsize(save_path),
            "source_asset": atlas_path,
            "atlas_page": {
                "kind": "atlas-page",
                "atlas_file": atlas_path,
                "page_file": page_path,
                "page_name": page.name,
                "page_number": page_number,   # 2 of 3 for "hina_cg1_2.png"
                "page_count": page_count,
                "page_size": [page.width, page.height],
                "pieces": len(digests),
                "pma": page.pma,
                "scale": page.scale,
                "digests": digests,
            },
        })
        log(f"  [ATLAS] {os.path.basename(atlas_path)}: page {page.name} "
            f"({page_number}/{page_count}) -> {len(digests)} pieces (1 item)")
    return entries


def group_updates_by_page(updates: Iterable[Tuple[AtlasRegion, str]]) -> Dict[str, List[Tuple[AtlasRegion, str]]]:
    """Groups (region, piece_path) updates per page image name."""
    grouped: Dict[str, List[Tuple[AtlasRegion, str]]] = {}
    for region, piece_path in updates:
        grouped.setdefault(region.page, []).append((region, piece_path))
    return grouped
