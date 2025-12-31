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
    description="查询 App Store 应用状态 (稳定版)",
    version="1.2.0"
)

# --- 配置管理 ---
KEY_ID = os.getenv("APP_STORE_KEY_ID")
ISSUER_ID = os.getenv("APP_STORE_ISSUER_ID")
# 自动处理私钥换行符，防止 JWT 加密报错
PRIVATE_KEY = os.getenv("APP_STORE_PRIVATE_KEY", "").replace("\\n", "\n")

# --- 请求会话优化 (增加重试机制) ---
session = requests.Session()
retries = Retry(total=2, backoff_factor=1, status_forcelist=[502, 503, 504])
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

# --- 工具函数 ---

def generate_token() -> str:
    """生成 JWT token"""
    if not all([KEY_ID, ISSUER_ID, PRIVATE_KEY]):
        raise ValueError("环境变量配置不全 (KEY_ID/ISSUER_ID/PRIVATE_KEY)")
        
    payload = {
        "iss": ISSUER_ID,
        "iat": int(time.time()),
        "exp": int(time.time()) + 900, # 15分钟有效期
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
        raise RuntimeError(f"JWT 生成失败: {str(e)}")

def get_apple_error_detail(response: requests.Response) -> str:
    """提取 Apple API 返回的具体错误信息"""
    try:
        data = response.json()
        errors = data.get("errors", [])
        if errors:
            return errors[0].get("detail", response.text)
    except:
        pass
    return response.text

# --- 路由接口 ---

@app.get("/", response_model=HealthResponse)
async def root():
    return {"status": "healthy", "message": "App Store Status API is online"}

@app.get("/status/{bundle_id}", response_model=AppStatusResponse)
async def get_app_status(bundle_id: str):
    base_url = "https://api.appstoreconnect.apple.com/v1"
    
    try:
        token = generate_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        # 1. 通过 Bundle ID 查找 App
        app_req = session.get(
            f"{base_url}/apps", 
            params={"filter[bundleId]": bundle_id}, 
            headers=headers, 
            timeout=15
        )
        
        if app_req.status_code != 200:
            err_msg = get_apple_error_detail(app_req)
            raise HTTPException(status_code=app_req.status_code, detail=f"查找App失败: {err_msg}")

        apps_data = app_req.json().get('data', [])
        if not apps_data:
            raise HTTPException(status_code=404, detail=f"未找到 Bundle ID: {bundle_id}")
            
        app_obj = apps_data[0]
        app_id = app_obj['id']
        app_name = app_obj['attributes']['name']
        
        # 2. 获取版本信息 (彻底移除 sort 和 limit 参数，防止 400 错误)
        version_url = f"{base_url}/apps/{app_id}/appStoreVersions"
        ver_req = session.get(version_url, headers=headers, timeout=15)
        
        if ver_req.status_code != 200:
            err_msg = get_apple_error_detail(ver_req)
            # 这里抛出的 detail 会包含 Apple 为什么要报 400 的真正原因
            raise HTTPException(status_code=ver_req.status_code, detail=f"获取版本失败: {err_msg}")

        versions_list = ver_req.json().get('data', [])
        if not versions_list:
            raise HTTPException(status_code=404, detail="该 App 尚未创建任何版本")
            
        # 手动排序获取最新版本（按创建时间倒序）
        # Apple 默认返回通常也是按时间排的，取第一个通常即为最新
        latest_version = versions_list[0]['attributes']
        
        status_map = {
            "READY_FOR_SALE": "已上架",
            "IN_REVIEW": "审核中",
            "WAITING_FOR_REVIEW": "等待审核",
            "PENDING_DEVELOPER_RELEASE": "等待开发者发布",
            "REJECTED": "被拒绝",
            "PREPARE_FOR_SUBMISSION": "准备提交",
            "DEVELOPER_REJECTED": "开发者撤回",
            "REMOVED_FROM_SALE": "已下架",
            "METADATA_REJECTED": "元数据被拒"
        }
        
        state = latest_version.get('appStoreState', 'UNKNOWN')
        
        return {
            "app_name": app_name,
            "bundle_id": bundle_id,
            "version": latest_version.get('versionString', 'N/A'),
            "status": state,
            "status_cn": status_map.get(state, state),
            "platform": latest_version.get('platform', 'IOS'),
            "created_date": latest_version.get('createdDate', '')
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"服务器内部错误: {str(e)}")

# --- 异常处理 ---
@app.exception_handler(Exception)
async def universal_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"success": False, "detail": str(exc)}
    )

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)