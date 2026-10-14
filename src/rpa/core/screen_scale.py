"""Computer Use 스크린샷 축소·좌표 역변환 (플랫폼 무관 순수 계산).

Claude API 이미지 한도 (computer_toolset_20260801 지원 모델 기준, 2026-10 문서 확인):
  - 긴 변 2576px
  - visual token ⌈w/28⌉ × ⌈h/28⌉ ≤ 4784
  - 한 요청에 이미지가 20장을 넘으면 각 변 2000px 이하
API는 자동 축소하지 않으므로 초과 이미지는 검증 오류가 된다.
모델이 준 좌표는 '우리가 보낸 전체 스크린샷' 픽셀 공간이므로 screen = origin + model / scale.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MAX_LONG_EDGE = 2576
MAX_VISUAL_TOKENS = 4784
TOKEN_PATCH = 28
MANY_IMAGES_MAX_SIDE = 2000


def visual_tokens(w: int, h: int) -> int:
    return math.ceil(w / TOKEN_PATCH) * math.ceil(h / TOKEN_PATCH)


@dataclass(frozen=True)
class ScreenshotPlan:
    source_width: int
    source_height: int
    origin_x: int
    origin_y: int
    scale: float
    width: int
    height: int

    @property
    def tokens(self) -> int:
        return visual_tokens(self.width, self.height)

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        """모델 좌표 → 실제 화면 물리 좌표. 범위를 벗어나면 ValueError (클램프하지 않음)."""
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise ValueError(f"좌표 ({x}, {y})가 스크린샷 범위 {self.width}x{self.height} 밖입니다")
        # 정수 절삭 때문에 축별 실제 비율을 사용한다
        sx = min(self.source_width - 1, int((x + 0.5) * self.source_width / self.width))
        sy = min(self.source_height - 1, int((y + 0.5) * self.source_height / self.height))
        return self.origin_x + sx, self.origin_y + sy

    def to_model(self, sx: int, sy: int) -> tuple[int, int]:
        return (
            int((sx - self.origin_x) * self.width / self.source_width),
            int((sy - self.origin_y) * self.height / self.source_height),
        )


def plan_screenshot(
    width: int, height: int, origin_x: int = 0, origin_y: int = 0, many_images: bool = False
) -> ScreenshotPlan:
    if width <= 0 or height <= 0:
        raise ValueError("화면 크기가 올바르지 않습니다")
    long_edge = MANY_IMAGES_MAX_SIDE if many_images else MAX_LONG_EDGE
    scale = min(1.0, long_edge / max(width, height))
    # 토큰 한도: 근사값에서 시작해 올림 효과로 넘치면 조금씩 줄인다
    scale = min(scale, math.sqrt(MAX_VISUAL_TOKENS * TOKEN_PATCH * TOKEN_PATCH / (width * height)))
    while True:
        w, h = max(1, int(width * scale)), max(1, int(height * scale))
        if visual_tokens(w, h) <= MAX_VISUAL_TOKENS and max(w, h) <= long_edge:
            return ScreenshotPlan(width, height, origin_x, origin_y, scale if scale < 1 else 1.0, w, h)
        scale *= 0.99
