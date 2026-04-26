from PIL import Image, ImageDraw
import os

def create_app_icon(output_path="app_icon.ico"):
    # 256x256 크기의 투명 배경 이미지 생성
    size = 256
    image = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)

    # 1. 원본 레이어 (어두운 회색, 뒤쪽)
    # [x0, y0, x1, y1]
    draw.rectangle([20, 40, 170, 190], fill=(70, 70, 70, 255), outline=(200, 200, 200, 255), width=10)
    
    # 2. 수정 레이어 (밝은 파란색, 앞쪽/겹침)
    draw.rectangle([80, 80, 230, 230], fill=(0, 120, 215, 255), outline=(255, 255, 255, 255), width=10)

    # 3. 변경을 상징하는 화살표 (흰색)
    # 뒤에서 앞으로 향하는 화살표
    draw.line([110, 135, 150, 135], fill="white", width=8)
    draw.polygon([150, 120, 170, 135, 150, 150], fill="white")

    # 다양한 크기를 포함하는 ICO 파일로 저장
    icon_sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    image.save(output_path, format='ICO', sizes=icon_sizes)
    print(f"Icon created successfully: {os.path.abspath(output_path)}")

if __name__ == "__main__":
    create_app_icon()