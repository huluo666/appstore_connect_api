#!/usr/bin/env python3
import os
import time
import requests
import jwt
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = FastAPI(
    title="App Store Connect API",
    description="查询 App Store 应用状态 (优化版)",
    version="1.1.0"
)

# --- 配置管理 ---
KEY_ID = os.getenv("APP_STORE_KEY_ID")
ISSUER_ID = os.getenv("APP_STORE_ISSUER_ID")
# 处理私钥可能存在的转义字符问题
PRIVATE_KEY = os.getenv("APP_STORE_PRIVATE_KEY", "").replace("\\n", "\n")

# --- 请求会话优化 (增加重试机制) ---
session = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retries))

# --- 模型定义 ---
class AppStatusResponse(BaseModel):
    app_name: str
    bundle_id: str
    version: str
    status: str
    status_cn: str
    platform: str
    created_date: str

class HealthResponse(BaseModel):
    status: str
    message: str

# --- 核心逻辑 ---

def generate_token() -> str:
    """生成 JWT token，增加对私钥格式的验证"""
    if not all([KEY_ID, ISSUER_ID, PRIVATE_KEY]):
        raise ValueError("缺少必要的 APP_STORE 环境变量配置")
        
    payload = {
        "iss": ISSUER_ID,
        "iat": int(time.time()),
        "exp": int(time.time()) + 1200, # 20分钟有效期
        "aud": "appstoreconnect-v1"
    }
    
    try:
        return jwt.encode(
            payload,
            PRIVATE_KEY,
            algorithm="ES256",
            headers={"kid": KEY_ID}
        )
    except Exception as e:
        raise RuntimeError(f"JWT 加密失败，请检查私钥格式是否正确: {str(e)}")

@app.get("/", response_model=HealthResponse)
async def root():
    return {"status": "healthy", "message": "Service is running"}

@app.get("/status/{bundle_id}", response_model=AppStatusResponse)
async def get_app_status(bundle_id: str):
    base_url = "https://api.appstoreconnect.apple.com/v1"
    
    try:
        token = generate_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        # 1. 获取 App ID
        # 增加对 bundleId 过滤结果的检查
        app_req = session.get(f"{base_url}/apps", params={"filter[bundleId]": bundle_id}, headers=headers, timeout=15)
        app_req.raise_for_status()
        
        apps_data = app_req.json().get('data', [])
        if not apps_data:
            raise HTTPException(status_code=404, detail=f"Bundle ID '{bundle_id}' 不存在或无权访问")
            
        app_obj = apps_data[0]
        app_id = app_obj['id']
        app_name = app_obj['attributes']['name']
        
        # 2. 获取版本信息
        # 优化点：明确请求字段，减少数据传输量
        version_url = f"{base_url}/apps/{app_id}/appStoreVersions"
        version_params = {"sort": "-createdDate", "limit": "1"}
        
        ver_req = session.get(version_url, params=version_params, headers=headers, timeout=15)
        
        # 处理 Apple 可能返回的详细错误 (避免模糊的 400 报错)
        if ver_req.status_code != 200:
            error_info = ver_req.json().get("errors", [{}])[0]
            error_detail = error_info.get("detail", "未知错误")
            raise HTTPException(status_code=ver_req.status_code, detail=f"Apple API 报错: {error_detail}")

        versions = ver_req.json().get('data', [])
        if not versions:
            raise HTTPException(status_code=404, detail="该应用尚未创建任何 App Store 版本")
            
        latest = versions[0]['attributes']
        
        status_map = {
            "READY_FOR_SALE": "已上架",
            "IN_REVIEW": "审核中",
            "WAITING_FOR_REVIEW": "等待审核",
            "PENDING_DEVELOPER_RELEASE": "等待开发者发布",
            "REJECTED": "被拒绝",
            "PREPARE_FOR_SUBMISSION": "准备提交",
            "DEVELOPER_REJECTED": "开发者撤回",
            "REMOVED_FROM_SALE": "已下架"
        }
        
        return {
            "app_name": app_name,
            "bundle_id": bundle_id,
            "version": latest.get('versionString', 'N/A'),
            "status": latest.get('appStoreState', 'UNKNOWN'),
            "status_cn": status_map.get(latest.get('appStoreState'), "其他状态"),
            "platform": latest.get('platform', 'ios'),
            "created_date": latest.get('createdDate', '')
        }

    except ValueError as ve:
        raise HTTPException(status_code=500, detail=str(ve))
    except requests.exceptions.RequestException as re:
        # 捕获所有请求异常（超时、连接错误等）
        detail = "连接 Apple 服务器超时" if isinstance(re, requests.exceptions.Timeout) else f"网络请求失败: {str(re)}"
        raise HTTPException(status_code=502, detail=detail)
    except Exception as e:
        # 防止抛出未处理的系统异常
        raise HTTPException(status_code=500, detail=f"系统内部错误: {str(e)}")

# --- 全局异常拦截 (生产环境建议隐藏具体堆栈) ---
@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "detail": exc.detail}
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))