from typing import Optional

from fastapi import APIRouter, HTTPException

from models import (
    InspectionReportCreate,
    InspectionItemResult,
    HazardSeverity,
    HazardStatus,
    HazardAcceptRequest,
    HazardSubmitRequest,
    HazardReviewRequest,
)
from inspection import (
    get_standard_inspection_items,
    create_inspection_report,
    get_inspection_report,
    get_inspection_history,
    assign_hazard_responsibility,
    accept_hazard,
    submit_hazard_rectification,
    review_hazard,
    get_hazard,
    get_hazards,
    check_overdue_hazards,
    get_crane_hazard_stats,
)

router = APIRouter(prefix="/api/inspection", tags=["塔吊安全巡检与隐患管理"])


@router.get("/items/standard", summary="获取标准检查项模板")
def api_get_standard_items():
    items = get_standard_inspection_items()
    return {
        "code": 0,
        "total": len(items),
        "items": items,
    }


@router.post("/reports", summary="提交巡检报告")
def api_create_inspection_report(create: InspectionReportCreate):
    result = create_inspection_report(create)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {
        "code": 0,
        "message": result["message"],
        "report": result["report"],
        "created_hazards_count": result["created_hazards_count"],
        "created_hazards": result["created_hazards"],
    }


@router.get("/reports/{report_id}", summary="查询单个巡检报告详情")
def api_get_inspection_report(report_id: str):
    report = get_inspection_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail=f"巡检报告 {report_id} 不存在")
    report_hazards = get_hazards(crane_id=report.crane_id, include_closed=True)
    source_hazards = [h for h in report_hazards if h.source_report_id == report_id]
    return {
        "code": 0,
        "report": report,
        "related_hazards": source_hazards,
    }


@router.get("/reports", summary="查询巡检历史(按日期范围和塔吊筛选)")
def api_get_inspection_history(
    crane_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 100,
):
    reports = get_inspection_history(crane_id=crane_id, start_date=start_date, end_date=end_date)
    return {
        "code": 0,
        "total": len(reports),
        "reports": reports[:limit],
    }


@router.post("/hazards/{hazard_id}/assign", summary="指定隐患整改责任人和期限")
def api_assign_hazard_responsibility(
    hazard_id: str,
    responsible_person: str,
    deadline: Optional[float] = None,
):
    result = assign_hazard_responsibility(hazard_id, responsible_person, deadline)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {
        "code": 0,
        "message": "责任人和期限已指定",
        "hazard": result["hazard"],
    }


@router.post("/hazards/{hazard_id}/accept", summary="责任人接单确认")
def api_accept_hazard(hazard_id: str, request: HazardAcceptRequest):
    result = accept_hazard(hazard_id, request.responsible_person)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {
        "code": 0,
        "message": result["message"],
        "hazard": result["hazard"],
    }


@router.post("/hazards/{hazard_id}/submit", summary="提交整改完成说明")
def api_submit_hazard_rectification(hazard_id: str, request: HazardSubmitRequest):
    result = submit_hazard_rectification(hazard_id, request.rectification_remark)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {
        "code": 0,
        "message": result["message"],
        "hazard": result["hazard"],
    }


@router.post("/hazards/{hazard_id}/review", summary="安全员复查")
def api_review_hazard(hazard_id: str, request: HazardReviewRequest):
    result = review_hazard(
        hazard_id,
        request.reviewer,
        request.passed,
        request.review_remark,
        request.reject_reason,
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {
        "code": 0,
        "message": result["message"],
        "hazard": result["hazard"],
    }


@router.get("/hazards/{hazard_id}", summary="查询单个隐患详情")
def api_get_hazard(hazard_id: str):
    hazard = get_hazard(hazard_id)
    if not hazard:
        raise HTTPException(status_code=404, detail=f"隐患 {hazard_id} 不存在")
    return {
        "code": 0,
        "hazard": hazard,
    }


@router.get("/hazards", summary="查询隐患列表(按状态和严重程度筛选)")
def api_get_hazards(
    crane_id: Optional[str] = None,
    status: Optional[HazardStatus] = None,
    severity: Optional[HazardSeverity] = None,
    include_closed: bool = True,
    is_overdue: Optional[bool] = None,
    limit: int = 200,
):
    hazards = get_hazards(
        crane_id=crane_id,
        status=status,
        severity=severity,
        include_closed=include_closed,
        is_overdue=is_overdue,
    )
    return {
        "code": 0,
        "total": len(hazards),
        "hazards": hazards[:limit],
    }


@router.post("/hazards/check-overdue", summary="手动触发超期隐患检查")
def api_check_overdue_hazards():
    check_overdue_hazards()
    overdue_hazards = get_hazards(include_closed=False, is_overdue=True)
    return {
        "code": 0,
        "message": "超期隐患检查完成",
        "overdue_count": len(overdue_hazards),
        "overdue_hazards": overdue_hazards,
    }


@router.get("/stats/cranes", summary="获取各塔吊隐患统计")
def api_get_crane_hazard_stats(crane_id: Optional[str] = None):
    check_overdue_hazards()
    stats = get_crane_hazard_stats(crane_id)
    return {
        "code": 0,
        "total": len(stats),
        "stats": stats,
    }
