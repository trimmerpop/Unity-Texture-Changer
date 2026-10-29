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
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from PIL import Image, ImageChops

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


def composite_region(page_image: Image.Image, piece: Image.Image, region: AtlasRegion,
                     source_premultiplied: bool = False) -> None:
    """Writes piece into page_image at the region's original rect (in place).

    source_premultiplied tells whether the piece already matches the page's alpha
    convention: region images extracted by this module keep the page pixels as-is
    (so pass True), while plain replacement art is straight alpha and gets
    premultiplied for `pma` pages.
    """
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
            composite_region(page_image, piece, region, source_premultiplied=premultiplied)
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


def group_updates_by_page(updates: Iterable[Tuple[AtlasRegion, str]]) -> Dict[str, List[Tuple[AtlasRegion, str]]]:
    """Groups (region, piece_path) updates per page image name."""
    grouped: Dict[str, List[Tuple[AtlasRegion, str]]] = {}
    for region, piece_path in updates:
        grouped.setdefault(region.page, []).append((region, piece_path))
    return grouped
