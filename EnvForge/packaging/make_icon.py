"""生成 EnvForge 应用图标（仅开发期使用，产物提交到 assets/）。

图形语义：三组“滑块”代表可调的环境配置；蓝绿渐变代表开发与运行。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 256
SCALE = 4
ASSETS = Path(__file__).resolve().parent.parent / "assets"


def _gradient(width: int, height: int) -> Image.Image:
    top = (22, 104, 220)
    bottom = (18, 184, 166)
    strip = Image.new("RGB", (1, height))
    pixels = strip.load()
    for y in range(height):
        ratio = y / max(height - 1, 1)
        pixels[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3))
    return strip.resize((width, height), Image.BICUBIC)


def main() -> None:
    size = SIZE * SCALE
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=int(size * 0.22), fill=255
    )
    canvas.paste(_gradient(size, size), (0, 0), mask)

    draw = ImageDraw.Draw(canvas)
    track_left = int(size * 0.20)
    track_right = int(size * 0.80)
    thickness = int(size * 0.035)
    rows = [0.375, 0.5, 0.625]
    knobs = [0.62, 0.35, 0.71]
    for row, knob in zip(rows, knobs):
        y = int(size * row)
        draw.rounded_rectangle(
            (track_left, y - thickness // 2, track_right, y + thickness // 2),
            radius=thickness // 2,
            fill=(255, 255, 255, 210),
        )
        cx = int(size * knob)
        radius = int(size * 0.062)
        draw.ellipse(
            (cx - radius, y - radius, cx + radius, y + radius),
            fill=(255, 255, 255, 255),
        )
        inner = int(radius * 0.42)
        draw.ellipse(
            (cx - inner, y - inner, cx + inner, y + inner),
            fill=(18, 138, 210, 255),
        )

    image = canvas.resize((SIZE, SIZE), Image.LANCZOS)
    ASSETS.mkdir(parents=True, exist_ok=True)
    image.save(ASSETS / "app.png")
    image.save(
        ASSETS / "app.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"已生成 {ASSETS / 'app.png'} 与 {ASSETS / 'app.ico'}")


if __name__ == "__main__":
    main()
