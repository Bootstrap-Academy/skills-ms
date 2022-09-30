from fastapi import APIRouter

from . import skills


INTERNAL_ROUTERS: list[APIRouter] = [module.router for module in [skills]]
