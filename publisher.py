"""Meta Page publishing adapter. Supports Page feed and photos, not video."""
import os
from pathlib import Path
import httpx

class PublishingError(RuntimeError):
    pass

class FacebookPagePublisher:
    """Only handles authorized Page publishing. Secrets never appear in responses."""
    def __init__(self, brand: str):
        suffix = {"bigdawg": "BIGDAWG", "curiosity": "CURIOSITY"}.get(brand)
        if suffix is None:
            raise PublishingError("Unsupported brand")
        self.page_id = os.getenv(f"META_PAGE_ID_{suffix}", "")
        self.token = os.getenv(f"META_PAGE_TOKEN_{suffix}", "")
        self.version = os.getenv("META_GRAPH_API_VERSION", "v27.0")
        if not self.version.startswith("v") or not self.version[1:].replace(".", "").isdigit():
            raise PublishingError("Invalid Graph API version")
        if not self.page_id or not self.token:
            raise PublishingError("Missing Meta Page ID/token for this brand")

    def publish(self, message: str, media: Path | None = None) -> str:
        if media is not None and media.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise PublishingError("Automatic Facebook publishing currently supports text and photos only")
        edge = "photos" if media is not None else "feed"
        url = f"https://graph.facebook.com/{self.version}/{self.page_id}/{edge}"
        try:
            with httpx.Client(timeout=55, follow_redirects=False) as client:
                if media:
                    with media.open("rb") as handle:
                        response = client.post(url, data={"message": message, "access_token": self.token},
                                               files={"source": (media.name, handle)})
                else:
                    response = client.post(url, data={"message": message, "access_token": self.token})
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError, OSError):
            raise PublishingError("Facebook request failed. Check Page permissions, token and Meta status.") from None
        post_id = data.get("post_id") or data.get("id")
        if not post_id:
            raise PublishingError("Facebook did not return a post identifier; verify the Page before retrying")
        return str(post_id)
