import os
import re
import subprocess
import shutil
import sys

def read_app_version(project_dir):
    """Reads APP_VERSION from main.py without importing it (no Qt side effects)."""
    path = os.path.join(project_dir, "main.py")
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        match = re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
        if match:
            return match.group(1)
    except OSError:
        pass
    return "0.0"


def write_version_file(project_dir, version):
    """Writes the PyInstaller version resource so Explorer shows the exe version."""
    parts = [int(part) for part in re.findall(r"\d+", version)[:3]] or [0]
    while len(parts) < 4:
        parts.append(0)
    tuple_version = tuple(parts[:4])
    content = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={tuple_version},
    prodvers={tuple_version},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'trimmerpop'),
        StringStruct('FileDescription', 'Unity Texture Changer'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', 'UnityTextureChanger'),
        StringStruct('OriginalFilename', 'UnityTextureChanger.exe'),
        StringStruct('ProductName', 'Unity Texture Changer'),
        StringStruct('ProductVersion', '{version}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    path = os.path.join(project_dir, "version_info.txt")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


def build():
    # 1. Clean up previous builds
    for folder in ['build', 'dist']:
        if os.path.exists(folder):
            shutil.rmtree(folder)
    
    spec_name = "UnityTextureChanger"
    project_dir = os.path.dirname(os.path.abspath(__file__))
    version = read_app_version(project_dir)
    version_file = write_version_file(project_dir, version)
    print(f"Building {spec_name} v{version} (version resource: {os.path.basename(version_file)})")
    
    # Get fmod.dll path from installed fmod_toolkit package
    try:
        import fmod_toolkit
        fmod_dll_path = os.path.join(
            os.path.dirname(fmod_toolkit.__file__),
            'libfmod', 'Windows', 'x64', 'fmod.dll'
        )
    except ImportError:
        print("ERROR: fmod_toolkit package not found. Please install it first.")
        return
    
    # 2. PyInstaller command
    cmd = [
        "pyinstaller",
        "--noconsole",
        "--onefile",
        "--icon=app_icon.ico",
        f"--name={spec_name}",
        f"--version-file={version_file}",
        "--hidden-import=PyQt6.sip",
        "--hidden-import=UnityPy",
        "--hidden-import=UnityPy.helpers.TypeTreeHelper",
        "--hidden-import=etcpak",
        "--hidden-import=brotli",
        "--hidden-import=lz4",
        "--hidden-import=zstandard",
        "--hidden-import=backports.lzma",
        "--hidden-import=PIL",
        "--hidden-import=cv2",
        "--hidden-import=numpy",
        "--hidden-import=packaging",
        "--hidden-import=packaging.version",
        "--hidden-import=packaging.specifiers",
        "--hidden-import=packaging.requirements",
        "--hidden-import=fmod_toolkit",
        "--add-data=app_icon.ico;.",
        f"--add-data={fmod_dll_path};fmod_toolkit/libfmod/Windows/x64",
        "--collect-all=UnityPy",
        "--collect-all=archspec",
        "--collect-all=astc_encoder",
        "main.py"
    ]
    
    print(f"Running command: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    
    if result.returncode == 0:
        print("\nBuild successful!")
        print(f"Executable can be found in the 'dist' folder.")
        
        # Guide about uber-apk-signer.jar
        print("\nIMPORTANT: Remember to copy 'uber-apk-signer.jar' to the 'dist' folder")
        print("if you want to use the APK repacking feature in the executable.")
    else:
        print("\nBuild failed.")

if __name__ == "__main__":
    build()