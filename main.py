#!/usr/bin/env python3

#!/usr/bin/env python3
import os
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import jwt
import time
import requests
from typing import Optional

app = FastAPI(
	title="App Store Connect API",
	description="查询 App Store 应用状态",
	version="1.0.0"
)

# 从环境变量读取配置
KEY_ID = os.getenv("APP_STORE_KEY_ID")
ISSUER_ID = os.getenv("APP_STORE_ISSUER_ID")
PRIVATE_KEY = os.getenv("APP_STORE_PRIVATE_KEY")

# 响应模型
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
	
def generate_token():
	"""生成 JWT token"""
	if not all([KEY_ID, ISSUER_ID, PRIVATE_KEY]):
		raise HTTPException(
			status_code=500,
			detail="服务器配置错误：缺少必要的认证信息"
		)
		
	payload = {
		"iss": ISSUER_ID,
		"iat": int(time.time()),
		"exp": int(time.time()) + 1200,
		"aud": "appstoreconnect-v1"
	}
	
	try:
		return jwt.encode(
			payload,
			PRIVATE_KEY,
			algorithm="ES256",
			headers={"alg": "ES256", "kid": KEY_ID, "typ": "JWT"}
		)
	except Exception as e:
		raise HTTPException(
			status_code=500,
			detail=f"Token 生成失败: {str(e)}"
		)
		
def get_headers():
	return {
		"Authorization": f"Bearer {generate_token()}",
		"Content-Type": "application/json"
	}
	
@app.get("/", response_model=HealthResponse)
def root():
	"""健康检查端点"""
	return HealthResponse(
		status="healthy",
		message="App Store Connect API Service is running. Use /status/{bundle_id} to query app status."
	)
	
@app.get("/health", response_model=HealthResponse)
def health_check():
	"""Render 健康检查端点"""
	return HealthResponse(
		status="healthy",
		message="Service is operational"
	)
	
@app.get("/status/{bundle_id}", response_model=AppStatusResponse)
def get_app_status(bundle_id: str):
	"""
	查询指定 Bundle ID 的应用状态
	
	参数:
		bundle_id: 应用的 Bundle ID (例如: com.example.app)
	
	返回:
		应用的详细状态信息
	"""
	base_url = "https://api.appstoreconnect.apple.com/v1"
	
	try:
		# 1. 通过 Bundle ID 获取应用
		url = f"{base_url}/apps"
		params = {"filter[bundleId]": bundle_id}
		response = requests.get(url, headers=get_headers(), params=params, timeout=30)
		response.raise_for_status()
		
		apps = response.json().get('data', [])
		if not apps:
			raise HTTPException(
				status_code=404,
				detail=f"未找到 Bundle ID: {bundle_id}"
			)
			
		app = apps[0]
		app_id = app['id']
		app_name = app['attributes']['name']
		
		# 2. 获取最新版本
		url = f"{base_url}/apps/{app_id}/appStoreVersions"
		params = {"sort": "-createdDate", "limit": "1"}
		response = requests.get(url, headers=get_headers(), params=params, timeout=30)
		response.raise_for_status()
		
		versions = response.json().get('data', [])
		if not versions:
			raise HTTPException(
				status_code=404,
				detail="暂无版本信息"
			)
			
		latest = versions[0]['attributes']
		
		# 状态映射
		status_map = {
			"READY_FOR_SALE": "已上架",
			"IN_REVIEW": "审核中",
			"WAITING_FOR_REVIEW": "等待审核",
			"PENDING_DEVELOPER_RELEASE": "等待开发者发布",
			"REJECTED": "被拒绝",
			"PREPARE_FOR_SUBMISSION": "准备提交",
			"PENDING_APPLE_RELEASE": "等待苹果发布",
			"DEVELOPER_REJECTED": "开发者撤回",
			"METADATA_REJECTED": "元数据被拒",
			"REMOVED_FROM_SALE": "已下架",
			"INVALID_BINARY": "二进制文件无效"
		}
		
		return AppStatusResponse(
			app_name=app_name,
			bundle_id=bundle_id,
			version=latest['versionString'],
			status=latest['appStoreState'],
			status_cn=status_map.get(latest['appStoreState'], latest['appStoreState']),
			platform=latest['platform'],
			created_date=latest['createdDate']
		)
	
	except requests.exceptions.Timeout:
		raise HTTPException(
			status_code=504,
			detail="请求超时，请稍后重试"
		)
	except requests.exceptions.HTTPError as e:
		status_code = e.response.status_code
		if status_code == 401:
			detail = "认证失败，请检查 API 密钥配置"
		elif status_code == 403:
			detail = "权限不足，请检查 API 密钥权限"
		else:
			detail = f"API 请求失败: {str(e)}"
		raise HTTPException(status_code=status_code, detail=detail)
	except HTTPException:
		raise
	except Exception as e:
		raise HTTPException(
			status_code=500,
			detail=f"服务器内部错误: {str(e)}"
		)
		
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
	"""全局异常处理"""
	return JSONResponse(
		status_code=500,
		content={"detail": f"未预期的错误: {str(exc)}"}
	)
	
if __name__ == "__main__":
	import uvicorn
	port = int(os.getenv("PORT", 10000))
	uvicorn.run(
		app,
		host="0.0.0.0",
		port=port,
		log_level="info"
	)