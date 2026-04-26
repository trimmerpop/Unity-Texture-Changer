import sys
import os
import shutil
import json
import zipfile
import subprocess
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                             QPushButton, QLineEdit, QLabel, QFileDialog, QTableWidget, 
                             QTableWidgetItem, QHeaderView, QCheckBox, QProgressBar, QMenu,
                             QMessageBox, QInputDialog, QDialog, QListWidget, QListWidgetItem,
                             QPlainTextEdit, QTreeWidget, QTreeWidgetItem, QComboBox, QSplitter)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, pyqtSlot, QObject, QSize, QUrl, QPoint, QRect, QCoreApplication, QSettings
from PyQt6.QtGui import QIcon, QAction, QDragEnterEvent, QDropEvent, QMouseEvent, QWheelEvent, QPixmap, QImage, QPainter, QPen, QFontMetrics
from PIL import Image
from asset_manager import AssetManager
from similarity import get_image_hash, hamming_similarity, compare_images, load_image
from comparison_widget import ComparisonWidget
import ctypes # For Windows taskbar icon fix
import cv2
import time
from packaging import version

def set_app_user_model_id():
    """
    Sets the Application User Model ID for the current process.
    This helps Windows 7+ to correctly group taskbar icons.
    """
    if sys.platform == "win32":
        # Arbitrary unique string for your application
        my_app_id = u"UnityTextureChanger.App" 
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(my_app_id)


class SortableTreeWidgetItem(QTreeWidgetItem):
    def natural_key(self, text):
        import re
        return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', text)]

    @staticmethod
    def get_pixels(text):
        if not text or 'x' not in text: return 0
        try:
            ps = text.lower().strip().split('x')
            return int(ps[0]) * int(ps[1])
        except: return 0

    @staticmethod
    def get_bytes(text):
        if not text: return 0
        try:
            t = text.upper().strip()
            if 'MB' in t: return float(t.replace('MB', '')) * 1024 * 1024
            if 'KB' in t: return float(t.replace('KB', '')) * 1024
            return float(t.replace('B', '')) if 'B' in t else 0
        except: return 0

    def __lt__(self, other):
        column = self.treeWidget().sortColumn()
        header = self.treeWidget().header()
        order = header.sortIndicatorOrder()
        
        # Helper for stable comparison (Tie-breaker chain)
        def tie_breaker():
            # 1. Name Natural (Primary fallback)
            t1 = self.text(1)
            t2 = other.text(1)
            n1 = self.natural_key(t1)
            n2 = other.natural_key(t2)
            if n1 != n2: return n1 < n2
            if t1 != t2: return t1 < t2
            
            # 2. Resolution (Secondary fallback)
            # Use original resolution (Col 2)
            v1 = self.get_pixels(self.text(2))
            v2 = self.get_pixels(other.text(2))
            if v1 != v2: return v1 < v2
            
            # 3. Size (Tertiary fallback)
            # Use original size (Col 3)
            v1 = self.get_bytes(self.text(3))
            v2 = self.get_bytes(other.text(3))
            if v1 != v2: return v1 < v2

            # 4. Ultimate persistent tie-breaker: Path
            d1 = self.data(1, Qt.ItemDataRole.UserRole)
            d2 = other.data(1, Qt.ItemDataRole.UserRole)
            p1 = ""
            p2 = ""
            if isinstance(d1, dict):
                p1 = d1.get('original', {}).get('path', '') or d1.get('save_path', '')
            if isinstance(d2, dict):
                p2 = d2.get('original', {}).get('path', '') or d2.get('save_path', '')
            
            if p1 != p2: return p1 < p2
            return False

        # If this is a child item (Candidate), ALWAYS sort by Similarity (Col 4) DESCENDING
        if self.parent():
            try:
                s1 = float(self.text(4) or "0")
                s2 = float(other.text(4) or "0")
                if s1 != s2:
                    if order == Qt.SortOrder.AscendingOrder:
                        return s1 > s2
                    else:
                        return s1 < s2
            except: pass
            return tie_breaker()

        # Column 0: Replace Checkbox (Parents Only)
        if column == 0:
            v1 = self.data(0, Qt.ItemDataRole.UserRole)
            v2 = other.data(0, Qt.ItemDataRole.UserRole)
            if v1 is not None and v2 is not None:
                if v1 != v2:
                    return v1 > v2  # Checked before Unchecked

        # Natural sorting for Name (1, 5)
        if column in (1, 5):
            t1 = self.text(column)
            t2 = other.text(column)
            n1 = self.natural_key(t1)
            n2 = other.natural_key(t2)
            if n1 != n2: return n1 < n2
            if t1 != t2: return t1 < t2
            return tie_breaker()

        # Column 2, 6: Resolution (Numeric)
        if column in (2, 6):
            v1 = self.get_pixels(self.text(column))
            v2 = self.get_pixels(other.text(column))
            if v1 != v2: return v1 < v2

        # Column 3, 7: Size (Numeric)
        if column in (3, 7):
            v1 = self.get_bytes(self.text(column))
            v2 = self.get_bytes(other.text(column))
            if v1 != v2: return v1 < v2

        # Column 4: Similarity (Numeric)
        if column == 4:
            try:
                s1 = float(self.text(4) or "0")
                s2 = float(other.text(4) or "0")
                if s1 != s2: return s1 < s2
            except: pass

        # If everything else is equal, use tie-breaker
        return tie_breaker()

class ThumbnailLabel(QLabel):
    clicked = pyqtSignal(float, float) # normalized x, y
    wheeled = pyqtSignal(int) # delta

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self._handle_click(event.position())

    def mouseMoveEvent(self, event: QMouseEvent):
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._handle_click(event.position())

    def _handle_click(self, pos):
        # Clamp to label size
        x = max(0, min(self.width(), pos.x()))
        y = max(0, min(self.height(), pos.y()))
        x_norm = x / self.width() if self.width() > 0 else 0.5
        y_norm = y / self.height() if self.height() > 0 else 0.5
        self.clicked.emit(x_norm, y_norm)

    def wheelEvent(self, event: QWheelEvent):
        self.wheeled.emit(event.angleDelta().y())
        event.accept()

class PathLineEdit(QLineEdit):
    def __init__(self, placeholder="", parent=None):
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setAcceptDrops(True)
        self.setReadOnly(False)

    def mouseDoubleClickEvent(self, event):
        path = QFileDialog.getExistingDirectory(self, "Select Folder")
        if path:
            self.setText(path)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        urls = event.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            self.setText(path)

def get_base_name(name):
    """Strips Unity-style _PathID suffixes (e.g., _12345) from texture names."""
    import re
    # Match underscore followed by one or more digits at the end of the string
    return re.sub(r'_\d+$', '', name)

class EnhancedTreeWidget(QTreeWidget):
    """QTreeWidget subclass that supports shortcuts like Space, Delete, and keypad +/-."""
    sig_delete_items = pyqtSignal()
    sig_check_all = pyqtSignal(bool)
    sig_inverse_all = pyqtSignal()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space:
            selected = self.selectedItems()
            if selected:
                # Sub-items (candidates) should ALWAYS be checked, not toggled.
                # Top-level items (originals) should be toggled based on the first item's current state.
                first_item = selected[0]
                if first_item.parent():
                    # If the first selected item is a sub-item, we intend to check all selected sub-items.
                    new_state_for_parents = Qt.CheckState.Checked 
                else:
                    first_state = first_item.checkState(0)
                    new_state_for_parents = Qt.CheckState.Checked if first_state == Qt.CheckState.Unchecked else Qt.CheckState.Unchecked
                
                for item in selected:
                    if item.parent():
                        # Candidates: Always check
                        item.setCheckState(0, Qt.CheckState.Checked)
                    else:
                        # Originals: Toggle
                        item.setCheckState(0, new_state_for_parents)
                
                # Prevent default space handling (scrolling)
                event.accept()
                return
        elif event.key() == Qt.Key.Key_Delete:
            self.sig_delete_items.emit()
            event.accept()
            return
        elif event.key() in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            # Equal key handles shift+= which is the standard Plus char
            self.sig_check_all.emit(True)
            event.accept()
            return
        elif event.key() == Qt.Key.Key_Minus:
            self.sig_check_all.emit(False)
            event.accept()
            return
        elif event.key() == Qt.Key.Key_Asterisk:
            self.sig_inverse_all.emit()
            event.accept()
            return

        super().keyPressEvent(event)

