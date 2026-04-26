import cv2
import numpy as np
import os
from PIL import Image

def load_image(path, flags=cv2.IMREAD_UNCHANGED):
    """Robustly loads an image from a path, handling Unicode/special characters on Windows.
    Standardizes on BGRA (4 channels) for analytical consistency."""
    if not path or not isinstance(path, (str, bytes)) or not os.path.exists(path):
        return None
        
    try:
        # 1. Try OpenCV first (Faster, standard BGR/BGRA layout)
        try:
            img_array = np.fromfile(path, np.uint8)
            img = cv2.imdecode(img_array, flags)
            
            if img is not None:
                # Standardize to 4 channels (BGRA) if it's 3 channels (BGR)
                if len(img.shape) == 3:
                    if img.shape[2] == 3:
                        img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
                elif len(img.shape) == 2:
                    # Grayscale
                    img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
                
                # Normalize 16-bit to 8-bit for analytical consistency
                if img.dtype == np.uint16:
                    img = (img // 256).astype(np.uint8)
                return img
        except Exception:
            pass

        # 2. Fallback to PIL (Most robust for specific PNG extensions)
        with Image.open(path) as pil_img:
            # Standardizing to RGBA for direct buffer mapping
            if pil_img.mode != 'RGBA':
                pil_img = pil_img.convert('RGBA')
            
            data = pil_img.tobytes("raw", 'RGBA')
            w, h = pil_img.size
            img = np.frombuffer(data, dtype=np.uint8).reshape((h, w, 4))
            
            # Final conversion to OpenCV/BGRA format
            return cv2.cvtColor(img, cv2.COLOR_RGBA2BGRA).copy()
                
    except Exception as e:
        if path:
            print(f"Error loading image {path}: {e}")
        return None

def ensure_grayscale(img):
    """Converts an image to grayscale regardless of its channel count (1, 3, or 4)."""
    if len(img.shape) == 2:
        return img # Already grayscale
    
    channels = img.shape[2]
    if channels == 1:
        # Grayscale but 3D (H, W, 1)
        return img.reshape(img.shape[0], img.shape[1])
    elif channels == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    elif channels == 4:
        # For 4 channels, we convert BGRA to GRAY
        # CRITICAL: Unity textures often have garbage colors in transparent areas (Alpha=0).
        # We mask these out to ensure similarity only compares visible pixels.
        bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        alpha = img[:, :, 3]
        # Mask out any pixels that are nearly transparent (Alpha < 10)
        gray[alpha < 10] = 0
        return gray
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) # Fallback

def get_image_hash(image_path):
    """Calculates a perceptual hash for the image."""
    try:
        # Load as-is first then convert to grayscale strictly
        img = load_image(image_path)
        if img is None:
            return None
        
        gray = ensure_grayscale(img)
        gray = cv2.resize(gray, (16, 16), interpolation=cv2.INTER_AREA)
        avg = gray.mean()
        diff = gray > avg
        return diff.flatten().tolist()
    except Exception as e:
        print(f"Error hashing image {image_path}: {e}")
        return None

def hamming_similarity(hash1, hash2):
    """Calculates similarity based on Hamming distance."""
    if hash1 is None or hash2 is None or len(hash1) != len(hash2):
        return 0.0
    hash1 = np.array(hash1)
    hash2 = np.array(hash2)
    return 1.0 - (np.count_nonzero(hash1 != hash2) / len(hash1))

def compare_images(img1_path, img2_path):
    """Deep comparison of two images using MSE and SSIM."""
    try:
        img1 = load_image(img1_path)
        img2 = load_image(img2_path)
        
        if img1 is None or img2 is None:
            return 0.0
            
        # Standardize color space (ensure both are BGR/BGRA or Gray)
        # Resize img2 to match img1 if needed
        if img1.shape[:2] != img2.shape[:2]:
            img2 = cv2.resize(img2, (img1.shape[1], img1.shape[0]), interpolation=cv2.INTER_AREA)
            
        # Convert to grayscale for MSE calculation to focus on luminance/structure
        gray1 = ensure_grayscale(img1)
        gray2 = ensure_grayscale(img2)
        
        # Calculate Mean Squared Error
        mse = np.mean((gray1.astype("float") - gray2.astype("float")) ** 2)
        if mse == 0:
            return 1.0

        # Similarity score (1.0 is identical)
        # Using a slightly different heuristic to be more sensitive to structure
        similarity = 1.0 / (1.0 + mse/1000.0)

        return similarity
    except Exception as e:
        print(f"Error comparing images: {e}")
        return 0.0

