import os
import UnityPy
from PIL import Image
import io
import shutil
import json
import re
import zlib
import hashlib
import traceback
import struct
from UnityPy.export.Texture2DConverter import get_image_from_texture2d, parse_image_data
from UnityPy.files import BundleFile, SerializedFile
from UnityPy.helpers.UnityVersion import UnityVersion

# Picture extensions handled by the whole app. Every file listed here is decoded by
# similarity.load_image() (OpenCV -> Pillow fallback) and by PIL.Image.open(), i.e. only
# decoders that already ship with opencv-python / Pillow, so no extra dependency is needed.
#
# Deliberately NOT listed:
#   .hdr / .pfm  -> float pixels (0..1): similarity.ensure_grayscale() alpha masking
#                   (`gray[alpha < 10] = 0`) wipes every pixel, so all matches become 1.0
#   .exr         -> needs OpenEXR (missing in the OpenCV wheel) / no Pillow plugin
#   .psd         -> multi layer / CMYK sources, rarely correct as a texture
#   .pcx .sgi .im .msp .xbm .xpm .pcd .ras .icns .fits -> legacy/oddball encoders
#   .pdf .eps .ps .h5  -> need Ghostscript/h5py or are documents, not textures
SUPPORTED_IMAGE_EXTS = (
    '.png', '.apng', '.jpg', '.jpeg', '.jpe', '.jfif',
    '.bmp', '.dib', '.gif', '.tif', '.tiff', '.webp',
    '.tga', '.dds', '.jp2', '.j2k', '.j2c', '.jpc',
    '.ppm', '.pgm', '.pbm', '.pnm', '.avif', '.qoi',
)

# Extension -> Pillow save format. Must stay in sync with SUPPORTED_IMAGE_EXTS.
IMAGE_SAVE_FORMATS = {
    '.png': 'PNG', '.apng': 'PNG',
    '.jpg': 'JPEG', '.jpeg': 'JPEG', '.jpe': 'JPEG', '.jfif': 'JPEG',
    '.bmp': 'BMP', '.dib': 'BMP', '.gif': 'GIF',
    '.tif': 'TIFF', '.tiff': 'TIFF', '.webp': 'WEBP',
    '.tga': 'TGA', '.dds': 'DDS',
    '.jp2': 'JPEG2000', '.j2k': 'JPEG2000', '.j2c': 'JPEG2000', '.jpc': 'JPEG2000',
    '.ppm': 'PPM', '.pgm': 'PPM', '.pbm': 'PPM', '.pnm': 'PPM',
    '.avif': 'AVIF', '.qoi': 'QOI',
}

# Formats that cannot reliably store an alpha channel, so RGBA sources are flattened to RGB.
NO_ALPHA_IMAGE_EXTS = (
    '.jpg', '.jpeg', '.jpe', '.jfif', '.bmp', '.dib',
    '.ppm', '.pgm', '.pbm', '.pnm', '.dds',
)


def convert_image_format(src_path, dst_path):
    """Re-encodes src_path into the file format implied by the extension of dst_path.

    Returns True on success. dst_path is only touched when the conversion succeeded
    (the image is written to a temporary file first), so a failure never leaves a
    corrupt texture behind.
    """
    ext = os.path.splitext(dst_path)[1].lower()
    save_format = IMAGE_SAVE_FORMATS.get(ext)
    if not save_format:
        return False

    tmp_path = dst_path + ".tmp"
    try:
        with Image.open(src_path) as img:
            img.load()
            if ext in NO_ALPHA_IMAGE_EXTS:
                out_img = img.convert("RGB")
            elif img.mode in ("RGB", "RGBA"):
                out_img = img.copy()
            elif "A" in img.getbands() or img.mode in ("P", "PA", "LA"):
                out_img = img.convert("RGBA")
            else:
                out_img = img.convert("RGB")
            out_img.save(tmp_path, format=save_format)
        os.replace(tmp_path, dst_path)
        return True
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False


