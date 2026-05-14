"""맵 이미지에서 픽셀 좌표 클릭으로 확인하는 도구.

실행:
    cd ui/pyqt/factory_operator
    .venv/bin/python pick_pixel.py

클릭하면 터미널에 좌표 출력. Ctrl+클릭 시 복사 가능한 상수 형식으로 출력.
"""

import sys
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap, QPainter, QPen, QColor, QFont
from PyQt5.QtWidgets import QApplication, QLabel, QMainWindow, QStatusBar

IMG_PATH = Path(__file__).parent / "src/factory_operator/app/assets/real_maplayout.png"
SCALE = 1.0  # 창이 너무 크면 0.8 등으로 줄이기


class PixelPicker(QLabel):
    def __init__(self, pixmap):
        super().__init__()
        self._orig = pixmap
        self._marks: list[tuple[int, int, str]] = []
        self._update_display()
        self.setMouseTracking(True)

    def _update_display(self):
        canvas = self._orig.copy()
        painter = QPainter(canvas)
        pen = QPen(QColor("#ff0000"), 2)
        painter.setPen(pen)
        font = QFont("Sans", 9, QFont.Bold)
        painter.setFont(font)
        for x, y, label in self._marks:
            painter.drawEllipse(x - 6, y - 6, 12, 12)
            painter.drawText(x + 8, y - 4, label)
        painter.end()
        self.setPixmap(canvas)

    def mousePressEvent(self, event):
        x = int(event.x() / SCALE)
        y = int(event.y() / SCALE)

        label = ""
        if event.modifiers() & Qt.ControlModifier:
            label, ok = _ask_label()
            if not ok:
                label = f"#{len(self._marks)+1}"
        else:
            label = f"#{len(self._marks)+1}"

        self._marks.append((x, y, label))
        self._update_display()

        print(f"[{label}] scene 좌표: ({x}, {y})")
        print(f"       → 상수 형식:  ({x}.0, {y}.0)")
        print()

        bar = self.window().statusBar()
        bar.showMessage(f"{label}: ({x}, {y})  |  우클릭=되돌리기  |  Ctrl+클릭=라벨 지정")

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.RightButton and self._marks:
            removed = self._marks.pop()
            print(f"← 제거: {removed[2]} ({removed[0]}, {removed[1]})")
            self._update_display()

    def mouseMoveEvent(self, event):
        x = int(event.x() / SCALE)
        y = int(event.y() / SCALE)
        bar = self.window().statusBar()
        bar.showMessage(f"hover: ({x}, {y})  |  클릭=좌표찍기  |  우클릭=되돌리기  |  Ctrl+클릭=라벨 지정")


def _ask_label():
    from PyQt5.QtWidgets import QInputDialog
    text, ok = QInputDialog.getText(None, "라벨", "마커 이름:")
    return text, ok


def main():
    app = QApplication(sys.argv)
    pixmap = QPixmap(str(IMG_PATH))
    if pixmap.isNull():
        print(f"이미지 없음: {IMG_PATH}")
        sys.exit(1)

    if SCALE != 1.0:
        pixmap = pixmap.scaled(
            int(pixmap.width() * SCALE),
            int(pixmap.height() * SCALE),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )

    win = QMainWindow()
    win.setWindowTitle(f"픽셀 좌표 확인 — {IMG_PATH.name}  ({pixmap.width()}×{pixmap.height()})")
    win.setStatusBar(QStatusBar())
    win.statusBar().showMessage("클릭하면 좌표 출력. Ctrl+클릭은 라벨 지정. 우클릭은 되돌리기.")

    picker = PixelPicker(pixmap)
    win.setCentralWidget(picker)
    win.resize(pixmap.width(), pixmap.height() + 24)
    win.show()

    print(f"이미지 크기: {pixmap.width()}×{pixmap.height()} px  (원본 기준 1150×625)")
    print("클릭 → 좌표 출력. Ctrl+클릭 → 라벨 지정. 우클릭 → 되돌리기.\n")
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
