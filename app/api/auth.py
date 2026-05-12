"""JWT login and registration"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from datetime import datetime, timedelta
import jwt
from app.config import settings

router = APIRouter()

class LoginRequest(BaseModel):
    email: str
    password: str

class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"

@router.post("/register")
async def register(email: str, password: str):
    """Register a new user"""
    # TODO: Hash password and store in DB
    return {"message": "User created", "email": email}

@router.post("/login", response_model=LoginResponse)
async def login(request: LoginRequest):
    """Login and get JWT token"""
    # TODO: Verify credentials against DB
    
    payload = {
        "sub": request.email,
        "exp": datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    }
    
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return {"access_token": token}

@router.post("/logout")
async def logout():
    """Logout (token blacklist)"""
    return {"message": "Logged out"}

def get_current_user(token: str = Depends(lambda: None)) -> str:
    """Dependency to get current user from token"""
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload.get("sub")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