class WorkerThread(QThread):
    sig_progress = pyqtSignal(int, str)
    sig_log = pyqtSignal(str)
    sig_error = pyqtSignal(str, str) # title, message
    sig_finished = pyqtSignal(object)
    
    def __init__(self, task_type, **kwargs):
        super().__init__()
        self.task_type = task_type
        self.args = kwargs
        self._temp_apk_folders = [] # Keep track of temporary folders created during APK extraction

    def _find_uber_apk_signer_jar(self):
        """Finds the uber-apk-signer jar in the script directory."""
        if getattr(sys, 'frozen', False):
            script_dir = os.path.dirname(os.path.abspath(sys.executable))
        else:
            script_dir = os.path.dirname(os.path.abspath(__file__))
        
        # 1. Look for 'uber-apk-signer.jar'
        default_jar_path = os.path.join(script_dir, "uber-apk-signer.jar")
        if os.path.exists(default_jar_path):
            return default_jar_path

        # 2. Look for 'uber-apk-signer-*.jar' and pick the latest version
        import re
        jars = [f for f in os.listdir(script_dir) if f.startswith("uber-apk-signer-") and f.endswith(".jar")]
        if not jars: return None
        
        try:
            jars.sort(key=lambda f: version.parse(re.search(r'uber-apk-signer-(.*?)\.jar', f).group(1)), reverse=True)
            return os.path.join(script_dir, jars[0])
        except Exception:
            # Fallback if versioning fails
            jars.sort(reverse=True)
            return os.path.join(script_dir, jars[0])

    def run(self):
        if self.task_type == "extract":
            self.run_extract()
        elif self.task_type == "match":
            self.run_match()
        elif self.task_type == "apply":
            self.run_apply()
        elif self.task_type == "copy":
            self.run_copy()

    def run_apply(self):
        to_replace = self.args['to_replace']
        orig_dir = self.args['orig_dir']
        mod_dir = self.args['mod_dir']
        temp_orig = self.args['temp_orig']
        temp_mod = self.args['temp_mod']
        mode = self.args['mode']
        high_quality = self.args.get('high_quality', False)
        
        am = AssetManager(orig_dir, mod_dir, temp_orig, temp_mod)
        total = len(to_replace)
        count = 0
        
        self.sig_log.emit(f"Active Apply Mode: {mode}")
        
        # 1. GROUPING: Separate Unity replacements from direct file replacements
        unity_groups = {} # source_file -> {'replacements': [], 'results': []}
        direct_files = [] # list of (o_path, new_image_path, o_name)
        
        for res in to_replace:
            o = res['original']
            m_path = res['match_file']
            if not m_path: continue
            
            if 'source_asset' in o and 'path_id' in o:
                src = o['source_asset']
                if src not in unity_groups: unity_groups[src] = {'replacements': [], 'results': []}
                unity_groups[src]['replacements'].append((o['path_id'], m_path))
                unity_groups[src]['results'].append(res)
            else:
                direct_files.append(res)

        total_unity = sum(len(data['replacements']) for data in unity_groups.values())
        total_direct = len(direct_files)
        
        processed_count = 0
        
        # 2. PHASE 1: Process Unity Asset Replacements
        if unity_groups:
            self.sig_log.emit(f"Processing Unity assets for {total_unity} replacements...")
            for src_file, group_data in unity_groups.items():
                original_src = src_file
                replacements = group_data['replacements']
                
                # REBASE LOGIC: Check if asset exists at recorded path
                # If in APK mode, it should already be pointing to the extracted folder.
                if not os.path.exists(src_file):
                    fname = os.path.basename(src_file)
                    self.sig_log.emit(f"Asset path out-of-date: {fname}. Searching in {orig_dir}...")
                    
                    found_path = None
                    for root, dirs, fnames in os.walk(orig_dir):
                        if fname in fnames:
                            found_path = os.path.join(root, fname)
                            break
                    
                    if found_path:
                        src_file = found_path
                        self.sig_log.emit(f"Rebased asset path: {os.path.basename(original_src)} -> {src_file}")
                    else:
                        self.sig_log.emit(f"ERROR: Could not find {fname} in current folder {orig_dir}. Skipping.")
                        processed_count += len(replacements)
                        continue

                self.sig_progress.emit(int((processed_count/total)*100), f"Applying to assets: {os.path.basename(src_file)}...")
                success = am.replace_textures_batch(src_file, replacements, log_callback=self.sig_log.emit, high_quality=high_quality)
                if success:
                    count += len(replacements)
                    # Sync successful replacements to original file paths
                    for res in group_data['results']:
                        try:
                            # Copy the modified image to overwrite the original extracted image
                            if res.get('match_file'):
                                shutil.copy2(res['match_file'], res['original']['save_path'])
                                res['best_similarity'] = 1.0
                        except Exception as e:
                            self.sig_log.emit(f"Sync failed for {res['original']['name']}: {e}")
                processed_count += len(replacements)
        
        # 3. PHASE 2: Process Direct File Replacements (Non-Unity)
        if direct_files:
            self.sig_log.emit(f"Processing direct file copies for {total_direct} items...")
            for i, res in enumerate(direct_files):
                o = res['original']
                
                # SAFETY CHECK: If save_path is in temp folder, this is likely WRONG
                if temp_orig in os.path.abspath(o['save_path']):
                     self.sig_log.emit(f"WARNING: Applying {o['name']} to TEMP folder instead of GAME folder.")
                     self.sig_log.emit(f"  Target: {o['save_path']}")
                     self.sig_log.emit("  If this is a Unity game, extraction metadata for 'source_asset' or 'path_id' might be missing.")
                
                self.sig_progress.emit(int((processed_count/total)*100), f"Copying files {i+1}/{total_direct}: {o['name']}")
                try:
                    shutil.copy2(res['match_file'], o['save_path'])
                    count += 1
                    res['best_similarity'] = 1.0
                except Exception as e:
                    self.sig_log.emit(f"Failed to overwrite {o['save_path']}: {e}")
                processed_count += 1

        # 4. PHASE 3: APK Repacking (If Unity APK mode)
        if mode == "Unity APK" and 'apk_info' in self.args:
            apk_info = self.args['apk_info'] # {original_apk_path: temp_dir}
            self.sig_log.emit("Starting APK repacking process...")
            
            for original_apk_path, extract_dir in apk_info.items():
                self.sig_log.emit(f"Repacking for {os.path.basename(original_apk_path)}...")
                base, ext = os.path.splitext(original_apk_path)
                output_apk_path = f"{base}_mod{ext}"
                
                # Identify modified files in this APK's extract dir
                modified_files = []
                for res in to_replace:
                    if 'source_asset' in res['original']:
                        src_asset = res['original']['source_asset']
                        if src_asset.startswith(extract_dir):
                            if src_asset not in modified_files:
                                modified_files.append(src_asset)
                
                if not modified_files:
                    self.sig_log.emit(f"No files modified for APK {os.path.basename(original_apk_path)}")
                    continue

                try:
                    # Create temporary unsigned APK
                    unsigned_apk_path = os.path.join(temp_orig, f"temp_unsigned_{os.path.basename(original_apk_path)}")
                    self.sig_log.emit(f"Rebuilding APK with {len(modified_files)} modified files...")

                    modified_files_map = {os.path.relpath(f, extract_dir): f for f in modified_files}
                    
                    with zipfile.ZipFile(original_apk_path, 'r') as zin:
                        with zipfile.ZipFile(unsigned_apk_path, 'w') as zout:
                            for item in zin.infolist():
                                if item.filename in modified_files_map:
                                    mod_file = modified_files_map[item.filename]
                                    compress_type = zipfile.ZIP_STORED if item.filename.lower().endswith('.so') else zipfile.ZIP_DEFLATED
                                    zout.write(mod_file, item.filename, compress_type=compress_type)
                                    self.sig_log.emit(f"  -> Updated: {item.filename}")
                                else:
                                    buffer = zin.read(item.filename)
                                    zout.writestr(item, buffer)
                    
                    # Sign the APK
                    jar_path = self._find_uber_apk_signer_jar()
                    if not jar_path:
                        self.sig_log.emit("ERROR: uber-apk-signer.jar not found. Cannot sign APK.")
                        self.sig_log.emit(f"Unsigned APK saved to: {unsigned_apk_path}")
                        continue

                    self.sig_log.emit("Signing APK with debug key...")
                    apk_dir = os.path.dirname(unsigned_apk_path)
                    
                    # Command: java -jar uber-apk-signer.jar -a <unsigned_apk> --out <out_dir>
                    cmd = ["java", "-Dfile.encoding=UTF-8", "-jar", jar_path, "-a", unsigned_apk_path, "--out", apk_dir]
                    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
                    
                    if result.returncode != 0:
                        self.sig_log.emit(f"ERROR: signing failed: {result.stderr or result.stdout}")
                        continue
                        
                    # find signed apk
                    base_name = os.path.splitext(os.path.basename(unsigned_apk_path))[0]
                    signed_apk = os.path.join(apk_dir, f"{base_name}-aligned-debugSigned.apk")
                    if os.path.exists(signed_apk):
                        if os.path.exists(output_apk_path): os.remove(output_apk_path)
                        shutil.move(signed_apk, output_apk_path)
                        self.sig_log.emit(f"SUCCESS: Signed APK saved to: {output_apk_path}")
                    else:
                        self.sig_log.emit("ERROR: Could not find signed APK outcome.")

                    if os.path.exists(unsigned_apk_path): os.remove(unsigned_apk_path)

                except Exception as e:
                    self.sig_log.emit(f"EXCEPTION during repacking: {e}")
        
        self.sig_progress.emit(100, "Apply complete.")
        self.sig_finished.emit({"success": count, "total": total})

    def run_copy(self):
        to_replace = self.args['to_replace']
        target_dir = self.args['target_dir']
        orig_dir = self.args.get('orig_dir')
        mode = self.args.get('mode')
        
        count = 0
        total = len(to_replace)
        
        for i, res in enumerate(to_replace):
            src = res['match_file']
            if not src: continue
            
            orig = res['original']
            ext = os.path.splitext(src)[1]

            if mode == "Image" and orig_dir:
                # Preserve directory structure for Image mode
                try:
                    rel_path = os.path.relpath(orig['save_path'], orig_dir)
                    # Ensure extension matches the match_file
                    rel_path_no_ext = os.path.splitext(rel_path)[0]
                    dst_name = rel_path_no_ext + ext
                    dst = os.path.join(target_dir, dst_name)
                except ValueError:
                    dst_name = orig['name'] + ext
                    dst = os.path.join(target_dir, dst_name)
            else:
                # Flat export for Unity/Other modes
                if self.args.get('use_uabea_format'):
                    assets_name = os.path.basename(orig.get('source_asset', 'Unknown'))
                    path_id = orig.get('path_id', '0')
                    dst_name = f"{orig['name']}-{assets_name}-{path_id}{ext}"
                else:
                    dst_name = orig['name'] + ext
                dst = os.path.join(target_dir, dst_name)

            self.sig_progress.emit(int((i/total)*100), f"Copying {i+1}/{total}: {dst_name}")
            
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                count += 1
            except Exception as e:
                self.sig_log.emit(f"Failed to copy {src}: {e}")
        
        self.sig_progress.emit(100, "Copy complete.")
        self.sig_finished.emit({"success": count, "total": total, "target_dir": target_dir})

    def run_extract(self):
        orig_dir = self.args['orig_dir']
        mod_dir = self.args['mod_dir']
        temp_orig = self.args['temp_orig']
        temp_mod = self.args['temp_mod']
        
        settings = QSettings("config.ini", QSettings.Format.IniFormat)
        last_orig = settings.value("last_extract/original", "")
        last_mod = settings.value("last_extract/modified", "")
        
        am = AssetManager(orig_dir, mod_dir, temp_orig, temp_mod)
        
        # Helper for individual folder extraction
        def process_folder(folder_path, temp_path, last_path, prog_start, prog_range, label, scan_all=False):
            # APK Extraction Logic
            is_apk_mode = self.args.get('mode') == "Unity APK"
            current_target_dir = folder_path
            apk_found_path = None
            
            if is_apk_mode:
                # Environment Check for APK Mode
                java_ok = shutil.which("java") is not None
                jar_path = self._find_uber_apk_signer_jar()

                if not java_ok or not jar_path:
                    error_msg = "APK Repacking Environment Check Failed:\n\n"
                    if not java_ok: error_msg += "• Java is not installed or not in your system's PATH.\n"
                    if not jar_path:
                        if getattr(sys, 'frozen', False):
                            script_dir = os.path.dirname(os.path.abspath(sys.executable))
                        else:
                            script_dir = os.path.dirname(os.path.abspath(__file__))
                        error_msg += f"• 'uber-apk-signer.jar' not found in:\n  {script_dir}\n"
                    
                    error_msg += "\nRepacking will NOT be possible. Please ensure dependencies are met."
                    clean_error = error_msg.replace('\n', ' ')
                    self.sig_log.emit(f"ERROR: {clean_error}")
                    self.sig_error.emit("Environment Warning", error_msg)
                
                # Find APK file in folder_path or folder_path itself
                if folder_path.lower().endswith('.apk'):
                    apk_found_path = folder_path
                else:
                    for f in os.listdir(folder_path):
                        if f.lower().endswith('.apk'):
                            apk_found_path = os.path.join(folder_path, f)
                            break
            
            if apk_found_path:
                self.sig_log.emit(f"Extracting APK {os.path.basename(apk_found_path)} for {label}...")
                apk_extract_sub = os.path.join(temp_path, "apk_contents")
                if os.path.exists(apk_extract_sub):
                    try: shutil.rmtree(apk_extract_sub)
                    except: pass
                os.makedirs(apk_extract_sub, exist_ok=True)
                
                try:
                    with zipfile.ZipFile(apk_found_path, 'r') as z:
                        z.extractall(apk_extract_sub)
                    current_target_dir = apk_extract_sub
                    self.sig_log.emit(f"APK extraction complete: {label}")
                    
                    # Store for repacking if this is original
                    if label == "Original":
                        if 'apk_info' not in self.args: self.args['apk_info'] = {}
                        self.args['apk_info'][apk_found_path] = apk_extract_sub
                except Exception as ex:
                    self.sig_log.emit(f"ERROR: APK extraction failed: {ex}")
                    return []

            textures = None
            # If same path AND metadata exists, try to reuse
            # For APK mode, we compare the APK path
            compare_path = apk_found_path if apk_found_path else folder_path
            
            if compare_path == last_path:
                self.sig_log.emit(f"Checking existing {label} extraction...")
                textures = am.load_metadata(temp_path)
                if textures:
                    # Ensure save_path and size are present
                    for t in textures:
                        t['save_path'] = os.path.join(temp_path, t['file'])
                        if 'size' not in t:
                            if os.path.exists(t['save_path']):
                                t['size'] = os.path.getsize(t['save_path'])
                            else:
                                t['size'] = 0
                    self.sig_log.emit(f"Reusing existing {label} extraction ({len(textures)} items).")
                    self.sig_progress.emit(prog_start + prog_range, f"{label} reuse complete.")
                    return textures
            
            # Otherwise, clear and extract
            self.sig_log.emit(f"Extracting textures from {label} assets...")
            # Note: We already cleared temp_path if we extracted APK, but am.extract_textures might need it.
            # But wait, if we extracted APK into temp_path/apk_contents, we shouldn't wipe temp_path.
            if not apk_found_path:
                if os.path.exists(temp_path):
                    try: shutil.rmtree(temp_path)
                    except: pass
                os.makedirs(temp_path, exist_ok=True)
            
            def progress_cb(pct, text):
                prog = prog_start + int(pct * (prog_range / 100))
                self.sig_progress.emit(prog, f"Extracting {label}: {text}")
            
            textures = am.extract_textures(current_target_dir, temp_path, progress_callback=progress_cb, scan_all=scan_all)
            # Ensure save_path and size are present
            for t in textures:
                if 'file' in t and 'save_path' not in t:
                    t['save_path'] = os.path.join(temp_path, t['file'])
                if 'size' not in t:
                    if t.get('save_path', '') and os.path.exists(t['save_path']):
                        t['size'] = os.path.getsize(t['save_path'])
                    else:
                        t['size'] = 0
                if 'name' not in t:
                    t['name'] = os.path.splitext(os.path.basename(t.get('save_path', 'Unknown')))[0]
            am.save_metadata(textures, temp_path)
            
            # Update last_path setting for reuse
            settings.setValue(f"last_extract/{label.lower()}", compare_path)
            settings.sync()
            
            return textures

        # 1. Original
        orig_textures = process_folder(orig_dir, temp_orig, str(settings.value("last_extract/original", "")), 0, 45, "Original", 
                                       self.args['orig_scan_all'])
        
        # 2. Modified
        self.sig_progress.emit(50, "Original complete. Processing Modified...")
        mod_textures = process_folder(mod_dir, temp_mod, str(settings.value("last_extract/modified", "")), 50, 45, "Modified",
                                      self.args['mod_scan_all'])
        
        self.sig_progress.emit(100, "Extraction process complete!")
        self.sig_finished.emit({
            "orig": orig_textures, 
            "mod": mod_textures,
            "apk_info": self.args.get('apk_info', {})
        })

    def run_match(self):
        orig_textures = self.args['orig_textures']
        mod_textures = self.args['mod_textures']
        num_candidates = self.args['num_candidates']
        unchecked_same = self.args['unchecked_same']
        same_res = self.args['same_res']
        filter_enabled = self.args.get('filter_enabled', False)
        sim_cutoff = self.args.get('sim_cutoff', 0.5)

        self.sig_log.emit(f"Matching {len(orig_textures)} originals against {len(mod_textures)} modified textures.")

        self.sig_log.emit("Hashing Modified textures...")
        total_mod = len(mod_textures)
        for i, tex in enumerate(mod_textures):
            if 'hash' not in tex:
                tex['hash'] = get_image_hash(tex['save_path'])
            if i % 20 == 0:
                self.sig_progress.emit(int((i/total_mod)*100), f"Hashing Modified {i}/{total_mod}...")

        self.sig_log.emit("Matching textures...")
        results = []
        for i, o_tex in enumerate(orig_textures):
            # if o_tex['name'] == 'HCCG_insert_0007':
            #     breakpoint()
            o_tex['hash'] = get_image_hash(o_tex['save_path'])
            
            candidate_pool = {} # save_path -> m_tex
            
            # 1. Name match candidates (including base name to ignore _PathID)
            o_name_lower = o_tex['name'].lower()
            o_base_lower = get_base_name(o_tex['name']).lower()
            
            for m_tex in mod_textures:
                # Name matching should bypass resolution filter
                if m_tex['name'].lower() == o_name_lower or get_base_name(m_tex['name']).lower() == o_base_lower:
                    candidate_pool[m_tex['save_path']] = m_tex
                elif same_res and (o_tex.get('width') != m_tex.get('width') or o_tex.get('height') != m_tex.get('height')):
                    continue
            
            # 2. Similarity based candidates
            scores = []
            for m_tex in mod_textures:
                if same_res and (o_tex.get('width') != m_tex.get('width') or o_tex.get('height') != m_tex.get('height')):
                    continue
                sim = hamming_similarity(o_tex['hash'], m_tex['hash'])
                scores.append((sim, m_tex))
            
            scores.sort(key=lambda x: x[0], reverse=True)
            for sim, m_tex in scores[:num_candidates]:
                candidate_pool[m_tex['save_path']] = m_tex
            
            # Refine all candidates in the pool
            all_potentials = []
            for m_tex in candidate_pool.values():
                refined_sim = compare_images(o_tex['save_path'], m_tex['save_path'])
                is_exact = (m_tex['name'].lower() == o_tex['name'].lower())

                is_same_res = (m_tex.get('width') == o_tex.get('width') and m_tex.get('height') == o_tex.get('height'))
                all_potentials.append({
                    'tex': m_tex, 'similarity': refined_sim, 
                    'is_exact': is_exact, 'is_same_res': is_same_res
                })
            
            if not all_potentials:
                results.append({"original": o_tex, "match_file": None, "best_similarity": 0.0, "candidates": [], "replace": False})
                continue

            # 1. Strictly Filter Candidates: Must meet cutoff OR be a Name match
            final_refined = []
            for r in all_potentials:
                passes_cutoff = r['similarity'] >= sim_cutoff
                if not filter_enabled or passes_cutoff or r['is_exact']:
                    final_refined.append(r)

            if not final_refined:
                # ALL candidates below cutoff and NO name match Found
                results.append({"original": o_tex, "match_file": None, "best_similarity": 0.0, "candidates": [], "replace": False})
                continue

            # 2. Identify "ABSOLUTE BEST" Candidate using Name/Res/Sim priority from the FILTERED list 
            def best_sort_key(x):
                # Priority: name match (is_exact) > resolution match (is_same_res) > high similarity (similarity)
                return (1 if x['is_exact'] else 0, 1 if x['is_same_res'] else 0, x['similarity'])
            
            best_candidate = max(final_refined, key=best_sort_key)
            best_match_path = best_candidate['tex']['save_path']
            best_similarity = best_candidate['similarity']

            # 3. Sort sub-items by absolute Similarity (Descending) as previously requested
            final_refined.sort(key=lambda x: x['similarity'], reverse=True)
            
            # Simplified candidates list (up to num_candidates)
            IsAddedBestCandidate = False
            candidates = []
            for r in final_refined[:num_candidates]:
                if not IsAddedBestCandidate and r['tex']['save_path'] == best_match_path:
                    IsAddedBestCandidate = True
                m_t = r['tex']
                candidates.append({
                    "save_path": m_t['save_path'],
                    "name": m_t.get('name', 'Unknown'),
                    "width": m_t.get('width', 0),
                    "height": m_t.get('height', 0),
                    "size": m_t.get('size', 0),
                    "similarity": round(r['similarity'], 4),
                    "is_exact": r['is_exact'],
                    "is_same_res": r['is_same_res']
                })
            if not IsAddedBestCandidate:
                if len(candidates) >= num_candidates:
                    candidates.pop()
                m_t = best_candidate['tex']
                candidates.append({
                    "save_path": m_t['save_path'],
                    "name": m_t.get('name', 'Unknown'),
                    "width": m_t.get('width', 0),
                    "height": m_t.get('height', 0),
                    "size": m_t.get('size', 0),
                    "similarity": round(best_candidate['similarity'], 4),
                    "is_exact": best_candidate['is_exact'],
                    "is_same_res": best_candidate['is_same_res']
                })

            # Unchecked Same/Cut-off logic
            should_replace = True
            if filter_enabled and best_similarity < sim_cutoff:
                # If even the best match is below cutoff, don't auto-check it for replacement
                should_replace = False
            elif best_similarity >= 1.0 and unchecked_same:
                # Already identical
                should_replace = False

            results.append({
                "original": o_tex,
                "match_file": best_match_path,
                "best_similarity": best_similarity,
                "candidates": candidates,
                "replace": should_replace
            })
            
            total = len(orig_textures)
            if i % 10 == 0:
                self.sig_progress.emit(int((i/total)*100), f"Matching {i}/{total}...")
        
        self.sig_finished.emit(results)

