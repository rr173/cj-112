from typing import Optional

from fastapi import APIRouter, HTTPException

from models import (
    DailyReportStatus,
    DailyReportGenerateRequest,
    DailyReportApproveRequest,
)
from daily_report import (
    generate_daily_reports,
    get_daily_reports,
    get_daily_report,
    approve_daily_report,
    generate_summary,
)

router = APIRouter(prefix="/api/reports", tags=["日报管理"])


@router.post("/daily/generate", summary="手动触发生成日报")
def api_generate_daily_reports(req: DailyReportGenerateRequest):
    result = generate_daily_reports(req.date, req.crane_id)
    return {
        "code": 0,
        "message": f"日报生成完成，成功生成 {result['generated_count']} 份，跳过 {result['skipped_count']} 份，已锁定不可覆盖 {result['locked_count']} 份",
        "data": result,
    }


@router.get("/daily", summary="查询日报列表")
def api_list_daily_reports(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    status: Optional[DailyReportStatus] = None,
    crane_id: Optional[str] = None,
):
    reports = get_daily_reports(start_date, end_date, status, crane_id)
    return {
        "code": 0,
        "total": len(reports),
        "reports": reports,
    }


@router.get("/daily/{report_id}", summary="查询单份日报详情")
def api_get_daily_report(report_id: str):
    report = get_daily_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail=f"日报 {report_id} 不存在")
    return {
        "code": 0,
        "report": report,
    }


@router.post("/daily/{report_id}/approve", summary="审批日报")
def api_approve_daily_report(report_id: str, req: DailyReportApproveRequest):
    result = approve_daily_report(report_id, req.action, req.approver, req.remarks)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {
        "code": 0,
        "message": f"审批成功，日报状态已更新为 {req.action}",
        "report": result["report"],
    }


@router.get("/summary", summary="导出日报摘要（按塔吊分组统计）")
def api_get_summary(start_date: str, end_date: str):
    if not start_date or not end_date:
        raise HTTPException(status_code=400, detail="start_date 和 end_date 均为必填")
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date 不能大于 end_date")

    summary = generate_summary(start_date, end_date)
    return {
        "code": 0,
        "summary": summary,
    }
