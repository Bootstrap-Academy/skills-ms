from pydantic import ConfigDict

from api.redis import auth_redis
from api.schemas import BaseModel


class User(BaseModel):
    id: str
    email_verified: bool
    admin: bool


class UserAccessTokenData(BaseModel):
    email_verified: bool
    admin: bool

    model_config = ConfigDict(extra="ignore")


class UserAccessToken(BaseModel):
    uid: str
    rt: str
    data: UserAccessTokenData

    model_config = ConfigDict(extra="ignore")

    def to_user(self) -> User:
        return User(id=self.uid, **self.data.model_dump())

    async def is_revoked(self) -> bool:
        return bool(await auth_redis.exists(f"access_token_invalidated:{self.rt}"))