class PreviewWorker(QObject):
    sig_preview_ready = pyqtSignal(object) # Data dict with pixmaps and request_id
    
    def __init__(self, parent_window):
        super().__init__()
        self.parent_window = parent_window

    @pyqtSlot(int, str, str, bool)
    def run_preview(self, request_id, path1, path2, show_diff):
        # 1. Check if this request is already outdated
        if request_id != self.parent_window.current_preview_id:
            return

        from similarity import get_difference_mask, load_image
        
        def get_safe_qimage(path):
            """Unifies loading using the standardized analytical pipeline (load_image)
            to ensure that visual previews exactly match difference calculations."""
            if not path or not os.path.exists(path): return None
            
            try:
                from similarity import load_image
                img = load_image(path) # returns uint8 BGRA
                if img is None:
                    # Final fallback to native Qt
                    qimg = QImage(path)
                    return qimg.copy() if not qimg.isNull() else None
                
                # Standardize using a PNG-encoded buffer handoff (Stabilizes cross-thread rendering)
                success, buffer = cv2.imencode('.png', img)
                if success:
                    qimg = QImage()
                    # loadFromData creates a deep-copied, Qt-managed memory buffer
                    if qimg.loadFromData(buffer.tobytes()):
                        return qimg
                
                # Native fallback as last resort
                return QPixmap(path).toImage()
            except Exception as e:
                print(f"Standardized loading failed for {path}: {e}")
                qimg = QImage(path)
                return qimg.copy() if not qimg.isNull() else None

        # Load Original
        img1 = get_safe_qimage(path1)
        if request_id != self.parent_window.current_preview_id: return

        # Load Comparison
        img2 = get_safe_qimage(path2)
        if request_id != self.parent_window.current_preview_id: return

        # Generate Diff Thumbnail (Small but slow)
        thumb_img = None
        if path1 and path2:
            diff_img = get_difference_mask(path1, path2, grayscale_bg=True)
            if diff_img is not None:
                try:
                    # Encode to PNG for a stable memory handoff to the GUI thread
                    success, buffer = cv2.imencode('.png', diff_img)
                    if success:
                        thumb_img = QImage()
                        thumb_img.loadFromData(buffer.tobytes())
                except Exception as e:
                    print(f"Thumb diff rendering failed: {e}")
            
            if request_id != self.parent_window.current_preview_id: return

        # Generate Main Diff if needed
        main_diff_img_obj = None
        if show_diff and path1 and path2:
            main_diff_img = get_difference_mask(path1, path2, grayscale_bg=False)
            if main_diff_img is not None:
                try:
                    success, buffer = cv2.imencode('.png', main_diff_img)
                    if success:
                        main_diff_img_obj = QImage()
                        main_diff_img_obj.loadFromData(buffer.tobytes())
                except Exception as e:
                    print(f"Diff mask rendering failed: {e}")
            
            if request_id != self.parent_window.current_preview_id: return

        # Final check before emitting
        if request_id == self.parent_window.current_preview_id:
            self.sig_preview_ready.emit({
                "request_id": request_id,
                "img1": img1,
                "img2": img2,
                "thumb_img": thumb_img,
                "main_diff_img": main_diff_img_obj,
                "path1": path1,
                "path2": path2
            })

