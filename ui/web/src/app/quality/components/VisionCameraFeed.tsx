"use client";

import { useEffect, useState } from "react";
import { CheckCircle, XCircle } from "lucide-react";
import type { InspectionRecord } from "@/lib/types";
import { formatDate } from "@/lib/utils";

// 2026-05-15: 검사 이미지 base URL — backend HttpImageServer 또는 AI 서버 HTTP.
// 환경변수로 오버라이드 가능 (예: http://100.66.177.119:8080).
const IMAGE_BASE_URL = (
  process.env.NEXT_PUBLIC_INSPECTION_IMAGE_BASE ?? "http://localhost:18800"
).replace(/\/$/, "");

interface VisionCameraFeedProps {
  latestInspection: InspectionRecord | null;
}

export function VisionCameraFeed({ latestInspection }: VisionCameraFeedProps) {
  const isLatestPass = latestInspection?.result === "pass";
  const [imageError, setImageError] = useState(false);

  // 검사가 바뀌면 error state 초기화.
  const imageId = latestInspection?.imageId ?? null;
  useEffect(() => {
    setImageError(false);
  }, [imageId]);

  const imageUrl = imageId ? `${IMAGE_BASE_URL}/${imageId}.jpg` : null;
  const showImage = !!imageUrl && !imageError;

  return (
    <div className="relative bg-gray-950 rounded-xl overflow-hidden aspect-video flex items-center justify-center border border-gray-800">
      {/* 스캔라인 효과 */}
      <div className="absolute inset-0 opacity-[0.07]">
        {Array.from({ length: 30 }).map((_, i) => (
          <div
            key={i}
            className="w-full border-t border-green-400"
            style={{ marginTop: `${i * 3.33}%` }}
          />
        ))}
      </div>
      {/* 그리드 오버레이 */}
      <div
        className="absolute inset-0 opacity-[0.04]"
        style={{
          backgroundImage:
            "linear-gradient(rgba(34,197,94,1) 1px, transparent 1px), linear-gradient(90deg, rgba(34,197,94,1) 1px, transparent 1px)",
          backgroundSize: "20% 20%",
        }}
      />
      {/* 비네팅 효과 */}
      <div className="absolute inset-0 bg-gradient-to-r from-black/30 via-transparent to-black/30" />
      <div className="absolute inset-0 bg-gradient-to-b from-black/20 via-transparent to-black/30" />
      {/* 이미지 표시 — 검사 row 의 imageId 로 fetch. 실패 시 NO IMAGE 워터마크. */}
      {showImage && imageUrl && (
        <img
          src={imageUrl}
          alt={imageId ?? "inspection"}
          onError={() => setImageError(true)}
          className="absolute inset-0 z-10 h-full w-full object-contain"
        />
      )}
      {!showImage && (
        <div className="relative z-10 flex flex-col items-center gap-3">
          <span className="text-5xl font-bold text-gray-500/30 select-none tracking-[0.4em]">
            NO IMAGE
          </span>
          <span className="text-sm text-green-500/80 font-mono tracking-wider">
            item_id: {latestInspection?.castingId ?? "---"}
          </span>
        </div>
      )}
      {/* PASS / FAIL 배지 (검사 결과가 있을 때만) */}
      {latestInspection && (
        <div className="absolute top-3 right-3 z-20">
          {isLatestPass ? (
            <span className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg text-base font-bold bg-green-600 text-white shadow-lg shadow-green-600/40">
              <CheckCircle className="w-4 h-4" /> PASS
            </span>
          ) : (
            <span className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg text-base font-bold bg-red-600 text-white shadow-lg shadow-red-600/40">
              <XCircle className="w-4 h-4" /> FAIL
            </span>
          )}
        </div>
      )}
      {/* 타임스탬프 */}
      <div className="absolute bottom-3 right-3 z-20">
        <span className="text-sm font-mono text-gray-400 bg-black/70 px-2.5 py-1 rounded-md border border-gray-700/50">
          {latestInspection ? formatDate(latestInspection.inspectedAt) : "--:--"}
        </span>
      </div>
    </div>
  );
}
