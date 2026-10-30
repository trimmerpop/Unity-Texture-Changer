"""Tests for the Unity atlas support (sprite rects + Spine text assets).

Run: python test_unity_atlas.py

No Unity build is required: the UnityPy objects are stubbed, and the page logic is checked on
synthetic PNG pages so rect/orientation bugs show up immediately.
"""
import os
import shutil
import sys
import tempfile

PROJECT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT)
sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

from PIL import Image

import atlas as atlas_mod
import unity_atlas

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


class Rect:
    def __init__(self, x, y, width, height):
        self.x, self.y, self.width, self.height = x, y, width, height


class Vec2:
    def __init__(self, x, y):
        self.x, self.y = x, y


class Pointer:
    def __init__(self, path_id):
        self.path_id = path_id


class RenderData:
    def __init__(self, texture_id, rect, settings_raw=0):
        self.texture = Pointer(texture_id)
        self.textureRect = rect
        self.settingsRaw = settings_raw


class Sprite:
    def __init__(self, name, path_id, texture_id, rect, settings_raw=0, sprite_rect=None,
                 offset=None, atlas=None, render_key=None):
        self.m_Name = name
        self.path_id = path_id
        self.m_RD = RenderData(texture_id, rect, settings_raw)
        self.m_Rect = sprite_rect or Rect(0, 0, rect.width, rect.height)
        self.m_Offset = offset or Vec2(0, 0)
        self.m_SpriteAtlas = atlas
        self.m_RenderDataKey = render_key


class FakeType:
    def __init__(self, name):
        self.name = name


class Object:
    def __init__(self, type_name, payload, path_id=0):
        self.type = FakeType(type_name)
        self._payload = payload
        self.path_id = path_id

    def read(self):
        return self._payload


class TextAsset:
    def __init__(self, name, text):
        self.m_Name = name
        self.m_Script = text


def test_sprite_piece():
    sprite = Sprite("head", 1, 7, Rect(10, 20, 40, 30))
    entry = unity_atlas.sprite_piece("head", sprite.m_RD, sprite.m_Rect, sprite.m_Offset, 200)
    check("sprite: rect x kept", entry["rect"][0] == 10, entry["rect"])
    check("sprite: y flipped to top-left", entry["rect"][1] == 200 - (20 + 30), entry["rect"])
    check("sprite: upright size", entry["sprite_size"] == [40, 30], entry["sprite_size"])
    check("sprite: no rotation", entry["rotate"] == 0, entry["rotate"])

    # rotated 90: packed bit 0 + rotation 4 (settingsRaw = (4 << 2) | 1)
    rotated = Sprite("arm", 2, 7, Rect(0, 0, 30, 40), settings_raw=(4 << 2) | 1)
    entry_rot = unity_atlas.sprite_piece("arm", rotated.m_RD, rotated.m_Rect,
                                         rotated.m_Offset, 100)
    check("sprite: rotate 90 detected", entry_rot["rotate"] == 90, entry_rot["rotate"])
    check("sprite: packed size stays as stored", entry_rot["rect"][2:] == [30, 40],
          entry_rot["rect"])
    check("sprite: upright size is swapped", entry_rot["sprite_size"] == [40, 30],
          entry_rot["sprite_size"])

    turned = Sprite("body", 3, 7, Rect(0, 0, 10, 10), settings_raw=(3 << 2) | 1)
    check("sprite: rotate 180 detected",
          unity_atlas.sprite_piece("body", turned.m_RD, turned.m_Rect,
                                   turned.m_Offset, 50)["rotate"] == 180)

    flipped = Sprite("eye", 4, 7, Rect(0, 0, 10, 10), settings_raw=(1 << 2) | 1)
    entry_flip = unity_atlas.sprite_piece("eye", flipped.m_RD, flipped.m_Rect,
                                         flipped.m_Offset, 50)
    check("sprite: horizontal flip is flagged", entry_flip.get("flipped") is True, entry_flip)
    check("sprite: flip keeps rotation 0", entry_flip["rotate"] == 0, entry_flip["rotate"])

    trimmed = Sprite("hair", 5, 7, Rect(0, 0, 20, 10), sprite_rect=Rect(0, 0, 30, 40),
                     offset=Vec2(5, 25))
    entry_trim = unity_atlas.sprite_piece("hair", trimmed.m_RD, trimmed.m_Rect,
                                         trimmed.m_Offset, 100)
    check("sprite: untrimmed size recorded", entry_trim["orig_size"] == [30, 40],
          entry_trim["orig_size"])
    check("sprite: offset converted to top-down", entry_trim["offset"] == [5, 40 - 25 - 10],
          entry_trim["offset"])

    empty = Sprite("none", 6, 7, Rect(0, 0, 0, 0))
    check("sprite: empty rect is ignored",
          unity_atlas.sprite_piece("none", empty.m_RD, empty.m_Rect, empty.m_Offset, 100) is None)


