from fastapi import APIRouter

from . import courses, daily_limit, skills, users

INTERNAL_ROUTERS: list[APIRouter] = [module.router for module in [courses, daily_limit, skills, users]]
