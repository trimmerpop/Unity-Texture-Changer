[English](./README.md) | [한국어](./README_ko.md)

# Unity Texture Changer

유니티 에셋, APK 파일 또는 일반 이미지 폴더 내의 텍스처를 지능적으로 비교하고 교체할 수 있는 GUI 도구입니다. 지각적 해싱(Perceptual Hashing)과 구조적 유사도 알고리즘을 사용하여 원본과 수정본 사이의 매칭 작업을 자동화합니다.

## 주요 기능

- **다양한 모드 지원**:
  - **Unity**: 설치된 유니티 게임 폴더에서 직접 텍스처를 추출 및 교체합니다.
  - **Unity APK**: APK 파일을 해체하여 텍스처를 수정하고, 다시 패키징 및 서명(Signing)까지 수행합니다.
  - **Image**: 일반 이미지 폴더 간의 1:1 파일 매칭 및 덮어쓰기를 지원합니다.
- **지능형 매칭**:
  - Hamming Distance 기반의 빠른 후보 탐색.
  - MSE(Mean Squared Error) 기반의 정밀 유사도 분석.
  - 이름 기반 매칭(PathID 제외 로직 포함) 및 해상도 필터링 지원.
- **고급 뷰어**:
  - 원본과 수정본의 동기화된 스크롤 및 줌 기능.
  - 차이점 강조(Difference Highlight) 기능으로 미세한 변화 감지.
- **유니티 엔진 최적화**:
  - Unity 2021.3.45 등 최신 버전의 비표준 헤더 수동 디코딩 지원.
  - Addressables `catalog.json`의 CRC/Size/MD5 자동 패치로 게임 프리징 방지.
  - 고품질 BC7 압축 강제 옵션 지원.

## 요구 사항
- assets 파일을 추출하는 학업에서 많은 스토리지가 필요할 수 있습니다.

### 필수 소프트웨어
- Python 3.10 이상
- **(APK 모드 사용 시)** Java Runtime Environment (JRE) 또는 JDK (시스템 PATH 등록 필요)

### 라이브러리 설치
```bash
pip install PyQt6 UnityPy Pillow opencv-python numpy packaging
```

### 추가 파일
- APK 리패키징 기능을 사용하려면 `uber-apk-signer.jar` 파일을 실행 파일 또는 `main.py`와 동일한 폴더에 배치해야 합니다.

## 사용 방법

1. **경로 설정**: 'Original Folder'에 원본 경로를, 'Modified Folder'에 수정된 이미지 경로를 입력합니다. (드래그 앤 드롭 지원)
2. **모드 선택**: 작업 대상에 따라 Unity, APK, 또는 Image 모드를 선택합니다.
3. **추출 (Extract)**: Unity/APK 모드에서 에셋의 텍스처를 추출합니다. 기존 작업 내역은 자동으로 로드됩니다.
4. **매칭 (Match)**: 'Match' 버튼을 눌러 시각적 유사도가 높은 이미지들을 자동으로 찾습니다.
5. **검토**: 리스트에서 항목을 선택하여 두 이미지의 차이를 확인하고 교체 대상을 확정합니다.
6. **적용 (Apply)**: 'Apply Changes'를 클릭하여 에셋을 수정하거나 원본을 덮어씁니다. (APK 모드는 신규 서명 APK 생성)

## 빌드 방법 (EXE 생성)

제공된 빌드 스크립트를 사용하여 단일 실행 파일을 만들 수 있습니다.

```bash
python build_exe.py
```
빌드 결과물은 `dist` 폴더 내에 생성됩니다.

## 주의 사항

- **백업 필수**: 'Apply Changes'는 원본 파일을 직접 수정할 수 있으므로 작업 전에 반드시 백업을 생성하십시오.
- **APK 서명**: 생성된 모드 APK는 디버그 키로 서명됩니다. 기존 정식 앱이 설치되어 있다면 삭제 후 설치해야 할 수 있습니다.

## 라이선스
이 도구는 개인적인 용도로 제작되었으며, 유니티 에셋 처리를 위해 `UnityPy` 라이브러리를 사용합니다.