def test_scan_sprites():
    objects = [
        Object("Sprite", Sprite("a", 1, 100, Rect(0, 0, 10, 10))),
        Object("Sprite", Sprite("b", 2, 100, Rect(10, 0, 10, 10))),
        Object("Sprite", Sprite("c", 3, 200, Rect(0, 0, 5, 5))),
        Object("Texture2D", None),
    ]
    grouped = unity_atlas.scan_sprites(objects)
    check("scan: two textures found", sorted(grouped) == [100, 200], sorted(grouped))
    check("scan: sprites grouped by texture", [len(grouped[100]), len(grouped[200])] == [2, 1],
          {key: len(value) for key, value in grouped.items()})

    class Atlas:
        def __init__(self):
            self.m_RenderDataMap = [("key-1", RenderData(300, Rect(4, 4, 8, 8)))]

    class AtlasPointer:
        def deref_parse_as_object(self):
            return Atlas()

    packed = Sprite("d", 4, 0, Rect(0, 0, 1, 1), atlas=AtlasPointer(), render_key="key-1")
    packed.m_RD = None  # type: ignore[assignment]
    result = unity_atlas.pieces_from_sprite(packed)
    check("scan: SpriteAtlas render data is used", result is not None and result[0] == 300, result)


def test_text_detection():
    spine_atlas = ("hero.png\nsize:512,512\nfilter:Linear,Linear\npma:true\n"
                   "head\nbounds:0,0,10,10\n")
    check("text: Spine .atlas recognized", unity_atlas.looks_like_spine_atlas(spine_atlas))
    check("text: random text rejected",
          not unity_atlas.looks_like_spine_atlas("hello world\nthis is not an atlas"))
    check("text: Spine .json recognized",
          unity_atlas.is_spine_json('{"skeleton": {"hash": "abc"}, "bones": []}'))
    check("text: plain json not mistaken for a skeleton",
          not unity_atlas.is_spine_json('{"items": []}'))



def test_attach_pages():
    root = tempfile.mkdtemp(prefix="unity_atlas_")
    try:
        page_path = os.path.join(root, "atlas_page.png")
        page = Image.new("RGBA", (64, 32), (0, 0, 0, 0))
        page.paste(Image.new("RGBA", (10, 10), (200, 40, 40, 255)), (0, 0))
        page.paste(Image.new("RGBA", (10, 10), (40, 200, 40, 255)), (10, 0))
        page.save(page_path, format="PNG")

        entries = [{"name": "atlas_page", "path_id": 11, "save_path": page_path,
                    "width": 64, "height": 32, "source_asset": "resources.assets"}]
        # Unity rect for the top-left square: bottom-up y = 32 - 10 = 22
        pieces = {11: [unity_atlas.sprite_piece("red", RenderData(11, Rect(0, 22, 10, 10)),
                                                Rect(0, 0, 10, 10), Vec2(0, 0), 0),
                       unity_atlas.sprite_piece("green", RenderData(11, Rect(10, 22, 10, 10)),
                                                Rect(0, 0, 10, 10), Vec2(0, 0), 0)]}
        # the stub always returns an entry, keep the types simple for pyright
        sprite_pieces = {key: [entry for entry in value if entry]
                         for key, value in pieces.items()}
        created = unity_atlas.attach_pages(entries, sprite_pieces, [], root)
        check("attach: one page created", created == 1, created)
        meta = entries[0].get("atlas_page") or {}
        check("attach: kind is unity-page", meta.get("kind") == "unity-page", meta.get("kind"))
        check("attach: regions recorded", len(meta.get("regions") or []) == 2, meta.get("regions"))
        check("attach: digests computed", len(meta.get("digests") or {}) == 2, meta.get("digests"))
        regions = atlas_mod.regions_from_metadata(meta["regions"])
        check("attach: regions rebuild", len(regions) == 2, len(regions))
        red = next(r for r in regions if r.name == "red")
        check("attach: red rect is the top square", (red.x, red.y) == (0, 0), (red.x, red.y))
        page_image = atlas_mod.load_page_image(page_path)
        check("attach: red digest matches the page pixels",
              page_image is not None
              and meta["digests"].get("red") == atlas_mod.piece_digest(
                  atlas_mod.unpack_region(page_image, red)))

        # 2) Spine atlas text (TextAsset) with its page texture in the same file
        text = ("atlas_page.png\nsize:64,32\nfilter:Linear,Linear\npma:false\n"
                "square\nbounds:0,0,10,10\n")
        entries2 = [{"name": "atlas_page", "path_id": 12, "save_path": page_path,
                     "width": 64, "height": 32}]
        created2 = unity_atlas.attach_pages(entries2, {}, [("hero", text)], root)
        check("attach: Spine text asset becomes a page", created2 == 1, created2)
        meta2 = entries2[0].get("atlas_page") or {}
        check("attach: Spine page points at the dumped .atlas",
              str(meta2.get("atlas_file", "")).endswith(".atlas"), meta2.get("atlas_file"))
        check("attach: Spine page digests", len(meta2.get("digests") or {}) == 1,
              meta2.get("digests"))
        check("attach: Spine page kind", meta2.get("kind") == "unity-spine", meta2.get("kind"))
        check("attach: dumped atlas still parses",
              len(atlas_mod.parse_atlas(meta2["atlas_file"]).pages) == 1)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    print("== sprite pieces ==")
    test_sprite_piece()
    print("== scanning ==")
    test_scan_sprites()
    print("== text detection ==")
    test_text_detection()
    print("== attach pages ==")
    test_attach_pages()

    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print("FAILED:", FAILURES)
        sys.exit(1)


if __name__ == "__main__":
    main()