class MainWindow(QMainWindow):
    sig_request_preview = pyqtSignal(int, str, str, bool)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Unity Texture Changer")
        self.resize(1280, 800)
        self.results = []
        self.orig_textures = []
        self.mod_textures = []
        self.worker_thread = None
        self.current_preview_id = 0
        
        # Setup Async Preview Thread
        self.preview_thread = QThread()
        self.preview_worker = PreviewWorker(self)
        self.preview_worker.moveToThread(self.preview_thread)
        self.sig_request_preview.connect(self.preview_worker.run_preview)
        self.preview_worker.sig_preview_ready.connect(self.on_preview_ready)
        self.preview_thread.start()

        # Ensure temp folders are always relative to the executable/script location
        # Asset/Bundled files are in _MEIPASS when frozen
        if getattr(sys, 'frozen', False):
            exe_dir = os.path.dirname(os.path.abspath(sys.executable))
            bundle_dir = getattr(sys, '_MEIPASS', exe_dir)
        else:
            exe_dir = os.path.dirname(os.path.abspath(__file__))
            bundle_dir = exe_dir

        self.temp_dir_orig = os.path.join(exe_dir, "temp_original")
        self.temp_dir_mod = os.path.join(exe_dir, "temp_modified")
        
        # Set Window Icon
        icon_path = os.path.join(bundle_dir, "app_icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
            
        self.start_time = None
        self.current_orig_img = None
        self.current_pix = None
        self.last_viewport = (0, 0, 1, 1)
        self.worker_thread = None
        self._is_deleting = False
        self.init_ui()
        self.load_settings()

    def _normalize_textures(self, textures, temp_path):
        """Ensures all textures have required fields like 'save_path' and 'size'."""
        if not textures: return
        changed = False
        for t in textures:
            # 1. Ensure save_path
            if 'save_path' not in t and 'file' in t:
                t['save_path'] = os.path.join(temp_path, t['file'])
                changed = True
            
            # 2. Ensure size
            if 'size' not in t:
                if t.get('save_path') and os.path.exists(t['save_path']):
                    t['size'] = os.path.getsize(t['save_path'])
                else:
                    t['size'] = 0
                changed = True
                
            # 3. Ensure name
            if 'name' not in t:
                if t.get('save_path'):
                    t['name'] = os.path.splitext(os.path.basename(t['save_path']))[0]
                else:
                    t['name'] = "Unknown"
                changed = True
                
            # 4. Ensure width/height
            if 'width' not in t or 'height' not in t:
                if t.get('save_path') and os.path.exists(t['save_path']):
                    try:
                        # Use the robust load_image function
                        img_data = load_image(t['save_path'])
                        if img_data is not None:
                            t['height'], t['width'] = img_data.shape[:2]
                        else:
                            t['width'] = t.get('width', 0)
                            t['height'] = t.get('height', 0)
                    except Exception as e:
                        self.log(f"Error getting dimensions for {t.get('save_path', 'Unknown')}: {e}")
                        t['width'] = t.get('width', 0)
                        t['height'] = t.get('height', 0)
                else:
                    t['width'] = t.get('width', 0)
                    t['height'] = t.get('height', 0)
                changed = True
        return changed

    def closeEvent(self, event):
        # 1. Save all settings to config.ini
        self.save_settings()
        
        # 2. Sync metadata for matching results
        self._sync_metadata_to_disk()
        
        # 3. Securely stop background threads
        if hasattr(self, 'preview_thread') and self.preview_thread.isRunning():
            self.preview_thread.quit()
            self.preview_thread.wait()
            
        super().closeEvent(event)

    def save_settings(self):
        settings = QSettings("config.ini", QSettings.Format.IniFormat)
        settings.setValue("path/original", self.orig_path.text())
        settings.setValue("path/modified", self.mod_path.text())
        settings.setValue("options/mode", self.mode_combo.currentText())
        settings.setValue("options/same_res", self.chk_same_res.isChecked())
        settings.setValue("options/sim_filter", self.chk_sim_filter.isChecked())
        settings.setValue("options/sim_cutoff", self.txt_sim_cutoff.text())
        settings.setValue("options/num_candidates", self.num_candidates.text())
        settings.setValue("options/highlight_diff", self.diff_check.isChecked())
        settings.setValue("options/high_quality", self.chk_high_quality.isChecked())
        settings.setValue("options/scan_all_orig", self.chk_orig_scan_all.isChecked())
        settings.setValue("options/scan_all_mod", self.chk_mod_scan_all.isChecked())
        settings.sync() # Force write to disk immediately

    def load_settings(self):
        settings = QSettings("config.ini", QSettings.Format.IniFormat)
        self._is_loading = True
        try:
            # Paths
            orig = settings.value("path/original", "")
            mod = settings.value("path/modified", "")
            self.orig_path.setText(orig)
            self.mod_path.setText(mod)
            
            # Options
            mode = settings.value("options/mode", "Unity")
            self.mode_combo.setCurrentText(mode)
            
            # Helper for boolean settings
            def get_bool_val(key, default_val="false"):
                try:
                    val = settings.value(key, default_val)
                    if isinstance(val, bool): return val
                    return str(val).lower() == "true"
                except:
                    return str(default_val).lower() == "true"
    
            self.chk_same_res.setChecked(get_bool_val("options/same_res", "false"))
            
            sim_filter = get_bool_val("options/sim_filter", "false")
            self.chk_sim_filter.setChecked(sim_filter)
            self.txt_sim_cutoff.setText(str(settings.value("options/sim_cutoff", "0.5")))
            self.txt_sim_cutoff.setEnabled(sim_filter)
            
            self.num_candidates.setText(str(settings.value("options/num_candidates", "15")))
            
            highlight_diff = get_bool_val("options/highlight_diff", "true")
            self.diff_check.setChecked(highlight_diff)
            
            self.chk_high_quality.setChecked(get_bool_val("options/high_quality", "false"))
            
            self.chk_orig_scan_all.setChecked(get_bool_val("options/scan_all_orig", "true"))
            self.chk_mod_scan_all.setChecked(get_bool_val("options/scan_all_mod", "true"))
            
            # Trigger the visual style update for diff_check
            self.on_diff_toggle(Qt.CheckState.Checked if highlight_diff else Qt.CheckState.Unchecked)
    
            if mod: self.on_path_changed_direct(mod)
            if orig: self.on_path_changed_direct(orig)
        finally:
            self._is_loading = False

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        
        # Header Controls Wrapper (to measure height for thumbnail scaling)
        self.header_controls_widget = QWidget()
        header_controls_vbox = QVBoxLayout(self.header_controls_widget)
        header_controls_vbox.setContentsMargins(0, 0, 0, 0)
        
        folder_layout = QVBoxLayout()
        placeholder = "Drag & Drop or Double click to select a folder or file"
        
        orig_row = QHBoxLayout()
        orig_row.addWidget(QLabel("Original Folder:"))
        self.orig_path = PathLineEdit(placeholder)
        self.orig_path.textChanged.connect(self.on_path_changed)
        orig_row.addWidget(self.orig_path)
        btn_orig = QPushButton("Browse")
        btn_orig.clicked.connect(lambda: self.browse_folder(self.orig_path))
        orig_row.addWidget(btn_orig)
        self.chk_orig_scan_all = QCheckBox("Scan All Files")
        self.chk_orig_scan_all.setChecked(True)
        orig_row.addWidget(self.chk_orig_scan_all)
        folder_layout.addLayout(orig_row)
        
        mod_row = QHBoxLayout()
        mod_row.addWidget(QLabel("Modified Folder:"))
        self.mod_path = PathLineEdit(placeholder)
        self.mod_path.textChanged.connect(self.on_path_changed)
        mod_row.addWidget(self.mod_path)
        btn_mod = QPushButton("Browse")
        btn_mod.clicked.connect(lambda: self.browse_folder(self.mod_path))
        mod_row.addWidget(btn_mod)
        self.chk_mod_scan_all = QCheckBox("Scan All Files")
        self.chk_mod_scan_all.setChecked(True)
        mod_row.addWidget(self.chk_mod_scan_all)
        folder_layout.addLayout(mod_row)
        
        header_controls_vbox.addLayout(folder_layout)
        
        # Options row 1: Mode and Checks
        opt_row1 = QHBoxLayout()
        opt_row1.addWidget(QLabel("Mode:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Unity", "Unity APK", "Image"])
        self.mode_combo.currentTextChanged.connect(self.on_mode_changed)
        opt_row1.addWidget(self.mode_combo)
        
        opt_row1.addSpacing(10)
        self.chk_same_res = QCheckBox("Same Resolution")
        self.chk_same_res.setToolTip("Only match textures with the same resolution")
        self.chk_same_res.setChecked(False)
        opt_row1.addWidget(self.chk_same_res)

        opt_row1.addSpacing(10)
        self.chk_sim_filter = QCheckBox("Cut-off:")
        self.chk_sim_filter.setChecked(False)
        opt_row1.addWidget(self.chk_sim_filter)
        self.txt_sim_cutoff = QLineEdit("0.5")
        self.txt_sim_cutoff.setFixedWidth(40)
        self.txt_sim_cutoff.setEnabled(False)
        self.chk_sim_filter.toggled.connect(self.txt_sim_cutoff.setEnabled)
        opt_row1.addWidget(self.txt_sim_cutoff)

        opt_row1.addSpacing(10)
        opt_row1.addWidget(QLabel("Max Candidates:"))
        self.num_candidates = QLineEdit("5")
        self.num_candidates.setFixedWidth(40)
        opt_row1.addWidget(self.num_candidates)

        opt_row1.addSpacing(10)
        self.btn_clear = QPushButton("Clear temp Folders")
        self.btn_clear.clicked.connect(self.clear_temp_folders)
        opt_row1.addWidget(self.btn_clear)
        
        opt_row1.addSpacing(5)
        self.chk_show_log = QCheckBox("Show Log")
        self.chk_show_log.setChecked(False)
        self.chk_show_log.stateChanged.connect(self.toggle_log_visibility)
        opt_row1.addWidget(self.chk_show_log)

        opt_row1.addStretch()

        self.diff_check = QCheckBox("Highlight\nDifference")
        self.diff_check.setChecked(True)
        self.diff_check.setStyleSheet("background-color: yellow; color: black;")
        self.diff_check.stateChanged.connect(self.on_diff_toggle)
        opt_row1.addWidget(self.diff_check)
        
        opt_row1.addSpacing(1) # Gap before thumbnail edge
        
        header_controls_vbox.addLayout(opt_row1)

        # Options row 2: Action Buttons (Moved inside header)
        opt_row2 = QHBoxLayout()
        self.btn_toggle_expand = QPushButton("Expand All")
        self.btn_toggle_expand.setEnabled(False)
        self.btn_toggle_expand.clicked.connect(self.toggle_expand_all)
        opt_row2.addWidget(self.btn_toggle_expand)
        
        opt_row2.addSpacing(10)
        self.btn_extract = QPushButton("Extract")
        self.btn_extract.clicked.connect(self.extract)
        opt_row2.addWidget(self.btn_extract)
        
        self.btn_match = QPushButton("Match")
        self.btn_match.clicked.connect(self.match)
        opt_row2.addWidget(self.btn_match)

        self.btn_uncheck_same = QPushButton("Uncheck Same")
        self.btn_uncheck_same.setToolTip("Uncheck items that are already identical (Similarity >= 1.0)")
        self.btn_uncheck_same.clicked.connect(self.uncheck_same_items)
        opt_row2.addWidget(self.btn_uncheck_same)

        opt_row2.addSpacing(10)
        self.btn_apply = QPushButton("Apply Changes")
        self.btn_apply.setEnabled(False)
        self.btn_apply.clicked.connect(self.apply_changes)
        opt_row2.addWidget(self.btn_apply)

        self.chk_high_quality = QCheckBox("High Quality (BC7)")
        self.chk_high_quality.setToolTip("Force high quality BC7 compression to fix banding")
        self.chk_high_quality.setChecked(False)
        opt_row2.addWidget(self.chk_high_quality)

        opt_row2.addSpacing(10)
        self.btn_copy_to = QPushButton("Copy to...")
        self.btn_copy_to.setEnabled(False)
        self.btn_copy_to.clicked.connect(self.copy_to)
        opt_row2.addWidget(self.btn_copy_to)

        self.chk_uabea_format = QCheckBox("Use UABEA format")
        self.chk_uabea_format.setToolTip("Export as Name-Assets-PathID.png to prevent overwriting duplicates")
        self.chk_uabea_format.setChecked(False)
        opt_row2.addWidget(self.chk_uabea_format)
        
        opt_row2.addStretch()
        header_controls_vbox.addLayout(opt_row2)

        main_top_layout = QHBoxLayout()
        main_top_layout.addWidget(self.header_controls_widget, 1)
        
        # Thumbnail anchored top-right
        thumb_container = QVBoxLayout()
        self.thumbnail_preview = ThumbnailLabel()
        self.thumbnail_preview.setStyleSheet("border: 1px solid gray; background-color: #222;")
        self.thumbnail_preview.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        self.thumbnail_preview.setMinimumSize(40, 40)
        self.thumbnail_preview.setScaledContents(True)
        self.thumbnail_preview.clicked.connect(self.on_thumbnail_clicked)
        self.thumbnail_preview.wheeled.connect(self.on_thumbnail_wheeled)
        thumb_container.addWidget(self.thumbnail_preview)
        thumb_container.addStretch()
        main_top_layout.addLayout(thumb_container, 0)
        
        main_layout.addLayout(main_top_layout)
        
        
        # Main Content Panel (wrapped to handle resizing when log shows)
        self.main_content_panel = QWidget()
        self.main_content_layout = QVBoxLayout(self.main_content_panel)
        self.main_content_layout.setContentsMargins(0, 0, 0, 0)
        
        # Content Splitter Layout
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # Left Panel (Tree + Progress)
        self.left_panel = QWidget()
        tree_area = QVBoxLayout(self.left_panel)
        tree_area.setContentsMargins(0, 0, 0, 0)
        
        # Tree for results (Using EnhancedTreeWidget for Space bar support)
        self.tree = EnhancedTreeWidget()
        self.tree.setColumnCount(8)
        self.tree.setHeaderLabels([
            "Replace", "Name", "Res", "Size", "Similarity", "Match File", "M-Res", "M-Size"
        ])
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.tree.header().setSectionsClickable(True)
        #self.tree.header().sectionClicked.connect(self.on_header_clicked)
        self.tree.itemSelectionChanged.connect(self.on_selection_changed)
        self.tree.itemChanged.connect(self.on_item_changed)
        self.tree.sig_delete_items.connect(self.delete_selected_items)
        self.tree.sig_check_all.connect(self.check_all_items)
        self.tree.sig_inverse_all.connect(self.inverse_all_items)
        self.tree.setSortingEnabled(True)
        self.tree.header().setSortIndicator(1, Qt.SortOrder.AscendingOrder)
        tree_area.addWidget(self.tree)
        
        # Progress Bar moved below tree
        self.progress_bar = QProgressBar()
        self.progress_label = QLabel("Ready")
        self.full_status_text = "Ready"
        tree_area.addWidget(self.progress_bar)
        tree_area.addWidget(self.progress_label)
        
        self.main_splitter.addWidget(self.left_panel)
        
        # Right Panel (Comparison Side)
        self.right_panel = QWidget()
        comp_side = QVBoxLayout(self.right_panel)
        comp_side.setContentsMargins(0, 0, 0, 0)
        self.comp_widget = ComparisonWidget()
        self.comp_widget.viewportChanged.connect(self.on_viewport_changed)
        comp_side.addWidget(self.comp_widget)
        
        self.main_splitter.addWidget(self.right_panel)
        
        # Set initial sizes (40% tree, 60% comparison)
        self.main_splitter.setSizes([400, 600])
        
        self.main_content_layout.addWidget(self.main_splitter)
        main_layout.addWidget(self.main_content_panel, 1) # Give it stretch
        # Log Area (Toggable)
        self.log_container = QWidget()
        log_layout = QVBoxLayout(self.log_container)
        log_layout.setContentsMargins(0, 5, 0, 0)
        log_layout.addWidget(QLabel("Logs:"))
        self.log_area = QPlainTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setMinimumHeight(100)
        self.log_area.setMaximumHeight(150)
        log_layout.addWidget(self.log_area)
        self.log_container.setVisible(False) # Hidden by default
        main_layout.addWidget(self.log_container)
        
        self.update_buttons()
        
        # Responsive Sizing: Detect screen and resize if small
        screen = QApplication.primaryScreen()
        if screen:
            sz = screen.size()
            if sz.width() <= self.width() or sz.height() <= self.height():
                self.resize(sz.width() - 40, sz.height() - 80)
                self.move(20, 5)
                # Alternatively, self.showMaximized() if window is meant to be full screen

    def start_worker(self, task_type, **kwargs):
        if self.worker_thread:
            return
            
        self.worker_thread = WorkerThread(task_type, **kwargs)
        self.worker_thread.sig_progress.connect(self.update_progress)
        self.worker_thread.sig_log.connect(self.log)
        self.worker_thread.sig_error.connect(self.on_worker_error)
        self.worker_thread.sig_finished.connect(self.on_finished)
        self.worker_thread.start()
        self.start_time = time.time()
        self.update_buttons()

    def get_asset_manager(self):
        return AssetManager(self.orig_path.text(), self.mod_path.text(), self.temp_dir_orig, self.temp_dir_mod)

    def on_worker_error(self, title, message):
        QMessageBox.warning(self, title, message)

    def on_finished(self, data):
        self.worker_thread = None
        self.update_buttons()
        # Route to specific handlers based on task type
        if isinstance(data, dict) and 'orig' in data:
            self.on_extraction_finished(data)
        elif isinstance(data, list):
            self.on_matching_finished(data)
        elif isinstance(data, dict) and 'success' in data:
            self.on_apply_finished(data)

    def toggle_log_visibility(self, state):
        # Guard: Prevent UI jumps while background task is running
        if self.worker_thread and self.worker_thread.isRunning():
            # Force restore visual state of checkbox if it was toggled? 
            # Or just ignore the action. Disabling the checkbox is better.
            return
            
        is_visible = (state == Qt.CheckState.Checked.value or state == Qt.CheckState.Checked)
        self.log_container.setVisible(is_visible)

    def toggle_expand_all(self):
        if self.btn_toggle_expand.text() == "Expand All":
            self.tree.expandAll()
            self.btn_toggle_expand.setText("Collapse All")
        else:
            self.tree.collapseAll()
            self.btn_toggle_expand.setText("Expand All")

    def log(self, message):
        print(message) # Also print to console so errors during exit/crash are visible
        self.log_area.appendPlainText(message)
        # Scroll to bottom
        self.log_area.verticalScrollBar().setValue(self.log_area.verticalScrollBar().maximum())

    def has_images(self, path):
        if not path or not os.path.isdir(path): return False
        path = os.path.normpath(path.strip())
        try:
            for root, dirs, fnames in os.walk(path):
                if any(f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff', '.tif')) for f in fnames):
                    return True
                # No limit on depth, search everything
        except: pass
        return False

    def get_engine_type(self, path):
        if not path or not os.path.exists(path):
            return "Image"
            
        if self.detect_apk(path):
            return "Unity APK"
            
        if not os.path.isdir(path):
            return "Image"
            
        try:
            files = os.listdir(path)
            files_lower = [f.lower() for f in files]
            
            # To be Unity mode, it needs a combination of factors:
            # 1. Has UnityPlayer.dll (Strong sign)
            # 2. Has GameAssembly.dll (Strong sign for IL2CPP)
            # 3. Has an .exe AND a *_Data folder (Strong sign for Mono/IL2CPP)
            
            has_data_folder = any(f.lower().endswith('_data') and os.path.isdir(os.path.join(path, f)) for f in files)
            has_exe = any(f.endswith('.exe') for f in files_lower)
            has_unity_dll = 'unityplayer.dll' in files_lower or 'gameassembly.dll' in files_lower
            
            if (has_exe and has_data_folder) or has_unity_dll:
                return "Unity"
            
            # Check for standard asset files in root
            if any(f.endswith(('.assets', '.unity3d', '.bundle')) for f in files_lower):
                return "Unity"
            
            # Deep check for signatures if not in root (limit depth to 2)
            for root, dirs, fnames in os.walk(path):
                fn_lower = [fn.lower() for fn in fnames]
                if 'gameassembly.dll' in fn_lower or 'unityplayer.dll' in fn_lower:
                    return "Unity"
                if any(d.lower().endswith('_data') for d in dirs):
                    return "Unity"
                if root.count(os.sep) - path.count(os.sep) >= 2:
                    del dirs[:]
        except: pass

        # 2. RPGM (MV/MZ)
        if os.path.exists(os.path.join(path, "www", "data")) or \
           os.path.exists(os.path.join(path, "data", "Actors.json")):
            return "RPGM"
        # 3. RPGM (VX/VX Ace)
        if any(f.lower().endswith(('.rgss3a', '.rgss2a', '.rgssad')) for f in (os.listdir(path) if os.path.exists(path) else [])):
            return "RPGM"
        # 4. KIRIKIRI
        if any(f.lower().endswith('.xp3') for f in (os.listdir(path) if os.path.exists(path) else [])) or \
           os.path.exists(os.path.join(path, "data.xp3")):
            return "KIRIKIRI"
        # 5. Tyrano
        if os.path.exists(os.path.join(path, "index.html")) and \
           (os.path.exists(os.path.join(path, "tyrano")) or os.path.exists(os.path.join(path, "data", "scenario"))):
            return "Tyrano"
            
        return "Image"

    def on_header_clicked(self, logicalIndex):
        # Disable sorting for Similarity (Column 4)
        # We also want to ensure that if they click it, it doesn't change the sortIndicator
        if logicalIndex == 4:
            # Revert to previous sort if necessary, or just don't sort.
            # Easiest is to block sorting for this column.
            self.tree.setSortingEnabled(False)
            self.tree.setSortingEnabled(True)
            self.log("Sorting by Similarity header is disabled.")

    def on_path_changed(self, _):
        # Auto-detect mode and clear temp if path changed
        sender = self.sender()
        path = sender.text().strip() if sender else ""
        
        # Only check if it's a valid directory to prevent constant clearing while typing
        if path and os.path.exists(path):
            is_loading = getattr(self, '_is_loading', False)
            if sender == self.orig_path:
                self.orig_textures = []
                if not self.is_extracted(path, self.temp_dir_orig):
                    if not is_loading:
                        self.check_and_clear_temp(self.temp_dir_orig)
                        self.results = []
                        self.tree.clear()
                        self.update_status_counts()
            elif sender == self.mod_path:
                self.mod_textures = []
                if not self.is_extracted(path, self.temp_dir_mod):
                    if not is_loading:
                        self.check_and_clear_temp(self.temp_dir_mod)
                        self.mod_textures = []
                        self.results = []
                        self.tree.clear()
                        self.update_status_counts()

        # APK Detection and Mode Selection
        self.on_path_changed_direct(path)
        self.auto_detect_apk_mode()

    def detect_apk(self, path):
        """Checks if a path is an APK or contains an APK."""
        if not path or not os.path.exists(path): return None
        if path.lower().endswith('.apk'): return path
        if os.path.isdir(path):
            try:
                for f in os.listdir(path):
                    if f.lower().endswith('.apk'):
                        return os.path.join(path, f)
            except: pass
        return None

    def auto_detect_apk_mode(self):
        """Automatically switches to Unity APK mode if an APK is found in orig or mod paths."""
        orig_path = self.orig_path.text()
        mod_path = self.mod_path.text()
        
        has_apk = self.detect_apk(orig_path) or self.detect_apk(mod_path)
        
        if has_apk:
            if self.mode_combo.currentText() != "Unity APK" or self.mode_combo.isEnabled():
                self.mode_combo.setCurrentText("Unity APK")
                self.mode_combo.setEnabled(False)
                self.log(f"APK detected. Switching to 'Unity APK' mode (Read-only).")
        else:
            # Re-enable if it was locked
            if not self.mode_combo.isEnabled() and self.mode_combo.currentText() == "Unity APK":
                self.mode_combo.setEnabled(True)
                self.log("No APK detected. Mode selection re-enabled.")
                    
    def check_and_clear_temp(self, temp_path):
        """Clears the temp folder if it contains files from a previous extraction."""
        if os.path.exists(temp_path) and os.listdir(temp_path):
            # Only clear if metadata.json exists inside, meaning it was used for extraction
            metadata_path = os.path.join(temp_path, "metadata.json")
            if os.path.exists(metadata_path):
                try:
                    # Clear contents rather than rmtree itself to keep the handles if any
                    for item in os.listdir(temp_path):
                        item_path = os.path.join(temp_path, item)
                        try:
                            if os.path.isfile(item_path): os.unlink(item_path)
                            elif os.path.isdir(item_path): shutil.rmtree(item_path)
                        except: pass
                    self.log(f"Cleared temporary folder for {os.path.basename(temp_path)} (switched project).")
                except Exception as e:
                    self.log(f"Failed to clear {temp_path}: {e}")

    def on_path_changed_direct(self, path):
        if not path: return
        engine = self.get_engine_type(path)
        if engine != "Image":
            self.mode_combo.setCurrentText(engine)
        else:
            self.mode_combo.setCurrentText("Image")
        
        # Try to auto-restore results if both paths are set
        if self.orig_path.text() and self.mod_path.text():
            self.restore_matching_results()
            
        self.update_buttons()

    def on_apply_finished(self, summary):
        self.update_tree(show_progress=False)
        self._sync_metadata_to_disk()
        self.update_buttons(is_finishing=True)
        self.log(f"Apply finished: {summary}")
        QMessageBox.information(self, "Finished", f"Successfully applied {summary['success']} textures.")

    def on_mode_changed(self, mode):
        # Enable Packer only for Unity mode
        self.orig_textures = []
        self.mod_textures = []
        self.results = []
        self.tree.clear()
        self.update_buttons()
        self.log(f"Switched to {mode} mode.")

    def get_asset_manager(self):
        orig_path = self.orig_path.text()
        mod_path = self.mod_path.text()
        return AssetManager(orig_path, mod_path, self.temp_dir_orig, self.temp_dir_mod)

    def browse_folder(self, line_edit):
        path = QFileDialog.getExistingDirectory(self, "Select Folder")
        if path:
            line_edit.setText(path)
            self.on_path_changed_direct(path)

    def extract(self):
        orig = self.orig_path.text()
        mod = self.mod_path.text()
        
        if not os.path.exists(orig) or not os.path.exists(mod):
            QMessageBox.warning(self, "Error", "Selected paths do not exist.")
            return

        self.progress_bar.setValue(0)
        self.progress_label.setText("Starting extraction...")
        
        self.start_worker("extract", orig_dir=orig, mod_dir=mod, 
                          temp_orig=self.temp_dir_orig, temp_mod=self.temp_dir_mod,
                          mode=self.mode_combo.currentText(),
                          orig_scan_all=self.chk_orig_scan_all.isChecked(),
                          mod_scan_all=self.chk_mod_scan_all.isChecked())

    def on_extraction_finished(self, data):
        self.orig_textures = data['orig']
        self.mod_textures = data['mod']
        self.apk_info = data.get('apk_info', {})
        self.results = [] # Clear any previous match results before starting new ones
        self.update_buttons(is_finishing=True)
        self.log(f"Extraction complete. {len(self.orig_textures)} Original, {len(self.mod_textures)} Modified.")
        if not self.orig_textures or not self.mod_textures:
            self.log("Warning: One or both texture lists are empty. 'Match' will be disabled.")
        elif self.btn_match.isEnabled():
            self.log("Auto-starting Match...")
            self.match()

    def restore_matching_results(self):
        """Tries to load existing matching results from metadata.json."""
        if not self.temp_dir_orig or not os.path.exists(self.temp_dir_orig):
            return
            
        metadata_path = os.path.join(self.temp_dir_orig, "metadata.json")
        if not os.path.exists(metadata_path):
            return

        am = self.get_asset_manager()
        loaded_metadata = am.load_metadata(self.temp_dir_orig)
        if loaded_metadata and isinstance(loaded_metadata, list) and len(loaded_metadata) > 0:
            first = loaded_metadata[0]
            if isinstance(first, dict) and 'original' in first and 'candidates' in first:
                self.results = loaded_metadata
                self.update_tree()
                self.tree.sortByColumn(1, Qt.SortOrder.AscendingOrder)
                self.log("Restored previous matching results and checkbox states from metadata.json.")
                # Reset progress bar once restoration and tree building are complete
                self.progress_bar.setValue(0)
                self.update_status_counts(is_initial=True) # Ensure label says "Matching complete"
                self.update_uabea_auto_check() # Check for duplicates in restored checked items
                return True
            else:
                # It's just extraction metadata
                self.orig_textures = loaded_metadata
                self.log("Auto-loaded extraction metadata from metadata.json.")
        return False

    def match(self):
        # Fallback: If in Image mode and textures are missing, scan folders directly
        is_image_mode = self.mode_combo.currentText() == "Image"
        orig_dir = self.orig_path.text().strip()
        mod_dir = self.mod_path.text().strip()
        
        if is_image_mode:
            # Always fresh scan for Image mode to reflect filesystem changes
            self.orig_textures = self.scan_image_folder(orig_dir)
            self.mod_textures = self.scan_image_folder(mod_dir)
        else:
            # For Unity/APK, prioritize memory then disk metadata
            if not self.orig_textures:
                self.orig_textures = self.get_asset_manager().load_metadata(self.temp_dir_orig) or []
            if not self.mod_textures:
                self.mod_textures = self.get_asset_manager().load_metadata(self.temp_dir_mod) or []
            
        if not self.orig_textures or not self.mod_textures:
            msg = "Extraction metadata missing. Please Extract first."
            if is_image_mode:
                msg = "No images found in the selected folders. Please check your paths."
            QMessageBox.warning(self, "Error", msg)
            return

        self._normalize_textures(self.orig_textures, self.temp_dir_orig)
        self._normalize_textures(self.mod_textures, self.temp_dir_mod)

        self.progress_bar.setValue(0)
        self.progress_label.setText("Matching...")
        
        self.start_worker("match", orig_textures=self.orig_textures, mod_textures=self.mod_textures,
                          num_candidates=int(self.num_candidates.text()),
                          unchecked_same=False,
                          same_res=self.chk_same_res.isChecked(),
                          filter_enabled=self.chk_sim_filter.isChecked(),
                          sim_cutoff=float(self.txt_sim_cutoff.text()))

    def scan_image_folder(self, folder):
        textures = []
        if not folder or not os.path.exists(folder.strip()): return textures
        folder = os.path.normpath(folder.strip())
        self.log(f"Scanning images in {folder} (including all subfolders)...")
        for root, dirs, fnames in os.walk(folder):
            for f in fnames:
                if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff', '.tif')):
                    path = os.path.join(root, f)
                    try:
                        img = Image.open(path)
                        name = os.path.splitext(f)[0]
                        textures.append({
                            "name": name,
                            "save_path": path,
                            "width": img.width,
                            "height": img.height,
                            "size": os.path.getsize(path)
                        })
                    except Exception as e: 
                        self.log(f"Skipping {f}: Could not open image ({e})")
                        continue
        self.log(f"Total images found in {folder}: {len(textures)}")
        return textures

    def update_progress(self, val, text):
        self.progress_bar.setValue(val)
        import time
        if self.start_time:
            elapsed = time.time() - self.start_time
            if val > 0:
                total_est = elapsed / (val / 100.0)
                remaining = max(0, total_est - elapsed)
                min_e, sec_e = divmod(int(elapsed), 60)
                min_r, sec_r = divmod(int(remaining), 60)
                time_str = f"Elapsed: {min_e}:{sec_e:02d} | ETA: {min_r}:{sec_r:02d}"
                self.set_status_text(f"{text} ({time_str})")
            else:
                self.set_status_text(text)
        else:
            self.set_status_text(text)

    def set_status_text(self, text):
        """Sets status text with elision based on current label width."""
        self.full_status_text = text
        metrics = QFontMetrics(self.progress_label.font())
        # Use ElideMiddle to ensure ETA/Time info at the end is visible
        elided = metrics.elidedText(text, Qt.TextElideMode.ElideMiddle, self.progress_label.width())
        self.progress_label.setText(elided)
        self.progress_label.setToolTip(text if elided != text else "")

    def on_matching_finished(self, results):
        self.results = results
        self.update_tree()
        self.update_buttons(is_finishing=True)
        self.btn_match.setEnabled(False) # Disable match after it finishes
        self.log(f"Matching finished. Found matches for {len(results)} textures.")
        am = self.get_asset_manager()
        # Strip hashes before saving to metadata.json
        clean_results = self._strip_metadata(self.results)
        am.save_metadata(clean_results, self.temp_dir_orig)
        self.update_buttons()
        
        # User: "Match가 끝난 뒤에 'Name' 오른차순으로 되게해줘."
        # Column 1 is Name
        self.tree.sortByColumn(1, Qt.SortOrder.AscendingOrder)
        
        self.update_status_counts()

        self.progress_bar.setValue(100)
        self.update_status_counts(is_initial=True)
        self.update_copy_button_state()
        self.update_uabea_auto_check() # Check for duplicates after matching
        self.btn_toggle_expand.setText("Expand All")
        self.log("Matching process finished.")

    def update_uabea_auto_check(self):
        """Automatically checks Use UABEA format if duplicate names exist among checked items."""
        if not hasattr(self, 'results') or not self.results:
            return
            
        checked_names = {} # name -> count
        has_duplicates = False
        
        for res in self.results:
            if res.get('replace') and res.get('match_file'):
                name = res['original'].get('name', 'Unknown')
                checked_names[name] = checked_names.get(name, 0) + 1
                if checked_names[name] > 1:
                    has_duplicates = True
                    break
        
        # Update checkbox state
        if hasattr(self, 'chk_uabea_format'):
            self.chk_uabea_format.setChecked(has_duplicates)

    def update_status_counts(self, is_initial=False):
        checked_count = sum(1 for r in self.results if r.get('replace', False))
        total_count = len(self.results)
        selected_count = len(self.tree.selectedItems())
        
        prefix = "Matching complete." if is_initial else "Status:"
        status_text = f"{prefix} ({checked_count} / {total_count} items to replace)"
        if selected_count > 0:
            status_text += f" | {selected_count} selected rows"
            
        self.set_status_text(status_text)
        QCoreApplication.processEvents()

    def update_tree(self, show_progress=True):
        # Save current sort state
        header = self.tree.header()
        sort_col = header.sortIndicatorSection()
        sort_order = header.sortIndicatorOrder()
        
        self.tree.setUpdatesEnabled(False)
        self.tree.setSortingEnabled(False)
        self.tree.blockSignals(True)
        self.tree.clear()
        
        import time
        start_update = time.time()
        total = len(self.results)
        
        all_top_items = []
        
        for i, res in enumerate(self.results):
            if show_progress and i % 50 == 0:
                elapsed = time.time() - start_update
                prog = int((i / total) * 100) if total > 0 else 0
                self.progress_bar.setValue(prog)
                
                eta_str = ""
                if i > 0:
                    eta = (elapsed / i) * (total - i)
                    min_e, sec_e = divmod(int(elapsed), 60)
                    min_r, sec_r = divmod(int(eta), 60)
                    eta_str = f" (Elapsed: {min_e}:{sec_e:02d} | ETA: {min_r}:{sec_r:02d})"
                
                if show_progress:
                    self.progress_label.setText(f"Building Tree... {i}/{total}{eta_str}")
                QCoreApplication.processEvents()

            orig = res['original']
            # Parent item
            item = SortableTreeWidgetItem()
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            
            # Original Replace Checkbox state
            is_replace = res.get('replace', True)
            item.setCheckState(0, Qt.CheckState.Checked if is_replace else Qt.CheckState.Unchecked)
            item.setData(0, Qt.ItemDataRole.UserRole, is_replace)
            
            item.setText(1, orig.get('name', 'Unknown'))
            item.setText(2, f"{orig.get('width', 0)}x{orig.get('height', 0)}")
            item.setText(3, f"{orig.get('size', 0)/1024:.1f} KB")
            # Store the result object and its original index
            item.setData(1, Qt.ItemDataRole.UserRole, res)
            item.setData(2, Qt.ItemDataRole.UserRole, i)
            
            # If we have a match file, update the columns
            if res.get('match_file'):
                # Find the candidate info for display
                match_info = None
                for cand in res.get('candidates', []):
                    if cand['save_path'] == res['match_file']:
                        match_info = cand
                        break
                
                if match_info:
                    # Construct a dummy m_tex for the update helper
                    m_tex = {
                        "save_path": match_info['save_path'],
                        "name": match_info['name'],
                        "width": match_info['width'],
                        "height": match_info['height'],
                        "size": match_info['size']
                    }
                    self._update_item_match_cols(item, m_tex, match_info['similarity'])
            
            # Child items for candidates
            if res['candidates']:
                sorted_candidates = sorted(res['candidates'], key=lambda x: x['similarity'], reverse=True)
                
                for cand in sorted_candidates:
                    c_sim = cand['similarity']
                    c_item = SortableTreeWidgetItem(item)
                    c_item.setFlags(c_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    
                    # Exclusive Checkbox for candidate
                    is_this_match = (res.get('match_file') == cand['save_path'])
                    c_item.setCheckState(0, Qt.CheckState.Checked if is_this_match else Qt.CheckState.Unchecked)
                    c_item.setData(0, Qt.ItemDataRole.UserRole, is_this_match)
                    
                    c_item.setText(1, cand.get('name', 'Unknown'))
                    c_item.setText(2, f"{cand.get('width', 0)}x{cand.get('height', 0)}")
                    c_item.setText(3, f"{cand.get('size', 0)/1024:.1f} KB")
                    c_item.setText(4, f"{c_sim:.4f}")
                    # Change: cand is already in a simplified format
                    c_item.setData(1, Qt.ItemDataRole.UserRole, cand)

                    # Green highlighting for matching Name, Res, Size (columns 1, 2, 3)
                    green_brush = Qt.GlobalColor.green
                    if cand.get('name') == orig.get('name'):
                        c_item.setBackground(1, green_brush)
                    if cand.get('width') == orig.get('width') and cand.get('height') == orig.get('height'):
                        c_item.setBackground(2, green_brush)
                    if cand.get('size') == orig.get('size'):
                        c_item.setBackground(3, green_brush)
            
            all_top_items.append(item)

        self.tree.addTopLevelItems(all_top_items)
        self.tree.blockSignals(False)
        
        # Restore sort state
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(sort_col, sort_order)
        
        self.tree.setUpdatesEnabled(True)
        self.update_status_counts()
        self.update_buttons()
        # Reset progress bar after tree construction is fully finished
        if show_progress:
            self.progress_bar.setValue(0)

    def _update_item_match_cols(self, item, match_tex, similarity):
        res_data = item.data(1, Qt.ItemDataRole.UserRole)
        orig = res_data['original']
        sim = similarity
        
        item.setText(4, f"{sim:.4f}")
        item.setText(5, os.path.basename(match_tex.get('save_path', 'Unknown')))
        item.setText(6, f"{match_tex.get('width', 0)}x{match_tex.get('height', 0)}")
        item.setText(7, f"{match_tex.get('size', 0)/1024:.1f} KB")

        # Green highlighting for parent MATCH columns if matching ORIGINAL
        green_brush = Qt.GlobalColor.green
        # Column 5 (Match File) vs Column 1 (Original Name)
        m_name = match_tex.get('name', '')
        o_name = orig.get('name', '')
        if m_name and o_name and m_name == o_name:
            item.setBackground(5, green_brush)
        else:
            item.setData(5, Qt.ItemDataRole.BackgroundRole, None)

        # Column 6 (M-Res) vs Column 2 (Original Res)
        m_w, m_h = match_tex.get('width', 0), match_tex.get('height', 0)
        o_w, o_h = orig.get('width', 0), orig.get('height', 0)
        if m_w == o_w and m_h == o_h and m_w > 0:
            item.setBackground(6, green_brush)
        else:
            item.setData(6, Qt.ItemDataRole.BackgroundRole, None)

        # Column 7 (M-Size) vs Column 3 (Original Size)
        m_size = match_tex.get('size', 0)
        o_size = orig.get('size', 0)
        if m_size == o_size and m_size > 0:
            item.setBackground(7, green_brush)
        else:
            item.setData(7, Qt.ItemDataRole.BackgroundRole, None)

    def update_copy_button_state(self):
        any_checked = any(r.get('replace', False) for r in self.results)
        self.btn_copy_to.setEnabled(any_checked)

    def on_item_changed(self, item, column):
        if column != 0: return
        is_checked = (item.checkState(0) == Qt.CheckState.Checked)
        
        parent = item.parent()
        if parent:
            # Child Item (Candidate)
            if is_checked:
                self.tree.blockSignals(True)
                # Uncheck siblings
                for idx in range(parent.childCount()):
                    child = parent.child(idx)
                    if child != item:
                        child.setCheckState(0, Qt.CheckState.Unchecked)
                        child.setData(0, Qt.ItemDataRole.UserRole, False)
                
                # Update data
                cand = item.data(1, Qt.ItemDataRole.UserRole)
                idx = parent.data(2, Qt.ItemDataRole.UserRole) # Index in self.results
                
                # Update self.results directly to ensure persistence
                self.results[idx]['match_file'] = cand['save_path']
                self.results[idx]['best_similarity'] = cand['similarity']
                self.results[idx]['replace'] = True
                
                # Ensure parent is checked
                parent.setCheckState(0, Qt.CheckState.Checked)
                parent.setData(0, Qt.ItemDataRole.UserRole, True)
                item.setData(0, Qt.ItemDataRole.UserRole, True)
                
                # Update parent columns
                # Construct a dummy m_tex for the update helper
                m_tex = {
                    "save_path": cand['save_path'],
                    "name": cand['name'],
                    "width": cand['width'],
                    "height": cand['height'],
                    "size": cand['size']
                }
                self._update_item_match_cols(parent, m_tex, cand['similarity'])
                
                self.tree.blockSignals(False)
                self.update_buttons()
                self.update_status_counts()
                self.update_copy_button_state()
                self.log(f"Selected {os.path.basename(cand['save_path'])} for {self.results[idx]['original']['name']}")
            else:
                # Disable toggle-off for candidates (Sticky selection)
                idx = parent.data(2, Qt.ItemDataRole.UserRole)
                if self.results[idx].get('match_file') == item.data(1, Qt.ItemDataRole.UserRole)['save_path']:
                    self.tree.blockSignals(True)
                    item.setCheckState(0, Qt.CheckState.Checked)
                    self.tree.blockSignals(False)
                else:
                    item.setData(0, Qt.ItemDataRole.UserRole, False)
                self.update_buttons()
        else:
            # Top-level Item (Original)
            idx = item.data(2, Qt.ItemDataRole.UserRole)
            if idx is not None:
                self.results[idx]['replace'] = is_checked
                item.setData(0, Qt.ItemDataRole.UserRole, is_checked)
                self.update_buttons()
                self.update_status_counts()
                self.update_copy_button_state()
                self.update_uabea_auto_check() # Auto-toggle based on duplicates
        
        # Trigger immediate re-sort if necessary
        if self.tree.sortColumn() == 0:
            self.tree.sortItems(0, self.tree.header().sortIndicatorOrder())
        
    def on_tree_double_clicked(self, item, column):
        data = item.data(1, Qt.ItemDataRole.UserRole)
        if not data: return
        
        # Toggle checkbox on double click
        current = item.checkState(0)
        new_state = Qt.CheckState.Checked if current == Qt.CheckState.Unchecked else Qt.CheckState.Unchecked
        item.setCheckState(0, new_state)
        # on_item_changed will handle the rest

    def uncheck_same_items(self):
        changed = False
        self.tree.blockSignals(True)
        items_to_uncheck = []
        for row in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(row)
            res = item.data(1, Qt.ItemDataRole.UserRole)
            if res and res.get('best_similarity', 0) >= 1.0:
                items_to_uncheck.append(item)
        
        for item in items_to_uncheck:
            res = item.data(1, Qt.ItemDataRole.UserRole)
            idx = item.data(2, Qt.ItemDataRole.UserRole)
            
            if res.get('replace', False):
                if idx is not None and idx < len(self.results):
                    self.results[idx]['replace'] = False
                
                item.setCheckState(0, Qt.CheckState.Unchecked)
                item.setData(0, Qt.ItemDataRole.UserRole, False)
                changed = True
        
        self.tree.blockSignals(False)
        if changed:
            self.update_buttons()
            self.update_status_counts()
            self.update_copy_button_state()
            if self.tree.sortColumn() == 0:
                self.tree.sortItems(0, self.tree.header().sortIndicatorOrder())
            self.log("Unchecked items that were already identical (Similarity >= 1.0) and saved metadata.")
        else:
            self.log("No identical items found to uncheck.")

    def delete_selected_items(self):
        if self._is_deleting: return
        self._is_deleting = True
        try:
            selected = self.tree.selectedItems()
            if not selected: return
            
            # Capture the context for selection restoration:
            # We want to select the item that was either at the same visual index or after it.
            first_selected_item = selected[0]
            # If it's a child, we want the parent's index if the parent is also being deleted?
            # Let's just track the visual position.
            first_idx = self.tree.indexOfTopLevelItem(first_selected_item)
            if first_idx == -1 and first_selected_item.parent():
                 first_idx = self.tree.indexOfTopLevelItem(first_selected_item.parent())
            
            changed = False
            indices_to_remove = set()
            candidates_to_remove = [] # (parent_res, cand_obj)
            
            for item in selected:
                if item.parent() is None:
                    # Parent item (Original)
                    idx = item.data(2, Qt.ItemDataRole.UserRole)
                    if idx is not None:
                        indices_to_remove.add(idx)
                else:
                    # Child item (Candidate)
                    p = item.parent()
                    # Use the parent's index to get the LIVE result object from self.results
                    parent_idx = p.data(2, Qt.ItemDataRole.UserRole)
                    cand_data = item.data(1, Qt.ItemDataRole.UserRole)
                    if parent_idx is not None and 0 <= parent_idx < len(self.results) and cand_data:
                        res_obj = self.results[parent_idx]
                        candidates_to_remove.append((res_obj, cand_data))
            
            # 1. Process Child deletions (Sub-items)
            for res, cand in candidates_to_remove:
                target_path = cand.get('save_path')
                if not target_path: continue
                
                cands = res.get('candidates', [])
                # Find and remove by path to be safer
                removed_this = False
                for i in range(len(cands) - 1, -1, -1):
                    if cands[i].get('save_path') == target_path:
                        cands.pop(i)
                        removed_this = True
                
                if removed_this:
                    self.log(f"Removed candidate {os.path.basename(target_path)} from data.")
                    changed = True
                    # If this was the currently active match, pick next best or clear
                    if res.get('match_file') == target_path:
                        if cands:
                            # Re-sort by similarity for best pick
                            sorted_cands = sorted(cands, key=lambda x: x['similarity'], reverse=True)
                            new_best = sorted_cands[0]
                            res['match_file'] = new_best['save_path']
                            res['best_similarity'] = new_best['similarity']
                        else:
                            res['match_file'] = None
                            res['best_similarity'] = 0.0
                
            # 2. Process Top-level deletions
            if indices_to_remove:
                for idx in sorted(list(indices_to_remove), reverse=True):
                    if 0 <= idx < len(self.results):
                        self.results.pop(idx)
                changed = True

            if changed:
                num_deleted = len(indices_to_remove) + len(candidates_to_remove)
                # update_tree already handles capturing and restoring the sort column/order.
                self.update_tree(show_progress=False)
                
                # Restore selection to nearby item
                new_count = self.tree.topLevelItemCount()
                if new_count > 0 and first_idx != -1:
                    next_idx = min(first_idx, new_count - 1)
                    item_to_sel = self.tree.topLevelItem(next_idx)
                    if item_to_sel:
                        try:
                            self.tree.setCurrentItem(item_to_sel)
                            item_to_sel.setSelected(True)
                        except (RuntimeError, AttributeError):
                            pass
                
                self.update_status_counts()
                self.update_copy_button_state()
                self.log(f"Deleted {num_deleted} items and updated metadata.json.")
        finally:
            self._is_deleting = False

    def _sync_metadata_to_disk(self):
        """Synchronizes current results back to metadata.json on disk."""
        if not hasattr(self, 'temp_dir_orig') or not self.temp_dir_orig or not self.results:
            return
            
        # Strip hashes before saving
        clean_results = self._strip_metadata(self.results)
        
        # Atomic write to prevent 0-byte file on crash/error
        metadata_path = os.path.join(self.temp_dir_orig, "metadata.json")
        temp_path = metadata_path + ".tmp"
        
        # Ensure the directory exists (especially important for Image mode where Extract might be skipped)
        os.makedirs(self.temp_dir_orig, exist_ok=True)

        try:
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(clean_results, f, ensure_ascii=False, indent=2)
            
            # Use os.replace for atomic replacement of metadata.json
            if os.path.exists(temp_path):
                os.replace(temp_path, metadata_path)
        except Exception as e:
            self.log(f"Failed to save persistent metadata: {e}")
            if os.path.exists(temp_path):
                try: os.remove(temp_path)
                except: pass

    def _strip_metadata(self, data):
        """Recursively removes 'hash' keys from metadata for storage."""
        if isinstance(data, list):
            return [self._strip_metadata(item) for item in data]
        if isinstance(data, dict):
            return {k: self._strip_metadata(v) for k, v in data.items() if k != 'hash'}
        return data

    def check_all_items(self, checked):
        changed = False
        self.tree.blockSignals(True)
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            if item.checkState(0) != state:
                item.setCheckState(0, state)
                idx = item.data(2, Qt.ItemDataRole.UserRole)
                if idx is not None and 0 <= idx < len(self.results):
                    self.results[idx]['replace'] = checked
                changed = True
        self.tree.blockSignals(False)
        
        if changed:
            self.update_buttons()
            self.update_status_counts()
            self.update_copy_button_state()
            self.log(f"{'Checked' if checked else 'Unchecked'} all items.")

    def inverse_all_items(self):
        self.tree.blockSignals(True)
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            is_checked = (item.checkState(0) == Qt.CheckState.Checked)
            new_state = Qt.CheckState.Unchecked if is_checked else Qt.CheckState.Checked
            item.setCheckState(0, new_state)
            
            idx = item.data(2, Qt.ItemDataRole.UserRole)
            if idx is not None and 0 <= idx < len(self.results):
                self.results[idx]['replace'] = not is_checked
        self.tree.blockSignals(False)
        self.update_buttons()
        self.update_status_counts()
        self.update_copy_button_state()
        self.log("Inversed all selections.")

    def on_selection_changed(self):
        self.update_status_counts()
        if self.worker_thread and self.worker_thread.isRunning():
            return

        selected_items = self.tree.selectedItems()
        if not selected_items: return
        item = selected_items[0]
        
        parent = item.parent()
        if parent:
            # 하위 항목(후보) 선택 시: 부모 행의 정보를 이 후보의 정보로 업데이트하고 비교 화면 표시
            cand = item.data(1, Qt.ItemDataRole.UserRole)
            parent_idx = parent.data(2, Qt.ItemDataRole.UserRole)
            res = self.results[parent_idx]
            
            self._update_item_match_cols(parent, cand, float(cand['similarity']))
            self.update_comparison_view(res['original'], cand)
        else:
            # 상위 항목(원본) 선택 시: 현재 체크된 후보 항목을 찾아 부모 행 정보를 유지하고 비교 화면 표시
            idx = item.data(2, Qt.ItemDataRole.UserRole)
            res = self.results[idx]
            
            match_info = None
            # 1. 체크박스가 선택된 자식 항목 찾기
            for i in range(item.childCount()):
                child = item.child(i)
                if child.checkState(0) == Qt.CheckState.Checked:
                    match_info = child.data(1, Qt.ItemDataRole.UserRole)
                    break
            
            # 2. 체크된 것이 없다면 데이터 구조의 match_file 기준 검색
            if not match_info and res.get('match_file'):
                for c in res.get('candidates', []):
                    if c['save_path'] == res['match_file']:
                        match_info = c
                        break
            
            if match_info:
                self._update_item_match_cols(item, match_info, float(match_info['similarity']))
            
            self.update_comparison_view(res['original'], match_info)


    def on_preview_ready(self, data):
        # Final gatekeeper: Only update UI if this is REALLY the latest request
        if data['request_id'] != self.current_preview_id:
            return
            
        path1 = data['path1']
        path2 = data['path2']
        img1 = data['img1']
        img2 = data['img2']
        thumb_img = data['thumb_img']
        main_diff_img = data['main_diff_img']
        
        # Convert QImages to QPixmaps on GUI thread
        pix1 = QPixmap.fromImage(img1) if img1 else QPixmap()
        pix2 = QPixmap.fromImage(img2) if img2 else QPixmap()
        thumb_pix = QPixmap.fromImage(thumb_img) if thumb_img else None
        main_diff_pix = QPixmap.fromImage(main_diff_img) if main_diff_img else None

        # 1. Update Thumbnail
        self.current_orig_img = path1
        if thumb_pix:
            self.current_pix = thumb_pix
        else:
            # Safe Fallback: Don't use toImage() on pixmaps if we suspect shifting issues
            self.current_pix = pix1 if pix1 else QPixmap()
        
        if self.current_pix and not self.current_pix.isNull():
            self._update_thumbnail_size()
        
        self.draw_thumbnail_viewport()
        
        # 2. Update Comparison Labels
        
        # 2. Update Comparison Labels
        if main_diff_pix:
            self.comp_widget.original_label.set_image(main_diff_pix)
        else:
            self.comp_widget.original_label.set_image(pix1)
        
        self.comp_widget.matched_label.set_image(pix2)
        
        # Update IDs
        self.comp_widget.current_img1_path = path1
        self.comp_widget.current_img2_path = path2

    def update_comparison_view(self, orig_tex, match_tex):
        if not orig_tex: return
        path1 = orig_tex['save_path']
        path2 = match_tex['save_path'] if match_tex else None
        
        # Trigger background preview
        self.current_preview_id += 1
        self.sig_request_preview.emit(self.current_preview_id, path1, path2 if path2 else "", self.diff_check.isChecked())

    def on_viewport_changed(self, x, y, w, h):
        self.last_viewport = (x, y, w, h)
        if self.current_orig_img:
            self.draw_thumbnail_viewport()

    def _update_thumbnail_size(self):
        if not hasattr(self, 'current_pix') or self.current_pix is None or self.current_pix.isNull():
            return

        # Height target: Header height
        S = self.header_controls_widget.height()
        if S < 50: S = 160 # Default

        w, h = self.current_pix.width(), self.current_pix.height()
        if h > 0 and w > 0:
            new_h = S
            new_w = int(w * (S / h))
            width_limit = self.width() // 3

            if new_w > width_limit:
                new_w = width_limit
                new_h = int(h * (width_limit / w))
            
            self.thumbnail_preview.setFixedSize(new_w, new_h)

    def draw_thumbnail_viewport(self):
        if not self.current_orig_img or not self.current_pix: return
        
        # Scale to match the thumbnail preview widget exactly (smoothly)
        target_size = self.thumbnail_preview.size()
        if target_size.width() <= 0 or target_size.height() <= 0: return

        # We draw on a copy of the CURRENT pixmap (which might have diff highlights)
        pix = self.current_pix.scaled(target_size, 
                                      Qt.AspectRatioMode.IgnoreAspectRatio, 
                                      Qt.TransformationMode.SmoothTransformation)
        painter = QPainter(pix)
        
        # 2px XOR border for higher contrast
        pen = QPen(Qt.GlobalColor.white)
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Xor)
        
        x, y, w, h = self.last_viewport
        # Check if viewport is actually a sub-area
        if w < 0.99 or h < 0.99:
            rect = QRect(int(x * pix.width()), int(y * pix.height()), 
                         int(w * pix.width()), int(h * pix.height()))
            painter.drawRect(rect)
        
        painter.end()
        self.thumbnail_preview.setPixmap(pix)

    def on_thumbnail_clicked(self, x, y):
        self.comp_widget.scroll_to_normalized(x, y)

    def on_thumbnail_wheeled(self, delta):
        # Construct a fake wheel event to pass to comp_widget
        # but simpler to just call zoom logic if it was public.
        # Let's just create a QWheelEvent and send it.
        from PyQt6.QtGui import QWheelEvent
        from PyQt6.QtCore import QPointF
        
        # We'll zoom based on the current center of the comparison widget
        center = self.comp_widget.rect().center()
        event = QWheelEvent(
            QPointF(center), QPointF(center),
            QPoint(0, 0), QPoint(0, delta),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False
        )
        self.comp_widget.wheelEvent(event)

    def on_unchecked_same_toggled(self, checked):
        if not checked or not self.results: return
        
        # Uncheck Perfect matches if they exist among ANY candidates
        changed = False
        for res in self.results:
            is_perfect_exists = any(c['similarity'] >= 1.0 for c in res.get('candidates', []))
            if is_perfect_exists:
                if res.get('replace', True):
                    res['replace'] = False
                    changed = True
        
        if changed:
            # Sync tree checkboxes
            for i in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(i)
                res = item.data(1, Qt.ItemDataRole.UserRole)
                is_perfect_exists = any(c['similarity'] >= 1.0 for c in res.get('candidates', []))
                if is_perfect_exists:
                    chk = self.tree.itemWidget(item, 0)
                    if isinstance(chk, QCheckBox):
                        chk.blockSignals(True)
                        chk.setChecked(False)
                        chk.blockSignals(False)
            
            self.update_status_counts()
            self.update_copy_button_state()

    def is_extracted(self, folder_path, temp_path):
        if not folder_path or not os.path.exists(folder_path):
            return False
        
        settings = QSettings("config.ini", QSettings.Format.IniFormat)
        last_path = settings.value(f"last_extract/{'original' if temp_path == self.temp_dir_orig else 'modified'}", "")
        
        # In APK mode, we might be comparing the folder to the stored APK path or vice versa.
        # Normalize comparison:
        compare_path = self.detect_apk(folder_path) or folder_path
        
        # Normcase for Windows path comparison
        if os.path.normcase(os.path.normpath(compare_path)) != os.path.normcase(os.path.normpath(last_path)):
            return False
            
        metadata_path = os.path.join(temp_path, "metadata.json")
        if not os.path.exists(metadata_path):
            return False
            
        # Also check if directory is not empty
        if not os.listdir(temp_path):
            return False
            
        return True

    def update_buttons(self, is_finishing=False):
        # Determine if any background task is running
        is_running = not is_finishing and self.worker_thread is not None and self.worker_thread.isRunning()
        
        # 1. Clear button
        has_temp_files = False
        for folder in [self.temp_dir_orig, self.temp_dir_mod]:
            if os.path.exists(folder) and os.listdir(folder):
                has_temp_files = True
                break
        self.btn_clear.setEnabled(has_temp_files and not is_running)

        # 2. Extract button
        orig = self.orig_path.text()
        mod = self.mod_path.text()
        
        orig_engine = self.get_engine_type(orig)
        mod_engine = self.get_engine_type(mod)
        
        is_orig_game = (orig_engine != "Image")
        is_mod_game = (mod_engine != "Image")
        
        orig_extracted = self.is_extracted(orig, self.temp_dir_orig)
        mod_extracted = self.is_extracted(mod, self.temp_dir_mod)
        
        # Extract button logic:
        # Enable if at least one is a game and needs extraction (not yet extracted to temp)
        needs_extract = (is_orig_game and not orig_extracted) or (is_mod_game and not mod_extracted)
        self.btn_extract.setEnabled(needs_extract and not is_running)

        # 3. Match button
        mode = self.mode_combo.currentText()
        paths_valid = os.path.exists(orig) and os.path.exists(mod)
        
        can_match = False
        if mode == "Image":
            can_match = paths_valid
        else:
            # For game modes, require both to be extracted
            # Check in-memory lists first (to enable button immediately after extraction)
            orig_ready = orig_extracted or not is_orig_game or self.has_images(orig) or bool(getattr(self, 'orig_textures', None))
            mod_ready = mod_extracted or not is_mod_game or self.has_images(mod) or bool(getattr(self, 'mod_textures', None))
            can_match = paths_valid and orig_ready and mod_ready
            
        # Check if we already have matching results (candidates)
        has_match_results = bool(getattr(self, 'results', None)) and any('candidates' in r for r in self.results)
        
        self.btn_match.setEnabled(can_match and not has_match_results and not is_running)

        # 4. Apply / Copy
        has_results = bool(getattr(self, 'results', None))
        
        # Apply logic: orig must be a game (to apply back) and mod must be ready
        # If both are images, apply usually means copy (overwriting orig).
        # Requirement: "org 경로가 게임이고, mod 경로에 이미지 파일만 있거나, mod 임시 폴더에 이미 다 풀려서 이미지파일들이 있는 경우"
        # Translation: Orig is game, and (Mod is image OR Mod is extracted)
        mod_ready = (not is_mod_game) or mod_extracted
        can_apply = (is_orig_game or mode == "Image") and mod_ready and has_results
        
        any_checked = any(r.get('replace', False) for r in self.results)
        
        self.btn_apply.setEnabled(can_apply and any_checked and not is_running)
        self.btn_copy_to.setEnabled(has_results and any_checked and not is_running)

        # 5. Options
        self.num_candidates.setEnabled(not is_running)
        self.chk_sim_filter.setEnabled(not is_running)
        self.txt_sim_cutoff.setEnabled(not is_running and self.chk_sim_filter.isChecked())
        self.chk_same_res.setEnabled(not is_running)
        self.diff_check.setEnabled(not is_running)
        self.chk_show_log.setEnabled(not is_running)
        self.btn_toggle_expand.setEnabled(has_results and not is_running)
        self.btn_uncheck_same.setEnabled(has_results and not is_running)

        is_unity_mode = mode in ["Unity", "Unity APK"]
        self.chk_uabea_format.setVisible(is_unity_mode)
        self.chk_uabea_format.setEnabled(is_unity_mode and not is_running)

        self.chk_high_quality.setVisible(is_unity_mode)
        self.chk_high_quality.setEnabled(is_unity_mode and not is_running)

        # UI Guarding: Disable tree and thumbnail interaction during tasks
        self.tree.setEnabled(not is_running)
        self.thumbnail_preview.setEnabled(not is_running)
        
        if is_running:
            self.log_area.appendPlainText("DEBUG: UI restricted due to active worker thread.")

    def clear_temp_folders(self):
        confirm = QMessageBox.question(self, "Confirm", "Clear all temporary extraction files?")
        if confirm == QMessageBox.StandardButton.Yes:
            for folder in [self.temp_dir_orig, self.temp_dir_mod]:
                if os.path.exists(folder):
                    shutil.rmtree(folder)
                    os.makedirs(folder)
            
            # Reset application state
            self.results = []
            self.orig_textures = []
            self.mod_textures = []
            self.tree.clear()
            self.update_status_counts()
            
            self.log("Temporary folders cleared and result list reset.")
            self.update_buttons()
            
    def on_diff_toggle(self, state):
        is_checked = (state == Qt.CheckState.Checked.value or state == Qt.CheckState.Checked)
        if is_checked:
            self.diff_check.setStyleSheet("background-color: yellow; color: black;")
        else:
            self.diff_check.setStyleSheet("")
            
        # Don't call comp_widget.toggle_diff as it triggers synchronous loading
        self.comp_widget.diff_mode = is_checked
        # Instead, trigger a fresh async preview for current selection
        self.on_selection_changed()



    def copy_to(self):
        to_replace = [r for r in self.results if r.get('replace') and r.get('match_file')]
        if not to_replace:
            QMessageBox.information(self, "Info", "No textures selected.")
            return

        target_dir = QFileDialog.getExistingDirectory(self, "Select Target Folder", self.orig_path.text())
        if not target_dir: return

        self.start_worker("copy", 
                          to_replace=to_replace, 
                          target_dir=target_dir,
                          orig_dir=os.path.normpath(self.orig_path.text()),
                          mode=self.mode_combo.currentText(),
                          use_uabea_format=self.chk_uabea_format.isChecked())
        self.update_buttons()

    def on_copy_finished(self, summary):
        self.update_buttons(is_finishing=True)
        QMessageBox.information(self, "Success", f"Copied {summary['success']} files to {summary['target_dir']}")
        self.log(f"Copied {summary['success']} files to {summary['target_dir']}")

    def apply_changes(self):
        to_replace = [r for r in self.results if r.get('replace') and r.get('match_file')]
        if not to_replace:
            QMessageBox.information(self, "Info", "No textures selected for replacement.")
            return

        confirm = QMessageBox.question(self, "Confirm", f"Replace {len(to_replace)} textures? This will modify original game files.")
        if confirm != QMessageBox.StandardButton.Yes:
            return
            
        orig_dir = self.orig_path.text()
        mod_dir = self.mod_path.text()
        
        self.progress_bar.setValue(0)
        self.progress_label.setText("Preparing apply...")
        self.start_worker("apply", to_replace=to_replace, 
                          orig_dir=orig_dir, mod_dir=mod_dir,
                          temp_orig=self.temp_dir_orig, temp_mod=self.temp_dir_mod,
                          mode=self.mode_combo.currentText(),
                          apk_info=getattr(self, 'apk_info', {}),
                          high_quality=self.chk_high_quality.isChecked())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_thumbnail_size()
        self.draw_thumbnail_viewport()
        # Update status text elision on resize
        if hasattr(self, 'full_status_text'):
            self.set_status_text(self.full_status_text)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    set_app_user_model_id() # Set AppUserModelID before creating the main window
    window = MainWindow()
    # Connect double click
    window.tree.itemDoubleClicked.connect(window.on_tree_double_clicked)
    window.show()
    sys.exit(app.exec())
