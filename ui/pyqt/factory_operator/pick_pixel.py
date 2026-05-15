"""맵 이미지에서 픽셀 좌표 클릭으로 Scene 좌표 확인하는 도구.

실행:
    cd ui/pyqt/factory_operator
    .venv/bin/python pick_pixel.py

이미지(1150×625)와 QGraphicsScene(1300×700)의 크기가 달라서,
클릭 좌표를 자동으로 Scene 좌표로 환산해 출력합니다.

클릭    → Scene 좌표 출력 (LOCATION_TO_SCENE 에 바로 붙여넣기 가능)
Ctrl+클릭 → 라벨 이름 입력
우클릭  → 마지막 마커 되돌리기
"""

import sys
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import QApplication, QLabel, QMainWindow, QStatusBar

IMG_PATH = Path(__file__).parent / "src/factory_operator/app/assets/real_maplayout.png"
DISPLAY_SCALE = 1.0  # 창이 너무 크면 0.8 등으로 줄이기

# QGraphicsScene 실제 크기 — _constants.py 의 SCENE_W / SCENE_H 와 동일해야 함
# roscamp-repo-2 는 이미지(1150×625)와 Scene 크기가 같으므로 스케일=1.0 (변환 없음)
SCENE_W = 1150
SCENE_H = 625


class PixelPicker(QLabel):
    def __init__(self, pixmap: QPixmap, img_w: int, img_h: int) -> None:
        super().__init__()
        self._orig = pixmap
        self._img_w = img_w
        self._img_h = img_h
        # 이미지 → Scene 스케일 계수
        self._sx = SCENE_W / img_w
        self._sy = SCENE_H / img_h
        self._marks: list[tuple[int, int, str]] = []  # (img_x, img_y, label)
        self._update_display()
        self.setMouseTracking(True)

    def _img_to_scene(self, ix: int, iy: int) -> tuple[int, int]:
        return round(ix * self._sx), round(iy * self._sy)

    def _update_display(self) -> None:
        canvas = self._orig.copy()
        painter = QPainter(canvas)
        pen = QPen(QColor("#ff0000"), 2)
        painter.setPen(pen)
        font = QFont("Sans", 9, QFont.Bold)
        painter.setFont(font)
        for ix, iy, label in self._marks:
            painter.drawEllipse(ix - 6, iy - 6, 12, 12)
            painter.drawText(ix + 8, iy - 4, label)
        painter.end()
        self.setPixmap(canvas)

    def mousePressEvent(self, event) -> None:
        ix = int(event.x() / DISPLAY_SCALE)
        iy = int(event.y() / DISPLAY_SCALE)
        sx, sy = self._img_to_scene(ix, iy)

        if event.modifiers() & Qt.ControlModifier:
            label, ok = _ask_label()
            if not ok:
                label = f"#{len(self._marks)+1}"
        else:
            label = f"#{len(self._marks)+1}"

        self._marks.append((ix, iy, label))
        self._update_display()

        print(f"[{label}]  이미지 픽셀: ({ix}, {iy})  →  Scene 좌표: ({sx}, {sy})")
        print(f'       LOCATION_TO_SCENE 에 추가: "{label}": ({sx}, {sy}),')
        print()

        self.window().statusBar().showMessage(
            f"{label}: 이미지({ix},{iy}) → Scene({sx},{sy})  |  우클릭=되돌리기  |  Ctrl+클릭=라벨"
        )

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.RightButton and self._marks:
            removed = self._marks.pop()
            sx, sy = self._img_to_scene(removed[0], removed[1])
            print(f"← 제거: {removed[2]}  이미지({removed[0]},{removed[1]}) → Scene({sx},{sy})")
            self._update_display()

    def mouseMoveEvent(self, event) -> None:
        ix = int(event.x() / DISPLAY_SCALE)
        iy = int(event.y() / DISPLAY_SCALE)
        sx, sy = self._img_to_scene(ix, iy)
        self.window().statusBar().showMessage(
            f"hover → Scene({sx}, {sy})  |  클릭=좌표찍기  |  우클릭=되돌리기  |  Ctrl+클릭=라벨"
        )


def _ask_label():
    from PyQt5.QtWidgets import QInputDialog
    text, ok = QInputDialog.getText(None, "라벨 입력", "LOCATION_TO_SCENE 키 이름 (예: PP, charging):")
    return text, ok


def main() -> None:
    app = QApplication(sys.argv)
    raw = QPixmap(str(IMG_PATH))
    if raw.isNull():
        print(f"이미지 없음: {IMG_PATH}")
        sys.exit(1)

    img_w, img_h = raw.width(), raw.height()

    pixmap = raw
    if DISPLAY_SCALE != 1.0:
        pixmap = raw.scaled(
            int(img_w * DISPLAY_SCALE),
            int(img_h * DISPLAY_SCALE),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )

    win = QMainWindow()
    win.setWindowTitle(
        f"Scene 좌표 확인 — {IMG_PATH.name} "
        f"({img_w}×{img_h} → Scene {SCENE_W}×{SCENE_H})"
    )
    win.setStatusBar(QStatusBar())
    win.statusBar().showMessage(
        "클릭 → Scene 좌표 출력.  Ctrl+클릭 → 키 이름 입력.  우클릭 → 되돌리기."
    )

    picker = PixelPicker(pixmap, img_w, img_h)
    win.setCentralWidget(picker)
    win.resize(pixmap.width(), pixmap.height() + 24)
    win.show()

    print(f"이미지: {img_w}×{img_h}px  →  Scene: {SCENE_W}×{SCENE_H}  "
          f"(sx={SCENE_W/img_w:.4f}, sy={SCENE_H/img_h:.4f})")
    print("클릭하면 Scene 좌표를 LOCATION_TO_SCENE 형식으로 출력합니다.\n")

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
