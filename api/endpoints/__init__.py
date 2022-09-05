from fastapi import APIRouter

from . import course, skill


ROUTERS: dict[str, tuple[APIRouter, str | None]] = {
    module.router.tags[0]: (module.router, module.__doc__) for module in [skill, course]
}
