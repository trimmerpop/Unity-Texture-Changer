"""Unity support for atlas pages.

Two shapes are handled, both ending up as the same ``atlas_page`` metadata the rest of the
program already understands (one item per page image, pieces compared by name):

1. Unity's own sprite atlas: ``Sprite`` objects (or ``SpriteAtlas.m_RenderDataMap`` entries)
   describe a rect inside a ``Texture2D``. The rects are converted to top-left coordinates
   (our extracted PNGs use ``Texture2D.image``, i.e. flip=True) and the packing rotation bits
   are mapped onto the region rotation the atlas module uses.
2. A Spine atlas shipped inside the build: ``TextAsset`` holding .atlas text plus the page
   images as ``Texture2D``. The text is dumped next to the extraction and parsed by the normal
   .atlas parser, so the existing page logic applies unchanged.

Nothing here writes into the game data; the apply step composites into the extracted page
image and lets the existing texture replacement put it back into the asset.
"""
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from PIL import Image

import atlas as atlas_mod

# UnityPackingRotation (settingsRaw bits 2..5)
_ROTATION_TO_REGION = {0: 0, 3: 180, 4: 90}
_FLIP_ROTATIONS = (1, 2)   # kSPRFlipHorizontal / kSPRFlipVertical


@dataclass
class SpriteSettings:
    packed: bool
    tight: bool
    rotation: int

    @classmethod
    def from_raw(cls, settings_raw) -> "SpriteSettings":
        raw = int(settings_raw or 0)
        return cls(packed=bool(raw & 1), tight=bool((raw >> 1) & 1), rotation=(raw >> 2) & 0xF)


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _rect_values(rect) -> Tuple[float, float, float, float]:
    if rect is None:
        return 0.0, 0.0, 0.0, 0.0
    return (_num(getattr(rect, "x", 0)), _num(getattr(rect, "y", 0)),
            _num(getattr(rect, "width", 0)), _num(getattr(rect, "height", 0)))


def top_left_y(rect_y: float, rect_height: float, page_height: int) -> int:
    """Unity textureRect starts at the bottom-left corner, our PNGs at the top-left."""
    return max(0, int(round(page_height - (rect_y + rect_height))))


def sprite_piece(name: str, render_data, sprite_rect, offset, page_height: int) -> Optional[dict]:
    """Turns one sprite's render data into a region entry (top-left coordinates).

    A rotated sprite is packed with swapped width/height, which is reported as the upright
    sprite size plus the rotation that restores it (same convention as the Spine parser:
    90 is restored with ROTATE_270). page_height = 0 keeps the raw bottom-up y so the flip can
    be applied once the texture is decoded (finalize_pieces).
    """
    x, y, width, height = _rect_values(getattr(render_data, "textureRect", None))
    if width <= 0 or height <= 0:
        return None
    settings = SpriteSettings.from_raw(getattr(render_data, "settingsRaw", 0))
    rotate = _ROTATION_TO_REGION.get(settings.rotation, 0) if settings.packed else 0
    flipped = settings.packed and settings.rotation in _FLIP_ROTATIONS

    packed_w, packed_h = int(round(width)), int(round(height))
    if rotate in (90, 270):
        upright_w, upright_h = packed_h, packed_w
    else:
        upright_w, upright_h = packed_w, packed_h

    top = 0 if page_height <= 0 else top_left_y(y, height, page_height)

    orig_w, orig_h = _rect_values(sprite_rect)[2:]
    if orig_w <= 0 or orig_h <= 0:
        orig_w, orig_h = float(upright_w), float(upright_h)
    off_x = _num(getattr(offset, "x", 0))
    off_y_up = _num(getattr(offset, "y", 0))
    # metadata offsets are top-down inside the untrimmed sprite, like the Spine parser
    off_y = int(round(orig_h - off_y_up - upright_h))

    entry = {
        "kind": "atlas",
        "region": name,
        "atlas_file": "",
        "page_file": "",
        "rect": [int(round(x)), int(round(y)) if page_height <= 0 else top, packed_w, packed_h],
        "sprite_size": [upright_w, upright_h],
        "rotate": rotate,
        "index": -1,
        "offset": [int(round(off_x)), off_y],
        "orig_size": [int(round(orig_w)), int(round(orig_h))],
        "pma": False,
        "scale": 1.0,
    }
    if flipped:
        entry["flipped"] = True
    return entry


