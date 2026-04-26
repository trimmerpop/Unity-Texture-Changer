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

class AssetManager:
    def __init__(self, orig_dir, mod_dir, output_dir_orig, output_dir_mod):
        self.orig_dir = orig_dir
        self.mod_dir = mod_dir
        self.output_dir_orig = output_dir_orig
        self.output_dir_mod = output_dir_mod
        self.last_known_version = None
        # FORCE 2021.3.45 for this specific project as requested by user
        self.forced_engine_version = (2021, 3, 45, 2)

    def sanitize_filename(self, filename):
        """Sanitizes filename for Windows/Unix compatibility by removing specific characters."""
        # Remove characters that are illegal in Windows filenames: \ / : * ? " < > |
        # Also limit non-ASCII if needed, but here we focus on illegal punctuation.
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
            # Check if this stride is plausible given the total data size
            if (stride * height) <= actual_size:
                # Perfect match (stride * height == actual_size) gets a massive bonus
                diff = abs(actual_size - (stride * height))
                fit_score = 1.0 - (diff / actual_size)
                
                # Priority weight: Prefer 16-byte for 2021.3.45, 4-byte for non-Po2
                is_po2 = (width & (width-1) == 0) and (height & (height-1) == 0)
                weight = 1.0
                if align == 16: weight = 1.5
                elif align == 4 and not is_po2: weight = 1.2
                
                candidates.append({
                    'align': align,
                    'stride': stride,
                    'score': fit_score * weight
                })
        
        if not candidates:
            return raw_data
            
        if log_callback:
            log_callback(f"      [STRIDE] Analyzing {width}x{height} (BPP: {bpp}, Row: {row_size}b)")
            # Show top 4 candidates (4, 8, 16, 32)
            temp_candidates = sorted(candidates, key=lambda x: x['score'], reverse=True)
            for c in temp_candidates:
                log_callback(f"        - Align {c['align']}: Stride {c['stride']}, Score {c['score']:.3f}")

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



    def extract_textures(self, game_dir, output_dir, progress_callback=None, log_callback=None, scan_all=False):
        """Extracts Texture2D assets from Unity files in game_dir to output_dir.
        If scan_all is True, searches all files regardless of extension.
        """
        found_textures = []
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
            
        def log(msg):
            if log_callback: log_callback(msg)
            else: print(msg)

        # Safety Check: Is directory writable?
        if not os.access(output_dir, os.W_OK):
            log(f"Error: Output directory {output_dir} is not writable.")
            return found_textures
            
        # 1. Searching for Unity files
        all_files = []
        # Obvious non-Unity files to skip in scan_all mode
        skip_exts = ('.png', '.jpg', '.jpeg', '.json', '.txt', '.py', '.ini', '.bak', '.apk', '.exe', '.dll', '.wav', '.mp3', '.ogg', '.mp4')
        for root, dirs, files in os.walk(game_dir):
            for file in files:
                filepath = os.path.join(root, file)
                file_lower = file.lower()

                # Explicitly skip requested junk
                if file_lower.endswith(('.bak', '.apk')):
                    continue

                if scan_all:
                    if not file_lower.endswith(skip_exts):
                        all_files.append(filepath)
                else:
                    # Normal Scan: Known Unity extensions OR files with NO extension (like 'unity default resources')
                    has_ext = '.' in file
                    if not has_ext or file_lower.endswith(('.assets', '.sharedassets', '.unity3d', '.bundle', '.resource', '.resS')):
                        all_files.append(filepath)
        
        total_files = len(all_files)
        if total_files == 0:
            log("No Unity asset files found in the selected folder.")
            return found_textures

        # 2. Extract textures from each file
        for idx, path in enumerate(all_files):
            if progress_callback:
                progress_callback(int((idx / total_files) * 100), f"Processing {os.path.basename(path)}...")
                
            try:
                file_size = os.path.getsize(path)
                log(f"Processing {os.path.basename(path)} ({file_size} bytes)...")
                
                env = UnityPy.load(path)
                log(f"  UnityPy Version: {UnityPy.__version__}")
                
                # Log Unity version and format
                engine_version = "Unknown"
                asset_format = "Unknown"
                try:
                    # Handle version detection and split asset logic
                    raw_ver = getattr(env.file, "version", "Unknown") if hasattr(env, "file") else "Unknown"
                    
                    # Detect if version looks like a proper engine version (e.g. "2021.3.45")
                    # Ignore format versions (7, 8, 15, etc.)
                    is_valid_eng_ver = False
                    if isinstance(raw_ver, tuple) and len(raw_ver) >= 2 and raw_ver[0] > 2000:
                        is_valid_eng_ver = True
                    elif isinstance(raw_ver, str) and "." in raw_ver and len(raw_ver) > 5:
                        is_valid_eng_ver = True
                        
                    # Inject version if current one is totally missing OR just a format version
                    is_bad = (raw_ver == "Unknown" or not raw_ver or raw_ver == (0,0,0,0) or 
                              (isinstance(raw_ver, str) and (len(raw_ver) < 3 or raw_ver.isdigit())) or 
                              (isinstance(raw_ver, int) and raw_ver < 100))
                    
                    if is_bad:
                        # Priority: 1. Forced Version, 2. Last Known, 3. Hardcoded Fallback
                        target_ver = self.forced_engine_version or self.last_known_version or (2021, 3, 45, 2)
                        log(f"  Version mismatch/bad (found {raw_ver}). Injecting engine version: {target_ver}")
                        if hasattr(env, "file"):
                            try: env.file.version = target_ver
                            except: pass
                        engine_version = target_ver
                    else:
                        engine_version = raw_ver
                    
                    # Keep track of the highest/latest engine version we've seen
                    if is_valid_eng_ver:
                        self.last_known_version = engine_version

                    log(f"  Actual Unity Version: {engine_version} (Format: {asset_format})")
                except Exception as ve:
                    log(f"  Version Info Error Core: {ve}")

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
                            if t2d_count <= 5:
                                log(f"  [DEBUG] Processing Texture2D ID {obj.path_id}...")

                            data = None
                            img = None
                            is_tree = False
                            manual_img = None
                            
                            # Priorities: 
                            # 1. Manual Parser (FORCED for 2021.3.45 to fix shifting/colors)
                            # 2. Standard UnityPy
                            # 3. TypeTree
                            
                            is_2021_3_45 = (engine_version == (2021, 3, 45, 2) or "2021.3.45" in str(engine_version))
                            
                            # For 2021.3.45, we prioritize our verified manual heuristic
                            if is_2021_3_45:
                                try:
                                    manual_img = self.manual_decode_texture2d(obj, log_callback=log)
                                    if manual_img:
                                        img = manual_img
                                        if t2d_count <= 5: log(f"    Forced Manual Parser successful for 2021.3.45.")
                                except: pass

                            # Standard read fallback
                            if img is None:
                                try:
                                    data = obj.read()
                                    if hasattr(data, "m_Width") and 0 < data.m_Width < 16384:
                                        try:
                                            img = data.image
                                            
                                            # STRIDE CHECK: If uncompressed, verify if it needs manual stride fix
                                            fmt_int = getattr(data, "m_TextureFormat", 0)
                                            if fmt_int in [1, 2, 3, 4, 5, 13]:
                                                bpp = 4 if fmt_int in [4, 5] else (3 if fmt_int == 3 else (2 if fmt_int in [2, 13] else 1))
                                                raw_pixels = data.get_image_data()
                                                if raw_pixels and len(raw_pixels) > (data.m_Width * data.m_Height * bpp):
                                                    log(f"    [DEBUG] Uncompressed texture {data.m_Name} has extra bytes. Applying Stride Fix...")
                                                    fixed_pixels = self.apply_stride_correction(raw_pixels, data.m_Width, data.m_Height, bpp, log)
                                                    from UnityPy.export.Texture2DConverter import get_image_from_texture2d
                                                    img = get_image_from_texture2d(data, fixed_pixels)
                                        except: img = None
                                except Exception as e:
                                    if t2d_count <= 5: log(f"    Standard Read Exception: {e}")

                            # TypeTree last resort
                            if img is None:
                                try:
                                    data = obj.read_typetree()
                                    is_tree = True
                                    if t2d_count <= 5: log(f"    Read via TypeTree successful.")
                                except: pass
                            
                            if img is None: 
                                if t2d_count <= 5: log(f"    Skipping ID {obj.path_id}: No image produced by any method.")
                                continue

                            # 3. Extract metadata
                            if manual_img:
                                import struct
                                raw = obj.get_raw_data()
                                try:
                                    nlen = struct.unpack("<i", raw[0:4])[0]
                                    raw_name = raw[4:4+nlen].decode('utf-8', 'ignore') if (0 < nlen < 256) else f"Manual_{obj.path_id}"
                                except: raw_name = f"Manual_{obj.path_id}"
                                width, height = img.size
                                texture_format = "Manual"
                            elif is_tree:
                                raw_name = data.get("m_Name", f"Unnamed_{obj.path_id}")
                                width = data.get("m_Width", 0)
                                height = data.get("m_Height", 0)
                                texture_format = str(data.get("m_TextureFormat", "Unknown"))
                            else:
                                raw_name = getattr(data, "m_Name", getattr(data, "name", f"Unnamed_{obj.path_id}"))
                                width = getattr(data, "m_Width", 0)
                                height = getattr(data, "m_Height", 0)
                                texture_format = str(getattr(data, "m_TextureFormat", "Unknown"))

                            texture_name = self.sanitize_filename(raw_name)
                            safe_name = f"{texture_name}_{obj.path_id}.png"
                            save_path = os.path.join(output_dir, safe_name)
                            
                            if t2d_count <= 5 or file_textures < 5:
                                log(f"  Texture: {texture_name} (ID: {obj.path_id}, {width}x{height}, {texture_format})")

                            # Robust saving
                            buf = io.BytesIO()
                            img.save(buf, format="PNG")
                            with open(save_path, "wb") as f:
                                f.write(buf.getvalue())
                                
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
                                
                        except Exception as e:
                            log(f"    Critical error on ID {obj.path_id}: {e}")
                            continue
                    
                    elif obj.type.name == "Sprite":
                        try:
                            # Test if Sprite read works
                            if file_textures == 0 and obj_count % 1000 == 0: 
                                try:
                                    sdata = obj.read()
                                    # Use safe access
                                    sname = "Unnamed"
                                    if hasattr(sdata, "m_Name"): sname = sdata.m_Name
                                    elif hasattr(sdata, "name"): sname = sdata.name
                                    
                                    log(f"  Success reading Sprite: {sname} (ID: {obj.path_id})")
                                    # Check its texture reference
                                    if hasattr(sdata, "m_RD") and hasattr(sdata.m_RD, "texture"):
                                        t_ref = sdata.m_RD.texture
                                        log(f"    Sprite references Texture ID: {t_ref.path_id}")
                                except Exception as se:
                                    if obj_count < 5000: # Only log first few failures
                                        log(f"  Fail reading Sprite object (ID: {obj.path_id}): {se}")
                        except:
                            pass
                
                log(f"  Finished {os.path.basename(path)}: Found {obj_count} objects.")
                if type_counts:
                    types_str = ", ".join([f"{k}: {v}" for k, v in type_counts.items()])
                    log(f"  Types: {types_str}")
                log(f"  Extracted {file_textures} textures.")
                
            except Exception as e:
                log(f"Error loading {os.path.basename(path)}: {e}")
        
        # Save metadata for later use
        self.save_metadata(found_textures, output_dir)
        return found_textures

    def replace_textures_batch(self, source_asset_path, replacements, log_callback=None, high_quality=False):
        """
        Replaces multiple textures in a single Unity asset file.
        replacements: list of (path_id, new_image_path)
        """
        def log(msg):
            if log_callback: log_callback(msg)
            else: print(msg)

        if not replacements:
            log("No replacements provided to replace_textures_batch.")
            return False
            
        abs_source_path = os.path.abspath(source_asset_path)
        log(f"Batch replacing {len(replacements)} textures in {abs_source_path}")
            
        # Create backup if not exists
        bak_path = abs_source_path + ".bak"
        if not os.path.exists(bak_path):
            try:
                shutil.copy2(abs_source_path, bak_path)
                log(f"  Created backup: {os.path.basename(bak_path)}")
            except Exception as e:
                log(f"  Failed to create backup: {e}")
                return False
        
        # 1. Load the asset COMPLETELY into memory FIRST.
        # This is critical to avoid "Read out of bounds" errors if we truncate the file later.
        try:
            env = UnityPy.load(abs_source_path)
            
            modified_count = 0
            # Pre-filter objects by type to speed up lookup
            tex_map = {obj.path_id: obj for obj in env.objects if obj.type.name == "Texture2D"}
            
            for path_id, new_image_path in replacements:
                if path_id in tex_map:
                    obj = tex_map[path_id]
                    try:
                        # Attempt to read and modify metadata
                        data = obj.read()
                        
                        orig_format = getattr(data, 'm_TextureFormat', None)
                        if high_quality and orig_format is not None:
                            # Try to force BC7 (25) for high quality
                            try:
                                # We temporarily change the format to trick UnityPy's save() into encoding it as BC7
                                data.m_TextureFormat = 25 # BC7
                                with Image.open(new_image_path) as img:
                                    data.image = img
                                    data.save()
                                # Success!
                            except Exception as bc_err:
                                # Fallback if BC7 encoder is missing or fails
                                log(f"      Warning: BC7 encoding failed for ID {path_id}: {bc_err}. Falling back to original format.")
                                data.m_TextureFormat = orig_format
                                with Image.open(new_image_path) as img:
                                    data.image = img
                                    data.save()
                        else:
                            # Standard replacement (Preserve original format)
                            with Image.open(new_image_path) as img:
                                data.image = img
                                data.save()
                        modified_count += 1
                        # Success message suppressed for brevity in log window, but count is tracked
                    except Exception as e:
                        log(f"    Error modifying Texture2D {path_id} in {os.path.basename(abs_source_path)}: {e}")
                        # Continue with other replacements even if one fails
                else:
                    log(f"Warning: Texture2D {path_id} not found in {os.path.basename(abs_source_path)}")

            if modified_count > 0:
                # 2. Capture OLD file stats before overwriting
                old_crc32 = self.calculate_crc32(abs_source_path)
                old_size = os.path.getsize(abs_source_path)

                # 3. Save the modified environment to a buffer
                try:
                    # new_data = env.file.save(packer=target_packer)
                    new_data = env.file.save(packer='original')
                except Exception as save_err:
                    # log(f"  Compression failed (Flag {target_packer}): {save_err}")
                    log(f"  Compression failed (Flag original): {save_err}")
                    log(traceback.format_exc())
                    log("  Attempting fallback to uncompressed (Packer 0)...")
                    try:
                        new_data = env.file.save(packer=0)
                    except Exception as fallback_err:
                        log(f"  Critical: Fallback also failed: {fallback_err}")
                        raise fallback_err
                
                # 4. ONLY THEN open the file for writing and save it.
                with open(abs_source_path, "wb") as f:
                    f.write(new_data)
                
                log(f"  Successfully wrote {len(new_data)} bytes to {os.path.basename(abs_source_path)}")
                
                # 5. Capture NEW file stats
                new_size = len(new_data)
                new_crc32 = self.calculate_crc32(abs_source_path)
                new_md5 = hashlib.md5(new_data).hexdigest()
                
                # We need the old MD5 too for matching
                # But since we already have the new CRC/Size, let's just use those for matching
                # and patch MD5 if we find a string at the same index in parallel arrays.
                
                # 6. Addressables Catalog Support: Update metadata in catalog.json
                if abs_source_path.lower().endswith('.bundle'):
                    self.update_addressable_catalogs(abs_source_path, old_crc32, new_crc32, old_size, new_size, new_md5)
                
                # Explicitly close and delete to release handles
                del env
                return True
            else:
                log(f"  No textures were actually modified in {os.path.basename(abs_source_path)}")
            
        except Exception as e:
            log(f"Critical error during batch replacement in {os.path.basename(abs_source_path)}: {e}")
            log(traceback.format_exc())
            return False
            
        return False

    def save_metadata(self, textures, output_dir):
        """Saves extraction metadata to a JSON file."""
        metadata_path = os.path.join(output_dir, "metadata.json")
        try:
            with open(metadata_path, 'w', encoding='utf-8') as f:
                json.dump(textures, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"Failed to save metadata: {e}")
            return False

    def load_metadata(self, output_dir):
        """Loads extraction metadata from a JSON file."""
        metadata_path = os.path.join(output_dir, "metadata.json")
        if not os.path.exists(metadata_path):
            return None
        try:
            with open(metadata_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"Failed to load metadata: {e}")
            return None

    def calculate_crc32(self, file_path):
        """Calculates the CRC32 checksum of a file."""
        crc = 0
        with open(file_path, 'rb') as f:
            while chunk := f.read(8192):
                crc = zlib.crc32(chunk, crc)
        return crc & 0xFFFFFFFF

    def update_addressable_catalogs(self, bundle_path, old_crc, new_crc, old_size, new_size, new_md5):
        """
        Attempts to find and update the CRC and Size for the bundle in any nearby catalog.json files.
        This helps prevent freezes in games that use Unity Addressables.
        """
        bundle_name = os.path.basename(bundle_path)
        print(f"Updating Addressables metadata for {bundle_name}")
        print(f"  Old CRC: {old_crc:#10x}, New CRC: {new_crc:#10x}")
        print(f"  Old Size: {old_size}, New Size: {new_size}")
        print(f"  New MD5: {new_md5}")
        
        # Search for catalog.json in parent directories (up to 3 levels)
        search_dir = os.path.dirname(bundle_path)
        for _ in range(4):
            cat_path = os.path.join(search_dir, "catalog.json")
            if os.path.exists(cat_path):
                self._patch_catalog(cat_path, bundle_name, old_crc, new_crc, old_size, new_size, new_md5)
                # Also look for timestamped catalogs (catalog_2024.11.12.12.34.56.json)
                for f in os.listdir(search_dir):
                    if f.startswith("catalog_") and f.endswith(".json"):
                        self._patch_catalog(os.path.join(search_dir, f), bundle_name, old_crc, new_crc, old_size, new_size, new_md5)
                break
            
            parent = os.path.dirname(search_dir)
            if parent == search_dir: break
            search_dir = parent

    def _patch_catalog(self, cat_path, bundle_name, old_crc, new_crc, old_size, new_size, new_md5):
        """
        Deep patches a specific catalog.json file.
        Addressables store numeric metadata in several parallel arrays (m_BucketData, m_ExtraData, etc).
        Instead of identifying the exact version, we search for the original CRC/Size values
        at the same index where the bundle name was found.
        """
        try:
            # Read current data
            with open(cat_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            modified = False
            
            # 1. Find the bundle index in m_InternalIds
            internal_ids = data.get("m_InternalIds", [])
            bundle_idx = -1
            for i, uid in enumerate(internal_ids):
                # The ID can be just the name, or a path like "StandaloneWindows64/name.bundle"
                if bundle_name in uid:
                    bundle_idx = i
                    break
            
            if bundle_idx == -1:
                return # Bundle not in this catalog

            print(f"  Located {bundle_name} at index {bundle_idx} in {os.path.basename(cat_path)}")

            # 2. Search and Replace numeric/string metadata
            # We look for arrays that have the old_crc or old_size at bundle_idx
            # CRC32 can be stored as signed or unsigned in JSON.
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
                                print(f"    Patched CRC at index {bundle_idx}")
                            elif val == old_size:
                                obj[bundle_idx] = new_size
                                modified = True
                                print(f"    Patched Size at index {bundle_idx}")
                        elif isinstance(val, str) and len(val) == 32:
                            # Heuristic: If it's a 32-char string, it's likely an MD5 hash
                            # We patch it with the NEW hash since we can't easily verify the old one 
                            # if it's salt-hashed, but standard Addressables is plain MD5.
                            # Usually there's only one MD5 per bundle index.
                            obj[bundle_idx] = new_md5
                            modified = True
                            print(f"    Patched MD5 Hash at index {bundle_idx}")
                
                    for item in obj:
                        patch_recursive(item)
                elif isinstance(obj, dict):
                    for k, v in obj.items():
                        patch_recursive(v)

            patch_recursive(data)
            
            # If we detected a modification, save catalog and update hash file
            if modified:
                # Create backup if it doesn't exist, ONLY when modification is detected
                bak_path = cat_path + ".bak"
                if not os.path.exists(bak_path):
                    shutil.copy2(cat_path, bak_path)
                    print(f"  Created backup: {os.path.basename(bak_path)}")

                with open(cat_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, separators=(',', ':')) # Use compact separators
                
                print(f"  Saved patched catalog: {os.path.basename(cat_path)}")

                # Update catalog.json.hash (Crucial for remote providers)
                hash_path = cat_path + ".hash"
                if os.path.exists(hash_path):
                    with open(cat_path, 'rb') as f:
                        content = f.read()
                        new_hash = hashlib.md5(content).hexdigest()
                        with open(hash_path, 'w', encoding='utf-8') as hf:
                            hf.write(new_hash)
                        print(f"  Updated hash file: {os.path.basename(hash_path)}")
            else:
                print(f"  No numeric match found for CRC/Size in {os.path.basename(cat_path)}. Heuristic patching skipped.")

        except Exception as e:
            print(f"  Failed to patch catalog {cat_path}: {e}")

    def manual_decode_texture2d(self, obj, log_callback=None):
        """
        Manually parses Texture2D raw data for non-standard Unity versions (e.g. 2021.3.45 with missing fields).
        Based on pattern: Name (stream) + 12-byte padding -> Width -> Height -> CompleteSize -> [4b] -> Format.
        """
        import struct
        import io
        from UnityPy.export.Texture2DConverter import get_image_from_texture2d
        
        def dlog(msg):
            if log_callback: log_callback(f"      [MANUAL] {msg}")

        try:
            raw = obj.get_raw_data()
            if len(raw) < 64: 
                dlog("Data too short")
                return None
            
            # RELENTLESS Name Search: Try every single offset for standard name read or brute-force
            found_name_info = None
            try:
                # Try standard name length decode at multiples of 1, 2, 4
                for n_off in range(0, min(len(raw), 128), 1):
                    n_len = struct.unpack("<i", raw[n_off:n_off+4])[0]
                    if 2 <= n_len <= 128 and (n_off + 4 + n_len) < len(raw):
                        n_bytes = raw[n_off + 4 : n_off + 4 + n_len]
                        try:
                            n_text = n_bytes.decode('utf-8')
                            if all(32 <= ord(c) <= 126 for c in n_text):
                                found_name_info = (n_text, n_off + 4 + n_len)
                                dlog(f"Dynamic name found: {n_text} at {n_off}")
                                break
                        except: continue
            except: pass

            if not found_name_info:
                dlog("Dynamic name search failed. Brute-forcing target name...")
                try:
                    target_name = getattr(obj, "m_Name", "") or "UnitySplash-cube"
                    name_bytes = target_name.encode('utf-8')
                    idx = raw.find(name_bytes)
                    if -1 < idx < 1024:
                        found_name_info = (target_name, idx + len(name_bytes))
                        dlog(f"Brute-force found name: {target_name} at {idx}")
                except: pass

            if found_name_info:
                name, pos = found_name_info
            else:
                name, pos = getattr(obj, "m_Name", "Unknown"), 4
            
            if pos % 4 != 0:
                pos += 4 - (pos % 4)
            
            # --- SMART HEADER SCAN ---
            # Collect ALL potential [W, H] pairs in first 2KB of header
            header_ints = []
            scan_limit = min(len(raw), 2048)
            # Scan every 2 bytes to catch unaligned shorts/ints
            for i in range(pos + 4, scan_limit - 4, 2):
                val = struct.unpack("<i", raw[i:i+4])[0]
                if 1 <= val <= 16384:
                    header_ints.append((i, val))
            
            # 3. Collect ALL potential data vectors
            vector_candidates = []
            for i in range(pos + 16, scan_limit - 4, 4):
                v_size = struct.unpack("<I", raw[i:i+4])[0]
                if 1024 <= v_size <= (len(raw) - (i + 4)):
                    rem = len(raw) - (i + 4)
                    # Support splitting padding (up to 4KB)
                    if v_size <= rem <= v_size + 4096:
                        vector_candidates.append((i + 4, v_size))
            
            # RELENTLESS Stream Scan
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
                                s_bytes = raw[j+4 : j+4+s_len]
                                s_text = s_bytes.decode('utf-8', 'ignore')
                                if ext.decode().lower() in s_text.lower():
                                    s_start = j; break
                        except: continue
                    
                    if s_start != -1:
                        # Find nearby metadata
                        pot_size, pot_offset = 0, 0
                        # Pattern: [Offset 8, Size 4, Path String]
                        try:
                            pot_size = struct.unpack("<I", raw[s_start - 4 : s_start])[0]
                            pot_offset = struct.unpack("<Q", raw[s_start - 12 : s_start - 4])[0]
                        except: pass
                        # Pattern fallback
                        if pot_size < 1024:
                            try:
                                s_end = s_start + 4 + struct.unpack("<i", raw[s_start:s_start+4])[0]
                                if s_end % 4 != 0: s_end += 4 - (s_end % 4)
                                pot_offset = struct.unpack("<Q", raw[s_end : s_end + 8])[0]
                                pot_size = struct.unpack("<I", raw[s_end + 8 : s_end + 12])[0]
                            except: pass

                        if 1024 < pot_size < 1024 * 1024 * 1024:
                            stream_info = {'path': s_text, 'offset': pot_offset, 'size': pot_size, 'header_idx': s_start}
                            dlog(f"Detected Stream: {s_text} (Size: {pot_size})")
                            break
            
            if log_callback and (obj.path_id == 26 or obj.path_id == 105 or "cursor" in name.lower()):
                hex_head = raw[pos:pos+160].hex(' ', 4)
                log_callback(f"      [DUMP] ID {obj.path_id} ({name}) Header: {hex_head}")

            # --- DYNAMIC STRUCTURAL SCANNER (Robust for 2021.3.x shifts) ---
            # Collect ALL potential [W, H, Size, Format] candidates and pick the best one
            candidates = []
            factors = [
                (4, 4), (3, 3), (2, 2), (1, 12), (0.5, 10), (2, 13), 
                (1, 1), (4, 5), (4, 26), (4, 27), (2, 47), (1, 34), (1, 45)
            ]
            
            for i in range(pos, min(len(raw), pos + 160), 4):
                try:
                    w_t, h_t, size_t = struct.unpack("<iiI", raw[i:i+12])
                    if 32 <= w_t <= 16384 and 32 <= h_t <= 16384: # Prefer reasonable sizes first
                        for f_off in [12, 16, 20, 24]:
                            if i + f_off + 4 > len(raw): continue
                            fmt_t = struct.unpack("<i", raw[i+f_off:i+f_off+4])[0]
                            if 1 <= fmt_t <= 64:
                                # Validation: size_t must be somewhat proportional to w_t * h_t
                                ratio = size_t / (w_t * h_t) if (w_t * h_t) > 0 else 0
                                if 0.1 <= ratio <= 16:
                                    candidates.append((w_t, h_t, size_t, fmt_t, i))
                except: continue

            # Fallback if no reasonable sized candidates found
            if not candidates:
                for i in range(pos, min(len(raw), pos + 160), 4):
                    try:
                        w_t, h_t, size_t = struct.unpack("<iiI", raw[i:i+12])
                        if 1 <= w_t <= 16384 and 1 <= h_t <= 16384:
                            for f_off in [12, 16, 20, 24]:
                                if i + f_off + 4 > len(raw): continue
                                fmt_t = struct.unpack("<i", raw[i+f_off:i+f_off+4])[0]
                                if 1 <= fmt_t <= 64:
                                    candidates.append((w_t, h_t, size_t, fmt_t, i))
                    except: continue

            discovered_w, discovered_h, discovered_size, discovered_fmt = 0, 0, 0, 0
            discovered_colorspace = 1
            
            if candidates:
                # Sort by resolution area (W * H) descending to find the "real" main texture
                candidates.sort(key=lambda x: x[0] * x[1], reverse=True)
                discovered_w, discovered_h, discovered_size, discovered_fmt, found_idx = candidates[0]
                found_h_match = True
                if log_callback:
                    log_callback(f"      [SCAN] ID {obj.path_id}: Found {len(candidates)} candidates. Selected: {discovered_w}x{discovered_h} (Fmt: {discovered_fmt}, Size: {discovered_size})")
            else:
                found_h_match = False

            if not found_h_match:
                dlog("Dynamic scanner failed. Using fallback offsets.")
                try:
                    discovered_w = struct.unpack("<i", raw[pos+8:pos+12])[0]
                    discovered_h = struct.unpack("<i", raw[pos+12:pos+16])[0]
                    discovered_fmt = struct.unpack("<i", raw[pos+24:pos+28])[0]
                except: pass

            discovered_faces = 1
            image_data = None
            found_start = -1
            
            # CASE A: Streamed Data
            if stream_info:
                # Use stream metadata if scanner failed to find anything better
                if discovered_w == 0:
                    discovered_w, discovered_h, discovered_fmt = stream_info.get('w', 1024), stream_info.get('h', 1024), 4
                
                stream_file_data = None
                asset_full_path = getattr(obj.assets_file, "path", None)
                if not asset_full_path and hasattr(obj.assets_file, "parent"):
                    asset_full_path = getattr(obj.assets_file.parent, "path", None)
                asset_dir = os.path.dirname(asset_full_path) if asset_full_path else os.getcwd()
                fname = os.path.basename(stream_info['path'].replace("archive:/", "").replace("\\", "/"))
                stream_path = os.path.join(asset_dir, fname)
                
                if os.path.exists(stream_path):
                    try:
                        with open(stream_path, "rb") as f:
                            f.seek(stream_info['offset'])
                            image_data = f.read(stream_info['size'])
                    except: pass
                
                # Bundle Fallback
                if not image_data and hasattr(obj.assets_file, "environment"):
                    env_files = getattr(obj.assets_file.environment, "files", {})
                    target_file = env_files.get(stream_info['path']) or env_files.get(fname)
                    if target_file:
                        try:
                            target_file.seek(stream_info['offset'])
                            image_data = target_file.read(stream_info['size'])
                        except: pass

            # CASE B: Local Data
            if not image_data:
                # Standard Unity ByteArray pattern: [Size (4b)] [Data (Size bytes)]
                # The data block is almost always at the very end of the raw object data.
                # We search for the size field that matches our discovered_size.
                search_limit = found_idx + 12 # Skip the metadata we already found
                for i in range(len(raw) - discovered_size - 4, search_limit - 1, -4):
                    try:
                        v_len = struct.unpack("<I", raw[i:i+4])[0]
                        if v_len == discovered_size:
                            test_start = i + 4
                            # Alignment check (optional but good for sanity)
                            target_align = 16 if discovered_fmt == 12 else (8 if discovered_fmt == 10 else 4)
                            image_data = raw[test_start : test_start + v_len]
                            found_start = test_start
                            dlog(f"MATCH (Local)! {discovered_w}x{discovered_h}, Format {discovered_fmt} at Absolute Offset {test_start}")
                            break
                    except: continue

            if not image_data:
                dlog("Data retrieval failed.")
                return None
            
            # --- COLORSPACE & STRIDE FIX ---
            if discovered_fmt in [1, 2, 3, 4, 5, 13]:
                bpp = 4 if discovered_fmt in [4, 5] else (3 if discovered_fmt == 3 else (2 if discovered_fmt in [2, 13] else 1))
                image_data = self.apply_stride_correction(image_data, discovered_w, discovered_h, bpp, dlog)
            
            discovery_factor = next((f for f,fmt in factors if fmt == discovered_fmt), 1.0)
            
            # 5. Create a Mock object for UnityPy's converter
            class MockTexture2D:
                def __init__(self, name, w, h, fmt, data, obj, faces=1, factor=1.0, color_space=1):
                    self.m_Name = name
                    self.m_Width = w
                    self.m_Height = h
                    self.m_TextureFormat = fmt
                    self.image_data = data
                    self.m_StreamData = None 
                    self.assets_file = obj.assets_file
                    self.version = obj.assets_file.version
                    # Mipmap Safety: calculate base size for 1 face
                    base_size = int(w * h * factor)
                    if len(data) < base_size:
                        ratio = (len(data) / base_size) ** 0.5
                        self.m_Width = max(4, int(w * ratio) // 4 * 4)
                        self.m_Height = max(4, int(h * ratio) // 4 * 4)
                        self.m_MipCount = 1
                    else:
                        self.m_MipCount = 1 # Force 1 to avoid reading past buffer
                        
                    self.m_TextureDimension = 4 if faces == 6 else 2
                    self.m_CompleteImageSize = len(data)
                    self.m_ColorSpace = color_space
                    self.m_IsReadable = True
                    self.m_LightmapFormat = 0
                    self.object_reader = obj
                
                def get_image_data(self):
                    return self.image_data

            # Convert texture_format int to UnityPy IntEnum
            try:
                from UnityPy.enums import TextureFormat
                mock_fmt_enum = TextureFormat(discovered_fmt)
            except:
                mock_fmt_enum = discovered_fmt

            # Construct finalized mock object
            mock_obj = MockTexture2D(name, discovered_w, discovered_h, mock_fmt_enum, image_data, obj, 
                                     faces=discovered_faces, factor=discovery_factor, color_space=discovered_colorspace)
            
            try:
                from UnityPy.export import Texture2DConverter
                # Correct method name is get_image_from_texture2d
                img = Texture2DConverter.get_image_from_texture2d(mock_obj)
            except Exception as te:
                dlog(f"UnityPy Converter failed: {te}")
                img = None
            
            if img: 
                dlog(f"Successfully decoded {'Cubemap' if discovered_faces==6 else 'Texture'} to PIL")
                return img
            return None
            
        except Exception as e:
            dlog(f"Global manual_decode error: {e}")
            return None

if __name__ == "__main__":
    # Test stub
    pass
