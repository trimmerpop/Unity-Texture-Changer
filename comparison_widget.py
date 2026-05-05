from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea
from PyQt6.QtCore import Qt, QPoint, QRect, QSize, pyqtSignal
from PyQt6.QtGui import QPixmap, QImage, QPainter, QPen, QWheelEvent, QMouseEvent
import cv2
import numpy as np
from similarity import load_image

class NoWheelScrollArea(QScrollArea):
    def wheelEvent(self, event):
        # Ignore wheel event to let it propagate to ComparisonWidget for zooming
        event.ignore()

class ImageLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.main_pixmap = None
        self.zoom_factor = 1.0
        self.last_mouse_pos = QPoint()
        
    def set_image(self, pixmap):
        self.main_pixmap = pixmap
        if pixmap and not pixmap.isNull():
            self.setText("")
        self.update_view()
        
    def update_view(self):
        if not self.main_pixmap:
            if not self.text():
                self.setPixmap(QPixmap())
                self.setFixedSize(0, 0)
            return
            
        scaled_size = self.main_pixmap.size() * self.zoom_factor
        scaled_pixmap = self.main_pixmap.scaled(scaled_size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.setPixmap(scaled_pixmap)
        self.setFixedSize(scaled_pixmap.size())

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self.last_mouse_pos = event.globalPosition().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event: QMouseEvent):
        if event.buttons() & Qt.MouseButton.LeftButton:
            delta = event.globalPosition().toPoint() - self.last_mouse_pos
            self.last_mouse_pos = event.globalPosition().toPoint()
            # Find the scroll area parent
            scroll_area = self.parent().parent()
            if isinstance(scroll_area, QScrollArea):
                h_bar = scroll_area.horizontalScrollBar()
                v_bar = scroll_area.verticalScrollBar()
                h_bar.setValue(h_bar.value() - delta.x())
                v_bar.setValue(v_bar.value() - delta.y())

    def mouseReleaseEvent(self, event: QMouseEvent):
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def wheelEvent(self, event: QWheelEvent):
        # Let wheel events bubble up to ComparisonWidget for zooming
        event.ignore()