class AssetManager:
    def __init__(self, orig_dir, mod_dir, output_dir_orig, output_dir_mod):
        self.orig_dir = orig_dir
        self.mod_dir = mod_dir
        self.output_dir_orig = output_dir_orig
        self.output_dir_mod = output_dir_mod
        self.last_known_version = None
        # FORCE 2021.3.45 for this specific project as requested by user
        self.forced_engine_version = (2021, 3, 45, 2)

    @staticmethod
    def file_signature(path):
        """Returns (size, mtime_ns) of a file, or None when it cannot be stat'ed.

        Used to detect that a big asset changed on disk (another tool, a copied folder,
        a re-downloaded build) between reading it and writing it back.
        """
        try:
            stat = os.stat(path)
            return (stat.st_size, stat.st_mtime_ns)
        except OSError:
            return None

    def sanitize_filename(self, filename):
        """Sanitizes filename for Windows/Unix compatibility by removing specific characters."""
        return re.sub(r'[\\/:*?"<>|]', '_', filename)

    def apply_stride_correction(self, raw_data, width, height, bpp, log_callback=None):
        """
        Removes per-row padding (Stride) using exhaustive alignment evaluation.
        Unity 2021.3.x often uses 16-byte alignment, but some assets use 4 or 8.
        """
        row_size = width * bpp
        actual_size = len(raw_data)
        
        candidates = []
        for align in [4, 8, 16, 32]:
            stride = (row_size + (align - 1)) & ~(align - 1)
            if (stride * height) <= actual_size:
                diff = abs(actual_size - (stride * height))
                fit_score = 1.0 - (diff / actual_size)
                is_po2 = (width & (width-1) == 0) and (height & (height-1) == 0)
                weight = 1.0
                if align == 16: weight = 1.5
                elif align == 4 and not is_po2: weight = 1.2
                candidates.append({'align': align, 'stride': stride, 'score': fit_score * weight})
        
        if not candidates: return raw_data
        candidates.sort(key=lambda x: x['score'], reverse=True)
        best = candidates[0]
        if best['stride'] > row_size:
            if log_callback: log_callback(f"      [STRIDE] Selected {best['align']}-byte alignment (Stride: {best['stride']}, Score: {best['score']:.3f})")
            corrected = bytearray()
            for y in range(height):
                start = y * best['stride']
                if start + row_size <= actual_size:
                    corrected.extend(raw_data[start : start + row_size])
            return bytes(corrected)
        return raw_data

    def decode_texture_with_fixed_pixels(self, texture_2d, fixed_pixels):
        """Decodes already stride-corrected pixel bytes with UnityPy's own decoder.

        get_image_from_texture2d(texture_2d, flip=True) always pulls the pixels from
        texture_2d.get_image_data(), and its second parameter is the `flip` flag, so
        corrected bytes cannot be handed over as an argument. They are swapped into
        image_data only for the duration of the decode and the original value (bytes or
        None for streamed textures) is restored afterwards.
        """
        original_pixels = getattr(texture_2d, "image_data", None)
        try:
            texture_2d.image_data = fixed_pixels
            return get_image_from_texture2d(texture_2d)
        finally:
            texture_2d.image_data = original_pixels

    @staticmethod
    def iter_env_files(env):
        """Yields every file of a loaded environment, descending into bundles / web files.

        SerializedFile.files returns its *objects* instead of files, so the descent stops
        at a SerializedFile; the nested ones are reached through the BundleFile / WebFile
        that own them.
        """
        pending = [getattr(env, "file", None)]
        pending.extend(getattr(env, "files", {}).values())
        seen = set()
        while pending:
            node = pending.pop()
            if node is None or id(node) in seen: continue
            seen.add(id(node))
            yield node
            if isinstance(node, SerializedFile): continue  # .files are objects, not files
            children = getattr(node, "files", None)
            if isinstance(children, dict): pending.extend(children.values())

    @staticmethod
    def engine_version_tuple(value):
        """Normalises a Unity engine version into (major, minor, build, patch) or None.

        UnityPy reports SerializedFile.version as a UnityVersion and BundleFile.version as
        the bundle *header* format number, so only UnityVersion objects, tuples/lists and
        version strings ("2021.3.45f2") actually carry an engine version. Everything else
        (ints, empty strings, None) is treated as unknown.

        Note: UnityVersion.__eq__ raises NotImplementedError for str/int operands, so raw
        version values must never be compared against strings or plain ints directly.
        """
        if isinstance(value, UnityVersion):
            return (value.major, value.minor, value.build, value.type_number)
        if isinstance(value, str):
            match = re.match(r"\s*(\d+)\.(\d+)\.(\d+)(?:f(\d+))?", value)
            if not match: return None
            major, minor, build, patch = match.groups()
            return (int(major), int(minor), int(build), int(patch) if patch else 0)
        if isinstance(value, (tuple, list)) and len(value) >= 3:
            try: parts = [int(part) for part in value[:4]]
            except (TypeError, ValueError): return None
            return tuple(parts + [0] * (4 - len(parts)))
        return None

    @staticmethod
    def is_usable_engine_version(version):
        """False for the placeholders UnityPy reports when a file carries no version string."""
        if not isinstance(version, tuple) or len(version) < 3: return False
        return version[0] >= 3  # rejects 0.0.0 and the 2.5.0f5 placeholder

    @staticmethod
    def version_string(version):
        """Formats a (major, minor, build, patch) tuple as a Unity version string."""
        major, minor, build, patch = (list(version) + [0, 0, 0, 0])[:4]
        return f"{major}.{minor}.{build}f{patch}"

    def apply_engine_version(self, env, version):
        """Writes version into every SerializedFile of env (bundle children included).

        The engine version has to go through SerializedFile.set_version() and the bundle's
        version_engine string, because SerializedFile.version is a UnityVersion (int
        subclass) and BundleFile.version is the bundle *header* format number: assigning a
        version tuple to `.version` either raises or corrupts the header written by save().
        Returns the number of files that were updated.
        """
        version_str = self.version_string(version)
        updated = 0
        for asset_file in self.iter_env_files(env):
            try:
                if isinstance(asset_file, SerializedFile):
                    asset_file.set_version(version_str)
                    updated += 1
                elif isinstance(asset_file, BundleFile):
                    asset_file.version_engine = version_str
                    updated += 1
            except Exception:
                continue
        return updated

    def resolve_engine_version(self, env, log_callback=None):
        """Returns the engine version of env as a (major, minor, build, patch) tuple.

        Files without a readable engine version (stripped typetree, empty version_engine
        string) get forced_engine_version / last_known_version written into them, so the
        typetree parser and the texture decoders agree on the version of this project.
        """
        files = list(self.iter_env_files(env))
        candidates = [getattr(f, "version", None) for f in files if isinstance(f, SerializedFile)]
        candidates += [getattr(f, "version_engine", None) for f in files if isinstance(f, BundleFile)]
        for candidate in candidates:
            version = self.engine_version_tuple(candidate)
            if self.is_usable_engine_version(version):
                self.last_known_version = version
                return version

        fallback = self.engine_version_tuple(
            self.forced_engine_version or self.last_known_version or (2021, 3, 45, 2)
        ) or (2021, 3, 45, 2)
        updated = self.apply_engine_version(env, fallback)
        if log_callback and updated:
            log_callback(f"  [VERSION] Engine version unknown, forced {self.version_string(fallback)} on {updated} file(s).")
        return fallback

    def extract_textures(self, game_dir, output_dir, progress_callback=None, log_callback=None, scan_all=False):
        found_textures = []
        if not os.path.exists(output_dir): os.makedirs(output_dir)
            
        def log(msg):
            if log_callback: log_callback(msg)
            else: print(msg)

        if not os.access(output_dir, os.W_OK):
            log(f"Error: Output directory {output_dir} is not writable.")
            return found_textures
            
        all_files = []
        skip_exts = SUPPORTED_IMAGE_EXTS + ('.json', '.txt', '.py', '.ini', '.bak', '.apk', '.exe', '.dll', '.wav', '.mp3', '.ogg', '.mp4')
        for root, dirs, files in os.walk(game_dir):
            for file in files:
                filepath = os.path.join(root, file)
                file_lower = file.lower()
                # USER: "unity mode가 되는데, assets을 추출할 때, apk 파일은 제외하고 작업하도록 해."
                if file_lower.endswith(('.bak', '.apk', '.zip', '.rar', '.7z')): continue
                if scan_all:
                    if not file_lower.endswith(skip_exts): all_files.append(filepath)
                else:
                    has_ext = '.' in file
                    if not has_ext or file_lower.endswith(('.assets', '.sharedassets', '.unity3d', '.bundle', '.resource', '.resS')):
                        all_files.append(filepath)
        
        total_files = len(all_files)
        if total_files == 0:
            log("No Unity asset files found in the selected folder.")
            return found_textures

        for idx, path in enumerate(all_files):
            if progress_callback:
                progress_callback(int((idx / total_files) * 100), f"Processing {os.path.basename(path)}...")
            try:
                file_size = os.path.getsize(path)
                log(f"Processing {os.path.basename(path)} ({file_size} bytes)...")
                signature_before = self.file_signature(path)
                env = UnityPy.load(path)
                if self.file_signature(path) != signature_before:
                    # The folder/file was swapped (or the build was copied over) while the
                    # asset was being read: whatever we parse now belongs to a moving target.
                    log(f"  [SKIP] {os.path.basename(path)} changed on disk while it was read. Skipped.")
                    continue
                
                # Engine version of the loaded file(s); unknown versions are forced onto the
                # files instead of being assigned to `.version` directly (see
                # resolve_engine_version() for why the types differ per file kind).
                engine_version = self.resolve_engine_version(env, log)

                obj_count = 0
                type_counts = {}
                file_textures = 0
                
                for obj in env.objects:
                    obj_count += 1
                    tname = str(obj.type.name) if hasattr(obj.type, 'name') else str(obj.type)
                    type_counts[tname] = type_counts.get(tname, 0) + 1
                    
                    if obj.type.name == "Texture2D":
                        try:
                            t2d_count = type_counts.get("Texture2D", 0)
                            data = None
                            img = None
                            is_tree = False
                            
                            is_2021_3_45 = engine_version[:3] == (2021, 3, 45)
                            
                            h_w, h_h, h_fmt = 0, 0, 0
                            h_name = f"Unnamed_{obj.path_id}"
                            
                            # PHASE 1: Standard Read
                            try:
                                data = obj.read()
                                if data:
                                    h_w = getattr(data, "m_Width", 0)
                                    h_h = getattr(data, "m_Height", 0)
                                    h_fmt = getattr(data, "m_TextureFormat", 0)
                                    h_name = getattr(data, "m_Name", getattr(data, "name", h_name))
                                    if 0 < h_w < 16384:
                                        try:
                                            img = data.image
                                            if img and h_fmt in [1, 2, 3, 4, 5, 13]:
                                                bpp = 4 if h_fmt in [4, 5] else (3 if h_fmt == 3 else (2 if h_fmt in [2, 13] else 1))
                                                raw_pixels = data.get_image_data()
                                                if raw_pixels and len(raw_pixels) > (h_w * h_h * bpp):
                                                    fixed_pixels = self.apply_stride_correction(raw_pixels, h_w, h_h, bpp, log)
                                                    # Only trust a correction that yields exactly one mip level.
                                                    if len(fixed_pixels) == h_w * h_h * bpp:
                                                        img = self.decode_texture_with_fixed_pixels(data, fixed_pixels)
                                        except: img = None
                            except: pass

                            # PHASE 2: Manual Decoder Fallback/Fix
                            if is_2021_3_45 or img is None:
                                try:
                                    m_img = self.manual_decode_texture2d(obj, log_callback=log, 
                                                                         width_hint=h_w, height_hint=h_h, format_hint=h_fmt)
                                    if m_img:
                                        if img:
                                            if m_img.size != img.size:
                                                log(f"    [WARNING] Resolution conflict for {h_name}: Standard {img.size} vs Manual {m_img.size}. Trusting Standard.")
                                            else:
                                                img = m_img
                                        else:
                                            img = m_img
                                except: pass

                            # PHASE 3: TypeTree last resort
                            if img is None:
                                try:
                                    data = obj.read_typetree()
                                    is_tree = True
                                    h_w = data.get("m_Width", 0)
                                    h_h = data.get("m_Height", 0)
                                    h_fmt = data.get("m_TextureFormat", 0)
                                    h_name = data.get("m_Name", h_name)
                                except: pass
                            
                            if img is None: continue

                            raw_name = h_name
                            width, height = img.size
                            texture_format = str(h_fmt)

                            texture_name = self.sanitize_filename(raw_name)
                            safe_name = f"{texture_name}_{obj.path_id}.png"
                            save_path = os.path.join(output_dir, safe_name)
                            
                            if t2d_count <= 5 or file_textures < 5:
                                log(f"  Texture: {texture_name} (ID: {obj.path_id}, {width}x{height}, {texture_format})")

                            buf = io.BytesIO()
                            img.save(buf, format="PNG")
                            try:
                                with open(save_path, "wb") as f:
                                    f.write(buf.getvalue())
                            except OSError as error:
                                # e.g. the temp folder was cleared while this asset was read
                                log(f"  [SKIP] Could not write {safe_name}: {error}")
                                continue
                                
                            found_textures.append({
                                "name": texture_name,
                                "path_id": obj.path_id,
                                "file": safe_name,
                                "source_asset": path,
                                "width": width,
                                "height": height,
                                "size": len(buf.getvalue())
                            })
                            file_textures += 1
                        except: continue
                log(f"  Finished {os.path.basename(path)}: Found {file_textures} textures.")
            except Exception as e:
                log(f"Error loading {os.path.basename(path)}: {e}")
        
        self.save_metadata(found_textures, output_dir)
        return found_textures

    def replace_textures_batch(self, source_asset_path, replacements, log_callback=None, high_quality=False):
        def log(msg):
            if log_callback: log_callback(msg)
            else: print(msg)
        if not replacements: return False
        abs_source_path = os.path.abspath(source_asset_path)
        signature_before = self.file_signature(abs_source_path)
        if signature_before is None:
            log(f"ERROR: {os.path.basename(abs_source_path)} not found.")
            return False
        bak_path = abs_source_path + ".bak"
        if not os.path.exists(bak_path):
            try: shutil.copy2(abs_source_path, bak_path)
            except: return False
        try:
            env = UnityPy.load(abs_source_path)
            modified_count = 0
            tex_map = {obj.path_id: obj for obj in env.objects if obj.type.name == "Texture2D"}
            for path_id, new_image_path in replacements:
                if path_id in tex_map:
                    obj = tex_map[path_id]
                    try:
                        data = obj.read()
                        orig_format = getattr(data, 'm_TextureFormat', None)
                        if high_quality and orig_format is not None:
                            try:
                                data.m_TextureFormat = 25 # BC7
                                with Image.open(new_image_path) as img:
                                    data.image = img
                                    data.save()
                            except:
                                data.m_TextureFormat = orig_format
                                with Image.open(new_image_path) as img:
                                    data.image = img
                                    data.save()
                        else:
                            with Image.open(new_image_path) as img:
                                data.image = img
                                data.save()
                        modified_count += 1
                    except: pass
            if modified_count > 0:
                old_crc32 = self.calculate_crc32(abs_source_path)
                old_size = os.path.getsize(abs_source_path)
                # env.file is a SerializedFile or a BundleFile; both expose save(packer=...)
                save_file = getattr(env.file, "save", None)
                if save_file is None: return False
                try: new_data = save_file(packer='original')
                except: new_data = save_file(packer='none')
                if self.file_signature(abs_source_path) != signature_before:
                    # The asset was replaced on disk (folder swapped / re-extracted) after we
                    # read it. Writing now would overwrite whatever the newer file contains.
                    log(f"ERROR: {os.path.basename(abs_source_path)} changed on disk while it was processed. "
                        f"Nothing was written to avoid overwriting newer data.")
                    return False
                with open(abs_source_path, "wb") as f: f.write(new_data)
                new_md5 = hashlib.md5(new_data).hexdigest()
                if abs_source_path.lower().endswith('.bundle'):
                    self.update_addressable_catalogs(abs_source_path, old_crc32, old_crc32, old_size, len(new_data), new_md5)
                return True
        except: return False
        return False

    def save_metadata(self, textures, output_dir):
        metadata_path = os.path.join(output_dir, "metadata.json")
        try:
            with open(metadata_path, 'w', encoding='utf-8') as f:
                json.dump(textures, f, ensure_ascii=False, indent=2)
            return True
        except: return False

    def load_metadata(self, output_dir):
        metadata_path = os.path.join(output_dir, "metadata.json")
        if not os.path.exists(metadata_path): return None
        try:
            with open(metadata_path, 'r', encoding='utf-8') as f: return json.load(f)
        except: return None

    def calculate_crc32(self, file_path):
        crc = 0
        with open(file_path, 'rb') as f:
            while chunk := f.read(8192): crc = zlib.crc32(chunk, crc)
        return crc & 0xFFFFFFFF

    def update_addressable_catalogs(self, bundle_path, old_crc, new_crc, old_size, new_size, new_md5):
        bundle_name = os.path.basename(bundle_path)
        search_dir = os.path.dirname(bundle_path)
        for _ in range(4):
            cat_path = os.path.join(search_dir, "catalog.json")
            if os.path.exists(cat_path):
                self._patch_catalog(cat_path, bundle_name, old_crc, new_crc, old_size, new_size, new_md5)
                for f in os.listdir(search_dir):
                    if f.startswith("catalog_") and f.endswith(".json"):
                        self._patch_catalog(os.path.join(search_dir, f), bundle_name, old_crc, new_crc, old_size, new_size, new_md5)
                break
            parent = os.path.dirname(search_dir)
            if parent == search_dir: break
            search_dir = parent

    def _patch_catalog(self, cat_path, bundle_name, old_crc, new_crc, old_size, new_size, new_md5):
        try:
            with open(cat_path, 'r', encoding='utf-8') as f: data = json.load(f)
            modified = False
            internal_ids = data.get("m_InternalIds", [])
            bundle_idx = -1
            for i, uid in enumerate(internal_ids):
                if bundle_name in uid:
                    bundle_idx = i
                    break
            if bundle_idx == -1: return
            old_crc_signed = old_crc - 4294967296 if old_crc > 2147483647 else old_crc
            def patch_recursive(obj):
                nonlocal modified
                if isinstance(obj, list):
                    if len(obj) > bundle_idx:
                        val = obj[bundle_idx]
                        if isinstance(val, int):
                            if val == old_crc or val == old_crc_signed:
                                obj[bundle_idx] = new_crc
                                modified = True
                            elif val == old_size:
                                obj[bundle_idx] = new_size
                                modified = True
                        elif isinstance(val, str) and len(val) == 32:
                            obj[bundle_idx] = new_md5
                            modified = True
                    for item in obj: patch_recursive(item)
                elif isinstance(obj, dict):
                    for k, v in obj.items(): patch_recursive(v)
            patch_recursive(data)
            if modified:
                if not os.path.exists(cat_path + ".bak"): shutil.copy2(cat_path, cat_path + ".bak")
                with open(cat_path, 'w', encoding='utf-8') as f: json.dump(data, f, separators=(',', ':'))
                hash_path = cat_path + ".hash"
                if os.path.exists(hash_path):
                    with open(cat_path, 'rb') as f:
                        new_hash = hashlib.md5(f.read()).hexdigest()
                        with open(hash_path, 'w', encoding='utf-8') as hf: hf.write(new_hash)
        except: pass

    def manual_decode_texture2d(self, obj, log_callback=None, width_hint=0, height_hint=0, format_hint=0):
        def dlog(msg):
            if log_callback: log_callback(f"      [MANUAL] {msg}")
        try:
            raw = obj.get_raw_data()
            if len(raw) < 64: return None
            found_name_info = None
            try:
                for n_off in range(0, min(len(raw), 128), 1):
                    n_len = struct.unpack("<i", raw[n_off:n_off+4])[0]
                    if 2 <= n_len <= 128 and (n_off + 4 + n_len) < len(raw):
                        n_bytes = raw[n_off + 4 : n_off + 4 + n_len]
                        try:
                            n_text = n_bytes.decode('utf-8')
                            if all(32 <= ord(c) <= 126 for c in n_text):
                                found_name_info = (n_text, n_off + 4 + n_len)
                                break
                        except: continue
            except: pass
            if not found_name_info:
                try:
                    target_name = getattr(obj, "m_Name", "") or "UnitySplash-cube"
                    name_bytes = target_name.encode('utf-8')
                    idx = raw.find(name_bytes)
                    if -1 < idx < 1024: found_name_info = (target_name, idx + len(name_bytes))
                except: pass
            name, pos = found_name_info if found_name_info else (getattr(obj, "m_Name", "Unknown"), 4)
            if pos % 4 != 0: pos += 4 - (pos % 4)
            
            # SCAN for [W, H, Size]
            candidates = []
            is_cursor = "cursor" in name.lower() or obj.path_id in [26, 105]
            for i in range(pos, min(len(raw), pos + 160), 4):
                try:
                    w_t, h_t, size_t = struct.unpack("<iiI", raw[i:i+12])
                    if width_hint > 0 and height_hint > 0:
                        if w_t != width_hint or h_t != height_hint: continue
                    if 1 <= w_t <= 16384 and 1 <= h_t <= 16384:
                        for f_off in [12, 16, 20, 24]:
                            if i + f_off + 4 > len(raw): continue
                            fmt_t = struct.unpack("<i", raw[i+f_off:i+f_off+4])[0]
                            if 1 <= fmt_t <= 64:
                                ratio = size_t / (w_t * h_t) if (w_t * h_t) > 0 else 0
                                if 0.1 <= ratio <= 16: candidates.append((w_t, h_t, size_t, fmt_t, i))
                except: continue
            
            if not candidates: # Fallback if hints failed
                for i in range(pos, min(len(raw), pos + 160), 4):
                    try:
                        w_t, h_t, size_t = struct.unpack("<iiI", raw[i:i+12])
                        if 1 <= w_t <= 16384 and 1 <= h_t <= 16384:
                            for f_off in [12, 16, 20, 24]:
                                if i + f_off + 4 > len(raw): continue
                                fmt_t = struct.unpack("<i", raw[i+f_off:i+f_off+4])[0]
                                if 1 <= fmt_t <= 64: candidates.append((w_t, h_t, size_t, fmt_t, i))
                    except: continue

            if not candidates: return None
            candidates.sort(key=lambda x: x[0] * x[1], reverse=True)
            discovered_w, discovered_h, discovered_size, discovered_fmt, found_idx = candidates[0]
            
            image_data = None
            # Streamed Check
            stream_info = None
            for ext in [b".resS", b".resource", b".assets", b".dat"]:
                idx_ext = raw.find(ext, pos)
                if -1 < idx_ext < 2048:
                    s_start = -1
                    for j in range(idx_ext, idx_ext - 128, -2):
                        if j < pos: break
                        try:
                            s_len = struct.unpack("<i", raw[j:j+4])[0]
                            if 3 < s_len < 256 and (j + 4 + s_len) >= (idx_ext + len(ext)):
                                s_text = raw[j+4 : j+4+s_len].decode('utf-8', 'ignore')
                                if ext.decode().lower() in s_text.lower(): s_start = j; break
                        except: continue
                    if s_start != -1:
                        try:
                            pot_size = struct.unpack("<I", raw[s_start - 4 : s_start])[0]
                            pot_offset = struct.unpack("<Q", raw[s_start - 12 : s_start - 4])[0]
                            if 1024 < pot_size < 1024 * 1024 * 1024:
                                stream_info = {'path': s_text, 'offset': pot_offset, 'size': pot_size}
                                break
                        except: pass

            if stream_info:
                asset_full_path = getattr(obj.assets_file, "path", None)
                if not asset_full_path and hasattr(obj.assets_file, "parent"): asset_full_path = getattr(obj.assets_file.parent, "path", None)
                asset_dir = os.path.dirname(asset_full_path) if asset_full_path else os.getcwd()
                fname = os.path.basename(stream_info['path'].replace("archive:/", "").replace("\\", "/"))
                stream_path = os.path.join(asset_dir, fname)
                if os.path.exists(stream_path):
                    with open(stream_path, "rb") as f:
                        f.seek(stream_info['offset'])
                        image_data = f.read(stream_info['size'])
            
            if not image_data:
                for i in range(len(raw) - discovered_size - 4, pos - 1, -4):
                    if struct.unpack("<I", raw[i:i+4])[0] == discovered_size:
                        image_data = raw[i + 4 : i + 4 + discovered_size]
                        break
            
            if not image_data: return None
            if discovered_fmt in [1, 2, 3, 4, 5, 13]:
                bpp = 4 if discovered_fmt in [4, 5] else (3 if discovered_fmt == 3 else (2 if discovered_fmt in [2, 13] else 1))
                image_data = self.apply_stride_correction(image_data, discovered_w, discovered_h, bpp, dlog)
            
            try:
                from UnityPy.enums import BuildTarget, TextureFormat
                # parse_image_data() is what get_image_from_texture2d() delegates to, so it
                # decodes raw bytes without needing a Texture2D instance to wrap them in.
                return parse_image_data(
                    image_data,
                    discovered_w,
                    discovered_h,
                    TextureFormat(discovered_fmt),
                    getattr(obj, "version", (0, 0, 0, 0)),
                    getattr(obj, "platform", BuildTarget.UnknownPlatform),
                )
            except: return None
        except: return None

if __name__ == "__main__":
    pass