def get_difference_mask(img1_path, img2_path, grayscale_bg=False):
    """Generates a mask showing differences in yellow.
    If grayscale_bg is True, the background (img1) is converted to grayscale first.
    Otherwise, the original color is preserved."""
    try:
        img1 = load_image(img1_path)
        img2 = load_image(img2_path)
        
        if img1 is None or img2 is None:
            return None
            
        if img1.shape != img2.shape:
            # Resize if dimensions differ
            if img1.shape[:2] != img2.shape[:2]:
                img2 = cv2.resize(img2, (img1.shape[1], img1.shape[0]), interpolation=cv2.INTER_AREA)
            
            # Normalize channel count if they differ (e.g. BGR vs BGRA)
            c1 = img1.shape[2] if len(img1.shape) == 3 else 1
            c2 = img2.shape[2] if len(img2.shape) == 3 else 1
            if c1 != c2:
                if c1 != 4:
                    img1 = cv2.cvtColor(img1, cv2.COLOR_BGR2BGRA if c1==3 else cv2.COLOR_GRAY2BGRA)
                if c2 != 4:
                    img2 = cv2.cvtColor(img2, cv2.COLOR_BGR2BGRA if c2==3 else cv2.COLOR_GRAY2BGRA)
        elif np.array_equal(img1, img2):
            # Perfectly identical, no difference to highlight
            return None
            
        # Compute absolute difference
        diff = cv2.absdiff(img1, img2)
        
        # Collapse to grayscale mask
        if len(diff.shape) == 3:
            # CRITICAL: Ignore Alpha (Channel 3) for the difference mask.
            # Unity de-compression artifacts are most prevalent in Alpha, 
            # and differening Alpha rarely affects the visual comparison 
            # but causes massive 'yellow streak' highlight noise.
            if diff.shape[2] == 4:
                mask = np.max(diff[:, :, :3], axis=2) # Peak diff in R, G, or B
            else:
                mask = np.max(diff, axis=2)
        else:
            mask = diff
            
        # Threshold the mask to get significant differences (set to 15 for optimal balance)
        _, thresh = cv2.threshold(mask, 20, 255, cv2.THRESH_BINARY)
        
        # --- ALPHA MASKING: Ignore differences in fully transparent areas ---
        if len(img1.shape) == 3 and img1.shape[2] == 4:
            alpha1 = img1[:, :, 3]
            alpha2 = img2[:, :, 3]
            # Visibility Mask: Only consider pixels that are visible in at least one image
            visible_mask = cv2.max(alpha1, alpha2)
            _, visible_thresh = cv2.threshold(visible_mask, 10, 255, cv2.THRESH_BINARY)
            thresh = cv2.bitwise_and(thresh, visible_thresh)
        
        # --- NOISE FILTER ---
        if cv2.countNonZero(thresh) > 0:
            # Removed MORPH_OPEN to preserve even single-pixel differences as requested.
            # Only applying a small median blur to smooth out extreme outliers.
            thresh = cv2.medianBlur(thresh, 3)
        
        if cv2.countNonZero(thresh) == 0:
            # No significant differences found after filtering
            return None
        
        if grayscale_bg:
            # Create a grayscale background from the original image
            gray_bg = ensure_grayscale(img1)
            # Suggestion 2: Darken the background by 50% to make yellow pop more
            gray_bg = (gray_bg.astype(np.float32) * 0.5).astype(np.uint8)
            
            # Suggestion 1: Dilate the mask so tiny changes (1px) are visible in thumbnails
            kernel = np.ones((3,3), np.uint8)
            thresh = cv2.dilate(thresh, kernel, iterations=1)

            # Convert grayscale background to BGR (or BGRA) so we can draw yellow on it
            if len(img1.shape) == 3 and img1.shape[2] == 4:
                result = cv2.cvtColor(gray_bg, cv2.COLOR_GRAY2BGRA)
            else:
                result = cv2.cvtColor(gray_bg, cv2.COLOR_GRAY2BGR)
        else:
            result = img1.copy()
        
        # Create yellow color
        # Determine if we have an alpha channel
        has_alpha = result.shape[2] == 4 if len(result.shape) == 3 else False
        yellow = [0, 255, 255, 255] if has_alpha else [0, 255, 255]
        
        # Highlight the differences in yellow
        if grayscale_bg:
            # Solid yellow for grayscale background (small thumbnail)
            result[thresh > 0] = yellow
        else:
            # Semi-transparent yellow for color background (large comparison view)
            alpha = 0.4 # Adjust transparency as needed (0.4 = 40% yellow, 60% original)
            overlay = result.copy()
            overlay[thresh > 0] = yellow
            cv2.addWeighted(overlay, alpha, result, 1 - alpha, 0, result)
        
        return result.copy()
    except Exception as e:
        print(f"Error generating difference mask: {e}")
        return None

if __name__ == "__main__":
    pass