class ComparisonWidget(QWidget):
    viewportChanged = pyqtSignal(float, float, float, float) # x, y, w, h (normalized)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.main_layout = QHBoxLayout(self)
        
        # Original Image
        self.original_scroll = NoWheelScrollArea()
        self.original_label = ImageLabel()
        self.original_scroll.setWidget(self.original_label)
        self.original_scroll.setWidgetResizable(True)
        
        # Matched Image
        self.matched_scroll = NoWheelScrollArea()
        self.matched_label = ImageLabel()
        self.matched_scroll.setWidget(self.matched_label)
        self.matched_scroll.setWidgetResizable(True)
        
        self.main_layout.addWidget(self.original_scroll)
        self.main_layout.addWidget(self.matched_scroll)
        
        # Synchronize scrolls
        self.original_scroll.horizontalScrollBar().valueChanged.connect(
            self.matched_scroll.horizontalScrollBar().setValue)
        self.original_scroll.verticalScrollBar().valueChanged.connect(
            self.matched_scroll.verticalScrollBar().setValue)
        self.matched_scroll.horizontalScrollBar().valueChanged.connect(
            self.original_scroll.horizontalScrollBar().setValue)
        self.matched_scroll.verticalScrollBar().valueChanged.connect(
            self.original_scroll.verticalScrollBar().setValue)
            
        # Emit viewport change on scroll
        self.original_scroll.horizontalScrollBar().valueChanged.connect(lambda: self.emit_viewport_changed())
        self.original_scroll.verticalScrollBar().valueChanged.connect(lambda: self.emit_viewport_changed())
            
        self._is_updating = False
        self.diff_mode = False
        self.current_img1_path = None
        self.current_img2_path = None

    def set_images(self, img1_path, img2_path, show_diff=False):
        self._is_updating = True
        try:
            self.current_img1_path = img1_path
            self.current_img2_path = img2_path
            self.diff_mode = show_diff
            
            # Clear loading state
            self.original_label.setText("")
            self.matched_label.setText("")
            
            def get_safe_pixmap(path):
                """Unifies loading using the standardized analytical pipeline (load_image)."""
                if not path or not os.path.exists(path): return QPixmap()
                
                try:
                    from similarity import load_image
                    img = load_image(path) # returns uint8 BGRA
                    if img is None:
                        # Final fallback to native Qt
                        return QPixmap(path)
                    
                    # Standardize using a PNG-encoded buffer handoff
                    success, buffer = cv2.imencode('.png', img)
                    if success:
                        qimg = QImage()
                        if qimg.loadFromData(buffer.tobytes()):
                            return QPixmap.fromImage(qimg)
                    
                except Exception as e:
                    print(f"Standardized loading failed for {path}: {e}")
                    return QPixmap(path)
                return QPixmap()

            if self.diff_mode:
                from similarity import get_difference_mask
                diff_img = get_difference_mask(img1_path, img2_path, grayscale_bg=False)
                if diff_img is not None:
                    try:
                        success, buffer = cv2.imencode('.png', diff_img)
                        if success:
                            qimg = QImage()
                            if qimg.loadFromData(buffer.tobytes()):
                                self.original_label.set_image(QPixmap.fromImage(qimg))
                    except Exception as e:
                        print(f"Comparison diff rendering failed: {e}")
                        # Use existing safe pixmap logic
                        self.original_label.set_image(get_safe_pixmap(img1_path))
                else:
                    self.original_label.set_image(get_safe_pixmap(img1_path))
            else:
                self.original_label.set_image(get_safe_pixmap(img1_path))
                
            self.matched_label.set_image(get_safe_pixmap(img2_path))
        finally:
            self._is_updating = False
        self.emit_viewport_changed()

    def set_loading(self, loading=True):
        self._is_updating = True
        try:
            if loading:
                # Show loading text
                loading_style = "font-size: 18px; font-weight: bold; color: #888;"
                self.original_label.setText("Loading...")
                self.original_label.setStyleSheet(loading_style)
                self.original_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                # Make labels fill viewport for centering
                self.original_label.setFixedSize(self.original_scroll.viewport().size())
                
                self.matched_label.setText("Loading...")
                self.matched_label.setStyleSheet(loading_style)
                self.matched_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self.matched_label.setFixedSize(self.matched_scroll.viewport().size())
            else:
                self.original_label.setText("")
                self.matched_label.setText("")
                self.original_label.setStyleSheet("")
                self.matched_label.setStyleSheet("")
        finally:
            self._is_updating = False

    def emit_viewport_changed(self):
        if self._is_updating: return
        if not self.original_label.main_pixmap: return
        h_bar = self.original_scroll.horizontalScrollBar()
        v_bar = self.original_scroll.verticalScrollBar()
        viewport_rect = self.original_scroll.viewport().rect()
        label_size = self.original_label.size()
        img_size = self.original_label.main_pixmap.size()
        if label_size.width() == 0 or label_size.height() == 0: return
        x_ratio = img_size.width() / label_size.width()
        y_ratio = img_size.height() / label_size.height()
        visible_x = h_bar.value() * x_ratio
        visible_y = v_bar.value() * y_ratio
        visible_w = viewport_rect.width() * x_ratio
        visible_h = viewport_rect.height() * y_ratio
        self.viewportChanged.emit(visible_x / img_size.width(), 
                                  visible_y / img_size.height(),
                                  min(1.0, visible_w / img_size.width()), 
                                  min(1.0, visible_h / img_size.height()))

    def scroll_to_normalized(self, x_norm, y_norm):
        if not self.original_label.main_pixmap: return
        
        label_size = self.original_label.size()
        h_bar = self.original_scroll.horizontalScrollBar()
        v_bar = self.original_scroll.verticalScrollBar()
        viewport_rect = self.original_scroll.viewport().rect()
        
        target_x = int(x_norm * label_size.width()) - viewport_rect.width() // 2
        target_y = int(y_norm * label_size.height()) - viewport_rect.height() // 2
        
        h_bar.setValue(target_x)
        v_bar.setValue(target_y)

    def set_scroll_normalized(self, x_norm, y_norm):
        """Sets scroll position to top-left normalized coordinates."""
        if not self.original_label.main_pixmap: return
        label_size = self.original_label.size()
        self.original_scroll.horizontalScrollBar().setValue(int(x_norm * label_size.width()))
        self.original_scroll.verticalScrollBar().setValue(int(y_norm * label_size.height()))

    def wheelEvent(self, event: QWheelEvent):
        angle = event.angleDelta().y()
        if angle == 0: return
        
        # Calculate mouse position relative to image before zoom
        pos = event.position().toPoint()
        # Which widget is under mouse? (Original or Matched)
        is_matched = self.matched_scroll.underMouse()
        scroll_area = self.matched_scroll if is_matched else self.original_scroll
        label = self.matched_label if is_matched else self.original_label
        
        # Normalized position in label
        local_pos = label.mapFrom(self, pos)
        x_mapped = local_pos.x() / label.width() if label.width() > 0 else 0.5
        y_mapped = local_pos.y() / label.height() if label.height() > 0 else 0.5

        # Mouse position relative to viewport
        viewport_pos = scroll_area.viewport().mapFrom(self, pos)

        factor = 1.1 if angle > 0 else 0.9
        old_zoom = self.original_label.zoom_factor
        new_zoom = max(0.1, min(10.0, old_zoom * factor))
        
        self.original_label.zoom_factor = new_zoom
        self.matched_label.zoom_factor = new_zoom
        self.original_label.update_view()
        self.matched_label.update_view()
        
        # Adjust scroll bars to keep mouse position fixed
        h_bar = scroll_area.horizontalScrollBar()
        v_bar = scroll_area.verticalScrollBar()
        
        new_x = int(x_mapped * label.width()) - viewport_pos.x()
        new_y = int(y_mapped * label.height()) - viewport_pos.y()
        
        h_bar.setValue(new_x)
        v_bar.setValue(new_y)
        
        self.emit_viewport_changed()
        event.accept()

    def toggle_diff(self, enabled):
        self.diff_mode = enabled
        if self.current_img1_path and self.current_img2_path:
            self.set_images(self.current_img1_path, self.current_img2_path, self.diff_mode)
