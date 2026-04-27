[English](./README.md) | [한국어](./README_ko.md)

# Unity Texture Changer

A sophisticated GUI tool designed to intelligently compare and replace textures within Unity assets, APK files, or standard image folders. It leverages Perceptual Hashing and Structural Similarity algorithms to automate the matching process between original and modified versions.

## Key Features

- **Multiple Mode Support**:
  - **Unity**: Directly extract and replace textures from installed Unity game folders.
  - **Unity APK**: Decompile APKs, modify textures, and perform repackaging/signing.
  - **Image**: Support for 1:1 file matching and overwriting between standard image folders.
- **Intelligent Matching**:
  - Fast candidate search using Hamming Distance.
  - Precision similarity analysis based on MSE (Mean Squared Error).
  - Name-based matching (logic to ignore Unity's `_PathID` suffixes) and resolution filtering.
- **Advanced Viewer**:
  - Synchronized scrolling and zooming for side-by-side comparison.
  - Difference Highlight feature to detect subtle pixel changes.
- **Unity Engine Optimizations**:
  - Manual decoding for non-standard headers (e.g., Unity 2021.3.45).
  - Automatic patching of Addressables `catalog.json` (CRC/Size/MD5) to prevent game freezes.
  - Support for high-quality BC7 compression forcing.

## Requirements
- You may need a lot of storage for your studies involving the extraction of asset files.

### Prerequisites
- Python 3.10+
- **(For APK Mode)** Java Runtime Environment (JRE) or JDK installed and added to PATH.

### Library Installation
```bash
pip install PyQt6 UnityPy Pillow opencv-python numpy packaging
```

### Additional Files
- To use the APK repacking feature, place the `uber-apk-signer.jar` file in the same directory as the executable or `main.py`.

## Usage

1. **Set Paths**: Enter the path to the original game/images in 'Original Folder' and your modified images in 'Modified Folder'. (Supports Drag & Drop)
2. **Select Mode**: Choose the appropriate mode (Unity, APK, or Image).
3. **Extract**: For Unity/APK modes, extract textures from the assets. Previous sessions will be auto-loaded if available.
4. **Match**: Click 'Match' to automatically find the best candidates based on visual similarity.
5. **Review**: Select items in the list to compare them side-by-side and confirm the replacement candidates.
6. **Apply**: Click 'Apply Changes' to patch the assets or overwrite original files. (APK mode generates a new signed APK).

## Building the Executable

You can create a standalone `.exe` using the provided build script:

```bash
python build_exe.py
```
The output will be located in the `dist` folder.

## Hotkeys & Controls

### List (Results)
- **`Space`**: Toggle the 'Replace' checkbox for selected items. (Always checks sub-items).
- **`Delete`**: Remove selected original or candidate items from the list.
- **`+` (or `=`)**: Check all items for replacement.
- **`-`**: Uncheck all items.
- **`*` (Asterisk)**: Inverse all selection states.
- **`Double Click`**: (Original items) Scroll to and center the texture in the comparison view.

### Comparison Viewer
- **`Mouse Wheel`**: Zoom in/out relative to the mouse pointer.
- **`Left Drag`**: Pan/Scroll images.
- **`Top Right Thumbnail Click`**: Jump to the clicked position on the texture.

### Paths
- **`Double Click`**: Open folder selection dialog.
- **`Drag & Drop`**: Drop folders or APK files directly onto the input fields.

## Important Notes

- **Backup**: 'Apply Changes' modifies original files. Always create a backup before proceeding.
- **APK Signing**: Modded APKs are signed with a debug key. You may need to uninstall the original app before installing the modded version.

## License
This tool is for personal use and utilizes the `UnityPy` library for asset manipulation.