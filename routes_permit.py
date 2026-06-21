from typing import Optional

from fastapi import APIRouter, HTTPException

from models import (
    WorkPermitApplyRequest,
    WorkPermitExtensionRequest,
    WorkPermitExtensionApproveRequest,
    WorkPermitStatus,
)
from work_permit import (
    apply_work_permit,
    get_permit_status,
    request_extension,
    approve_extension,
    get_permit_history,
    get_permit,
)

router = APIRouter(prefix="/api/work-permit", tags=["作业许可"])


@router.post("/apply", summary="申请当日作业许可证")
def api_apply_permit(req: WorkPermitApplyRequest):
    result = apply_work_permit(req.crane_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/crane/{crane_id}/status", summary="查询塔吊当前许可状态")
def api_get_permit_status(crane_id: str):
    result = get_permit_status(crane_id)
    return result


@router.get("/history", summary="查询许可证历史记录")
def api_get_permit_history(
    crane_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    status: Optional[WorkPermitStatus] = None,
    limit: int = 200,
):
    permits = get_permit_history(
        crane_id=crane_id,
        start_date=start_date,
        end_date=end_date,
        status=status,
        limit=limit,
    )
    return {
        "total": len(permits),
        "permits": permits,
    }


@router.get("/{permit_id}", summary="查询单个许可证详情")
def api_get_permit(permit_id: str):
    permit = get_permit(permit_id)
    if not permit:
        raise HTTPException(status_code=404, detail=f"作业许可证 {permit_id} 不存在")
    return permit


@router.post("/extension/request", summary="申请许可证延期")
def api_request_extension(req: WorkPermitExtensionRequest):
    result = request_extension(req.crane_id, req.requested_by, req.requested_expiry)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/extension/approve", summary="审批许可证延期申请")
def api_approve_extension(req: WorkPermitExtensionApproveRequest):
    result = approve_extension(
        permit_id=req.permit_id,
        approved=req.approved,
        reviewed_by=req.reviewed_by,
        review_remark=req.review_remark or "",
        approved_expiry=req.approved_expiry,
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result
