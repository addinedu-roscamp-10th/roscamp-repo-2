import io
import base64

import cv2
import numpy as np
from PIL import Image

import torch
import torchvision.transforms.functional as F


def preprocess(image_bytes: bytes, img_h: int, img_w: int, device: str) -> tuple[torch.Tensor, np.ndarray]:
    """추론용 텐서(img_h x img_w)와 시각화용 BGR 이미지(원본 해상도) 반환."""
    image_pil = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    image_np = np.array(image_pil)

    # CLAHE (학습 코드와 동일)
    clahe_cv = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    lab = cv2.cvtColor(image_np, cv2.COLOR_RGB2LAB)
    lab[:, :, 0] = clahe_cv.apply(lab[:, :, 0])
    clahe_np = cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)

    # 추론용 텐서: 학습과 동일하게 [H, W]로 강제 resize
    tensor = F.to_tensor(clahe_np)
    tensor = F.resize(tensor, [img_h, img_w])
    tensor = F.normalize(tensor, mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    tensor = tensor.unsqueeze(0).to(device)

    # 시각화용: 원본 해상도 그대로 (resize X)
    vis_bgr = cv2.cvtColor(image_np, cv2.COLOR_RGB2BGR)
    return tensor, vis_bgr


def make_result_image(original_bgr: np.ndarray, anomaly_map: np.ndarray, pred_mask: np.ndarray) -> bytes:
    """원본 / heatmap / mask contour 3패널 PNG bytes."""
    h, w = original_bgr.shape[:2]

    # anomaly_map과 pred_mask를 원본 해상도로 resize
    anomaly_map_resized = cv2.resize(anomaly_map, (w, h), interpolation=cv2.INTER_LINEAR)
    pred_mask_resized = cv2.resize(pred_mask, (w, h), interpolation=cv2.INTER_NEAREST)

    panel_original = original_bgr.copy()
    # cv2.putText(panel_original, "Image", (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2)

    heatmap = cv2.applyColorMap((anomaly_map_resized * 255).astype(np.uint8), cv2.COLORMAP_JET)
    panel_heatmap = cv2.addWeighted(original_bgr, 0.6, heatmap, 0.4, 0)
    # cv2.putText(panel_heatmap, "Image + Anomaly Map", (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2)

    panel_mask = original_bgr.copy()
    contours, _ = cv2.findContours(pred_mask_resized, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(panel_mask, contours, -1, (0, 0, 255), 2)
    # cv2.putText(panel_mask, "Image + Pred Mask", (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2)

    combined = np.hstack([panel_original, panel_heatmap, panel_mask])
    success, buf = cv2.imencode(".png", combined)
    if not success:
        raise RuntimeError("Failed to encode result image")
    return buf.tobytes()


def run_inference(model, image_bytes: bytes, img_h: int, img_w: int, device: str) -> dict:
    """전처리 → 모델 → 후처리 → 시각화까지의 단일 진입점."""
    tensor, vis_bgr = preprocess(image_bytes, img_h, img_w, device)

    with torch.no_grad():
        raw_output = model.model(tensor)
        output = model.post_processor(raw_output)

    anomaly_map = output.anomaly_map.squeeze().detach().cpu().numpy().astype(np.float32)
    if getattr(output, "pred_mask", None) is not None:
        pred_mask = (output.pred_mask.squeeze().cpu().numpy().astype(np.uint8)) * 255
    else:
        pixel_threshold = model.post_processor.pixel_threshold.item()
        pred_mask = (anomaly_map > pixel_threshold).astype(np.uint8) * 255

    result_png = make_result_image(vis_bgr, anomaly_map, pred_mask)
    result_b64 = base64.b64encode(result_png).decode("ascii")

    ok, buf = cv2.imencode(".png", vis_bgr)
    if not ok:
        raise RuntimeError("Failed to encode input image")
    segment_b64 = base64.b64encode(buf.tobytes()).decode("ascii")

    return {"pred_label": "Anomalous" if bool(output.pred_label.item()) else "Normal",
            "pred_score": round(output.pred_score.item(), 4),
            "segmented_image": segment_b64,
            "result_image": result_b64}