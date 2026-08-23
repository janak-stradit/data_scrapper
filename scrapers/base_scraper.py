"""Base scraper class with shared utilities."""
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import List, Dict, Any


class BaseScraper(ABC):
    """Abstract base for all platform scrapers."""

    def __init__(self, platform: str):
        self.platform = platform

    @abstractmethod
    async def scrape(self, identifier: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Scrape posts from the platform. Must be implemented by subclasses."""
        pass


    async def _run_actor(
        self,
        client,
        actor_id: str,
        run_input: Dict[str, Any],
        timeout_secs: int,
        memory_mbytes: int = None,
    ) -> List[Dict[str, Any]]:
        """Run an Apify actor and return its default dataset items.

        Handles both apify-client 1.x (``wait_secs`` + dict run) and
        3.x (``wait_duration`` + ``Run`` model).
        """
        actor = client.actor(actor_id)
        opts = {"memory_mbytes": memory_mbytes} if memory_mbytes else {}
        try:
            run = await actor.call(
                run_input=run_input,
                wait_duration=timedelta(seconds=timeout_secs),
                **opts,
            )
        except TypeError:
            run = await actor.call(run_input=run_input, wait_secs=timeout_secs, **opts)

        if run is None:
            raise RuntimeError(f"Actor {actor_id} did not finish within {timeout_secs}s")

        dataset_id = (
            run.get("defaultDatasetId")
            if isinstance(run, dict)
            else getattr(run, "default_dataset_id", None)
        )
        status = run.get("status") if isinstance(run, dict) else getattr(run, "status", None)
        if not dataset_id:
            raise RuntimeError(f"Actor {actor_id} returned no dataset (status={status})")

        page = await client.dataset(dataset_id).list_items()
        return list(page.items)

    def _error_response(self, error_message: str) -> List[Dict[str, Any]]:
        """Standardized error response."""
        return [{
            "platform": self.platform,
            "error": True,
            "message": error_message,
            "posts": [],
            "scraped_at": datetime.utcnow().isoformat() + "Z",
        }]

    def _format_post(
        self,
        rank: int,
        post_url: str,
        text: str,
        author: str,
        published_at: str = None,
        engagement: Dict = None,
        media: List = None,
        extra: Dict = None,
    ) -> Dict[str, Any]:
        """Standardized post format across all platforms."""
        post = {
            "platform": self.platform,
            "rank": rank,
            "post_url": post_url,
            "text": text,
            "author": author,
            "published_at": published_at,
            "engagement": engagement or {},
            "media": media or [],
            "scraped_at": datetime.utcnow().isoformat() + "Z",
        }
        if extra:
            post.update(extra)
        return post
