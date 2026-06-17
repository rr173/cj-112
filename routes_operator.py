from typing import Optional

from fastapi import APIRouter, HTTPException

from models import (
    OperatorCreate,
    OperatorUpdate,
    OperatorGrade,
    AssessmentCreate,
    ShiftHandoverRequest,
    OperatorBindRequest,
)
from collision import cranes_config
from operator_training import (
    create_operator,
    update_operator,
    get_operator,
    list_operators,
    record_assessment,
    get_operator_assessments,
    get_operator_qualification,
    bind_operator_to_crane,
    unbind_operator_from_crane,
    shift_handover,
    get_crane_attendance_timeline,
    get_crane_current_operator,
    get_handover_history,
)

router = APIRouter(prefix="/api/operators", tags=["操作员安全培训考核"])


@router.post("", summary="录入操作员信息")
def api_create_operator(create: OperatorCreate):
    result = create_operator(create)
    return {
        "code": 0,
        "message": result["message"],
        "operator": result["operator"],
    }


@router.put("/{operator_id}", summary="更新操作员信息")
def api_update_operator(operator_id: str, update: OperatorUpdate):
    result = update_operator(operator_id, update)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {
        "code": 0,
        "message": result["message"],
        "operator": result["operator"],
    }


@router.get("", summary="查询操作员列表(可按资质等级筛选)")
def api_list_operators(grade: Optional[OperatorGrade] = None):
    operators_list = list_operators(grade)
    return {
        "code": 0,
        "total": len(operators_list),
        "operators": operators_list,
    }


@router.get("/{operator_id}", summary="查询操作员详情")
def api_get_operator(operator_id: str):
    operator = get_operator(operator_id)
    if not operator:
        raise HTTPException(status_code=404, detail=f"操作员 {operator_id} 不存在")
    qualification = get_operator_qualification(operator_id)
    return {
        "code": 0,
        "operator": operator,
        "qualification": qualification.get("status"),
    }


@router.post("/{operator_id}/assessments", summary="录入操作员考核成绩")
def api_record_assessment(operator_id: str, create: AssessmentCreate):
    result = record_assessment(operator_id, create)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {
        "code": 0,
        "message": result["message"],
        "assessment": result["assessment"],
    }


@router.get("/{operator_id}/assessments", summary="查询操作员考核历史")
def api_get_assessments(operator_id: str, limit: int = 50):
    operator = get_operator(operator_id)
    if not operator:
        raise HTTPException(status_code=404, detail=f"操作员 {operator_id} 不存在")
    assessments = get_operator_assessments(operator_id)
    return {
        "code": 0,
        "operator_id": operator_id,
        "total": len(assessments),
        "assessments": assessments[-limit:],
    }


@router.get("/{operator_id}/qualification", summary="查询操作员资质状态")
def api_get_qualification(operator_id: str):
    operator = get_operator(operator_id)
    if not operator:
        raise HTTPException(status_code=404, detail=f"操作员 {operator_id} 不存在")
    qualification = get_operator_qualification(operator_id)
    return {
        "code": 0,
        "qualification": qualification.get("status"),
    }


@router.post("/cranes/{crane_id}/bind", summary="绑定操作员到塔吊")
def api_bind_operator(crane_id: str, req: OperatorBindRequest):
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    result = bind_operator_to_crane(crane_id, req.operator_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {
        "code": 0,
        "message": result["message"],
        "binding": result["binding"],
    }


@router.post("/cranes/{crane_id}/unbind", summary="解绑塔吊当前操作员")
def api_unbind_operator(crane_id: str):
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    result = unbind_operator_from_crane(crane_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {
        "code": 0,
        "message": result["message"],
    }


@router.post("/cranes/{crane_id}/handover", summary="操作员换班交接")
def api_shift_handover(crane_id: str, req: ShiftHandoverRequest):
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    result = shift_handover(
        crane_id, req.from_operator_id, req.to_operator_id, req.remarks
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {
        "code": 0,
        "message": result["message"],
        "handover": result["handover"],
    }


@router.get("/cranes/{crane_id}/current", summary="查询塔吊当前操作员")
def api_get_current_operator(crane_id: str):
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    current = get_crane_current_operator(crane_id)
    if not current:
        return {
            "code": 0,
            "has_operator": False,
            "message": f"塔吊 {crane_id} 当前无操作员在岗",
        }
    return {
        "code": 0,
        "has_operator": True,
        "operator": current["operator"],
        "binding": current["binding"],
        "qualification": current["qualification"],
    }


@router.get("/cranes/{crane_id}/attendance", summary="查询塔吊当天操作员出勤时间线")
def api_get_attendance(crane_id: str, date: Optional[str] = None):
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    result = get_crane_attendance_timeline(crane_id, date)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {
        "code": 0,
        "crane_id": result["crane_id"],
        "date": result["date"],
        "total_segments": result["total_segments"],
        "segments": result["segments"],
    }


@router.get("/cranes/{crane_id}/handovers", summary="查询塔吊换班交接记录")
def api_get_handover_history(crane_id: str, limit: int = 50):
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    records = get_handover_history(crane_id, limit)
    return {
        "code": 0,
        "crane_id": crane_id,
        "total": len(records),
        "handovers": records,
    }
