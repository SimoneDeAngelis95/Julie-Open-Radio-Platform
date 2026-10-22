"""Anonymous integration endpoints: no original paths, no cookies, no cache retention."""

import os
import re
from functools import wraps

from django.core.paginator import EmptyPage, Paginator
from django.http import (
    FileResponse,
    Http404,
    HttpResponse,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404
from django.urls import reverse

from .publication import public_candidates, replay_exists
from .site_config import cors, public_url
from .storage import data_path


def public_endpoint(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.method == "OPTIONS":
            response = HttpResponse(status=204)
        elif request.method not in ["GET", "HEAD"]:
            response = HttpResponse(status=405, headers={"Allow": "GET, HEAD, OPTIONS"})
        else:
            try:
                response = view(request, *args, **kwargs)
            except (Http404, OSError):
                response = JsonResponse(
                    {"error": "Contenuto non disponibile."}, status=404
                )
            if request.method == "HEAD":
                if not response.streaming and "Content-Length" not in response:
                    response["Content-Length"] = len(response.content)
                if response.streaming:
                    response.close()
                headers = dict(response.headers)
                response = HttpResponse(status=response.status_code, headers=headers)
        response["Cache-Control"] = "no-store, max-age=0"
        cors(response, request)
        response["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
        response["Access-Control-Allow-Headers"] = "Range"
        response["Access-Control-Expose-Headers"] = (
            "Content-Length, Content-Range, Accept-Ranges"
        )
        return response

    return wrapped


def payload(request, asset):
    return {
        "id": str(asset.id),
        "title": asset.title,
        "artist": asset.artist,
        "description": asset.description,
        "duration_seconds": asset.duration_seconds,
        "first_broadcast_at": asset.first_broadcast_at.isoformat()
        if asset.first_broadcast_at
        else None,
        "published_at": asset.published_at.isoformat(),
        "audio_url": public_url(
            request, reverse("library:public_audio", args=[asset.id])
        ),
        "url": public_url(request, reverse("library:public_podcast", args=[asset.id])),
    }


@public_endpoint
def podcasts(request):
    try:
        page_number = int(request.GET.get("page", "1"))
        page_size = int(request.GET.get("page_size", "20"))
        if page_number < 1 or not 1 <= page_size <= 100:
            raise ValueError
    except ValueError:
        return JsonResponse(
            {"error": "page deve essere positivo; page_size da 1 a 100."}, status=400
        )
    assets = [
        asset
        for asset in public_candidates().order_by("-published_at", "id")
        if replay_exists(asset)
    ]
    paginator = Paginator(assets, page_size)
    try:
        page = paginator.page(page_number)
    except EmptyPage:
        raise Http404 from None

    def page_url(number):
        return (
            public_url(request, reverse("library:public_podcasts"))
            + f"?page={number}&page_size={page_size}"
        )

    return JsonResponse(
        {
            "count": paginator.count,
            "next": page_url(page.next_page_number()) if page.has_next() else None,
            "previous": page_url(page.previous_page_number())
            if page.has_previous()
            else None,
            "results": [payload(request, asset) for asset in page],
        }
    )


@public_endpoint
def podcast(request, asset_id):
    asset = get_object_or_404(public_candidates(), pk=asset_id)
    if not replay_exists(asset):
        raise Http404
    return JsonResponse(payload(request, asset))


class FileSlice:
    def __init__(self, source, remaining):
        self.source, self.remaining = source, remaining

    def __iter__(self):
        return self

    def __next__(self):
        if self.remaining <= 0:
            self.close()
            raise StopIteration
        chunk = self.source.read(min(65536, self.remaining))
        if not chunk:
            self.close()
            raise StopIteration
        self.remaining -= len(chunk)
        return chunk

    def close(self):
        self.source.close()


@public_endpoint
def audio(request, asset_id):
    asset = get_object_or_404(public_candidates(), pk=asset_id)
    if not replay_exists(asset):
        raise Http404
    source = data_path(asset.replay_path).open("rb")
    size = os.fstat(source.fileno()).st_size
    # Range applies to GET; HEAD describes the complete representation.
    header = request.headers.get("Range") if request.method == "GET" else None
    if header:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
        try:
            if not match or not any(match.groups()):
                raise ValueError
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(int(last), size - 1) if last else size - 1
            else:
                count = int(last)
                if count <= 0:
                    raise ValueError
                start = max(0, size - count)
                end = size - 1
            if start >= size or end < start:
                raise ValueError
        except ValueError:
            source.close()
            return HttpResponse(
                status=416, headers={"Content-Range": f"bytes */{size}"}
            )
        source.seek(start)
        response = StreamingHttpResponse(
            FileSlice(source, end - start + 1), status=206, content_type="audio/mpeg"
        )
        response["Content-Range"] = f"bytes {start}-{end}/{size}"
        response["Content-Length"] = end - start + 1
    elif request.method == "HEAD":
        source.close()
        response = HttpResponse(
            content_type="audio/mpeg", headers={"Content-Length": str(size)}
        )
    else:
        response = FileResponse(
            source, content_type="audio/mpeg", filename=f"podcast-{asset.id}.mp3"
        )
    response["Accept-Ranges"] = "bytes"
    response["Content-Disposition"] = f'inline; filename="podcast-{asset.id}.mp3"'
    return response
