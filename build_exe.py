import os
import subprocess
import shutil
import sys

def build():
    # 1. Clean up previous builds
    for folder in ['build', 'dist']:
        if os.path.exists(folder):
            shutil.rmtree(folder)
    
    spec_name = "UnityTextureChanger"
    
    # 2. PyInstaller command
    cmd = [
        "pyinstaller",
        "--noconsole",
        "--onefile",
        "--icon=app_icon.ico",
        f"--name={spec_name}",
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
        "--add-data=app_icon.ico;.",
        "--collect-all=UnityPy",
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
