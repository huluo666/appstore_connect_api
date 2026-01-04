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
    description="查询 App Store 应用状态 (多账号自动映射版)",
    version="2.0.0"
)

# --- 私钥缓存 (启动时从环境变量加载) ---
PRIVATE_KEYS = {
    "ACCOUNT1": os.getenv("PRIVATE_KEY_ACCOUNT1", "").replace("\\n", "\n"),
    "ACCOUNT2": os.getenv("PRIVATE_KEY_ACCOUNT2", "").replace("\\n", "\n"),
    # 如果有第三个账号，继续添加
    # "ACCOUNT3": os.getenv("PRIVATE_KEY_ACCOUNT3", "").replace("\\n", "\n"),
}

# --- 应用配置映射 (根据 Bundle ID 自动选择账号) ---
APP_CONFIGS = {
    "com.opalive.ios": {
        "name": "4part",
        "key_id": "6UTNX28TTA",
        "issuer_id": "54b0385b-4752-4e0f-b50e-e2873b621c62",
        "private_key": "ACCOUNT1"  # 使用 PRIVATE_KEYS 中的 key
    },
    "com.lami.ios": {
        "name": "Nova Live",
        "key_id": "8S8K59Y236",
        "issuer_id": "bfd37f46-f3ac-4364-a30e-c4ee9975b4cf",
        "private_key": "ACCOUNT2"
    },
    # 继续添加新应用就往这里加
    # "com.yourapp.xxx": {
    #     "name": "Your App Name",
    #     "key_id": "XXXXXXXXXX",
    #     "issuer_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    #     "private_key": "ACCOUNT1"  # 可以复用已有账号
    # },
}

# --- 请求会话优化 ---
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
    account_name: Optional[str] = None

class HealthResponse(BaseModel):
    status: str
    message: str
    supported_apps: int

class AppListResponse(BaseModel):
    total: int
    apps: list

# --- 工具函数 ---

def get_app_config(bundle_id: str) -> Dict[str, str]:
    """根据 Bundle ID 获取应用配置"""
    config = APP_CONFIGS.get(bundle_id)
    if not config:
        raise ValueError(f"未配置的 Bundle ID: {bundle_id}")
    return config

def get_private_key(key_name: str) -> str:
    """获取私钥"""
    private_key = PRIVATE_KEYS.get(key_name)
    if not private_key:
        raise ValueError(f"私钥 {key_name} 未配置或为空，请检查环境变量 PRIVATE_KEY_{key_name}")
    return private_key

def generate_token(key_id: str, issuer_id: str, private_key: str) -> str:
    """生成 JWT token"""
    payload = {
        "iss": issuer_id,
        "iat": int(time.time()),
        "exp": int(time.time()) + 900,  # 15分钟有效期
        "aud": "appstoreconnect-v1"
    }
    
    try:
        return jwt.encode(
            payload,
            private_key,
            algorithm="ES256",
            headers={"kid": key_id}
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
    return {
        "status": "healthy",
        "message": "App Store Status API is online",
        "supported_apps": len(APP_CONFIGS)
    }

@app.get("/ping")
async def ping():
    """轻量级保活接口（给外部定时任务用）"""
    return {"status": "pong", "timestamp": int(time.time())}

@app.get("/apps", response_model=AppListResponse)
async def list_apps():
    """列出所有支持的应用"""
    apps = [
        {
            "bundle_id": bundle_id,
            "name": config["name"],
            "key_id": config["key_id"]
        }
        for bundle_id, config in APP_CONFIGS.items()
    ]
    return {
        "total": len(apps),
        "apps": apps
    }

@app.get("/status/{bundle_id}", response_model=AppStatusResponse)
async def get_app_status(bundle_id: str):
    """查询指定应用的状态（自动选择对应账号）"""
    base_url = "https://api.appstoreconnect.apple.com/v1"
    
    try:
        # 1. 根据 Bundle ID 获取配置
        try:
            config = get_app_config(bundle_id)
        except ValueError as e:
            raise HTTPException(
                status_code=404,
                detail=f"{str(e)}. 支持的 Bundle ID: {', '.join(APP_CONFIGS.keys())}"
            )
        
        # 2. 获取该应用对应的私钥
        try:
            private_key = get_private_key(config["private_key"])
        except ValueError as e:
            raise HTTPException(
                status_code=500,
                detail=f"配置错误: {str(e)}"
            )
        
        # 3. 生成 JWT Token
        token = generate_token(
            config["key_id"],
            config["issuer_id"],
            private_key
        )
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        # 4. 通过 Bundle ID 查找 App
        app_req = session.get(
            f"{base_url}/apps",
            params={"filter[bundleId]": bundle_id},
            headers=headers,
            timeout=15
        )
        
        if app_req.status_code != 200:
            err_msg = get_apple_error_detail(app_req)
            raise HTTPException(
                status_code=app_req.status_code,
                detail=f"查找 App 失败: {err_msg}"
            )

        apps_data = app_req.json().get('data', [])
        if not apps_data:
            raise HTTPException(
                status_code=404,
                detail=f"Apple 后台未找到 Bundle ID: {bundle_id}"
            )
            
        app_obj = apps_data[0]
        app_id = app_obj['id']
        app_name = app_obj['attributes']['name']
        
        # 5. 获取版本信息
        version_url = f"{base_url}/apps/{app_id}/appStoreVersions"
        ver_req = session.get(version_url, headers=headers, timeout=15)
        
        if ver_req.status_code != 200:
            err_msg = get_apple_error_detail(ver_req)
            raise HTTPException(
                status_code=ver_req.status_code,
                detail=f"获取版本失败: {err_msg}"
            )

        versions_list = ver_req.json().get('data', [])
        if not versions_list:
            raise HTTPException(
                status_code=404,
                detail="该 App 尚未创建任何版本"
            )
            
        # 手动排序获取最新版本
        latest_version = versions_list[0]['attributes']
        
        # 状态映射
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
            "created_date": latest_version.get('createdDate', ''),
            "account_name": config["name"]
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"服务器内部错误: {str(e)}"
        )

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

"""
=== Render 环境变量配置 ===

在 Render Dashboard 设置以下环境变量：

PRIVATE_KEY_ACCOUNT1=-----BEGIN PRIVATE KEY-----
MIGTAgEAMBMG...（账号1的完整私钥）
-----END PRIVATE KEY-----

PRIVATE_KEY_ACCOUNT2=-----BEGIN PRIVATE KEY-----
MIHcAgEBBEIB...（账号2的完整私钥）
-----END PRIVATE KEY-----

# 如果有第三个账号
PRIVATE_KEY_ACCOUNT3=...

注意：
1. 私钥可以直接粘贴多行内容，Render 支持
2. 或者使用 \n 转义：-----BEGIN PRIVATE KEY-----\nMIG...\n-----END PRIVATE KEY-----
3. 每个账号的私钥对应一个环境变量

=== 使用方式 ===

# 查询应用状态（自动选择对应账号）
GET /status/com.opalive.ios

# 列出所有支持的应用
GET /apps

# 健康检查
GET /

=== 添加新应用 ===

只需在 APP_CONFIGS 字典中添加新条目：
1. 如果是新账号，先添加对应的 PRIVATE_KEY_ACCOUNTX 环境变量
2. 在 APP_CONFIGS 中添加应用配置
3. 重新部署 Render 服务
"""