def texture_path_id(pointer) -> Optional[int]:
    """path_id of a PPtr<Texture2D> (None when it points nowhere)."""
    if pointer is None:
        return None
    path_id = getattr(pointer, "path_id", None)
    if not path_id:
        path_id = getattr(pointer, "m_PathID", None)
    try:
        return int(path_id) if path_id else None
    except (TypeError, ValueError):
        return None


def pieces_from_sprite(sprite) -> Optional[Tuple[int, dict]]:
    """(texture path_id, piece entry) for one Sprite object, or None."""
    render_data = getattr(sprite, "m_RD", None)
    atlas_data = None
    atlas_pointer = getattr(sprite, "m_SpriteAtlas", None)
    if atlas_pointer is not None:
        # packed into a SpriteAtlas: the rect lives in m_RenderDataMap, not in m_RD
        try:
            atlas = atlas_pointer.deref_parse_as_object()
        except Exception:
            atlas = None
        key = getattr(sprite, "m_RenderDataKey", None)
        if atlas is not None and key is not None:
            for map_key, value in (getattr(atlas, "m_RenderDataMap", None) or []):
                if map_key == key:
                    atlas_data = value
                    break
    source = atlas_data if atlas_data is not None else render_data
    if source is None:
        return None
    pointer = getattr(source, "texture", None)
    if pointer is None:
        pointer = getattr(render_data, "texture", None)
    path_id = texture_path_id(pointer)
    if not path_id:
        return None
    name = getattr(sprite, "m_Name", "") or f"sprite_{getattr(sprite, 'path_id', 0)}"
    entry = sprite_piece(name, source, getattr(sprite, "m_Rect", None),
                         getattr(sprite, "m_Offset", None), 0)
    if entry is None:
        return None
    return path_id, entry


def scan_sprites(objects, log=None) -> Dict[int, List[dict]]:
    """Groups sprite pieces by the path_id of the texture they are packed into.

    The page height is only known once the texture is decoded, so the vertical flip of the
    rect is applied later (see finalize_pieces).
    """
    grouped: Dict[int, List[dict]] = {}
    for obj in objects:
        type_name = getattr(getattr(obj, "type", None), "name", "")
        if type_name != "Sprite":
            continue
        try:
            sprite = obj.read()
        except Exception as error:
            if log:
                log(f"  [ATLAS] Could not read a Sprite: {error}")
            continue
        result = pieces_from_sprite(sprite)
        if result is None:
            continue
        path_id, entry = result
        grouped.setdefault(path_id, []).append(entry)
    return grouped


def scan_text_assets(objects, log=None) -> List[Tuple[str, str]]:
    """[(name, text)] for TextAssets that could be Spine atlas text."""
    found: List[Tuple[str, str]] = []
    for obj in objects:
        type_name = getattr(getattr(obj, "type", None), "name", "")
        if type_name != "TextAsset":
            continue
        try:
            data = obj.read()
        except Exception:
            continue
        text = getattr(data, "m_Script", "") or getattr(data, "script", "") or ""
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        name = (getattr(data, "m_Name", "") or getattr(data, "name", "")
                or f"text_{getattr(obj, 'path_id', 0)}")
        if looks_like_spine_atlas(text) or is_spine_json(text):
            found.append((name, text))
    return found


def finalize_pieces(pieces: List[dict], page_height: int, page_file: str) -> List[dict]:
    """Flips the rects into top-left coordinates and points them at the page image."""
    fixed: List[dict] = []
    for entry in pieces:
        piece = dict(entry)
        x, raw_y, packed_w, packed_h = piece["rect"]
        piece["rect"] = [x, top_left_y(raw_y, packed_h, page_height), packed_w, packed_h]
        piece["page_file"] = page_file
        fixed.append(piece)
    return fixed


def looks_like_spine_atlas(text: str) -> bool:
    """Cheap check for .atlas text shipped as a TextAsset."""
    if not text:
        return False
    head = text.lstrip("\ufeff").splitlines()[:40]
    has_page = any(line.strip().lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
                   for line in head)
    has_keys = any(re.match(r"\s*(size|format|filter|pma|scale)\s*:", line, re.IGNORECASE)
                   for line in head)
    return has_page and has_keys


def is_spine_json(text: str) -> bool:
    """Spine .json skeleton: reported only, it carries no page rects we could reuse."""
    if not text:
        return False
    stripped = text.lstrip("\ufeff \r\n\t")
    return stripped.startswith("{") and '"skeleton"' in stripped[:4000]




