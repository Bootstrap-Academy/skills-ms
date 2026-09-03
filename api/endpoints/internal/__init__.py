from fastapi import APIRouter

from . import courses, skills, users


INTERNAL_ROUTERS: list[APIRouter] = [module.router for module in [courses, skills, users]]
