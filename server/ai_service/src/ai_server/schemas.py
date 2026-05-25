# schemas.py
from typing import Literal
from pydantic import BaseModel, Field

class PredictResponse(BaseModel):
    pred_label: Literal["Normal", "Anomalous"] = Field(
        ..., description="이상 여부 판정 결과"
    )
    pred_score: float = Field(
        ..., description="이상 점수 (높을수록 비정상)"
    )
    segmented_image: str = Field(
        ..., description="전처리된 입력 이미지 PNG (base64 인코딩)"
    )
    result_image: str = Field(
        ..., description="시각화 결과 PNG 이미지 (base64 인코딩)"
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "pred_label": "Anomalous",
                "pred_score": 0.7234,
                "segmented_image": "iVBORw0KGgoAAAANSUhEUg...",
                "result_image": "iVBORw0KGgoAAAANSUhEUg...",
            }
        }
    }


class ErrorResponse(BaseModel):
    detail: str = Field(..., description="에러 메시지")