def page_metadata(pieces: List[dict], page_file: str, page_size, page_name: str = "") -> dict:
    """atlas_page block for one Unity texture page (inline regions, no .atlas file)."""
    return {
        "kind": "unity-page",
        "atlas_file": "",
        "page_file": page_file,
        "page_name": page_name or os.path.basename(page_file),
        "page_size": [int(page_size[0]), int(page_size[1])],
        "pieces": len(pieces),
        "pma": False,
        "regions": pieces,
        "digests": {},
    }


def spine_page_metadata(atlas_path: str, page_file: str, page_name: str, pieces: int,
                        page_size, page_number: int = 1, page_count: int = 1) -> dict:
    """atlas_page block for a Spine page that lives inside a Unity build."""
    return {
        "kind": "unity-spine",
        "atlas_file": atlas_path,
        "page_file": page_file,
        "page_name": page_name,
        "page_number": page_number,
        "page_count": page_count,
        "page_size": [int(page_size[0]), int(page_size[1])],
        "pieces": pieces,
        "pma": False,
        "digests": {},
    }


def dump_atlas_text(output_dir: str, name: str, text: str) -> str:
    """Writes a TextAsset atlas next to the extraction so the normal parser can read it."""
    folder = os.path.join(output_dir, "atlas_assets")
    os.makedirs(folder, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "atlas"
    path = os.path.join(folder, f"{safe}.atlas")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def attach_pages(entries: List[dict], sprite_pieces: Dict[int, List[dict]],
                 atlas_texts: List[Tuple[str, str]], output_dir: str, log=None) -> int:
    """Adds atlas_page metadata to the extracted Unity texture entries.

    entries: texture entries of one asset file (name/path_id/save_path/width/height)
    sprite_pieces: path_id -> piece entries (see scan_sprites)
    atlas_texts: [(text asset name, text)] for Spine atlases found in the same file
    Returns the number of page items created.
    """
    def say(message):
        if log:
            log(message)

    by_path_id = {entry.get("path_id"): entry for entry in entries}
    by_name: Dict[str, dict] = {}
    for entry in entries:
        by_name.setdefault(str(entry.get("name", "")).lower(), entry)

    images: Dict[str, Optional[Image.Image]] = {}

    def page_image(entry) -> Optional[Image.Image]:
        path = entry.get("save_path") or ""
        if path not in images:
            images[path] = atlas_mod.load_page_image(path)
        return images[path]

    created = 0

    # 1) Unity native sprite atlas: the rects coming from Sprite objects
    for path_id, pieces in sprite_pieces.items():
        entry = by_path_id.get(path_id)
        if entry is None or entry.get("atlas_page"):
            continue
        image = page_image(entry)
        if image is None:
            continue
        regions = finalize_pieces(pieces, image.height, entry["save_path"])
        meta = page_metadata(regions, entry["save_path"], image.size,
                             page_name=entry.get("name", ""))
        meta["digests"] = atlas_mod.page_piece_digests(
            image, atlas_mod.regions_from_metadata(meta["regions"]))
        entry["atlas_page"] = meta
        created += 1
        say(f"  [ATLAS] {entry.get('name')} -> 1 page, {len(regions)} sprites "
            f"(Unity sprite atlas)")

    # 2) Spine atlas shipped as a TextAsset: its pages are ordinary textures here
    for text_name, text in atlas_texts:
        if not looks_like_spine_atlas(text):
            if is_spine_json(text):
                say(f"  [ATLAS] {text_name}: Spine .json skeleton found "
                    f"(no page rects in it, textures stay plain images)")
            continue
        atlas_path = dump_atlas_text(output_dir, text_name, text)
        try:
            parsed = atlas_mod.parse_atlas(atlas_path)
        except Exception as error:
            say(f"  [ATLAS] {text_name}: atlas text could not be parsed ({error})")
            continue
        if not parsed.pages:
            continue
        page_count = len(parsed.pages)
        for number, page in enumerate(parsed.pages, start=1):
            stem = os.path.splitext(os.path.basename(page.name))[0].lower()
            entry = by_name.get(stem)
            if entry is None or entry.get("atlas_page"):
                continue
            image = page_image(entry)
            if image is None:
                continue
            digests = atlas_mod.page_piece_digests(image, page.regions)
            meta = spine_page_metadata(atlas_path, entry["save_path"], page.name,
                                       len(digests), image.size, number, page_count)
            meta["digests"] = digests
            entry["atlas_page"] = meta
            created += 1
            say(f"  [ATLAS] {text_name}: page {page.name} ({number}/{page_count}) "
                f"-> {len(digests)} pieces")
    return created
