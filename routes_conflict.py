from typing import Optional

from fastapi import APIRouter, HTTPException

from models import (
    ConflictDetectionRequest,
    ConflictDetectionReportStatus,
    ConflictSuggestionAcceptRequest,
)
from conflict_scheduler import (
    detect_conflicts,
    accept_suggestion,
    get_conflict_report,
    get_conflict_reports,
)

router = APIRouter(prefix="/api/conflict-detection", tags=["跨塔吊作业冲突检测"])


@router.post("", summary="提交工单进行冲突检测")
def api_detect_conflicts(req: ConflictDetectionRequest):
    if not req.orders:
        raise HTTPException(status_code=400, detail="工单列表不能为空")

    report = detect_conflicts(req.orders, req.submitter)

    return {
        "code": 0,
        "message": f"冲突检测完成，共发现 {report.conflict_count} 个潜在冲突点",
        "report": report,
    }


@router.get("/history", summary="查询历史检测报告列表")
def api_list_reports(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    status: Optional[ConflictDetectionReportStatus] = None,
):
    reports = get_conflict_reports(start_date, end_date, status)
    return {
        "code": 0,
        "total": len(reports),
        "reports": reports,
    }


@router.get("/{report_id}", summary="查询检测报告详情")
def api_get_report(report_id: str):
    report = get_conflict_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail=f"检测报告 {report_id} 不存在")
    return {
        "code": 0,
        "report": report,
    }


@router.post("/{report_id}/conflicts/{conflict_id}/suggestions/{suggestion_id}/accept",
             summary="接受某条建议并自动调整工单")
def api_accept_suggestion(
    report_id: str,
    conflict_id: str,
    suggestion_id: str,
    req: ConflictSuggestionAcceptRequest,
):
    result = accept_suggestion(
        report_id=report_id,
        conflict_id=conflict_id,
        suggestion_id=suggestion_id,
        accepted_by=req.accepted_by,
        remarks=req.remarks,
    )

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return {
        "code": 0,
        "message": result["message"],
        "adjustment": result["adjustment"],
        "report": result["report"],
    }


@router.get("/{report_id}/conflicts", summary="查询报告中的所有冲突点")
def api_get_report_conflicts(report_id: str):
    report = get_conflict_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail=f"检测报告 {report_id} 不存在")
    return {
        "code": 0,
        "report_id": report_id,
        "total_conflicts": report.conflict_count,
        "resolved_conflicts": report.resolved_conflict_count,
        "conflicts": report.conflicts,
    }


@router.get("/{report_id}/conflicts/{conflict_id}", summary="查询单个冲突点详情")
def api_get_conflict_detail(report_id: str, conflict_id: str):
    report = get_conflict_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail=f"检测报告 {report_id} 不存在")

    conflict = None
    for c in report.conflicts:
        if c.conflict_id == conflict_id:
            conflict = c
            break

    if not conflict:
        raise HTTPException(status_code=404, detail=f"冲突点 {conflict_id} 不存在")

    return {
        "code": 0,
        "report_id": report_id,
        "conflict": conflict,
    }


@router.get("/{report_id}/conflicts/{conflict_id}/suggestions",
            summary="查询冲突点的所有处理建议")
def api_get_conflict_suggestions(report_id: str, conflict_id: str):
    report = get_conflict_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail=f"检测报告 {report_id} 不存在")

    conflict = None
    for c in report.conflicts:
        if c.conflict_id == conflict_id:
            conflict = c
            break

    if not conflict:
        raise HTTPException(status_code=404, detail=f"冲突点 {conflict_id} 不存在")

    return {
        "code": 0,
        "report_id": report_id,
        "conflict_id": conflict_id,
        "total_suggestions": len(conflict.suggestions),
        "suggestions": conflict.suggestions,
    }
