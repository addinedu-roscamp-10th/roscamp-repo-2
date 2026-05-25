# segmenter.py
import io
import numpy as np
import torch
from PIL import Image

from sam2.build_sam import build_sam2
from sam2.sam2_image_predictor import SAM2ImagePredictor
from transformers import AutoProcessor, AutoModelForZeroShotObjectDetection


class GroundedSAM2:
    def __init__(self,
                 grounding_model_id: str = "IDEA-Research/grounding-dino-tiny",
                 sam2_checkpoint: str = "./checkpoints/sam2.1_hiera_large.pt",
                 sam2_config: str = "configs/sam2.1/sam2.1_hiera_l.yaml",
                 device: str = "cuda"):
        self.device = device

        # Grounding DINO
        self.processor = AutoProcessor.from_pretrained(grounding_model_id)
        self.grounding_model = AutoModelForZeroShotObjectDetection.from_pretrained(grounding_model_id).to(device).eval()

        # SAM2
        sam2_model = build_sam2(sam2_config, sam2_checkpoint, device=device)
        self.sam2_predictor = SAM2ImagePredictor(sam2_model)

    @torch.no_grad()
    def segment(self,
                image_bytes: bytes,
                text_prompt: str = "white object on green conveyor.",
                box_threshold: float = 0.4,
                text_threshold: float = 0.3,
                alpha_threshold: int = 200,
                padding: int = 0) -> np.ndarray | None:
        """
        Returns cropped RGBA ndarray (H, W, 4) — 배경은 (0,0,0,0).
        객체 미검출 시 None.
        """
        image_pil = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        image_np = np.array(image_pil)
        w, h = image_pil.size

        # 1) Grounding DINO: text → bbox
        inputs = self.processor(images=image_pil, text=text_prompt, return_tensors="pt").to(self.device)
        outputs = self.grounding_model(**inputs)
        results = self.processor.post_process_grounded_object_detection(outputs,
                                                                        inputs.input_ids,
                                                                        threshold=box_threshold,
                                                                        text_threshold=text_threshold,
                                                                        target_sizes=[image_pil.size[::-1]])
        input_boxes = results[0]["boxes"].cpu().numpy()
        if len(input_boxes) == 0:
            return None

        # 2) SAM2: bbox → mask
        self.sam2_predictor.set_image(image_np)
        masks, _, _ = self.sam2_predictor.predict(point_coords=None,
                                                  point_labels=None,
                                                  box=input_boxes,
                                                  multimask_output=False)
        if masks.ndim == 4:
            masks = masks.squeeze(1)
        mask = masks[0]

        # 3) 경계 픽셀 제거 + 배경 검정
        alpha = (mask * 255).astype(np.uint8)
        strict_alpha = (alpha >= alpha_threshold).astype(np.uint8) * 255
        object_mask = strict_alpha > 0
        result_rgb = np.where(object_mask[:, :, np.newaxis], image_np, 0)
        rgba = np.dstack([result_rgb, strict_alpha])

        # 4) bbox crop
        ys, xs = np.where(object_mask)
        if xs.size == 0 or ys.size == 0:
            return None
        x1, x2 = max(xs.min() - padding, 0), min(xs.max() + padding + 1, w)
        y1, y2 = max(ys.min() - padding, 0), min(ys.max() + padding + 1, h)
        return rgba[y1:y2, x1:x2].astype(np.uint8)