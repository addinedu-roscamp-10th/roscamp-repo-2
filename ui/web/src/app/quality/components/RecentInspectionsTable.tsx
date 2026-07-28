"use client";

import { useMemo } from "react";
import { ClipboardList } from "lucide-react";
import { mockInspectionStandards } from "@/lib/mock-data";
import type { InspectionRecord } from "@/lib/types";

const MAX_ROWS = 10;

// productId → productName (PyQt 와 동일 라벨).
const PRODUCT_NAMES: Record<string, string> = Object.fromEntries(
  mockInspectionStandards.map((s) => [s.productId, s.productName])
);

// "2026-03-30T09:31:00" → "03/30 09:31".
function formatInspectedAt(iso: string | undefined | null): string {
  if (!iso) return "-";
  const m = iso.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  return m ? `${m[2]}/${m[3]} ${m[4]}:${m[5]}` : iso;
}

interface ResultBadgeProps {
  result: InspectionRecord["result"];
}

function ResultBadge({ result }: ResultBadgeProps) {
  if (result === "pass") {
    return (
      <span className="inline-flex items-center px-2 py-0.5 rounded-md text-xs font-semibold bg-green-100 text-green-700">
        OK
      </span>
    );
  }
  if (result === "fail") {
    return (
      <span className="inline-flex items-center px-2 py-0.5 rounded-md text-xs font-semibold bg-red-100 text-red-700">
        NG
      </span>
    );
  }
  return <span className="text-gray-400 text-xs">-</span>;
}

interface Props {
  inspections: InspectionRecord[];
  selectedId?: string | null;
  onSelect?: (insp: InspectionRecord) => void;
}

export function RecentInspectionsTable({ inspections, selectedId, onSelect }: Props) {
  // 슬라이스는 한 번만, 렌더 사이 안정성.
  const rows = useMemo(() => inspections.slice(0, MAX_ROWS), [inspections]);

  return (
    <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm h-full flex flex-col">
      <div className="flex items-center gap-2 mb-3">
        <div className="w-8 h-8 rounded-lg bg-gray-100 flex items-center justify-center">
          <ClipboardList className="w-4 h-4 text-gray-600" />
        </div>
        <h2 className="text-xl font-bold text-gray-900">최근 검사 이력</h2>
        <span className="ml-auto text-xs text-gray-500 font-medium">
          {rows.length} / {Math.min(inspections.length, MAX_ROWS)} 건
        </span>
      </div>

      <div className="overflow-hidden rounded-lg border border-gray-100">
        <table className="w-full text-sm table-fixed">
          <colgroup>
            <col className="w-12" />
            <col className="w-28" />
            <col />
            <col className="w-14" />
            <col className="w-20" />
            <col className="w-28" />
            <col />
          </colgroup>
          <thead className="bg-gray-50 text-xs text-gray-600">
            <tr>
              <th className="px-2 py-2 text-center">이미지</th>
              <th className="px-2 py-2 text-left">검사 시각</th>
              <th className="px-2 py-2 text-left"></th>
              <th className="px-2 py-2 text-center">결과</th>
              <th className="px-2 py-2 text-left">불량 유형</th>
              <th className="px-2 py-2 text-left">카메라</th>
              <th className="px-2 py-2 text-left">비고</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((insp) => {
              const product = insp.productId
                ? (PRODUCT_NAMES[insp.productId] ?? insp.productId)
                : "-";
              const icon = insp.result === "pass" ? "📷" : insp.result === "fail" ? "⚠" : "·";
              const isSelected = selectedId === insp.id;
              return (
                <tr
                  key={insp.id}
                  onClick={() => onSelect?.(insp)}
                  className={`border-t border-gray-100 cursor-pointer transition-colors ${
                    isSelected
                      ? "bg-blue-100 hover:bg-blue-100"
                      : "even:bg-gray-50/50 hover:bg-blue-50/60"
                  }`}
                >
                  <td className="px-2 py-1.5 text-center">{icon}</td>
                  <td className="px-2 py-1.5 whitespace-nowrap text-gray-700 tabular-nums">
                    {formatInspectedAt(insp.inspectedAt)}
                  </td>
                  <td className="px-2 py-1.5 truncate text-gray-800" title={product}>
                    {product}
                  </td>
                  <td className="px-2 py-1.5 text-center">
                    <ResultBadge result={insp.result} />
                  </td>
                  <td className="px-2 py-1.5 truncate text-gray-700" title={insp.defectType ?? ""}>
                    {insp.defectType ?? "-"}
                  </td>
                  <td className="px-2 py-1.5 truncate text-gray-700" title={insp.inspectorId ?? ""}>
                    {insp.inspectorId ?? "-"}
                  </td>
                  <td className="px-2 py-1.5 truncate text-gray-500" title={insp.defectDetail ?? ""}>
                    {insp.defectDetail ?? ""}
                  </td>
                </tr>
              );
            })}
            {rows.length === 0 && (
              <tr>
                <td colSpan={7} className="px-2 py-6 text-center text-gray-400 text-sm">
                  최근 검사 이력이 없습니다.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
