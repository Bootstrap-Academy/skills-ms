"""Private browser assets authorized by short-lived, course-scoped package grants."""

import mimetypes
from pathlib import PurePosixPath

from fastapi import APIRouter, Response
from redis.exceptions import RedisError

from api.services.private_lesson_modules import asset_redirect

router = APIRouter()


@router.api_route("/lesson-assets/{grant}/{artifact}/{asset:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def get_asset(grant: str, artifact: str, asset: str) -> Response:
    # The private Nginx locations own the browser policy for both this response
    # and the internal file response (including Range and Nginx-generated errors).
    try:
        target = await asset_redirect(grant, artifact, asset)
    except (OSError, ValueError, KeyError, TypeError):
        return Response(status_code=404)
    except RedisError:
        return Response(status_code=503)
    suffix = PurePosixPath(asset).suffix.lower()
    media_type = "application/javascript" if suffix in (".js", ".mjs") else mimetypes.guess_type(asset)[0]
    return Response(headers={"X-Accel-Redirect": target}, media_type=media_type or "application/octet-stream")
